"""M13-03 source-owned cross-modal temporal grounding contracts.

The module joins caller-supplied graph events and role candidates on an explicit source/target
clock.  It does not decode media, infer causality, select a relation winner, or turn an alignment
score into a fact.  Every interval and relation remains inspectable and bounded.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import cast

from .canonical import canonical_fingerprint
from .errors import TemporalEventAlignmentError
from .reference_role_resolution import ReferenceRoleResolutionGraph
from .unified_evidence_graph import UnifiedEvidenceGraph

TEMPORAL_EVENT_ALIGNMENT_SCHEMA = "h3.temporal_event_alignment.v1"
MAX_TEMPORAL_EVENTS = 512
MAX_TEMPORAL_RELATIONS = 1024
MAX_TEMPORAL_IDS = 256
MAX_TEMPORAL_ASSETS = 256
MAX_TEMPORAL_TEXT = 4096
MAX_TEMPORAL_TIME_MS = 86_400_000
MAX_TEMPORAL_TIMEBASE = 1_000_000
MAX_TEMPORAL_SCALE = 10_000
MAX_TEMPORAL_OFFSET_MS = 86_400_000
MAX_TEMPORAL_ALIGNMENT_OUTPUT_BYTES = 65_536

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = (
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


class TemporalAlignmentStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    EMPTY = "empty"


class TemporalTimeDomain(str, Enum):
    SOURCE = "source"
    TARGET = "target"


class TemporalEventKind(str, Enum):
    SHOT = "shot"
    ACTION = "action"
    STATE_CHANGE = "state_change"
    DIALOGUE = "dialogue"
    VISIBLE_TEXT = "visible_text"
    AUDIO_EVENT = "audio_event"
    TRANSITION = "transition"
    REFERENCE = "reference"


class TemporalEventSupport(str, Enum):
    SUPPORTED = "supported"
    UNCERTAIN = "uncertain"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"


class TemporalEventResolution(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    CONFLICTING = "conflicting"


class TemporalRelationKind(str, Enum):
    OVERLAP = "overlap"
    GAP = "gap"
    CUT = "cut"
    CROSS_SHOT_AUDIO = "cross_shot_audio"
    CONTINUATION = "continuation"
    COPY_EVENT = "copy_event"
    BEFORE = "before"
    AFTER = "after"
    CAUSES = "causes"
    CAUSED_BY = "caused_by"
    UNCERTAIN_CAUSALITY = "uncertain_causality"


class TemporalRelationResolution(str, Enum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    UNCERTAIN = "uncertain"
    UNRESOLVED = "unresolved"
    CONFLICTING = "conflicting"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise TemporalEventAlignmentError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise TemporalEventAlignmentError(f"{field_name} contains sensitive or locator material")
    return value


def _text(value: object, field_name: str, maximum: int = MAX_TEMPORAL_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise TemporalEventAlignmentError(f"{field_name} must be bounded non-empty text")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise TemporalEventAlignmentError(f"{field_name} contains sensitive or locator material")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise TemporalEventAlignmentError(f"{field_name} contains an unsafe wire code point")
    return value


def _ids(
    values: object,
    field_name: str,
    maximum: int = MAX_TEMPORAL_IDS,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise TemporalEventAlignmentError(f"{field_name} must be a bounded tuple")
    if required and not values:
        raise TemporalEventAlignmentError(f"{field_name} must not be empty")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise TemporalEventAlignmentError(f"{field_name} must not contain duplicate identifiers")
    return result


def _fps(values: object, field_name: str, maximum: int = MAX_TEMPORAL_ASSETS) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise TemporalEventAlignmentError(f"{field_name} must be a bounded tuple")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
            raise TemporalEventAlignmentError(f"{field_name} must contain SHA-256 fingerprints")
        result.append(value)
    return tuple(result)


def _bounded_int(value: object, field_name: str, maximum: int, *, positive: bool = False) -> int:
    lower = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= maximum:
        raise TemporalEventAlignmentError(f"{field_name} is outside its finite bound")
    return value


def _confidence(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal("0") <= value <= Decimal("1")
    ):
        raise TemporalEventAlignmentError(f"{field_name} must be a finite Decimal between 0 and 1")
    return value


def _enum(value: object, expected: type[Enum], field_name: str) -> Enum:
    try:
        return value if isinstance(value, expected) else expected(value)
    except (TypeError, ValueError):
        raise TemporalEventAlignmentError(f"{field_name} is unsupported") from None


@dataclass(frozen=True, slots=True)
class TemporalTimeModel:
    """Declared source/target clocks and an exact bounded affine mapping."""

    model_id: str
    source_time_base_num: int
    source_time_base_den: int
    target_time_base_num: int
    target_time_base_den: int
    source_duration_ms: int
    target_duration_ms: int
    scale_num: int
    scale_den: int
    offset_ms: int

    def __post_init__(self) -> None:
        _identifier(self.model_id, "time model_id")
        for value, field_name in (
            (self.source_time_base_num, "source_time_base_num"),
            (self.source_time_base_den, "source_time_base_den"),
            (self.target_time_base_num, "target_time_base_num"),
            (self.target_time_base_den, "target_time_base_den"),
        ):
            _bounded_int(value, field_name, MAX_TEMPORAL_TIMEBASE, positive=True)
        _bounded_int(
            self.source_duration_ms, "source_duration_ms", MAX_TEMPORAL_TIME_MS, positive=True
        )
        _bounded_int(
            self.target_duration_ms, "target_duration_ms", MAX_TEMPORAL_TIME_MS, positive=True
        )
        _bounded_int(self.scale_num, "scale_num", MAX_TEMPORAL_SCALE, positive=True)
        _bounded_int(self.scale_den, "scale_den", MAX_TEMPORAL_SCALE, positive=True)
        if isinstance(self.offset_ms, bool) or not isinstance(self.offset_ms, int):
            raise TemporalEventAlignmentError("offset_ms must be an integer")
        if not -MAX_TEMPORAL_OFFSET_MS <= self.offset_ms <= MAX_TEMPORAL_OFFSET_MS:
            raise TemporalEventAlignmentError("offset_ms is outside its finite bound")

    def map_ms(self, source_ms: int, field_name: str = "source time") -> int:
        _bounded_int(source_ms, field_name, self.source_duration_ms)
        mapped = Fraction(source_ms * self.scale_num, self.scale_den) + self.offset_ms
        if mapped.denominator != 1:
            raise TemporalEventAlignmentError(
                "source-to-target mapping must produce integer milliseconds"
            )
        target_ms = mapped.numerator
        _bounded_int(target_ms, "mapped target time", self.target_duration_ms)
        return target_ms

    def to_wire(self) -> dict[str, int | str]:
        return {
            "model_id": self.model_id,
            "source_time_base_num": self.source_time_base_num,
            "source_time_base_den": self.source_time_base_den,
            "target_time_base_num": self.target_time_base_num,
            "target_time_base_den": self.target_time_base_den,
            "source_duration_ms": self.source_duration_ms,
            "target_duration_ms": self.target_duration_ms,
            "scale_num": self.scale_num,
            "scale_den": self.scale_den,
            "offset_ms": self.offset_ms,
        }


@dataclass(frozen=True, slots=True)
class TemporalInterval:
    """A non-empty half-open interval in either the source or target domain."""

    interval_id: str
    domain: TemporalTimeDomain | str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        _identifier(self.interval_id, "interval_id")
        object.__setattr__(
            self, "domain", _enum(self.domain, TemporalTimeDomain, "interval domain")
        )
        _bounded_int(self.start_ms, "interval start_ms", MAX_TEMPORAL_TIME_MS)
        _bounded_int(self.end_ms, "interval end_ms", MAX_TEMPORAL_TIME_MS)
        if self.end_ms <= self.start_ms:
            raise TemporalEventAlignmentError("interval end_ms must be after start_ms")

    def to_wire(self) -> dict[str, str | int]:
        return {
            "interval_id": self.interval_id,
            "domain": cast(TemporalTimeDomain, self.domain).value,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }


@dataclass(frozen=True, slots=True)
class TemporalEvent:
    """One graph-owned event grounded in source and target intervals."""

    event_id: str
    kind: TemporalEventKind | str
    label: str
    observation_ids: tuple[str, ...]
    source_asset_ids: tuple[str, ...]
    source_fingerprints: tuple[str, ...]
    source_interval: TemporalInterval
    target_interval: TemporalInterval
    source_ids: tuple[str, ...] = ()
    shot_ids: tuple[str, ...] = ()
    role_candidate_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    support: TemporalEventSupport | str = TemporalEventSupport.SUPPORTED
    resolution: TemporalEventResolution | str = TemporalEventResolution.RESOLVED
    confidence: Decimal | None = None
    evidence_ids: tuple[str, ...] = ()
    uncertainty_ids: tuple[str, ...] = ()
    schema: str = TEMPORAL_EVENT_ALIGNMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        object.__setattr__(self, "kind", _enum(self.kind, TemporalEventKind, "event kind"))
        _text(self.label, "event label")
        object.__setattr__(self, "observation_ids", _ids(self.observation_ids, "observation_ids"))
        object.__setattr__(
            self,
            "source_asset_ids",
            _ids(self.source_asset_ids, "source_asset_ids", MAX_TEMPORAL_ASSETS),
        )
        object.__setattr__(
            self,
            "source_fingerprints",
            _fps(self.source_fingerprints, "source_fingerprints"),
        )
        if len(self.source_asset_ids) != len(self.source_fingerprints):
            raise TemporalEventAlignmentError("source assets and fingerprints must be paired")
        if self.source_ids and len(self.source_ids) != len(self.source_asset_ids):
            raise TemporalEventAlignmentError("source_ids must pair with source assets")
        object.__setattr__(
            self, "source_ids", _ids(self.source_ids, "source_ids", MAX_TEMPORAL_ASSETS)
        )
        object.__setattr__(self, "shot_ids", _ids(self.shot_ids, "shot_ids"))
        object.__setattr__(
            self, "role_candidate_ids", _ids(self.role_candidate_ids, "role_candidate_ids")
        )
        object.__setattr__(self, "entity_ids", _ids(self.entity_ids, "entity_ids"))
        object.__setattr__(self, "candidate_ids", _ids(self.candidate_ids, "candidate_ids"))
        if not isinstance(self.source_interval, TemporalInterval):
            raise TemporalEventAlignmentError("source_interval must be a TemporalInterval")
        if not isinstance(self.target_interval, TemporalInterval):
            raise TemporalEventAlignmentError("target_interval must be a TemporalInterval")
        if self.source_interval.domain is not TemporalTimeDomain.SOURCE:
            raise TemporalEventAlignmentError("event source_interval must use the source domain")
        if self.target_interval.domain is not TemporalTimeDomain.TARGET:
            raise TemporalEventAlignmentError("event target_interval must use the target domain")
        object.__setattr__(
            self, "support", _enum(self.support, TemporalEventSupport, "event support")
        )
        object.__setattr__(
            self,
            "resolution",
            _enum(self.resolution, TemporalEventResolution, "event resolution"),
        )
        _confidence(self.confidence, "event confidence")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids, "evidence_ids"))
        object.__setattr__(self, "uncertainty_ids", _ids(self.uncertainty_ids, "uncertainty_ids"))
        if self.support is TemporalEventSupport.SUPPORTED and not self.source_asset_ids:
            raise TemporalEventAlignmentError("supported event requires source assets")
        if self.support is not TemporalEventSupport.SUPPORTED and not self.uncertainty_ids:
            raise TemporalEventAlignmentError("non-supported event requires uncertainty IDs")
        if (
            self.resolution
            in {
                TemporalEventResolution.AMBIGUOUS,
                TemporalEventResolution.CONFLICTING,
            }
            and len(self.candidate_ids) < 2
        ):
            raise TemporalEventAlignmentError("ambiguous/conflicting event requires candidates")
        if self.resolution is TemporalEventResolution.UNRESOLVED and self.candidate_ids:
            raise TemporalEventAlignmentError("unresolved event cannot carry candidates")
        if self.schema != TEMPORAL_EVENT_ALIGNMENT_SCHEMA:
            raise TemporalEventAlignmentError("unsupported temporal event alignment schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "kind": cast(TemporalEventKind, self.kind).value,
            "label": self.label,
            "observation_ids": list(self.observation_ids),
            "source_asset_ids": list(self.source_asset_ids),
            "source_fingerprints": list(self.source_fingerprints),
            "source_ids": list(self.source_ids),
            "source_interval": self.source_interval.to_wire(),
            "target_interval": self.target_interval.to_wire(),
            "shot_ids": list(self.shot_ids),
            "role_candidate_ids": list(self.role_candidate_ids),
            "entity_ids": list(self.entity_ids),
            "candidate_ids": list(self.candidate_ids),
            "support": cast(TemporalEventSupport, self.support).value,
            "resolution": cast(TemporalEventResolution, self.resolution).value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "evidence_ids": list(self.evidence_ids),
            "uncertainty_ids": list(self.uncertainty_ids),
        }


@dataclass(frozen=True, slots=True)
class TemporalRelation:
    """Explicit relation between source and target events."""

    relation_id: str
    kind: TemporalRelationKind | str
    source_event_ids: tuple[str, ...]
    target_event_ids: tuple[str, ...]
    resolution: TemporalRelationResolution | str
    confidence: Decimal | None = None
    evidence_ids: tuple[str, ...] = ()
    uncertainty_ids: tuple[str, ...] = ()
    detail: str | None = None
    schema: str = TEMPORAL_EVENT_ALIGNMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.relation_id, "relation_id")
        object.__setattr__(self, "kind", _enum(self.kind, TemporalRelationKind, "relation kind"))
        object.__setattr__(
            self, "source_event_ids", _ids(self.source_event_ids, "source_event_ids", required=True)
        )
        object.__setattr__(
            self, "target_event_ids", _ids(self.target_event_ids, "target_event_ids", required=True)
        )
        object.__setattr__(
            self,
            "resolution",
            _enum(self.resolution, TemporalRelationResolution, "relation resolution"),
        )
        _confidence(self.confidence, "relation confidence")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids, "relation evidence_ids"))
        object.__setattr__(
            self, "uncertainty_ids", _ids(self.uncertainty_ids, "relation uncertainty_ids")
        )
        if self.detail is not None:
            _text(self.detail, "relation detail")
        if self.kind is TemporalRelationKind.UNCERTAIN_CAUSALITY:
            if self.resolution is TemporalRelationResolution.EXPLICIT:
                raise TemporalEventAlignmentError("uncertain causality cannot be an explicit fact")
            if not self.uncertainty_ids:
                raise TemporalEventAlignmentError("uncertain causality requires uncertainty IDs")
        if self.schema != TEMPORAL_EVENT_ALIGNMENT_SCHEMA:
            raise TemporalEventAlignmentError("unsupported temporal event alignment schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "relation_id": self.relation_id,
            "kind": cast(TemporalRelationKind, self.kind).value,
            "source_event_ids": list(self.source_event_ids),
            "target_event_ids": list(self.target_event_ids),
            "resolution": cast(TemporalRelationResolution, self.resolution).value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "evidence_ids": list(self.evidence_ids),
            "uncertainty_ids": list(self.uncertainty_ids),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class TemporalAlignmentMetrics:
    """Structural interval/relation counts, never model accuracy claims."""

    event_count: int
    relation_count: int
    interval_error_count: int
    gap_count: int
    overlap_count: int
    cut_count: int
    cross_shot_audio_count: int
    continuation_count: int
    copy_event_count: int
    uncertain_causality_count: int

    def __post_init__(self) -> None:
        values = (
            self.event_count,
            self.relation_count,
            self.interval_error_count,
            self.gap_count,
            self.overlap_count,
            self.cut_count,
            self.cross_shot_audio_count,
            self.continuation_count,
            self.copy_event_count,
            self.uncertain_causality_count,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise TemporalEventAlignmentError(
                "temporal alignment metrics must be non-negative integers"
            )

    @property
    def interval_metric_pass(self) -> bool:
        return self.interval_error_count == 0

    def to_wire(self) -> dict[str, int | bool]:
        return {
            "event_count": self.event_count,
            "relation_count": self.relation_count,
            "interval_error_count": self.interval_error_count,
            "gap_count": self.gap_count,
            "overlap_count": self.overlap_count,
            "cut_count": self.cut_count,
            "cross_shot_audio_count": self.cross_shot_audio_count,
            "continuation_count": self.continuation_count,
            "copy_event_count": self.copy_event_count,
            "uncertain_causality_count": self.uncertain_causality_count,
            "interval_metric_pass": self.interval_metric_pass,
        }


@dataclass(frozen=True, slots=True)
class TemporalEventAlignment:
    """Inspectable deterministic temporal grounding document."""

    status: TemporalAlignmentStatus
    source_graph_fingerprint: str
    role_graph_fingerprint: str
    time_model: TemporalTimeModel
    events: tuple[TemporalEvent, ...]
    relations: tuple[TemporalRelation, ...]
    metrics: TemporalAlignmentMetrics
    diagnostics: tuple[str, ...] = ()
    schema: str = TEMPORAL_EVENT_ALIGNMENT_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.status, TemporalAlignmentStatus, "alignment status")
        for value, field_name in (
            (self.source_graph_fingerprint, "source_graph_fingerprint"),
            (self.role_graph_fingerprint, "role_graph_fingerprint"),
        ):
            if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
                raise TemporalEventAlignmentError(f"{field_name} must be a SHA-256 fingerprint")
        if not isinstance(self.time_model, TemporalTimeModel):
            raise TemporalEventAlignmentError("time_model must be a TemporalTimeModel")
        for values, maximum, expected, field_name, identifier_field in (
            (self.events, MAX_TEMPORAL_EVENTS, TemporalEvent, "events", "event_id"),
            (self.relations, MAX_TEMPORAL_RELATIONS, TemporalRelation, "relations", "relation_id"),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > maximum
                or not all(isinstance(item, expected) for item in values)
            ):
                raise TemporalEventAlignmentError(f"{field_name} are outside the bounded envelope")
            identifiers = tuple(getattr(item, identifier_field) for item in values)
            if len(identifiers) != len(set(identifiers)):
                raise TemporalEventAlignmentError(f"{field_name} IDs must be unique")
        if not isinstance(self.metrics, TemporalAlignmentMetrics):
            raise TemporalEventAlignmentError("metrics must be TemporalAlignmentMetrics")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_TEMPORAL_IDS:
            raise TemporalEventAlignmentError("diagnostics are outside the bounded envelope")
        for diagnostic in self.diagnostics:
            _identifier(diagnostic, "diagnostic")
        if len(self.to_wire_bytes()) > MAX_TEMPORAL_ALIGNMENT_OUTPUT_BYTES:
            raise TemporalEventAlignmentError("temporal event alignment exceeds output limit")

    @property
    def complete(self) -> bool:
        return self.status is TemporalAlignmentStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "complete": self.complete,
            "source_graph_fingerprint": self.source_graph_fingerprint,
            "role_graph_fingerprint": self.role_graph_fingerprint,
            "time_model": self.time_model.to_wire(),
            "events": [item.to_wire() for item in self.events],
            "relations": [item.to_wire() for item in self.relations],
            "metrics": self.metrics.to_wire(),
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


def _intervals_overlap(left: TemporalInterval, right: TemporalInterval) -> bool:
    return max(left.start_ms, right.start_ms) < min(left.end_ms, right.end_ms)


def _intervals_gap(left: TemporalInterval, right: TemporalInterval) -> bool:
    return left.end_ms <= right.start_ms or right.end_ms <= left.start_ms


def _relation_pairs(
    relation: TemporalRelation, events: dict[str, TemporalEvent]
) -> tuple[tuple[TemporalEvent, TemporalEvent], ...]:
    return tuple(
        (events[source_id], events[target_id])
        for source_id in relation.source_event_ids
        for target_id in relation.target_event_ids
    )


def _validate_relation_shape(relation: TemporalRelation, events: dict[str, TemporalEvent]) -> None:
    pairs = _relation_pairs(relation, events)
    if relation.kind is TemporalRelationKind.OVERLAP and not any(
        _intervals_overlap(source.target_interval, target.target_interval)
        for source, target in pairs
    ):
        raise TemporalEventAlignmentError("overlap relation has no overlapping target intervals")
    if relation.kind is TemporalRelationKind.GAP and not all(
        _intervals_gap(source.target_interval, target.target_interval) for source, target in pairs
    ):
        raise TemporalEventAlignmentError("gap relation has overlapping target intervals")
    if relation.kind is TemporalRelationKind.CUT and not any(
        source.target_interval.end_ms == target.target_interval.start_ms
        or target.target_interval.end_ms == source.target_interval.start_ms
        for source, target in pairs
    ):
        raise TemporalEventAlignmentError("cut relation is not anchored at a target boundary")
    if relation.kind is TemporalRelationKind.BEFORE and not all(
        source.target_interval.end_ms <= target.target_interval.start_ms for source, target in pairs
    ):
        raise TemporalEventAlignmentError("before relation has reversed target intervals")
    if relation.kind is TemporalRelationKind.AFTER and not all(
        source.target_interval.start_ms >= target.target_interval.end_ms for source, target in pairs
    ):
        raise TemporalEventAlignmentError("after relation has reversed target intervals")
    if relation.kind is TemporalRelationKind.CROSS_SHOT_AUDIO:
        endpoint_events = tuple(
            events[event_id]
            for event_id in (*relation.source_event_ids, *relation.target_event_ids)
        )
        if not any(event.kind is TemporalEventKind.AUDIO_EVENT for event in endpoint_events):
            raise TemporalEventAlignmentError("cross-shot audio relation requires an audio event")
        shot_ids = {shot_id for event in endpoint_events for shot_id in event.shot_ids}
        if len(shot_ids) < 2:
            raise TemporalEventAlignmentError("cross-shot audio relation requires two shot IDs")
    if relation.kind is TemporalRelationKind.CONTINUATION:
        endpoint_events = tuple(
            events[event_id]
            for event_id in (*relation.source_event_ids, *relation.target_event_ids)
        )
        shared_shots = set(endpoint_events[0].shot_ids)
        shared_roles = set(endpoint_events[0].role_candidate_ids)
        shared_entities = set(endpoint_events[0].entity_ids)
        if not (
            shared_shots.intersection(*(set(event.shot_ids) for event in endpoint_events[1:]))
            or shared_roles.intersection(
                *(set(event.role_candidate_ids) for event in endpoint_events[1:])
            )
            or shared_entities.intersection(
                *(set(event.entity_ids) for event in endpoint_events[1:])
            )
        ):
            raise TemporalEventAlignmentError("continuation relation lacks a shared grounding")
    if relation.kind is TemporalRelationKind.COPY_EVENT and not any(
        event.kind is TemporalEventKind.REFERENCE
        for event_id in (*relation.source_event_ids, *relation.target_event_ids)
        for event in (events[event_id],)
    ):
        raise TemporalEventAlignmentError("copy-event relation requires a reference event")


def build_temporal_event_alignment(
    graph: UnifiedEvidenceGraph,
    role_graph: ReferenceRoleResolutionGraph,
    time_model: TemporalTimeModel,
    events: tuple[TemporalEvent, ...] = (),
    relations: tuple[TemporalRelation, ...] = (),
) -> TemporalEventAlignment:
    """Build a deterministic temporal document from typed graph/role/event values."""

    if not isinstance(graph, UnifiedEvidenceGraph):
        raise TemporalEventAlignmentError("graph must be a UnifiedEvidenceGraph")
    if not isinstance(role_graph, ReferenceRoleResolutionGraph):
        raise TemporalEventAlignmentError("role_graph must be a ReferenceRoleResolutionGraph")
    if role_graph.source_graph_fingerprint != graph.fingerprint:
        raise TemporalEventAlignmentError(
            "role graph does not belong to the supplied evidence graph"
        )
    if not isinstance(time_model, TemporalTimeModel):
        raise TemporalEventAlignmentError("time_model must be a TemporalTimeModel")
    if (
        not isinstance(events, tuple)
        or len(events) > MAX_TEMPORAL_EVENTS
        or not all(isinstance(item, TemporalEvent) for item in events)
    ):
        raise TemporalEventAlignmentError("events must be a bounded tuple of TemporalEvent values")
    if (
        not isinstance(relations, tuple)
        or len(relations) > MAX_TEMPORAL_RELATIONS
        or not all(isinstance(item, TemporalRelation) for item in relations)
    ):
        raise TemporalEventAlignmentError(
            "relations must be a bounded tuple of TemporalRelation values"
        )
    event_ids = tuple(item.event_id for item in events)
    relation_ids = tuple(item.relation_id for item in relations)
    if len(event_ids) != len(set(event_ids)):
        raise TemporalEventAlignmentError("event IDs must be unique")
    if len(relation_ids) != len(set(relation_ids)):
        raise TemporalEventAlignmentError("relation IDs must be unique")
    graph_observations = {item.observation_id: item for item in graph.observations}
    role_candidates = {item.candidate_id for item in role_graph.assignments}
    role_candidates.update(label.asset_id for label in role_graph.labels)
    source_assets = {item.asset_id for item in graph.observations}
    uncertainties = {item.uncertainty_id for item in graph.uncertainties}
    ordered_events = tuple(
        sorted(events, key=lambda item: (item.source_interval.start_ms, item.event_id))
    )
    event_map = {item.event_id: item for item in ordered_events}
    partial = False
    ambiguous = False
    conflicting = False
    diagnostics: set[str] = set()
    for event in ordered_events:
        if not event.observation_ids and not event.role_candidate_ids:
            raise TemporalEventAlignmentError("event requires an observation or role candidate")
        if not set(event.source_asset_ids).issubset(
            source_assets | {label.asset_id for label in role_graph.labels}
        ):
            raise TemporalEventAlignmentError("event references an unknown source asset")
        if event.observation_ids:
            observations = []
            for observation_id in event.observation_ids:
                observation = graph_observations.get(observation_id)
                if observation is None:
                    raise TemporalEventAlignmentError("event references an unknown observation")
                observations.append(observation)
            if not {observation.asset_id for observation in observations}.issubset(
                set(event.source_asset_ids)
            ):
                raise TemporalEventAlignmentError("event observation is outside its source assets")
            for observation in observations:
                if observation.source_span is None:
                    raise TemporalEventAlignmentError("event observation lacks a source span")
                if (
                    observation.support.value != "supported"
                    and event.support is TemporalEventSupport.SUPPORTED
                ):
                    raise TemporalEventAlignmentError(
                        "supported event cannot hide non-supported observation"
                    )
        for asset_id, fingerprint in zip(
            event.source_asset_ids, event.source_fingerprints, strict=True
        ):
            matches = [
                observation
                for observation in graph_observations.values()
                if observation.asset_id == asset_id
                and observation.source_span is not None
                and observation.source_span.source_fingerprint == fingerprint
            ]
            if not matches and asset_id not in {label.asset_id for label in role_graph.labels}:
                raise TemporalEventAlignmentError("event source fingerprint is not graph-owned")
        if event.source_ids:
            for asset_id, source_id in zip(event.source_asset_ids, event.source_ids, strict=True):
                if not any(
                    observation.asset_id == asset_id
                    and observation.source_span is not None
                    and observation.source_span.source_id == source_id
                    for observation in graph_observations.values()
                ):
                    raise TemporalEventAlignmentError("event source ID is not graph-owned")
        if not set(event.role_candidate_ids).issubset(role_candidates):
            raise TemporalEventAlignmentError("event references an unknown role candidate")
        if event.candidate_ids and event.resolution is TemporalEventResolution.RESOLVED:
            raise TemporalEventAlignmentError("resolved event cannot carry candidate alternatives")
        mapped_start = time_model.map_ms(event.source_interval.start_ms, "event source start_ms")
        mapped_end = time_model.map_ms(event.source_interval.end_ms, "event source end_ms")
        if (mapped_start, mapped_end) != (
            event.target_interval.start_ms,
            event.target_interval.end_ms,
        ):
            raise TemporalEventAlignmentError(
                "event target interval does not match declared time mapping"
            )
        if event.support is not TemporalEventSupport.SUPPORTED:
            partial = True
        if event.resolution is TemporalEventResolution.AMBIGUOUS:
            ambiguous = True
        if event.resolution is TemporalEventResolution.CONFLICTING:
            conflicting = True
        for uncertainty_id in event.uncertainty_ids:
            if uncertainty_id not in uncertainties:
                # Explicit event-level uncertainty may be introduced by a provider; keep the ID
                # typed but do not allow it to masquerade as graph provenance.
                _identifier(uncertainty_id, "event uncertainty_id")
    ordered_relations = tuple(sorted(relations, key=lambda item: item.relation_id))
    for relation in ordered_relations:
        if not set(relation.source_event_ids + relation.target_event_ids).issubset(event_map):
            raise TemporalEventAlignmentError("relation references an unknown event")
        _validate_relation_shape(relation, event_map)
        if relation.kind is TemporalRelationKind.UNCERTAIN_CAUSALITY:
            ambiguous = True
            diagnostics.add(f"uncertain_causality:{relation.relation_id}")
        if relation.resolution is TemporalRelationResolution.CONFLICTING:
            conflicting = True
        if relation.resolution is TemporalRelationResolution.UNRESOLVED:
            ambiguous = True
        for uncertainty_id in relation.uncertainty_ids:
            if uncertainty_id not in uncertainties:
                _identifier(uncertainty_id, "relation uncertainty_id")
    metrics = TemporalAlignmentMetrics(
        len(ordered_events),
        len(ordered_relations),
        0,
        sum(item.kind is TemporalRelationKind.GAP for item in ordered_relations),
        sum(item.kind is TemporalRelationKind.OVERLAP for item in ordered_relations),
        sum(item.kind is TemporalRelationKind.CUT for item in ordered_relations),
        sum(item.kind is TemporalRelationKind.CROSS_SHOT_AUDIO for item in ordered_relations),
        sum(item.kind is TemporalRelationKind.CONTINUATION for item in ordered_relations),
        sum(item.kind is TemporalRelationKind.COPY_EVENT for item in ordered_relations),
        sum(item.kind is TemporalRelationKind.UNCERTAIN_CAUSALITY for item in ordered_relations),
    )
    status = (
        TemporalAlignmentStatus.EMPTY
        if not ordered_events
        else TemporalAlignmentStatus.CONFLICTING
        if conflicting
        or graph.status.value == "conflicting"
        or role_graph.status.value == "conflicting"
        else TemporalAlignmentStatus.AMBIGUOUS
        if ambiguous or graph.status.value == "ambiguous" or role_graph.status.value == "ambiguous"
        else TemporalAlignmentStatus.PARTIAL
        if partial or graph.status.value == "partial" or role_graph.status.value == "partial"
        else TemporalAlignmentStatus.COMPLETE
    )
    return TemporalEventAlignment(
        status,
        graph.fingerprint,
        role_graph.fingerprint,
        time_model,
        ordered_events,
        ordered_relations,
        metrics,
        tuple(sorted(diagnostics)),
    )


__all__ = [
    "TEMPORAL_EVENT_ALIGNMENT_SCHEMA",
    "MAX_TEMPORAL_EVENTS",
    "MAX_TEMPORAL_RELATIONS",
    "MAX_TEMPORAL_ALIGNMENT_OUTPUT_BYTES",
    "TemporalAlignmentMetrics",
    "TemporalAlignmentStatus",
    "TemporalEvent",
    "TemporalEventAlignment",
    "TemporalEventAlignmentError",
    "TemporalEventKind",
    "TemporalEventResolution",
    "TemporalEventSupport",
    "TemporalInterval",
    "TemporalRelation",
    "TemporalRelationKind",
    "TemporalRelationResolution",
    "TemporalTimeDomain",
    "TemporalTimeModel",
    "build_temporal_event_alignment",
]
