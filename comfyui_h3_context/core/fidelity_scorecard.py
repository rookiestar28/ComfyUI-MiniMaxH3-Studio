"""Frozen fidelity scorecard with fail-closed non-equivalence claims.

The scorecard aggregates accepted deterministic and local evidence while preserving unavailable
oracle, perception, fixed-H3, human, and resource lanes as missing. It deliberately contains no
provider, model, media, GPU, human-study, training, subprocess, network, or filesystem executor.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Protocol, TypeVar, cast

from . import fixed_h3_generation as _fixed_h3_generation
from . import human_review as _human_review
from . import local_qualification as _local_qualification
from . import perturbation_evaluation as _perturbation_evaluation
from . import semantic_graph_comparator as _semantic_graph_comparator
from . import training_authorization as _training_authorization
from .canonical import canonical_fingerprint

FIDELITY_SCORECARD_SCHEMA = "h3.fidelity_scorecard.v1"
FIDELITY_SCORECARD_VERSION = "1.0.0"
FIDELITY_SCORECARD_RECORD_DATE = "2026-08-09"
MAX_FIDELITY_SCORECARD_WIRE_BYTES = 32_768
MAX_FIDELITY_SCORECARD_DEPTH = 8
MAX_FIDELITY_SCORECARD_CONTAINER_ITEMS = 128
MAX_FIDELITY_SCORECARD_STRING_LENGTH = 256
FROZEN_FIDELITY_SCORECARD_FINGERPRINT = (
    "sha256:613dbb6e9459508d04241c1f6359fea24bf4264a74da41193405035d5b995e80"
)

_LOCAL_QUALIFICATION_FINGERPRINT = (
    "sha256:693ae2289aa4b184dbf9300abd110f1ba82ffee7689446a90b5c33becff274c4"
)
_OFFICIAL_TERMINAL_FINGERPRINT = (
    "sha256:4a21488a2d7f607e4a25ff6dcaac829fd347d4e25c9a619f57aeb0c443674122"
)
_PERTURBATION_CORPUS_FINGERPRINT = (
    "sha256:96cfebbae82b1decab9cfa352f5ddd6151596576b9a094d2890c782a5c998c25"
)
_SEMANTIC_EVALUATION_FINGERPRINT = (
    "sha256:a86a67781151dc7fb46a8739964a4efe4d6cc3ca11173377826bbd3b4056cf7d"
)
_FIXED_H3_TERMINAL_FINGERPRINT = (
    "sha256:9a11ebfd5e2df401e6e892fa9ef3fc6fecc53bc0a28a4c4e84d003a7de4f52b0"
)
_HUMAN_REVIEW_TERMINAL_FINGERPRINT = (
    "sha256:d1a13b37fc07fb17c1af363256134f2fd182554f3c1b297025dcca6c63dadf16"
)
_TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT = (
    "sha256:55a1993f024b4fb3942acb46eb3908c4cadbcabc521477503b55bd02a257cace"
)
_PERTURBATION_DIMENSION_INVENTORY = (
    ("WORDING", "wording"),
    ("UNICODE", "unicode"),
    ("METADATA", "metadata"),
    ("ASSET_ORDER", "asset_order"),
    ("ASSET_ROLE", "asset_role"),
    ("MODALITY_DUPLICATE", "modality_duplicate"),
    ("MODALITY_REMOVAL", "modality_removal"),
    ("MODALITY_REPLACEMENT", "modality_replacement"),
    ("TEMPORAL_SHIFT", "temporal_shift"),
    ("TEMPORAL_REVERSE", "temporal_reverse"),
    ("TEMPORAL_SPLICE", "temporal_splice"),
    ("SUBJECT_FUSION", "subject_fusion"),
    ("DIRECTIVE_COPY", "directive_copy"),
    ("DIRECTIVE_RETAIN", "directive_retain"),
    ("DIRECTIVE_ADAPT", "directive_adapt"),
    ("DIRECTIVE_EXCLUDE", "directive_exclude"),
    ("AMBIGUITY", "ambiguity"),
    ("CONFLICT", "conflict"),
    ("DURATION", "duration"),
    ("DIALOGUE", "dialogue"),
    ("VISIBLE_TEXT", "visible_text"),
    ("STYLE", "style"),
    ("MOTION", "motion"),
    ("CAMERA", "camera"),
    ("VIDEO_AUDIO", "video_audio"),
    ("DISTRACTOR", "distractor"),
    ("UNSUPPORTED_INPUT", "unsupported_input"),
)

_FORBIDDEN_VALUE_MARKERS = (
    "http://",
    "https://",
    "file:",
    ".planning",
    "reference/",
    "token=",
    "password=",
    "authorization:",
    "bearer ",
    "/mnt/",
    "\\",
    ":\\",
    "@",
)
_SENSITIVE_KEYS = frozenset(
    {"token", "password", "secret", "credential", "cookie", "api_key", "private_key"}
)


class FidelityScorecardError(ValueError):
    """Raised when scorecard evidence is malformed, imputed, or authority-drifted."""


class FidelityLane(str, Enum):
    STRUCTURAL = "structural"
    PERCEPTION = "perception"
    PLANNER = "planner"
    ORACLE = "oracle"
    FIXED_H3 = "fixed_h3"
    HUMAN = "human"
    RESOURCE = "resource"
    SECURITY = "security"


class EvidenceDisposition(str, Enum):
    QUALIFIED = "QUALIFIED"
    LOCAL_ONLY = "LOCAL_ONLY"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    UNAVAILABLE_PROHIBITED = "UNAVAILABLE_PROHIBITED"
    EXHAUSTED = "EXHAUSTED"
    DRIFT_SUSPENDED = "DRIFT_SUSPENDED"


class ClaimLevel(str, Enum):
    DETERMINISTIC_CONFORMANCE = "deterministic_format_contract_conformance"
    SEMANTIC_RECONSTRUCTION = "semantic_reconstruction"
    OFFICIAL_BEHAVIORAL_AGREEMENT = "official_boundary_behavioral_agreement"
    FIXED_H3_NONINFERIORITY_EQUIVALENCE = "fixed_h3_noninferiority_equivalence"
    PRIVATE_INTERNAL_RECOVERY = "private_internal_recovery"


class ClaimDisposition(str, Enum):
    SUPPORTED = "SUPPORTED"
    SUPPORTED_LOCAL_ONLY = "SUPPORTED_LOCAL_ONLY"
    UNVERIFIABLE = "UNVERIFIABLE"


def _exact(value: object, expected: object, field_name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise FidelityScorecardError(f"{field_name} is not the admitted scorecard value")


def _exact_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if type(value) is not enum_type:
        raise FidelityScorecardError(f"{field_name} must be an exact enum member")


def _exact_tuple(value: object, expected: tuple[str, ...], field_name: str) -> None:
    if type(value) is not tuple or len(value) != len(expected):
        raise FidelityScorecardError(f"{field_name} is not the admitted inventory")
    for actual, admitted in zip(value, expected, strict=True):
        if type(actual) is not str or actual != admitted:
            raise FidelityScorecardError(f"{field_name} is not the admitted inventory")


_PREDECESSOR_VALUES: dict[str, tuple[str, str, str, str]] = {
    "official_oracle_terminal": (
        "h3-context-official-oracle-terminal-disposition/1",
        _OFFICIAL_TERMINAL_FINGERPRINT,
        "UNAVAILABLE_PROHIBITED",
        "STRUCTURAL_AND_LOCAL_ONLY",
    ),
    "perturbation_corpus": (
        "h3.context.perturbation.evaluation.v1",
        _PERTURBATION_CORPUS_FINGERPRINT,
        "LOCAL_ONLY",
        "NO_OFFICIAL_BEHAVIOR_EVIDENCE",
    ),
    "semantic_evaluation": (
        "h3.semantic_graph_comparator.evaluation.v1",
        _SEMANTIC_EVALUATION_FINGERPRINT,
        "LOCAL_QUALIFIED_OFFICIAL_UNAVAILABLE",
        "NO_OFFICIAL_AGREEMENT_CLAIM",
    ),
    "local_qualification": (
        "h3.local.qualification.v1",
        _LOCAL_QUALIFICATION_FINGERPRINT,
        "MANUAL_ONLY_SCOPED",
        "NO_ASSISTED_PROFILE_CLAIM",
    ),
    "fixed_h3_terminal": (
        "h3.fixed_generation.terminal.v1",
        _FIXED_H3_TERMINAL_FINGERPRINT,
        "UNAVAILABLE",
        "NO_FIXED_H3_GENERATION_EVIDENCE",
    ),
    "human_review_terminal": (
        "h3.human_review.terminal.v1",
        _HUMAN_REVIEW_TERMINAL_FINGERPRINT,
        "HUMAN_REVIEW_UNAVAILABLE",
        "NO_HUMAN_QUALITY_CLAIM",
    ),
    "training_authorization_terminal": (
        "h3.training_authorization.terminal.v1",
        _TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT,
        "DO_NOT_IMPLEMENT",
        "NO_TRAINING_AUTHORIZATION_OR_EVIDENCE",
    ),
}


@dataclass(frozen=True, slots=True)
class PredecessorReceipt:
    authority: str
    schema: str
    fingerprint: str
    disposition: str
    claim_cap: str

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> PredecessorReceipt:
        if type(self) is not PredecessorReceipt or type(self.authority) is not str:
            raise FidelityScorecardError("predecessor receipt is not exact")
        expected = _PREDECESSOR_VALUES.get(self.authority)
        if expected is None:
            raise FidelityScorecardError("predecessor authority is unknown")
        for actual, admitted, field_name in zip(
            (self.schema, self.fingerprint, self.disposition, self.claim_cap),
            expected,
            ("schema", "fingerprint", "disposition", "claim cap"),
            strict=True,
        ):
            _exact(actual, admitted, f"predecessor {field_name}")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "authority": self.authority,
            "schema": self.schema,
            "fingerprint": self.fingerprint,
            "disposition": self.disposition,
            "claim_cap": self.claim_cap,
        }


_MODE_VALUES: dict[str, tuple[int, int, int, int, int, int, int, int]] = {
    "t2va": (41, 11, 1, 0, 40, 0, 0, 41),
    "i2va": (1, 1, 1, 0, 0, 0, 0, 1),
    "fl2va": (1, 1, 1, 0, 0, 0, 0, 1),
    "ref2va": (27, 9, 3, 24, 0, 0, 0, 27),
}


@dataclass(frozen=True, slots=True)
class ModeReceipt:
    mode: str
    case_count: int
    source_cluster_count: int
    guide_case_count: int
    p0_case_count: int
    non_p0_case_count: int
    failure_count: int
    official_result_count: int
    missing_official_result_count: int

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> ModeReceipt:
        if type(self) is not ModeReceipt or type(self.mode) is not str:
            raise FidelityScorecardError("mode receipt is not exact")
        expected = _MODE_VALUES.get(self.mode)
        actual = (
            self.case_count,
            self.source_cluster_count,
            self.guide_case_count,
            self.p0_case_count,
            self.non_p0_case_count,
            self.failure_count,
            self.official_result_count,
            self.missing_official_result_count,
        )
        if (
            expected is None
            or any(type(value) is not int for value in actual)
            or actual != expected
        ):
            raise FidelityScorecardError("mode receipt differs from the frozen design")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "mode": self.mode,
            "case_count": str(self.case_count),
            "source_cluster_count": str(self.source_cluster_count),
            "guide_case_count": str(self.guide_case_count),
            "p0_case_count": str(self.p0_case_count),
            "non_p0_case_count": str(self.non_p0_case_count),
            "failure_count": str(self.failure_count),
            "official_result_count": str(self.official_result_count),
            "missing_official_result_count": str(self.missing_official_result_count),
        }


_METRIC_VALUES: dict[str, tuple[int, int, str, tuple[str, str] | None]] = {
    "parse_schema": (70, 70, "1.00000000", None),
    "hard_constraint_detection": (6, 6, "1.00000000", None),
    "reference_role_order_ownership_detection": (12, 12, "1.00000000", None),
    "guide_round_trip": (6, 6, "1.00000000", None),
    "p0_mutation_detection": (24, 24, "1.00000000", None),
    "semantic_precision": (20, 20, "1.00000000", ("0.83887484", "1.00000000")),
    "semantic_recall": (20, 20, "1.00000000", ("0.83887484", "1.00000000")),
    "portable_privacy": (1, 1, "1.00000000", None),
}


@dataclass(frozen=True, slots=True)
class MetricReceipt:
    metric: str
    passed_count: int
    total_count: int
    point_estimate: str
    confidence_interval: tuple[str, str] | None

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> MetricReceipt:
        if type(self) is not MetricReceipt or type(self.metric) is not str:
            raise FidelityScorecardError("metric receipt is not exact")
        expected = _METRIC_VALUES.get(self.metric)
        if expected is None:
            raise FidelityScorecardError("metric receipt differs from the admitted result")
        for actual, admitted, field_name in (
            (self.passed_count, expected[0], "metric passed count"),
            (self.total_count, expected[1], "metric total count"),
            (self.point_estimate, expected[2], "metric point estimate"),
        ):
            _exact(actual, admitted, field_name)
        if expected[3] is None:
            _exact(self.confidence_interval, None, "metric confidence interval")
        else:
            _exact_tuple(
                self.confidence_interval,
                expected[3],
                "metric confidence interval",
            )
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "metric": self.metric,
            "passed_count": str(self.passed_count),
            "total_count": str(self.total_count),
            "point_estimate": self.point_estimate,
            "confidence_interval": (
                None if self.confidence_interval is None else list(self.confidence_interval)
            ),
        }


_LANE_VALUES: dict[
    FidelityLane,
    tuple[
        EvidenceDisposition,
        int,
        int,
        int,
        int,
        tuple[str, ...],
        str | None,
        str | None,
        bool,
        tuple[str, ...],
    ],
] = {
    FidelityLane.STRUCTURAL: (
        EvidenceDisposition.QUALIFIED,
        70,
        20,
        0,
        0,
        (
            "parse_schema",
            "hard_constraint_detection",
            "reference_role_order_ownership_detection",
        ),
        "offline_frozen_2026-08-09",
        _SEMANTIC_EVALUATION_FINGERPRINT,
        False,
        ("deterministic_contract_only",),
    ),
    FidelityLane.PERCEPTION: (
        EvidenceDisposition.MISSING,
        0,
        0,
        0,
        1,
        (),
        None,
        None,
        False,
        ("unmocked_raw_media_evidence_missing", "no_ocr_asr_quality_claim"),
    ),
    FidelityLane.PLANNER: (
        EvidenceDisposition.LOCAL_ONLY,
        70,
        20,
        0,
        0,
        (
            "guide_round_trip",
            "p0_mutation_detection",
            "semantic_precision",
            "semantic_recall",
        ),
        "offline_frozen_2026-08-09",
        _SEMANTIC_EVALUATION_FINGERPRINT,
        False,
        ("local_public_guide_only", "no_official_agreement_claim"),
    ),
    FidelityLane.ORACLE: (
        EvidenceDisposition.UNAVAILABLE_PROHIBITED,
        0,
        0,
        0,
        1,
        (),
        None,
        None,
        False,
        ("official_oracle_evidence_missing", "no_official_behavior_claim"),
    ),
    FidelityLane.FIXED_H3: (
        EvidenceDisposition.UNAVAILABLE,
        0,
        0,
        0,
        1,
        (),
        None,
        None,
        True,
        ("fixed_h3_evidence_missing", "noncompensatory"),
    ),
    FidelityLane.HUMAN: (
        EvidenceDisposition.UNAVAILABLE,
        0,
        0,
        0,
        1,
        (),
        None,
        None,
        True,
        ("human_review_evidence_missing", "noncompensatory"),
    ),
    FidelityLane.RESOURCE: (
        EvidenceDisposition.MISSING,
        0,
        0,
        0,
        1,
        (),
        None,
        None,
        False,
        ("resource_quality_evidence_missing", "no_performance_claim"),
    ),
    FidelityLane.SECURITY: (
        EvidenceDisposition.QUALIFIED,
        1,
        1,
        0,
        0,
        ("portable_privacy",),
        "offline_frozen_2026-08-09",
        _TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT,
        False,
        ("portable_content_free_contract_only",),
    ),
}


@dataclass(frozen=True, slots=True)
class LaneReceipt:
    lane: FidelityLane
    disposition: EvidenceDisposition
    case_count: int
    source_cluster_count: int
    failed_count: int
    missing_count: int
    metrics: tuple[MetricReceipt, ...]
    confidence_interval: tuple[str, str] | None
    achieved_power: str | None
    service_window: str | None
    evidence_fingerprint: str | None
    noncompensatory: bool
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> LaneReceipt:
        if type(self) is not LaneReceipt:
            raise FidelityScorecardError("lane receipt is not exact")
        _exact_enum(self.lane, FidelityLane, "fidelity lane")
        expected = _LANE_VALUES[self.lane]
        (
            disposition,
            case_count,
            source_cluster_count,
            failed_count,
            missing_count,
            metric_names,
            service_window,
            evidence_fingerprint,
            noncompensatory,
            limitations,
        ) = expected
        _exact_enum(self.disposition, EvidenceDisposition, "lane disposition")
        for actual, admitted, field_name in (
            (self.disposition, disposition, "lane disposition"),
            (self.case_count, case_count, "lane case count"),
            (self.source_cluster_count, source_cluster_count, "lane source-cluster count"),
            (self.failed_count, failed_count, "lane failed count"),
            (self.missing_count, missing_count, "lane missing count"),
            (self.confidence_interval, None, "lane aggregate interval"),
            (self.achieved_power, None, "lane achieved power"),
            (self.service_window, service_window, "lane service window"),
            (self.evidence_fingerprint, evidence_fingerprint, "lane evidence fingerprint"),
            (self.noncompensatory, noncompensatory, "lane noncompensation state"),
        ):
            _exact(actual, admitted, field_name)
        if type(self.metrics) is not tuple or len(self.metrics) != len(metric_names):
            raise FidelityScorecardError("lane metric inventory drifted")
        for receipt, metric_name in zip(self.metrics, metric_names, strict=True):
            if type(receipt) is not MetricReceipt:
                raise FidelityScorecardError("lane contains a non-metric receipt")
            receipt.require_admitted()
            _exact(receipt.metric, metric_name, "lane metric order")
        _exact_tuple(self.limitations, limitations, "lane limitations")
        # CRITICAL: missing or unavailable evidence must never receive an imputed numeric result.
        if self.disposition in {
            EvidenceDisposition.MISSING,
            EvidenceDisposition.UNAVAILABLE,
            EvidenceDisposition.UNAVAILABLE_PROHIBITED,
            EvidenceDisposition.EXHAUSTED,
            EvidenceDisposition.DRIFT_SUSPENDED,
        } and (
            self.metrics
            or self.confidence_interval is not None
            or self.achieved_power is not None
            or self.service_window is not None
            or self.evidence_fingerprint is not None
        ):
            raise FidelityScorecardError("missing evidence was imputed or substituted")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "lane": self.lane.value,
            "disposition": self.disposition.value,
            "case_count": str(self.case_count),
            "source_cluster_count": str(self.source_cluster_count),
            "failed_count": str(self.failed_count),
            "missing_count": str(self.missing_count),
            "metrics": [item.to_wire() for item in self.metrics],
            "confidence_interval": None,
            "achieved_power": None,
            "service_window": self.service_window,
            "evidence_fingerprint": self.evidence_fingerprint,
            "noncompensatory": self.noncompensatory,
            "limitations": list(self.limitations),
        }


_CLAIM_VALUES: dict[ClaimLevel, tuple[ClaimDisposition, str | None, str, tuple[str, ...]]] = {
    ClaimLevel.DETERMINISTIC_CONFORMANCE: (
        ClaimDisposition.SUPPORTED,
        _SEMANTIC_EVALUATION_FINGERPRINT,
        "format_contract_only",
        ("does_not_imply_semantic_or_quality_equivalence",),
    ),
    ClaimLevel.SEMANTIC_RECONSTRUCTION: (
        ClaimDisposition.SUPPORTED_LOCAL_ONLY,
        _SEMANTIC_EVALUATION_FINGERPRINT,
        "local_public_guide_only",
        ("does_not_imply_official_behavioral_agreement",),
    ),
    ClaimLevel.OFFICIAL_BEHAVIORAL_AGREEMENT: (
        ClaimDisposition.UNVERIFIABLE,
        None,
        "official_evidence_missing",
        ("does_not_imply_fixed_h3_quality",),
    ),
    ClaimLevel.FIXED_H3_NONINFERIORITY_EQUIVALENCE: (
        ClaimDisposition.UNVERIFIABLE,
        None,
        "fixed_h3_evidence_missing",
        ("does_not_imply_human_preference_or_private_recovery",),
    ),
    ClaimLevel.PRIVATE_INTERNAL_RECOVERY: (
        ClaimDisposition.UNVERIFIABLE,
        None,
        "unknown_private_internals",
        ("private_internal_recovery_is_not_claimed",),
    ),
}


@dataclass(frozen=True, slots=True)
class ClaimReceipt:
    level: ClaimLevel
    disposition: ClaimDisposition
    evidence_fingerprint: str | None
    scope: str
    implies_next: bool
    promotion_authorized: bool
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> ClaimReceipt:
        if type(self) is not ClaimReceipt:
            raise FidelityScorecardError("claim receipt is not exact")
        _exact_enum(self.level, ClaimLevel, "claim level")
        _exact_enum(self.disposition, ClaimDisposition, "claim disposition")
        disposition, fingerprint, scope, limitations = _CLAIM_VALUES[self.level]
        for actual, admitted, field_name in (
            (self.disposition, disposition, "claim disposition"),
            (self.evidence_fingerprint, fingerprint, "claim evidence fingerprint"),
            (self.scope, scope, "claim scope"),
            (self.implies_next, False, "claim implication"),
            (self.promotion_authorized, False, "claim promotion"),
        ):
            _exact(actual, admitted, field_name)
        _exact_tuple(self.limitations, limitations, "claim limitations")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "level": self.level.value,
            "disposition": self.disposition.value,
            "evidence_fingerprint": self.evidence_fingerprint,
            "scope": self.scope,
            "implies_next": self.implies_next,
            "promotion_authorized": self.promotion_authorized,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class StatisticalProtocol:
    confidence_level_basis_points: int
    interval_method: str
    multiplicity_method: str
    planned_power_floor_basis_points: int
    failures_remain_in_denominators: bool
    pilot_required_for_change: bool
    threshold_change_after_holdout_allowed: bool
    repeatability_runs: int
    fixed_h3_human_noncompensatory: bool

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> StatisticalProtocol:
        if type(self) is not StatisticalProtocol:
            raise FidelityScorecardError("statistical protocol is not exact")
        for actual, admitted, field_name in (
            (self.confidence_level_basis_points, 9500, "confidence level"),
            (self.interval_method, "wilson_two_sided_95", "interval method"),
            (self.multiplicity_method, "holm_bonferroni", "multiplicity method"),
            (self.planned_power_floor_basis_points, 8000, "planned power floor"),
            (self.failures_remain_in_denominators, True, "failure denominator rule"),
            (self.pilot_required_for_change, True, "pilot rule"),
            (self.threshold_change_after_holdout_allowed, False, "holdout change rule"),
            (self.repeatability_runs, 2, "repeatability runs"),
            (self.fixed_h3_human_noncompensatory, True, "noncompensation rule"),
        ):
            _exact(actual, admitted, field_name)
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "confidence_level_basis_points": str(self.confidence_level_basis_points),
            "interval_method": self.interval_method,
            "multiplicity_method": self.multiplicity_method,
            "planned_power_floor_basis_points": str(self.planned_power_floor_basis_points),
            "failures_remain_in_denominators": self.failures_remain_in_denominators,
            "pilot_required_for_change": self.pilot_required_for_change,
            "threshold_change_after_holdout_allowed": (self.threshold_change_after_holdout_allowed),
            "repeatability_runs": str(self.repeatability_runs),
            "fixed_h3_human_noncompensatory": self.fixed_h3_human_noncompensatory,
        }


_ORACLE_TARGETS: dict[str, tuple[str, str]] = {
    "semantic_micro_f1_lower_95": ("minimum", "0.90000000"),
    "every_stratum_macro_f1_lower_95": ("minimum", "0.80000000"),
    "unsupported_fact_upper_95": ("maximum", "0.05000000"),
}


@dataclass(frozen=True, slots=True)
class OracleTarget:
    metric: str
    direction: str
    threshold: str
    applicability: str
    observed_value: str | None
    observed_interval: tuple[str, str] | None
    verdict: str | None

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> OracleTarget:
        if type(self) is not OracleTarget or type(self.metric) is not str:
            raise FidelityScorecardError("oracle target is not exact")
        expected = _ORACLE_TARGETS.get(self.metric)
        if expected is None:
            raise FidelityScorecardError("oracle target is unknown")
        for actual, admitted, field_name in (
            (self.direction, expected[0], "oracle target direction"),
            (self.threshold, expected[1], "oracle threshold"),
            (self.applicability, "CONDITIONAL_NOT_EXECUTED", "oracle applicability"),
            (self.observed_value, None, "oracle observed value"),
            (self.observed_interval, None, "oracle observed interval"),
            (self.verdict, None, "oracle threshold verdict"),
        ):
            _exact(actual, admitted, field_name)
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "metric": self.metric,
            "direction": self.direction,
            "threshold": self.threshold,
            "applicability": self.applicability,
            "observed_value": None,
            "observed_interval": None,
            "verdict": None,
        }


_ROOT_LIMITATIONS = (
    "manual_only_product_scope",
    "no_composite_score",
    "missing_evidence_not_imputed",
    "no_assisted_profile_claim",
    "no_official_behavioral_agreement_claim",
    "no_fixed_h3_quality_claim",
    "no_human_quality_claim",
    "no_private_internal_recovery_claim",
)


def _new_predecessors() -> tuple[PredecessorReceipt, ...]:
    return tuple(
        PredecessorReceipt(authority, *values) for authority, values in _PREDECESSOR_VALUES.items()
    )


def _new_modes() -> tuple[ModeReceipt, ...]:
    return tuple(ModeReceipt(mode, *values) for mode, values in _MODE_VALUES.items())


def _new_metrics(metric_names: tuple[str, ...]) -> tuple[MetricReceipt, ...]:
    return tuple(MetricReceipt(name, *_METRIC_VALUES[name]) for name in metric_names)


def _new_lanes() -> tuple[LaneReceipt, ...]:
    lanes: list[LaneReceipt] = []
    for lane in FidelityLane:
        (
            disposition,
            case_count,
            source_cluster_count,
            failed_count,
            missing_count,
            metric_names,
            service_window,
            evidence_fingerprint,
            noncompensatory,
            limitations,
        ) = _LANE_VALUES[lane]
        lanes.append(
            LaneReceipt(
                lane,
                disposition,
                case_count,
                source_cluster_count,
                failed_count,
                missing_count,
                _new_metrics(metric_names),
                None,
                None,
                service_window,
                evidence_fingerprint,
                noncompensatory,
                limitations,
            )
        )
    return tuple(lanes)


def _new_claims() -> tuple[ClaimReceipt, ...]:
    return tuple(
        ClaimReceipt(level, disposition, fingerprint, scope, False, False, limitations)
        for level, (disposition, fingerprint, scope, limitations) in _CLAIM_VALUES.items()
    )


def _new_protocol() -> StatisticalProtocol:
    return StatisticalProtocol(
        9500,
        "wilson_two_sided_95",
        "holm_bonferroni",
        8000,
        True,
        True,
        False,
        2,
        True,
    )


def _new_oracle_targets() -> tuple[OracleTarget, ...]:
    return tuple(
        OracleTarget(metric, direction, threshold, "CONDITIONAL_NOT_EXECUTED", None, None, None)
        for metric, (direction, threshold) in _ORACLE_TARGETS.items()
    )


@dataclass(frozen=True, slots=True)
class FidelityScorecard:
    """The one admitted manual-only scorecard; all authority fields are source-owned."""

    schema: str
    scorecard_version: str
    record_date: str
    product_scope: str
    claim_cap: str
    assisted_profile_ids: tuple[str, ...]
    composite_score: None
    promotion_authorized: bool
    predecessors: tuple[PredecessorReceipt, ...]
    modes: tuple[ModeReceipt, ...]
    lanes: tuple[LaneReceipt, ...]
    claims: tuple[ClaimReceipt, ...]
    statistical_protocol: StatisticalProtocol
    oracle_targets: tuple[OracleTarget, ...]
    perturbation_source_cluster_count: int
    perturbation_case_count: int
    official_relation_state: str
    semantic_source_cluster_count: int
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> FidelityScorecard:
        # IMPORTANT: every construction and portable projection rechecks the live predecessor
        # chain; otherwise an object built before upstream drift could emit stale evidence.
        _verify_predecessor_authority()
        if type(self) is not FidelityScorecard:
            raise FidelityScorecardError("scorecard must be an exact admitted value")
        for actual, admitted, field_name in (
            (self.schema, FIDELITY_SCORECARD_SCHEMA, "scorecard schema"),
            (self.scorecard_version, FIDELITY_SCORECARD_VERSION, "scorecard version"),
            (self.record_date, FIDELITY_SCORECARD_RECORD_DATE, "scorecard date"),
            (self.product_scope, "MANUAL_ONLY_SCOPED", "product scope"),
            (self.claim_cap, "MANUAL_DETERMINISTIC_LOCAL_ONLY", "claim cap"),
            (self.composite_score, None, "composite score"),
            (self.promotion_authorized, False, "promotion authorization"),
            (self.perturbation_source_cluster_count, 9, "perturbation source-cluster count"),
            (self.perturbation_case_count, 27, "perturbation case count"),
            (self.official_relation_state, "MISSING", "official relation state"),
            (self.semantic_source_cluster_count, 20, "semantic source-cluster count"),
        ):
            _exact(actual, admitted, field_name)
        _exact_tuple(self.assisted_profile_ids, (), "assisted profile inventory")
        _exact_tuple(self.limitations, _ROOT_LIMITATIONS, "scorecard limitations")
        _require_receipt_inventory(
            self.predecessors,
            PredecessorReceipt,
            tuple(_PREDECESSOR_VALUES),
            lambda item: item.authority,
            "predecessor",
        )
        _require_receipt_inventory(
            self.modes,
            ModeReceipt,
            tuple(_MODE_VALUES),
            lambda item: item.mode,
            "mode",
        )
        _require_receipt_inventory(
            self.lanes,
            LaneReceipt,
            tuple(FidelityLane),
            lambda item: item.lane,
            "lane",
        )
        _require_receipt_inventory(
            self.claims,
            ClaimReceipt,
            tuple(ClaimLevel),
            lambda item: item.level,
            "claim",
        )
        if type(self.statistical_protocol) is not StatisticalProtocol:
            raise FidelityScorecardError("scorecard protocol is not exact")
        self.statistical_protocol.require_admitted()
        _require_receipt_inventory(
            self.oracle_targets,
            OracleTarget,
            tuple(_ORACLE_TARGETS),
            lambda item: item.metric,
            "oracle target",
        )
        if sum(item.case_count for item in self.modes) != 70:
            raise FidelityScorecardError("mode case inventory is not closed")
        if sum(item.source_cluster_count for item in self.modes) != 22:
            raise FidelityScorecardError("mode source-cluster membership inventory is not closed")
        if sum(item.missing_official_result_count for item in self.modes) != 70:
            raise FidelityScorecardError("official missing-result inventory is not closed")
        return self

    def _wire_without_fingerprint(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "schema": self.schema,
            "scorecard_version": self.scorecard_version,
            "record_date": self.record_date,
            "product_scope": self.product_scope,
            "claim_cap": self.claim_cap,
            "assisted_profile_ids": list(self.assisted_profile_ids),
            "composite_score": None,
            "promotion_authorized": self.promotion_authorized,
            "predecessors": [item.to_wire() for item in self.predecessors],
            "modes": [item.to_wire() for item in self.modes],
            "lanes": [item.to_wire() for item in self.lanes],
            "claims": [item.to_wire() for item in self.claims],
            "statistical_protocol": self.statistical_protocol.to_wire(),
            "oracle_targets": [item.to_wire() for item in self.oracle_targets],
            "perturbation_source_cluster_count": str(self.perturbation_source_cluster_count),
            "perturbation_case_count": str(self.perturbation_case_count),
            "official_relation_state": self.official_relation_state,
            "semantic_source_cluster_count": str(self.semantic_source_cluster_count),
            "limitations": list(self.limitations),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self._wire_without_fingerprint())

    def to_wire(self) -> dict[str, object]:
        wire = self._wire_without_fingerprint()
        # IMPORTANT: this wire was already fully admitted above; re-entering the public
        # fingerprint property would repeat the complete live predecessor reconstruction.
        wire["scorecard_fingerprint"] = canonical_fingerprint(wire)
        return wire

    def to_wire_bytes(self) -> bytes:
        return _canonical_wire_bytes(self.to_wire())


class _AdmittedReceipt(Protocol):
    def require_admitted(self) -> object: ...


_ReceiptT = TypeVar("_ReceiptT", bound=_AdmittedReceipt)


def _require_receipt_inventory(
    value: object,
    receipt_type: type[_ReceiptT],
    expected_keys: tuple[object, ...],
    key: Callable[[_ReceiptT], object],
    field_name: str,
) -> None:
    if type(value) is not tuple or len(value) != len(expected_keys):
        raise FidelityScorecardError(f"{field_name} inventory is not closed")
    sequence = cast(tuple[object, ...], value)
    for receipt, expected_key in zip(sequence, expected_keys, strict=True):
        if type(receipt) is not receipt_type:
            raise FidelityScorecardError(f"{field_name} inventory contains a non-receipt")
        admitted_receipt = receipt
        admitted_receipt.require_admitted()
        actual_key = key(admitted_receipt)
        if type(actual_key) is not type(expected_key) or actual_key != expected_key:
            raise FidelityScorecardError(f"{field_name} inventory order drifted")


def _live_predecessor_signature() -> tuple[int, ...]:
    # IMPORTANT: compare live declarations to independent admissions before any cache lookup;
    # direct-import aliases or attacker equality must never authorize upstream drift.
    declarations = (
        (
            _semantic_graph_comparator.M14_01_TERMINAL_DISPOSITION_SHA256,
            _OFFICIAL_TERMINAL_FINGERPRINT.removeprefix("sha256:"),
            "official terminal fingerprint",
        ),
        (
            _perturbation_evaluation.PERTURBATION_EVALUATION_SCHEMA,
            "h3.context.perturbation.evaluation.v1",
            "perturbation schema",
        ),
        (
            _perturbation_evaluation.PERTURBATION_CORPUS_VERSION,
            "1.0.0",
            "perturbation corpus version",
        ),
        (
            _perturbation_evaluation.FROZEN_CORPUS_FINGERPRINT,
            _PERTURBATION_CORPUS_FINGERPRINT,
            "perturbation fingerprint",
        ),
        (
            _perturbation_evaluation.FROZEN_SOURCE_CLUSTER_COUNT,
            9,
            "perturbation source-cluster count",
        ),
        (_perturbation_evaluation.FROZEN_CASE_COUNT, 27, "perturbation case count"),
        (
            _semantic_graph_comparator.SEMANTIC_EVALUATION_SCHEMA,
            "h3.semantic_graph_comparator.evaluation.v1",
            "semantic schema",
        ),
        (
            _semantic_graph_comparator.FROZEN_EVALUATION_BUNDLE_FINGERPRINT,
            _SEMANTIC_EVALUATION_FINGERPRINT,
            "semantic fingerprint",
        ),
        (_semantic_graph_comparator.FROZEN_GUIDE_CLUSTER_COUNT, 2, "semantic guide clusters"),
        (_semantic_graph_comparator.FROZEN_GUIDE_SUPPORT, 6, "semantic guide support"),
        (_semantic_graph_comparator.FROZEN_P0_CLUSTER_COUNT, 8, "semantic P0 clusters"),
        (_semantic_graph_comparator.FROZEN_P0_SUPPORT, 24, "semantic P0 support"),
        (
            _semantic_graph_comparator.FROZEN_NON_P0_CLUSTER_COUNT,
            10,
            "semantic non-P0 clusters",
        ),
        (_semantic_graph_comparator.FROZEN_NON_P0_SUPPORT, 40, "semantic non-P0 support"),
        (
            _semantic_graph_comparator.FROZEN_NON_P0_POSITIVE_SUPPORT,
            20,
            "semantic positive support",
        ),
        (
            _semantic_graph_comparator.FROZEN_NON_P0_NEGATIVE_SUPPORT,
            20,
            "semantic negative support",
        ),
        (
            _semantic_graph_comparator.FROZEN_INTERVAL_METHOD,
            "wilson_two_sided_95",
            "semantic interval method",
        ),
        (
            _semantic_graph_comparator.FROZEN_INTERVAL_Z,
            "1.959963984540054",
            "semantic interval Z",
        ),
        (
            _semantic_graph_comparator.FROZEN_POINT_TARGET,
            "0.95000000",
            "semantic point target",
        ),
        (
            _fixed_h3_generation.FROZEN_FIXED_H3_TERMINAL_FINGERPRINT,
            _FIXED_H3_TERMINAL_FINGERPRINT,
            "fixed-H3 fingerprint",
        ),
        (
            _human_review.FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT,
            _HUMAN_REVIEW_TERMINAL_FINGERPRINT,
            "human-review fingerprint",
        ),
        (
            _training_authorization.FROZEN_TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT,
            _TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT,
            "training-authorization fingerprint",
        ),
    )
    for actual, admitted, field_name in declarations:
        _exact(actual, admitted, field_name)
    # IMPORTANT: the terminal cache key includes each imported predecessor seam used by the
    # transitive builders; otherwise a warm cache can conceal a live dependency replacement.
    functions = (
        _fixed_h3_generation.build_fixed_h3_terminal_record,
        _fixed_h3_generation.evaluate_local_qualification,  # type: ignore[attr-defined]
        _human_review.build_human_review_terminal_record,
        _human_review.build_fixed_h3_terminal_record,  # type: ignore[attr-defined]
        _training_authorization.build_training_authorization_terminal_record,
        _training_authorization.build_human_review_terminal_record,  # type: ignore[attr-defined]
    )
    return (
        id(_fixed_h3_generation.FixedH3TerminalRecord),
        id(_fixed_h3_generation.FixedH3Disposition),
        id(_fixed_h3_generation.FixedH3ClaimCap),
        id(_human_review.HumanReviewTerminalRecord),
        id(_human_review.HumanReviewDisposition),
        id(_human_review.HumanReviewClaimCap),
        id(_training_authorization.TrainingAuthorizationTerminalRecord),
        id(_training_authorization.TrainingDecisionDisposition),
        id(_training_authorization.TrainingClaimCap),
        *(
            identity
            for function in functions
            for identity in (id(function), id(getattr(function, "__code__", None)))
        ),
    )


@lru_cache(maxsize=1)
def _verify_derived_predecessor_authority(_signature: tuple[int, ...]) -> None:
    fixed = _fixed_h3_generation.build_fixed_h3_terminal_record()
    if (
        type(fixed) is not _fixed_h3_generation.FixedH3TerminalRecord
        or fixed.fingerprint != _FIXED_H3_TERMINAL_FINGERPRINT
        or _fixed_h3_generation.FROZEN_FIXED_H3_TERMINAL_FINGERPRINT
        != _FIXED_H3_TERMINAL_FINGERPRINT
        or fixed.disposition is not _fixed_h3_generation.FixedH3Disposition.UNAVAILABLE
        or fixed.claim_cap
        is not _fixed_h3_generation.FixedH3ClaimCap.NO_FIXED_H3_GENERATION_EVIDENCE
    ):
        raise FidelityScorecardError("fixed-H3 predecessor authority drifted")
    human = _human_review.build_human_review_terminal_record()
    if (
        type(human) is not _human_review.HumanReviewTerminalRecord
        or human.fingerprint != _HUMAN_REVIEW_TERMINAL_FINGERPRINT
        or _human_review.FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT
        != _HUMAN_REVIEW_TERMINAL_FINGERPRINT
        or human.disposition is not _human_review.HumanReviewDisposition.UNAVAILABLE
        or human.claim_cap is not _human_review.HumanReviewClaimCap.NO_HUMAN_QUALITY_CLAIM
    ):
        raise FidelityScorecardError("human-review predecessor authority drifted")
    # IMPORTANT: resolve through the module so predecessor reloads cannot leave stale class aliases.
    training = _training_authorization.build_training_authorization_terminal_record()
    if (
        type(training) is not _training_authorization.TrainingAuthorizationTerminalRecord
        or training.fingerprint != _TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT
        or _training_authorization.FROZEN_TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT
        != _TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT
        or training.disposition
        is not _training_authorization.TrainingDecisionDisposition.DO_NOT_IMPLEMENT
        or training.claim_cap
        is not _training_authorization.TrainingClaimCap.NO_TRAINING_AUTHORIZATION_OR_EVIDENCE
    ):
        raise FidelityScorecardError("training predecessor authority drifted")


def _verify_uncached_predecessor_authority() -> None:
    dimensions = tuple(_perturbation_evaluation.PerturbationDimension)
    if len(dimensions) != len(_PERTURBATION_DIMENSION_INVENTORY):
        raise FidelityScorecardError("perturbation predecessor authority drifted")
    for dimension, (expected_name, expected_value) in zip(
        dimensions,
        _PERTURBATION_DIMENSION_INVENTORY,
        strict=True,
    ):
        if (
            type(dimension.name) is not str
            or dimension.name != expected_name
            or type(dimension.value) is not str
            or dimension.value != expected_value
        ):
            raise FidelityScorecardError("perturbation predecessor authority drifted")

    semantic_interval = _semantic_graph_comparator.wilson_interval(20, 20)
    _exact_tuple(
        semantic_interval,
        ("0.83887484", "1.00000000"),
        "semantic predecessor report interval",
    )

    local = _local_qualification.evaluate_local_qualification(
        _local_qualification.build_default_local_qualification_plan()
    )
    if (
        type(local) is not _local_qualification.LocalQualificationReport
        or local.fingerprint != _LOCAL_QUALIFICATION_FINGERPRINT
        or local.product_scope
        is not _local_qualification.ProductScopeDisposition.MANUAL_ONLY_SCOPED
        or local.qualified_candidate_ids
    ):
        raise FidelityScorecardError("local-qualification predecessor authority drifted")


def _verify_predecessor_authority() -> None:
    try:
        signature = _live_predecessor_signature()
        _verify_derived_predecessor_authority(signature)
        # CRITICAL: dimension, statistical, and local authority must be regenerated on every
        # projection; these dependency graphs are intentionally excluded from the terminal cache.
        _verify_uncached_predecessor_authority()
    except FidelityScorecardError:
        raise
    except Exception:
        raise FidelityScorecardError("predecessor authority verification failed") from None


def _new_scorecard() -> FidelityScorecard:
    return FidelityScorecard(
        FIDELITY_SCORECARD_SCHEMA,
        FIDELITY_SCORECARD_VERSION,
        FIDELITY_SCORECARD_RECORD_DATE,
        "MANUAL_ONLY_SCOPED",
        "MANUAL_DETERMINISTIC_LOCAL_ONLY",
        (),
        None,
        False,
        _new_predecessors(),
        _new_modes(),
        _new_lanes(),
        _new_claims(),
        _new_protocol(),
        _new_oracle_targets(),
        9,
        27,
        "MISSING",
        20,
        _ROOT_LIMITATIONS,
    )


def _build_scorecard_with_wire() -> tuple[FidelityScorecard, dict[str, object]]:
    record = _new_scorecard()
    wire = record.to_wire()
    if wire.get("scorecard_fingerprint") != FROZEN_FIDELITY_SCORECARD_FINGERPRINT:
        raise FidelityScorecardError("fidelity scorecard fingerprint drifted")
    return record, wire


def build_fidelity_scorecard() -> FidelityScorecard:
    """Build a fresh scorecard without executing any unavailable evidence lane."""

    return _build_scorecard_with_wire()[0]


def _canonical_wire_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


#: Built on first use, not at import.  Eagerly serialising the whole scorecard here cost 217 ms in
#: every interpreter that touched this package -- 29% of its entire import time -- whether or not
#: anything ever validated a wire.  Deferring changes nothing about the comparison: the same builder
#: produces the same bytes, once, and they are cached.  Assigning this name directly still overrides
#: the cache, which is what the malformed-cache regression relies on.
_ADMITTED_FIDELITY_SCORECARD_WIRE_BYTES: bytes | None = None


def _admitted_wire_bytes() -> bytes:
    """The one admitted scorecard's canonical wire, computed once."""

    global _ADMITTED_FIDELITY_SCORECARD_WIRE_BYTES
    cached = _ADMITTED_FIDELITY_SCORECARD_WIRE_BYTES
    if cached is None:
        cached = _canonical_wire_bytes(_new_scorecard().to_wire())
        _ADMITTED_FIDELITY_SCORECARD_WIRE_BYTES = cached
    return cached


def _assert_exact_json_member_types(
    value: object,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
    key_name: str | None = None,
) -> None:
    # CRITICAL: exact-type closure must run before caller-controlled equality or serialization.
    if depth > MAX_FIDELITY_SCORECARD_DEPTH:
        raise FidelityScorecardError("fidelity scorecard wire exceeds the depth limit")
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        text = value
        if not text or len(text) > MAX_FIDELITY_SCORECARD_STRING_LENGTH:
            raise FidelityScorecardError("fidelity scorecard wire contains unsafe text")
        if any(
            ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF
            for character in text
        ):
            raise FidelityScorecardError("fidelity scorecard wire contains unsafe text")
        folded = text.casefold()
        if any(marker in folded for marker in _FORBIDDEN_VALUE_MARKERS):
            raise FidelityScorecardError("fidelity scorecard wire contains a locator")
        if key_name is not None and key_name.casefold() in _SENSITIVE_KEYS:
            raise FidelityScorecardError("fidelity scorecard wire contains sensitive data")
        return
    if type(value) not in {dict, list}:
        raise FidelityScorecardError("fidelity scorecard wire contains a non-JSON member type")
    identities = seen if seen is not None else set()
    identity = id(value)
    if identity in identities:
        raise FidelityScorecardError("fidelity scorecard wire contains a cycle")
    identities.add(identity)
    try:
        if type(value) is dict:
            mapping = cast(dict[object, object], value)
            if len(mapping) > MAX_FIDELITY_SCORECARD_CONTAINER_ITEMS:
                raise FidelityScorecardError("fidelity scorecard wire exceeds the container limit")
            for member_name, member in mapping.items():
                if type(member_name) is not str:
                    raise FidelityScorecardError(
                        "fidelity scorecard wire contains a non-string member name"
                    )
                _assert_exact_json_member_types(
                    member_name, depth=depth + 1, seen=identities, key_name=None
                )
                _assert_exact_json_member_types(
                    member, depth=depth + 1, seen=identities, key_name=member_name
                )
        else:
            sequence = cast(list[object], value)
            if len(sequence) > MAX_FIDELITY_SCORECARD_CONTAINER_ITEMS:
                raise FidelityScorecardError("fidelity scorecard wire exceeds the container limit")
            for member in sequence:
                _assert_exact_json_member_types(
                    member, depth=depth + 1, seen=identities, key_name=key_name
                )
    finally:
        identities.remove(identity)


def validate_fidelity_scorecard_wire(value: object) -> FidelityScorecard:
    """Validate and regenerate the one admitted portable scorecard."""

    _assert_exact_json_member_types(value)
    if type(value) is not dict:
        raise FidelityScorecardError("fidelity scorecard wire must be an object")
    try:
        actual_bytes = _canonical_wire_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FidelityScorecardError("fidelity scorecard wire is not serializable") from exc
    if len(actual_bytes) > MAX_FIDELITY_SCORECARD_WIRE_BYTES:
        raise FidelityScorecardError("fidelity scorecard wire exceeds the byte limit")
    if actual_bytes != _admitted_wire_bytes():
        raise FidelityScorecardError("fidelity scorecard wire is not the admitted scorecard")
    record, expected = _build_scorecard_with_wire()
    if _canonical_wire_bytes(expected) != _admitted_wire_bytes():
        raise FidelityScorecardError("fidelity scorecard admitted wire drifted")
    return record


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FidelityScorecardError("fidelity scorecard JSON contains duplicate members")
        result[key] = value
    return result


def _reject_numeric_token(token: str) -> object:
    del token
    raise FidelityScorecardError("fidelity scorecard JSON contains an unexpected number")


def _reject_constant(token: str) -> object:
    del token
    raise FidelityScorecardError("fidelity scorecard JSON contains a non-finite value")


def decode_fidelity_scorecard_json(
    payload: str | bytes | bytearray,
) -> FidelityScorecard:
    """Decode strict UTF-8 JSON with duplicate and resource closure."""

    if type(payload) is str:
        text = payload
        try:
            encoded = text.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise FidelityScorecardError("fidelity scorecard JSON is not strict UTF-8") from exc
    elif type(payload) in {bytes, bytearray}:
        encoded = bytes(cast(bytes | bytearray, payload))
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise FidelityScorecardError("fidelity scorecard JSON is not strict UTF-8") from exc
    else:
        raise FidelityScorecardError("fidelity scorecard JSON must be text or bytes")
    if not encoded or len(encoded) > MAX_FIDELITY_SCORECARD_WIRE_BYTES:
        raise FidelityScorecardError("fidelity scorecard JSON exceeds the byte limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_members,
            parse_int=_reject_numeric_token,
            parse_float=_reject_numeric_token,
            parse_constant=_reject_constant,
        )
    except FidelityScorecardError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeError) as exc:
        raise FidelityScorecardError("fidelity scorecard JSON is malformed") from exc
    return validate_fidelity_scorecard_wire(value)


__all__ = [
    "FIDELITY_SCORECARD_SCHEMA",
    "FIDELITY_SCORECARD_VERSION",
    "FIDELITY_SCORECARD_RECORD_DATE",
    "MAX_FIDELITY_SCORECARD_WIRE_BYTES",
    "FROZEN_FIDELITY_SCORECARD_FINGERPRINT",
    "FidelityScorecardError",
    "FidelityLane",
    "EvidenceDisposition",
    "ClaimLevel",
    "ClaimDisposition",
    "PredecessorReceipt",
    "ModeReceipt",
    "MetricReceipt",
    "LaneReceipt",
    "ClaimReceipt",
    "StatisticalProtocol",
    "OracleTarget",
    "FidelityScorecard",
    "build_fidelity_scorecard",
    "validate_fidelity_scorecard_wire",
    "decode_fidelity_scorecard_json",
]
