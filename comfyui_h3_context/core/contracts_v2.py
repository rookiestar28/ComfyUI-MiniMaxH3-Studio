"""Portable, provider-free M10 contract-v2 semantic values.

The v2 layer closes the semantic relationships between source-owned observations, entities,
tracks, events, timelines, and reports without importing ComfyUI, media runtimes, SDKs, or model
packages. Runtime handles and execution receipts are deliberately separate from canonical values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from .errors import ContractV2Error

if TYPE_CHECKING:
    from .context_reporting import ContextReport

CONTRACT_V2_SCHEMA = "h3.context.contract.v2"
CONTRACT_V2_VERSION = "2.0"
MAX_V2_ITEMS = 256
MAX_V2_TEXT = 4096
MAX_V2_DIAGNOSTICS = 256
MAX_V2_ATTRIBUTES = 32
MAX_V2_UNCERTAINTIES = 32
MAX_V2_DURATION_MS = 86_400_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE_MARKERS = (
    "http://",
    "https://",
    "file://",
    "authorization",
    "bearer ",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token=",
    "sig=",
    "x-amz-",
    "-----begin",
)


class ContractV2Status(str, Enum):
    """Explicit report lifecycle state; partial data never becomes complete implicitly."""

    DRAFT = "draft"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class PortableSourceKind(str, Enum):
    """Source provenance class without storing a dereferenceable locator."""

    PUBLIC_DEREFERENCEABLE = "public_dereferenceable"
    USER_LOCAL = "user_local"
    USER_HASH_ONLY = "user_hash_only"
    RUNTIME_ONLY = "runtime_only"


class ObservationKind(str, Enum):
    """Closed cross-modal observation families."""

    SUBJECT = "subject"
    OBJECT = "object"
    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"
    STYLE = "style"
    TEXT = "text"
    AUDIO = "audio"
    OTHER = "other"


class EntityKind(str, Enum):
    """Stable identities that may group observations across time and modalities."""

    SUBJECT = "subject"
    OBJECT = "object"
    SCENE = "scene"
    SPEAKER = "speaker"
    OTHER = "other"


class EventKind(str, Enum):
    """Events link observations and entities to an explicit temporal span."""

    SCENE = "scene"
    ACTION = "action"
    SPEECH = "speech"
    EDIT = "edit"
    AUDIO = "audio"
    OTHER = "other"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ContractV2Error(f"{field} must be a bounded identifier")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise ContractV2Error(f"{field} must be a lower-case bounded code")
    return value


def _text(value: object, field: str, maximum: int = MAX_V2_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractV2Error(f"{field} must be a bounded non-empty string")
    if any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ContractV2Error(f"{field} contains an unsafe wire code point")
    lowered = value.casefold()
    if (
        any(marker in lowered for marker in _SENSITIVE_MARKERS)
        or value.startswith("/")
        or "\\" in value
    ):
        raise ContractV2Error(f"{field} contains a locator, secret, or runtime value")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> Enum:
    if not isinstance(value, expected):
        raise ContractV2Error(f"{field} must be a {expected.__name__}")
    return value


def _ids(values: object, field: str, maximum: int = MAX_V2_ITEMS) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ContractV2Error(f"{field} must be a bounded tuple")
    result = tuple(_identifier(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise ContractV2Error(f"{field} must not contain duplicate identifiers")
    return result


def _codes(values: object, field: str, maximum: int = MAX_V2_DIAGNOSTICS) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ContractV2Error(f"{field} must be a bounded tuple")
    result = tuple(_code(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise ContractV2Error(f"{field} must not contain duplicate codes")
    return result


def _attributes(values: object, field: str = "attributes") -> tuple[tuple[str, str], ...]:
    if not isinstance(values, tuple) or len(values) > MAX_V2_ATTRIBUTES:
        raise ContractV2Error(f"{field} must be a bounded tuple")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ContractV2Error(f"{field} items must be key/value tuples")
        key = _code(item[0], f"{field} key")
        value = _text(item[1], f"{field} value", 1024)
        if key in seen:
            raise ContractV2Error(f"{field} keys must be unique")
        seen.add(key)
        result.append((key, value))
    return tuple(result)


def _confidence(value: object, field: str = "confidence") -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
        raise ContractV2Error(f"{field} must be a finite Decimal between 0 and 1")
    return value


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ContractV2Error(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


@dataclass(frozen=True, slots=True, order=True)
class TemporalSpan:
    """Integer millisecond span that is independent of a decoder's frame clock."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        for value, field in ((self.start_ms, "start_ms"), (self.end_ms, "end_ms")):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_V2_DURATION_MS
            ):
                raise ContractV2Error(f"{field} must be a bounded non-negative integer")
        if self.end_ms < self.start_ms:
            raise ContractV2Error("temporal span end must not precede start")

    def to_wire(self) -> dict[str, int]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms}


@dataclass(frozen=True, slots=True)
class Observation:
    """One source-owned observation with explicit provenance, uncertainty, and temporal scope."""

    observation_id: str
    asset_id: str
    kind: ObservationKind
    label: str
    source_kind: PortableSourceKind
    source_fingerprint: str
    evidence_ids: tuple[str, ...] = ()
    span: TemporalSpan | None = None
    confidence: Decimal | None = None
    uncertainty_codes: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "observation_id")
        _identifier(self.asset_id, "asset_id")
        _enum(self.kind, ObservationKind, "observation kind")
        _text(self.label, "observation label")
        _enum(self.source_kind, PortableSourceKind, "observation source_kind")
        _fingerprint(self.source_fingerprint, "source_fingerprint")
        _ids(self.evidence_ids, "observation evidence_ids")
        if self.span is not None and not isinstance(self.span, TemporalSpan):
            raise ContractV2Error("observation span must be a TemporalSpan or None")
        _confidence(self.confidence)
        _codes(self.uncertainty_codes, "uncertainty_codes", MAX_V2_UNCERTAINTIES)
        _ids(self.entity_ids, "observation entity_ids")
        _attributes(self.attributes, "observation attributes")

    def to_wire(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "label": self.label,
            "source_kind": self.source_kind.value,
            "source_fingerprint": self.source_fingerprint,
            "evidence_ids": list(self.evidence_ids),
            "span": None if self.span is None else self.span.to_wire(),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainty_codes": list(self.uncertainty_codes),
            "entity_ids": list(self.entity_ids),
            "attributes": [list(item) for item in self.attributes],
        }


@dataclass(frozen=True, slots=True)
class Entity:
    """Cross-observation identity; it does not rewrite source observations."""

    entity_id: str
    kind: EntityKind
    observation_ids: tuple[str, ...] = ()
    confidence: Decimal | None = None
    uncertainty_codes: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.entity_id, "entity_id")
        _enum(self.kind, EntityKind, "entity kind")
        _ids(self.observation_ids, "entity observation_ids")
        _confidence(self.confidence)
        _codes(self.uncertainty_codes, "entity uncertainty_codes", MAX_V2_UNCERTAINTIES)
        _attributes(self.attributes, "entity attributes")

    def to_wire(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind.value,
            "observation_ids": list(self.observation_ids),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainty_codes": list(self.uncertainty_codes),
            "attributes": [list(item) for item in self.attributes],
        }


@dataclass(frozen=True, slots=True)
class Track:
    """A bounded temporal continuity claim over one entity and its observations."""

    track_id: str
    entity_id: str
    observation_ids: tuple[str, ...]
    spans: tuple[TemporalSpan, ...]
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        _identifier(self.track_id, "track_id")
        _identifier(self.entity_id, "track entity_id")
        _ids(self.observation_ids, "track observation_ids")
        if not isinstance(self.spans, tuple) or len(self.spans) > MAX_V2_ITEMS:
            raise ContractV2Error("track spans must be a bounded tuple")
        if not all(isinstance(item, TemporalSpan) for item in self.spans):
            raise ContractV2Error("track spans must contain TemporalSpan values")
        _confidence(self.confidence)

    def to_wire(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "entity_id": self.entity_id,
            "observation_ids": list(self.observation_ids),
            "spans": [item.to_wire() for item in self.spans],
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
        }


@dataclass(frozen=True, slots=True)
class Event:
    """An ordered event linking one primary entity to source observations."""

    event_id: str
    kind: EventKind
    ordinal: int
    entity_id: str | None
    observation_ids: tuple[str, ...]
    span: TemporalSpan
    confidence: Decimal | None = None
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        _enum(self.kind, EventKind, "event kind")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal <= 0:
            raise ContractV2Error("event ordinal must be a positive integer")
        if self.entity_id is not None:
            _identifier(self.entity_id, "event entity_id")
        _ids(self.observation_ids, "event observation_ids")
        if not isinstance(self.span, TemporalSpan):
            raise ContractV2Error("event span must be a TemporalSpan")
        _confidence(self.confidence)
        _attributes(self.attributes, "event attributes")

    def to_wire(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "ordinal": self.ordinal,
            "entity_id": self.entity_id,
            "observation_ids": list(self.observation_ids),
            "span": self.span.to_wire(),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "attributes": [list(item) for item in self.attributes],
        }


@dataclass(frozen=True, slots=True)
class Timeline:
    """Ordered multimodal events with a bounded declared duration."""

    timeline_id: str
    events: tuple[Event, ...]
    duration_ms: int
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.timeline_id, "timeline_id")
        if not isinstance(self.events, tuple) or len(self.events) > MAX_V2_ITEMS:
            raise ContractV2Error("timeline events must be a bounded tuple")
        if not all(isinstance(item, Event) for item in self.events):
            raise ContractV2Error("timeline events must contain Event values")
        event_ids = tuple(item.event_id for item in self.events)
        if len(event_ids) != len(set(event_ids)):
            raise ContractV2Error("timeline event IDs must be unique")
        ordinals = tuple(item.ordinal for item in self.events)
        if ordinals and ordinals != tuple(range(1, len(ordinals) + 1)):
            raise ContractV2Error("timeline event ordinals must be contiguous and ordered")
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or not 0 <= self.duration_ms <= MAX_V2_DURATION_MS
        ):
            raise ContractV2Error("timeline duration_ms must be bounded")
        if any(item.span.end_ms > self.duration_ms for item in self.events):
            raise ContractV2Error("timeline event exceeds declared duration")
        _codes(self.diagnostics, "timeline diagnostics")

    def to_wire(self) -> dict[str, object]:
        return {
            "timeline_id": self.timeline_id,
            "events": [item.to_wire() for item in self.events],
            "duration_ms": self.duration_ms,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class RuntimeReceipt:
    """Non-canonical, redacted adapter metadata kept outside portable report semantics."""

    receipt_id: str
    adapter_id: str
    status: str
    metadata: tuple[tuple[str, str], ...] = ()
    output_fingerprint: str | None = None
    redacted: bool = True

    def __post_init__(self) -> None:
        _identifier(self.receipt_id, "receipt_id")
        _identifier(self.adapter_id, "adapter_id")
        _code(self.status, "receipt status")
        _attributes(self.metadata, "receipt metadata")
        if self.output_fingerprint is not None:
            _fingerprint(self.output_fingerprint, "output_fingerprint")
        if self.redacted is not True:
            raise ContractV2Error("runtime receipts must be marked redacted")

    def to_wire(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "adapter_id": self.adapter_id,
            "status": self.status,
            "metadata": [list(item) for item in self.metadata],
            "output_fingerprint": self.output_fingerprint,
            "redacted": self.redacted,
        }


def _wire_fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextReportV2:
    """Portable report joining observations, identity, events, and explicit diagnostics."""

    report_id: str
    revision: int
    status: ContractV2Status
    asset_ids: tuple[str, ...] = ()
    observations: tuple[Observation, ...] = ()
    entities: tuple[Entity, ...] = ()
    tracks: tuple[Track, ...] = ()
    timeline: Timeline | None = None
    diagnostics: tuple[str, ...] = ()
    migrated_from: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.report_id, "report_id")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision <= 0
        ):
            raise ContractV2Error("report revision must be a positive integer")
        _enum(self.status, ContractV2Status, "report status")
        _ids(self.asset_ids, "report asset_ids")
        if not isinstance(self.observations, tuple) or len(self.observations) > MAX_V2_ITEMS:
            raise ContractV2Error("report observations must be a bounded tuple")
        if not all(isinstance(item, Observation) for item in self.observations):
            raise ContractV2Error("report observations contain an invalid value")
        if not isinstance(self.entities, tuple) or len(self.entities) > MAX_V2_ITEMS:
            raise ContractV2Error("report entities must be a bounded tuple")
        if not all(isinstance(item, Entity) for item in self.entities):
            raise ContractV2Error("report entities contain an invalid value")
        if not isinstance(self.tracks, tuple) or len(self.tracks) > MAX_V2_ITEMS:
            raise ContractV2Error("report tracks must be a bounded tuple")
        if not all(isinstance(item, Track) for item in self.tracks):
            raise ContractV2Error("report tracks contain an invalid value")
        if self.timeline is not None and not isinstance(self.timeline, Timeline):
            raise ContractV2Error("report timeline must be a Timeline or None")
        _codes(self.diagnostics, "report diagnostics")
        if self.migrated_from is not None:
            _code(self.migrated_from.replace(".", "_"), "migrated_from")

        asset_set = set(self.asset_ids)
        observation_ids = {item.observation_id for item in self.observations}
        entity_ids = {item.entity_id for item in self.entities}
        track_ids = {item.track_id for item in self.tracks}
        if len(observation_ids) != len(self.observations):
            raise ContractV2Error("report observation IDs must be unique")
        if len(entity_ids) != len(self.entities):
            raise ContractV2Error("report entity IDs must be unique")
        if len(track_ids) != len(self.tracks):
            raise ContractV2Error("report track IDs must be unique")
        for observation in self.observations:
            if observation.asset_id not in asset_set:
                raise ContractV2Error("observation references an unknown asset")
            if not set(observation.entity_ids).issubset(entity_ids):
                raise ContractV2Error("observation references an unknown entity")
        for entity in self.entities:
            if not set(entity.observation_ids).issubset(observation_ids):
                raise ContractV2Error("entity references an unknown observation")
        for track in self.tracks:
            if track.entity_id not in entity_ids:
                raise ContractV2Error("track references an unknown entity")
            if not set(track.observation_ids).issubset(observation_ids):
                raise ContractV2Error("track references an unknown observation")
        if self.timeline is not None:
            for event in self.timeline.events:
                if event.entity_id is not None and event.entity_id not in entity_ids:
                    raise ContractV2Error("event references an unknown entity")
                if not set(event.observation_ids).issubset(observation_ids):
                    raise ContractV2Error("event references an unknown observation")

    @property
    def claim_ceiling(self) -> str:
        return {
            ContractV2Status.COMPLETE: "complete",
            ContractV2Status.PARTIAL: "partial",
            ContractV2Status.DRAFT: "no_claim",
            ContractV2Status.FAILED: "no_claim",
        }[self.status]

    @property
    def fingerprint(self) -> str:
        return _wire_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": CONTRACT_V2_SCHEMA,
            "version": CONTRACT_V2_VERSION,
            "report_id": self.report_id,
            "revision": self.revision,
            "status": self.status.value,
            "claim_ceiling": self.claim_ceiling,
            "asset_ids": list(self.asset_ids),
            "observations": [item.to_wire() for item in self.observations],
            "entities": [item.to_wire() for item in self.entities],
            "tracks": [item.to_wire() for item in self.tracks],
            "timeline": None if self.timeline is None else self.timeline.to_wire(),
            "diagnostics": list(self.diagnostics),
            "migrated_from": self.migrated_from,
            "fingerprint": self.fingerprint_without_self(),
        }

    def fingerprint_without_self(self) -> str:
        payload = {
            "schema": CONTRACT_V2_SCHEMA,
            "version": CONTRACT_V2_VERSION,
            "report_id": self.report_id,
            "revision": self.revision,
            "status": self.status.value,
            "asset_ids": list(self.asset_ids),
            "observations": [item.to_wire() for item in self.observations],
            "entities": [item.to_wire() for item in self.entities],
            "tracks": [item.to_wire() for item in self.tracks],
            "timeline": None if self.timeline is None else self.timeline.to_wire(),
            "diagnostics": list(self.diagnostics),
            "migrated_from": self.migrated_from,
        }
        return _wire_fingerprint(payload)


def migrate_v1_report(report: ContextReport) -> ContextReportV2:
    """Convert an existing v1 report without inventing observations or runtime provenance."""

    report_id = getattr(report, "report_id", None)
    schema_version = getattr(report, "schema_version", None)
    if not isinstance(report_id, str) or schema_version is None:
        raise ContractV2Error("v1 report is missing its identity")
    if str(schema_version) != "1.0":
        raise ContractV2Error("only h3 context report v1.0 can be migrated")
    request = getattr(report, "request", None)
    assets = getattr(request, "assets", ()) if request is not None else ()
    asset_ids: list[str] = []
    for asset in assets:
        asset_id = getattr(asset, "asset_id", None)
        if not isinstance(asset_id, str):
            raise ContractV2Error("v1 asset identity is not portable")
        asset_ids.append(_identifier(asset_id, "v1 asset_id"))
    has_errors = bool(getattr(report, "has_errors", False))
    status = ContractV2Status.FAILED if has_errors else ContractV2Status.PARTIAL
    diagnostics = (
        "v1_migration_requires_observations",
        "v1_migration_requires_timeline",
    )
    return ContextReportV2(
        report_id=report_id,
        revision=1,
        status=status,
        asset_ids=tuple(asset_ids),
        diagnostics=diagnostics,
        migrated_from="h3.context.report.v1",
    )


__all__ = [
    "CONTRACT_V2_SCHEMA",
    "CONTRACT_V2_VERSION",
    "ContractV2Status",
    "PortableSourceKind",
    "ObservationKind",
    "EntityKind",
    "EventKind",
    "TemporalSpan",
    "Observation",
    "Entity",
    "Track",
    "Event",
    "Timeline",
    "RuntimeReceipt",
    "ContextReportV2",
    "migrate_v1_report",
]
