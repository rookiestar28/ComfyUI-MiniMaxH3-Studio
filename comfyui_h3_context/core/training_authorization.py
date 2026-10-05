"""Fail-closed terminal decision for optional training and distillation work.

The accepted authority inventory does not admit training, data collection, model/output use,
compute spend, or an implementation experiment. This module records that negative decision as a
portable, content-free contract. It deliberately contains no executor, provider, model, dataset,
GPU, subprocess, or filesystem implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .human_review import (
    FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT,
    HumanReviewClaimCap,
    HumanReviewDisposition,
    HumanReviewTerminalRecord,
    build_human_review_terminal_record,
)

TRAINING_AUTHORIZATION_SCHEMA = "h3.training_authorization.terminal.v1"
TRAINING_AUTHORIZATION_RECORD_DATE = "2026-08-09"
MAX_TRAINING_AUTHORIZATION_WIRE_BYTES = 32_768
MAX_TRAINING_AUTHORIZATION_DEPTH = 8
MAX_TRAINING_AUTHORIZATION_CONTAINER_ITEMS = 128
MAX_TRAINING_AUTHORIZATION_STRING_LENGTH = 256
M14_06_HUMAN_REVIEW_TERMINAL_FINGERPRINT = FROZEN_HUMAN_REVIEW_TERMINAL_FINGERPRINT
FROZEN_TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT = (
    "sha256:55a1993f024b4fb3942acb46eb3908c4cadbcabc521477503b55bd02a257cace"
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


class TrainingAuthorizationError(ValueError):
    """Raised when the terminal decision is malformed or contradicts the admitted state."""


class TrainingDecisionDisposition(str, Enum):
    DO_NOT_IMPLEMENT = "DO_NOT_IMPLEMENT"


class TrainingClaimCap(str, Enum):
    NO_TRAINING_AUTHORIZATION_OR_EVIDENCE = "NO_TRAINING_AUTHORIZATION_OR_EVIDENCE"


class TrainingMethod(str, Enum):
    ORACLE_OUTPUT_TRAINING = "oracle_output_training"
    SYNTHETIC_AUGMENTATION = "synthetic_augmentation"
    PREFERENCE_OPTIMIZATION = "preference_optimization"
    DISTILLATION = "distillation"
    LOCAL_FINE_TUNING = "local_fine_tuning"


class TrainingReviewDomain(str, Enum):
    LEGAL = "legal"
    TERMS = "terms"
    PRIVACY = "privacy"
    LICENSE = "license"
    COMPUTE = "compute"
    MARGINAL_VALUE = "marginal_value"


class TrainingReviewStatus(str, Enum):
    APPROVED = "approved"
    UNRESOLVED = "unresolved"
    DENIED_BY_CURRENT_DECISION = "denied_by_current_decision"
    NOT_ADMITTED = "not_admitted"
    NOT_AUTHORIZED = "not_authorized"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class TrainingMethodStatus(str, Enum):
    BLOCKED = "blocked"


def _domain_result(domain: TrainingReviewDomain) -> tuple[TrainingReviewStatus, str]:
    if domain is TrainingReviewDomain.LEGAL:
        return TrainingReviewStatus.UNRESOLVED, "legal_authority_unresolved"
    if domain is TrainingReviewDomain.TERMS:
        return (
            TrainingReviewStatus.DENIED_BY_CURRENT_DECISION,
            "current_terms_decision_denies_training_distillation",
        )
    if domain is TrainingReviewDomain.PRIVACY:
        return (
            TrainingReviewStatus.NOT_ADMITTED,
            "training_data_privacy_authority_not_admitted",
        )
    if domain is TrainingReviewDomain.LICENSE:
        return (
            TrainingReviewStatus.NOT_ADMITTED,
            "model_output_and_data_license_not_admitted",
        )
    if domain is TrainingReviewDomain.COMPUTE:
        return (
            TrainingReviewStatus.NOT_AUTHORIZED,
            "compute_and_spend_envelope_not_authorized",
        )
    if domain is TrainingReviewDomain.MARGINAL_VALUE:
        return (
            TrainingReviewStatus.INSUFFICIENT_EVIDENCE,
            "marginal_value_not_established",
        )
    raise TrainingAuthorizationError("review domain is not admitted")


def _method_reason(method: TrainingMethod) -> str:
    if method is TrainingMethod.ORACLE_OUTPUT_TRAINING:
        return "official_oracle_output_authority_denied"
    if method is TrainingMethod.SYNTHETIC_AUGMENTATION:
        return "synthetic_data_authority_and_marginal_value_not_established"
    if method is TrainingMethod.PREFERENCE_OPTIMIZATION:
        return "preference_data_and_human_evidence_unavailable"
    if method is TrainingMethod.DISTILLATION:
        return "source_output_and_model_authority_not_admitted"
    if method is TrainingMethod.LOCAL_FINE_TUNING:
        return "model_data_compute_and_marginal_value_authority_not_admitted"
    raise TrainingAuthorizationError("training method is not admitted")


_FUTURE_APPROVAL_REQUIREMENTS = (
    "fresh_legal_terms_privacy_license_review",
    "admitted_data_authority",
    "bounded_objective",
    "bounded_compute_envelope",
    "measured_expected_gain",
    "rollback_plan",
    "separate_explicit_user_authorization",
    "dated_separate_roadmap_item_and_plan",
)

_LIMITATIONS = (
    "no_training_authorization",
    "no_training_or_quality_evidence",
    "no_data_or_model_authority",
    "no_compute_or_spend_authorization",
    "no_marginal_value_evidence",
    "affirmative_work_requires_separate_item_and_user_authorization",
    "not_legal_advice",
)


def _require_exact_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if type(value) is not enum_type:
        raise TrainingAuthorizationError(f"{field_name} must be an exact enum member")


def _require_exact_value(value: object, expected: object, field_name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise TrainingAuthorizationError(f"{field_name} is not the admitted decision value")


def _require_exact_string_tuple(value: object, expected: tuple[str, ...], field_name: str) -> None:
    if type(value) is not tuple or len(value) != len(expected):
        raise TrainingAuthorizationError(f"{field_name} is not the admitted decision inventory")
    for actual, admitted in zip(value, expected, strict=True):
        if type(actual) is not str or actual != admitted:
            raise TrainingAuthorizationError(f"{field_name} is not the admitted decision inventory")


@dataclass(frozen=True, slots=True)
class TrainingAuthorizationDomainReceipt:
    """One blocking review-domain result in the negative decision."""

    domain: TrainingReviewDomain
    status: TrainingReviewStatus = field(init=False)
    blocking: bool = field(default=True, init=False)
    reason: str = field(init=False)
    authority_receipts: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        _require_exact_enum(self.domain, TrainingReviewDomain, "review domain")
        status, reason = _domain_result(self.domain)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", reason)
        self.require_admitted()

    def require_admitted(self) -> TrainingAuthorizationDomainReceipt:
        if type(self) is not TrainingAuthorizationDomainReceipt:
            raise TrainingAuthorizationError("domain receipt must be an exact admitted value")
        _require_exact_enum(self.domain, TrainingReviewDomain, "review domain")
        expected_status, expected_reason = _domain_result(self.domain)
        _require_exact_enum(self.status, TrainingReviewStatus, "review status")
        _require_exact_value(self.status, expected_status, "review status")
        _require_exact_value(self.blocking, True, "review blocking state")
        _require_exact_value(self.reason, expected_reason, "review reason")
        _require_exact_string_tuple(self.authority_receipts, (), "review authority receipts")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "domain": self.domain.value,
            "status": self.status.value,
            "blocking": self.blocking,
            "reason": self.reason,
            "authority_receipts": list(self.authority_receipts),
        }


@dataclass(frozen=True, slots=True)
class TrainingAuthorizationMethodReceipt:
    """One candidate training method, closed as both unpermitted and unnecessary."""

    method: TrainingMethod
    status: TrainingMethodStatus = field(default=TrainingMethodStatus.BLOCKED, init=False)
    permitted: bool = field(default=False, init=False)
    necessary: bool = field(default=False, init=False)
    reason: str = field(init=False)
    authority_receipts: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        _require_exact_enum(self.method, TrainingMethod, "training method")
        object.__setattr__(self, "reason", _method_reason(self.method))
        self.require_admitted()

    def require_admitted(self) -> TrainingAuthorizationMethodReceipt:
        if type(self) is not TrainingAuthorizationMethodReceipt:
            raise TrainingAuthorizationError("method receipt must be an exact admitted value")
        _require_exact_enum(self.method, TrainingMethod, "training method")
        _require_exact_enum(self.status, TrainingMethodStatus, "method status")
        _require_exact_value(self.status, TrainingMethodStatus.BLOCKED, "method status")
        _require_exact_value(self.permitted, False, "method permission")
        _require_exact_value(self.necessary, False, "method necessity")
        _require_exact_value(self.reason, _method_reason(self.method), "method reason")
        _require_exact_string_tuple(self.authority_receipts, (), "method authority receipts")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "method": self.method.value,
            "status": self.status.value,
            "permitted": self.permitted,
            "necessary": self.necessary,
            "reason": self.reason,
            "authority_receipts": list(self.authority_receipts),
        }


def _new_domain_reviews() -> tuple[TrainingAuthorizationDomainReceipt, ...]:
    return tuple(TrainingAuthorizationDomainReceipt(domain) for domain in TrainingReviewDomain)


def _new_method_reviews() -> tuple[TrainingAuthorizationMethodReceipt, ...]:
    return tuple(TrainingAuthorizationMethodReceipt(method) for method in TrainingMethod)


@dataclass(frozen=True, slots=True)
class TrainingAuthorizationTerminalRecord:
    """Exact no-training decision; every authority-bearing field is constructor-owned."""

    schema: str = field(default=TRAINING_AUTHORIZATION_SCHEMA, init=False)
    record_date: str = field(default=TRAINING_AUTHORIZATION_RECORD_DATE, init=False)
    disposition: TrainingDecisionDisposition = field(
        default=TrainingDecisionDisposition.DO_NOT_IMPLEMENT, init=False
    )
    claim_cap: TrainingClaimCap = field(
        default=TrainingClaimCap.NO_TRAINING_AUTHORIZATION_OR_EVIDENCE, init=False
    )
    decision_reason: str = field(default="authority_and_necessity_not_established", init=False)
    human_review_terminal_fingerprint: str = field(
        default=M14_06_HUMAN_REVIEW_TERMINAL_FINGERPRINT, init=False
    )
    human_review_disposition: str = field(default="HUMAN_REVIEW_UNAVAILABLE", init=False)
    human_review_claim_cap: str = field(default="NO_HUMAN_QUALITY_CLAIM", init=False)
    official_oracle_terms_status: str = field(default="do_not_implement", init=False)
    official_oracle_training_distillation: str = field(default="deny", init=False)
    official_oracle_training_lane: str = field(default="unavailable_prohibited", init=False)
    method_reviews: tuple[TrainingAuthorizationMethodReceipt, ...] = field(
        default_factory=_new_method_reviews, init=False
    )
    domain_reviews: tuple[TrainingAuthorizationDomainReceipt, ...] = field(
        default_factory=_new_domain_reviews, init=False
    )
    training_authorized: bool = field(default=False, init=False)
    implementation_authorized: bool = field(default=False, init=False)
    data_collection_authorized: bool = field(default=False, init=False)
    provider_output_use_authorized: bool = field(default=False, init=False)
    model_weight_use_authorized: bool = field(default=False, init=False)
    compute_spend_authorized: bool = field(default=False, init=False)
    publication_authorized: bool = field(default=False, init=False)
    requires_separate_user_authorization: bool = field(default=True, init=False)
    requires_new_roadmap_item: bool = field(default=True, init=False)
    bounded_objective: None = field(default=None, init=False)
    data_authority: None = field(default=None, init=False)
    compute_envelope: None = field(default=None, init=False)
    expected_gain: None = field(default=None, init=False)
    rollback_plan: None = field(default=None, init=False)
    required_new_roadmap_item: None = field(default=None, init=False)
    source_datasets: tuple[str, ...] = field(default=(), init=False)
    training_examples: tuple[str, ...] = field(default=(), init=False)
    oracle_outputs: tuple[str, ...] = field(default=(), init=False)
    synthetic_outputs: tuple[str, ...] = field(default=(), init=False)
    preference_records: tuple[str, ...] = field(default=(), init=False)
    compute_receipts: tuple[str, ...] = field(default=(), init=False)
    training_runs: tuple[str, ...] = field(default=(), init=False)
    model_artifacts: tuple[str, ...] = field(default=(), init=False)
    retained_artifacts: tuple[str, ...] = field(default=(), init=False)
    publications: tuple[str, ...] = field(default=(), init=False)
    future_approval_requirements: tuple[str, ...] = field(
        default=_FUTURE_APPROVAL_REQUIREMENTS, init=False
    )
    limitations: tuple[str, ...] = field(default=_LIMITATIONS, init=False)

    def __post_init__(self) -> None:
        # IMPORTANT: join the live predecessor once per fresh record; projections revalidate self.
        _verify_predecessor_authority()
        self.require_admitted()

    def require_admitted(self) -> TrainingAuthorizationTerminalRecord:
        if type(self) is not TrainingAuthorizationTerminalRecord:
            raise TrainingAuthorizationError("terminal record must be an exact admitted value")
        for value, expected, field_name in (
            (self.schema, TRAINING_AUTHORIZATION_SCHEMA, "terminal schema"),
            (self.record_date, TRAINING_AUTHORIZATION_RECORD_DATE, "terminal record date"),
            (
                self.disposition,
                TrainingDecisionDisposition.DO_NOT_IMPLEMENT,
                "terminal disposition",
            ),
            (
                self.claim_cap,
                TrainingClaimCap.NO_TRAINING_AUTHORIZATION_OR_EVIDENCE,
                "terminal claim cap",
            ),
            (
                self.decision_reason,
                "authority_and_necessity_not_established",
                "decision reason",
            ),
            (
                self.human_review_terminal_fingerprint,
                M14_06_HUMAN_REVIEW_TERMINAL_FINGERPRINT,
                "human-review predecessor fingerprint",
            ),
            (
                self.human_review_disposition,
                "HUMAN_REVIEW_UNAVAILABLE",
                "human-review disposition",
            ),
            (
                self.human_review_claim_cap,
                "NO_HUMAN_QUALITY_CLAIM",
                "human-review claim cap",
            ),
            (
                self.official_oracle_terms_status,
                "do_not_implement",
                "official-oracle terms status",
            ),
            (
                self.official_oracle_training_distillation,
                "deny",
                "official-oracle training decision",
            ),
            (
                self.official_oracle_training_lane,
                "unavailable_prohibited",
                "official-oracle training lane",
            ),
        ):
            _require_exact_value(value, expected, field_name)
        for field_name in (
            "training_authorized",
            "implementation_authorized",
            "data_collection_authorized",
            "provider_output_use_authorized",
            "model_weight_use_authorized",
            "compute_spend_authorized",
            "publication_authorized",
        ):
            _require_exact_value(getattr(self, field_name), False, field_name)
        for field_name in (
            "requires_separate_user_authorization",
            "requires_new_roadmap_item",
        ):
            _require_exact_value(getattr(self, field_name), True, field_name)
        for field_name in (
            "bounded_objective",
            "data_authority",
            "compute_envelope",
            "expected_gain",
            "rollback_plan",
            "required_new_roadmap_item",
        ):
            _require_exact_value(getattr(self, field_name), None, field_name)
        for field_name in (
            "source_datasets",
            "training_examples",
            "oracle_outputs",
            "synthetic_outputs",
            "preference_records",
            "compute_receipts",
            "training_runs",
            "model_artifacts",
            "retained_artifacts",
            "publications",
        ):
            _require_exact_string_tuple(getattr(self, field_name), (), field_name)
        _require_exact_string_tuple(
            self.future_approval_requirements,
            _FUTURE_APPROVAL_REQUIREMENTS,
            "future approval requirements",
        )
        _require_exact_string_tuple(self.limitations, _LIMITATIONS, "limitations")
        if type(self.method_reviews) is not tuple or len(self.method_reviews) != len(
            TrainingMethod
        ):
            raise TrainingAuthorizationError("training method inventory is not admitted")
        for receipt, method in zip(self.method_reviews, TrainingMethod, strict=True):
            if type(receipt) is not TrainingAuthorizationMethodReceipt:
                raise TrainingAuthorizationError("training method inventory contains a non-receipt")
            receipt.require_admitted()
            if receipt.method is not method:
                raise TrainingAuthorizationError("training method inventory order drifted")
        if type(self.domain_reviews) is not tuple or len(self.domain_reviews) != len(
            TrainingReviewDomain
        ):
            raise TrainingAuthorizationError("review domain inventory is not admitted")
        for domain_receipt, domain in zip(self.domain_reviews, TrainingReviewDomain, strict=True):
            if type(domain_receipt) is not TrainingAuthorizationDomainReceipt:
                raise TrainingAuthorizationError("review domain inventory contains a non-receipt")
            domain_receipt.require_admitted()
            if domain_receipt.domain is not domain:
                raise TrainingAuthorizationError("review domain inventory order drifted")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "schema": self.schema,
            "record_date": self.record_date,
            "disposition": self.disposition.value,
            "claim_cap": self.claim_cap.value,
            "decision_reason": self.decision_reason,
            "human_review_terminal_fingerprint": self.human_review_terminal_fingerprint,
            "human_review_disposition": self.human_review_disposition,
            "human_review_claim_cap": self.human_review_claim_cap,
            "official_oracle_terms_status": self.official_oracle_terms_status,
            "official_oracle_training_distillation": (self.official_oracle_training_distillation),
            "official_oracle_training_lane": self.official_oracle_training_lane,
            "method_reviews": [receipt.to_wire() for receipt in self.method_reviews],
            "domain_reviews": [receipt.to_wire() for receipt in self.domain_reviews],
            "training_authorized": self.training_authorized,
            "implementation_authorized": self.implementation_authorized,
            "data_collection_authorized": self.data_collection_authorized,
            "provider_output_use_authorized": self.provider_output_use_authorized,
            "model_weight_use_authorized": self.model_weight_use_authorized,
            "compute_spend_authorized": self.compute_spend_authorized,
            "publication_authorized": self.publication_authorized,
            "requires_separate_user_authorization": self.requires_separate_user_authorization,
            "requires_new_roadmap_item": self.requires_new_roadmap_item,
            "bounded_objective": self.bounded_objective,
            "data_authority": self.data_authority,
            "compute_envelope": self.compute_envelope,
            "expected_gain": self.expected_gain,
            "rollback_plan": self.rollback_plan,
            "required_new_roadmap_item": self.required_new_roadmap_item,
            "source_datasets": list(self.source_datasets),
            "training_examples": list(self.training_examples),
            "oracle_outputs": list(self.oracle_outputs),
            "synthetic_outputs": list(self.synthetic_outputs),
            "preference_records": list(self.preference_records),
            "compute_receipts": list(self.compute_receipts),
            "training_runs": list(self.training_runs),
            "model_artifacts": list(self.model_artifacts),
            "retained_artifacts": list(self.retained_artifacts),
            "publications": list(self.publications),
            "future_approval_requirements": list(self.future_approval_requirements),
            "limitations": list(self.limitations),
        }

    def to_wire_bytes(self) -> bytes:
        return _canonical_wire_bytes(self.to_wire())

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def _verify_predecessor_authority() -> None:
    predecessor = build_human_review_terminal_record()
    if (
        type(predecessor) is not HumanReviewTerminalRecord
        or predecessor.fingerprint != M14_06_HUMAN_REVIEW_TERMINAL_FINGERPRINT
        or predecessor.disposition is not HumanReviewDisposition.UNAVAILABLE
        or predecessor.claim_cap is not HumanReviewClaimCap.NO_HUMAN_QUALITY_CLAIM
    ):
        raise TrainingAuthorizationError("human-review predecessor authority drifted")


def _canonical_wire_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


# Immutable bytes permit malformed-wire rejection without rebuilding the accepted predecessor.
# Built on first use: doing it at import cost 89 ms in every interpreter that touched this package,
# paid whether or not any wire was ever validated.  Assigning this name overrides the cache.
_ADMITTED_TRAINING_AUTHORIZATION_WIRE_BYTES: bytes | None = None


def _admitted_wire_bytes() -> bytes:
    """The accepted terminal record's canonical wire, computed once."""

    global _ADMITTED_TRAINING_AUTHORIZATION_WIRE_BYTES
    cached = _ADMITTED_TRAINING_AUTHORIZATION_WIRE_BYTES
    if cached is None:
        cached = _canonical_wire_bytes(TrainingAuthorizationTerminalRecord().to_wire())
        _ADMITTED_TRAINING_AUTHORIZATION_WIRE_BYTES = cached
    return cached


def _build_training_authorization_terminal_record_with_wire() -> tuple[
    TrainingAuthorizationTerminalRecord, dict[str, object]
]:
    record = TrainingAuthorizationTerminalRecord()
    wire = record.to_wire()
    fingerprint = canonical_fingerprint(wire)
    if fingerprint != FROZEN_TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT:
        raise TrainingAuthorizationError("training authorization terminal fingerprint drifted")
    return record, wire


def build_training_authorization_terminal_record() -> TrainingAuthorizationTerminalRecord:
    """Build a fresh, isolated negative decision without executing optional work."""

    return _build_training_authorization_terminal_record_with_wire()[0]


def _assert_exact_json_member_types(
    value: object,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
    key_name: str | None = None,
) -> None:
    # CRITICAL: exact-type closure must run before any caller-controlled equality or serialization.
    if depth > MAX_TRAINING_AUTHORIZATION_DEPTH:
        raise TrainingAuthorizationError("training authorization wire exceeds the depth limit")
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        text = value
        if not text or len(text) > MAX_TRAINING_AUTHORIZATION_STRING_LENGTH:
            raise TrainingAuthorizationError("training authorization wire contains unsafe text")
        if any(ord(character) < 32 or ord(character) == 127 for character in text):
            raise TrainingAuthorizationError("training authorization wire contains unsafe text")
        folded = text.casefold()
        if any(marker in folded for marker in _FORBIDDEN_VALUE_MARKERS):
            raise TrainingAuthorizationError("training authorization wire contains a locator")
        if key_name is not None and key_name.casefold() in _SENSITIVE_KEYS:
            raise TrainingAuthorizationError("training authorization wire contains sensitive data")
        return
    if type(value) not in {dict, list}:
        raise TrainingAuthorizationError(
            "training authorization wire contains a non-JSON member type"
        )
    identities = seen if seen is not None else set()
    identity = id(value)
    if identity in identities:
        raise TrainingAuthorizationError("training authorization wire contains a cycle")
    identities.add(identity)
    try:
        if type(value) is dict:
            mapping = cast(dict[object, object], value)
            if len(mapping) > MAX_TRAINING_AUTHORIZATION_CONTAINER_ITEMS:
                raise TrainingAuthorizationError(
                    "training authorization wire exceeds the container limit"
                )
            for key, member in mapping.items():
                if type(key) is not str:
                    raise TrainingAuthorizationError(
                        "training authorization wire contains a non-string member name"
                    )
                _assert_exact_json_member_types(
                    key, depth=depth + 1, seen=identities, key_name=None
                )
                _assert_exact_json_member_types(
                    member,
                    depth=depth + 1,
                    seen=identities,
                    key_name=key,
                )
        else:
            sequence = cast(list[object], value)
            if len(sequence) > MAX_TRAINING_AUTHORIZATION_CONTAINER_ITEMS:
                raise TrainingAuthorizationError(
                    "training authorization wire exceeds the container limit"
                )
            for member in sequence:
                _assert_exact_json_member_types(
                    member, depth=depth + 1, seen=identities, key_name=key_name
                )
    finally:
        identities.remove(identity)


def validate_training_authorization_terminal_wire(
    value: object,
) -> TrainingAuthorizationTerminalRecord:
    """Validate and regenerate the one admitted portable terminal decision."""

    _assert_exact_json_member_types(value)
    if type(value) is not dict:
        raise TrainingAuthorizationError("training authorization wire must be an object")
    try:
        actual_bytes = _canonical_wire_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TrainingAuthorizationError("training authorization wire is not serializable") from exc
    if len(actual_bytes) > MAX_TRAINING_AUTHORIZATION_WIRE_BYTES:
        raise TrainingAuthorizationError("training authorization wire exceeds the byte limit")
    if actual_bytes != _admitted_wire_bytes():
        raise TrainingAuthorizationError("training authorization wire is not the admitted decision")
    record, expected = _build_training_authorization_terminal_record_with_wire()
    if _canonical_wire_bytes(expected) != _admitted_wire_bytes():
        raise TrainingAuthorizationError("training authorization admitted wire drifted")
    return record


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TrainingAuthorizationError(
                "training authorization JSON contains duplicate members"
            )
        result[key] = value
    return result


def _reject_numeric_token(token: str) -> object:
    del token
    raise TrainingAuthorizationError("training authorization JSON contains an unexpected number")


def _reject_constant(token: str) -> object:
    del token
    raise TrainingAuthorizationError("training authorization JSON contains a non-finite value")


def decode_training_authorization_terminal_json(
    payload: str | bytes | bytearray,
) -> TrainingAuthorizationTerminalRecord:
    """Decode strict UTF-8 JSON with duplicate and resource closure."""

    if type(payload) is str:
        text = payload
        try:
            encoded = text.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise TrainingAuthorizationError(
                "training authorization JSON is not strict UTF-8"
            ) from exc
    elif type(payload) in {bytes, bytearray}:
        encoded = bytes(cast(bytes | bytearray, payload))
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise TrainingAuthorizationError(
                "training authorization JSON is not strict UTF-8"
            ) from exc
    else:
        raise TrainingAuthorizationError("training authorization JSON must be text or bytes")
    if not encoded or len(encoded) > MAX_TRAINING_AUTHORIZATION_WIRE_BYTES:
        raise TrainingAuthorizationError("training authorization JSON exceeds the byte limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_members,
            parse_int=_reject_numeric_token,
            parse_float=_reject_numeric_token,
            parse_constant=_reject_constant,
        )
    except TrainingAuthorizationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeError) as exc:
        raise TrainingAuthorizationError("training authorization JSON is malformed") from exc
    return validate_training_authorization_terminal_wire(value)


__all__ = [
    "TRAINING_AUTHORIZATION_SCHEMA",
    "TRAINING_AUTHORIZATION_RECORD_DATE",
    "MAX_TRAINING_AUTHORIZATION_WIRE_BYTES",
    "M14_06_HUMAN_REVIEW_TERMINAL_FINGERPRINT",
    "FROZEN_TRAINING_AUTHORIZATION_TERMINAL_FINGERPRINT",
    "TrainingAuthorizationError",
    "TrainingDecisionDisposition",
    "TrainingClaimCap",
    "TrainingMethod",
    "TrainingReviewDomain",
    "TrainingReviewStatus",
    "TrainingMethodStatus",
    "TrainingAuthorizationDomainReceipt",
    "TrainingAuthorizationMethodReceipt",
    "TrainingAuthorizationTerminalRecord",
    "build_training_authorization_terminal_record",
    "validate_training_authorization_terminal_wire",
    "decode_training_authorization_terminal_json",
]
