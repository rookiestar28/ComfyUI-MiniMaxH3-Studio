"""Provider-free Base/Reference comparative evaluation and regression contracts.

This layer composes redacted route metadata; it never launches a provider, model, ComfyUI host,
media reader, or reviewer. Structural and hard-constraint evidence remain independent from optional
oracle, fixed-H3, measurement, and subjective reviewer evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import isfinite
from types import MappingProxyType

from .base_evaluation import (
    EvaluationMeasurement,
    FixedH3Receipt,
    FixedH3Status,
    OracleComparison,
    OracleComparisonStatus,
)
from .canonical import binary64_token, canonical_fingerprint
from .errors import ContractValidationError

COMPARATIVE_EVALUATION_SCHEMA = "h3.comparative.evaluation.v1"
MAX_COMPARATIVE_CASES = 64
MAX_COMPARATIVE_DIAGNOSTICS = 128
MAX_COMPARATIVE_LIMITATIONS = 128
MAX_COMPARATIVE_FINGERPRINTS = 64
MAX_COMPARATIVE_REVIEW_SCORES = 32

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "bearer ",
    "cookie",
    "file://",
    "http://",
    "https://",
    "password",
    "secret",
    "sig=",
    "token=",
)


class ComparativeProfile(str, Enum):
    """Accepted prompt profiles represented by the comparative corpus."""

    BASE = "base"
    REFERENCE = "reference"


class ComparativeRoute(str, Enum):
    """Explicit route identities; unavailable routes are never silently substituted."""

    MANUAL_DETERMINISTIC = "manual_deterministic"
    LOCAL_ASSISTED = "local_assisted"
    REMOTE_CUSTOM = "remote_custom"
    OFFICIAL_ORACLE = "official_oracle"


class ComparativeEvidenceStatus(str, Enum):
    """Terminal state of one route's evidence record."""

    RECORDED = "recorded"
    NOT_REQUESTED = "not_requested"
    BLOCKED = "blocked"
    FAILED = "failed"
    STALE = "stale"


class ComparativeReportStatus(str, Enum):
    """Aggregate protocol status, independent from subjective quality."""

    PASSED = "passed"
    FAILED = "failed"


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


def _bounded_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{field_name} must be a non-negative integer")
    return value


def _fingerprints(values: object, field_name: str, *, require_one: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_COMPARATIVE_FINGERPRINTS:
        raise ContractValidationError(f"{field_name} must be a bounded tuple")
    if require_one and not values:
        raise ContractValidationError(f"{field_name} must contain at least one fingerprint")
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


def _fingerprintable(value: object) -> object:
    if isinstance(value, float):
        return binary64_token(value)
    if isinstance(value, Mapping):
        return {key: _fingerprintable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fingerprintable(item) for item in value]
    return value


def _ratio_token(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0"
    return format((Decimal(numerator) / Decimal(denominator)).normalize(), "f")


@dataclass(frozen=True, slots=True)
class ComparativeReviewerRubric:
    """Versioned dimensions and scale for optional human review."""

    rubric_id: str
    version: str
    dimensions: tuple[str, ...]
    scale_min: float = 0.0
    scale_max: float = 5.0
    schema: str = COMPARATIVE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.rubric_id, "rubric_id")
        _metadata(self.version, "rubric version", 64)
        if (
            not isinstance(self.dimensions, tuple)
            or not self.dimensions
            or len(self.dimensions) > 16
        ):
            raise ContractValidationError("rubric dimensions must be bounded and non-empty")
        tuple(_identifier(item, "rubric dimension") for item in self.dimensions)
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ContractValidationError("rubric dimensions must be unique")
        minimum = _finite(self.scale_min, "rubric scale_min")
        maximum = _finite(self.scale_max, "rubric scale_max")
        if maximum <= minimum:
            raise ContractValidationError("rubric scale_max must exceed scale_min")
        if self.schema != COMPARATIVE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported comparative rubric schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "rubric_id": self.rubric_id,
            "version": self.version,
            "dimensions": list(self.dimensions),
            "scale_min": self.scale_min,
            "scale_max": self.scale_max,
        }


@dataclass(frozen=True, slots=True)
class ComparativeThresholdPolicy:
    """Versioned non-subjective regression thresholds."""

    policy_id: str
    version: str
    max_latency_seconds: float | None = None
    max_peak_memory_bytes: int | None = None
    max_output_bytes: int | None = None
    min_structural_pass_ratio: float = 1.0
    min_hard_constraint_pass_ratio: float = 1.0
    min_reviewer_score: float | None = None
    schema: str = COMPARATIVE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "policy_id")
        _metadata(self.version, "policy version", 64)
        if self.max_latency_seconds is not None:
            _finite(self.max_latency_seconds, "max_latency_seconds", positive=True)
        for value, field_name in (
            (self.max_peak_memory_bytes, "max_peak_memory_bytes"),
            (self.max_output_bytes, "max_output_bytes"),
        ):
            if value is not None:
                _bounded_int(value, field_name)
        for ratio_value, field_name in (
            (self.min_structural_pass_ratio, "min_structural_pass_ratio"),
            (self.min_hard_constraint_pass_ratio, "min_hard_constraint_pass_ratio"),
        ):
            ratio = _finite(ratio_value, field_name)
            if ratio > 1:
                raise ContractValidationError(f"{field_name} must be between 0 and 1")
        if self.min_reviewer_score is not None:
            _finite(self.min_reviewer_score, "min_reviewer_score")
        if self.schema != COMPARATIVE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported comparative threshold schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "policy_id": self.policy_id,
            "version": self.version,
            "max_latency_seconds": self.max_latency_seconds,
            "max_peak_memory_bytes": self.max_peak_memory_bytes,
            "max_output_bytes": self.max_output_bytes,
            "min_structural_pass_ratio": self.min_structural_pass_ratio,
            "min_hard_constraint_pass_ratio": self.min_hard_constraint_pass_ratio,
            "min_reviewer_score": self.min_reviewer_score,
        }


@dataclass(frozen=True, slots=True)
class ComparativeReviewerScore:
    """One bounded score; dimensions and scale are checked against the corpus rubric."""

    dimension: str
    score: float
    schema: str = COMPARATIVE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.dimension, "reviewer dimension")
        _finite(self.score, "reviewer score")
        if self.schema != COMPARATIVE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported comparative reviewer score schema")

    def to_wire(self) -> dict[str, object]:
        return {"schema": self.schema, "dimension": self.dimension, "score": self.score}


@dataclass(frozen=True, slots=True)
class ComparativeRouteEvidence:
    """Redacted evidence for one explicit comparative route."""

    route: ComparativeRoute
    status: ComparativeEvidenceStatus
    structural_valid: bool | None = None
    hard_constraints_preserved: bool | None = None
    prompt_fingerprint: str | None = None
    measurement: EvaluationMeasurement | None = None
    reviewer_scores: tuple[ComparativeReviewerScore, ...] = ()
    oracle: OracleComparison | None = None
    fixed_h3: FixedH3Receipt | None = None
    diagnostic_codes: tuple[str, ...] = ()
    limitation_codes: tuple[str, ...] = ()
    schema: str = COMPARATIVE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.route, ComparativeRoute):
            raise ContractValidationError("comparative route must be a ComparativeRoute")
        if not isinstance(self.status, ComparativeEvidenceStatus):
            raise ContractValidationError("comparative evidence status is invalid")
        if self.structural_valid is not None and not isinstance(self.structural_valid, bool):
            raise ContractValidationError("structural_valid must be a boolean or None")
        if self.hard_constraints_preserved is not None and not isinstance(
            self.hard_constraints_preserved, bool
        ):
            raise ContractValidationError("hard_constraints_preserved must be a boolean or None")
        if self.prompt_fingerprint is not None:
            _fingerprint(self.prompt_fingerprint, "prompt_fingerprint")
        if self.measurement is not None and not isinstance(self.measurement, EvaluationMeasurement):
            raise ContractValidationError("measurement must be EvaluationMeasurement or None")
        if (
            not isinstance(self.reviewer_scores, tuple)
            or len(self.reviewer_scores) > MAX_COMPARATIVE_REVIEW_SCORES
            or not all(isinstance(item, ComparativeReviewerScore) for item in self.reviewer_scores)
        ):
            raise ContractValidationError("reviewer_scores must be a bounded tuple")
        if len({item.dimension for item in self.reviewer_scores}) != len(self.reviewer_scores):
            raise ContractValidationError("reviewer dimensions must be unique per route")
        if self.oracle is not None and not isinstance(self.oracle, OracleComparison):
            raise ContractValidationError("oracle must be OracleComparison or None")
        if self.fixed_h3 is not None:
            if type(self.fixed_h3) is not FixedH3Receipt:
                raise ContractValidationError("fixed_h3 must be FixedH3Receipt or None")
            self.fixed_h3.require_admitted()
        _codes(self.diagnostic_codes, "diagnostic_codes", MAX_COMPARATIVE_DIAGNOSTICS)
        _codes(self.limitation_codes, "limitation_codes", MAX_COMPARATIVE_LIMITATIONS)
        if self.status is ComparativeEvidenceStatus.RECORDED:
            if not isinstance(self.structural_valid, bool) or not isinstance(
                self.hard_constraints_preserved, bool
            ):
                raise ContractValidationError("recorded evidence requires structural flags")
            if self.prompt_fingerprint is None or self.measurement is None:
                raise ContractValidationError(
                    "recorded evidence requires fingerprint and measurement"
                )
            if self.diagnostic_codes:
                raise ContractValidationError("recorded evidence cannot carry failure diagnostics")
        elif self.status is ComparativeEvidenceStatus.FAILED:
            if not self.diagnostic_codes:
                raise ContractValidationError("failed evidence requires diagnostics")
        elif self.status is ComparativeEvidenceStatus.BLOCKED:
            if not self.diagnostic_codes:
                raise ContractValidationError("blocked evidence requires diagnostics")
            self._require_empty_payload("blocked")
        elif self.status is ComparativeEvidenceStatus.NOT_REQUESTED:
            if not self.limitation_codes:
                raise ContractValidationError("not-requested evidence requires limitations")
            self._require_empty_payload("not-requested")
        elif self.status is ComparativeEvidenceStatus.STALE and not self.diagnostic_codes:
            raise ContractValidationError("stale evidence requires diagnostics")
        if (
            self.route is ComparativeRoute.OFFICIAL_ORACLE
            and self.status is ComparativeEvidenceStatus.RECORDED
        ):
            if self.oracle is None or self.oracle.status not in {
                OracleComparisonStatus.MATCHED,
                OracleComparisonStatus.DIFFERENT,
            }:
                raise ContractValidationError(
                    "recorded official oracle evidence requires comparison"
                )
        elif self.route is not ComparativeRoute.OFFICIAL_ORACLE and self.oracle is not None:
            raise ContractValidationError("oracle comparison belongs only to official_oracle route")
        if self.schema != COMPARATIVE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported comparative route evidence schema")

    def _require_empty_payload(self, label: str) -> None:
        if (
            any(
                value is not None
                for value in (
                    self.structural_valid,
                    self.hard_constraints_preserved,
                    self.prompt_fingerprint,
                )
            )
            or self.measurement is not None
            or self.reviewer_scores
            or self.oracle is not None
            or self.fixed_h3 is not None
        ):
            raise ContractValidationError(f"{label} evidence cannot carry recorded payload")

    def to_wire(self) -> dict[str, object]:
        fixed_h3_wire: dict[str, object] | None = None
        if self.fixed_h3 is not None:
            if type(self.fixed_h3) is not FixedH3Receipt:
                raise ContractValidationError("fixed_h3 must be FixedH3Receipt or None")
            self.fixed_h3.require_admitted()
            fixed_h3_wire = self.fixed_h3.to_wire()
        return {
            "schema": self.schema,
            "route": self.route.value,
            "status": self.status.value,
            "structural_valid": self.structural_valid,
            "hard_constraints_preserved": self.hard_constraints_preserved,
            "prompt_fingerprint": self.prompt_fingerprint,
            "measurement": None if self.measurement is None else self.measurement.to_wire(),
            "reviewer_scores": [item.to_wire() for item in self.reviewer_scores],
            "oracle": None if self.oracle is None else self.oracle.to_wire(),
            "fixed_h3": fixed_h3_wire,
            "diagnostic_codes": list(self.diagnostic_codes),
            "limitation_codes": list(self.limitation_codes),
        }


_REQUIRED_ROUTES = tuple(ComparativeRoute)


@dataclass(frozen=True, slots=True)
class ComparativeEvaluationCase:
    """One Base or Reference case with all four route identities represented."""

    case_id: str
    profile: ComparativeProfile
    source_fingerprints: tuple[str, ...]
    routes: tuple[ComparativeRouteEvidence, ...]
    schema: str = COMPARATIVE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.case_id, "comparative case_id")
        if not isinstance(self.profile, ComparativeProfile):
            raise ContractValidationError("comparative profile is invalid")
        _fingerprints(self.source_fingerprints, "source_fingerprints", require_one=True)
        if (
            not isinstance(self.routes, tuple)
            or len(self.routes) != len(_REQUIRED_ROUTES)
            or not all(isinstance(item, ComparativeRouteEvidence) for item in self.routes)
        ):
            raise ContractValidationError("comparative case must contain all four route records")
        route_ids = tuple(item.route for item in self.routes)
        if set(route_ids) != set(_REQUIRED_ROUTES):
            raise ContractValidationError(
                "comparative case route identities are incomplete or duplicated"
            )
        if self.schema != COMPARATIVE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported comparative case schema")

    @property
    def route_map(self) -> Mapping[ComparativeRoute, ComparativeRouteEvidence]:
        return MappingProxyType({item.route: item for item in self.routes})

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "profile": self.profile.value,
            "source_fingerprints": list(self.source_fingerprints),
            "routes": [item.to_wire() for item in self.routes],
        }


@dataclass(frozen=True, slots=True)
class ComparativeEvaluationCorpus:
    """Versioned ordered Base/Reference corpus and threshold/rubric policy."""

    corpus_id: str
    cases: tuple[ComparativeEvaluationCase, ...]
    thresholds: ComparativeThresholdPolicy
    reviewer_rubric: ComparativeReviewerRubric
    schema: str = COMPARATIVE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.corpus_id, "comparative corpus_id")
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_COMPARATIVE_CASES
            or not all(isinstance(item, ComparativeEvaluationCase) for item in self.cases)
        ):
            raise ContractValidationError("comparative corpus cases must be bounded and non-empty")
        ids = tuple(item.case_id for item in self.cases)
        if len(ids) != len(set(ids)):
            raise ContractValidationError("comparative case IDs must be unique")
        if not isinstance(self.thresholds, ComparativeThresholdPolicy):
            raise ContractValidationError("comparative thresholds must be typed")
        if not isinstance(self.reviewer_rubric, ComparativeReviewerRubric):
            raise ContractValidationError("comparative reviewer rubric must be typed")
        if self.schema != COMPARATIVE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported comparative corpus schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "corpus_id": self.corpus_id,
            "thresholds": self.thresholds.to_wire(),
            "reviewer_rubric": self.reviewer_rubric.to_wire(),
            "cases": [item.to_wire() for item in self.cases],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(_fingerprintable(self.to_wire()))


@dataclass(frozen=True, slots=True)
class ComparativeEvaluationResult:
    """Case result with independently visible ratios, route states, and limitations."""

    result_id: str
    case_id: str
    profile: ComparativeProfile
    status: ComparativeReportStatus
    route_statuses: Mapping[str, ComparativeEvidenceStatus]
    route_prompt_fingerprint_match: Mapping[str, bool | None]
    structural_pass_ratio: str
    hard_constraint_pass_ratio: str
    reviewer_mean: float | None
    diagnostic_codes: tuple[str, ...]
    limitations: tuple[str, ...]
    fixed_h3_settings_fingerprint: str | None
    schema: str = COMPARATIVE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.result_id, "comparative result_id")
        _identifier(self.case_id, "comparative case_id")
        if not isinstance(self.profile, ComparativeProfile):
            raise ContractValidationError("comparative result profile is invalid")
        if not isinstance(self.status, ComparativeReportStatus):
            raise ContractValidationError("comparative result status is invalid")
        if set(self.route_statuses) != {route.value for route in _REQUIRED_ROUTES}:
            raise ContractValidationError("comparative result route statuses are incomplete")
        if not all(
            isinstance(value, ComparativeEvidenceStatus) for value in self.route_statuses.values()
        ):
            raise ContractValidationError("comparative result route status is invalid")
        if set(self.route_prompt_fingerprint_match) != {route.value for route in _REQUIRED_ROUTES}:
            raise ContractValidationError("comparative prompt comparison is incomplete")
        if not all(
            value is None or isinstance(value, bool)
            for value in self.route_prompt_fingerprint_match.values()
        ):
            raise ContractValidationError("comparative prompt comparison values are invalid")
        for ratio, field_name in (
            (self.structural_pass_ratio, "structural_pass_ratio"),
            (self.hard_constraint_pass_ratio, "hard_constraint_pass_ratio"),
        ):
            try:
                parsed = Decimal(ratio)
            except (InvalidOperation, ValueError) as exc:
                raise ContractValidationError(f"{field_name} must be between 0 and 1") from exc
            if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
                raise ContractValidationError(f"{field_name} must be between 0 and 1")
        if self.reviewer_mean is not None:
            _finite(self.reviewer_mean, "reviewer_mean")
        _codes(self.diagnostic_codes, "diagnostic_codes", MAX_COMPARATIVE_DIAGNOSTICS)
        _codes(self.limitations, "limitations", MAX_COMPARATIVE_LIMITATIONS)
        if self.fixed_h3_settings_fingerprint is not None:
            _fingerprint(self.fixed_h3_settings_fingerprint, "fixed_h3_settings_fingerprint")
        if self.status is ComparativeReportStatus.FAILED and not self.diagnostic_codes:
            raise ContractValidationError("failed comparative result requires diagnostics")
        if self.schema != COMPARATIVE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported comparative result schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "result_id": self.result_id,
            "case_id": self.case_id,
            "profile": self.profile.value,
            "status": self.status.value,
            "route_statuses": {key: value.value for key, value in self.route_statuses.items()},
            "route_prompt_fingerprint_match": dict(self.route_prompt_fingerprint_match),
            "structural_pass_ratio": self.structural_pass_ratio,
            "hard_constraint_pass_ratio": self.hard_constraint_pass_ratio,
            "reviewer_mean": self.reviewer_mean,
            "diagnostic_codes": list(self.diagnostic_codes),
            "limitations": list(self.limitations),
            "fixed_h3_settings_fingerprint": self.fixed_h3_settings_fingerprint,
        }


def _validate_reviewer_scores(
    case: ComparativeEvaluationCase,
    rubric: ComparativeReviewerRubric,
    diagnostics: list[str],
) -> list[float]:
    scores: list[float] = []
    allowed = set(rubric.dimensions)
    for evidence in case.routes:
        for item in evidence.reviewer_scores:
            if item.dimension not in allowed:
                diagnostics.append("reviewer.unknown_dimension")
            if not rubric.scale_min <= item.score <= rubric.scale_max:
                diagnostics.append("reviewer.score_out_of_range")
            scores.append(item.score)
    return scores


def evaluate_comparative_case(
    case: ComparativeEvaluationCase,
    *,
    thresholds: ComparativeThresholdPolicy,
    reviewer_rubric: ComparativeReviewerRubric,
) -> ComparativeEvaluationResult:
    """Evaluate one case without invoking any optional route."""

    if not isinstance(case, ComparativeEvaluationCase):
        raise ContractValidationError("case must be a ComparativeEvaluationCase")
    diagnostics: list[str] = []
    limitations: list[str] = []
    route_map = case.route_map
    manual = route_map[ComparativeRoute.MANUAL_DETERMINISTIC]
    if manual.status is not ComparativeEvidenceStatus.RECORDED:
        diagnostics.append("manual_deterministic.unavailable")

    for evidence in case.routes:
        if evidence.status in {
            ComparativeEvidenceStatus.NOT_REQUESTED,
            ComparativeEvidenceStatus.BLOCKED,
            ComparativeEvidenceStatus.STALE,
        }:
            limitations.extend(
                f"route.{evidence.route.value}.{evidence.status.value}.{code}"
                for code in (evidence.limitation_codes or evidence.diagnostic_codes)
            )
        if evidence.status is ComparativeEvidenceStatus.FAILED:
            diagnostics.extend(
                f"route.{evidence.route.value}.{code}" for code in evidence.diagnostic_codes
            )

    recorded = tuple(
        evidence
        for evidence in case.routes
        if evidence.status in {ComparativeEvidenceStatus.RECORDED, ComparativeEvidenceStatus.FAILED}
    )
    manual_fingerprint = (
        manual.prompt_fingerprint if manual.status is ComparativeEvidenceStatus.RECORDED else None
    )
    fingerprint_matches: dict[str, bool | None] = {}
    for evidence in case.routes:
        if manual_fingerprint is None or evidence.prompt_fingerprint is None:
            fingerprint_matches[evidence.route.value] = None
        else:
            matches = evidence.prompt_fingerprint == manual_fingerprint
            fingerprint_matches[evidence.route.value] = matches
            if evidence.route is not ComparativeRoute.MANUAL_DETERMINISTIC and not matches:
                limitations.append(f"route.{evidence.route.value}.prompt_fingerprint_differs")
    structural_values = tuple(
        evidence.structural_valid for evidence in recorded if evidence.structural_valid is not None
    )
    hard_constraint_values = tuple(
        evidence.hard_constraints_preserved
        for evidence in recorded
        if evidence.hard_constraints_preserved is not None
    )
    if not structural_values:
        diagnostics.append("structural.evidence_missing")
    if not hard_constraint_values:
        diagnostics.append("hard_constraint.evidence_missing")
    structural_ratio = (
        _ratio_token(sum(value is True for value in structural_values), len(structural_values))
        if structural_values
        else "0"
    )
    hard_constraint_ratio = (
        _ratio_token(
            sum(value is True for value in hard_constraint_values), len(hard_constraint_values)
        )
        if hard_constraint_values
        else "0"
    )
    if Decimal(structural_ratio) < Decimal(str(thresholds.min_structural_pass_ratio)):
        diagnostics.append("threshold.structural_pass_ratio")
    if Decimal(hard_constraint_ratio) < Decimal(str(thresholds.min_hard_constraint_pass_ratio)):
        diagnostics.append("threshold.hard_constraint_pass_ratio")

    for evidence in recorded:
        measurement = evidence.measurement
        if measurement is None:
            diagnostics.append(f"measurement.{evidence.route.value}.missing")
            continue
        if (
            thresholds.max_latency_seconds is not None
            and measurement.latency_seconds > thresholds.max_latency_seconds
        ):
            diagnostics.append(f"threshold.{evidence.route.value}.latency")
        if thresholds.max_peak_memory_bytes is not None and (
            measurement.peak_memory_bytes is None
            or measurement.peak_memory_bytes > thresholds.max_peak_memory_bytes
        ):
            diagnostics.append(f"threshold.{evidence.route.value}.peak_memory")
        if thresholds.max_output_bytes is not None and (
            measurement.output_bytes is None
            or measurement.output_bytes > thresholds.max_output_bytes
        ):
            diagnostics.append(f"threshold.{evidence.route.value}.output_bytes")

    scores = _validate_reviewer_scores(case, reviewer_rubric, diagnostics)
    reviewer_mean = sum(scores) / len(scores) if scores else None
    if thresholds.min_reviewer_score is not None:
        if reviewer_mean is None:
            diagnostics.append("threshold.reviewer_score_missing")
        elif reviewer_mean < thresholds.min_reviewer_score:
            diagnostics.append("threshold.reviewer_score")

    # CRITICAL: route objects are externally held values; recheck nested runtime authority at use.
    for evidence in recorded:
        if evidence.fixed_h3 is not None:
            if type(evidence.fixed_h3) is not FixedH3Receipt:
                raise ContractValidationError("fixed_h3 must be FixedH3Receipt or None")
            evidence.fixed_h3.require_admitted()
    fixed_settings: tuple[str, ...] = tuple(
        canonical_fingerprint(_fingerprintable(evidence.fixed_h3.settings.to_wire()))
        for evidence in recorded
        if evidence.fixed_h3 is not None and evidence.fixed_h3.status is FixedH3Status.PASSED
    )
    fixed_h3_settings_fingerprint = fixed_settings[0] if fixed_settings else None
    if len(set(fixed_settings)) > 1:
        diagnostics.append("fixed_h3.settings_drift")
    for evidence in recorded:
        if evidence.fixed_h3 is not None and evidence.fixed_h3.status is not FixedH3Status.PASSED:
            limitations.append(f"fixed_h3.{evidence.route.value}.{evidence.fixed_h3.status.value}")

    official = route_map[ComparativeRoute.OFFICIAL_ORACLE]
    if official.oracle is not None and official.oracle.status in {
        OracleComparisonStatus.MATCHED,
        OracleComparisonStatus.DIFFERENT,
    }:
        limitations.append("official_oracle_does_not_prove_equivalence")
    if official.status is not ComparativeEvidenceStatus.RECORDED:
        limitations.append("official_oracle_evidence_unavailable")

    diagnostics_tuple = tuple(dict.fromkeys(diagnostics))
    limitations_tuple = tuple(dict.fromkeys(limitations))
    status = ComparativeReportStatus.FAILED if diagnostics_tuple else ComparativeReportStatus.PASSED
    route_statuses = MappingProxyType(
        {evidence.route.value: evidence.status for evidence in case.routes}
    )
    result_material = {
        "schema": COMPARATIVE_EVALUATION_SCHEMA,
        "case_id": case.case_id,
        "profile": case.profile.value,
        "status": status.value,
        "route_statuses": {key: value.value for key, value in route_statuses.items()},
        "route_prompt_fingerprint_match": fingerprint_matches,
        "structural_pass_ratio": structural_ratio,
        "hard_constraint_pass_ratio": hard_constraint_ratio,
        "reviewer_mean": reviewer_mean,
        "diagnostic_codes": list(diagnostics_tuple),
        "limitations": list(limitations_tuple),
        "fixed_h3_settings_fingerprint": fixed_h3_settings_fingerprint,
    }
    result_id = (
        "result." + canonical_fingerprint(_fingerprintable(result_material)).split(":", 1)[1][:32]
    )
    return ComparativeEvaluationResult(
        result_id,
        case.case_id,
        case.profile,
        status,
        route_statuses,
        MappingProxyType(dict(fingerprint_matches)),
        structural_ratio,
        hard_constraint_ratio,
        reviewer_mean,
        diagnostics_tuple,
        limitations_tuple,
        fixed_h3_settings_fingerprint,
    )


@dataclass(frozen=True, slots=True)
class ComparativeEvaluationReport:
    """Aggregate report; optional route absence remains visible in route_summary."""

    report_id: str
    corpus_id: str
    corpus_fingerprint: str
    status: ComparativeReportStatus
    results: tuple[ComparativeEvaluationResult, ...]
    route_summary: Mapping[str, Mapping[str, int]]
    limitations: tuple[str, ...]
    schema: str = COMPARATIVE_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.report_id, "comparative report_id")
        _identifier(self.corpus_id, "comparative corpus_id")
        _fingerprint(self.corpus_fingerprint, "corpus_fingerprint")
        if not isinstance(self.status, ComparativeReportStatus):
            raise ContractValidationError("comparative report status is invalid")
        if not isinstance(self.results, tuple) or not self.results:
            raise ContractValidationError("comparative report results must be non-empty")
        if not all(isinstance(item, ComparativeEvaluationResult) for item in self.results):
            raise ContractValidationError("comparative report results must be typed")
        if len({item.result_id for item in self.results}) != len(self.results):
            raise ContractValidationError("comparative result IDs must be unique")
        expected_routes = {route.value for route in _REQUIRED_ROUTES}
        if set(self.route_summary) != expected_routes:
            raise ContractValidationError("comparative route summary is incomplete")
        expected_fields = {"case_count", *(status.value for status in ComparativeEvidenceStatus)}
        for route, summary in self.route_summary.items():
            if not isinstance(summary, Mapping) or set(summary) != expected_fields:
                raise ContractValidationError(f"comparative route summary {route!r} is invalid")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in summary.values()
            ):
                raise ContractValidationError("comparative route summary counts are invalid")
            if summary["case_count"] != sum(
                summary[status.value] for status in ComparativeEvidenceStatus
            ):
                raise ContractValidationError("comparative route summary count does not match")
        _codes(self.limitations, "limitations", MAX_COMPARATIVE_LIMITATIONS)
        if self.status is ComparativeReportStatus.PASSED and not self.is_gate_passed:
            raise ContractValidationError("passed comparative report contains a failed result")
        if self.status is ComparativeReportStatus.FAILED and not any(
            item.diagnostic_codes for item in self.results
        ):
            raise ContractValidationError("failed comparative report requires diagnostics")
        if self.schema != COMPARATIVE_EVALUATION_SCHEMA:
            raise ContractValidationError("unsupported comparative report schema")

    @property
    def is_gate_passed(self) -> bool:
        return all(item.status is ComparativeReportStatus.PASSED for item in self.results)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "report_id": self.report_id,
            "corpus_id": self.corpus_id,
            "corpus_fingerprint": self.corpus_fingerprint,
            "status": self.status.value,
            "gate_passed": self.is_gate_passed,
            "results": [item.to_wire() for item in self.results],
            "route_summary": {
                route: dict(summary) for route, summary in self.route_summary.items()
            },
            "limitations": list(self.limitations),
        }


def evaluate_comparative_corpus(corpus: ComparativeEvaluationCorpus) -> ComparativeEvaluationReport:
    """Evaluate a provider-free corpus and preserve every optional lane state."""

    if not isinstance(corpus, ComparativeEvaluationCorpus):
        raise ContractValidationError("corpus must be a ComparativeEvaluationCorpus")
    results = tuple(
        evaluate_comparative_case(
            case,
            thresholds=corpus.thresholds,
            reviewer_rubric=corpus.reviewer_rubric,
        )
        for case in corpus.cases
    )
    route_summary: dict[str, dict[str, int]] = {}
    for route in _REQUIRED_ROUTES:
        counts = {"case_count": 0, **{status.value: 0 for status in ComparativeEvidenceStatus}}
        for case in corpus.cases:
            state = case.route_map[route].status.value
            counts["case_count"] += 1
            counts[state] += 1
        route_summary[route.value] = counts
    limitations = tuple(
        dict.fromkeys(limitation for result in results for limitation in result.limitations)
    )
    status = (
        ComparativeReportStatus.PASSED
        if all(item.status is ComparativeReportStatus.PASSED for item in results)
        else ComparativeReportStatus.FAILED
    )
    report_material = {
        "schema": COMPARATIVE_EVALUATION_SCHEMA,
        "corpus_id": corpus.corpus_id,
        "corpus_fingerprint": corpus.fingerprint,
        "status": status.value,
        "results": [item.to_wire() for item in results],
        "route_summary": route_summary,
        "limitations": list(limitations),
    }
    report_id = (
        "report." + canonical_fingerprint(_fingerprintable(report_material)).split(":", 1)[1][:32]
    )
    return ComparativeEvaluationReport(
        report_id,
        corpus.corpus_id,
        corpus.fingerprint,
        status,
        results,
        MappingProxyType(
            {route: MappingProxyType(values) for route, values in route_summary.items()}
        ),
        limitations,
    )


__all__ = [
    "COMPARATIVE_EVALUATION_SCHEMA",
    "MAX_COMPARATIVE_CASES",
    "MAX_COMPARATIVE_DIAGNOSTICS",
    "MAX_COMPARATIVE_FINGERPRINTS",
    "MAX_COMPARATIVE_LIMITATIONS",
    "MAX_COMPARATIVE_REVIEW_SCORES",
    "ComparativeEvidenceStatus",
    "ComparativeEvaluationCase",
    "ComparativeEvaluationCorpus",
    "ComparativeEvaluationReport",
    "ComparativeEvaluationResult",
    "ComparativeProfile",
    "ComparativeReportStatus",
    "ComparativeReviewerRubric",
    "ComparativeReviewerScore",
    "ComparativeRoute",
    "ComparativeRouteEvidence",
    "ComparativeThresholdPolicy",
    "evaluate_comparative_case",
    "evaluate_comparative_corpus",
]
