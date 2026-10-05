"""Offline Base-mode structural, oracle, and fixed-H3 evaluation contracts.

Evaluation is intentionally metadata-driven: this module renders and lints a typed local plan, but
never launches ComfyUI, loads a model, opens media, calls a provider, or claims visual quality. The
three evidence layers remain separately named in every result and route summary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from math import isfinite
from types import MappingProxyType

from .canonical import canonical_fingerprint
from .context_reporting import ContextPlan, PromptDocument, ProviderOutcome
from .contracts import PromptProfile, ValidationDiagnostic, ValidationSeverity
from .errors import ContractValidationError, PromptRenderingError
from .linting import PromptLintResult, lint_prompt
from .official_context_ir_recording import OfficialContextIRRecording
from .rendering import render_base_prompt

BASE_EVALUATION_SCHEMA = "h3.base.evaluation.v1"
MAX_EVALUATION_CASES = 256
MAX_EVALUATION_FINGERPRINTS = 64
MAX_EVALUATION_DIAGNOSTICS = 512
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
    "https://",
    "http://",
    "file://",
    "/",
    "\\",
)


class EvaluationRoute(str, Enum):
    """Named execution lane; routes are never collapsed into one quality score."""

    DETERMINISTIC = "deterministic"
    ASSISTED = "assisted"
    OFFICIAL_PROVIDER = "official_provider"


class EvaluationStatus(str, Enum):
    """Structural evaluation status independent from oracle/fixed-generation status."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    BLOCKED = "blocked"


class FixedH3Status(str, Enum):
    """Metadata state for an explicitly pinned fixed-H3 generation lane."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class OracleComparisonStatus(str, Enum):
    """Sanitized optional official-oracle comparison state."""

    NOT_REQUESTED = "not_requested"
    MATCHED = "matched"
    DIFFERENT = "different"
    UNAVAILABLE = "unavailable"


class OracleComparisonMethod(str, Enum):
    """Declared oracle comparison method and its limitations."""

    FINGERPRINT = "fingerprint"
    STRUCTURAL = "structural"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ContractValidationError(f"{field_name} contains sensitive material")
    return value


def _metadata(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field_name} must be bounded metadata")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ContractValidationError(f"{field_name} contains sensitive material")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a SHA-256 fingerprint")
    return value


def _finite(value: object, field_name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ContractValidationError(f"{field_name} must be finite")
    result = float(value)
    if positive and result <= 0:
        raise ContractValidationError(f"{field_name} must be positive")
    if not positive and result < 0:
        raise ContractValidationError(f"{field_name} must be non-negative")
    return result


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{field_name} must be a non-negative integer")
    return value


def _fingerprint_tuple(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_EVALUATION_FINGERPRINTS:
        raise ContractValidationError(f"{field_name} must be a bounded tuple")
    result = tuple(_fingerprint(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise ContractValidationError(f"{field_name} must not contain duplicates")
    return result


def _fingerprintable(value: object) -> object:
    """Encode raw floats before passing typed wire values to the strict canonicalizer."""

    if isinstance(value, float):
        from .canonical import binary64_token

        return binary64_token(value)
    if isinstance(value, Mapping):
        return {key: _fingerprintable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fingerprintable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class EvaluationMeasurement:
    """Caller-supplied bounded timing/resource measurement; no clock is read implicitly."""

    latency_seconds: float
    peak_memory_bytes: int | None = None
    output_bytes: int | None = None
    schema: str = BASE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _finite(self.latency_seconds, "latency_seconds")
        for value, field_name in (
            (self.peak_memory_bytes, "peak_memory_bytes"),
            (self.output_bytes, "output_bytes"),
        ):
            if value is not None:
                _non_negative_int(value, field_name)
        if self.schema != BASE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Base evaluation schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "latency_seconds": self.latency_seconds,
            "peak_memory_bytes": self.peak_memory_bytes,
            "output_bytes": self.output_bytes,
        }


@dataclass(frozen=True, slots=True)
class FixedH3Settings:
    """Pinned generation settings; this value never launches the fixed-H3 lane."""

    model_revision: str
    host_revision: str
    workflow_fingerprint: str
    seed: int
    schedule: str
    resolution: str
    duration_seconds: float
    dtype: str
    device: str
    media_fingerprints: tuple[str, ...] = ()
    schema: str = BASE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.model_revision, "model_revision"),
            (self.host_revision, "host_revision"),
            (self.workflow_fingerprint, "workflow_fingerprint"),
            (self.schedule, "schedule"),
            (self.resolution, "resolution"),
            (self.dtype, "dtype"),
            (self.device, "device"),
            (self.schema, "schema"),
        ):
            if type(value) is not str:
                raise ContractValidationError(f"{field_name} must be an exact string")
        _metadata(self.model_revision, "model_revision")
        _metadata(self.host_revision, "host_revision")
        _fingerprint(self.workflow_fingerprint, "workflow_fingerprint")
        if type(self.seed) is not int or not -(2**63) <= self.seed <= 2**63 - 1:
            raise ContractValidationError("seed must be a signed 64-bit integer")
        _metadata(self.schedule, "schedule")
        _metadata(self.resolution, "resolution")
        if type(self.duration_seconds) not in {int, float}:
            raise ContractValidationError("duration_seconds must be an exact finite number")
        _finite(self.duration_seconds, "duration_seconds", positive=True)
        _metadata(self.dtype, "dtype")
        _metadata(self.device, "device")
        if type(self.media_fingerprints) is not tuple or any(
            type(value) is not str for value in self.media_fingerprints
        ):
            raise ContractValidationError("media_fingerprints must contain exact strings")
        _fingerprint_tuple(self.media_fingerprints, "media_fingerprints")
        if self.schema != BASE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Fixed-H3 settings schema")

    def require_admitted(self) -> FixedH3Settings:
        """Deeply revalidate settings before any receipt or portable projection uses them."""

        if type(self) is not FixedH3Settings:
            raise ContractValidationError("fixed H3 settings must be FixedH3Settings")
        FixedH3Settings.__post_init__(self)
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "schema": self.schema,
            "model_revision": self.model_revision,
            "host_revision": self.host_revision,
            "workflow_fingerprint": self.workflow_fingerprint,
            "seed": self.seed,
            "schedule": self.schedule,
            "resolution": self.resolution,
            "duration_seconds": self.duration_seconds,
            "dtype": self.dtype,
            "device": self.device,
            "media_fingerprints": list(self.media_fingerprints),
        }


@dataclass(frozen=True, slots=True)
class FixedH3Receipt:
    """Redacted fixed-generation state; the current public scope admits NOT_RUN only."""

    receipt_id: str
    status: FixedH3Status
    settings: FixedH3Settings
    output_fingerprint: str | None = None
    redacted_message: str | None = None
    schema: str = BASE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        if type(self.receipt_id) is not str:
            raise ContractValidationError("fixed receipt_id must be an exact string")
        _identifier(self.receipt_id, "fixed receipt_id")
        if type(self.status) is not FixedH3Status:
            raise ContractValidationError("fixed H3 status must be a FixedH3Status")
        if type(self.settings) is not FixedH3Settings:
            raise ContractValidationError("fixed H3 settings must be FixedH3Settings")
        self.settings.require_admitted()
        # CRITICAL: caller metadata is not runtime authority. Until a separate authorized issuer
        # exists, executed statuses must never become fixed-H3 generation evidence.
        if self.status is not FixedH3Status.NOT_RUN:
            raise ContractValidationError("no fixed H3 runtime authority is activated")
        if self.output_fingerprint is not None:
            raise ContractValidationError("not-run fixed H3 receipt must not carry output")
        if type(self.redacted_message) is not str or not self.redacted_message:
            raise ContractValidationError("not-run fixed H3 receipt requires a redacted message")
        _metadata(self.redacted_message, "fixed redacted_message", 1024)
        if type(self.schema) is not str or self.schema != BASE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Fixed-H3 receipt schema")

    def require_admitted(self) -> FixedH3Receipt:
        """Recheck authority at every aggregate boundary, including after object tampering."""

        if type(self) is not FixedH3Receipt:
            raise ContractValidationError("no fixed H3 runtime authority is activated")
        FixedH3Receipt.__post_init__(self)
        return self

    @property
    def is_successful(self) -> bool:
        self.require_admitted()
        return False

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "status": self.status.value,
            "settings": self.settings.to_wire(),
            "output_fingerprint": self.output_fingerprint,
            "redacted_message": self.redacted_message,
        }


@dataclass(frozen=True, slots=True)
class OracleComparison:
    """Sanitized optional oracle comparison with an explicit non-equivalence limitation."""

    comparison_id: str
    method: OracleComparisonMethod
    official_recording: OfficialContextIRRecording | None = None
    status: OracleComparisonStatus = OracleComparisonStatus.NOT_REQUESTED
    local_fingerprint: str | None = None
    difference_codes: tuple[str, ...] = ()
    limitation: str = "oracle comparison is not requested"
    schema: str = BASE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.comparison_id, "oracle comparison_id")
        if not isinstance(self.method, OracleComparisonMethod):
            raise ContractValidationError("oracle method must be an OracleComparisonMethod")
        if self.official_recording is not None and not isinstance(
            self.official_recording, OfficialContextIRRecording
        ):
            raise ContractValidationError("official_recording must be sanitized metadata")
        if self.local_fingerprint is not None:
            _fingerprint(self.local_fingerprint, "local_fingerprint")
        if not isinstance(self.status, OracleComparisonStatus):
            raise ContractValidationError("oracle status must be an OracleComparisonStatus")
        if not isinstance(self.difference_codes, tuple) or len(self.difference_codes) > 64:
            raise ContractValidationError("difference_codes must be bounded")
        for code in self.difference_codes:
            _identifier(code, "oracle difference code")
        _metadata(self.limitation, "oracle limitation", 1024)
        if self.status is OracleComparisonStatus.NOT_REQUESTED and (
            self.local_fingerprint is not None or self.difference_codes
        ):
            raise ContractValidationError(
                "not-requested oracle comparison cannot carry resolved comparison data"
            )
        if self.status in {OracleComparisonStatus.MATCHED, OracleComparisonStatus.DIFFERENT} and (
            self.official_recording is None or self.local_fingerprint is None
        ):
            raise ContractValidationError("completed oracle comparison requires both fingerprints")
        if self.status is OracleComparisonStatus.MATCHED and self.difference_codes:
            raise ContractValidationError("matched oracle comparison cannot contain differences")
        if self.status is OracleComparisonStatus.DIFFERENT and not self.difference_codes:
            raise ContractValidationError("different oracle comparison requires difference codes")
        if self.schema != BASE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported oracle comparison schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "comparison_id": self.comparison_id,
            "method": self.method.value,
            "official_recording": (
                None
                if self.official_recording is None
                else self.official_recording.to_public_dict()
            ),
            "status": self.status.value,
            "local_fingerprint": self.local_fingerprint,
            "difference_codes": list(self.difference_codes),
            "limitation": self.limitation,
        }


@dataclass(frozen=True, slots=True)
class BaseEvaluationCase:
    """One typed Base plan plus optional sanitized comparison/generation metadata."""

    case_id: str
    route: EvaluationRoute
    plan: ContextPlan
    baseline_plan: ContextPlan | None = None
    measurement: EvaluationMeasurement = field(default_factory=lambda: EvaluationMeasurement(0.0))
    source_fingerprints: tuple[str, ...] = ()
    oracle: OracleComparison | None = None
    fixed_h3: FixedH3Receipt | None = None
    schema: str = BASE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.case_id, "evaluation case_id")
        if not isinstance(self.route, EvaluationRoute):
            raise ContractValidationError("evaluation route must be an EvaluationRoute")
        if not isinstance(self.plan, ContextPlan):
            raise ContractValidationError("evaluation plan must be a ContextPlan")
        if self.plan.request.profile.name is not PromptProfile.BASE:
            raise ContractValidationError("Base evaluation requires the h3_base profile")
        if self.baseline_plan is not None and not isinstance(self.baseline_plan, ContextPlan):
            raise ContractValidationError("baseline_plan must be a ContextPlan or None")
        if not isinstance(self.measurement, EvaluationMeasurement):
            raise ContractValidationError("measurement must be an EvaluationMeasurement")
        _fingerprint_tuple(self.source_fingerprints, "source_fingerprints")
        if self.oracle is not None and not isinstance(self.oracle, OracleComparison):
            raise ContractValidationError("oracle must be an OracleComparison or None")
        if self.fixed_h3 is not None:
            if type(self.fixed_h3) is not FixedH3Receipt:
                raise ContractValidationError("fixed_h3 must be a FixedH3Receipt or None")
            self.fixed_h3.require_admitted()
        if self.schema != BASE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Base evaluation case schema")

    def to_wire(self) -> dict[str, object]:
        """Return safe corpus metadata without serializing the private plan contents."""

        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "route": self.route.value,
            "task_mode": self.plan.request.task_mode.value,
            "source_fingerprints": list(self.source_fingerprints),
        }


@dataclass(frozen=True, slots=True)
class BaseEvaluationResult:
    """Separate structural metrics and optional oracle/fixed-H3 evidence for one case."""

    result_id: str
    case_id: str
    route: EvaluationRoute
    status: EvaluationStatus
    structural_valid: bool
    hard_constraints_preserved: bool
    evidence_total: int
    evidence_referenced: int
    evidence_coverage: str
    prompt_fingerprint: str | None
    diagnostic_codes: tuple[str, ...]
    diagnostics: tuple[ValidationDiagnostic, ...]
    measurement: EvaluationMeasurement
    oracle: OracleComparison
    fixed_h3: FixedH3Receipt
    schema: str = BASE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.result_id, "evaluation result_id")
        _identifier(self.case_id, "evaluation case_id")
        if not isinstance(self.route, EvaluationRoute):
            raise ContractValidationError("result route must be an EvaluationRoute")
        if not isinstance(self.status, EvaluationStatus):
            raise ContractValidationError("result status must be an EvaluationStatus")
        for value, field_name in (
            (self.structural_valid, "structural_valid"),
            (self.hard_constraints_preserved, "hard_constraints_preserved"),
        ):
            if not isinstance(value, bool):
                raise ContractValidationError(f"{field_name} must be a boolean")
        for count, field_name in (
            (self.evidence_total, "evidence_total"),
            (self.evidence_referenced, "evidence_referenced"),
        ):
            _non_negative_int(count, field_name)
        if self.evidence_referenced > self.evidence_total:
            raise ContractValidationError("evidence_referenced cannot exceed evidence_total")
        coverage = Decimal(self.evidence_coverage)
        if not coverage.is_finite() or not Decimal("0") <= coverage <= Decimal("1"):
            raise ContractValidationError("evidence_coverage must be between 0 and 1")
        if self.prompt_fingerprint is not None:
            _fingerprint(self.prompt_fingerprint, "prompt_fingerprint")
        if (
            not isinstance(self.diagnostic_codes, tuple)
            or len(self.diagnostic_codes) > MAX_EVALUATION_DIAGNOSTICS
        ):
            raise ContractValidationError("diagnostic_codes must be bounded")
        for code in self.diagnostic_codes:
            _identifier(code, "diagnostic code")
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > MAX_EVALUATION_DIAGNOSTICS
        ):
            raise ContractValidationError("diagnostics must be bounded")
        if not all(isinstance(item, ValidationDiagnostic) for item in self.diagnostics):
            raise ContractValidationError("diagnostics must contain ValidationDiagnostic values")
        if self.diagnostic_codes != tuple(item.code for item in self.diagnostics):
            raise ContractValidationError("diagnostic_codes must match diagnostics")
        if not isinstance(self.measurement, EvaluationMeasurement):
            raise ContractValidationError("result measurement must be an EvaluationMeasurement")
        if not isinstance(self.oracle, OracleComparison):
            raise ContractValidationError("result oracle must be an OracleComparison")
        if type(self.fixed_h3) is not FixedH3Receipt:
            raise ContractValidationError("result fixed_h3 must be a FixedH3Receipt")
        self.fixed_h3.require_admitted()
        if self.status is EvaluationStatus.PASSED and not (
            self.structural_valid and self.hard_constraints_preserved
        ):
            raise ContractValidationError(
                "passed evaluation requires structural and constraint pass"
            )
        if self.status is EvaluationStatus.FAILED and not self.diagnostics:
            raise ContractValidationError("failed evaluation requires diagnostics")
        if self.schema != BASE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Base evaluation result schema")

    @property
    def is_complete(self) -> bool:
        return self.status is EvaluationStatus.PASSED

    def to_wire(self) -> dict[str, object]:
        if type(self.fixed_h3) is not FixedH3Receipt:
            raise ContractValidationError("result fixed_h3 must be FixedH3Receipt")
        self.fixed_h3.require_admitted()
        return {
            "schema": self.schema,
            "result_id": self.result_id,
            "case_id": self.case_id,
            "route": self.route.value,
            "status": self.status.value,
            "structural_valid": self.structural_valid,
            "hard_constraints_preserved": self.hard_constraints_preserved,
            "evidence_total": self.evidence_total,
            "evidence_referenced": self.evidence_referenced,
            "evidence_coverage": self.evidence_coverage,
            "prompt_fingerprint": self.prompt_fingerprint,
            "diagnostic_codes": list(self.diagnostic_codes),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "measurement": self.measurement.to_wire(),
            "oracle": self.oracle.to_wire(),
            "fixed_h3": self.fixed_h3.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class BaseEvaluationCorpus:
    """Versioned ordered case collection; duplicate IDs and route ambiguity fail closed."""

    corpus_id: str
    cases: tuple[BaseEvaluationCase, ...]
    schema: str = BASE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.corpus_id, "corpus_id")
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_EVALUATION_CASES
        ):
            raise ContractValidationError("evaluation corpus cases must be bounded and non-empty")
        if not all(isinstance(item, BaseEvaluationCase) for item in self.cases):
            raise ContractValidationError(
                "evaluation corpus cases must be BaseEvaluationCase values"
            )
        ids = tuple(item.case_id for item in self.cases)
        if len(ids) != len(set(ids)):
            raise ContractValidationError("evaluation case IDs must be unique")
        if self.schema != BASE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Base evaluation corpus schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "corpus_id": self.corpus_id,
            "cases": [case.to_wire() for case in self.cases],
        }


@dataclass(frozen=True, slots=True)
class BaseEvaluationReport:
    """Deterministic report preserving route-level evidence separation."""

    report_id: str
    corpus_id: str
    results: tuple[BaseEvaluationResult, ...]
    route_summary: Mapping[str, Mapping[str, int]]
    schema: str = BASE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.report_id, "evaluation report_id")
        _identifier(self.corpus_id, "corpus_id")
        if not isinstance(self.results, tuple) or not self.results:
            raise ContractValidationError("evaluation report results must be non-empty")
        if not all(isinstance(item, BaseEvaluationResult) for item in self.results):
            raise ContractValidationError("evaluation report results must be typed")
        if len({item.result_id for item in self.results}) != len(self.results):
            raise ContractValidationError("evaluation result IDs must be unique")
        if not isinstance(self.route_summary, Mapping):
            raise ContractValidationError("route_summary must be a mapping")
        valid_routes = {route.value for route in EvaluationRoute}
        for route, summary in self.route_summary.items():
            if route not in valid_routes or not isinstance(summary, Mapping):
                raise ContractValidationError("route_summary contains an invalid route bucket")
            if set(summary) != {"case_count", "passed", "failed"}:
                raise ContractValidationError("route_summary bucket fields are invalid")
            counts = tuple(summary[field] for field in ("case_count", "passed", "failed"))
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in counts
            ):
                raise ContractValidationError("route_summary counts must be non-negative integers")
            if summary["case_count"] != summary["passed"] + summary["failed"]:
                raise ContractValidationError("route_summary case count does not match outcomes")
        if self.schema != BASE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported Base evaluation report schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "report_id": self.report_id,
            "corpus_id": self.corpus_id,
            "results": [item.to_wire() for item in self.results],
            "route_summary": {
                route: dict(summary) for route, summary in self.route_summary.items()
            },
        }


def _immutable_snapshot(plan: ContextPlan) -> dict[str, object]:
    return {
        "user_intent": plan.request.user_intent,
        "profile": plan.request.profile.to_wire(),
        "task_mode": plan.request.task_mode.value,
        "effective_duration_seconds": str(plan.request.effective_duration_seconds),
        "hard_constraints": plan.hard_constraints.to_wire(),
        "assets": [asset.to_wire() for asset in plan.request.assets],
        "reference_registry": plan.request.reference_registry.to_wire(),
        "timeline": [segment.to_wire() for segment in plan.intent_graph.segments],
    }


def _default_fixed_receipt(case: BaseEvaluationCase) -> FixedH3Receipt:
    settings = FixedH3Settings(
        "not_run.model",
        "not_run.host",
        canonical_fingerprint(_fingerprintable(case.plan.to_wire())),
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


def _diagnostic(
    code: str, message: str, severity: ValidationSeverity = ValidationSeverity.ERROR
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "evaluation")


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


def evaluate_base_case(case: BaseEvaluationCase) -> BaseEvaluationResult:
    """Render/lint one case without executing any runtime and preserve separate evidence layers."""

    if not isinstance(case, BaseEvaluationCase):
        raise ContractValidationError("case must be a BaseEvaluationCase")
    diagnostics: list[ValidationDiagnostic] = []
    prompt_fingerprint: str | None = None
    structural_valid = False
    hard_constraints_preserved = True
    evidence_total = 0
    evidence_referenced = 0
    evidence_coverage = "0"
    try:
        document = render_base_prompt(case.plan)
        prompt_fingerprint = canonical_fingerprint(_fingerprintable(document.to_wire()))
        lint_result: PromptLintResult = lint_prompt(case.plan, document)
        structural_valid = lint_result.is_valid
        diagnostics.extend(
            _diagnostic(item.code, item.message, item.severity) for item in lint_result.diagnostics
        )
        evidence_total, evidence_referenced, evidence_coverage = _coverage(case.plan, document)
    except PromptRenderingError as exc:
        diagnostics.append(_diagnostic("evaluation.render_failed", "Base prompt rendering failed"))
        document = None
        del exc

    if case.baseline_plan is not None and _immutable_snapshot(case.plan) != _immutable_snapshot(
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
    if not structural_valid:
        hard_constraints_preserved = (
            False
            if any(item.code.startswith("constraint.") for item in diagnostics)
            else hard_constraints_preserved
        )
    status = (
        EvaluationStatus.PASSED
        if structural_valid and hard_constraints_preserved
        else EvaluationStatus.FAILED
    )
    oracle = _resolve_oracle(case.oracle, prompt_fingerprint)
    fixed_h3 = case.fixed_h3 if case.fixed_h3 is not None else _default_fixed_receipt(case)
    diagnostic_codes = tuple(item.code for item in diagnostics)
    result_material = {
        "schema": BASE_EVALUATION_SCHEMA,
        "case_id": case.case_id,
        "route": case.route.value,
        "status": status.value,
        "structural_valid": structural_valid,
        "hard_constraints_preserved": hard_constraints_preserved,
        "evidence_total": evidence_total,
        "evidence_referenced": evidence_referenced,
        "evidence_coverage": evidence_coverage,
        "prompt_fingerprint": prompt_fingerprint,
        "diagnostic_codes": list(diagnostic_codes),
        "measurement": case.measurement.to_wire(),
        "oracle": oracle.to_wire(),
        "fixed_h3": fixed_h3.to_wire(),
    }
    result_id = (
        "result_" + canonical_fingerprint(_fingerprintable(result_material)).split(":", 1)[1][:32]
    )
    return BaseEvaluationResult(
        result_id,
        case.case_id,
        case.route,
        status,
        structural_valid,
        hard_constraints_preserved,
        evidence_total,
        evidence_referenced,
        evidence_coverage,
        prompt_fingerprint,
        diagnostic_codes,
        tuple(diagnostics),
        case.measurement,
        oracle,
        fixed_h3,
    )


def evaluate_base_corpus(corpus: BaseEvaluationCorpus) -> BaseEvaluationReport:
    """Evaluate ordered cases and summarize each named route independently."""

    if not isinstance(corpus, BaseEvaluationCorpus):
        raise ContractValidationError("corpus must be a BaseEvaluationCorpus")
    results = tuple(evaluate_base_case(case) for case in corpus.cases)
    summary: dict[str, dict[str, int]] = {}
    for result in results:
        route = result.route.value
        bucket = summary.setdefault(route, {"case_count": 0, "passed": 0, "failed": 0})
        bucket["case_count"] += 1
        bucket["passed" if result.status is EvaluationStatus.PASSED else "failed"] += 1
    frozen_summary: Mapping[str, Mapping[str, int]] = MappingProxyType(
        {route: MappingProxyType(values) for route, values in summary.items()}
    )
    report_material = {
        "schema": BASE_EVALUATION_SCHEMA,
        "corpus_id": corpus.corpus_id,
        "results": [result.to_wire() for result in results],
        "route_summary": {route: dict(values) for route, values in frozen_summary.items()},
    }
    report_id = (
        "report_" + canonical_fingerprint(_fingerprintable(report_material)).split(":", 1)[1][:32]
    )
    return BaseEvaluationReport(report_id, corpus.corpus_id, results, frozen_summary)


__all__ = [
    "BASE_EVALUATION_SCHEMA",
    "MAX_EVALUATION_CASES",
    "MAX_EVALUATION_DIAGNOSTICS",
    "MAX_EVALUATION_FINGERPRINTS",
    "BaseEvaluationCase",
    "BaseEvaluationCorpus",
    "BaseEvaluationReport",
    "BaseEvaluationResult",
    "EvaluationMeasurement",
    "EvaluationRoute",
    "EvaluationStatus",
    "FixedH3Receipt",
    "FixedH3Settings",
    "FixedH3Status",
    "OracleComparison",
    "OracleComparisonMethod",
    "OracleComparisonStatus",
    "evaluate_base_case",
    "evaluate_base_corpus",
]
