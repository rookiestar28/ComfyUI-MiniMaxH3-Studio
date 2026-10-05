"""Provider-free Full-Reference security and evaluation gates.

This module evaluates the accepted Full-Reference ``ContextPlan`` through the canonical renderer
and linter, then records separate security, privacy, resource, cancellation, oracle, and fixed-H3
lanes. It never opens media, calls a provider, launches ComfyUI, or emits the plan/prompt payload
in its public result. A missing required lane fails closed; optional comparison lanes remain
explicitly not requested or blocked and never imply official equivalence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from .base_evaluation import (
    EvaluationMeasurement,
    EvaluationRoute,
    FixedH3Receipt,
    FixedH3Settings,
    FixedH3Status,
    OracleComparison,
    OracleComparisonMethod,
    OracleComparisonStatus,
)
from .canonical import binary64_token, canonical_fingerprint
from .context_reporting import ContextPlan, PromptDocument, ProviderOutcome
from .contracts import (
    PromptProfile,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import ContractValidationError, PromptRenderingError
from .failure_containment import ContainmentResult, ContainmentStatus
from .linting import PromptLintResult, lint_prompt
from .rendering import full_reference_declared_asset_ids, render_full_reference_prompt
from .resource_scheduling import ResourceExecutionResult, ResourceExecutionStatus

FULL_REFERENCE_EVALUATION_SCHEMA = "h3.full_reference.evaluation.v1"
MAX_FULL_REFERENCE_EVALUATION_CASES = 64
MAX_FULL_REFERENCE_EVALUATION_DIAGNOSTICS = 512
MAX_FULL_REFERENCE_EVALUATION_LIMITATIONS = 64
MAX_FULL_REFERENCE_EVALUATION_FINGERPRINTS = 64

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REFERENCE_LABEL_PATTERN = re.compile(r"<(Subject|Picture|Video|Audio) [1-9][0-9]*>")
_ASSET_LABEL = re.compile(r"<(?:Picture|Video|Audio) [1-9][0-9]*>")
_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "cookie",
    "credential",
    "file://",
    "http://",
    "https://",
    "password",
    "secret",
    "signed",
    "sig=",
    "token=",
)


class FullReferenceEvaluationStatus(str, Enum):
    """Per-case and aggregate status; only PASSED is an accepted gate result."""

    PASSED = "passed"
    FAILED = "failed"


class FullReferenceLane(str, Enum):
    """Evidence lanes kept independent so one cannot mask another."""

    STRUCTURAL = "structural"
    HARD_CONSTRAINT = "hard_constraint"
    SECURITY = "security"
    PRIVACY = "privacy"
    RESOURCE = "resource"
    CANCELLATION = "cancellation"
    ORACLE = "oracle"
    FIXED_H3 = "fixed_h3"


class FullReferenceLaneStatus(str, Enum):
    """Outcome for a named lane."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_REQUESTED = "not_requested"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


_REQUIRED_LANES = (
    FullReferenceLane.STRUCTURAL,
    FullReferenceLane.HARD_CONSTRAINT,
    FullReferenceLane.SECURITY,
    FullReferenceLane.PRIVACY,
    FullReferenceLane.RESOURCE,
    FullReferenceLane.CANCELLATION,
)
_ALL_LANES = tuple(FullReferenceLane)


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise ContractValidationError(f"{field_name} contains sensitive material")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a SHA-256 fingerprint")
    return value


def _fingerprints(values: object, field_name: str) -> tuple[str, ...]:
    if (
        not isinstance(values, tuple)
        or not values
        or len(values) > MAX_FULL_REFERENCE_EVALUATION_FINGERPRINTS
    ):
        raise ContractValidationError(f"{field_name} must contain one to 64 fingerprints")
    result = tuple(_fingerprint(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise ContractValidationError(f"{field_name} must not contain duplicates")
    return result


def _codes(values: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ContractValidationError(f"{field_name} must be a bounded tuple")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise ContractValidationError(f"{field_name} must not contain duplicates")
    return result


def _labels(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 128:
        raise ContractValidationError(f"{field_name} must be a bounded tuple")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or _REFERENCE_LABEL_PATTERN.fullmatch(value) is None:
            raise ContractValidationError(f"{field_name} contains an invalid label")
        result.append(value)
    result_tuple = tuple(result)
    if len(result_tuple) != len(set(result_tuple)):
        raise ContractValidationError(f"{field_name} must not contain duplicates")
    return result_tuple


def _fingerprintable(value: object) -> object:
    """Encode raw floats before passing typed metadata to the strict canonicalizer."""

    if isinstance(value, float):
        return binary64_token(value)
    if isinstance(value, Mapping):
        return {key: _fingerprintable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fingerprintable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FullReferenceSecurityEvidence:
    """Expected containment outcomes for representative and hostile source fragments."""

    results: tuple[ContainmentResult, ...]
    expected_statuses: tuple[ContainmentStatus, ...]
    schema: str = FULL_REFERENCE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        if (
            not isinstance(self.results, tuple)
            or not self.results
            or len(self.results) > 128
            or not all(isinstance(item, ContainmentResult) for item in self.results)
        ):
            raise ContractValidationError("security results must be a bounded non-empty tuple")
        if (
            not isinstance(self.expected_statuses, tuple)
            or len(self.expected_statuses) != len(self.results)
            or not all(isinstance(item, ContainmentStatus) for item in self.expected_statuses)
        ):
            raise ContractValidationError("security expected statuses must match results")
        if self.schema != FULL_REFERENCE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Full-Reference evaluation schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "result_count": len(self.results),
            "statuses": [item.status.value for item in self.results],
            "expected_statuses": [item.value for item in self.expected_statuses],
        }


@dataclass(frozen=True, slots=True)
class FullReferenceRuntimeEvidence:
    """Redacted M6-06 receipt and the expected terminal outcome for this case."""

    result: ResourceExecutionResult
    expected_statuses: tuple[ResourceExecutionStatus, ...]
    cancellation_required: bool = False
    schema: str = FULL_REFERENCE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.result, ResourceExecutionResult):
            raise ContractValidationError("runtime result must be a ResourceExecutionResult")
        if (
            not isinstance(self.expected_statuses, tuple)
            or not self.expected_statuses
            or len(self.expected_statuses) > 8
            or not all(isinstance(item, ResourceExecutionStatus) for item in self.expected_statuses)
        ):
            raise ContractValidationError("runtime expected statuses must be bounded and non-empty")
        if len(self.expected_statuses) != len(set(self.expected_statuses)):
            raise ContractValidationError("runtime expected statuses must not contain duplicates")
        if not isinstance(self.cancellation_required, bool):
            raise ContractValidationError("cancellation_required must be a boolean")
        if self.schema != FULL_REFERENCE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Full-Reference evaluation schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.result.status.value,
            "expected_statuses": [item.value for item in self.expected_statuses],
            "cancellation_required": self.cancellation_required,
            "reservation_released": self.result.receipt.reservation_released,
        }


@dataclass(frozen=True, slots=True)
class FullReferenceEvaluationCase:
    """One Full-Reference plan plus explicit security and runtime evidence."""

    case_id: str
    plan: ContextPlan
    security: FullReferenceSecurityEvidence
    runtime: FullReferenceRuntimeEvidence
    baseline_plan: ContextPlan | None = None
    route: EvaluationRoute = EvaluationRoute.DETERMINISTIC
    measurement: EvaluationMeasurement = field(default_factory=lambda: EvaluationMeasurement(0.0))
    source_fingerprints: tuple[str, ...] = ()
    oracle: OracleComparison | None = None
    fixed_h3: FixedH3Receipt | None = None
    schema: str = FULL_REFERENCE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.case_id, "evaluation case_id")
        if not isinstance(self.plan, ContextPlan):
            raise ContractValidationError("evaluation plan must be a ContextPlan")
        if (
            self.plan.request.profile.name is not PromptProfile.FULL_REFERENCE
            or self.plan.request.task_mode is not TaskMode.REF2VA
        ):
            raise ContractValidationError("Full-Reference evaluation requires a ref2va plan")
        if self.baseline_plan is not None and not isinstance(self.baseline_plan, ContextPlan):
            raise ContractValidationError("baseline_plan must be a ContextPlan or None")
        if not isinstance(self.route, EvaluationRoute):
            raise ContractValidationError("route must be an EvaluationRoute")
        if not isinstance(self.measurement, EvaluationMeasurement):
            raise ContractValidationError("measurement must be an EvaluationMeasurement")
        if not isinstance(self.security, FullReferenceSecurityEvidence):
            raise ContractValidationError("security must be FullReferenceSecurityEvidence")
        if not isinstance(self.runtime, FullReferenceRuntimeEvidence):
            raise ContractValidationError("runtime evidence is required for the evaluation gate")
        _fingerprints(self.source_fingerprints, "source_fingerprints")
        if self.oracle is not None and not isinstance(self.oracle, OracleComparison):
            raise ContractValidationError("oracle must be an OracleComparison or None")
        if self.fixed_h3 is not None:
            if type(self.fixed_h3) is not FixedH3Receipt:
                raise ContractValidationError("fixed_h3 must be a FixedH3Receipt or None")
            self.fixed_h3.require_admitted()
        if self.schema != FULL_REFERENCE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Full-Reference evaluation schema")

    @property
    def reference_labels(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.plan.request.reference_registry.labels)

    def to_wire(self) -> dict[str, object]:
        """Return redacted case metadata; plans and raw security fragments stay private."""

        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "route": self.route.value,
            "task_mode": self.plan.request.task_mode.value,
            "source_fingerprints": list(self.source_fingerprints),
            "reference_labels": list(self.reference_labels),
            "security": self.security.to_wire(),
            "runtime": self.runtime.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class FullReferenceEvaluationResult:
    """Separate lane result with safe fingerprints and optional comparison receipts."""

    result_id: str
    case_id: str
    route: EvaluationRoute
    status: FullReferenceEvaluationStatus
    lane_statuses: Mapping[str, FullReferenceLaneStatus]
    structural_valid: bool
    hard_constraints_preserved: bool
    evidence_total: int
    evidence_referenced: int
    evidence_coverage: str
    prompt_fingerprint: str | None
    reference_labels: tuple[str, ...]
    diagnostic_codes: tuple[str, ...]
    diagnostics: tuple[ValidationDiagnostic, ...]
    measurement: EvaluationMeasurement
    source_fingerprints: tuple[str, ...]
    oracle: OracleComparison
    fixed_h3: FixedH3Receipt
    limitations: tuple[str, ...]
    schema: str = FULL_REFERENCE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.result_id, "result_id")
        _identifier(self.case_id, "case_id")
        if not isinstance(self.route, EvaluationRoute):
            raise ContractValidationError("result route must be an EvaluationRoute")
        if not isinstance(self.status, FullReferenceEvaluationStatus):
            raise ContractValidationError("result status must be a FullReferenceEvaluationStatus")
        if set(self.lane_statuses) != {lane.value for lane in _ALL_LANES}:
            raise ContractValidationError("result lane_statuses must contain every evaluation lane")
        if not all(
            isinstance(value, FullReferenceLaneStatus) for value in self.lane_statuses.values()
        ):
            raise ContractValidationError("result lane_statuses contain an invalid status")
        if not isinstance(self.structural_valid, bool) or not isinstance(
            self.hard_constraints_preserved, bool
        ):
            raise ContractValidationError("structural and hard-constraint flags must be booleans")
        for value, field_name in (
            (self.evidence_total, "evidence_total"),
            (self.evidence_referenced, "evidence_referenced"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{field_name} must be a non-negative integer")
        if self.evidence_referenced > self.evidence_total:
            raise ContractValidationError("evidence_referenced cannot exceed evidence_total")
        coverage = Decimal(self.evidence_coverage)
        if not coverage.is_finite() or not Decimal("0") <= coverage <= Decimal("1"):
            raise ContractValidationError("evidence_coverage must be between 0 and 1")
        if self.prompt_fingerprint is not None:
            _fingerprint(self.prompt_fingerprint, "prompt_fingerprint")
        _labels(self.reference_labels, "reference_labels")
        _codes(self.diagnostic_codes, "diagnostic_codes", MAX_FULL_REFERENCE_EVALUATION_DIAGNOSTICS)
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > MAX_FULL_REFERENCE_EVALUATION_DIAGNOSTICS
        ):
            raise ContractValidationError("diagnostics must be bounded")
        if not all(isinstance(item, ValidationDiagnostic) for item in self.diagnostics):
            raise ContractValidationError("diagnostics must contain ValidationDiagnostic values")
        if self.diagnostic_codes != tuple(item.code for item in self.diagnostics):
            raise ContractValidationError("diagnostic_codes must match diagnostics")
        if not isinstance(self.measurement, EvaluationMeasurement):
            raise ContractValidationError("measurement must be an EvaluationMeasurement")
        _fingerprints(self.source_fingerprints, "source_fingerprints")
        if not isinstance(self.oracle, OracleComparison):
            raise ContractValidationError("oracle must be an OracleComparison")
        if type(self.fixed_h3) is not FixedH3Receipt:
            raise ContractValidationError("fixed_h3 must be a FixedH3Receipt")
        self.fixed_h3.require_admitted()
        _codes(self.limitations, "limitations", MAX_FULL_REFERENCE_EVALUATION_LIMITATIONS)
        if self.status is FullReferenceEvaluationStatus.PASSED and any(
            self.lane_statuses[lane.value]
            not in {FullReferenceLaneStatus.PASSED, FullReferenceLaneStatus.NOT_APPLICABLE}
            for lane in _REQUIRED_LANES
        ):
            raise ContractValidationError("passed result requires every required lane to pass")
        if self.status is FullReferenceEvaluationStatus.FAILED and not self.diagnostics:
            raise ContractValidationError("failed result requires diagnostics")
        if self.schema != FULL_REFERENCE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Full-Reference evaluation schema")

    def to_wire(self) -> dict[str, object]:
        if type(self.fixed_h3) is not FixedH3Receipt:
            raise ContractValidationError("fixed_h3 must be a FixedH3Receipt")
        self.fixed_h3.require_admitted()
        return {
            "schema": self.schema,
            "result_id": self.result_id,
            "case_id": self.case_id,
            "route": self.route.value,
            "status": self.status.value,
            "lane_statuses": {key: value.value for key, value in self.lane_statuses.items()},
            "structural_valid": self.structural_valid,
            "hard_constraints_preserved": self.hard_constraints_preserved,
            "evidence_total": self.evidence_total,
            "evidence_referenced": self.evidence_referenced,
            "evidence_coverage": self.evidence_coverage,
            "prompt_fingerprint": self.prompt_fingerprint,
            "reference_labels": list(self.reference_labels),
            "diagnostic_codes": list(self.diagnostic_codes),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "measurement": self.measurement.to_wire(),
            "source_fingerprints": list(self.source_fingerprints),
            "oracle": self.oracle.to_wire(),
            "fixed_h3": self.fixed_h3.to_wire(),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class FullReferenceEvaluationCorpus:
    """Bounded ordered Full-Reference case collection."""

    corpus_id: str
    cases: tuple[FullReferenceEvaluationCase, ...]
    schema: str = FULL_REFERENCE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.corpus_id, "corpus_id")
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_FULL_REFERENCE_EVALUATION_CASES
            or not all(isinstance(item, FullReferenceEvaluationCase) for item in self.cases)
        ):
            raise ContractValidationError("evaluation corpus cases must be bounded and non-empty")
        ids = tuple(item.case_id for item in self.cases)
        if len(ids) != len(set(ids)):
            raise ContractValidationError("evaluation case IDs must be unique")
        if self.schema != FULL_REFERENCE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Full-Reference evaluation schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "corpus_id": self.corpus_id,
            "cases": [item.to_wire() for item in self.cases],
        }


@dataclass(frozen=True, slots=True)
class FullReferenceEvaluationReport:
    """Aggregate report whose gate requires an observed pass for every required lane."""

    report_id: str
    corpus_id: str
    status: FullReferenceEvaluationStatus
    results: tuple[FullReferenceEvaluationResult, ...]
    lane_summary: Mapping[str, Mapping[str, int]]
    limitations: tuple[str, ...]
    schema: str = FULL_REFERENCE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.report_id, "report_id")
        _identifier(self.corpus_id, "corpus_id")
        if not isinstance(self.status, FullReferenceEvaluationStatus):
            raise ContractValidationError("report status must be a FullReferenceEvaluationStatus")
        if not isinstance(self.results, tuple) or not self.results:
            raise ContractValidationError("evaluation report results must be non-empty")
        if not all(isinstance(item, FullReferenceEvaluationResult) for item in self.results):
            raise ContractValidationError("evaluation report results must be typed")
        if len({item.result_id for item in self.results}) != len(self.results):
            raise ContractValidationError("evaluation result IDs must be unique")
        if set(self.lane_summary) != {lane.value for lane in _ALL_LANES}:
            raise ContractValidationError("lane_summary must contain every evaluation lane")
        expected_counts = {"case_count", *(status.value for status in FullReferenceLaneStatus)}
        for lane, summary in self.lane_summary.items():
            if not isinstance(summary, Mapping) or set(summary) != expected_counts:
                raise ContractValidationError(f"lane_summary bucket {lane!r} is invalid")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in summary.values()
            ):
                raise ContractValidationError("lane_summary counts must be non-negative integers")
            if summary["case_count"] != sum(
                summary[status.value] for status in FullReferenceLaneStatus
            ):
                raise ContractValidationError("lane_summary count does not match outcomes")
        _codes(self.limitations, "limitations", MAX_FULL_REFERENCE_EVALUATION_LIMITATIONS)
        if self.status is FullReferenceEvaluationStatus.PASSED and not self.is_gate_passed:
            raise ContractValidationError("passed report must satisfy every required lane")
        if self.schema != FULL_REFERENCE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Full-Reference evaluation schema")

    @property
    def is_gate_passed(self) -> bool:
        for lane in _REQUIRED_LANES:
            summary = self.lane_summary[lane.value]
            if (
                summary[FullReferenceLaneStatus.PASSED.value] <= 0
                or summary[FullReferenceLaneStatus.FAILED.value] > 0
                or summary[FullReferenceLaneStatus.NOT_REQUESTED.value] > 0
                or summary[FullReferenceLaneStatus.BLOCKED.value] > 0
            ):
                return False
        return all(result.status is FullReferenceEvaluationStatus.PASSED for result in self.results)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "report_id": self.report_id,
            "corpus_id": self.corpus_id,
            "status": self.status.value,
            "gate_passed": self.is_gate_passed,
            "results": [item.to_wire() for item in self.results],
            "lane_summary": {lane: dict(summary) for lane, summary in self.lane_summary.items()},
            "limitations": list(self.limitations),
        }


def _diagnostic(
    code: str,
    message: str,
    severity: ValidationSeverity = ValidationSeverity.ERROR,
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "full_reference_evaluation")


def _coverage(plan: ContextPlan, document: PromptDocument) -> tuple[int, int, str]:
    evidence_ids = {record.evidence_id for record in plan.evidence.records}
    referenced = set(document.source_evidence_ids)
    for section in document.sections:
        referenced.update(section.source_evidence_ids)
    referenced_count = len(evidence_ids.intersection(referenced))
    total = len(evidence_ids)
    if total == 0:
        return 0, 0, "1"
    ratio = (Decimal(referenced_count) / Decimal(total)).quantize(Decimal("0.000001"))
    return total, referenced_count, format(ratio, "f")


def _plan_snapshot(plan: ContextPlan) -> dict[str, object]:
    """Capture user-owned fields used to detect unauthorized mutation."""

    return {
        "user_intent": plan.request.user_intent,
        "hard_constraints": plan.hard_constraints.to_wire(),
        "assets": [item.to_wire() for item in plan.request.assets],
        "reference_registry": plan.request.reference_registry.to_wire(),
        "timeline": [item.to_wire() for item in plan.intent_graph.segments],
    }


def _default_oracle() -> OracleComparison:
    return OracleComparison(
        "oracle.not_requested",
        OracleComparisonMethod.FINGERPRINT,
        limitation="official-oracle comparison was not requested",
    )


def _resolve_oracle(
    oracle: OracleComparison | None, prompt_fingerprint: str | None
) -> OracleComparison:
    if oracle is None:
        return _default_oracle()
    if oracle.official_recording is None:
        return oracle
    if prompt_fingerprint is None:
        return replace(
            oracle,
            status=OracleComparisonStatus.UNAVAILABLE,
            local_fingerprint=None,
            difference_codes=("oracle.local_prompt_unavailable",),
        )
    if oracle.official_recording.outcome is not ProviderOutcome.SUCCEEDED:
        return replace(
            oracle,
            status=OracleComparisonStatus.UNAVAILABLE,
            local_fingerprint=prompt_fingerprint,
            difference_codes=("oracle.recording_not_successful",),
        )
    if oracle.method is OracleComparisonMethod.FINGERPRINT:
        if prompt_fingerprint == oracle.official_recording.output_fingerprint:
            return replace(
                oracle,
                status=OracleComparisonStatus.MATCHED,
                local_fingerprint=prompt_fingerprint,
                difference_codes=(),
            )
        return replace(
            oracle,
            status=OracleComparisonStatus.DIFFERENT,
            local_fingerprint=prompt_fingerprint,
            difference_codes=("oracle.output_fingerprint_differs",),
        )
    return replace(
        oracle,
        status=OracleComparisonStatus.UNAVAILABLE,
        local_fingerprint=prompt_fingerprint,
        difference_codes=("oracle.structural_method_requires_review",),
    )


def _default_fixed_h3(case: FullReferenceEvaluationCase) -> FixedH3Receipt:
    settings = FixedH3Settings(
        "not_run.model",
        "not_run.host",
        canonical_fingerprint(
            {
                "schema": FULL_REFERENCE_EVALUATION_SCHEMA,
                "plan_id": case.plan.plan_id,
                "source_fingerprints": list(case.source_fingerprints),
            }
        ),
        0,
        "not_run",
        "not_run",
        float(case.plan.request.effective_duration_seconds),
        "not_run",
        "not_run",
        case.source_fingerprints,
    )
    return FixedH3Receipt(
        f"fixed.{case.case_id}",
        FixedH3Status.NOT_RUN,
        settings,
        redacted_message="fixed H3 execution was not run by the offline evaluator",
    )


def _security_lane(
    evidence: FullReferenceSecurityEvidence,
    diagnostics: list[ValidationDiagnostic],
) -> FullReferenceLaneStatus:
    for index, (result, expected) in enumerate(
        zip(evidence.results, evidence.expected_statuses, strict=True)
    ):
        if result.status is not expected:
            diagnostics.append(
                _diagnostic(
                    "security.containment_status",
                    f"containment result {index} did not match its declared terminal status",
                )
            )
        if any(fragment.trusted for fragment in result.fragments):
            diagnostics.append(
                _diagnostic(
                    "security.trusted_fragment",
                    "untrusted media/provider fragments cannot become trusted",
                )
            )
    return (
        FullReferenceLaneStatus.PASSED
        if not any(item.code.startswith("security.") for item in diagnostics)
        else FullReferenceLaneStatus.FAILED
    )


def _runtime_lane(
    evidence: FullReferenceRuntimeEvidence,
    diagnostics: list[ValidationDiagnostic],
) -> tuple[FullReferenceLaneStatus, FullReferenceLaneStatus]:
    result = evidence.result
    resource_ok = (
        result.status in evidence.expected_statuses and result.receipt.reservation_released
    )
    if not resource_ok:
        diagnostics.append(
            _diagnostic(
                "resource.unexpected_status",
                "resource execution did not produce the declared bounded terminal outcome",
            )
        )
    if evidence.cancellation_required:
        cancellation_ok = (
            result.status is ResourceExecutionStatus.CANCELLED
            and result.artifact is None
            and result.receipt.reservation_released
        )
        if not cancellation_ok:
            diagnostics.append(
                _diagnostic(
                    "cancellation.not_terminal",
                    "cancelled evaluation must release resources without an artifact",
                )
            )
        cancellation_status = (
            FullReferenceLaneStatus.PASSED if cancellation_ok else FullReferenceLaneStatus.FAILED
        )
    else:
        cancellation_status = FullReferenceLaneStatus.NOT_APPLICABLE
    return (
        FullReferenceLaneStatus.PASSED if resource_ok else FullReferenceLaneStatus.FAILED,
        cancellation_status,
    )


def _privacy_lane(
    case: FullReferenceEvaluationCase,
    oracle: OracleComparison,
    fixed_h3: FixedH3Receipt,
    diagnostics: list[ValidationDiagnostic],
) -> FullReferenceLaneStatus:
    """Scan only the redacted result material; raw fragments and prompt text are never emitted."""

    oracle_wire = oracle.to_wire()
    recording = oracle_wire.get("official_recording")
    if isinstance(recording, dict):
        # This is a bounded public label, not an authorization value. Credentials are not
        # representable by OfficialContextIRRecording and remain outside this projection.
        recording = dict(recording)
        recording.pop("authorization_label", None)
        oracle_wire["official_recording"] = recording
    material = json.dumps(
        {
            "schema": FULL_REFERENCE_EVALUATION_SCHEMA,
            "case": case.case_id,
            "source_fingerprints": list(case.source_fingerprints),
            "security_statuses": [item.status.value for item in case.security.results],
            "runtime": case.runtime.to_wire(),
            "oracle": oracle_wire,
            "fixed_h3": fixed_h3.to_wire(),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).casefold()
    if any(marker in material for marker in _SENSITIVE_MARKERS):
        diagnostics.append(
            _diagnostic(
                "privacy.redacted_material_leak",
                "evaluation metadata contains a forbidden locator or credential marker",
            )
        )
        return FullReferenceLaneStatus.FAILED
    return FullReferenceLaneStatus.PASSED


def evaluate_full_reference_case(
    case: FullReferenceEvaluationCase,
) -> FullReferenceEvaluationResult:
    """Render and audit one case without executing media, providers, models, or ComfyUI."""

    if not isinstance(case, FullReferenceEvaluationCase):
        raise ContractValidationError("case must be a FullReferenceEvaluationCase")
    diagnostics: list[ValidationDiagnostic] = []
    prompt_fingerprint: str | None = None
    document: PromptDocument | None = None
    evidence_total = 0
    evidence_referenced = 0
    evidence_coverage = "0"
    try:
        document = render_full_reference_prompt(case.plan)
        prompt_fingerprint = canonical_fingerprint(document.to_wire())
        lint_result: PromptLintResult = lint_prompt(case.plan, document)
        diagnostics.extend(
            ValidationDiagnostic(item.severity, item.code, item.message, item.location)
            for item in lint_result.diagnostics
        )
        evidence_total, evidence_referenced, evidence_coverage = _coverage(case.plan, document)
    except (PromptRenderingError, TypeError, ValueError):
        diagnostics.append(
            _diagnostic("evaluation.render_failed", "Full-Reference prompt rendering failed")
        )

    structural_valid = document is not None and not any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in diagnostics
    )
    labels = case.reference_labels
    if document is not None:
        declared_ids = full_reference_declared_asset_ids(case.plan)
        declared_labels = {
            item.label
            for item in case.plan.request.reference_registry.labels
            if item.asset_id in declared_ids
        }
        cited_labels = set(_ASSET_LABEL.findall(document.text))
        # GUARD: attached media is not a prompt role. Requiring every registry label manufactures
        # prose for unused inputs, while checking no labels lets a renderer drop a declared source.
        # Compare the typed declared/used projection and independently reject fabricated labels.
        if declared_labels - cited_labels:
            diagnostics.append(
                _diagnostic(
                    "reference.label_missing",
                    "a declared backend reference label is absent from the rendered prompt",
                )
            )
            structural_valid = False
        if cited_labels - set(labels):
            diagnostics.append(
                _diagnostic(
                    "reference.label_unknown",
                    "the rendered prompt cites a reference label the registry never assigned",
                )
            )
            structural_valid = False

    hard_constraints_preserved = True
    if case.baseline_plan is not None and _plan_snapshot(case.plan) != _plan_snapshot(
        case.baseline_plan
    ):
        hard_constraints_preserved = False
        diagnostics.append(
            _diagnostic(
                "evaluation.hard_constraint_mutation",
                "immutable user intent, hard constraints, assets, references, or timeline changed",
            )
        )
    if any(item.code.startswith("constraint.") for item in diagnostics):
        hard_constraints_preserved = False

    lane_statuses: dict[str, FullReferenceLaneStatus] = {
        FullReferenceLane.STRUCTURAL.value: (
            FullReferenceLaneStatus.PASSED if structural_valid else FullReferenceLaneStatus.FAILED
        ),
        FullReferenceLane.HARD_CONSTRAINT.value: (
            FullReferenceLaneStatus.PASSED
            if hard_constraints_preserved
            else FullReferenceLaneStatus.FAILED
        ),
    }
    lane_statuses[FullReferenceLane.SECURITY.value] = _security_lane(case.security, diagnostics)
    oracle = _resolve_oracle(case.oracle, prompt_fingerprint)
    fixed_h3 = case.fixed_h3 if case.fixed_h3 is not None else _default_fixed_h3(case)
    lane_statuses[FullReferenceLane.PRIVACY.value] = _privacy_lane(
        case, oracle, fixed_h3, diagnostics
    )
    resource_status, cancellation_status = _runtime_lane(case.runtime, diagnostics)
    lane_statuses[FullReferenceLane.RESOURCE.value] = resource_status
    lane_statuses[FullReferenceLane.CANCELLATION.value] = cancellation_status
    lane_statuses[FullReferenceLane.ORACLE.value] = (
        FullReferenceLaneStatus.NOT_REQUESTED
        if oracle.status is OracleComparisonStatus.NOT_REQUESTED
        else (
            FullReferenceLaneStatus.PASSED
            if oracle.status
            in {
                OracleComparisonStatus.MATCHED,
                OracleComparisonStatus.DIFFERENT,
            }
            else FullReferenceLaneStatus.BLOCKED
        )
    )
    lane_statuses[FullReferenceLane.FIXED_H3.value] = {
        FixedH3Status.NOT_RUN: FullReferenceLaneStatus.NOT_REQUESTED,
        FixedH3Status.PASSED: FullReferenceLaneStatus.PASSED,
        FixedH3Status.FAILED: FullReferenceLaneStatus.FAILED,
        FixedH3Status.BLOCKED: FullReferenceLaneStatus.BLOCKED,
    }[fixed_h3.status]

    limitations: list[str] = []
    if oracle.status is OracleComparisonStatus.NOT_REQUESTED:
        limitations.append("official_oracle_not_requested")
    elif oracle.status is not OracleComparisonStatus.MATCHED:
        limitations.append("official_oracle_does_not_prove_equivalence")
    if fixed_h3.status is FixedH3Status.NOT_RUN:
        limitations.append("fixed_h3_not_requested")
    if case.plan.limitations:
        limitations.append("timeline_contains_declared_limitations")
    required_statuses = tuple(lane_statuses[lane.value] for lane in _REQUIRED_LANES)
    status = (
        FullReferenceEvaluationStatus.PASSED
        if all(
            value in {FullReferenceLaneStatus.PASSED, FullReferenceLaneStatus.NOT_APPLICABLE}
            for value in required_statuses
        )
        else FullReferenceEvaluationStatus.FAILED
    )
    if status is FullReferenceEvaluationStatus.FAILED and not diagnostics:
        diagnostics.append(_diagnostic("evaluation.required_lane_failed", "required lane failed"))
    diagnostic_codes = tuple(item.code for item in diagnostics)
    result_material = {
        "schema": FULL_REFERENCE_EVALUATION_SCHEMA,
        "case_id": case.case_id,
        "route": case.route.value,
        "status": status.value,
        "lane_statuses": {key: value.value for key, value in lane_statuses.items()},
        "structural_valid": structural_valid,
        "hard_constraints_preserved": hard_constraints_preserved,
        "evidence_total": evidence_total,
        "evidence_referenced": evidence_referenced,
        "evidence_coverage": evidence_coverage,
        "prompt_fingerprint": prompt_fingerprint,
        "reference_labels": list(labels),
        "diagnostic_codes": list(diagnostic_codes),
        "measurement": case.measurement.to_wire(),
        "source_fingerprints": list(case.source_fingerprints),
        "oracle": oracle.to_wire(),
        "fixed_h3": fixed_h3.to_wire(),
        "limitations": limitations,
    }
    result_id = (
        "result_" + canonical_fingerprint(_fingerprintable(result_material)).split(":", 1)[1][:32]
    )
    return FullReferenceEvaluationResult(
        result_id,
        case.case_id,
        case.route,
        status,
        MappingProxyType(dict(lane_statuses)),
        structural_valid,
        hard_constraints_preserved,
        evidence_total,
        evidence_referenced,
        evidence_coverage,
        prompt_fingerprint,
        labels,
        diagnostic_codes,
        tuple(diagnostics),
        case.measurement,
        case.source_fingerprints,
        oracle,
        fixed_h3,
        tuple(limitations),
    )


def evaluate_full_reference_corpus(
    corpus: FullReferenceEvaluationCorpus,
) -> FullReferenceEvaluationReport:
    """Evaluate ordered cases and require every required lane to be observed and passing."""

    if not isinstance(corpus, FullReferenceEvaluationCorpus):
        raise ContractValidationError("corpus must be a FullReferenceEvaluationCorpus")
    results = tuple(evaluate_full_reference_case(case) for case in corpus.cases)
    summary: dict[str, dict[str, int]] = {
        lane.value: {
            "case_count": 0,
            **{status.value: 0 for status in FullReferenceLaneStatus},
        }
        for lane in _ALL_LANES
    }
    for result in results:
        for lane, status in result.lane_statuses.items():
            summary[lane]["case_count"] += 1
            summary[lane][status.value] += 1
    frozen_summary: Mapping[str, Mapping[str, int]] = MappingProxyType(
        {lane: MappingProxyType(values) for lane, values in summary.items()}
    )
    limitations = sorted({limitation for result in results for limitation in result.limitations})
    report_material = {
        "schema": FULL_REFERENCE_EVALUATION_SCHEMA,
        "corpus_id": corpus.corpus_id,
        "results": [result.to_wire() for result in results],
        "lane_summary": {lane: dict(values) for lane, values in frozen_summary.items()},
        "limitations": limitations,
    }
    report_id = (
        "report_" + canonical_fingerprint(_fingerprintable(report_material)).split(":", 1)[1][:32]
    )
    report_status = (
        FullReferenceEvaluationStatus.PASSED
        if all(result.status is FullReferenceEvaluationStatus.PASSED for result in results)
        and all(
            summary[lane.value][FullReferenceLaneStatus.PASSED.value] > 0
            and summary[lane.value][FullReferenceLaneStatus.FAILED.value] == 0
            and summary[lane.value][FullReferenceLaneStatus.NOT_REQUESTED.value] == 0
            and summary[lane.value][FullReferenceLaneStatus.BLOCKED.value] == 0
            for lane in _REQUIRED_LANES
        )
        else FullReferenceEvaluationStatus.FAILED
    )
    return FullReferenceEvaluationReport(
        report_id,
        corpus.corpus_id,
        report_status,
        results,
        frozen_summary,
        tuple(limitations),
    )


__all__ = [
    "FULL_REFERENCE_EVALUATION_SCHEMA",
    "MAX_FULL_REFERENCE_EVALUATION_CASES",
    "MAX_FULL_REFERENCE_EVALUATION_DIAGNOSTICS",
    "MAX_FULL_REFERENCE_EVALUATION_FINGERPRINTS",
    "MAX_FULL_REFERENCE_EVALUATION_LIMITATIONS",
    "FullReferenceEvaluationCase",
    "FullReferenceEvaluationCorpus",
    "FullReferenceEvaluationReport",
    "FullReferenceEvaluationResult",
    "FullReferenceEvaluationStatus",
    "FullReferenceLane",
    "FullReferenceLaneStatus",
    "FullReferenceRuntimeEvidence",
    "FullReferenceSecurityEvidence",
    "evaluate_full_reference_case",
    "evaluate_full_reference_corpus",
]
