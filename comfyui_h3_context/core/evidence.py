"""Immutable evidence, provenance, and uncertainty values for the H3 context pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .constraints import TimePoint
from .contracts import EvidenceLevel, ProviderIdentity, ValidationSeverity
from .errors import ContractValidationError, EvidenceConflictError

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_TEXT_LENGTH = 65_536
_MAX_SHORT_TEXT_LENGTH = 4096


class EvidenceSourceKind(str, Enum):
    """Closed source families; media and provider payloads remain untrusted inputs."""

    USER_INPUT = "user_input"
    MEDIA_ASSET = "media_asset"
    PROVIDER_OUTPUT = "provider_output"
    SYSTEM_DERIVED = "system_derived"


class EvidenceOrigin(str, Enum):
    """How a claim entered the canonical context."""

    USER_DECLARED = "user_declared"
    OBSERVED = "observed"
    ASSISTED_PROPOSAL = "assisted_proposal"
    DERIVED = "derived"


class SupportStatus(str, Enum):
    """Whether a claim is supported, uncertain, unsupported, or rejected."""

    SUPPORTED = "supported"
    UNCERTAIN = "uncertain"
    UNSUPPORTED = "unsupported"
    REJECTED = "rejected"


class UncertaintyKind(str, Enum):
    """Machine-readable uncertainty reasons retained beside a claim."""

    MISSING_SOURCE = "missing_source"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    UNSUPPORTED = "unsupported"
    UNINTELLIGIBLE = "unintelligible"
    TRUNCATED = "truncated"
    STALE = "stale"


def _require_enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ContractValidationError(f"{field} must be a {expected.__name__}")


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a bounded identifier")
    return value


def _require_text(value: object, field: str, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field} must be a non-empty bounded string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ContractValidationError(f"{field} contains an unsafe wire code point")
    return value


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_text(value, field, maximum)


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """Stable source locator with optional semantic span and typed time range."""

    kind: EvidenceSourceKind
    source_id: str
    asset_id: str | None = None
    span: str | None = None
    start: TimePoint | None = None
    end: TimePoint | None = None

    def __post_init__(self) -> None:
        _require_enum(self.kind, EvidenceSourceKind, "evidence source kind")
        _require_identifier(self.source_id, "source_id")
        _optional_text(self.asset_id, "asset_id", 128)
        if self.asset_id is not None:
            _require_identifier(self.asset_id, "asset_id")
        _optional_text(self.span, "source span", _MAX_SHORT_TEXT_LENGTH)
        if self.start is not None and not isinstance(self.start, TimePoint):
            raise ContractValidationError("source start must be a TimePoint or None")
        if self.end is not None and not isinstance(self.end, TimePoint):
            raise ContractValidationError("source end must be a TimePoint or None")
        if self.end is not None and self.start is None:
            raise ContractValidationError("source end requires source start")
        if (
            self.start is not None
            and self.end is not None
            and self.end.seconds < self.start.seconds
        ):
            raise ContractValidationError("source end must not precede source start")
        if self.kind is EvidenceSourceKind.MEDIA_ASSET and self.asset_id is None:
            raise ContractValidationError("media evidence requires an asset_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "source_id": self.source_id,
            "asset_id": self.asset_id,
            "span": self.span,
            "start": None if self.start is None else self.start.to_wire(),
            "end": None if self.end is None else self.end.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class Provenance:
    """Provider and source revision attached to one evidence record."""

    source: EvidenceSource
    provider: ProviderIdentity
    evidence_level: EvidenceLevel
    provider_version: str | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise ContractValidationError("provenance source must be an EvidenceSource")
        _require_enum(self.provider, ProviderIdentity, "provenance provider")
        _require_enum(self.evidence_level, EvidenceLevel, "provenance evidence level")
        _optional_text(self.provider_version, "provider_version", 256)
        _optional_text(self.source_revision, "source_revision", 256)
        if self.provider is not ProviderIdentity.MANUAL and (
            self.provider_version is None or self.source_revision is None
        ):
            raise ContractValidationError(
                "non-manual provenance requires provider_version and source_revision"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "source": self.source.to_wire(),
            "provider": self.provider.value,
            "evidence_level": self.evidence_level.value,
            "provider_version": self.provider_version,
            "source_revision": self.source_revision,
        }


@dataclass(frozen=True, slots=True)
class Uncertainty:
    """A retained reason why a claim may be incomplete, ambiguous, or unsupported."""

    kind: UncertaintyKind
    detail: str
    severity: ValidationSeverity = ValidationSeverity.WARNING

    def __post_init__(self) -> None:
        _require_enum(self.kind, UncertaintyKind, "uncertainty kind")
        _require_text(self.detail, "uncertainty detail", _MAX_SHORT_TEXT_LENGTH)
        _require_enum(self.severity, ValidationSeverity, "uncertainty severity")

    def to_wire(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "detail": self.detail,
            "severity": self.severity.value,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One claim with explicit origin, support state, provenance, and uncertainty."""

    evidence_id: str
    claim: str
    origin: EvidenceOrigin
    support: SupportStatus
    provenance: Provenance
    confidence: Decimal | None = None
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.evidence_id, "evidence_id")
        _require_text(self.claim, "evidence claim")
        _require_enum(self.origin, EvidenceOrigin, "evidence origin")
        _require_enum(self.support, SupportStatus, "evidence support")
        if not isinstance(self.provenance, Provenance):
            raise ContractValidationError("evidence provenance must be a Provenance")
        if self.confidence is not None and (
            not isinstance(self.confidence, Decimal)
            or not self.confidence.is_finite()
            or not Decimal("0") <= self.confidence <= Decimal("1")
        ):
            raise ContractValidationError("confidence must be a Decimal between 0 and 1")
        if not isinstance(self.uncertainties, tuple) or not all(
            isinstance(value, Uncertainty) for value in self.uncertainties
        ):
            raise ContractValidationError("uncertainties must be a tuple of Uncertainty values")

        source_kind = self.provenance.source.kind
        if self.origin is EvidenceOrigin.USER_DECLARED:
            if source_kind is not EvidenceSourceKind.USER_INPUT:
                raise ContractValidationError("user declarations require a USER_INPUT source")
            if self.provenance.provider is not ProviderIdentity.MANUAL:
                raise ContractValidationError("user declarations require the manual provider")
        elif self.origin is EvidenceOrigin.OBSERVED:
            if self.support in {SupportStatus.UNSUPPORTED, SupportStatus.REJECTED}:
                raise ContractValidationError("observations cannot be unsupported or rejected")
            if source_kind not in {
                EvidenceSourceKind.MEDIA_ASSET,
                EvidenceSourceKind.PROVIDER_OUTPUT,
            }:
                raise ContractValidationError(
                    "observations require a media or provider-output source"
                )
        elif self.origin is EvidenceOrigin.ASSISTED_PROPOSAL:
            if self.provenance.provider is ProviderIdentity.MANUAL:
                raise ContractValidationError("assisted proposals require a non-manual provider")
            if source_kind not in {
                EvidenceSourceKind.PROVIDER_OUTPUT,
                EvidenceSourceKind.SYSTEM_DERIVED,
                EvidenceSourceKind.MEDIA_ASSET,
            }:
                raise ContractValidationError(
                    "assisted proposals require a provider, system, or media source"
                )
        elif (
            self.origin is EvidenceOrigin.DERIVED
            and source_kind is not EvidenceSourceKind.SYSTEM_DERIVED
        ):
            raise ContractValidationError("derived evidence requires a SYSTEM_DERIVED source")

        if self.support is SupportStatus.UNCERTAIN and not self.uncertainties:
            raise ContractValidationError("uncertain evidence requires an uncertainty entry")
        if self.support in {SupportStatus.UNSUPPORTED, SupportStatus.REJECTED} and self.origin in {
            EvidenceOrigin.USER_DECLARED,
            EvidenceOrigin.OBSERVED,
            EvidenceOrigin.DERIVED,
        }:
            raise ContractValidationError("unsupported or rejected evidence must remain a proposal")

    def to_wire(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "claim": self.claim,
            "origin": self.origin.value,
            "support": self.support.value,
            "provenance": self.provenance.to_wire(),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainties": [value.to_wire() for value in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class EvidenceSet:
    """Immutable insertion-ordered evidence records."""

    records: tuple[EvidenceRecord, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple) or not all(
            isinstance(value, EvidenceRecord) for value in self.records
        ):
            raise ContractValidationError("records must be a tuple of EvidenceRecord values")
        identifiers = [value.evidence_id for value in self.records]
        if len(identifiers) != len(set(identifiers)):
            raise ContractValidationError("evidence IDs must be unique")

    @classmethod
    def empty(cls) -> EvidenceSet:
        return cls()

    def get(self, evidence_id: str) -> EvidenceRecord:
        for record in self.records:
            if record.evidence_id == evidence_id:
                return record
        raise ContractValidationError(f"unknown evidence ID: {evidence_id}")

    def to_wire(self) -> dict[str, object]:
        return {"records": [record.to_wire() for record in self.records]}


def merge_evidence(*sets: EvidenceSet) -> EvidenceSet:
    """Merge evidence in insertion order and reject same-ID conflicts."""

    records: list[EvidenceRecord] = []
    by_id: dict[str, EvidenceRecord] = {}
    for evidence_set in sets:
        if not isinstance(evidence_set, EvidenceSet):
            raise ContractValidationError("merge inputs must be EvidenceSet values")
        for record in evidence_set.records:
            existing = by_id.get(record.evidence_id)
            if existing is None:
                by_id[record.evidence_id] = record
                records.append(record)
            elif existing != record:
                raise EvidenceConflictError(
                    f"evidence ID {record.evidence_id} has conflicting values"
                )
    return EvidenceSet(tuple(records))


__all__ = [
    "EvidenceOrigin",
    "EvidenceRecord",
    "EvidenceSet",
    "EvidenceSource",
    "EvidenceSourceKind",
    "Provenance",
    "SupportStatus",
    "Uncertainty",
    "UncertaintyKind",
    "merge_evidence",
]
