"""Deterministic source-owned multimodal evidence graph contracts.

The graph composes caller-supplied evidence and stable IDs.  It does not infer identity, open
media, execute provider output, or select a winner among alternatives and conflicts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TypeVar, cast

from .canonical import canonical_fingerprint
from .contracts import MediaKind, ValidationSeverity
from .errors import UnifiedEvidenceGraphError
from .evidence import EvidenceRecord, UncertaintyKind

UNIFIED_EVIDENCE_GRAPH_SCHEMA = "h3.unified.evidence_graph.v1"
MAX_GRAPH_OBSERVATIONS = 256
MAX_GRAPH_ENTITIES = 256
MAX_GRAPH_TRACKS = 256
MAX_GRAPH_EVENTS = 256
MAX_GRAPH_EVIDENCE = 512
MAX_GRAPH_ALTERNATIVES = 256
MAX_GRAPH_CONFLICTS = 256
MAX_GRAPH_UNCERTAINTIES = 1024
MAX_GRAPH_IDS = 256
MAX_GRAPH_TEXT = 4096
MAX_GRAPH_OUTPUT_BYTES = 65_536
MAX_GRAPH_TIME_MS = 86_400_000

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = (
    "http://",
    "https://",
    "file://",
    "/",
    "\\",
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
)
_TEXT_SENSITIVE = (
    "http://",
    "https://",
    "file://",
    "/mnt/",
    "c:\\",
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
)
_T = TypeVar("_T")


class GraphStatus(str, Enum):
    """Graph-level claim ceiling derived from explicit node dispositions."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    EMPTY = "empty"


class GraphSupport(str, Enum):
    """Support state retained on each observation or event."""

    SUPPORTED = "supported"
    UNCERTAIN = "uncertain"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"


class GraphResolution(str, Enum):
    """Resolution state for identity and event relationships."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    CONFLICTING = "conflicting"


class GraphObservationKind(str, Enum):
    SUBJECT = "subject"
    OBJECT = "object"
    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"
    STYLE = "style"
    TEXT = "text"
    AUDIO = "audio"
    SPEECH = "speech"
    OTHER = "other"


class GraphEntityKind(str, Enum):
    SUBJECT = "subject"
    OBJECT = "object"
    SCENE = "scene"
    SPEAKER = "speaker"
    OTHER = "other"


class GraphEventKind(str, Enum):
    SCENE = "scene"
    ACTION = "action"
    SPEECH = "speech"
    EDIT = "edit"
    AUDIO = "audio"
    TEXT = "text"
    OTHER = "other"


class GraphNodeKind(str, Enum):
    OBSERVATION = "observation"
    ENTITY = "entity"
    TRACK = "track"
    EVENT = "event"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise UnifiedEvidenceGraphError(f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _TEXT_SENSITIVE):
        raise UnifiedEvidenceGraphError(f"{field} contains sensitive or locator material")
    return value


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise UnifiedEvidenceGraphError(f"{field} must be a SHA-256 fingerprint")
    return value


def _text(value: object, field: str, maximum: int = MAX_GRAPH_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise UnifiedEvidenceGraphError(f"{field} must be bounded non-empty text")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise UnifiedEvidenceGraphError(f"{field} contains sensitive or locator material")
    if any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise UnifiedEvidenceGraphError(f"{field} contains an unsafe wire code point")
    return value


def _ids(
    values: object, field: str, maximum: int = MAX_GRAPH_IDS, *, required: bool = False
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise UnifiedEvidenceGraphError(f"{field} must be a bounded tuple")
    if required and not values:
        raise UnifiedEvidenceGraphError(f"{field} must not be empty")
    result = tuple(_identifier(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise UnifiedEvidenceGraphError(f"{field} must not contain duplicates")
    return result


def _confidence(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal("0") <= value <= Decimal("1")
    ):
        raise UnifiedEvidenceGraphError(f"{field} must be a finite Decimal between 0 and 1")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise UnifiedEvidenceGraphError(f"{field} must be a {expected.__name__}")


@dataclass(frozen=True, slots=True)
class GraphSourceSpan:
    """Canonical bounded source span; upstream rational PTS remains in its source receipt."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    start_ms: int
    end_ms: int
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "source span asset_id")
        _identifier(self.source_id, "source span source_id")
        _fingerprint(self.source_fingerprint, "source span source_fingerprint")
        for value, field in (
            (self.start_ms, "source span start_ms"),
            (self.end_ms, "source span end_ms"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_GRAPH_TIME_MS
            ):
                raise UnifiedEvidenceGraphError(f"{field} is outside the bounded time range")
        if self.end_ms < self.start_ms:
            raise UnifiedEvidenceGraphError("source span end_ms must not precede start_ms")
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }


@dataclass(frozen=True, slots=True)
class GraphUncertainty:
    """Typed uncertainty entry referenced by graph nodes."""

    uncertainty_id: str
    kind: UncertaintyKind
    detail: str
    severity: ValidationSeverity = ValidationSeverity.WARNING
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.uncertainty_id, "uncertainty_id")
        _enum(self.kind, UncertaintyKind, "uncertainty kind")
        _text(self.detail, "uncertainty detail")
        _enum(self.severity, ValidationSeverity, "uncertainty severity")
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "uncertainty_id": self.uncertainty_id,
            "kind": self.kind.value,
            "detail": self.detail,
            "severity": self.severity.value,
        }


@dataclass(frozen=True, slots=True)
class GraphObservation:
    """One source-owned visual, audio, or text observation."""

    observation_id: str
    asset_id: str
    modality: MediaKind
    kind: GraphObservationKind
    label: str
    source_span: GraphSourceSpan | None
    evidence_ids: tuple[str, ...] = ()
    support: GraphSupport = GraphSupport.SUPPORTED
    confidence: Decimal | None = None
    uncertainty_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    track_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "observation_id")
        _identifier(self.asset_id, "observation asset_id")
        _enum(self.modality, MediaKind, "observation modality")
        _enum(self.kind, GraphObservationKind, "observation kind")
        _text(self.label, "observation label")
        if self.source_span is not None:
            if not isinstance(self.source_span, GraphSourceSpan):
                raise UnifiedEvidenceGraphError("observation source_span must be GraphSourceSpan")
            if self.source_span.asset_id != self.asset_id:
                raise UnifiedEvidenceGraphError("observation source span asset ownership differs")
        _ids(self.evidence_ids, "observation evidence_ids")
        _enum(self.support, GraphSupport, "observation support")
        _confidence(self.confidence, "observation confidence")
        _ids(self.uncertainty_ids, "observation uncertainty_ids")
        _ids(self.entity_ids, "observation entity_ids")
        _ids(self.track_ids, "observation track_ids")
        _ids(self.event_ids, "observation event_ids")
        if self.support is GraphSupport.SUPPORTED and self.source_span is None:
            raise UnifiedEvidenceGraphError("supported observation requires a source span")
        if (
            self.support in {GraphSupport.UNCERTAIN, GraphSupport.MISSING, GraphSupport.UNSUPPORTED}
            and not self.uncertainty_ids
        ):
            raise UnifiedEvidenceGraphError("non-supported observation requires uncertainty IDs")
        if (
            self.support in {GraphSupport.MISSING, GraphSupport.UNSUPPORTED}
            and self.confidence is not None
        ):
            raise UnifiedEvidenceGraphError(
                "missing/unsupported observation cannot carry confidence"
            )
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "observation_id": self.observation_id,
            "asset_id": self.asset_id,
            "modality": self.modality.value,
            "kind": self.kind.value,
            "label": self.label,
            "source_span": None if self.source_span is None else self.source_span.to_wire(),
            "evidence_ids": list(self.evidence_ids),
            "support": self.support.value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainty_ids": list(self.uncertainty_ids),
            "entity_ids": list(self.entity_ids),
            "track_ids": list(self.track_ids),
            "event_ids": list(self.event_ids),
        }


@dataclass(frozen=True, slots=True)
class GraphEntity:
    """Explicit identity grouping; candidate hypotheses remain separate."""

    entity_id: str
    kind: GraphEntityKind
    observation_ids: tuple[str, ...] = ()
    track_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    resolution: GraphResolution = GraphResolution.RESOLVED
    candidate_ids: tuple[str, ...] = ()
    label: str | None = None
    confidence: Decimal | None = None
    uncertainty_ids: tuple[str, ...] = ()
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.entity_id, "entity_id")
        _enum(self.kind, GraphEntityKind, "entity kind")
        _ids(self.observation_ids, "entity observation_ids")
        _ids(self.track_ids, "entity track_ids")
        _ids(self.evidence_ids, "entity evidence_ids")
        _enum(self.resolution, GraphResolution, "entity resolution")
        _ids(self.candidate_ids, "entity candidate_ids")
        if self.label is not None:
            _text(self.label, "entity label")
        _confidence(self.confidence, "entity confidence")
        _ids(self.uncertainty_ids, "entity uncertainty_ids")
        if self.resolution is GraphResolution.AMBIGUOUS and len(self.candidate_ids) < 2:
            raise UnifiedEvidenceGraphError("ambiguous entity requires at least two candidates")
        if self.resolution is GraphResolution.UNRESOLVED and self.candidate_ids:
            raise UnifiedEvidenceGraphError("unresolved entity cannot carry candidates")
        if self.resolution is GraphResolution.CONFLICTING and len(self.candidate_ids) < 2:
            raise UnifiedEvidenceGraphError("conflicting entity requires at least two candidates")
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "entity_id": self.entity_id,
            "kind": self.kind.value,
            "observation_ids": list(self.observation_ids),
            "track_ids": list(self.track_ids),
            "evidence_ids": list(self.evidence_ids),
            "resolution": self.resolution.value,
            "candidate_ids": list(self.candidate_ids),
            "label": self.label,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainty_ids": list(self.uncertainty_ids),
        }


@dataclass(frozen=True, slots=True)
class GraphTrack:
    """Explicit temporal continuity claim over observations and optionally an entity."""

    track_id: str
    observation_ids: tuple[str, ...]
    entity_id: str | None = None
    spans: tuple[GraphSourceSpan, ...] = ()
    resolution: GraphResolution = GraphResolution.RESOLVED
    confidence: Decimal | None = None
    uncertainty_ids: tuple[str, ...] = ()
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.track_id, "track_id")
        _ids(self.observation_ids, "track observation_ids", required=True)
        if self.entity_id is not None:
            _identifier(self.entity_id, "track entity_id")
        if not isinstance(self.spans, tuple) or len(self.spans) > MAX_GRAPH_IDS:
            raise UnifiedEvidenceGraphError("track spans must be bounded")
        if not all(isinstance(item, GraphSourceSpan) for item in self.spans):
            raise UnifiedEvidenceGraphError("track spans contain an invalid value")
        _enum(self.resolution, GraphResolution, "track resolution")
        _confidence(self.confidence, "track confidence")
        _ids(self.uncertainty_ids, "track uncertainty_ids")
        if self.resolution is GraphResolution.RESOLVED and not self.spans:
            raise UnifiedEvidenceGraphError("resolved track requires source spans")
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "track_id": self.track_id,
            "observation_ids": list(self.observation_ids),
            "entity_id": self.entity_id,
            "spans": [item.to_wire() for item in self.spans],
            "resolution": self.resolution.value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainty_ids": list(self.uncertainty_ids),
        }


@dataclass(frozen=True, slots=True)
class GraphEvent:
    """Event linking explicit observations/entities to an optional source span."""

    event_id: str
    kind: GraphEventKind
    observation_ids: tuple[str, ...]
    entity_ids: tuple[str, ...] = ()
    span: GraphSourceSpan | None = None
    support: GraphSupport = GraphSupport.SUPPORTED
    resolution: GraphResolution = GraphResolution.RESOLVED
    evidence_ids: tuple[str, ...] = ()
    confidence: Decimal | None = None
    uncertainty_ids: tuple[str, ...] = ()
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        _enum(self.kind, GraphEventKind, "event kind")
        _ids(self.observation_ids, "event observation_ids", required=True)
        _ids(self.entity_ids, "event entity_ids")
        if self.span is not None and not isinstance(self.span, GraphSourceSpan):
            raise UnifiedEvidenceGraphError("event span must be GraphSourceSpan or None")
        _enum(self.support, GraphSupport, "event support")
        _enum(self.resolution, GraphResolution, "event resolution")
        _ids(self.evidence_ids, "event evidence_ids")
        _confidence(self.confidence, "event confidence")
        _ids(self.uncertainty_ids, "event uncertainty_ids")
        if self.support is GraphSupport.SUPPORTED and self.span is None:
            raise UnifiedEvidenceGraphError("supported event requires a source span")
        if self.support is not GraphSupport.SUPPORTED and not self.uncertainty_ids:
            raise UnifiedEvidenceGraphError("non-supported event requires uncertainty IDs")
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "kind": self.kind.value,
            "observation_ids": list(self.observation_ids),
            "entity_ids": list(self.entity_ids),
            "span": None if self.span is None else self.span.to_wire(),
            "support": self.support.value,
            "resolution": self.resolution.value,
            "evidence_ids": list(self.evidence_ids),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainty_ids": list(self.uncertainty_ids),
        }


@dataclass(frozen=True, slots=True)
class GraphAlternative:
    """Competing explicit candidates for one graph node."""

    alternative_id: str
    target_kind: GraphNodeKind
    target_id: str
    candidate_ids: tuple[str, ...]
    reason: str
    evidence_ids: tuple[str, ...] = ()
    uncertainty_ids: tuple[str, ...] = ()
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.alternative_id, "alternative_id")
        _enum(self.target_kind, GraphNodeKind, "alternative target_kind")
        _identifier(self.target_id, "alternative target_id")
        _ids(self.candidate_ids, "alternative candidate_ids", required=True)
        if len(self.candidate_ids) < 2:
            raise UnifiedEvidenceGraphError("alternative requires at least two candidates")
        _text(self.reason, "alternative reason")
        _ids(self.evidence_ids, "alternative evidence_ids")
        _ids(self.uncertainty_ids, "alternative uncertainty_ids")
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "alternative_id": self.alternative_id,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "candidate_ids": list(self.candidate_ids),
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "uncertainty_ids": list(self.uncertainty_ids),
        }


@dataclass(frozen=True, slots=True)
class GraphConflict:
    """Explicit disagreement retaining every conflicting target."""

    conflict_id: str
    target_kind: GraphNodeKind
    target_ids: tuple[str, ...]
    reason: str
    severity: ValidationSeverity = ValidationSeverity.ERROR
    evidence_ids: tuple[str, ...] = ()
    uncertainty_ids: tuple[str, ...] = ()
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.conflict_id, "conflict_id")
        _enum(self.target_kind, GraphNodeKind, "conflict target_kind")
        _ids(self.target_ids, "conflict target_ids", required=True)
        if len(self.target_ids) < 2:
            raise UnifiedEvidenceGraphError("conflict requires at least two targets")
        _text(self.reason, "conflict reason")
        _enum(self.severity, ValidationSeverity, "conflict severity")
        _ids(self.evidence_ids, "conflict evidence_ids")
        _ids(self.uncertainty_ids, "conflict uncertainty_ids")
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "conflict_id": self.conflict_id,
            "target_kind": self.target_kind.value,
            "target_ids": list(self.target_ids),
            "reason": self.reason,
            "severity": self.severity.value,
            "evidence_ids": list(self.evidence_ids),
            "uncertainty_ids": list(self.uncertainty_ids),
        }


def _sorted_observations(values: tuple[GraphObservation, ...]) -> tuple[GraphObservation, ...]:
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item.asset_id,
                -1 if item.source_span is None else item.source_span.start_ms,
                item.observation_id,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class UnifiedEvidenceGraph:
    """Inspectable deterministic graph and its explicit claim ceiling."""

    status: GraphStatus
    observations: tuple[GraphObservation, ...] = ()
    entities: tuple[GraphEntity, ...] = ()
    tracks: tuple[GraphTrack, ...] = ()
    events: tuple[GraphEvent, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    uncertainties: tuple[GraphUncertainty, ...] = ()
    alternatives: tuple[GraphAlternative, ...] = ()
    conflicts: tuple[GraphConflict, ...] = ()
    diagnostics: tuple[str, ...] = ()
    schema: str = UNIFIED_EVIDENCE_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.status, GraphStatus, "graph status")
        if self.schema != UNIFIED_EVIDENCE_GRAPH_SCHEMA:
            raise UnifiedEvidenceGraphError("unsupported unified evidence graph schema")
        self._validate_collection(
            self.observations,
            MAX_GRAPH_OBSERVATIONS,
            GraphObservation,
            "observations",
            "observation_id",
        )
        self._validate_collection(
            self.entities, MAX_GRAPH_ENTITIES, GraphEntity, "entities", "entity_id"
        )
        self._validate_collection(self.tracks, MAX_GRAPH_TRACKS, GraphTrack, "tracks", "track_id")
        self._validate_collection(self.events, MAX_GRAPH_EVENTS, GraphEvent, "events", "event_id")
        self._validate_collection(
            self.evidence, MAX_GRAPH_EVIDENCE, EvidenceRecord, "evidence", "evidence_id"
        )
        self._validate_collection(
            self.uncertainties,
            MAX_GRAPH_UNCERTAINTIES,
            GraphUncertainty,
            "uncertainties",
            "uncertainty_id",
        )
        self._validate_collection(
            self.alternatives,
            MAX_GRAPH_ALTERNATIVES,
            GraphAlternative,
            "alternatives",
            "alternative_id",
        )
        self._validate_collection(
            self.conflicts, MAX_GRAPH_CONFLICTS, GraphConflict, "conflicts", "conflict_id"
        )
        observation_ids = {item.observation_id for item in self.observations}
        entity_ids = {item.entity_id for item in self.entities}
        track_ids = {item.track_id for item in self.tracks}
        event_ids = {item.event_id for item in self.events}
        evidence_ids = {item.evidence_id for item in self.evidence}
        uncertainty_ids = {item.uncertainty_id for item in self.uncertainties}
        for observation in self.observations:
            self._subset(observation.evidence_ids, evidence_ids, "observation evidence")
            self._subset(observation.uncertainty_ids, uncertainty_ids, "observation uncertainty")
            self._subset(observation.entity_ids, entity_ids, "observation entity")
            self._subset(observation.track_ids, track_ids, "observation track")
            self._subset(observation.event_ids, event_ids, "observation event")
            for evidence_id in observation.evidence_ids:
                source_asset = next(
                    item.provenance.source.asset_id
                    for item in self.evidence
                    if item.evidence_id == evidence_id
                )
                if source_asset is not None and source_asset != observation.asset_id:
                    raise UnifiedEvidenceGraphError("observation evidence asset ownership differs")
        for entity in self.entities:
            self._subset(entity.observation_ids, observation_ids, "entity observation")
            self._subset(entity.track_ids, track_ids, "entity track")
            self._subset(entity.evidence_ids, evidence_ids, "entity evidence")
            self._subset(entity.uncertainty_ids, uncertainty_ids, "entity uncertainty")
        for track in self.tracks:
            self._subset(track.observation_ids, observation_ids, "track observation")
            if track.entity_id is not None and track.entity_id not in entity_ids:
                raise UnifiedEvidenceGraphError("track references an unknown entity")
            self._subset(track.uncertainty_ids, uncertainty_ids, "track uncertainty")
            observation_assets = {
                item.asset_id
                for item in self.observations
                if item.observation_id in track.observation_ids
            }
            if not {span.asset_id for span in track.spans}.issubset(observation_assets):
                raise UnifiedEvidenceGraphError("track span asset ownership differs")
        for event in self.events:
            self._subset(event.observation_ids, observation_ids, "event observation")
            self._subset(event.entity_ids, entity_ids, "event entity")
            self._subset(event.evidence_ids, evidence_ids, "event evidence")
            self._subset(event.uncertainty_ids, uncertainty_ids, "event uncertainty")
            if event.span is not None:
                observation_assets = {
                    item.asset_id
                    for item in self.observations
                    if item.observation_id in event.observation_ids
                }
                if event.span.asset_id not in observation_assets:
                    raise UnifiedEvidenceGraphError("event span asset ownership differs")
        node_sets: dict[GraphNodeKind, set[str]] = {
            GraphNodeKind.OBSERVATION: observation_ids,
            GraphNodeKind.ENTITY: entity_ids,
            GraphNodeKind.TRACK: track_ids,
            GraphNodeKind.EVENT: event_ids,
        }
        for alternative in self.alternatives:
            if alternative.target_id not in node_sets[alternative.target_kind]:
                raise UnifiedEvidenceGraphError("alternative references an unknown target")
            self._subset(alternative.evidence_ids, evidence_ids, "alternative evidence")
            self._subset(alternative.uncertainty_ids, uncertainty_ids, "alternative uncertainty")
        for conflict in self.conflicts:
            if not set(conflict.target_ids).issubset(node_sets[conflict.target_kind]):
                raise UnifiedEvidenceGraphError("conflict references an unknown target")
            self._subset(conflict.evidence_ids, evidence_ids, "conflict evidence")
            self._subset(conflict.uncertainty_ids, uncertainty_ids, "conflict uncertainty")
        for diagnostic in self.diagnostics:
            _identifier(diagnostic, "diagnostic")
        if len(self.to_wire_bytes()) > MAX_GRAPH_OUTPUT_BYTES:
            raise UnifiedEvidenceGraphError("unified evidence graph exceeds output limit")

    @staticmethod
    def _validate_collection(
        values: object,
        maximum: int,
        expected: type[object],
        field: str,
        identifier_field: str,
    ) -> None:
        if (
            not isinstance(values, tuple)
            or len(values) > maximum
            or not all(isinstance(item, expected) for item in values)
        ):
            raise UnifiedEvidenceGraphError(f"{field} are outside the bounded graph envelope")
        identifiers = tuple(getattr(item, identifier_field) for item in values)
        if len(identifiers) != len(set(identifiers)):
            raise UnifiedEvidenceGraphError(f"{field} IDs must be unique")

    @staticmethod
    def _subset(values: tuple[str, ...], allowed: set[str], field: str) -> None:
        if not set(values).issubset(allowed):
            raise UnifiedEvidenceGraphError(f"{field} references an unknown ID")

    @property
    def complete(self) -> bool:
        return self.status is GraphStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "complete": self.complete,
            "observations": [item.to_wire() for item in self.observations],
            "entities": [item.to_wire() for item in self.entities],
            "tracks": [item.to_wire() for item in self.tracks],
            "events": [item.to_wire() for item in self.events],
            "evidence": [item.to_wire() for item in self.evidence],
            "uncertainties": [item.to_wire() for item in self.uncertainties],
            "alternatives": [item.to_wire() for item in self.alternatives],
            "conflicts": [item.to_wire() for item in self.conflicts],
            "diagnostics": list(self.diagnostics),
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_public_dict(self) -> dict[str, object]:
        result = self.to_wire()
        result["fingerprint"] = self.fingerprint
        return result


def _tuple(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        raise UnifiedEvidenceGraphError(f"{field} must be a tuple")
    return value


def _checked_tuple(value: object, expected: type[_T], field: str) -> tuple[_T, ...]:
    values = _tuple(value, field)
    if not all(isinstance(item, expected) for item in values):
        raise UnifiedEvidenceGraphError(f"{field} contain an invalid value")
    return cast(tuple[_T, ...], values)


def build_unified_evidence_graph(
    observations: tuple[GraphObservation, ...] = (),
    entities: tuple[GraphEntity, ...] = (),
    tracks: tuple[GraphTrack, ...] = (),
    events: tuple[GraphEvent, ...] = (),
    *,
    evidence: tuple[EvidenceRecord, ...] = (),
    uncertainties: tuple[GraphUncertainty, ...] = (),
    alternatives: tuple[GraphAlternative, ...] = (),
    conflicts: tuple[GraphConflict, ...] = (),
    diagnostics: tuple[str, ...] = (),
) -> UnifiedEvidenceGraph:
    """Build a deterministic graph from explicit typed values only."""

    obs_input = _checked_tuple(observations, GraphObservation, "observations")
    ent_input = _checked_tuple(entities, GraphEntity, "entities")
    trk_input = _checked_tuple(tracks, GraphTrack, "tracks")
    evt_input = _checked_tuple(events, GraphEvent, "events")
    evd_input = _checked_tuple(evidence, EvidenceRecord, "evidence")
    unc_input = _checked_tuple(uncertainties, GraphUncertainty, "uncertainties")
    alt_input = _checked_tuple(alternatives, GraphAlternative, "alternatives")
    con_input = _checked_tuple(conflicts, GraphConflict, "conflicts")
    diagnostics_input = _checked_tuple(diagnostics, str, "diagnostics")
    for diagnostic in diagnostics_input:
        _identifier(diagnostic, "diagnostic")
    obs = _sorted_observations(obs_input)
    ent = tuple(sorted(ent_input, key=lambda item: item.entity_id))
    trk = tuple(sorted(trk_input, key=lambda item: item.track_id))
    evt = tuple(sorted(evt_input, key=lambda item: item.event_id))
    evd = tuple(sorted(evd_input, key=lambda item: item.evidence_id))
    unc = tuple(sorted(unc_input, key=lambda item: item.uncertainty_id))
    alt = tuple(sorted(alt_input, key=lambda item: item.alternative_id))
    con = tuple(sorted(con_input, key=lambda item: item.conflict_id))
    all_nodes = bool(obs or ent or trk or evt or evd or alt or con)
    has_conflict = (
        bool(con)
        or any(item.resolution is GraphResolution.CONFLICTING for item in ent)
        or any(item.resolution is GraphResolution.CONFLICTING for item in trk)
        or any(item.resolution is GraphResolution.CONFLICTING for item in evt)
    )
    has_ambiguity = (
        bool(alt)
        or any(
            item.resolution in {GraphResolution.AMBIGUOUS, GraphResolution.UNRESOLVED}
            for item in ent
        )
        or any(
            item.resolution in {GraphResolution.AMBIGUOUS, GraphResolution.UNRESOLVED}
            for item in trk
        )
        or any(
            item.resolution in {GraphResolution.AMBIGUOUS, GraphResolution.UNRESOLVED}
            for item in evt
        )
    )
    has_partial = any(
        item.support in {GraphSupport.UNCERTAIN, GraphSupport.UNSUPPORTED, GraphSupport.MISSING}
        for item in obs
    ) or any(
        item.support in {GraphSupport.UNCERTAIN, GraphSupport.UNSUPPORTED, GraphSupport.MISSING}
        for item in evt
    )
    status = (
        GraphStatus.EMPTY
        if not all_nodes
        else GraphStatus.CONFLICTING
        if has_conflict
        else GraphStatus.AMBIGUOUS
        if has_ambiguity
        else GraphStatus.PARTIAL
        if has_partial
        else GraphStatus.COMPLETE
    )
    return UnifiedEvidenceGraph(
        status=status,
        observations=obs,
        entities=ent,
        tracks=trk,
        events=evt,
        evidence=evd,
        uncertainties=unc,
        alternatives=alt,
        conflicts=con,
        diagnostics=tuple(sorted(set(diagnostics_input))),
    )


__all__ = [
    "UNIFIED_EVIDENCE_GRAPH_SCHEMA",
    "MAX_GRAPH_ALTERNATIVES",
    "MAX_GRAPH_CONFLICTS",
    "MAX_GRAPH_ENTITIES",
    "MAX_GRAPH_EVIDENCE",
    "MAX_GRAPH_EVENTS",
    "MAX_GRAPH_OBSERVATIONS",
    "MAX_GRAPH_OUTPUT_BYTES",
    "MAX_GRAPH_TRACKS",
    "MAX_GRAPH_UNCERTAINTIES",
    "GraphAlternative",
    "GraphConflict",
    "GraphEntity",
    "GraphEntityKind",
    "GraphEvent",
    "GraphEventKind",
    "GraphNodeKind",
    "GraphObservation",
    "GraphObservationKind",
    "GraphResolution",
    "GraphSourceSpan",
    "GraphStatus",
    "GraphSupport",
    "GraphTrack",
    "GraphUncertainty",
    "UnifiedEvidenceGraph",
    "UnifiedEvidenceGraphError",
    "build_unified_evidence_graph",
]
