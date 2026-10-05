"""Fail-closed terminal contract for an unavailable governed human-review study.

This module is deliberately offline and content-free. It freezes the requirements for a future
study while preventing absent reviewers, responses, privacy authority, or observations from
becoming human-quality or regression-promotion evidence.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .fixed_h3_generation import (
    FROZEN_FIXED_H3_TERMINAL_FINGERPRINT,
    FixedH3ClaimCap,
    FixedH3Disposition,
    build_fixed_h3_terminal_record,
)

HUMAN_REVIEW_SCHEMA = "h3.human_review.terminal.v1"
HUMAN_REVIEW_PROTOCOL_SCHEMA = "h3.human_review.protocol.v1"
HUMAN_REVIEW_RUBRIC_SCHEMA = "h3.human_review.rubric.v1"
HUMAN_REVIEW_RECORD_DATE = "2026-08-09"
MAX_HUMAN_REVIEW_WIRE_BYTES = 32_768
MAX_HUMAN_REVIEW_DEPTH = 8
MAX_HUMAN_REVIEW_CONTAINER_ITEMS = 128
MAX_HUMAN_REVIEW_STRING_LENGTH = 256
_MAX_HUMAN_REVIEW_DECIMAL_TOKEN_LENGTH = 32
_MAX_HUMAN_REVIEW_DECIMAL_EXPONENT = 128
M14_05_FIXED_H3_TERMINAL_FINGERPRINT = FROZEN_FIXED_H3_TERMINAL_FINGERPRINT
FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT = (
    "sha256:d1a13b37fc07fb17c1af363256134f2fd182554f3c1b297025dcca6c63dadf16"
)

_FORBIDDEN_VALUE_MARKERS = (
    "http://",
    "https://",
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
_RUBRIC_OUTCOMES = ("left_preferred", "tie", "right_preferred")
_RUBRIC_DIMENSIONS = (
    "instruction_adherence",
    "identity_consistency",
    "temporal_coherence",
    "visual_fidelity",
    "audio_alignment",
    "hard_constraint_preservation",
)


class HumanReviewError(ValueError):
    """Raised when governed human-review evidence is malformed or contradictory."""


class HumanReviewDisposition(str, Enum):
    UNAVAILABLE = "HUMAN_REVIEW_UNAVAILABLE"


class HumanReviewEvidenceClass(str, Enum):
    NONE = "NO_HUMAN_REVIEW_EVIDENCE"


class HumanReviewClaimCap(str, Enum):
    NO_HUMAN_QUALITY_CLAIM = "NO_HUMAN_QUALITY_CLAIM"


class HumanReviewEvidenceStatus(str, Enum):
    MISSING = "MISSING"


class HumanReviewGovernanceRequirement(str, Enum):
    INFORMED_CONSENT = "informed_consent"
    COMPENSATION_OR_VOLUNTEER_TERMS = "compensation_or_volunteer_terms"
    REVIEWER_PII_RETENTION_DELETION = "reviewer_pii_retention_deletion"
    RESPONSE_RETENTION_DELETION = "response_retention_deletion"
    LANGUAGE_QUALIFICATION = "language_qualification"
    TASK_QUALIFICATION = "task_qualification"
    RESTRICTED_MEDIA_ACCESS = "restricted_media_access"
    CONFLICTS = "conflicts"
    EXCLUSION = "exclusion"
    WITHDRAWAL = "withdrawal"
    ACCESSIBILITY = "accessibility"
    FATIGUE_SESSION_LIMITS = "fatigue_session_limits"
    ORDER_LEARNING_CONTROLS = "order_learning_controls"
    HUMAN_SUBJECTS_ETHICS_REVIEW = "human_subjects_ethics_review"


class HumanReviewFailureCategory(str, Enum):
    PERCEPTION = "perception"
    FUSION = "fusion"
    PLANNING = "planning"
    RENDERING = "rendering"
    PROVIDER = "provider"
    H3 = "h3"


def _require_exact_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if type(value) is not enum_type:
        raise HumanReviewError(f"{field_name} must be an exact enum member")


def _require_exact_value(value: object, expected: object, field_name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise HumanReviewError(f"{field_name} is not the admitted unavailable value")


def _require_exact_string_tuple(value: object, expected: tuple[str, ...], field_name: str) -> None:
    if type(value) is not tuple or len(value) != len(expected):
        raise HumanReviewError(f"{field_name} is not the admitted unavailable inventory")
    for actual, admitted in zip(value, expected, strict=True):
        if type(actual) is not str or actual != admitted:
            raise HumanReviewError(f"{field_name} is not the admitted unavailable inventory")


@dataclass(frozen=True, slots=True)
class HumanReviewGovernanceReceipt:
    """One absent governance authority required before a human study may start."""

    requirement: HumanReviewGovernanceRequirement
    status: HumanReviewEvidenceStatus = field(default=HumanReviewEvidenceStatus.MISSING, init=False)
    authority_granted: bool = field(default=False, init=False)
    reason: str = field(init=False)
    evidence_receipts: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        _require_exact_enum(
            self.requirement, HumanReviewGovernanceRequirement, "governance requirement"
        )
        object.__setattr__(self, "reason", f"{self.requirement.value}_authority_unavailable")
        self.require_admitted()

    def require_admitted(self) -> HumanReviewGovernanceReceipt:
        if type(self) is not HumanReviewGovernanceReceipt:
            raise HumanReviewError("governance receipt must be an exact admitted value")
        _require_exact_enum(
            self.requirement, HumanReviewGovernanceRequirement, "governance requirement"
        )
        _require_exact_value(self.status, HumanReviewEvidenceStatus.MISSING, "governance status")
        _require_exact_value(self.authority_granted, False, "governance authority")
        _require_exact_value(
            self.reason,
            f"{self.requirement.value}_authority_unavailable",
            "governance reason",
        )
        _require_exact_string_tuple(self.evidence_receipts, (), "governance evidence receipts")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "requirement": self.requirement.value,
            "status": self.status.value,
            "authority_granted": self.authority_granted,
            "reason": self.reason,
            "evidence_receipts": list(self.evidence_receipts),
        }


@dataclass(frozen=True, slots=True)
class HumanReviewProtocol:
    """Frozen requirements for a future study; not evidence that one was authorized or run."""

    schema: str = field(default=HUMAN_REVIEW_PROTOCOL_SCHEMA, init=False)
    protocol_date: str = field(default=HUMAN_REVIEW_RECORD_DATE, init=False)
    rubric_schema: str = field(default=HUMAN_REVIEW_RUBRIC_SCHEMA, init=False)
    rubric_version: str = field(default="1", init=False)
    rubric_outcomes: tuple[str, ...] = field(default=_RUBRIC_OUTCOMES, init=False)
    rubric_dimensions: tuple[str, ...] = field(default=_RUBRIC_DIMENSIONS, init=False)
    uncertainty_method: str = field(default="reviewer_source_clustered", init=False)
    candidate_position_target_basis_points: int = field(default=5000, init=False)
    study_authorized: bool = field(default=False, init=False)
    blinded_required: bool = field(default=True, init=False)
    randomized_order_required: bool = field(default=True, init=False)
    pairwise_tie_required: bool = field(default=True, init=False)
    balanced_candidate_position_required: bool = field(default=True, init=False)
    swapped_order_controls_required: bool = field(default=True, init=False)
    qualification_anchor_class: str = field(default="deterministic_p0", init=False)
    reviewer_source_uncertainty_required: bool = field(default=True, init=False)
    planned_power_floor_basis_points: int = field(default=8000, init=False)
    qualification_score_floor_basis_points: int = field(default=9000, init=False)
    swapped_order_consistency_floor_basis_points: int = field(default=9500, init=False)
    tie_adjusted_noninferiority_required: bool = field(default=True, init=False)
    two_sided_equivalence_required: bool = field(default=True, init=False)
    separate_comparison_reporting_required: bool = field(default=True, init=False)
    noninferiority_margin: None = field(default=None, init=False)
    equivalence_margin: None = field(default=None, init=False)
    observed_power: None = field(default=None, init=False)
    observed_qualification_score: None = field(default=None, init=False)
    observed_swapped_order_consistency: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> HumanReviewProtocol:
        if type(self) is not HumanReviewProtocol:
            raise HumanReviewError("human-review protocol must be an exact admitted value")
        for value, expected, field_name in (
            (self.schema, HUMAN_REVIEW_PROTOCOL_SCHEMA, "protocol schema"),
            (self.protocol_date, HUMAN_REVIEW_RECORD_DATE, "protocol date"),
            (self.rubric_schema, HUMAN_REVIEW_RUBRIC_SCHEMA, "rubric schema"),
            (self.rubric_version, "1", "rubric version"),
            (
                self.uncertainty_method,
                "reviewer_source_clustered",
                "uncertainty method",
            ),
            (
                self.candidate_position_target_basis_points,
                5000,
                "candidate-position target",
            ),
            (self.study_authorized, False, "study authorization"),
            (self.blinded_required, True, "blinding requirement"),
            (self.randomized_order_required, True, "randomized-order requirement"),
            (self.pairwise_tie_required, True, "pairwise/tie requirement"),
            (
                self.balanced_candidate_position_required,
                True,
                "candidate-position requirement",
            ),
            (self.swapped_order_controls_required, True, "swapped-order requirement"),
            (
                self.qualification_anchor_class,
                "deterministic_p0",
                "qualification anchor class",
            ),
            (
                self.reviewer_source_uncertainty_required,
                True,
                "reviewer/source uncertainty requirement",
            ),
            (self.planned_power_floor_basis_points, 8000, "planned-power floor"),
            (self.qualification_score_floor_basis_points, 9000, "qualification floor"),
            (
                self.swapped_order_consistency_floor_basis_points,
                9500,
                "swapped-order consistency floor",
            ),
            (
                self.tie_adjusted_noninferiority_required,
                True,
                "tie-adjusted noninferiority requirement",
            ),
            (
                self.two_sided_equivalence_required,
                True,
                "two-sided equivalence requirement",
            ),
            (
                self.separate_comparison_reporting_required,
                True,
                "separate comparison reporting requirement",
            ),
            (self.noninferiority_margin, None, "noninferiority margin"),
            (self.equivalence_margin, None, "equivalence margin"),
            (self.observed_power, None, "observed power"),
            (
                self.observed_qualification_score,
                None,
                "observed qualification score",
            ),
            (
                self.observed_swapped_order_consistency,
                None,
                "observed swapped-order consistency",
            ),
        ):
            _require_exact_value(value, expected, field_name)
        _require_exact_string_tuple(self.rubric_outcomes, _RUBRIC_OUTCOMES, "rubric outcomes")
        _require_exact_string_tuple(self.rubric_dimensions, _RUBRIC_DIMENSIONS, "rubric dimensions")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "schema": self.schema,
            "protocol_date": self.protocol_date,
            "rubric_schema": self.rubric_schema,
            "rubric_version": self.rubric_version,
            "rubric_outcomes": list(self.rubric_outcomes),
            "rubric_dimensions": list(self.rubric_dimensions),
            "uncertainty_method": self.uncertainty_method,
            "candidate_position_target_basis_points": (self.candidate_position_target_basis_points),
            "study_authorized": self.study_authorized,
            "blinded_required": self.blinded_required,
            "randomized_order_required": self.randomized_order_required,
            "pairwise_tie_required": self.pairwise_tie_required,
            "balanced_candidate_position_required": self.balanced_candidate_position_required,
            "swapped_order_controls_required": self.swapped_order_controls_required,
            "qualification_anchor_class": self.qualification_anchor_class,
            "reviewer_source_uncertainty_required": self.reviewer_source_uncertainty_required,
            "planned_power_floor_basis_points": self.planned_power_floor_basis_points,
            "qualification_score_floor_basis_points": self.qualification_score_floor_basis_points,
            "swapped_order_consistency_floor_basis_points": (
                self.swapped_order_consistency_floor_basis_points
            ),
            "tie_adjusted_noninferiority_required": self.tie_adjusted_noninferiority_required,
            "two_sided_equivalence_required": self.two_sided_equivalence_required,
            "separate_comparison_reporting_required": self.separate_comparison_reporting_required,
            "noninferiority_margin": self.noninferiority_margin,
            "equivalence_margin": self.equivalence_margin,
            "observed_power": self.observed_power,
            "observed_qualification_score": self.observed_qualification_score,
            "observed_swapped_order_consistency": self.observed_swapped_order_consistency,
        }


@dataclass(frozen=True, slots=True)
class HumanReviewAdjudicationRule:
    """One allowed failure category and its future privacy-safe fixture requirement."""

    category: HumanReviewFailureCategory
    status: HumanReviewEvidenceStatus = field(default=HumanReviewEvidenceStatus.MISSING, init=False)
    privacy_safe_fixture_required: bool = field(default=True, init=False)
    failure_count: int = field(default=0, init=False)
    fixture_count: int = field(default=0, init=False)
    fixture_fingerprints: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        _require_exact_enum(self.category, HumanReviewFailureCategory, "failure category")
        self.require_admitted()

    def require_admitted(self) -> HumanReviewAdjudicationRule:
        if type(self) is not HumanReviewAdjudicationRule:
            raise HumanReviewError("adjudication rule must be an exact admitted value")
        _require_exact_enum(self.category, HumanReviewFailureCategory, "failure category")
        _require_exact_value(self.status, HumanReviewEvidenceStatus.MISSING, "failure status")
        _require_exact_value(
            self.privacy_safe_fixture_required,
            True,
            "privacy-safe fixture requirement",
        )
        _require_exact_value(self.failure_count, 0, "failure count")
        _require_exact_value(self.fixture_count, 0, "fixture count")
        _require_exact_string_tuple(self.fixture_fingerprints, (), "fixture fingerprints")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "category": self.category.value,
            "status": self.status.value,
            "privacy_safe_fixture_required": self.privacy_safe_fixture_required,
            "failure_count": self.failure_count,
            "fixture_count": self.fixture_count,
            "fixture_fingerprints": list(self.fixture_fingerprints),
        }


def _new_governance_receipts() -> tuple[HumanReviewGovernanceReceipt, ...]:
    return tuple(
        HumanReviewGovernanceReceipt(requirement)
        for requirement in HumanReviewGovernanceRequirement
    )


def _new_protocol() -> HumanReviewProtocol:
    return HumanReviewProtocol()


def _new_adjudication_rules() -> tuple[HumanReviewAdjudicationRule, ...]:
    return tuple(HumanReviewAdjudicationRule(category) for category in HumanReviewFailureCategory)


_LIMITATIONS = (
    "lawful_review_authority_unavailable",
    "qualified_reviewer_cohort_unavailable",
    "human_subjects_ethics_review_unavailable",
    "restricted_media_access_unavailable",
    "fixed_h3_generation_evidence_unavailable",
    "no_human_review_evidence",
    "no_human_quality_claim",
    "no_noninferiority_claim",
    "no_equivalence_claim",
    "no_regression_promotion",
)


@dataclass(frozen=True, slots=True)
class HumanReviewTerminalRecord:
    """Exact zero-study record; every evidence-bearing field is constructor-owned."""

    schema: str = field(default=HUMAN_REVIEW_SCHEMA, init=False)
    record_date: str = field(default=HUMAN_REVIEW_RECORD_DATE, init=False)
    disposition: HumanReviewDisposition = field(
        default=HumanReviewDisposition.UNAVAILABLE, init=False
    )
    evidence_class: HumanReviewEvidenceClass = field(
        default=HumanReviewEvidenceClass.NONE, init=False
    )
    claim_cap: HumanReviewClaimCap = field(
        default=HumanReviewClaimCap.NO_HUMAN_QUALITY_CLAIM, init=False
    )
    fixed_h3_terminal_fingerprint: str = field(
        default=M14_05_FIXED_H3_TERMINAL_FINGERPRINT, init=False
    )
    fixed_h3_disposition: str = field(default="UNAVAILABLE", init=False)
    fixed_h3_claim_cap: str = field(default="NO_FIXED_H3_GENERATION_EVIDENCE", init=False)
    governance_receipts: tuple[HumanReviewGovernanceReceipt, ...] = field(
        default_factory=_new_governance_receipts, init=False
    )
    protocol: HumanReviewProtocol = field(default_factory=_new_protocol, init=False)
    adjudication_rules: tuple[HumanReviewAdjudicationRule, ...] = field(
        default_factory=_new_adjudication_rules, init=False
    )
    lawful_review_authority_granted: bool = field(default=False, init=False)
    qualified_reviewer_cohort_available: bool = field(default=False, init=False)
    ethics_review_complete: bool = field(default=False, init=False)
    restricted_media_access_granted: bool = field(default=False, init=False)
    recruitment_started: bool = field(default=False, init=False)
    reviewer_contacted: bool = field(default=False, init=False)
    study_started: bool = field(default=False, init=False)
    model_judge_used: bool = field(default=False, init=False)
    consent_collected: bool = field(default=False, init=False)
    pii_collected: bool = field(default=False, init=False)
    responses_collected: bool = field(default=False, init=False)
    compensation_paid: bool = field(default=False, init=False)
    media_accessed: bool = field(default=False, init=False)
    retention_started: bool = field(default=False, init=False)
    privacy_safe_fixture_created: bool = field(default=False, init=False)
    structural_failure_override_authorized: bool = field(default=False, init=False)
    security_failure_override_authorized: bool = field(default=False, init=False)
    promotion_authorized: bool = field(default=False, init=False)
    publication_authorized: bool = field(default=False, init=False)
    invited_reviewer_count: int = field(default=0, init=False)
    enrolled_reviewer_count: int = field(default=0, init=False)
    admitted_reviewer_count: int = field(default=0, init=False)
    response_count: int = field(default=0, init=False)
    pairwise_comparison_count: int = field(default=0, init=False)
    qualification_anchor_response_count: int = field(default=0, init=False)
    observed_failure_count: int = field(default=0, init=False)
    privacy_safe_fixture_count: int = field(default=0, init=False)
    retained_pii_count: int = field(default=0, init=False)
    retained_response_count: int = field(default=0, init=False)
    reviewer_ids: tuple[str, ...] = field(default=(), init=False)
    consent_receipts: tuple[str, ...] = field(default=(), init=False)
    response_receipts: tuple[str, ...] = field(default=(), init=False)
    payment_receipts: tuple[str, ...] = field(default=(), init=False)
    pii_receipts: tuple[str, ...] = field(default=(), init=False)
    media_receipts: tuple[str, ...] = field(default=(), init=False)
    fixture_fingerprints: tuple[str, ...] = field(default=(), init=False)
    locators: tuple[str, ...] = field(default=(), init=False)
    preference_estimate: None = field(default=None, init=False)
    reviewer_source_variance: None = field(default=None, init=False)
    achieved_power: None = field(default=None, init=False)
    qualification_score: None = field(default=None, init=False)
    swapped_order_consistency: None = field(default=None, init=False)
    noninferiority_estimate: None = field(default=None, init=False)
    noninferiority_interval: None = field(default=None, init=False)
    noninferiority_conclusion: None = field(default=None, init=False)
    equivalence_estimate: None = field(default=None, init=False)
    equivalence_interval: None = field(default=None, init=False)
    equivalence_conclusion: None = field(default=None, init=False)
    limitations: tuple[str, ...] = field(default=_LIMITATIONS, init=False)

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> HumanReviewTerminalRecord:
        if type(self) is not HumanReviewTerminalRecord:
            raise HumanReviewError("terminal record must be an exact admitted value")
        _verify_predecessor_authority()
        for value, expected, field_name in (
            (self.schema, HUMAN_REVIEW_SCHEMA, "terminal schema"),
            (self.record_date, HUMAN_REVIEW_RECORD_DATE, "terminal record date"),
            (self.disposition, HumanReviewDisposition.UNAVAILABLE, "terminal disposition"),
            (self.evidence_class, HumanReviewEvidenceClass.NONE, "terminal evidence class"),
            (
                self.claim_cap,
                HumanReviewClaimCap.NO_HUMAN_QUALITY_CLAIM,
                "terminal claim cap",
            ),
            (
                self.fixed_h3_terminal_fingerprint,
                M14_05_FIXED_H3_TERMINAL_FINGERPRINT,
                "fixed-H3 authority",
            ),
            (self.fixed_h3_disposition, "UNAVAILABLE", "fixed-H3 disposition"),
            (
                self.fixed_h3_claim_cap,
                "NO_FIXED_H3_GENERATION_EVIDENCE",
                "fixed-H3 claim cap",
            ),
        ):
            _require_exact_value(value, expected, field_name)
        for field_name in (
            "lawful_review_authority_granted",
            "qualified_reviewer_cohort_available",
            "ethics_review_complete",
            "restricted_media_access_granted",
            "recruitment_started",
            "reviewer_contacted",
            "study_started",
            "model_judge_used",
            "consent_collected",
            "pii_collected",
            "responses_collected",
            "compensation_paid",
            "media_accessed",
            "retention_started",
            "privacy_safe_fixture_created",
            "structural_failure_override_authorized",
            "security_failure_override_authorized",
            "promotion_authorized",
            "publication_authorized",
        ):
            _require_exact_value(getattr(self, field_name), False, field_name)
        for field_name in (
            "invited_reviewer_count",
            "enrolled_reviewer_count",
            "admitted_reviewer_count",
            "response_count",
            "pairwise_comparison_count",
            "qualification_anchor_response_count",
            "observed_failure_count",
            "privacy_safe_fixture_count",
            "retained_pii_count",
            "retained_response_count",
        ):
            _require_exact_value(getattr(self, field_name), 0, field_name)
        for field_name in (
            "reviewer_ids",
            "consent_receipts",
            "response_receipts",
            "payment_receipts",
            "pii_receipts",
            "media_receipts",
            "fixture_fingerprints",
            "locators",
        ):
            _require_exact_string_tuple(getattr(self, field_name), (), field_name)
        for field_name in (
            "preference_estimate",
            "reviewer_source_variance",
            "achieved_power",
            "qualification_score",
            "swapped_order_consistency",
            "noninferiority_estimate",
            "noninferiority_interval",
            "noninferiority_conclusion",
            "equivalence_estimate",
            "equivalence_interval",
            "equivalence_conclusion",
        ):
            _require_exact_value(getattr(self, field_name), None, field_name)
        _require_exact_string_tuple(self.limitations, _LIMITATIONS, "limitations")
        if type(self.governance_receipts) is not tuple or len(self.governance_receipts) != len(
            HumanReviewGovernanceRequirement
        ):
            raise HumanReviewError("governance inventory is not admitted")
        for receipt, requirement in zip(
            self.governance_receipts, HumanReviewGovernanceRequirement, strict=True
        ):
            if type(receipt) is not HumanReviewGovernanceReceipt:
                raise HumanReviewError("governance inventory contains a non-receipt")
            receipt.require_admitted()
            if receipt.requirement is not requirement:
                raise HumanReviewError("governance inventory order drifted")
        if type(self.protocol) is not HumanReviewProtocol:
            raise HumanReviewError("human-review protocol is not admitted")
        self.protocol.require_admitted()
        if type(self.adjudication_rules) is not tuple or len(self.adjudication_rules) != len(
            HumanReviewFailureCategory
        ):
            raise HumanReviewError("adjudication inventory is not admitted")
        for rule, category in zip(self.adjudication_rules, HumanReviewFailureCategory, strict=True):
            if type(rule) is not HumanReviewAdjudicationRule:
                raise HumanReviewError("adjudication inventory contains a non-rule")
            rule.require_admitted()
            if rule.category is not category:
                raise HumanReviewError("adjudication inventory order drifted")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "schema": self.schema,
            "record_date": self.record_date,
            "disposition": self.disposition.value,
            "evidence_class": self.evidence_class.value,
            "claim_cap": self.claim_cap.value,
            "fixed_h3_terminal_fingerprint": self.fixed_h3_terminal_fingerprint,
            "fixed_h3_disposition": self.fixed_h3_disposition,
            "fixed_h3_claim_cap": self.fixed_h3_claim_cap,
            "governance_receipts": [receipt.to_wire() for receipt in self.governance_receipts],
            "protocol": self.protocol.to_wire(),
            "adjudication_rules": [rule.to_wire() for rule in self.adjudication_rules],
            **{
                field_name: getattr(self, field_name)
                for field_name in (
                    "lawful_review_authority_granted",
                    "qualified_reviewer_cohort_available",
                    "ethics_review_complete",
                    "restricted_media_access_granted",
                    "recruitment_started",
                    "reviewer_contacted",
                    "study_started",
                    "model_judge_used",
                    "consent_collected",
                    "pii_collected",
                    "responses_collected",
                    "compensation_paid",
                    "media_accessed",
                    "retention_started",
                    "privacy_safe_fixture_created",
                    "structural_failure_override_authorized",
                    "security_failure_override_authorized",
                    "promotion_authorized",
                    "publication_authorized",
                    "invited_reviewer_count",
                    "enrolled_reviewer_count",
                    "admitted_reviewer_count",
                    "response_count",
                    "pairwise_comparison_count",
                    "qualification_anchor_response_count",
                    "observed_failure_count",
                    "privacy_safe_fixture_count",
                    "retained_pii_count",
                    "retained_response_count",
                    "preference_estimate",
                    "reviewer_source_variance",
                    "achieved_power",
                    "qualification_score",
                    "swapped_order_consistency",
                    "noninferiority_estimate",
                    "noninferiority_interval",
                    "noninferiority_conclusion",
                    "equivalence_estimate",
                    "equivalence_interval",
                    "equivalence_conclusion",
                )
            },
            "reviewer_ids": list(self.reviewer_ids),
            "consent_receipts": list(self.consent_receipts),
            "response_receipts": list(self.response_receipts),
            "payment_receipts": list(self.payment_receipts),
            "pii_receipts": list(self.pii_receipts),
            "media_receipts": list(self.media_receipts),
            "fixture_fingerprints": list(self.fixture_fingerprints),
            "locators": list(self.locators),
            "limitations": list(self.limitations),
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def _verify_predecessor_authority() -> None:
    predecessor = build_fixed_h3_terminal_record()
    if (
        predecessor.fingerprint != M14_05_FIXED_H3_TERMINAL_FINGERPRINT
        or predecessor.disposition is not FixedH3Disposition.UNAVAILABLE
        or predecessor.claim_cap is not FixedH3ClaimCap.NO_FIXED_H3_GENERATION_EVIDENCE
    ):
        raise HumanReviewError("fixed-H3 predecessor authority drifted")


def _build_human_review_terminal_record_with_wire() -> tuple[
    HumanReviewTerminalRecord, dict[str, object]
]:
    """Build once and retain the same admitted projection for internal bounds and matching."""

    record = HumanReviewTerminalRecord()
    wire = record.to_wire()
    encoded = json.dumps(wire, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    if len(encoded) > MAX_HUMAN_REVIEW_WIRE_BYTES:
        raise HumanReviewError("human-review terminal record exceeds its portable bound")
    if canonical_fingerprint(wire) != FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT:
        raise HumanReviewError("human-review terminal record fingerprint drifted")
    return record, wire


def build_human_review_terminal_record() -> HumanReviewTerminalRecord:
    """Build the only admitted record without contacting people or external systems."""

    record, _wire = _build_human_review_terminal_record_with_wire()
    return record


def _assert_exact_json_member_types(
    value: object, *, depth: int = 0, ancestors: frozenset[int] = frozenset()
) -> None:
    """Reject caller-controlled behavior before schema-compatible semantic comparison."""

    if depth > MAX_HUMAN_REVIEW_DEPTH:
        raise HumanReviewError("human-review wire exceeds its depth bound")
    value_type = type(value)
    if value_type is dict:
        mapping = cast(dict[object, object], value)
        if id(mapping) in ancestors:
            raise HumanReviewError("human-review wire contains a cycle")
        if len(mapping) > MAX_HUMAN_REVIEW_CONTAINER_ITEMS:
            raise HumanReviewError("human-review object exceeds its item bound")
        nested_ancestors = ancestors | {id(mapping)}
        for key, member in mapping.items():
            if type(key) is not str:
                raise HumanReviewError("human-review member names must be exact strings")
            if len(key) > 64 or key.casefold() in _SENSITIVE_KEYS:
                raise HumanReviewError("human-review member name is unsafe")
            _assert_exact_json_member_types(member, depth=depth + 1, ancestors=nested_ancestors)
        return
    if value_type is list:
        sequence = cast(list[object], value)
        if id(sequence) in ancestors:
            raise HumanReviewError("human-review wire contains a cycle")
        if len(sequence) > MAX_HUMAN_REVIEW_CONTAINER_ITEMS:
            raise HumanReviewError("human-review list exceeds its item bound")
        nested_ancestors = ancestors | {id(sequence)}
        for member in sequence:
            _assert_exact_json_member_types(member, depth=depth + 1, ancestors=nested_ancestors)
        return
    if value_type is str:
        text = cast(str, value)
        if len(text) > MAX_HUMAN_REVIEW_STRING_LENGTH:
            raise HumanReviewError("human-review string exceeds its bound")
        if any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in text):
            raise HumanReviewError("human-review string contains an unsafe code point")
        lowered = text.casefold()
        if any(marker in lowered for marker in _FORBIDDEN_VALUE_MARKERS):
            raise HumanReviewError("human-review string contains private or unsafe content")
        return
    if value_type in {bool, int, type(None)}:
        if value_type is int and not -1_000_000 <= cast(int, value) <= 1_000_000:
            raise HumanReviewError("human-review integer exceeds its bound")
        return
    if value_type is float:
        number = cast(float, value)
        if not math.isfinite(number) or not number.is_integer():
            raise HumanReviewError("human-review number is not an integral finite value")
        if not -1_000_000 <= number <= 1_000_000:
            raise HumanReviewError("human-review number exceeds its bound")
        return
    raise HumanReviewError("human-review wire contains a non-JSON member type")


def _matches_portable_wire(value: object, expected: object) -> bool:
    """Match the canonical record using Draft 2020-12 numeric equality only at integer leaves."""

    value_type = type(value)
    expected_type = type(expected)
    if expected_type is int:
        if value_type is int:
            return cast(int, value) == cast(int, expected)
        if value_type is float:
            number = cast(float, value)
            return number.is_integer() and int(number) == cast(int, expected)
        return False
    if value_type is not expected_type:
        return False
    if expected_type is dict:
        mapping = cast(dict[str, object], value)
        expected_mapping = cast(dict[str, object], expected)
        return len(mapping) == len(expected_mapping) and all(
            key in mapping and _matches_portable_wire(mapping[key], member)
            for key, member in expected_mapping.items()
        )
    if expected_type is list:
        sequence = cast(list[object], value)
        expected_sequence = cast(list[object], expected)
        return len(sequence) == len(expected_sequence) and all(
            _matches_portable_wire(member, expected_member)
            for member, expected_member in zip(sequence, expected_sequence, strict=True)
        )
    return value == expected


def validate_human_review_terminal_wire(value: object) -> HumanReviewTerminalRecord:
    """Validate and canonicalize a Python mapping from admitted authorities."""

    # CRITICAL: safe JSON-domain validation must precede semantic comparison so hostile Python
    # subclasses cannot execute caller-controlled equality inside this evidence boundary.
    _assert_exact_json_member_types(value)
    if type(value) is not dict:
        raise HumanReviewError("human-review wire must be a closed object")
    mapping = cast(dict[str, object], value)
    try:
        encoded = json.dumps(
            mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HumanReviewError("human-review wire is not canonical JSON") from exc
    if len(encoded) > MAX_HUMAN_REVIEW_WIRE_BYTES:
        raise HumanReviewError("human-review wire exceeds its portable bound")
    expected, expected_wire = _build_human_review_terminal_record_with_wire()
    # IMPORTANT: Draft 2020-12 treats 0 and 0.0 as the same integer-valued JSON number. Keep that
    # equality limited to expected integer leaves, then return the module-owned canonical record.
    if not _matches_portable_wire(mapping, expected_wire):
        raise HumanReviewError("human-review wire is not evaluator-derived")
    return expected


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HumanReviewError("human-review JSON contains duplicate members")
        result[key] = value
    return result


def _parse_bounded_integer(token: str) -> int:
    if len(token) > 16:
        raise HumanReviewError("human-review JSON integer exceeds its bound")
    return int(token)


def _parse_bounded_decimal(token: str) -> int:
    """Parse an exactly integral JSON decimal without binary-float rounding or underflow."""

    if len(token) > _MAX_HUMAN_REVIEW_DECIMAL_TOKEN_LENGTH:
        raise HumanReviewError("human-review JSON number exceeds its token bound")
    try:
        number = Decimal(token)
    except InvalidOperation as exc:
        raise HumanReviewError("human-review JSON number is invalid") from exc
    exponent = number.as_tuple().exponent
    if type(exponent) is not int or abs(exponent) > _MAX_HUMAN_REVIEW_DECIMAL_EXPONENT:
        raise HumanReviewError("human-review JSON number exceeds its exponent bound")
    if not number.is_finite() or number != number.to_integral_value():
        raise HumanReviewError("human-review JSON number is not exactly integral")
    if not Decimal(-1_000_000) <= number <= Decimal(1_000_000):
        raise HumanReviewError("human-review JSON number exceeds its value bound")
    return int(number)


def _reject_nonfinite(_token: str) -> object:
    raise HumanReviewError("human-review JSON contains a non-finite value")


def decode_human_review_terminal_json(
    payload: str | bytes | bytearray,
) -> HumanReviewTerminalRecord:
    """Decode bounded duplicate-aware JSON and validate exact unavailable semantics."""

    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise HumanReviewError("human-review JSON contains an unsafe code point") from exc
        text = payload
    elif type(payload) is bytes:
        encoded = payload
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise HumanReviewError("human-review JSON is not strict UTF-8") from exc
    elif type(payload) is bytearray:
        encoded = bytes(payload)
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise HumanReviewError("human-review JSON is not strict UTF-8") from exc
    else:
        raise HumanReviewError("human-review JSON must be text or bytes")
    if len(encoded) > MAX_HUMAN_REVIEW_WIRE_BYTES:
        raise HumanReviewError("human-review JSON exceeds its portable bound")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_int=_parse_bounded_integer,
            # CRITICAL: keep exact decimal tokens out of binary float so rounding/underflow cannot
            # turn a non-equal source value into admitted evidence.
            parse_float=_parse_bounded_decimal,
            parse_constant=_reject_nonfinite,
        )
    except HumanReviewError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise HumanReviewError("human-review JSON is invalid") from exc
    return validate_human_review_terminal_wire(value)


__all__ = [
    "HUMAN_REVIEW_SCHEMA",
    "HUMAN_REVIEW_PROTOCOL_SCHEMA",
    "HUMAN_REVIEW_RUBRIC_SCHEMA",
    "HUMAN_REVIEW_RECORD_DATE",
    "MAX_HUMAN_REVIEW_WIRE_BYTES",
    "M14_05_FIXED_H3_TERMINAL_FINGERPRINT",
    "FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT",
    "HumanReviewError",
    "HumanReviewDisposition",
    "HumanReviewEvidenceClass",
    "HumanReviewClaimCap",
    "HumanReviewEvidenceStatus",
    "HumanReviewGovernanceRequirement",
    "HumanReviewFailureCategory",
    "HumanReviewGovernanceReceipt",
    "HumanReviewProtocol",
    "HumanReviewAdjudicationRule",
    "HumanReviewTerminalRecord",
    "build_human_review_terminal_record",
    "validate_human_review_terminal_wire",
    "decode_human_review_terminal_json",
]
