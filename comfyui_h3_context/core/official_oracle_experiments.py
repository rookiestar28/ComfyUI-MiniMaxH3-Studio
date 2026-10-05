"""Controlled, offline differential experiments for the official-oracle boundary.

M9-06 is deliberately a measurement harness, not an oracle client.  It accepts an
injected mock or recorded executor, admits every permitted operation through the
M9-04 governance gate, and emits only bounded fingerprints and aggregate outcomes.
Live oracle variability is represented as missing until a separately approved lane
and transport exist; missingness is never converted into an estimate.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Protocol, runtime_checkable

from .canonical import canonical_fingerprint
from .contracts import ProviderIdentity
from .errors import ContractValidationError
from .official_context_ir import (
    OFFICIAL_CONTEXT_IR_ENDPOINT_REVISION,
    OFFICIAL_CONTEXT_IR_PROVIDER_VERSION,
)
from .official_oracle_governance import (
    OfficialOracleClaimCeiling,
    OfficialOracleExecutionGate,
    OfficialOracleExecutionMode,
    OfficialOracleExecutionScope,
    OfficialOracleLaneDisposition,
    OfficialOracleLaneStatus,
    OfficialOracleOperation,
    OfficialOracleRetentionPolicy,
)

OFFICIAL_ORACLE_EXPERIMENT_SCHEMA = "h3.context.ir.experiment.v1"
MAX_EXPERIMENT_CASES = 64
MAX_EXPERIMENT_REPLICATES = 64
MAX_EXPERIMENT_HYPOTHESES = 16
MAX_EXPERIMENT_DIAGNOSTICS = 32
MAX_EXPERIMENT_REVISION_LENGTH = 128
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
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


class OfficialOracleExperimentFactor(str, Enum):
    """One controlled input factor varied by a differential case."""

    WORDING = "wording"
    REFERENCE_ORDER = "reference_order"
    ROLE = "role"
    DURATION = "duration"
    AMBIGUITY = "ambiguity"
    MEDIA_COMPOSITION = "media_composition"
    DIALOGUE = "dialogue"
    OCR = "ocr"
    AUDIO = "audio"
    FAILURE_INPUT = "failure_input"


class OfficialOracleExperimentSampleStatus(str, Enum):
    """Result returned by an injected executor for one sample."""

    RECORDED = "recorded"
    FAILED = "failed"


class OfficialOracleExperimentObservationStatus(str, Enum):
    """Hash-only observation status after lane admission and execution."""

    RECORDED = "recorded"
    MISSING = "missing"
    FAILED = "failed"


class OfficialOracleExperimentPairStatus(str, Enum):
    """Comparison result for aligned baseline and variant observations."""

    MATCHED = "matched"
    DIFFERENT = "different"
    MISSING = "missing"
    FAILED = "failed"


class OfficialOracleExperimentVariabilityStatus(str, Enum):
    """Whether repeated output agreement was measured for a pair side."""

    MEASURED = "measured"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class OfficialOracleExperimentReportStatus(str, Enum):
    """Aggregate disposition of a differential run."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


def _sensitive_identifier(value: str) -> bool:
    segments = tuple(part for part in re.split(r"[_.:-]+", value.casefold()) if part)
    return any(segment in _SENSITIVE_SEGMENTS for segment in segments)


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded identifier")
    if _sensitive_identifier(value):
        raise ContractValidationError(f"{field_name} must not contain sensitive markers")
    return value


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a sha256 fingerprint")
    return value


def _require_raw_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContractValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _require_optional_hash(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_hash(value, field_name)


def _require_enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise ContractValidationError(f"{field_name} must be a {expected.__name__}")


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be a boolean")
    return value


def _require_positive_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise ContractValidationError(f"{field_name} must be a bounded positive integer")
    return value


def _require_tuple(value: object, field_name: str, maximum: int) -> tuple[object, ...]:
    if not isinstance(value, tuple) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field_name} must be a bounded non-empty tuple")
    return value


def _require_diagnostic_tuple(
    value: object, field_name: str = "diagnostic_codes"
) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_EXPERIMENT_DIAGNOSTICS:
        raise ContractValidationError(f"{field_name} must be a bounded tuple")
    if len(set(value)) != len(value):
        raise ContractValidationError(f"{field_name} must contain unique codes")
    for item in value:
        _require_identifier(item, field_name)
    return value


def _require_safe_metadata(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_EXPERIMENT_REVISION_LENGTH:
        raise ContractValidationError(f"{field_name} must be bounded metadata")
    if _CONTROL_PATTERN.search(value) or any(
        marker in value.casefold()
        for marker in ("http://", "https://", "bearer ", "token=", "api_key", "password")
    ):
        raise ContractValidationError(f"{field_name} contains unsafe metadata")
    return value


@dataclass(frozen=True, slots=True)
class OfficialOracleExperimentCase:
    """One paired baseline/variant case with exactly one declared changed factor."""

    case_id: str
    factor: OfficialOracleExperimentFactor
    baseline_input_fingerprint: str
    variant_input_fingerprint: str
    changed_fields: tuple[str, ...]
    source_fingerprints: tuple[str, ...]
    hypothesis_ids: tuple[str, ...] = ()
    replicates: int = 1
    negative_control: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.case_id, "experiment case_id")
        _require_enum(self.factor, OfficialOracleExperimentFactor, "experiment factor")
        _require_hash(self.baseline_input_fingerprint, "baseline_input_fingerprint")
        _require_hash(self.variant_input_fingerprint, "variant_input_fingerprint")
        _require_bool(self.negative_control, "negative_control")
        if (
            self.baseline_input_fingerprint == self.variant_input_fingerprint
            and not self.negative_control
        ):
            raise ContractValidationError("baseline and variant fingerprints must differ")
        if not isinstance(self.changed_fields, tuple) or self.changed_fields != (
            self.factor.value,
        ):
            raise ContractValidationError("changed_fields must contain exactly the selected factor")
        for field_name in self.changed_fields:
            _require_identifier(field_name, "changed field")
        source_fingerprints = _require_tuple(
            self.source_fingerprints, "source_fingerprints", MAX_EXPERIMENT_HYPOTHESES
        )
        for fingerprint in source_fingerprints:
            _require_hash(fingerprint, "source fingerprint")
        if len(set(source_fingerprints)) != len(source_fingerprints):
            raise ContractValidationError("source_fingerprints must be unique")
        if (
            not isinstance(self.hypothesis_ids, tuple)
            or len(self.hypothesis_ids) > MAX_EXPERIMENT_HYPOTHESES
        ):
            raise ContractValidationError("hypothesis_ids must be a bounded tuple")
        if len(set(self.hypothesis_ids)) != len(self.hypothesis_ids):
            raise ContractValidationError("hypothesis_ids must be unique")
        for hypothesis_id in self.hypothesis_ids:
            _require_identifier(hypothesis_id, "hypothesis_id")
        _require_positive_int(self.replicates, "replicates", MAX_EXPERIMENT_REPLICATES)

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_public_dict())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_EXPERIMENT_SCHEMA,
            "case_id": self.case_id,
            "factor": self.factor.value,
            "baseline_input_fingerprint": self.baseline_input_fingerprint,
            "variant_input_fingerprint": self.variant_input_fingerprint,
            "changed_fields": list(self.changed_fields),
            "source_fingerprints": list(self.source_fingerprints),
            "hypothesis_ids": list(self.hypothesis_ids),
            "replicates": self.replicates,
            "negative_control": self.negative_control,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleExperimentSample:
    """Executor return value; output content itself is never accepted."""

    status: OfficialOracleExperimentSampleStatus
    output_fingerprint: str | None = None
    diagnostic_code: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.status, OfficialOracleExperimentSampleStatus, "sample status")
        _require_optional_hash(self.output_fingerprint, "sample output_fingerprint")
        if self.diagnostic_code is not None:
            _require_identifier(self.diagnostic_code, "sample diagnostic_code")
        if self.status is OfficialOracleExperimentSampleStatus.RECORDED:
            if self.output_fingerprint is None or self.diagnostic_code is not None:
                raise ContractValidationError("recorded sample requires only an output fingerprint")
        elif self.diagnostic_code is None:
            raise ContractValidationError("failed sample requires a diagnostic code")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_EXPERIMENT_SCHEMA,
            "status": self.status.value,
            "output_fingerprint": self.output_fingerprint,
            "diagnostic_code": self.diagnostic_code,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleExperimentObservation:
    """One admitted, missing, or failed hash-only execution observation."""

    case_id: str
    variant: bool
    replicate_index: int
    lane_id: str
    execution_mode: OfficialOracleExecutionMode
    status: OfficialOracleExperimentObservationStatus
    request_fingerprint: str
    output_fingerprint: str | None
    diagnostic_code: str | None
    lane_status: OfficialOracleLaneStatus
    execution_scope: OfficialOracleExecutionScope
    claim_ceiling: OfficialOracleClaimCeiling
    retention_policy: OfficialOracleRetentionPolicy
    source_ledger_fingerprint: str
    terms_revision: str
    policy_revision: str
    source_revision: str = OFFICIAL_CONTEXT_IR_ENDPOINT_REVISION

    def __post_init__(self) -> None:
        _require_identifier(self.case_id, "observation case_id")
        _require_bool(self.variant, "observation variant")
        _require_positive_int(
            self.replicate_index, "observation replicate_index", MAX_EXPERIMENT_REPLICATES
        )
        _require_identifier(self.lane_id, "observation lane_id")
        _require_enum(
            self.execution_mode, OfficialOracleExecutionMode, "observation execution_mode"
        )
        _require_enum(self.status, OfficialOracleExperimentObservationStatus, "observation status")
        _require_hash(self.request_fingerprint, "observation request_fingerprint")
        _require_optional_hash(self.output_fingerprint, "observation output_fingerprint")
        if self.diagnostic_code is not None:
            _require_identifier(self.diagnostic_code, "observation diagnostic_code")
        _require_enum(self.lane_status, OfficialOracleLaneStatus, "observation lane_status")
        _require_enum(
            self.execution_scope, OfficialOracleExecutionScope, "observation execution_scope"
        )
        _require_enum(self.claim_ceiling, OfficialOracleClaimCeiling, "observation claim_ceiling")
        _require_enum(
            self.retention_policy, OfficialOracleRetentionPolicy, "observation retention_policy"
        )
        _require_raw_hash(self.source_ledger_fingerprint, "observation source_ledger_fingerprint")
        _require_identifier(self.terms_revision, "observation terms_revision")
        _require_identifier(self.policy_revision, "observation policy_revision")
        _require_identifier(self.source_revision, "observation source_revision")
        if self.status is OfficialOracleExperimentObservationStatus.RECORDED:
            if self.output_fingerprint is None or self.diagnostic_code is not None:
                raise ContractValidationError(
                    "recorded observation requires only an output fingerprint"
                )
        elif self.status is OfficialOracleExperimentObservationStatus.MISSING:
            if self.output_fingerprint is not None or self.diagnostic_code is None:
                raise ContractValidationError("missing observation requires a diagnostic code only")
        elif self.diagnostic_code is None:
            raise ContractValidationError("failed observation requires a diagnostic code")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_EXPERIMENT_SCHEMA,
            "case_id": self.case_id,
            "variant": self.variant,
            "replicate_index": self.replicate_index,
            "lane_id": self.lane_id,
            "execution_mode": self.execution_mode.value,
            "status": self.status.value,
            "request_fingerprint": self.request_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "diagnostic_code": self.diagnostic_code,
            "lane_status": self.lane_status.value,
            "execution_scope": self.execution_scope.value,
            "claim_ceiling": self.claim_ceiling.value,
            "retention_policy": self.retention_policy.value,
            "source_ledger_fingerprint": self.source_ledger_fingerprint,
            "terms_revision": self.terms_revision,
            "policy_revision": self.policy_revision,
            "source_revision": self.source_revision,
        }


def _require_ratio(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 32 or _CONTROL_PATTERN.search(value):
        raise ContractValidationError(f"{field_name} must be a bounded decimal ratio")
    try:
        ratio = Decimal(value)
    except InvalidOperation as exc:
        raise ContractValidationError(f"{field_name} must be a decimal ratio") from exc
    if not ratio.is_finite() or ratio < 0 or ratio > 1:
        raise ContractValidationError(f"{field_name} must be between zero and one")
    if ratio != ratio.normalize() or ("." in value and value.endswith("0")):
        raise ContractValidationError(f"{field_name} must use a normalized decimal form")
    return value


@dataclass(frozen=True, slots=True)
class OfficialOracleExperimentCaseResult:
    """Aggregate comparison and repeatability result for one experiment case."""

    case: OfficialOracleExperimentCase
    observations: tuple[OfficialOracleExperimentObservation, ...]
    pair_status: OfficialOracleExperimentPairStatus
    baseline_agreement_ratio: str | None
    variant_agreement_ratio: str | None
    variability_status: OfficialOracleExperimentVariabilityStatus
    diagnostic_codes: tuple[str, ...] = ()
    negative_control_violation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.case, OfficialOracleExperimentCase):
            raise ContractValidationError("case result case is invalid")
        if not isinstance(self.observations, tuple) or not self.observations:
            raise ContractValidationError("case result observations must be non-empty")
        if not all(
            isinstance(observation, OfficialOracleExperimentObservation)
            for observation in self.observations
        ):
            raise ContractValidationError("case result contains an invalid observation")
        if any(observation.case_id != self.case.case_id for observation in self.observations):
            raise ContractValidationError("case result observation IDs do not match the case")
        expected_count = self.case.replicates * 2
        if len(self.observations) != expected_count:
            raise ContractValidationError("case result must contain both sides for every replicate")
        if (
            len({(item.variant, item.replicate_index) for item in self.observations})
            != expected_count
        ):
            raise ContractValidationError("case result observations must be aligned and unique")
        _require_enum(self.pair_status, OfficialOracleExperimentPairStatus, "pair_status")
        _require_ratio(self.baseline_agreement_ratio, "baseline_agreement_ratio")
        _require_ratio(self.variant_agreement_ratio, "variant_agreement_ratio")
        _require_enum(
            self.variability_status, OfficialOracleExperimentVariabilityStatus, "variability_status"
        )
        _require_diagnostic_tuple(self.diagnostic_codes)
        _require_bool(self.negative_control_violation, "negative_control_violation")

    @property
    def case_id(self) -> str:
        return self.case.case_id

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_EXPERIMENT_SCHEMA,
            "case": self.case.to_public_dict(),
            "observations": [observation.to_public_dict() for observation in self.observations],
            "pair_status": self.pair_status.value,
            "baseline_agreement_ratio": self.baseline_agreement_ratio,
            "variant_agreement_ratio": self.variant_agreement_ratio,
            "variability_status": self.variability_status.value,
            "diagnostic_codes": list(self.diagnostic_codes),
            "negative_control_violation": self.negative_control_violation,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleExperimentReport:
    """Hash-only report for a bounded differential experiment run."""

    experiment_id: str
    experiment_revision: str
    lane_id: str
    lane_status: OfficialOracleLaneStatus
    execution_scope: OfficialOracleExecutionScope
    execution_mode: OfficialOracleExecutionMode
    claim_ceiling: OfficialOracleClaimCeiling
    retention_policy: OfficialOracleRetentionPolicy
    source_ledger_id: str
    source_ledger_fingerprint: str
    terms_revision: str
    policy_revision: str
    cases: tuple[OfficialOracleExperimentCase, ...]
    results: tuple[OfficialOracleExperimentCaseResult, ...]
    status: OfficialOracleExperimentReportStatus
    missing_observation_count: int
    failed_observation_count: int
    pair_status_counts: tuple[tuple[str, int], ...]
    variability_status: OfficialOracleExperimentVariabilityStatus
    diagnostic_codes: tuple[str, ...] = ()
    negative_control_violations: int = 0
    provider: ProviderIdentity = ProviderIdentity.OFFICIAL_MINIMAX
    provider_version: str = OFFICIAL_CONTEXT_IR_PROVIDER_VERSION
    source_revision: str = OFFICIAL_CONTEXT_IR_ENDPOINT_REVISION

    def __post_init__(self) -> None:
        _require_identifier(self.experiment_id, "experiment_id")
        _require_identifier(self.experiment_revision, "experiment_revision")
        _require_identifier(self.lane_id, "report lane_id")
        _require_enum(self.lane_status, OfficialOracleLaneStatus, "report lane_status")
        _require_enum(self.execution_scope, OfficialOracleExecutionScope, "report execution_scope")
        _require_enum(self.execution_mode, OfficialOracleExecutionMode, "report execution_mode")
        _require_enum(self.claim_ceiling, OfficialOracleClaimCeiling, "report claim_ceiling")
        _require_enum(
            self.retention_policy, OfficialOracleRetentionPolicy, "report retention_policy"
        )
        _require_identifier(self.source_ledger_id, "report source_ledger_id")
        _require_raw_hash(self.source_ledger_fingerprint, "report source_ledger_fingerprint")
        _require_identifier(self.terms_revision, "report terms_revision")
        _require_identifier(self.policy_revision, "report policy_revision")
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_EXPERIMENT_CASES
        ):
            raise ContractValidationError("report cases must be a bounded non-empty tuple")
        if not all(isinstance(case, OfficialOracleExperimentCase) for case in self.cases):
            raise ContractValidationError("report cases contain an invalid value")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ContractValidationError("report case IDs must be unique")
        if not isinstance(self.results, tuple) or len(self.results) != len(self.cases):
            raise ContractValidationError("report results must align with cases")
        if not all(
            isinstance(result, OfficialOracleExperimentCaseResult) for result in self.results
        ):
            raise ContractValidationError("report results contain an invalid value")
        if tuple(result.case_id for result in self.results) != tuple(
            case.case_id for case in self.cases
        ):
            raise ContractValidationError("report result order must match case order")
        _require_enum(self.status, OfficialOracleExperimentReportStatus, "report status")
        _require_non_negative_count(self.missing_observation_count, "missing_observation_count")
        _require_non_negative_count(self.failed_observation_count, "failed_observation_count")
        if not isinstance(self.pair_status_counts, tuple):
            raise ContractValidationError("pair_status_counts must be a tuple")
        seen: set[str] = set()
        for key, count in self.pair_status_counts:
            _require_identifier(key, "pair status count key")
            if key in seen:
                raise ContractValidationError("pair_status_counts keys must be unique")
            seen.add(key)
            _require_non_negative_count(count, "pair status count")
        if sum(count for _, count in self.pair_status_counts) != len(self.results):
            raise ContractValidationError("pair_status_counts must account for every result")
        _require_enum(
            self.variability_status,
            OfficialOracleExperimentVariabilityStatus,
            "report variability_status",
        )
        _require_diagnostic_tuple(self.diagnostic_codes)
        _require_non_negative_count(self.negative_control_violations, "negative_control_violations")
        if self.negative_control_violations > len(self.cases):
            raise ContractValidationError("negative_control_violations exceeds case count")
        if self.provider is not ProviderIdentity.OFFICIAL_MINIMAX:
            raise ContractValidationError("experiment provider must be official_minimax")
        _require_identifier(self.provider_version, "report provider_version")
        _require_identifier(self.source_revision, "report source_revision")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self._fingerprint_payload())

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_EXPERIMENT_SCHEMA,
            "experiment_id": self.experiment_id,
            "experiment_revision": self.experiment_revision,
            "lane_id": self.lane_id,
            "lane_status": self.lane_status.value,
            "execution_scope": self.execution_scope.value,
            "execution_mode": self.execution_mode.value,
            "claim_ceiling": self.claim_ceiling.value,
            "retention_policy": self.retention_policy.value,
            "source_ledger_id": self.source_ledger_id,
            "source_ledger_fingerprint": self.source_ledger_fingerprint,
            "terms_revision": self.terms_revision,
            "policy_revision": self.policy_revision,
            "cases": [case.to_public_dict() for case in self.cases],
            "results": [result.to_public_dict() for result in self.results],
            "status": self.status.value,
            "missing_observation_count": self.missing_observation_count,
            "failed_observation_count": self.failed_observation_count,
            "pair_status_counts": [list(item) for item in self.pair_status_counts],
            "variability_status": self.variability_status.value,
            "diagnostic_codes": list(self.diagnostic_codes),
            "negative_control_violations": self.negative_control_violations,
            "provider": self.provider.value,
            "provider_version": self.provider_version,
            "source_revision": self.source_revision,
        }

    def to_public_dict(self) -> dict[str, object]:
        payload = self._fingerprint_payload()
        payload["fingerprint"] = self.fingerprint
        return payload


def _require_non_negative_count(value: object, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_EXPERIMENT_CASES * MAX_EXPERIMENT_REPLICATES * 2
    ):
        raise ContractValidationError(f"{field_name} must be a bounded non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class OfficialOracleExperimentRun:
    """Report plus the immutable governance state after attempted admissions."""

    report: OfficialOracleExperimentReport
    next_gate: OfficialOracleExecutionGate

    def __post_init__(self) -> None:
        if not isinstance(self.report, OfficialOracleExperimentReport):
            raise ContractValidationError("experiment run report is invalid")
        if not isinstance(self.next_gate, OfficialOracleExecutionGate):
            raise ContractValidationError("experiment run next_gate is invalid")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_EXPERIMENT_SCHEMA,
            "report": self.report.to_public_dict(),
            "next_gate": self.next_gate.to_public_dict(),
        }


@runtime_checkable
class OfficialOracleExperimentExecutor(Protocol):
    """Injected deterministic mock, recorded fixture, or local relation executor."""

    def execute(
        self,
        experiment_case: OfficialOracleExperimentCase,
        *,
        variant: bool,
        replicate_index: int,
    ) -> OfficialOracleExperimentSample:
        """Return a hash-only sample or a typed diagnostic outcome."""


def _normalized_ratio(matches: int, total: int) -> str:
    ratio = (Decimal(matches) / Decimal(total)).normalize()
    text = format(ratio, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _side_summary(
    observations: tuple[OfficialOracleExperimentObservation, ...],
    *,
    variant: bool,
) -> tuple[str | None, OfficialOracleExperimentVariabilityStatus]:
    side = tuple(item for item in observations if item.variant is variant)
    if any(item.status is not OfficialOracleExperimentObservationStatus.RECORDED for item in side):
        return None, OfficialOracleExperimentVariabilityStatus.MISSING
    if len(side) < 2:
        return None, OfficialOracleExperimentVariabilityStatus.NOT_APPLICABLE
    first = side[0].output_fingerprint
    matches = sum(item.output_fingerprint == first for item in side)
    return _normalized_ratio(matches, len(side)), OfficialOracleExperimentVariabilityStatus.MEASURED


def _pair_summary(
    case: OfficialOracleExperimentCase,
    observations: tuple[OfficialOracleExperimentObservation, ...],
) -> tuple[OfficialOracleExperimentPairStatus, tuple[str, ...], bool]:
    diagnostics: tuple[str, ...] = ()
    if any(
        item.status is OfficialOracleExperimentObservationStatus.FAILED for item in observations
    ):
        pair_status = OfficialOracleExperimentPairStatus.FAILED
        diagnostics = ("observation.failed",)
    elif any(
        item.status is OfficialOracleExperimentObservationStatus.MISSING for item in observations
    ):
        pair_status = OfficialOracleExperimentPairStatus.MISSING
        diagnostics = ("observation.missing",)
    else:
        baseline = {
            item.replicate_index: item.output_fingerprint
            for item in observations
            if not item.variant
        }
        variant = {
            item.replicate_index: item.output_fingerprint for item in observations if item.variant
        }
        pair_status = (
            OfficialOracleExperimentPairStatus.MATCHED
            if all(baseline[index] == variant[index] for index in baseline)
            else OfficialOracleExperimentPairStatus.DIFFERENT
        )
        diagnostics = ()
    negative_control_violation = (
        case.negative_control and pair_status is OfficialOracleExperimentPairStatus.DIFFERENT
    )
    if negative_control_violation:
        diagnostics = (*diagnostics, "negative_control.output_differs")
    return pair_status, diagnostics, negative_control_violation


@dataclass(frozen=True, slots=True)
class OfficialOracleExperimentHarness:
    """Run paired one-factor experiments through a reviewed offline boundary."""

    gate: OfficialOracleExecutionGate

    def __post_init__(self) -> None:
        if not isinstance(self.gate, OfficialOracleExecutionGate):
            raise ContractValidationError("experiment harness gate is invalid")

    def run(
        self,
        cases: tuple[OfficialOracleExperimentCase, ...],
        executor: OfficialOracleExperimentExecutor,
        *,
        lane_id: str,
        execution_mode: OfficialOracleExecutionMode,
        now: str,
        rate_clock: float,
        experiment_id: str = "m9-06-differential",
        experiment_revision: str = "experiment.rev_1",
    ) -> OfficialOracleExperimentRun:
        """Execute a bounded run without ever invoking a live official endpoint.

        ``MOCKED`` and ``RECORDED`` calls consume one governance admission each.
        ``OFFLINE`` is reserved for a local relation executor and therefore has
        no provider/API admission; the lane still controls whether it can run.
        ``LIVE`` is always represented as missing, even if a future terms object
        happens to authorize that lane, until a separately reviewed transport is
        implemented.
        """

        self._validate_inputs(
            cases, executor, lane_id, execution_mode, experiment_id, experiment_revision
        )
        disposition = self.gate.terms.disposition_for(lane_id)
        current_gate = self.gate
        source_ledger_fingerprint = current_gate.source_ledger.fingerprint
        terms_revision = current_gate.terms.terms_revision
        policy_revision = current_gate.policy.policy_revision
        observations_by_case: list[
            tuple[OfficialOracleExperimentCase, tuple[OfficialOracleExperimentObservation, ...]]
        ] = []
        admitted_calls = 0

        for experiment_case in cases:
            observations: list[OfficialOracleExperimentObservation] = []
            for variant in (False, True):
                request_fingerprint = (
                    experiment_case.variant_input_fingerprint
                    if variant
                    else experiment_case.baseline_input_fingerprint
                )
                for replicate_index in range(1, experiment_case.replicates + 1):
                    if execution_mode is OfficialOracleExecutionMode.LIVE:
                        observations.append(
                            self._missing_observation(
                                experiment_case,
                                variant=variant,
                                replicate_index=replicate_index,
                                lane_id=lane_id,
                                execution_mode=execution_mode,
                                disposition=disposition,
                                request_fingerprint=request_fingerprint,
                                diagnostic_code="live_transport_unavailable",
                                source_ledger_fingerprint=source_ledger_fingerprint,
                                terms_revision=terms_revision,
                                policy_revision=policy_revision,
                            )
                        )
                        continue
                    if not disposition.allows(execution_mode):
                        observations.append(
                            self._missing_observation(
                                experiment_case,
                                variant=variant,
                                replicate_index=replicate_index,
                                lane_id=lane_id,
                                execution_mode=execution_mode,
                                disposition=disposition,
                                request_fingerprint=request_fingerprint,
                                diagnostic_code="lane_execution_not_permitted",
                                source_ledger_fingerprint=source_ledger_fingerprint,
                                terms_revision=terms_revision,
                                policy_revision=policy_revision,
                            )
                        )
                        continue

                    if execution_mode is not OfficialOracleExecutionMode.OFFLINE:
                        admission = current_gate.admit(
                            lane_id=lane_id,
                            execution_mode=execution_mode,
                            operation=OfficialOracleOperation.API_CALL,
                            now=now,
                            rate_clock=rate_clock + admitted_calls,
                            operation_id=self._operation_id(
                                experiment_case, variant, replicate_index
                            ),
                        )
                        current_gate = admission.next_gate
                        admitted_calls += 1

                    observations.append(
                        self._execute_observation(
                            experiment_case,
                            executor,
                            variant=variant,
                            replicate_index=replicate_index,
                            lane_id=lane_id,
                            execution_mode=execution_mode,
                            disposition=disposition,
                            request_fingerprint=request_fingerprint,
                            source_ledger_fingerprint=source_ledger_fingerprint,
                            terms_revision=terms_revision,
                            policy_revision=policy_revision,
                        )
                    )
            observations_by_case.append((experiment_case, tuple(observations)))

        results: list[OfficialOracleExperimentCaseResult] = []
        for experiment_case, observation_tuple in observations_by_case:
            baseline_ratio, baseline_variability = _side_summary(observation_tuple, variant=False)
            variant_ratio, variant_variability = _side_summary(observation_tuple, variant=True)
            pair_status, diagnostics, negative_control_violation = _pair_summary(
                experiment_case, observation_tuple
            )
            if (
                baseline_variability is OfficialOracleExperimentVariabilityStatus.MISSING
                or variant_variability is OfficialOracleExperimentVariabilityStatus.MISSING
            ):
                variability_status = OfficialOracleExperimentVariabilityStatus.MISSING
            elif (
                baseline_variability is OfficialOracleExperimentVariabilityStatus.MEASURED
                or variant_variability is OfficialOracleExperimentVariabilityStatus.MEASURED
            ):
                variability_status = OfficialOracleExperimentVariabilityStatus.MEASURED
            else:
                variability_status = OfficialOracleExperimentVariabilityStatus.NOT_APPLICABLE
            results.append(
                OfficialOracleExperimentCaseResult(
                    case=experiment_case,
                    observations=observation_tuple,
                    pair_status=pair_status,
                    baseline_agreement_ratio=baseline_ratio,
                    variant_agreement_ratio=variant_ratio,
                    variability_status=variability_status,
                    diagnostic_codes=diagnostics,
                    negative_control_violation=negative_control_violation,
                )
            )

        result_tuple = tuple(results)
        missing_count = sum(
            observation.status is OfficialOracleExperimentObservationStatus.MISSING
            for result in result_tuple
            for observation in result.observations
        )
        failed_count = sum(
            observation.status is OfficialOracleExperimentObservationStatus.FAILED
            for result in result_tuple
            for observation in result.observations
        )
        pair_counts = Counter(result.pair_status.value for result in result_tuple)
        if failed_count:
            report_status = OfficialOracleExperimentReportStatus.FAILED
        elif missing_count:
            report_status = OfficialOracleExperimentReportStatus.INCOMPLETE
        else:
            report_status = OfficialOracleExperimentReportStatus.COMPLETE
        if any(
            result.variability_status is OfficialOracleExperimentVariabilityStatus.MISSING
            for result in result_tuple
        ):
            aggregate_variability = OfficialOracleExperimentVariabilityStatus.MISSING
        elif any(
            result.variability_status is OfficialOracleExperimentVariabilityStatus.MEASURED
            for result in result_tuple
        ):
            aggregate_variability = OfficialOracleExperimentVariabilityStatus.MEASURED
        else:
            aggregate_variability = OfficialOracleExperimentVariabilityStatus.NOT_APPLICABLE
        diagnostics = tuple(
            sorted({code for result in result_tuple for code in result.diagnostic_codes})
        )
        report = OfficialOracleExperimentReport(
            experiment_id=experiment_id,
            experiment_revision=experiment_revision,
            lane_id=lane_id,
            lane_status=disposition.status,
            execution_scope=disposition.execution_scope,
            execution_mode=execution_mode,
            claim_ceiling=disposition.claim_ceiling,
            retention_policy=disposition.retention_policy,
            source_ledger_id=current_gate.source_ledger.ledger_id,
            source_ledger_fingerprint=current_gate.source_ledger.fingerprint,
            terms_revision=current_gate.terms.terms_revision,
            policy_revision=current_gate.policy.policy_revision,
            cases=cases,
            results=result_tuple,
            status=report_status,
            missing_observation_count=missing_count,
            failed_observation_count=failed_count,
            pair_status_counts=tuple(sorted(pair_counts.items())),
            variability_status=aggregate_variability,
            diagnostic_codes=diagnostics,
            negative_control_violations=sum(
                result.negative_control_violation for result in result_tuple
            ),
        )
        return OfficialOracleExperimentRun(report=report, next_gate=current_gate)

    @staticmethod
    def _validate_inputs(
        cases: tuple[OfficialOracleExperimentCase, ...],
        executor: OfficialOracleExperimentExecutor,
        lane_id: str,
        execution_mode: OfficialOracleExecutionMode,
        experiment_id: str,
        experiment_revision: str,
    ) -> None:
        if not isinstance(cases, tuple) or not cases or len(cases) > MAX_EXPERIMENT_CASES:
            raise ContractValidationError("experiment cases must be a bounded non-empty tuple")
        if not all(isinstance(case, OfficialOracleExperimentCase) for case in cases):
            raise ContractValidationError("experiment cases contain an invalid value")
        if len({case.case_id for case in cases}) != len(cases):
            raise ContractValidationError("experiment case IDs must be unique")
        if not isinstance(executor, OfficialOracleExperimentExecutor):
            raise ContractValidationError("experiment executor must implement execute")
        _require_identifier(lane_id, "experiment lane_id")
        _require_enum(execution_mode, OfficialOracleExecutionMode, "experiment execution_mode")
        _require_identifier(experiment_id, "experiment_id")
        _require_identifier(experiment_revision, "experiment_revision")

    @staticmethod
    def _operation_id(
        experiment_case: OfficialOracleExperimentCase,
        variant: bool,
        replicate_index: int,
    ) -> str:
        digest = experiment_case.fingerprint.removeprefix("sha256:")[:24]
        side = "variant" if variant else "baseline"
        return f"experiment.{digest}.{side}.{replicate_index}"

    @staticmethod
    def _missing_observation(
        experiment_case: OfficialOracleExperimentCase,
        *,
        variant: bool,
        replicate_index: int,
        lane_id: str,
        execution_mode: OfficialOracleExecutionMode,
        disposition: OfficialOracleLaneDisposition,
        request_fingerprint: str,
        diagnostic_code: str,
        source_ledger_fingerprint: str,
        terms_revision: str,
        policy_revision: str,
    ) -> OfficialOracleExperimentObservation:
        return OfficialOracleExperimentObservation(
            case_id=experiment_case.case_id,
            variant=variant,
            replicate_index=replicate_index,
            lane_id=lane_id,
            execution_mode=execution_mode,
            status=OfficialOracleExperimentObservationStatus.MISSING,
            request_fingerprint=request_fingerprint,
            output_fingerprint=None,
            diagnostic_code=diagnostic_code,
            lane_status=disposition.status,
            execution_scope=disposition.execution_scope,
            claim_ceiling=disposition.claim_ceiling,
            retention_policy=disposition.retention_policy,
            source_ledger_fingerprint=source_ledger_fingerprint,
            terms_revision=terms_revision,
            policy_revision=policy_revision,
        )

    @staticmethod
    def _execute_observation(
        experiment_case: OfficialOracleExperimentCase,
        executor: OfficialOracleExperimentExecutor,
        *,
        variant: bool,
        replicate_index: int,
        lane_id: str,
        execution_mode: OfficialOracleExecutionMode,
        disposition: OfficialOracleLaneDisposition,
        request_fingerprint: str,
        source_ledger_fingerprint: str,
        terms_revision: str,
        policy_revision: str,
    ) -> OfficialOracleExperimentObservation:
        try:
            sample = executor.execute(
                experiment_case,
                variant=variant,
                replicate_index=replicate_index,
            )
        except Exception:
            return OfficialOracleExperimentHarness._failed_observation(
                experiment_case,
                variant=variant,
                replicate_index=replicate_index,
                lane_id=lane_id,
                execution_mode=execution_mode,
                disposition=disposition,
                request_fingerprint=request_fingerprint,
                diagnostic_code="executor_failed",
                source_ledger_fingerprint=source_ledger_fingerprint,
                terms_revision=terms_revision,
                policy_revision=policy_revision,
            )
        if not isinstance(sample, OfficialOracleExperimentSample):
            return OfficialOracleExperimentHarness._failed_observation(
                experiment_case,
                variant=variant,
                replicate_index=replicate_index,
                lane_id=lane_id,
                execution_mode=execution_mode,
                disposition=disposition,
                request_fingerprint=request_fingerprint,
                diagnostic_code="executor_contract_invalid",
                source_ledger_fingerprint=source_ledger_fingerprint,
                terms_revision=terms_revision,
                policy_revision=policy_revision,
            )
        if sample.status is OfficialOracleExperimentSampleStatus.FAILED:
            return OfficialOracleExperimentHarness._failed_observation(
                experiment_case,
                variant=variant,
                replicate_index=replicate_index,
                lane_id=lane_id,
                execution_mode=execution_mode,
                disposition=disposition,
                request_fingerprint=request_fingerprint,
                diagnostic_code=sample.diagnostic_code or "executor_failed",
                source_ledger_fingerprint=source_ledger_fingerprint,
                terms_revision=terms_revision,
                policy_revision=policy_revision,
            )
        return OfficialOracleExperimentObservation(
            case_id=experiment_case.case_id,
            variant=variant,
            replicate_index=replicate_index,
            lane_id=lane_id,
            execution_mode=execution_mode,
            status=OfficialOracleExperimentObservationStatus.RECORDED,
            request_fingerprint=request_fingerprint,
            output_fingerprint=sample.output_fingerprint,
            diagnostic_code=None,
            lane_status=disposition.status,
            execution_scope=disposition.execution_scope,
            claim_ceiling=disposition.claim_ceiling,
            retention_policy=disposition.retention_policy,
            source_ledger_fingerprint=source_ledger_fingerprint,
            terms_revision=terms_revision,
            policy_revision=policy_revision,
        )

    @staticmethod
    def _failed_observation(
        experiment_case: OfficialOracleExperimentCase,
        *,
        variant: bool,
        replicate_index: int,
        lane_id: str,
        execution_mode: OfficialOracleExecutionMode,
        disposition: OfficialOracleLaneDisposition,
        request_fingerprint: str,
        diagnostic_code: str,
        source_ledger_fingerprint: str,
        terms_revision: str,
        policy_revision: str,
    ) -> OfficialOracleExperimentObservation:
        return OfficialOracleExperimentObservation(
            case_id=experiment_case.case_id,
            variant=variant,
            replicate_index=replicate_index,
            lane_id=lane_id,
            execution_mode=execution_mode,
            status=OfficialOracleExperimentObservationStatus.FAILED,
            request_fingerprint=request_fingerprint,
            output_fingerprint=None,
            diagnostic_code=diagnostic_code,
            lane_status=disposition.status,
            execution_scope=disposition.execution_scope,
            claim_ceiling=disposition.claim_ceiling,
            retention_policy=disposition.retention_policy,
            source_ledger_fingerprint=source_ledger_fingerprint,
            terms_revision=terms_revision,
            policy_revision=policy_revision,
        )


__all__ = [
    "OFFICIAL_ORACLE_EXPERIMENT_SCHEMA",
    "MAX_EXPERIMENT_CASES",
    "MAX_EXPERIMENT_REPLICATES",
    "OfficialOracleExperimentCase",
    "OfficialOracleExperimentCaseResult",
    "OfficialOracleExperimentExecutor",
    "OfficialOracleExperimentFactor",
    "OfficialOracleExperimentHarness",
    "OfficialOracleExperimentObservation",
    "OfficialOracleExperimentObservationStatus",
    "OfficialOracleExperimentPairStatus",
    "OfficialOracleExperimentReport",
    "OfficialOracleExperimentReportStatus",
    "OfficialOracleExperimentRun",
    "OfficialOracleExperimentSample",
    "OfficialOracleExperimentSampleStatus",
    "OfficialOracleExperimentVariabilityStatus",
]
