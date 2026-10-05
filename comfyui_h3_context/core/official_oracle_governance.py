"""Offline governance and budget admission for explicitly authorized oracle work.

The contracts in this module are intentionally independent of the official transport. They
record a dated terms decision, require visible operator/privacy controls, and account for finite
local budgets before a later adapter is allowed to perform an operation. No credential, media,
network, clock, or provider SDK is accessed here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from urllib.parse import urlsplit

from .contracts import ProviderIdentity
from .errors import ContractValidationError, OfficialOracleGovernanceError
from .provider_policy import ProviderExecutionPolicy, ProviderPrivacyMode

OFFICIAL_ORACLE_GOVERNANCE_SCHEMA = "h3.context.ir.governance.v1"
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "path",
    "secret",
    "signed",
    "token",
    "url",
)
_MAX_SOURCE_REFERENCES = 16
_MAX_CALLS = 1_000_000
_MAX_SPEND_MINOR_UNITS = 10**12
_MAX_OUTPUT_TOKENS = 10**12
_MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60.0
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class OfficialOracleReviewStatus(str, Enum):
    """Status of a dated, source-attributed review for one oracle use boundary."""

    NOT_REVIEWED = "not_reviewed"
    APPROVED_CONTROLLED_RESEARCH = "approved_controlled_research"
    DENIED = "denied"
    DO_NOT_IMPLEMENT = "do_not_implement"


class OfficialOracleDecision(str, Enum):
    """Decision for each independently governed use category."""

    ALLOW = "allow"
    DENY = "deny"
    NOT_REVIEWED = "not_reviewed"


class OfficialOracleOperation(str, Enum):
    """Operation categories that may be admitted by a terms decision."""

    API_CALL = "api_call"
    OUTPUT_RETENTION = "output_retention"
    REDISTRIBUTION = "redistribution"
    BENCHMARKING = "benchmarking"
    PUBLICATION = "publication"
    AUTOMATED_PROBING = "automated_probing"
    TRAINING_DISTILLATION = "training_distillation"


class OfficialOracleMediaPrivacy(str, Enum):
    """Privacy classification for media that a future transport may transfer."""

    NO_MEDIA = "no_media"
    SYNTHETIC_ONLY = "synthetic_only"
    USER_CONSENTED = "user_consented"
    RESTRICTED_APPROVED = "restricted_approved"


class OfficialOracleRetentionPolicy(str, Enum):
    """Local retention/deletion disposition for oracle inputs and outputs."""

    NONE = "none"
    HASH_ONLY = "hash_only"
    DELETE_AFTER_CAPTURE = "delete_after_capture"
    RETAIN_RESTRICTED = "retain_restricted"


class OfficialOracleAbortCondition(str, Enum):
    """Terminal conditions that must stop a future transport/capture operation."""

    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    MODERATION = "moderation"
    UNSUPPORTED_MEDIA = "unsupported_media"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    TRANSPORT = "transport"
    MALFORMED_RESPONSE = "malformed_response"


class OfficialOracleSourceCategory(str, Enum):
    """Source categories that must be resolved before an approval can be used."""

    GENERAL_TERMS = "general_terms"
    PRODUCT_TERMS = "product_terms"
    PRIVACY_POLICY = "privacy_policy"
    API_LIFECYCLE = "api_lifecycle"
    DELETION = "deletion"
    PRICING = "pricing"
    ACCOUNT_ORDER = "account_order"
    SUPPLEMENTAL = "supplemental"


class OfficialOracleSourceApplicability(str, Enum):
    """Whether a source category applies to the reviewed account/use."""

    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNRESOLVED = "unresolved"


class OfficialOracleLaneStatus(str, Enum):
    """Downstream disposition for one live or evidence lane."""

    AUTHORIZED = "authorized"
    LIMITED = "limited"
    UNAVAILABLE_PROHIBITED = "unavailable_prohibited"
    EXHAUSTED_SUSPENDED = "exhausted_suspended"
    EXPIRED = "expired"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


class OfficialOracleExecutionScope(str, Enum):
    """Maximum execution scope granted by a lane disposition."""

    LIVE_AND_MOCKED = "live_and_mocked"
    MOCKED_ONLY = "mocked_only"
    RECORDED_ONLY = "recorded_only"
    OFFLINE_ONLY = "offline_only"
    NONE = "none"


class OfficialOracleExecutionMode(str, Enum):
    """Concrete mode requested by a downstream caller."""

    LIVE = "live"
    MOCKED = "mocked"
    RECORDED = "recorded"
    OFFLINE = "offline"


class OfficialOracleClaimCeiling(str, Enum):
    """Highest claim a lane can support; absence never becomes evidence."""

    ORACLE_EVIDENCE = "oracle_evidence"
    RECORDED_EVIDENCE = "recorded_evidence"
    STRUCTURAL_ONLY = "structural_only"
    NO_CLAIM = "no_claim"


_REQUIRED_SOURCE_CATEGORIES = frozenset(
    {
        OfficialOracleSourceCategory.GENERAL_TERMS,
        OfficialOracleSourceCategory.PRODUCT_TERMS,
        OfficialOracleSourceCategory.PRIVACY_POLICY,
        OfficialOracleSourceCategory.API_LIFECYCLE,
        OfficialOracleSourceCategory.DELETION,
        OfficialOracleSourceCategory.PRICING,
        OfficialOracleSourceCategory.ACCOUNT_ORDER,
        OfficialOracleSourceCategory.SUPPLEMENTAL,
    }
)
_REQUIRED_LANE_IDS = frozenset(
    {
        "official_live_oracle",
        "official_mocked_contract",
        "recorded_oracle_fixture",
        "benchmark",
        "publication",
        "training_distillation",
    }
)
_SCOPE_ALLOWED_MODES: dict[OfficialOracleExecutionScope, frozenset[OfficialOracleExecutionMode]] = {
    OfficialOracleExecutionScope.LIVE_AND_MOCKED: frozenset(
        {OfficialOracleExecutionMode.LIVE, OfficialOracleExecutionMode.MOCKED}
    ),
    OfficialOracleExecutionScope.MOCKED_ONLY: frozenset({OfficialOracleExecutionMode.MOCKED}),
    OfficialOracleExecutionScope.RECORDED_ONLY: frozenset({OfficialOracleExecutionMode.RECORDED}),
    OfficialOracleExecutionScope.OFFLINE_ONLY: frozenset({OfficialOracleExecutionMode.OFFLINE}),
    OfficialOracleExecutionScope.NONE: frozenset(),
}
_STATUS_DEFAULTS: dict[
    OfficialOracleLaneStatus,
    tuple[OfficialOracleExecutionScope, OfficialOracleClaimCeiling],
] = {
    OfficialOracleLaneStatus.AUTHORIZED: (
        OfficialOracleExecutionScope.LIVE_AND_MOCKED,
        OfficialOracleClaimCeiling.ORACLE_EVIDENCE,
    ),
    OfficialOracleLaneStatus.LIMITED: (
        OfficialOracleExecutionScope.MOCKED_ONLY,
        OfficialOracleClaimCeiling.STRUCTURAL_ONLY,
    ),
    OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED: (
        OfficialOracleExecutionScope.OFFLINE_ONLY,
        OfficialOracleClaimCeiling.NO_CLAIM,
    ),
    OfficialOracleLaneStatus.EXHAUSTED_SUSPENDED: (
        OfficialOracleExecutionScope.OFFLINE_ONLY,
        OfficialOracleClaimCeiling.NO_CLAIM,
    ),
    OfficialOracleLaneStatus.EXPIRED: (
        OfficialOracleExecutionScope.OFFLINE_ONLY,
        OfficialOracleClaimCeiling.NO_CLAIM,
    ),
    OfficialOracleLaneStatus.REAUTHORIZATION_REQUIRED: (
        OfficialOracleExecutionScope.OFFLINE_ONLY,
        OfficialOracleClaimCeiling.NO_CLAIM,
    ),
}


def _has_sensitive_identifier_segment(value: str) -> bool:
    segments = tuple(part for part in re.split(r"[_.:-]+", value.casefold()) if part)
    return any(marker in segments for marker in _SENSITIVE_MARKERS)


def _require_enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise ContractValidationError(f"{field_name} must be a {expected.__name__}")


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be a boolean")
    return value


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded identifier")
    if _has_sensitive_identifier_segment(value):
        raise ContractValidationError(f"{field_name} must not contain sensitive markers")
    return value


def _require_non_negative_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ContractValidationError(f"{field_name} must be a bounded non-negative integer")
    return value


def _require_positive_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise ContractValidationError(f"{field_name} must be a bounded positive integer")
    return value


def _require_clock(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{field_name} must be a finite non-negative number")
    result = float(value)
    if not isfinite(result) or result < 0:
        raise ContractValidationError(f"{field_name} must be a finite non-negative number")
    return result


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ContractValidationError(f"{field_name} must be a bounded UTC timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ContractValidationError(f"{field_name} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ContractValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: object, field_name: str) -> str:
    return _parse_timestamp(value, field_name).isoformat().replace("+00:00", "Z")


def _require_date(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be an ISO date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ContractValidationError(f"{field_name} must be a valid ISO date") from None
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _require_source_url(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ContractValidationError(f"{field_name} must be a bounded HTTPS URL")
    if any(ord(char) < 0x21 or ord(char) == 0x7F or char == "\\" for char in value):
        raise ContractValidationError(f"{field_name} contains unsafe URL characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError:
        raise ContractValidationError(f"{field_name} is malformed") from None
    if parsed.scheme.casefold() != "https" or hostname is None:
        raise ContractValidationError(f"{field_name} must use HTTPS with a public host")
    if username is not None or password is not None or parsed.query or parsed.fragment:
        raise ContractValidationError(
            f"{field_name} must not contain credentials, query, or fragment"
        )
    return value


@dataclass(frozen=True, slots=True)
class OfficialOracleSourceRecord:
    """One source-ledger row pinned to a safe URL, revision, dates, and content hash."""

    source_id: str
    category: OfficialOracleSourceCategory
    url: str
    revision: str
    effective_date: str | None
    retrieved_at: str
    sha256: str
    authority: str
    jurisdiction: str
    applicability: OfficialOracleSourceApplicability

    def __post_init__(self) -> None:
        _require_identifier(self.source_id, "source_id")
        _require_enum(self.category, OfficialOracleSourceCategory, "source category")
        _require_source_url(self.url, "source URL")
        _require_identifier(self.revision, "source revision")
        if self.effective_date is not None:
            _require_date(self.effective_date, "effective_date")
        _require_date(self.retrieved_at, "retrieved_at")
        _require_sha256(self.sha256, "source sha256")
        _require_identifier(self.authority, "source authority")
        _require_identifier(self.jurisdiction, "source jurisdiction")
        _require_enum(self.applicability, OfficialOracleSourceApplicability, "source applicability")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "category": self.category.value,
            "url": self.url,
            "revision": self.revision,
            "effective_date": self.effective_date,
            "retrieved_at": self.retrieved_at,
            "sha256": self.sha256,
            "authority": self.authority,
            "jurisdiction": self.jurisdiction,
            "applicability": self.applicability.value,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleSourceLedger:
    """Ignored, review-owned source catalog used to resolve runtime source IDs."""

    ledger_id: str
    ledger_revision: str
    sources: tuple[OfficialOracleSourceRecord, ...]
    reviewer_reference: str
    approver_reference: str
    jurisdiction: str
    unresolved_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.ledger_id, "source ledger id")
        _require_identifier(self.ledger_revision, "source ledger revision")
        _require_identifier(self.reviewer_reference, "source ledger reviewer")
        _require_identifier(self.approver_reference, "source ledger approver")
        _require_identifier(self.jurisdiction, "source ledger jurisdiction")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ContractValidationError("source ledger requires at least one source")
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ContractValidationError("source ledger source IDs must be unique")
        if not all(isinstance(source, OfficialOracleSourceRecord) for source in self.sources):
            raise ContractValidationError("source ledger contains an invalid source")
        if not isinstance(self.unresolved_questions, tuple) or len(self.unresolved_questions) > 64:
            raise ContractValidationError("unresolved_questions must be a bounded tuple")
        for question in self.unresolved_questions:
            _require_identifier(question, "unresolved question")

    @property
    def fingerprint(self) -> str:
        payload = {
            "ledger_id": self.ledger_id,
            "ledger_revision": self.ledger_revision,
            "sources": [source.to_public_dict() for source in self.sources],
            "reviewer_reference": self.reviewer_reference,
            "approver_reference": self.approver_reference,
            "jurisdiction": self.jurisdiction,
            "unresolved_questions": list(self.unresolved_questions),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def resolve(self, source_ids: tuple[str, ...]) -> tuple[OfficialOracleSourceRecord, ...]:
        """Resolve all IDs and fail closed on missing, stale, or unresolved source metadata."""

        if not isinstance(source_ids, tuple) or not source_ids:
            raise OfficialOracleGovernanceError("source_unresolved", "terms contain no source IDs")
        by_id = {source.source_id: source for source in self.sources}
        try:
            resolved = tuple(by_id[source_id] for source_id in source_ids)
        except KeyError:
            raise OfficialOracleGovernanceError(
                "source_unresolved", "terms reference an unknown source-ledger ID"
            ) from None
        if self.unresolved_questions:
            raise OfficialOracleGovernanceError(
                "source_review_incomplete", "source-ledger review has unresolved questions"
            )
        categories = {source.category for source in resolved}
        if not _REQUIRED_SOURCE_CATEGORIES.issubset(categories):
            raise OfficialOracleGovernanceError(
                "source_category_missing", "source ledger does not cover every required category"
            )
        if any(
            source.applicability is OfficialOracleSourceApplicability.UNRESOLVED
            or source.effective_date is None
            for source in self.sources
            if source.category in _REQUIRED_SOURCE_CATEGORIES
        ):
            raise OfficialOracleGovernanceError(
                "source_review_incomplete", "a required source has unresolved applicability/date"
            )
        return resolved

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": f"{OFFICIAL_ORACLE_GOVERNANCE_SCHEMA}-sources",
            "ledger_id": self.ledger_id,
            "ledger_revision": self.ledger_revision,
            "sources": [source.to_public_dict() for source in self.sources],
            "reviewer_reference": self.reviewer_reference,
            "approver_reference": self.approver_reference,
            "jurisdiction": self.jurisdiction,
            "unresolved_questions": list(self.unresolved_questions),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleLaneDisposition:
    """Versioned downstream permissions and claim ceiling for one lane."""

    lane_id: str
    status: OfficialOracleLaneStatus
    execution_scope: OfficialOracleExecutionScope
    claim_ceiling: OfficialOracleClaimCeiling
    retention_policy: OfficialOracleRetentionPolicy
    reason_reference: str

    def __post_init__(self) -> None:
        _require_identifier(self.lane_id, "lane_id")
        _require_enum(self.status, OfficialOracleLaneStatus, "lane status")
        _require_enum(self.execution_scope, OfficialOracleExecutionScope, "execution scope")
        _require_enum(self.claim_ceiling, OfficialOracleClaimCeiling, "claim ceiling")
        _require_enum(self.retention_policy, OfficialOracleRetentionPolicy, "lane retention")
        _require_identifier(self.reason_reference, "lane reason reference")
        expected_scope, expected_ceiling = _STATUS_DEFAULTS[self.status]
        if self.status is OfficialOracleLaneStatus.AUTHORIZED:
            if (
                self.execution_scope is not expected_scope
                or self.claim_ceiling is not expected_ceiling
            ):
                raise ContractValidationError("authorized lane permissions are inconsistent")
        elif self.status is OfficialOracleLaneStatus.LIMITED:
            if self.execution_scope not in {
                OfficialOracleExecutionScope.MOCKED_ONLY,
                OfficialOracleExecutionScope.RECORDED_ONLY,
            } or self.claim_ceiling not in {
                OfficialOracleClaimCeiling.STRUCTURAL_ONLY,
                OfficialOracleClaimCeiling.RECORDED_EVIDENCE,
            }:
                raise ContractValidationError("limited lane permissions are inconsistent")
        elif (
            self.execution_scope
            not in {
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleExecutionScope.NONE,
            }
            or self.claim_ceiling is not OfficialOracleClaimCeiling.NO_CLAIM
        ):
            raise ContractValidationError("negative lane permissions are inconsistent")

    def allows(self, mode: OfficialOracleExecutionMode) -> bool:
        _require_enum(mode, OfficialOracleExecutionMode, "execution mode")
        return mode in _SCOPE_ALLOWED_MODES[self.execution_scope]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "lane_id": self.lane_id,
            "status": self.status.value,
            "execution_scope": self.execution_scope.value,
            "claim_ceiling": self.claim_ceiling.value,
            "retention_policy": self.retention_policy.value,
            "reason_reference": self.reason_reference,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleTermsDecision:
    """A complete, dated decision for every restricted official-oracle use category."""

    review_id: str
    source_ledger_id: str
    terms_revision: str
    reviewed_at: str
    reviewer_reference: str
    status: OfficialOracleReviewStatus
    source_references: tuple[str, ...]
    lane_dispositions: tuple[OfficialOracleLaneDisposition, ...]
    api_use: OfficialOracleDecision
    output_retention: OfficialOracleDecision
    redistribution: OfficialOracleDecision
    benchmarking: OfficialOracleDecision
    publication: OfficialOracleDecision
    automated_probing: OfficialOracleDecision
    training_distillation: OfficialOracleDecision
    valid_until: str | None = None
    provider: ProviderIdentity = ProviderIdentity.OFFICIAL_MINIMAX

    def __post_init__(self) -> None:
        _require_identifier(self.review_id, "review_id")
        _require_identifier(self.source_ledger_id, "source_ledger_id")
        _require_identifier(self.terms_revision, "terms_revision")
        _require_identifier(self.reviewer_reference, "reviewer_reference")
        reviewed_at = _canonical_timestamp(self.reviewed_at, "reviewed_at")
        object.__setattr__(self, "reviewed_at", reviewed_at)
        if self.valid_until is not None:
            valid_until = _canonical_timestamp(self.valid_until, "valid_until")
            if _parse_timestamp(valid_until, "valid_until") <= _parse_timestamp(
                reviewed_at, "reviewed_at"
            ):
                raise ContractValidationError("valid_until must be later than reviewed_at")
            object.__setattr__(self, "valid_until", valid_until)
        if self.provider is not ProviderIdentity.OFFICIAL_MINIMAX:
            raise ContractValidationError("official oracle terms provider must be official_minimax")
        _require_enum(self.status, OfficialOracleReviewStatus, "terms status")
        if (
            not isinstance(self.source_references, tuple)
            or not 1 <= len(self.source_references) <= _MAX_SOURCE_REFERENCES
            or len(set(self.source_references)) != len(self.source_references)
        ):
            raise ContractValidationError("source_references must be a bounded unique tuple")
        for reference in self.source_references:
            _require_identifier(reference, "source_reference")
        if not isinstance(self.lane_dispositions, tuple) or not self.lane_dispositions:
            raise ContractValidationError("lane_dispositions must be a non-empty tuple")
        if len({lane.lane_id for lane in self.lane_dispositions}) != len(self.lane_dispositions):
            raise ContractValidationError("lane disposition IDs must be unique")
        if not all(
            isinstance(lane, OfficialOracleLaneDisposition) for lane in self.lane_dispositions
        ):
            raise ContractValidationError("lane_dispositions contains an invalid value")
        lane_ids = {lane.lane_id for lane in self.lane_dispositions}
        if not _REQUIRED_LANE_IDS.issubset(lane_ids):
            raise ContractValidationError("lane dispositions must cover every governed lane")
        for value, field_name in (
            (self.api_use, "api_use"),
            (self.output_retention, "output_retention"),
            (self.redistribution, "redistribution"),
            (self.benchmarking, "benchmarking"),
            (self.publication, "publication"),
            (self.automated_probing, "automated_probing"),
            (self.training_distillation, "training_distillation"),
        ):
            _require_enum(value, OfficialOracleDecision, field_name)

    def decision_for(self, operation: OfficialOracleOperation) -> OfficialOracleDecision:
        _require_enum(operation, OfficialOracleOperation, "operation")
        return {
            OfficialOracleOperation.API_CALL: self.api_use,
            OfficialOracleOperation.OUTPUT_RETENTION: self.output_retention,
            OfficialOracleOperation.REDISTRIBUTION: self.redistribution,
            OfficialOracleOperation.BENCHMARKING: self.benchmarking,
            OfficialOracleOperation.PUBLICATION: self.publication,
            OfficialOracleOperation.AUTOMATED_PROBING: self.automated_probing,
            OfficialOracleOperation.TRAINING_DISTILLATION: self.training_distillation,
        }[operation]

    def disposition_for(self, lane_id: str) -> OfficialOracleLaneDisposition:
        _require_identifier(lane_id, "lane_id")
        for disposition in self.lane_dispositions:
            if disposition.lane_id == lane_id:
                return disposition
        raise OfficialOracleGovernanceError(
            "lane_unresolved", "terms contain no disposition for the requested lane"
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_GOVERNANCE_SCHEMA,
            "review_id": self.review_id,
            "source_ledger_id": self.source_ledger_id,
            "terms_revision": self.terms_revision,
            "reviewed_at": self.reviewed_at,
            "valid_until": self.valid_until,
            "reviewer_reference": self.reviewer_reference,
            "provider": self.provider.value,
            "status": self.status.value,
            "source_references": list(self.source_references),
            "lane_dispositions": [
                disposition.to_public_dict() for disposition in self.lane_dispositions
            ],
            "api_use": self.api_use.value,
            "output_retention": self.output_retention.value,
            "redistribution": self.redistribution.value,
            "benchmarking": self.benchmarking.value,
            "publication": self.publication.value,
            "automated_probing": self.automated_probing.value,
            "training_distillation": self.training_distillation.value,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleBudgetPolicy:
    """Finite local call, rate, spend, and output-token ceilings."""

    max_calls: int
    max_requests_per_window: int
    window_seconds: float
    max_spend_minor_units: int
    currency: str
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.max_calls, "max_calls", _MAX_CALLS)
        _require_positive_int(self.max_requests_per_window, "max_requests_per_window", _MAX_CALLS)
        window = _require_clock(self.window_seconds, "window_seconds")
        if window <= 0 or window > _MAX_WINDOW_SECONDS:
            raise ContractValidationError("window_seconds must be bounded and positive")
        object.__setattr__(self, "window_seconds", window)
        _require_non_negative_int(
            self.max_spend_minor_units, "max_spend_minor_units", _MAX_SPEND_MINOR_UNITS
        )
        _require_identifier(self.currency, "currency")
        if self.max_output_tokens is not None:
            _require_positive_int(self.max_output_tokens, "max_output_tokens", _MAX_OUTPUT_TOKENS)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "max_calls": self.max_calls,
            "max_requests_per_window": self.max_requests_per_window,
            "window_seconds": self.window_seconds,
            "max_spend_minor_units": self.max_spend_minor_units,
            "currency": self.currency,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleGovernancePolicy:
    """Operator-visible privacy, retention, deletion, abort, and budget controls."""

    policy_revision: str
    operator_consent: bool = False
    consent_reference: str | None = None
    consent_terms_revision: str | None = None
    consent_policy_revision: str | None = None
    media_privacy: OfficialOracleMediaPrivacy = OfficialOracleMediaPrivacy.NO_MEDIA
    media_upload_consent: bool = False
    retention_policy: OfficialOracleRetentionPolicy = OfficialOracleRetentionPolicy.NONE
    deletion_reference: str | None = None
    abort_on: frozenset[OfficialOracleAbortCondition] = frozenset()
    budget: OfficialOracleBudgetPolicy = field(
        default_factory=lambda: OfficialOracleBudgetPolicy(
            max_calls=1,
            max_requests_per_window=1,
            window_seconds=60.0,
            max_spend_minor_units=0,
            currency="usd",
        )
    )

    def __post_init__(self) -> None:
        _require_identifier(self.policy_revision, "policy_revision")
        _require_bool(self.operator_consent, "operator_consent")
        for value, field_name in (
            (self.consent_reference, "consent_reference"),
            (self.consent_terms_revision, "consent_terms_revision"),
            (self.consent_policy_revision, "consent_policy_revision"),
        ):
            if value is not None:
                _require_identifier(value, field_name)
        _require_enum(self.media_privacy, OfficialOracleMediaPrivacy, "media_privacy")
        _require_bool(self.media_upload_consent, "media_upload_consent")
        _require_enum(self.retention_policy, OfficialOracleRetentionPolicy, "retention_policy")
        if self.deletion_reference is not None:
            _require_identifier(self.deletion_reference, "deletion_reference")
        if not isinstance(self.abort_on, frozenset) or not all(
            isinstance(condition, OfficialOracleAbortCondition) for condition in self.abort_on
        ):
            raise ContractValidationError("abort_on must be a set of abort conditions")
        if not isinstance(self.budget, OfficialOracleBudgetPolicy):
            raise ContractValidationError("budget must be an OfficialOracleBudgetPolicy")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "policy_revision": self.policy_revision,
            "operator_consent": self.operator_consent,
            "consent_reference": self.consent_reference,
            "consent_terms_revision": self.consent_terms_revision,
            "consent_policy_revision": self.consent_policy_revision,
            "media_privacy": self.media_privacy.value,
            "media_upload_consent": self.media_upload_consent,
            "retention_policy": self.retention_policy.value,
            "deletion_reference": self.deletion_reference,
            "abort_on": sorted(condition.value for condition in self.abort_on),
            "budget": self.budget.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleUsage:
    """Redacted local usage counters carried between immutable gate states."""

    calls: int = 0
    window_calls: int = 0
    spend_minor_units: int = 0
    output_tokens: int = 0
    window_started_at: float | None = None

    def __post_init__(self) -> None:
        _require_non_negative_int(self.calls, "usage.calls", _MAX_CALLS)
        _require_non_negative_int(self.window_calls, "usage.window_calls", _MAX_CALLS)
        _require_non_negative_int(
            self.spend_minor_units, "usage.spend_minor_units", _MAX_SPEND_MINOR_UNITS
        )
        _require_non_negative_int(self.output_tokens, "usage.output_tokens", _MAX_OUTPUT_TOKENS)
        if self.window_started_at is not None:
            _require_clock(self.window_started_at, "usage.window_started_at")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "window_calls": self.window_calls,
            "spend_minor_units": self.spend_minor_units,
            "output_tokens": self.output_tokens,
            "window_started_at": self.window_started_at,
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleAdmission:
    """Successful local admission and the next immutable gate state."""

    operation_id: str
    lane_id: str
    execution_mode: OfficialOracleExecutionMode
    operation: OfficialOracleOperation
    provider: ProviderIdentity
    usage: OfficialOracleUsage
    next_gate: OfficialOracleExecutionGate

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, "operation_id")
        _require_identifier(self.lane_id, "lane_id")
        _require_enum(self.execution_mode, OfficialOracleExecutionMode, "execution mode")
        _require_enum(self.operation, OfficialOracleOperation, "operation")
        if self.provider is not ProviderIdentity.OFFICIAL_MINIMAX:
            raise ContractValidationError("admission provider must be official_minimax")
        if not isinstance(self.usage, OfficialOracleUsage):
            raise ContractValidationError("admission usage is invalid")
        if not isinstance(self.next_gate, OfficialOracleExecutionGate):
            raise ContractValidationError("admission next_gate is invalid")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_GOVERNANCE_SCHEMA,
            "operation_id": self.operation_id,
            "lane_id": self.lane_id,
            "execution_mode": self.execution_mode.value,
            "operation": self.operation.value,
            "provider": self.provider.value,
            "usage": self.usage.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class OfficialOracleExecutionGate:
    """Fail-closed, offline admission gate consumed by a future official transport."""

    terms: OfficialOracleTermsDecision
    policy: OfficialOracleGovernancePolicy
    source_ledger: OfficialOracleSourceLedger
    provider_policy: ProviderExecutionPolicy
    usage: OfficialOracleUsage = field(default_factory=OfficialOracleUsage)
    aborted: bool = False
    abort_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.terms, OfficialOracleTermsDecision):
            raise ContractValidationError("terms must be an OfficialOracleTermsDecision")
        if not isinstance(self.policy, OfficialOracleGovernancePolicy):
            raise ContractValidationError("policy must be an OfficialOracleGovernancePolicy")
        if not isinstance(self.source_ledger, OfficialOracleSourceLedger):
            raise ContractValidationError("source_ledger must be an OfficialOracleSourceLedger")
        if not isinstance(self.provider_policy, ProviderExecutionPolicy):
            raise ContractValidationError("provider_policy must be a ProviderExecutionPolicy")
        if not isinstance(self.usage, OfficialOracleUsage):
            raise ContractValidationError("usage must be an OfficialOracleUsage")
        _require_bool(self.aborted, "aborted")
        if self.abort_reason is not None:
            _require_identifier(self.abort_reason, "abort_reason")
        if self.aborted != (self.abort_reason is not None):
            raise ContractValidationError("aborted and abort_reason must agree")

    def abort(self, reason: str) -> OfficialOracleExecutionGate:
        """Return a terminal gate; no implicit reset or retry is possible after abort."""

        _require_identifier(reason, "abort_reason")
        return replace(self, aborted=True, abort_reason=reason)

    def abort_for(self, condition: OfficialOracleAbortCondition) -> OfficialOracleExecutionGate:
        _require_enum(condition, OfficialOracleAbortCondition, "abort condition")
        return self.abort(condition.value) if condition in self.policy.abort_on else self

    def admit(
        self,
        *,
        lane_id: str,
        execution_mode: OfficialOracleExecutionMode,
        operation: OfficialOracleOperation,
        now: str,
        rate_clock: float,
        operation_id: str = "official-oracle",
        media_present: bool = False,
        media_class: OfficialOracleMediaPrivacy | None = None,
        estimated_spend_minor_units: int | None = None,
        estimated_output_tokens: int = 0,
    ) -> OfficialOracleAdmission:
        """Reserve one bounded operation without performing provider or media I/O."""

        _require_enum(operation, OfficialOracleOperation, "operation")
        _require_identifier(lane_id, "lane_id")
        _require_enum(execution_mode, OfficialOracleExecutionMode, "execution_mode")
        _require_identifier(operation_id, "operation_id")
        _require_bool(media_present, "media_present")
        if media_class is not None:
            _require_enum(media_class, OfficialOracleMediaPrivacy, "media_class")
        if not media_present and media_class is not None:
            raise ContractValidationError("media_class cannot be supplied without media")
        now_value = _parse_timestamp(now, "now")
        rate_value = _require_clock(rate_clock, "rate_clock")
        output_tokens = _require_non_negative_int(
            estimated_output_tokens, "estimated_output_tokens", _MAX_OUTPUT_TOKENS
        )
        if self.aborted:
            raise OfficialOracleGovernanceError("aborted", "oracle operation was aborted")
        if self.terms.status is not OfficialOracleReviewStatus.APPROVED_CONTROLLED_RESEARCH:
            raise OfficialOracleGovernanceError(
                "terms_not_approved", "official-oracle terms are not approved"
            )
        if self.source_ledger.ledger_id != self.terms.source_ledger_id:
            raise OfficialOracleGovernanceError(
                "source_ledger_mismatch", "source ledger does not match the terms decision"
            )
        self.source_ledger.resolve(self.terms.source_references)
        reviewed_at = _parse_timestamp(self.terms.reviewed_at, "reviewed_at")
        if now_value < reviewed_at:
            raise OfficialOracleGovernanceError(
                "terms_not_active", "official-oracle terms review is not active yet"
            )
        if self.terms.valid_until is not None and now_value >= _parse_timestamp(
            self.terms.valid_until, "valid_until"
        ):
            raise OfficialOracleGovernanceError(
                "terms_expired", "official-oracle terms review has expired"
            )
        if self.terms.decision_for(operation) is not OfficialOracleDecision.ALLOW:
            raise OfficialOracleGovernanceError(
                "operation_not_permitted", "the reviewed terms do not permit this operation"
            )
        if (
            self.provider_policy.provider is not ProviderIdentity.OFFICIAL_MINIMAX
            or self.provider_policy.privacy_mode is not ProviderPrivacyMode.EXPLICIT_REMOTE
            or self.provider_policy.offline
            or not self.provider_policy.network_allowed
            or self.provider_policy.credential_reference is None
        ):
            raise OfficialOracleGovernanceError(
                "provider_policy_rejected", "official provider policy is not explicitly admitted"
            )
        disposition = self.terms.disposition_for(lane_id)
        if not disposition.allows(execution_mode):
            raise OfficialOracleGovernanceError(
                "lane_execution_not_permitted",
                "the lane disposition does not permit the requested execution mode",
            )
        if estimated_spend_minor_units is None:
            if execution_mode is OfficialOracleExecutionMode.LIVE:
                raise OfficialOracleGovernanceError(
                    "spend_estimate_required",
                    "live admission requires a trusted spend estimate",
                )
            spend = 0
        else:
            spend = _require_non_negative_int(
                estimated_spend_minor_units, "estimated_spend_minor_units", _MAX_SPEND_MINOR_UNITS
            )
        if not self.policy.operator_consent:
            raise OfficialOracleGovernanceError(
                "operator_consent_required", "explicit operator consent is required"
            )
        if self.policy.consent_reference is None:
            raise OfficialOracleGovernanceError(
                "consent_binding_required", "consent must be bound to a reviewed revision"
            )
        if (
            self.policy.consent_terms_revision != self.terms.terms_revision
            or self.policy.consent_policy_revision != self.policy.policy_revision
        ):
            raise OfficialOracleGovernanceError(
                "consent_revision_mismatch",
                "consent is bound to a different terms or policy revision",
            )
        if media_present:
            if media_class is None or self.policy.media_privacy is not media_class:
                raise OfficialOracleGovernanceError(
                    "media_privacy_mismatch", "media is not admitted by the privacy policy"
                )
            if not self.policy.media_upload_consent:
                raise OfficialOracleGovernanceError(
                    "media_consent_required", "explicit media-upload consent is required"
                )
            if not self.provider_policy.upload_consent:
                raise OfficialOracleGovernanceError(
                    "provider_media_consent_required",
                    "provider policy does not grant media-upload consent",
                )
            if self.policy.retention_policy is OfficialOracleRetentionPolicy.NONE:
                raise OfficialOracleGovernanceError(
                    "deletion_policy_required", "media execution requires a deletion policy"
                )
        if self.policy.retention_policy is not OfficialOracleRetentionPolicy.NONE and (
            self.policy.deletion_reference is None
        ):
            raise OfficialOracleGovernanceError(
                "deletion_policy_required", "retention requires a deletion reference"
            )
        if (
            execution_mode is not OfficialOracleExecutionMode.OFFLINE
            and self.policy.retention_policy is not disposition.retention_policy
        ):
            raise OfficialOracleGovernanceError(
                "retention_disposition_mismatch",
                "policy retention does not match the lane disposition",
            )
        if self.usage.calls >= self.policy.budget.max_calls:
            raise OfficialOracleGovernanceError("call_quota", "oracle call budget is exhausted")
        if self.usage.spend_minor_units + spend > self.policy.budget.max_spend_minor_units:
            raise OfficialOracleGovernanceError("spend_quota", "oracle spend budget is exhausted")
        if (
            self.policy.budget.max_output_tokens is not None
            and self.usage.output_tokens + output_tokens > self.policy.budget.max_output_tokens
        ):
            raise OfficialOracleGovernanceError(
                "output_token_quota", "oracle output-token budget is exhausted"
            )
        window_started = self.usage.window_started_at
        window_calls = self.usage.window_calls
        if (
            window_started is None
            or rate_value - window_started >= self.policy.budget.window_seconds
        ):
            window_started = rate_value
            window_calls = 0
        elif rate_value < window_started:
            raise OfficialOracleGovernanceError(
                "rate_clock_regression", "rate clock moved backwards"
            )
        if window_calls >= self.policy.budget.max_requests_per_window:
            raise OfficialOracleGovernanceError("rate_limit", "oracle rate budget is exhausted")
        next_usage = OfficialOracleUsage(
            calls=self.usage.calls + 1,
            window_calls=window_calls + 1,
            spend_minor_units=self.usage.spend_minor_units + spend,
            output_tokens=self.usage.output_tokens + output_tokens,
            window_started_at=window_started,
        )
        next_gate = replace(self, usage=next_usage)
        return OfficialOracleAdmission(
            operation_id=operation_id,
            lane_id=lane_id,
            execution_mode=execution_mode,
            operation=operation,
            provider=self.terms.provider,
            usage=next_usage,
            next_gate=next_gate,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_ORACLE_GOVERNANCE_SCHEMA,
            "provider": self.terms.provider.value,
            "provider_policy": {
                "provider": self.provider_policy.provider.value,
                "privacy_mode": self.provider_policy.privacy_mode.value,
                "offline": self.provider_policy.offline,
                "network_allowed": self.provider_policy.network_allowed,
                "upload_consent": self.provider_policy.upload_consent,
            },
            "source_ledger_id": self.source_ledger.ledger_id,
            "source_ledger_fingerprint": self.source_ledger.fingerprint,
            "terms": self.terms.to_public_dict(),
            "policy": self.policy.to_public_dict(),
            "usage": self.usage.to_public_dict(),
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
        }


def build_official_oracle_execution_gate(
    terms: OfficialOracleTermsDecision,
    policy: OfficialOracleGovernancePolicy,
    source_ledger: OfficialOracleSourceLedger,
    provider_policy: ProviderExecutionPolicy,
) -> OfficialOracleExecutionGate:
    """Construct a fresh offline gate for one explicit reviewed execution scope."""

    return OfficialOracleExecutionGate(
        terms=terms,
        policy=policy,
        source_ledger=source_ledger,
        provider_policy=provider_policy,
    )


__all__ = [
    "OFFICIAL_ORACLE_GOVERNANCE_SCHEMA",
    "OfficialOracleAbortCondition",
    "OfficialOracleAdmission",
    "OfficialOracleBudgetPolicy",
    "OfficialOracleDecision",
    "OfficialOracleExecutionGate",
    "OfficialOracleExecutionMode",
    "OfficialOracleGovernancePolicy",
    "OfficialOracleClaimCeiling",
    "OfficialOracleLaneDisposition",
    "OfficialOracleLaneStatus",
    "OfficialOracleMediaPrivacy",
    "OfficialOracleOperation",
    "OfficialOracleReviewStatus",
    "OfficialOracleRetentionPolicy",
    "OfficialOracleSourceApplicability",
    "OfficialOracleSourceCategory",
    "OfficialOracleSourceLedger",
    "OfficialOracleSourceRecord",
    "OfficialOracleTermsDecision",
    "OfficialOracleUsage",
    "build_official_oracle_execution_gate",
]
