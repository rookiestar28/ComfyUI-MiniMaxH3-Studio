"""Offline reconstruction baselines and immutable program-envelope controls.

M9-07 records what is actually known before concrete perception/runtime work.  A
baseline observation is scoped to one evidence layer and one frozen metadata
case; missing execution remains missing.  The program envelope is a separate
immutable admission boundary for bounded local work and never expands itself.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .canonical import canonical_fingerprint
from .errors import ContractValidationError, ProgramEnvelopeError

RECONSTRUCTION_BASELINE_SCHEMA = "h3.reconstruction.baseline.v1"
PROGRAM_ENVELOPE_SCHEMA = "h3.reconstruction.envelope.v1"
MAX_BASELINE_CASES = 256
MAX_BASELINE_OBSERVATIONS = MAX_BASELINE_CASES * 8
MAX_BASELINE_DIAGNOSTICS = 64
MAX_BASELINE_METRICS = 32
MAX_BASELINE_TAXONOMY = 128
MAX_BASELINE_REGRESSIONS = 128
MAX_ENVELOPE_CAPABILITIES = 64
MAX_ENVELOPE_LANES = 64
MAX_ENVELOPE_RULES = 64
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RAW_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f\\]")
_SENSITIVE_SEGMENTS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "password",
        "path",
        "secret",
        "signed",
        "token",
        "url",
    }
)


class BaselineEvidenceLayer(str, Enum):
    """Independent evidence layer; no aggregate quality score crosses these names."""

    STRUCTURAL = "structural"
    PERCEPTION = "perception"
    PLANNING = "planning"
    ORACLE = "oracle"
    FIXED_H3 = "fixed_h3"
    HUMAN = "human"
    LATENCY_RESOURCE = "latency_resource"
    FAILURE = "failure"


class BaselineOutcome(str, Enum):
    """Terminal observation state, with missing distinct from pass/fail."""

    PASSED = "passed"
    FAILED = "failed"
    MISSING = "missing"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class BaselineReportStatus(str, Enum):
    """Aggregate status that preserves partial/missing baseline evidence."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class BaselineClaimCeiling(str, Enum):
    """Highest local claim supported by the measured layer set."""

    QUALIFIED_LOCAL = "qualified_local"
    STRUCTURAL_ONLY = "structural_only"
    NO_CLAIM = "no_claim"


class ProgramEnvelopeDecision(str, Enum):
    """Admission decision; STOP never mutates usage or expands the envelope."""

    CONTINUE = "continue"
    STOP = "stop"
    REAUTHORIZE = "reauthorize"


class ProgramEnvelopeCapabilityMaturity(str, Enum):
    """Capability disposition frozen by the envelope."""

    MANDATORY = "mandatory"
    OPTIONAL = "optional"
    UNSUPPORTED = "unsupported"


class ProgramEnvelopeResource(str, Enum):
    """Finite resource dimensions governed by one envelope."""

    CALLS = "calls"
    SPEND_MINOR_UNITS = "spend_minor_units"
    COMPUTE_SECONDS = "compute_seconds"
    STORAGE_BYTES = "storage_bytes"
    RETENTION_DAYS = "retention_days"
    REVIEWER_MINUTES = "reviewer_minutes"
    CANDIDATES = "candidates"
    WALL_SECONDS = "wall_seconds"


class ProgramEnvelopeRegressionPriority(str, Enum):
    """Priority ordering for the accepted regression queue."""

    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


def _sensitive_identifier(value: str) -> bool:
    segments = tuple(part for part in re.split(r"[_.:-]+", value.casefold()) if part)
    return any(segment in _SENSITIVE_SEGMENTS for segment in segments)


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded identifier")
    if _sensitive_identifier(value):
        raise ContractValidationError(f"{field_name} must not contain sensitive markers")
    return value


def _metadata(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field_name} must be bounded metadata")
    if _CONTROL_PATTERN.search(value) or any(
        marker in value.casefold()
        for marker in ("http://", "https://", "bearer ", "token=", "api_key", "password")
    ):
        raise ContractValidationError(f"{field_name} contains unsafe metadata")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a sha256 fingerprint")
    return value


def _raw_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _RAW_DIGEST_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a raw SHA-256 digest")
    return value


def _date(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be an ISO date")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractValidationError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{field_name} must be a non-negative integer")
    return value


def _tuple_of_identifiers(value: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field_name} must be a bounded non-empty tuple")
    if len(set(value)) != len(value):
        raise ContractValidationError(f"{field_name} must be unique")
    for item in value:
        _identifier(item, field_name)
    return value


def _tuple_of_fingerprints(value: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field_name} must be a bounded non-empty tuple")
    if len(set(value)) != len(value):
        raise ContractValidationError(f"{field_name} must be unique")
    for item in value:
        _fingerprint(item, field_name)
    return value


def _optional_fingerprint(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field_name)


def _decimal(value: object, field_name: str) -> str:
    if not isinstance(value, str) or len(value) > 32 or _DECIMAL_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be an exact decimal string")
    return value


def _diagnostic_codes(value: object, field_name: str = "diagnostic_codes") -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_BASELINE_DIAGNOSTICS:
        raise ContractValidationError(f"{field_name} must be bounded")
    if len(set(value)) != len(value):
        raise ContractValidationError(f"{field_name} must be unique")
    for item in value:
        _identifier(item, field_name)
    return value


@dataclass(frozen=True, slots=True)
class ReconstructionBaselineCase:
    """One frozen corpus case represented without content-bearing inputs."""

    case_id: str
    corpus_id: str
    partition_id: str
    task_mode: str
    language: str
    risk_classes: tuple[str, ...]
    input_fingerprints: tuple[str, ...]
    source_fingerprints: tuple[str, ...]
    required_layers: tuple[BaselineEvidenceLayer, ...] = tuple(BaselineEvidenceLayer)

    def __post_init__(self) -> None:
        _identifier(self.case_id, "baseline case_id")
        _identifier(self.corpus_id, "baseline corpus_id")
        _identifier(self.partition_id, "baseline partition_id")
        _identifier(self.task_mode, "baseline task_mode")
        _identifier(self.language, "baseline language")
        _tuple_of_identifiers(self.risk_classes, "risk_classes", 32)
        _tuple_of_fingerprints(self.input_fingerprints, "input_fingerprints", 64)
        _tuple_of_fingerprints(self.source_fingerprints, "source_fingerprints", 64)
        if not isinstance(self.required_layers, tuple) or self.required_layers != tuple(
            BaselineEvidenceLayer
        ):
            raise ContractValidationError("required_layers must close all baseline evidence layers")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_public_dict())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": RECONSTRUCTION_BASELINE_SCHEMA,
            "case_id": self.case_id,
            "corpus_id": self.corpus_id,
            "partition_id": self.partition_id,
            "task_mode": self.task_mode,
            "language": self.language,
            "risk_classes": list(self.risk_classes),
            "input_fingerprints": list(self.input_fingerprints),
            "source_fingerprints": list(self.source_fingerprints),
            "required_layers": [layer.value for layer in self.required_layers],
        }


@dataclass(frozen=True, slots=True)
class BaselineObservation:
    """One layer-scoped result with exact-decimal metrics and no raw output."""

    case_id: str
    layer: BaselineEvidenceLayer
    outcome: BaselineOutcome
    lane_id: str
    input_fingerprints: tuple[str, ...]
    output_fingerprint: str | None = None
    metrics: tuple[tuple[str, str], ...] = ()
    diagnostic_codes: tuple[str, ...] = ()
    source_revision: str = "baseline.manual.v1"
    claim_ceiling: BaselineClaimCeiling = BaselineClaimCeiling.NO_CLAIM

    def __post_init__(self) -> None:
        _identifier(self.case_id, "observation case_id")
        if not isinstance(self.layer, BaselineEvidenceLayer):
            raise ContractValidationError("observation layer is invalid")
        if not isinstance(self.outcome, BaselineOutcome):
            raise ContractValidationError("observation outcome is invalid")
        _identifier(self.lane_id, "observation lane_id")
        _tuple_of_fingerprints(self.input_fingerprints, "observation input_fingerprints", 64)
        _optional_fingerprint(self.output_fingerprint, "observation output_fingerprint")
        if not isinstance(self.metrics, tuple) or len(self.metrics) > MAX_BASELINE_METRICS:
            raise ContractValidationError("observation metrics must be bounded")
        metric_keys: set[str] = set()
        for key, value in self.metrics:
            _identifier(key, "metric name")
            _decimal(value, f"metric {key}")
            if key in metric_keys:
                raise ContractValidationError("metric names must be unique")
            metric_keys.add(key)
        _diagnostic_codes(self.diagnostic_codes)
        _identifier(self.source_revision, "observation source_revision")
        if not isinstance(self.claim_ceiling, BaselineClaimCeiling):
            raise ContractValidationError("observation claim_ceiling is invalid")
        if self.outcome is BaselineOutcome.PASSED and not (self.metrics or self.output_fingerprint):
            raise ContractValidationError("passed observation requires a measurement")
        if (
            self.outcome
            in {
                BaselineOutcome.FAILED,
                BaselineOutcome.MISSING,
                BaselineOutcome.BLOCKED,
                BaselineOutcome.NOT_APPLICABLE,
            }
            and not self.diagnostic_codes
        ):
            raise ContractValidationError("non-passed observation requires diagnostics")
        if self.outcome is not BaselineOutcome.PASSED and self.metrics:
            raise ContractValidationError("non-passed observation must not carry scored metrics")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": RECONSTRUCTION_BASELINE_SCHEMA,
            "case_id": self.case_id,
            "layer": self.layer.value,
            "outcome": self.outcome.value,
            "lane_id": self.lane_id,
            "input_fingerprints": list(self.input_fingerprints),
            "output_fingerprint": self.output_fingerprint,
            "metrics": {key: value for key, value in self.metrics},
            "diagnostic_codes": list(self.diagnostic_codes),
            "source_revision": self.source_revision,
            "claim_ceiling": self.claim_ceiling.value,
        }


@dataclass(frozen=True, slots=True)
class BaselineLayerSummary:
    """Independent counts for one evidence layer."""

    layer: BaselineEvidenceLayer
    total: int
    passed: int
    failed: int
    missing: int
    blocked: int
    not_applicable: int

    def __post_init__(self) -> None:
        if not isinstance(self.layer, BaselineEvidenceLayer):
            raise ContractValidationError("layer summary layer is invalid")
        counts = (
            self.total,
            self.passed,
            self.failed,
            self.missing,
            self.blocked,
            self.not_applicable,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts
        ):
            raise ContractValidationError("layer summary counts must be non-negative integers")
        if (
            self.total
            != self.passed + self.failed + self.missing + self.blocked + self.not_applicable
        ):
            raise ContractValidationError("layer summary counts do not add up")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "missing": self.missing,
            "blocked": self.blocked,
            "not_applicable": self.not_applicable,
        }


@dataclass(frozen=True, slots=True)
class ReconstructionErrorTaxonomyEntry:
    """Accepted error code mapping without retaining error text or payloads."""

    error_code: str
    layer: BaselineEvidenceLayer
    classification: str
    description: str

    def __post_init__(self) -> None:
        _identifier(self.error_code, "taxonomy error_code")
        if not isinstance(self.layer, BaselineEvidenceLayer):
            raise ContractValidationError("taxonomy layer is invalid")
        _identifier(self.classification, "taxonomy classification")
        _metadata(self.description, "taxonomy description")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "layer": self.layer.value,
            "classification": self.classification,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ReconstructionRegressionItem:
    """Prioritized regression item accepted by the baseline report."""

    regression_id: str
    error_code: str
    layer: BaselineEvidenceLayer
    priority: ProgramEnvelopeRegressionPriority
    status: str
    case_count: int
    rationale: str
    next_action: str

    def __post_init__(self) -> None:
        _identifier(self.regression_id, "regression_id")
        _identifier(self.error_code, "regression error_code")
        if not isinstance(self.layer, BaselineEvidenceLayer):
            raise ContractValidationError("regression layer is invalid")
        if not isinstance(self.priority, ProgramEnvelopeRegressionPriority):
            raise ContractValidationError("regression priority is invalid")
        if self.status not in {"open", "accepted", "mitigated"}:
            raise ContractValidationError("regression status is invalid")
        _non_negative_int(self.case_count, "regression case_count")
        _metadata(self.rationale, "regression rationale")
        _metadata(self.next_action, "regression next_action")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "regression_id": self.regression_id,
            "error_code": self.error_code,
            "layer": self.layer.value,
            "priority": self.priority.value,
            "status": self.status,
            "case_count": self.case_count,
            "rationale": self.rationale,
            "next_action": self.next_action,
        }


@dataclass(frozen=True, slots=True)
class ReconstructionBaselineReport:
    """Portable baseline report with layer-level missingness and claim ceiling."""

    baseline_id: str
    baseline_revision: str
    corpus_id: str
    envelope_id: str
    envelope_revision: str
    envelope_fingerprint: str
    cases: tuple[ReconstructionBaselineCase, ...]
    observations: tuple[BaselineObservation, ...]
    layer_summary: tuple[BaselineLayerSummary, ...]
    error_taxonomy: tuple[ReconstructionErrorTaxonomyEntry, ...]
    regressions: tuple[ReconstructionRegressionItem, ...]
    status: BaselineReportStatus
    claim_ceiling: BaselineClaimCeiling
    missing_observation_count: int
    failed_observation_count: int
    blocked_observation_count: int

    def __post_init__(self) -> None:
        _identifier(self.baseline_id, "baseline_id")
        _identifier(self.baseline_revision, "baseline_revision")
        _identifier(self.corpus_id, "baseline corpus_id")
        _identifier(self.envelope_id, "envelope_id")
        _identifier(self.envelope_revision, "envelope_revision")
        _fingerprint(self.envelope_fingerprint, "envelope_fingerprint")
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_BASELINE_CASES
        ):
            raise ContractValidationError("baseline report cases must be bounded and non-empty")
        if not all(isinstance(case, ReconstructionBaselineCase) for case in self.cases):
            raise ContractValidationError("baseline report cases are invalid")
        case_ids = tuple(case.case_id for case in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise ContractValidationError("baseline report case IDs must be unique")
        if not isinstance(self.observations, tuple) or not self.observations:
            raise ContractValidationError("baseline report observations must be non-empty")
        if len(self.observations) > MAX_BASELINE_OBSERVATIONS:
            raise ContractValidationError("baseline report observations exceed the bound")
        if not all(isinstance(item, BaselineObservation) for item in self.observations):
            raise ContractValidationError("baseline report observations are invalid")
        expected = {(case.case_id, layer) for case in self.cases for layer in case.required_layers}
        actual = {(item.case_id, item.layer) for item in self.observations}
        if actual != expected or len(actual) != len(self.observations):
            raise ContractValidationError("baseline observations must close every case/layer pair")
        if any(case.corpus_id != self.corpus_id for case in self.cases):
            raise ContractValidationError("baseline cases must share the report corpus")
        if not isinstance(self.layer_summary, tuple) or tuple(
            item.layer for item in self.layer_summary
        ) != tuple(BaselineEvidenceLayer):
            raise ContractValidationError(
                "layer_summary must contain all layers in canonical order"
            )
        if not all(isinstance(item, BaselineLayerSummary) for item in self.layer_summary):
            raise ContractValidationError("layer_summary contains an invalid value")
        for item in self.error_taxonomy:
            if not isinstance(item, ReconstructionErrorTaxonomyEntry):
                raise ContractValidationError("error_taxonomy contains an invalid value")
        if len(self.error_taxonomy) > MAX_BASELINE_TAXONOMY:
            raise ContractValidationError("error_taxonomy exceeds the bound")
        if len({item.error_code for item in self.error_taxonomy}) != len(self.error_taxonomy):
            raise ContractValidationError("error_taxonomy codes must be unique")
        if (
            not isinstance(self.regressions, tuple)
            or len(self.regressions) > MAX_BASELINE_REGRESSIONS
        ):
            raise ContractValidationError("regressions must be bounded")
        if not all(isinstance(item, ReconstructionRegressionItem) for item in self.regressions):
            raise ContractValidationError("regressions contain an invalid value")
        if len({item.regression_id for item in self.regressions}) != len(self.regressions):
            raise ContractValidationError("regression IDs must be unique")
        priority_order = {
            priority: index for index, priority in enumerate(ProgramEnvelopeRegressionPriority)
        }
        if tuple(
            (priority_order[item.priority], item.regression_id) for item in self.regressions
        ) != tuple(
            sorted((priority_order[item.priority], item.regression_id) for item in self.regressions)
        ):
            raise ContractValidationError("regressions must be ordered by priority and ID")
        if not isinstance(self.status, BaselineReportStatus):
            raise ContractValidationError("baseline report status is invalid")
        if not isinstance(self.claim_ceiling, BaselineClaimCeiling):
            raise ContractValidationError("baseline claim ceiling is invalid")
        for value, field_name in (
            (self.missing_observation_count, "missing_observation_count"),
            (self.failed_observation_count, "failed_observation_count"),
            (self.blocked_observation_count, "blocked_observation_count"),
        ):
            _non_negative_int(value, field_name)
        derived_missing = sum(item.outcome is BaselineOutcome.MISSING for item in self.observations)
        derived_failed = sum(item.outcome is BaselineOutcome.FAILED for item in self.observations)
        derived_blocked = sum(item.outcome is BaselineOutcome.BLOCKED for item in self.observations)
        if (
            self.missing_observation_count != derived_missing
            or self.failed_observation_count != derived_failed
            or self.blocked_observation_count != derived_blocked
        ):
            raise ContractValidationError("baseline outcome counts do not match observations")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self._fingerprint_payload())

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema": RECONSTRUCTION_BASELINE_SCHEMA,
            "baseline_id": self.baseline_id,
            "baseline_revision": self.baseline_revision,
            "corpus_id": self.corpus_id,
            "envelope_id": self.envelope_id,
            "envelope_revision": self.envelope_revision,
            "envelope_fingerprint": self.envelope_fingerprint,
            "cases": [case.to_public_dict() for case in self.cases],
            "observations": [item.to_public_dict() for item in self.observations],
            "layer_summary": [item.to_public_dict() for item in self.layer_summary],
            "error_taxonomy": [item.to_public_dict() for item in self.error_taxonomy],
            "regressions": [item.to_public_dict() for item in self.regressions],
            "status": self.status.value,
            "claim_ceiling": self.claim_ceiling.value,
            "missing_observation_count": self.missing_observation_count,
            "failed_observation_count": self.failed_observation_count,
            "blocked_observation_count": self.blocked_observation_count,
        }

    def to_public_dict(self) -> dict[str, object]:
        payload = self._fingerprint_payload()
        payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeLimits:
    """Global finite limits frozen before broad reconstruction search."""

    max_calls: int
    max_spend_minor_units: int
    currency: str
    max_compute_seconds: int
    max_storage_bytes: int
    max_retention_days: int
    max_reviewer_minutes: int
    max_candidates: int
    max_wall_seconds: int

    def __post_init__(self) -> None:
        _positive_int(self.max_calls, "max_calls")
        for value, field_name in (
            (self.max_spend_minor_units, "max_spend_minor_units"),
            (self.max_compute_seconds, "max_compute_seconds"),
            (self.max_storage_bytes, "max_storage_bytes"),
            (self.max_retention_days, "max_retention_days"),
            (self.max_reviewer_minutes, "max_reviewer_minutes"),
            (self.max_candidates, "max_candidates"),
        ):
            _non_negative_int(value, field_name)
        _positive_int(self.max_wall_seconds, "max_wall_seconds")
        _identifier(self.currency, "envelope currency")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "max_calls": self.max_calls,
            "max_spend_minor_units": self.max_spend_minor_units,
            "currency": self.currency,
            "max_compute_seconds": self.max_compute_seconds,
            "max_storage_bytes": self.max_storage_bytes,
            "max_retention_days": self.max_retention_days,
            "max_reviewer_minutes": self.max_reviewer_minutes,
            "max_candidates": self.max_candidates,
            "max_wall_seconds": self.max_wall_seconds,
        }


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeLaneLimits:
    """Optional per-lane ceilings that cannot exceed the global envelope."""

    lane_id: str
    max_calls: int
    max_spend_minor_units: int
    max_compute_seconds: int
    max_wall_seconds: int

    def __post_init__(self) -> None:
        _identifier(self.lane_id, "lane limit lane_id")
        _positive_int(self.max_calls, "lane max_calls")
        _non_negative_int(self.max_spend_minor_units, "lane max_spend_minor_units")
        _non_negative_int(self.max_compute_seconds, "lane max_compute_seconds")
        _positive_int(self.max_wall_seconds, "lane max_wall_seconds")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "lane_id": self.lane_id,
            "max_calls": self.max_calls,
            "max_spend_minor_units": self.max_spend_minor_units,
            "max_compute_seconds": self.max_compute_seconds,
            "max_wall_seconds": self.max_wall_seconds,
        }


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeCapability:
    """Mandatory/optional/unsupported capability disposition."""

    capability_id: str
    lane_id: str
    maturity: ProgramEnvelopeCapabilityMaturity
    rationale: str

    def __post_init__(self) -> None:
        _identifier(self.capability_id, "capability_id")
        _identifier(self.lane_id, "capability lane_id")
        if not isinstance(self.maturity, ProgramEnvelopeCapabilityMaturity):
            raise ContractValidationError("capability maturity is invalid")
        _metadata(self.rationale, "capability rationale")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "lane_id": self.lane_id,
            "maturity": self.maturity.value,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeStopRule:
    """Hard stop threshold; it never grants an implicit extension."""

    rule_id: str
    resource: ProgramEnvelopeResource
    threshold: int
    decision: ProgramEnvelopeDecision
    rationale: str

    def __post_init__(self) -> None:
        _identifier(self.rule_id, "stop rule_id")
        if not isinstance(self.resource, ProgramEnvelopeResource):
            raise ContractValidationError("stop resource is invalid")
        _non_negative_int(self.threshold, "stop threshold")
        if self.decision not in {ProgramEnvelopeDecision.STOP, ProgramEnvelopeDecision.REAUTHORIZE}:
            raise ContractValidationError("stop rule decision must stop or reauthorize")
        _metadata(self.rationale, "stop rationale")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "resource": self.resource.value,
            "threshold": self.threshold,
            "decision": self.decision.value,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeReauthorizationTrigger:
    """Dated trigger that requires a new reviewed envelope decision."""

    trigger_id: str
    trigger_kind: str
    due_date: str
    rationale: str

    def __post_init__(self) -> None:
        _identifier(self.trigger_id, "reauthorization trigger_id")
        _identifier(self.trigger_kind, "reauthorization trigger_kind")
        _date(self.due_date, "reauthorization due_date")
        _metadata(self.rationale, "reauthorization rationale")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "trigger_id": self.trigger_id,
            "trigger_kind": self.trigger_kind,
            "due_date": self.due_date,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ProgramEnvelope:
    """Versioned resource/stop envelope used before broad candidate search."""

    envelope_id: str
    revision: str
    effective_date: str
    expires_at: str
    limits: ProgramEnvelopeLimits
    lane_limits: tuple[ProgramEnvelopeLaneLimits, ...]
    capabilities: tuple[ProgramEnvelopeCapability, ...]
    stop_rules: tuple[ProgramEnvelopeStopRule, ...]
    marginal_value_rules: tuple[str, ...]
    reauthorization_triggers: tuple[ProgramEnvelopeReauthorizationTrigger, ...]

    def __post_init__(self) -> None:
        _identifier(self.envelope_id, "envelope_id")
        _identifier(self.revision, "envelope revision")
        effective = _date(self.effective_date, "effective_date")
        expires = _date(self.expires_at, "expires_at")
        if expires <= effective:
            raise ContractValidationError("expires_at must be later than effective_date")
        if not isinstance(self.limits, ProgramEnvelopeLimits):
            raise ContractValidationError("envelope limits are invalid")
        if not isinstance(self.lane_limits, tuple) or len(self.lane_limits) > MAX_ENVELOPE_LANES:
            raise ContractValidationError("lane_limits must be bounded")
        if not all(isinstance(item, ProgramEnvelopeLaneLimits) for item in self.lane_limits):
            raise ContractValidationError("lane_limits contain an invalid value")
        if len({item.lane_id for item in self.lane_limits}) != len(self.lane_limits):
            raise ContractValidationError("lane limit IDs must be unique")
        for item in self.lane_limits:
            if item.max_calls > self.limits.max_calls:
                raise ContractValidationError("lane max_calls cannot exceed global max_calls")
            if item.max_spend_minor_units > self.limits.max_spend_minor_units:
                raise ContractValidationError("lane spend cannot exceed global spend")
            if item.max_compute_seconds > self.limits.max_compute_seconds:
                raise ContractValidationError("lane compute cannot exceed global compute")
            if item.max_wall_seconds > self.limits.max_wall_seconds:
                raise ContractValidationError("lane wall time cannot exceed global wall time")
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise ContractValidationError("envelope capabilities must be non-empty")
        if len(self.capabilities) > MAX_ENVELOPE_CAPABILITIES:
            raise ContractValidationError("capabilities exceed the bound")
        if not all(isinstance(item, ProgramEnvelopeCapability) for item in self.capabilities):
            raise ContractValidationError("capabilities contain an invalid value")
        if len({item.capability_id for item in self.capabilities}) != len(self.capabilities):
            raise ContractValidationError("capability IDs must be unique")
        if not isinstance(self.stop_rules, tuple) or len(self.stop_rules) > MAX_ENVELOPE_RULES:
            raise ContractValidationError("stop_rules must be bounded")
        if not all(isinstance(item, ProgramEnvelopeStopRule) for item in self.stop_rules):
            raise ContractValidationError("stop_rules contain an invalid value")
        if len({item.rule_id for item in self.stop_rules}) != len(self.stop_rules):
            raise ContractValidationError("stop rule IDs must be unique")
        _tuple_of_identifiers(self.marginal_value_rules, "marginal_value_rules", MAX_ENVELOPE_RULES)
        if (
            not isinstance(self.reauthorization_triggers, tuple)
            or len(self.reauthorization_triggers) > MAX_ENVELOPE_RULES
        ):
            raise ContractValidationError("reauthorization_triggers must be bounded")
        if not all(
            isinstance(item, ProgramEnvelopeReauthorizationTrigger)
            for item in self.reauthorization_triggers
        ):
            raise ContractValidationError("reauthorization triggers contain an invalid value")
        if len({item.trigger_id for item in self.reauthorization_triggers}) != len(
            self.reauthorization_triggers
        ):
            raise ContractValidationError("reauthorization trigger IDs must be unique")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_public_dict(include_fingerprint=False))

    def to_public_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": PROGRAM_ENVELOPE_SCHEMA,
            "envelope_id": self.envelope_id,
            "revision": self.revision,
            "effective_date": self.effective_date,
            "expires_at": self.expires_at,
            "limits": self.limits.to_public_dict(),
            "lane_limits": [item.to_public_dict() for item in self.lane_limits],
            "capabilities": [item.to_public_dict() for item in self.capabilities],
            "stop_rules": [item.to_public_dict() for item in self.stop_rules],
            "marginal_value_rules": list(self.marginal_value_rules),
            "reauthorization_triggers": [
                item.to_public_dict() for item in self.reauthorization_triggers
            ],
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeLaneUsage:
    """Per-lane immutable counters."""

    lane_id: str
    calls: int = 0
    spend_minor_units: int = 0
    compute_seconds: int = 0
    wall_seconds: int = 0

    def __post_init__(self) -> None:
        _identifier(self.lane_id, "lane usage lane_id")
        for value, field_name in (
            (self.calls, "lane usage calls"),
            (self.spend_minor_units, "lane usage spend"),
            (self.compute_seconds, "lane usage compute"),
            (self.wall_seconds, "lane usage wall"),
        ):
            _non_negative_int(value, field_name)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "lane_id": self.lane_id,
            "calls": self.calls,
            "spend_minor_units": self.spend_minor_units,
            "compute_seconds": self.compute_seconds,
            "wall_seconds": self.wall_seconds,
        }


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeUsage:
    """Global and per-lane usage carried by a single writer."""

    calls: int = 0
    spend_minor_units: int = 0
    compute_seconds: int = 0
    storage_bytes: int = 0
    retention_days: int = 0
    reviewer_minutes: int = 0
    candidates: int = 0
    wall_seconds: int = 0
    lane_usage: tuple[ProgramEnvelopeLaneUsage, ...] = ()

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.calls, "usage calls"),
            (self.spend_minor_units, "usage spend"),
            (self.compute_seconds, "usage compute"),
            (self.storage_bytes, "usage storage"),
            (self.retention_days, "usage retention"),
            (self.reviewer_minutes, "usage reviewer_minutes"),
            (self.candidates, "usage candidates"),
            (self.wall_seconds, "usage wall"),
        ):
            _non_negative_int(value, field_name)
        if not isinstance(self.lane_usage, tuple) or len(self.lane_usage) > MAX_ENVELOPE_LANES:
            raise ContractValidationError("lane_usage must be bounded")
        if not all(isinstance(item, ProgramEnvelopeLaneUsage) for item in self.lane_usage):
            raise ContractValidationError("lane_usage contains an invalid value")
        if len({item.lane_id for item in self.lane_usage}) != len(self.lane_usage):
            raise ContractValidationError("lane usage IDs must be unique")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "spend_minor_units": self.spend_minor_units,
            "compute_seconds": self.compute_seconds,
            "storage_bytes": self.storage_bytes,
            "retention_days": self.retention_days,
            "reviewer_minutes": self.reviewer_minutes,
            "candidates": self.candidates,
            "wall_seconds": self.wall_seconds,
            "lane_usage": [item.to_public_dict() for item in self.lane_usage],
        }


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeAdmission:
    """One accepted or stopped admission and the next immutable gate state."""

    operation_id: str
    lane_id: str
    capability_id: str
    decision: ProgramEnvelopeDecision
    reason_code: str
    usage: ProgramEnvelopeUsage
    next_gate: ProgramEnvelopeGate

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "admission operation_id")
        _identifier(self.lane_id, "admission lane_id")
        _identifier(self.capability_id, "admission capability_id")
        if not isinstance(self.decision, ProgramEnvelopeDecision):
            raise ContractValidationError("admission decision is invalid")
        _identifier(self.reason_code, "admission reason_code")
        if not isinstance(self.usage, ProgramEnvelopeUsage):
            raise ContractValidationError("admission usage is invalid")
        if not isinstance(self.next_gate, ProgramEnvelopeGate):
            raise ContractValidationError("admission next_gate is invalid")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": PROGRAM_ENVELOPE_SCHEMA,
            "operation_id": self.operation_id,
            "lane_id": self.lane_id,
            "capability_id": self.capability_id,
            "decision": self.decision.value,
            "reason_code": self.reason_code,
            "usage": self.usage.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProgramEnvelopeGate:
    """Fail-closed immutable resource admission for one envelope."""

    envelope: ProgramEnvelope
    usage: ProgramEnvelopeUsage = ProgramEnvelopeUsage()

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, ProgramEnvelope):
            raise ContractValidationError("envelope gate envelope is invalid")
        if not isinstance(self.usage, ProgramEnvelopeUsage):
            raise ContractValidationError("envelope gate usage is invalid")

    def admit(
        self,
        *,
        lane_id: str,
        capability_id: str,
        operation_id: str,
        as_of: str,
        calls: int = 1,
        spend_minor_units: int = 0,
        compute_seconds: int = 0,
        storage_bytes: int = 0,
        retention_days: int = 0,
        reviewer_minutes: int = 0,
        candidates: int = 0,
        wall_seconds: int = 0,
    ) -> ProgramEnvelopeAdmission:
        _identifier(lane_id, "admission lane_id")
        _identifier(capability_id, "admission capability_id")
        _identifier(operation_id, "admission operation_id")
        today = _date(as_of, "admission as_of")
        if today < self.envelope.effective_date:
            raise ProgramEnvelopeError("envelope_not_active", "program envelope is not active")
        if today >= self.envelope.expires_at:
            raise ProgramEnvelopeError("envelope_expired", "program envelope has expired")
        amounts = {
            "calls": _non_negative_int(calls, "admission calls"),
            "spend_minor_units": _non_negative_int(
                spend_minor_units, "admission spend_minor_units"
            ),
            "compute_seconds": _non_negative_int(compute_seconds, "admission compute_seconds"),
            "storage_bytes": _non_negative_int(storage_bytes, "admission storage_bytes"),
            "retention_days": _non_negative_int(retention_days, "admission retention_days"),
            "reviewer_minutes": _non_negative_int(reviewer_minutes, "admission reviewer_minutes"),
            "candidates": _non_negative_int(candidates, "admission candidates"),
            "wall_seconds": _non_negative_int(wall_seconds, "admission wall_seconds"),
        }
        if not any(amounts.values()):
            raise ContractValidationError("admission must reserve at least one resource")
        capability = next(
            (item for item in self.envelope.capabilities if item.capability_id == capability_id),
            None,
        )
        if capability is None:
            raise ProgramEnvelopeError(
                "capability_unresolved", "capability is absent from the envelope"
            )
        if capability.lane_id != lane_id:
            raise ProgramEnvelopeError(
                "capability_lane_mismatch", "capability belongs to another lane"
            )
        if capability.maturity is ProgramEnvelopeCapabilityMaturity.UNSUPPORTED:
            return self._stopped(operation_id, lane_id, capability_id, "capability_unsupported")
        lane_limit = next(
            (item for item in self.envelope.lane_limits if item.lane_id == lane_id),
            None,
        )
        lane_usage = next(
            (item for item in self.usage.lane_usage if item.lane_id == lane_id),
            ProgramEnvelopeLaneUsage(lane_id),
        )
        projected = {key: getattr(self.usage, key) + value for key, value in amounts.items()}
        global_limits = {
            "calls": self.envelope.limits.max_calls,
            "spend_minor_units": self.envelope.limits.max_spend_minor_units,
            "compute_seconds": self.envelope.limits.max_compute_seconds,
            "storage_bytes": self.envelope.limits.max_storage_bytes,
            "retention_days": self.envelope.limits.max_retention_days,
            "reviewer_minutes": self.envelope.limits.max_reviewer_minutes,
            "candidates": self.envelope.limits.max_candidates,
            "wall_seconds": self.envelope.limits.max_wall_seconds,
        }
        if any(projected[key] > global_limits[key] for key in projected):
            return self._stopped(operation_id, lane_id, capability_id, "limit_exhausted")
        for rule in self.envelope.stop_rules:
            if projected[rule.resource.value] >= rule.threshold:
                return self._stopped(
                    operation_id,
                    lane_id,
                    capability_id,
                    f"stop_rule.{rule.rule_id}",
                    decision=rule.decision,
                )
        if lane_limit is not None and (
            lane_usage.calls + amounts["calls"] > lane_limit.max_calls
            or lane_usage.spend_minor_units + amounts["spend_minor_units"]
            > lane_limit.max_spend_minor_units
            or lane_usage.compute_seconds + amounts["compute_seconds"]
            > lane_limit.max_compute_seconds
            or lane_usage.wall_seconds + amounts["wall_seconds"] > lane_limit.max_wall_seconds
        ):
            return self._stopped(operation_id, lane_id, capability_id, "limit_exhausted")
        next_lane_usage = ProgramEnvelopeLaneUsage(
            lane_id,
            calls=lane_usage.calls + amounts["calls"],
            spend_minor_units=lane_usage.spend_minor_units + amounts["spend_minor_units"],
            compute_seconds=lane_usage.compute_seconds + amounts["compute_seconds"],
            wall_seconds=lane_usage.wall_seconds + amounts["wall_seconds"],
        )
        lanes = tuple(item for item in self.usage.lane_usage if item.lane_id != lane_id) + (
            next_lane_usage,
        )
        next_usage = ProgramEnvelopeUsage(
            **projected,
            lane_usage=tuple(sorted(lanes, key=lambda item: item.lane_id)),
        )
        next_gate = ProgramEnvelopeGate(self.envelope, next_usage)
        return ProgramEnvelopeAdmission(
            operation_id,
            lane_id,
            capability_id,
            ProgramEnvelopeDecision.CONTINUE,
            "accepted",
            next_usage,
            next_gate,
        )

    def _stopped(
        self,
        operation_id: str,
        lane_id: str,
        capability_id: str,
        reason_code: str,
        *,
        decision: ProgramEnvelopeDecision = ProgramEnvelopeDecision.STOP,
    ) -> ProgramEnvelopeAdmission:
        return ProgramEnvelopeAdmission(
            operation_id,
            lane_id,
            capability_id,
            decision,
            reason_code,
            self.usage,
            self,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": PROGRAM_ENVELOPE_SCHEMA,
            "envelope": self.envelope.to_public_dict(),
            "usage": self.usage.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReconstructionBaselineRun:
    """Baseline report plus the immutable envelope state after measurement."""

    report: ReconstructionBaselineReport
    next_gate: ProgramEnvelopeGate

    def __post_init__(self) -> None:
        if not isinstance(self.report, ReconstructionBaselineReport):
            raise ContractValidationError("baseline run report is invalid")
        if not isinstance(self.next_gate, ProgramEnvelopeGate):
            raise ContractValidationError("baseline run next_gate is invalid")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": RECONSTRUCTION_BASELINE_SCHEMA,
            "report": self.report.to_public_dict(),
            "next_gate": self.next_gate.to_public_dict(),
        }


@runtime_checkable
class ReconstructionBaselineExecutor(Protocol):
    """Injected manual/deterministic measurement seam; no runtime is implied."""

    def execute(
        self,
        baseline_case: ReconstructionBaselineCase,
        *,
        layer: BaselineEvidenceLayer,
    ) -> BaselineObservation:
        """Return one layer-scoped observation with fingerprints and bounded metrics."""


@dataclass(frozen=True, slots=True)
class ReconstructionBaselineRunner:
    """Measure every frozen case/layer pair under one explicit envelope."""

    def run(
        self,
        cases: tuple[ReconstructionBaselineCase, ...],
        executor: ReconstructionBaselineExecutor,
        *,
        envelope: ProgramEnvelope,
        baseline_id: str,
        baseline_revision: str,
        as_of: str,
        lane_id: str = "manual_baseline",
        capability_id: str = "manual.baseline",
        error_taxonomy: tuple[ReconstructionErrorTaxonomyEntry, ...] = (),
        regressions: tuple[ReconstructionRegressionItem, ...] = (),
    ) -> ReconstructionBaselineRun:
        if not isinstance(cases, tuple) or not cases or len(cases) > MAX_BASELINE_CASES:
            raise ContractValidationError("baseline cases must be bounded and non-empty")
        if not all(isinstance(case, ReconstructionBaselineCase) for case in cases):
            raise ContractValidationError("baseline cases contain an invalid value")
        if len({case.case_id for case in cases}) != len(cases):
            raise ContractValidationError("baseline case IDs must be unique")
        if not isinstance(executor, ReconstructionBaselineExecutor):
            raise ContractValidationError("baseline executor must implement execute")
        if not isinstance(envelope, ProgramEnvelope):
            raise ContractValidationError("baseline envelope is invalid")
        _identifier(baseline_id, "baseline_id")
        _identifier(baseline_revision, "baseline_revision")
        _identifier(lane_id, "baseline lane_id")
        _identifier(capability_id, "baseline capability_id")
        gate = ProgramEnvelopeGate(envelope)
        observations: list[BaselineObservation] = []
        for index, baseline_case in enumerate(cases, start=1):
            admission = gate.admit(
                lane_id=lane_id,
                capability_id=capability_id,
                operation_id=f"baseline.{index}",
                as_of=as_of,
                calls=1,
            )
            if admission.decision is not ProgramEnvelopeDecision.CONTINUE:
                for layer in baseline_case.required_layers:
                    observations.append(
                        BaselineObservation(
                            baseline_case.case_id,
                            layer,
                            BaselineOutcome.BLOCKED,
                            lane_id,
                            baseline_case.input_fingerprints,
                            diagnostic_codes=(f"envelope.{admission.reason_code}",),
                            source_revision=baseline_revision,
                        )
                    )
                continue
            gate = admission.next_gate
            for layer in baseline_case.required_layers:
                try:
                    observation = executor.execute(baseline_case, layer=layer)
                except Exception:
                    observation = BaselineObservation(
                        baseline_case.case_id,
                        layer,
                        BaselineOutcome.FAILED,
                        lane_id,
                        baseline_case.input_fingerprints,
                        diagnostic_codes=("executor_failed",),
                        source_revision=baseline_revision,
                    )
                if not isinstance(observation, BaselineObservation):
                    observation = BaselineObservation(
                        baseline_case.case_id,
                        layer,
                        BaselineOutcome.FAILED,
                        lane_id,
                        baseline_case.input_fingerprints,
                        diagnostic_codes=("executor_contract_invalid",),
                        source_revision=baseline_revision,
                    )
                if (
                    observation.case_id != baseline_case.case_id
                    or observation.layer is not layer
                    or observation.input_fingerprints != baseline_case.input_fingerprints
                ):
                    observation = BaselineObservation(
                        baseline_case.case_id,
                        layer,
                        BaselineOutcome.FAILED,
                        lane_id,
                        baseline_case.input_fingerprints,
                        diagnostic_codes=("executor_contract_mismatch",),
                        source_revision=baseline_revision,
                    )
                observations.append(observation)

        observation_tuple = tuple(observations)
        summaries = tuple(
            self._summary(layer, observation_tuple) for layer in BaselineEvidenceLayer
        )
        missing_count = sum(item.outcome is BaselineOutcome.MISSING for item in observation_tuple)
        failed_count = sum(item.outcome is BaselineOutcome.FAILED for item in observation_tuple)
        blocked_count = sum(item.outcome is BaselineOutcome.BLOCKED for item in observation_tuple)
        if failed_count:
            status = BaselineReportStatus.FAILED
        elif (
            missing_count
            or blocked_count
            or any(item.outcome is BaselineOutcome.NOT_APPLICABLE for item in observation_tuple)
        ):
            status = BaselineReportStatus.PARTIAL
        else:
            status = BaselineReportStatus.COMPLETE
        structural = tuple(
            item for item in observation_tuple if item.layer is BaselineEvidenceLayer.STRUCTURAL
        )
        if all(item.outcome is BaselineOutcome.PASSED for item in structural):
            ceiling = (
                BaselineClaimCeiling.QUALIFIED_LOCAL
                if status is BaselineReportStatus.COMPLETE
                else BaselineClaimCeiling.STRUCTURAL_ONLY
            )
        else:
            ceiling = BaselineClaimCeiling.NO_CLAIM
        taxonomy = self._taxonomy(error_taxonomy, observation_tuple)
        regression_tuple = self._regressions(regressions, taxonomy, observation_tuple)
        report = ReconstructionBaselineReport(
            baseline_id=baseline_id,
            baseline_revision=baseline_revision,
            corpus_id=cases[0].corpus_id,
            envelope_id=envelope.envelope_id,
            envelope_revision=envelope.revision,
            envelope_fingerprint=envelope.fingerprint,
            cases=cases,
            observations=observation_tuple,
            layer_summary=summaries,
            error_taxonomy=taxonomy,
            regressions=regression_tuple,
            status=status,
            claim_ceiling=ceiling,
            missing_observation_count=missing_count,
            failed_observation_count=failed_count,
            blocked_observation_count=blocked_count,
        )
        return ReconstructionBaselineRun(report=report, next_gate=gate)

    @staticmethod
    def _summary(
        layer: BaselineEvidenceLayer,
        observations: tuple[BaselineObservation, ...],
    ) -> BaselineLayerSummary:
        selected = tuple(item for item in observations if item.layer is layer)
        counts = Counter(item.outcome for item in selected)
        return BaselineLayerSummary(
            layer,
            len(selected),
            counts[BaselineOutcome.PASSED],
            counts[BaselineOutcome.FAILED],
            counts[BaselineOutcome.MISSING],
            counts[BaselineOutcome.BLOCKED],
            counts[BaselineOutcome.NOT_APPLICABLE],
        )

    @staticmethod
    def _taxonomy(
        requested: tuple[ReconstructionErrorTaxonomyEntry, ...],
        observations: tuple[BaselineObservation, ...],
    ) -> tuple[ReconstructionErrorTaxonomyEntry, ...]:
        if requested:
            return requested
        rows: dict[str, ReconstructionErrorTaxonomyEntry] = {}
        for observation in observations:
            for code in observation.diagnostic_codes:
                rows.setdefault(
                    code,
                    ReconstructionErrorTaxonomyEntry(
                        code,
                        observation.layer,
                        observation.outcome.value,
                        "observed baseline diagnostic",
                    ),
                )
        return tuple(rows[key] for key in sorted(rows))

    @staticmethod
    def _regressions(
        requested: tuple[ReconstructionRegressionItem, ...],
        taxonomy: tuple[ReconstructionErrorTaxonomyEntry, ...],
        observations: tuple[BaselineObservation, ...],
    ) -> tuple[ReconstructionRegressionItem, ...]:
        if requested:
            return tuple(
                sorted(requested, key=lambda item: (item.priority.value, item.regression_id))
            )
        counts = Counter(
            code for observation in observations for code in observation.diagnostic_codes
        )
        by_code = {item.error_code: item for item in taxonomy}
        generated = []
        for code, count in sorted(counts.items()):
            item = by_code[code]
            generated.append(
                ReconstructionRegressionItem(
                    f"regression.{code}",
                    code,
                    item.layer,
                    ProgramEnvelopeRegressionPriority.P1,
                    "open",
                    count,
                    "baseline execution is incomplete or failed",
                    "qualify or explicitly disposition the affected lane",
                )
            )
        priority_order = {
            priority: index for index, priority in enumerate(ProgramEnvelopeRegressionPriority)
        }
        return tuple(
            sorted(generated, key=lambda item: (priority_order[item.priority], item.regression_id))
        )


__all__ = [
    "RECONSTRUCTION_BASELINE_SCHEMA",
    "PROGRAM_ENVELOPE_SCHEMA",
    "MAX_BASELINE_CASES",
    "MAX_BASELINE_OBSERVATIONS",
    "BaselineClaimCeiling",
    "BaselineEvidenceLayer",
    "BaselineLayerSummary",
    "BaselineObservation",
    "BaselineOutcome",
    "BaselineReportStatus",
    "ProgramEnvelope",
    "ProgramEnvelopeAdmission",
    "ProgramEnvelopeCapability",
    "ProgramEnvelopeCapabilityMaturity",
    "ProgramEnvelopeDecision",
    "ProgramEnvelopeError",
    "ProgramEnvelopeGate",
    "ProgramEnvelopeLaneLimits",
    "ProgramEnvelopeLaneUsage",
    "ProgramEnvelopeLimits",
    "ProgramEnvelopeRegressionPriority",
    "ProgramEnvelopeResource",
    "ProgramEnvelopeReauthorizationTrigger",
    "ProgramEnvelopeStopRule",
    "ProgramEnvelopeUsage",
    "ReconstructionBaselineCase",
    "ReconstructionBaselineExecutor",
    "ReconstructionBaselineReport",
    "ReconstructionBaselineRun",
    "ReconstructionBaselineRunner",
    "ReconstructionErrorTaxonomyEntry",
    "ReconstructionRegressionItem",
]
