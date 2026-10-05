"""M20-00: the reference-set authoring and soundtrack-ownership domain.

Later timeline authoring (`M20-02`) and the accessible Production editor (`M20-03`) need one pure
place that owns which admitted sources are selected, in what order, with which sparse soundtrack
ownership, and how much capacity remains.  This module is that place, and three decisions shape it:

* **Intent is not availability.**  A user command may only say *included* or *excluded* for a
  video's soundtrack.  Whether a qualified soundtrack source actually exists is a producer fact —
  ``available``/``unavailable``/``unknown`` — that arrives solely through
  :func:`apply_availability_facts` with a producer identity and a monotonic producer revision.
  Nothing a user command can express moves availability, ``unknown`` blocks the queue instead of
  collapsing either way, and a second producer identity is a conflict, not an update.  The
  producer named at finalization is the host-lane owner
  ``frontend/src/host/graphReferenceQualification.ts`` reaching this domain through the backend
  projection.

* **Soundtrack ownership is a sparse ID relation.**  ``include`` binds one admitted audio asset to
  one video by stable ID; no prefix count or presentation ordinal is ever inferred.  A bound audio
  projects immediately before its video only while the pairing derives *included*; an *excluded*
  or *unavailable* derivation creates **no audio label at all** — the bound asset must not silently
  become a standalone reference, because that would reinterpret the user's intent.  An explicit
  ``exclude`` unbinds, which visibly returns the asset to the standalone pool (and is therefore
  subject to the standalone capacity it re-enters).

* **Capacity is one input, reported together.**  Aggregate, per-kind/socket and timed-reference
  limits arrive as one validated :class:`ReferenceCapacityInput` — composed for H3-Base by
  :func:`build_h3_base_capacity` from the accepted `core.registry` socket authorities plus the
  H3-Base aggregate of twelve canonical reference files — and the remaining-capacity projection
  never advertises a per-kind maximum the aggregate would reject.

The legacy positional ``paired_audios`` node input keeps its surface: the selected disposition is
``KEEP_COMPAT``, and :func:`relations_from_legacy_positional` is the one explicit mapping from the
positional prefix into the sparse relations it has always produced (`context_request_nodes` ->
`core.registry` ``paired_video_id``), never a silent reinterpretation.

This module is not a second registry, prompt or graph authority.  Label text mirrors the
`core.registry` derivation and a dedicated test proves the agreement over the same canonical
sequence; native child mapping stays an adapter responsibility joined by stable identity.  Undo is
revision-scoped: every receipt carries a typed inverse command bound to the produced revision, so a
journal needs no hidden snapshots.  Restoring a removed video does not restore its availability
fact — facts are producer-owned and must be re-asserted, never replayed by a user-side inverse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_fingerprint
from .contracts import MediaKind
from .errors import ContractValidationError
from .registry import (
    MAX_PAIRED_VIDEO_AUDIO,
    MAX_REFERENCE_IMAGES,
    MAX_REFERENCE_VIDEOS,
    MAX_STANDALONE_AUDIO,
)

REFERENCE_SET_AUTHORING_SCHEMA = "h3-context-reference-set-authoring/1"

#: The selected legacy positional-pairing disposition (plan Metadata, frozen 2026-08-20): the node
#: surface keeps its positional prefix and its single boundary mapping; this domain speaks sparse
#: ID relations natively.
LEGACY_PAIRED_AUDIO_DISPOSITION = "KEEP_COMPAT"

#: H3-Base admission bounds the total canonical reference files independently of the per-socket
#: maxima (research `260816_MINIMAX_REFPACK_MULTI_REFERENCE_INTEGRATION_RESEARCH.md` §3): the four
#: socket ceilings sum to eighteen, and presenting them as simultaneously reachable is exactly the
#: defect the capacity projection exists to prevent.
H3_BASE_AGGREGATE_REFERENCE_FILES = 12

MAX_REVISION = 1_000_000
MAX_CAPACITY_CEILING = 64
MAX_AVAILABILITY_FACTS = 64
MAX_DURATION_MILLISECONDS = 86_400_000
MAX_TIMED_FRAMES = 1_000_000

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReferenceSetAuthoringError(ContractValidationError):
    """Typed atomic rejection; ``code`` is machine-readable and content-free."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _reject(code: str, message: str) -> ReferenceSetAuthoringError:
    return ReferenceSetAuthoringError(code, message)


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _reject("invalid_identifier", f"{field_name} must be a bounded identifier")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise _reject("invalid_fingerprint", f"{field_name} must be a sha256 fingerprint")
    return value


def _bounded_int(value: object, field_name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _reject("invalid_integer", f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise _reject("integer_bounds", f"{field_name} must be within [{minimum}, {maximum}]")
    return value


class SoundtrackIntent(str, Enum):
    """The only two things a user command may say about a video's soundtrack."""

    INCLUDED = "included"
    EXCLUDED = "excluded"


class SoundtrackAvailability(str, Enum):
    """Producer-attributed fact; never authored by a user command."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class DerivedSoundtrackState(str, Enum):
    """Intent joined with availability; ``blocked_unknown`` blocks the queue."""

    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNAVAILABLE = "unavailable"
    BLOCKED_UNKNOWN = "blocked_unknown"


class CommandKind(str, Enum):
    ADD_SOURCE = "add_source"
    REMOVE_SOURCE = "remove_source"
    REORDER_SOURCE = "reorder_source"
    INCLUDE_SOUNDTRACK = "include_soundtrack"
    EXCLUDE_SOUNDTRACK = "exclude_soundtrack"
    RESTORE_SOURCE = "restore_source"


@dataclass(frozen=True, slots=True)
class TimedReferenceLimits:
    """Per-reference limits for the timed kinds, from the accepted media capability profile."""

    max_duration_milliseconds: int
    max_frames: int

    def __post_init__(self) -> None:
        _bounded_int(
            self.max_duration_milliseconds,
            "max_duration_milliseconds",
            1,
            MAX_DURATION_MILLISECONDS,
        )
        _bounded_int(self.max_frames, "max_frames", 1, MAX_TIMED_FRAMES)

    def to_wire(self) -> dict[str, int]:
        return {
            "max_duration_milliseconds": self.max_duration_milliseconds,
            "max_frames": self.max_frames,
        }


@dataclass(frozen=True, slots=True)
class ReferenceCapacityInput:
    """The one accepted capability input capacity derives from; never re-derived here."""

    authority: str
    fingerprint: str
    aggregate_max: int
    image_max: int
    video_max: int
    paired_audio_max: int
    standalone_audio_max: int
    timed: TimedReferenceLimits

    def __post_init__(self) -> None:
        _identifier(self.authority, "capacity authority")
        _fingerprint(self.fingerprint, "capacity fingerprint")
        for name in (
            "aggregate_max",
            "image_max",
            "video_max",
            "paired_audio_max",
            "standalone_audio_max",
        ):
            _bounded_int(getattr(self, name), name, 1, MAX_CAPACITY_CEILING)
        if not isinstance(self.timed, TimedReferenceLimits):
            raise _reject("invalid_capacity", "timed must be a TimedReferenceLimits")

    def to_wire(self) -> dict[str, object]:
        return {
            "authority": self.authority,
            "fingerprint": self.fingerprint,
            "aggregate_max": self.aggregate_max,
            "image_max": self.image_max,
            "video_max": self.video_max,
            "paired_audio_max": self.paired_audio_max,
            "standalone_audio_max": self.standalone_audio_max,
            "timed": self.timed.to_wire(),
        }


def build_h3_base_capacity(
    *, authority: str, fingerprint: str, timed: TimedReferenceLimits
) -> ReferenceCapacityInput:
    """Compose the H3-Base capacity from the accepted socket authorities, never literals."""

    return ReferenceCapacityInput(
        authority=authority,
        fingerprint=fingerprint,
        aggregate_max=H3_BASE_AGGREGATE_REFERENCE_FILES,
        image_max=MAX_REFERENCE_IMAGES,
        video_max=MAX_REFERENCE_VIDEOS,
        paired_audio_max=MAX_PAIRED_VIDEO_AUDIO,
        standalone_audio_max=MAX_STANDALONE_AUDIO,
        timed=timed,
    )


@dataclass(frozen=True, slots=True)
class AdmittedSourceInput:
    """A visible host-owned source identity; no filename, path, URL or media byte."""

    source_id: str
    kind: MediaKind
    fingerprint: str
    duration_milliseconds: int | None

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        if not isinstance(self.kind, MediaKind):
            raise _reject("invalid_kind", "kind must be a MediaKind")
        _fingerprint(self.fingerprint, "source fingerprint")
        if self.kind is MediaKind.IMAGE:
            if self.duration_milliseconds is not None:
                raise _reject("kind_duration", "an image source cannot carry a duration")
            return
        if self.duration_milliseconds is not None:
            _bounded_int(
                self.duration_milliseconds,
                "duration_milliseconds",
                1,
                MAX_DURATION_MILLISECONDS,
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "fingerprint": self.fingerprint,
            "duration_milliseconds": self.duration_milliseconds,
        }


@dataclass(frozen=True, slots=True)
class SoundtrackRelation:
    """Sparse ownership: one video, an explicit intent, and — when included — one audio asset."""

    video_id: str
    intent: SoundtrackIntent
    soundtrack_source_id: str | None

    def __post_init__(self) -> None:
        _identifier(self.video_id, "relation video_id")
        if not isinstance(self.intent, SoundtrackIntent):
            raise _reject("invalid_intent", "intent must be a SoundtrackIntent")
        if self.intent is SoundtrackIntent.INCLUDED:
            if self.soundtrack_source_id is None:
                raise _reject("relation_incomplete", "an included relation names its audio asset")
            _identifier(self.soundtrack_source_id, "relation soundtrack_source_id")
        elif self.soundtrack_source_id is not None:
            raise _reject("relation_incomplete", "an excluded relation names no audio asset")

    def to_wire(self) -> dict[str, object]:
        return {
            "video_id": self.video_id,
            "intent": self.intent.value,
            "soundtrack_source_id": self.soundtrack_source_id,
        }


@dataclass(frozen=True, slots=True)
class AvailabilityFact:
    video_id: str
    availability: SoundtrackAvailability

    def __post_init__(self) -> None:
        _identifier(self.video_id, "fact video_id")
        if not isinstance(self.availability, SoundtrackAvailability):
            raise _reject("invalid_availability", "availability must be a SoundtrackAvailability")

    def to_wire(self) -> dict[str, str]:
        return {"video_id": self.video_id, "availability": self.availability.value}


@dataclass(frozen=True, slots=True)
class AvailabilityFactSet:
    """A producer-attributed snapshot; the only channel that moves availability."""

    producer: str
    producer_revision: int
    fingerprint: str
    facts: tuple[AvailabilityFact, ...]

    def __post_init__(self) -> None:
        _identifier(self.producer, "availability producer")
        _bounded_int(self.producer_revision, "producer_revision", 1, MAX_REVISION)
        _fingerprint(self.fingerprint, "availability fingerprint")
        if not isinstance(self.facts, tuple) or not all(
            isinstance(fact, AvailabilityFact) for fact in self.facts
        ):
            raise _reject("invalid_facts", "facts must be a tuple of AvailabilityFact")
        if len(self.facts) > MAX_AVAILABILITY_FACTS:
            raise _reject("facts_bounds", "availability fact count exceeds its bound")
        seen = {fact.video_id for fact in self.facts}
        if len(seen) != len(self.facts):
            raise _reject("duplicate_fact", "one fact per video per set")


# --------------------------------------------------------------------------- commands


@dataclass(frozen=True, slots=True)
class AddSource:
    expected_revision: int
    source: AdmittedSourceInput

    def __post_init__(self) -> None:
        _bounded_int(self.expected_revision, "expected_revision", 1, MAX_REVISION)
        if not isinstance(self.source, AdmittedSourceInput):
            raise _reject("invalid_command", "AddSource carries an AdmittedSourceInput")


@dataclass(frozen=True, slots=True)
class RemoveSource:
    expected_revision: int
    source_id: str

    def __post_init__(self) -> None:
        _bounded_int(self.expected_revision, "expected_revision", 1, MAX_REVISION)
        _identifier(self.source_id, "source_id")


@dataclass(frozen=True, slots=True)
class ReorderSource:
    expected_revision: int
    source_id: str
    new_index: int

    def __post_init__(self) -> None:
        _bounded_int(self.expected_revision, "expected_revision", 1, MAX_REVISION)
        _identifier(self.source_id, "source_id")
        _bounded_int(self.new_index, "new_index", 0, MAX_CAPACITY_CEILING)


@dataclass(frozen=True, slots=True)
class IncludeSoundtrack:
    expected_revision: int
    video_id: str
    soundtrack_source_id: str

    def __post_init__(self) -> None:
        _bounded_int(self.expected_revision, "expected_revision", 1, MAX_REVISION)
        _identifier(self.video_id, "video_id")
        _identifier(self.soundtrack_source_id, "soundtrack_source_id")


@dataclass(frozen=True, slots=True)
class ExcludeSoundtrack:
    expected_revision: int
    video_id: str

    def __post_init__(self) -> None:
        _bounded_int(self.expected_revision, "expected_revision", 1, MAX_REVISION)
        _identifier(self.video_id, "video_id")


@dataclass(frozen=True, slots=True)
class RestoreSource:
    """Typed inverse of :class:`RemoveSource`; never restores availability facts."""

    expected_revision: int
    source: AdmittedSourceInput
    kind_index: int
    relation: SoundtrackRelation | None

    def __post_init__(self) -> None:
        _bounded_int(self.expected_revision, "expected_revision", 1, MAX_REVISION)
        if not isinstance(self.source, AdmittedSourceInput):
            raise _reject("invalid_command", "RestoreSource carries an AdmittedSourceInput")
        _bounded_int(self.kind_index, "kind_index", 0, MAX_CAPACITY_CEILING)
        if self.relation is not None and not isinstance(self.relation, SoundtrackRelation):
            raise _reject("invalid_command", "relation must be a SoundtrackRelation or None")


Command = (
    AddSource | RemoveSource | ReorderSource | IncludeSoundtrack | ExcludeSoundtrack | RestoreSource
)

_COMMAND_KINDS: dict[type, CommandKind] = {
    AddSource: CommandKind.ADD_SOURCE,
    RemoveSource: CommandKind.REMOVE_SOURCE,
    ReorderSource: CommandKind.REORDER_SOURCE,
    IncludeSoundtrack: CommandKind.INCLUDE_SOUNDTRACK,
    ExcludeSoundtrack: CommandKind.EXCLUDE_SOUNDTRACK,
    RestoreSource: CommandKind.RESTORE_SOURCE,
}


# --------------------------------------------------------------------------- state


@dataclass(frozen=True, slots=True)
class ReferenceEntry:
    source_id: str
    kind: MediaKind
    fingerprint: str
    duration_milliseconds: int | None

    def to_wire(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "fingerprint": self.fingerprint,
            "duration_milliseconds": self.duration_milliseconds,
        }

    def to_input(self) -> AdmittedSourceInput:
        return AdmittedSourceInput(
            source_id=self.source_id,
            kind=self.kind,
            fingerprint=self.fingerprint,
            duration_milliseconds=self.duration_milliseconds,
        )


@dataclass(frozen=True, slots=True)
class ReferenceSetState:
    """Backend-revisioned authoring state; every mutation is a validated command."""

    capacity: ReferenceCapacityInput
    revision: int = 1
    images: tuple[ReferenceEntry, ...] = ()
    videos: tuple[ReferenceEntry, ...] = ()
    audios: tuple[ReferenceEntry, ...] = ()
    relations: tuple[SoundtrackRelation, ...] = ()
    availability_producer: str | None = None
    availability_revision: int = 0
    availability: tuple[AvailabilityFact, ...] = ()

    def entry(self, source_id: str) -> ReferenceEntry | None:
        for entry in self.images + self.videos + self.audios:
            if entry.source_id == source_id:
                return entry
        return None

    def relation_for(self, video_id: str) -> SoundtrackRelation | None:
        for relation in self.relations:
            if relation.video_id == video_id:
                return relation
        return None

    def availability_of(self, video_id: str) -> SoundtrackAvailability:
        for fact in self.availability:
            if fact.video_id == video_id:
                return fact.availability
        return SoundtrackAvailability.UNKNOWN

    def bound_audio_ids(self) -> frozenset[str]:
        return frozenset(
            relation.soundtrack_source_id
            for relation in self.relations
            if relation.intent is SoundtrackIntent.INCLUDED
            and relation.soundtrack_source_id is not None
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REFERENCE_SET_AUTHORING_SCHEMA,
            "revision": self.revision,
            "capacity": self.capacity.to_wire(),
            "images": [entry.to_wire() for entry in self.images],
            "videos": [entry.to_wire() for entry in self.videos],
            "audios": [entry.to_wire() for entry in self.audios],
            "relations": [relation.to_wire() for relation in self.relations],
            "availability_producer": self.availability_producer,
            "availability_revision": self.availability_revision,
            "availability": [fact.to_wire() for fact in self.availability],
        }


def create_reference_set(capacity: ReferenceCapacityInput) -> ReferenceSetState:
    if not isinstance(capacity, ReferenceCapacityInput):
        raise _reject("invalid_capacity", "capacity must be a ReferenceCapacityInput")
    return ReferenceSetState(capacity=capacity)


def derived_soundtrack_state(state: ReferenceSetState, video_id: str) -> DerivedSoundtrackState:
    relation = state.relation_for(_identifier(video_id, "video_id"))
    if relation is None or relation.intent is SoundtrackIntent.EXCLUDED:
        return DerivedSoundtrackState.EXCLUDED
    availability = state.availability_of(video_id)
    if availability is SoundtrackAvailability.AVAILABLE:
        return DerivedSoundtrackState.INCLUDED
    if availability is SoundtrackAvailability.UNAVAILABLE:
        return DerivedSoundtrackState.UNAVAILABLE
    return DerivedSoundtrackState.BLOCKED_UNKNOWN


# --------------------------------------------------------------------------- receipts


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    kind: CommandKind
    revision_before: int
    revision_after: int
    subject_ids: tuple[str, ...]
    inverse: Command
    state_fingerprint: str

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REFERENCE_SET_AUTHORING_SCHEMA,
            "kind": self.kind.value,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "subject_ids": list(self.subject_ids),
            "state_fingerprint": self.state_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class AvailabilityReceipt:
    producer: str
    producer_revision: int
    revision_before: int
    revision_after: int
    video_ids: tuple[str, ...]
    state_fingerprint: str

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REFERENCE_SET_AUTHORING_SCHEMA,
            "producer": self.producer,
            "producer_revision": self.producer_revision,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "video_ids": list(self.video_ids),
            "state_fingerprint": self.state_fingerprint,
        }


# --------------------------------------------------------------------------- projections


@dataclass(frozen=True, slots=True)
class ProjectedAsset:
    source_id: str
    kind: MediaKind
    label: str
    paired_with: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "label": self.label,
            "paired_with": self.paired_with,
        }


@dataclass(frozen=True, slots=True)
class QueueBlocker:
    video_id: str
    code: str

    def to_wire(self) -> dict[str, str]:
        return {"video_id": self.video_id, "code": self.code}


@dataclass(frozen=True, slots=True)
class SoundtrackProjection:
    video_id: str
    derived_state: DerivedSoundtrackState
    soundtrack_source_id: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "video_id": self.video_id,
            "derived_state": self.derived_state.value,
            "soundtrack_source_id": self.soundtrack_source_id,
        }


@dataclass(frozen=True, slots=True)
class CanonicalProjection:
    assets: tuple[ProjectedAsset, ...]
    soundtracks: tuple[SoundtrackProjection, ...]
    queue_blockers: tuple[QueueBlocker, ...]
    fingerprint: str

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REFERENCE_SET_AUTHORING_SCHEMA,
            "assets": [asset.to_wire() for asset in self.assets],
            "soundtracks": [row.to_wire() for row in self.soundtracks],
            "queue_blockers": [blocker.to_wire() for blocker in self.queue_blockers],
            "fingerprint": self.fingerprint,
        }


#: Mirrors `core.registry._label_text`; `tests/test_reference_set_authoring.py` proves agreement
#: against `build_reference_registry` over the same canonical sequence, so this cannot drift
#: silently into a second label authority.
_LABEL_PREFIX = {
    MediaKind.IMAGE: "Picture",
    MediaKind.VIDEO: "Video",
    MediaKind.AUDIO: "Audio",
}


def canonical_projection(state: ReferenceSetState) -> CanonicalProjection:
    """Images, then each derived-included soundtrack directly before its video, then standalone."""

    ordered: list[tuple[str, MediaKind, str | None]] = [
        (entry.source_id, entry.kind, None) for entry in state.images
    ]
    soundtracks: list[SoundtrackProjection] = []
    blockers: list[QueueBlocker] = []
    audio_by_id = {entry.source_id: entry for entry in state.audios}
    for video in state.videos:
        derived = derived_soundtrack_state(state, video.source_id)
        relation = state.relation_for(video.source_id)
        soundtrack_id = (
            relation.soundtrack_source_id
            if relation is not None and relation.intent is SoundtrackIntent.INCLUDED
            else None
        )
        soundtracks.append(
            SoundtrackProjection(
                video_id=video.source_id,
                derived_state=derived,
                soundtrack_source_id=soundtrack_id,
            )
        )
        if derived is DerivedSoundtrackState.BLOCKED_UNKNOWN:
            blockers.append(QueueBlocker(video.source_id, "blocked_unknown"))
        if derived is DerivedSoundtrackState.INCLUDED and soundtrack_id is not None:
            paired = audio_by_id[soundtrack_id]
            ordered.append((paired.source_id, paired.kind, video.source_id))
        ordered.append((video.source_id, video.kind, None))
    bound = state.bound_audio_ids()
    for audio in state.audios:
        if audio.source_id not in bound:
            ordered.append((audio.source_id, audio.kind, None))

    counters = {MediaKind.IMAGE: 0, MediaKind.VIDEO: 0, MediaKind.AUDIO: 0}
    assets: list[ProjectedAsset] = []
    for source_id, kind, paired_with in ordered:
        counters[kind] += 1
        assets.append(
            ProjectedAsset(
                source_id=source_id,
                kind=kind,
                label=f"<{_LABEL_PREFIX[kind]} {counters[kind]}>",
                paired_with=paired_with,
            )
        )
    wire = [asset.to_wire() for asset in assets]
    return CanonicalProjection(
        assets=tuple(assets),
        soundtracks=tuple(soundtracks),
        queue_blockers=tuple(blockers),
        fingerprint=canonical_fingerprint(wire),
    )


@dataclass(frozen=True, slots=True)
class CapacityProjection:
    """Aggregate, per-kind, socket and timed limits, reported together and never impossible."""

    aggregate_max: int
    aggregate_used: int
    aggregate_remaining: int
    image_used: int
    image_remaining: int
    video_used: int
    video_remaining: int
    paired_audio_used: int
    paired_audio_remaining: int
    standalone_audio_used: int
    standalone_audio_remaining: int
    timed: TimedReferenceLimits

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REFERENCE_SET_AUTHORING_SCHEMA,
            "aggregate_max": self.aggregate_max,
            "aggregate_used": self.aggregate_used,
            "aggregate_remaining": self.aggregate_remaining,
            "image_used": self.image_used,
            "image_remaining": self.image_remaining,
            "video_used": self.video_used,
            "video_remaining": self.video_remaining,
            "paired_audio_used": self.paired_audio_used,
            "paired_audio_remaining": self.paired_audio_remaining,
            "standalone_audio_used": self.standalone_audio_used,
            "standalone_audio_remaining": self.standalone_audio_remaining,
            "timed": self.timed.to_wire(),
        }


def capacity_projection(state: ReferenceSetState) -> CapacityProjection:
    capacity = state.capacity
    paired_used = len(state.bound_audio_ids())
    standalone_used = len(state.audios) - paired_used
    aggregate_used = len(state.images) + len(state.videos) + len(state.audios)
    aggregate_remaining = max(0, capacity.aggregate_max - aggregate_used)

    def remaining(kind_max: int, used: int) -> int:
        return min(max(0, kind_max - used), aggregate_remaining)

    return CapacityProjection(
        aggregate_max=capacity.aggregate_max,
        aggregate_used=aggregate_used,
        aggregate_remaining=aggregate_remaining,
        image_used=len(state.images),
        image_remaining=remaining(capacity.image_max, len(state.images)),
        video_used=len(state.videos),
        video_remaining=remaining(capacity.video_max, len(state.videos)),
        paired_audio_used=paired_used,
        paired_audio_remaining=remaining(capacity.paired_audio_max, paired_used),
        standalone_audio_used=standalone_used,
        standalone_audio_remaining=remaining(capacity.standalone_audio_max, standalone_used),
        timed=capacity.timed,
    )


# --------------------------------------------------------------------------- legacy mapping


def relations_from_legacy_positional(
    *, video_ids: tuple[str, ...], paired_audio_ids: tuple[str, ...]
) -> tuple[SoundtrackRelation, ...]:
    """The one explicit KEEP_COMPAT mapping from the positional node prefix to sparse relations.

    Position ``i`` of ``paired_audio_ids`` pairs position ``i`` of ``video_ids`` — exactly the
    semantics the node boundary has always applied — and a prefix longer than the ordered videos is
    rejected the same way `context_request_nodes` rejects it.
    """

    videos = tuple(_identifier(value, "legacy video_id") for value in video_ids)
    audios = tuple(_identifier(value, "legacy paired_audio_id") for value in paired_audio_ids)
    if len(set(videos)) != len(videos) or len(set(audios)) != len(audios):
        raise _reject("legacy_prefix_bounds", "legacy identifiers must be unique")
    if len(audios) > len(videos):
        raise _reject(
            "legacy_prefix_bounds",
            "paired_audios item count cannot exceed the ordered videos item count",
        )
    return tuple(
        SoundtrackRelation(
            video_id=videos[index],
            intent=SoundtrackIntent.INCLUDED,
            soundtrack_source_id=audios[index],
        )
        for index in range(len(audios))
    )


# --------------------------------------------------------------------------- command engine


def _lane_of(state: ReferenceSetState, kind: MediaKind) -> tuple[ReferenceEntry, ...]:
    if kind is MediaKind.IMAGE:
        return state.images
    if kind is MediaKind.VIDEO:
        return state.videos
    return state.audios


def _with_lane(
    state: ReferenceSetState,
    kind: MediaKind,
    lane: tuple[ReferenceEntry, ...],
    *,
    relations: tuple[SoundtrackRelation, ...] | None = None,
    availability: tuple[AvailabilityFact, ...] | None = None,
) -> ReferenceSetState:
    return ReferenceSetState(
        capacity=state.capacity,
        revision=_next_revision(state),
        images=lane if kind is MediaKind.IMAGE else state.images,
        videos=lane if kind is MediaKind.VIDEO else state.videos,
        audios=lane if kind is MediaKind.AUDIO else state.audios,
        relations=state.relations if relations is None else relations,
        availability_producer=state.availability_producer,
        availability_revision=state.availability_revision,
        availability=state.availability if availability is None else availability,
    )


def _next_revision(state: ReferenceSetState) -> int:
    if state.revision >= MAX_REVISION:
        raise _reject("revision_bounds", "the reference set revision space is exhausted")
    return state.revision + 1


def _check_add(
    state: ReferenceSetState, source: AdmittedSourceInput, *, binds_immediately: bool = False
) -> None:
    if state.entry(source.source_id) is not None:
        raise _reject("duplicate_source", "the source is already admitted")
    capacity = state.capacity
    if source.kind in (MediaKind.VIDEO, MediaKind.AUDIO):
        if source.duration_milliseconds is None:
            raise _reject("missing_duration", "a timed source declares its duration")
        if source.duration_milliseconds > capacity.timed.max_duration_milliseconds:
            raise _reject("timed_duration", "the source exceeds the timed-reference limit")
    if source.kind is MediaKind.IMAGE and len(state.images) + 1 > capacity.image_max:
        raise _reject("capacity_image", "the image socket ceiling is reached")
    if source.kind is MediaKind.VIDEO and len(state.videos) + 1 > capacity.video_max:
        raise _reject("capacity_video", "the video socket ceiling is reached")
    if source.kind is MediaKind.AUDIO and not binds_immediately:
        standalone_used = len(state.audios) - len(state.bound_audio_ids())
        if standalone_used + 1 > capacity.standalone_audio_max:
            raise _reject("capacity_standalone_audio", "the standalone audio ceiling is reached")
    aggregate = len(state.images) + len(state.videos) + len(state.audios)
    if aggregate + 1 > capacity.aggregate_max:
        raise _reject("capacity_aggregate", "the aggregate reference ceiling is reached")


def _apply_add(
    state: ReferenceSetState, command: AddSource
) -> tuple[ReferenceSetState, CommandReceipt]:
    source = command.source
    _check_add(state, source)
    entry = ReferenceEntry(
        source_id=source.source_id,
        kind=source.kind,
        fingerprint=source.fingerprint,
        duration_milliseconds=source.duration_milliseconds,
    )
    lane = _lane_of(state, source.kind) + (entry,)
    next_state = _with_lane(state, source.kind, lane)
    inverse = RemoveSource(next_state.revision, source.source_id)
    return next_state, _receipt(
        CommandKind.ADD_SOURCE, state, next_state, (source.source_id,), inverse
    )


def _apply_remove(
    state: ReferenceSetState, command: RemoveSource
) -> tuple[ReferenceSetState, CommandReceipt]:
    entry = state.entry(command.source_id)
    if entry is None:
        raise _reject("unknown_source", "no admitted source carries that identifier")
    lane = _lane_of(state, entry.kind)
    kind_index = next(
        index for index, value in enumerate(lane) if value.source_id == entry.source_id
    )
    removed_relation: SoundtrackRelation | None = None
    relations = state.relations
    availability = state.availability
    if entry.kind is MediaKind.VIDEO:
        removed_relation = state.relation_for(entry.source_id)
        if removed_relation is not None and removed_relation.intent is SoundtrackIntent.INCLUDED:
            # The freed audio visibly returns to the standalone pool, so the removal is
            # subject to the ceiling it would re-enter -- never a silent overflow.
            standalone_used = len(state.audios) - len(state.bound_audio_ids())
            if standalone_used + 1 > state.capacity.standalone_audio_max:
                raise _reject(
                    "capacity_standalone_audio",
                    "removing the video would overflow the standalone audio ceiling",
                )
        relations = tuple(r for r in relations if r.video_id != entry.source_id)
        availability = tuple(f for f in availability if f.video_id != entry.source_id)
    if entry.kind is MediaKind.AUDIO:
        for relation in relations:
            if relation.soundtrack_source_id == entry.source_id:
                removed_relation = relation
                relations = tuple(r for r in relations if r is not relation)
                break
    next_state = _with_lane(
        state,
        entry.kind,
        tuple(value for value in lane if value.source_id != entry.source_id),
        relations=relations,
        availability=availability,
    )
    inverse = RestoreSource(next_state.revision, entry.to_input(), kind_index, removed_relation)
    return next_state, _receipt(
        CommandKind.REMOVE_SOURCE, state, next_state, (entry.source_id,), inverse
    )


def _apply_restore(
    state: ReferenceSetState, command: RestoreSource
) -> tuple[ReferenceSetState, CommandReceipt]:
    source = command.source
    relation = command.relation
    restored_is_bound_audio = (
        relation is not None
        and relation.intent is SoundtrackIntent.INCLUDED
        and source.kind is MediaKind.AUDIO
        and relation.soundtrack_source_id == source.source_id
    )
    _check_add(state, source, binds_immediately=restored_is_bound_audio)
    lane = _lane_of(state, source.kind)
    if command.kind_index > len(lane):
        raise _reject("reorder_bounds", "the restore index exceeds the lane length")
    entry = ReferenceEntry(
        source_id=source.source_id,
        kind=source.kind,
        fingerprint=source.fingerprint,
        duration_milliseconds=source.duration_milliseconds,
    )
    new_lane = lane[: command.kind_index] + (entry,) + lane[command.kind_index :]
    relations = state.relations
    if relation is not None:
        conflicting = state.relation_for(relation.video_id)
        if conflicting is not None:
            raise _reject("soundtrack_already_included", "the video already has a relation")
        if relation.intent is SoundtrackIntent.INCLUDED:
            video_present = relation.video_id == source.source_id or (
                (existing_video := state.entry(relation.video_id)) is not None
                and existing_video.kind is MediaKind.VIDEO
            )
            if not video_present:
                raise _reject("unknown_video", "the restored relation names an unknown video")
            audio_present = restored_is_bound_audio or (
                (existing_audio := state.entry(relation.soundtrack_source_id or "")) is not None
                and existing_audio.kind is MediaKind.AUDIO
            )
            if not audio_present:
                raise _reject("unknown_audio", "the restored relation names an unknown audio")
            if (
                not restored_is_bound_audio
                and relation.soundtrack_source_id in state.bound_audio_ids()
            ):
                raise _reject("audio_already_bound", "the audio already pairs another video")
            if len(state.bound_audio_ids()) + 1 > state.capacity.paired_audio_max:
                raise _reject("capacity_paired_audio", "the paired soundtrack ceiling is reached")
        relations = relations + (relation,)
    next_state = _with_lane(state, source.kind, new_lane, relations=relations)
    inverse = RemoveSource(next_state.revision, source.source_id)
    return next_state, _receipt(
        CommandKind.RESTORE_SOURCE, state, next_state, (source.source_id,), inverse
    )


def _apply_reorder(
    state: ReferenceSetState, command: ReorderSource
) -> tuple[ReferenceSetState, CommandReceipt]:
    entry = state.entry(command.source_id)
    if entry is None:
        raise _reject("unknown_source", "no admitted source carries that identifier")
    lane = _lane_of(state, entry.kind)
    if command.new_index >= len(lane):
        raise _reject("reorder_bounds", "the target index exceeds the lane length")
    old_index = next(
        index for index, value in enumerate(lane) if value.source_id == entry.source_id
    )
    working = list(lane)
    working.insert(command.new_index, working.pop(old_index))
    next_state = _with_lane(state, entry.kind, tuple(working))
    inverse = ReorderSource(next_state.revision, entry.source_id, old_index)
    return next_state, _receipt(
        CommandKind.REORDER_SOURCE, state, next_state, (entry.source_id,), inverse
    )


def _apply_include(
    state: ReferenceSetState, command: IncludeSoundtrack
) -> tuple[ReferenceSetState, CommandReceipt]:
    video = state.entry(command.video_id)
    if video is None:
        raise _reject("unknown_video", "no admitted source carries the video identifier")
    if video.kind is not MediaKind.VIDEO:
        raise _reject("kind_mismatch", "a soundtrack targets a video")
    audio = state.entry(command.soundtrack_source_id)
    if audio is None:
        raise _reject("unknown_audio", "no admitted source carries the audio identifier")
    if audio.kind is not MediaKind.AUDIO:
        raise _reject("kind_mismatch", "a soundtrack is an audio asset")
    existing = state.relation_for(command.video_id)
    if existing is not None and existing.intent is SoundtrackIntent.INCLUDED:
        raise _reject("soundtrack_already_included", "the video already owns a soundtrack")
    if command.soundtrack_source_id in state.bound_audio_ids():
        raise _reject("audio_already_bound", "the audio already pairs another video")
    if len(state.bound_audio_ids()) + 1 > state.capacity.paired_audio_max:
        raise _reject("capacity_paired_audio", "the paired soundtrack ceiling is reached")
    relations = tuple(r for r in state.relations if r.video_id != command.video_id) + (
        SoundtrackRelation(
            video_id=command.video_id,
            intent=SoundtrackIntent.INCLUDED,
            soundtrack_source_id=command.soundtrack_source_id,
        ),
    )
    next_state = _with_lane(state, MediaKind.AUDIO, state.audios, relations=relations)
    inverse = ExcludeSoundtrack(next_state.revision, command.video_id)
    return next_state, _receipt(
        CommandKind.INCLUDE_SOUNDTRACK,
        state,
        next_state,
        (command.video_id, command.soundtrack_source_id),
        inverse,
    )


def _apply_exclude(
    state: ReferenceSetState, command: ExcludeSoundtrack
) -> tuple[ReferenceSetState, CommandReceipt]:
    video = state.entry(command.video_id)
    if video is None:
        raise _reject("unknown_video", "no admitted source carries the video identifier")
    if video.kind is not MediaKind.VIDEO:
        raise _reject("kind_mismatch", "a soundtrack exclusion targets a video")
    existing = state.relation_for(command.video_id)
    if existing is not None and existing.intent is SoundtrackIntent.INCLUDED:
        standalone_used = len(state.audios) - len(state.bound_audio_ids())
        if standalone_used + 1 > state.capacity.standalone_audio_max:
            raise _reject(
                "capacity_standalone_audio",
                "unbinding would exceed the standalone audio ceiling",
            )
    relations = tuple(r for r in state.relations if r.video_id != command.video_id) + (
        SoundtrackRelation(
            video_id=command.video_id,
            intent=SoundtrackIntent.EXCLUDED,
            soundtrack_source_id=None,
        ),
    )
    next_state = _with_lane(state, MediaKind.AUDIO, state.audios, relations=relations)
    if existing is not None and existing.soundtrack_source_id is not None:
        inverse: Command = IncludeSoundtrack(
            next_state.revision, command.video_id, existing.soundtrack_source_id
        )
    else:
        inverse = ExcludeSoundtrack(next_state.revision, command.video_id)
    return next_state, _receipt(
        CommandKind.EXCLUDE_SOUNDTRACK, state, next_state, (command.video_id,), inverse
    )


def _receipt(
    kind: CommandKind,
    before: ReferenceSetState,
    after: ReferenceSetState,
    subject_ids: tuple[str, ...],
    inverse: Command,
) -> CommandReceipt:
    return CommandReceipt(
        kind=kind,
        revision_before=before.revision,
        revision_after=after.revision,
        subject_ids=subject_ids,
        inverse=inverse,
        state_fingerprint=canonical_fingerprint(after.to_wire()),
    )


def apply_command(
    state: ReferenceSetState, command: Command
) -> tuple[ReferenceSetState, CommandReceipt]:
    """One exact next state and receipt, or one typed rejection with no partial state."""

    if not isinstance(state, ReferenceSetState):
        raise _reject("invalid_state", "state must be a ReferenceSetState")
    kind = _COMMAND_KINDS.get(type(command))
    if kind is None:
        raise _reject("invalid_command", "unknown command type")
    if command.expected_revision != state.revision:
        raise _reject("stale_revision", "the command expected a different revision")
    if kind is CommandKind.ADD_SOURCE:
        return _apply_add(state, command)  # type: ignore[arg-type]
    if kind is CommandKind.REMOVE_SOURCE:
        return _apply_remove(state, command)  # type: ignore[arg-type]
    if kind is CommandKind.REORDER_SOURCE:
        return _apply_reorder(state, command)  # type: ignore[arg-type]
    if kind is CommandKind.INCLUDE_SOUNDTRACK:
        return _apply_include(state, command)  # type: ignore[arg-type]
    if kind is CommandKind.EXCLUDE_SOUNDTRACK:
        return _apply_exclude(state, command)  # type: ignore[arg-type]
    return _apply_restore(state, command)  # type: ignore[arg-type]


def apply_video_source_batch(
    state: ReferenceSetState,
    sources: tuple[AdmittedSourceInput, ...],
) -> ReferenceSetState:
    """Atomically admit one ordered VIDEO batch with one reference revision advance.

    Validation is performed against an unpublished staged state so capacity and duplicate errors
    cannot expose a prefix.  This is intentionally narrower than the ordinary command vocabulary:
    M25-29 imports generated VIDEO only and creates no soundtrack relation.
    """

    if not isinstance(state, ReferenceSetState):
        raise _reject("invalid_state", "state must be a ReferenceSetState")
    if (
        type(sources) is not tuple
        or not sources
        or not all(type(source) is AdmittedSourceInput for source in sources)
        or any(source.kind is not MediaKind.VIDEO for source in sources)
    ):
        raise _reject("invalid_command", "batch must contain VIDEO source inputs")
    if len({source.source_id for source in sources}) != len(sources):
        raise _reject("duplicate_source", "batch source identifiers must be unique")
    if state.revision >= MAX_REVISION:
        raise _reject("revision_bounds", "the reference set revision space is exhausted")
    staged = state
    for source in sources:
        _check_add(staged, source)
        entry = ReferenceEntry(
            source_id=source.source_id,
            kind=source.kind,
            fingerprint=source.fingerprint,
            duration_milliseconds=source.duration_milliseconds,
        )
        staged = ReferenceSetState(
            capacity=staged.capacity,
            revision=state.revision,
            images=staged.images,
            videos=staged.videos + (entry,),
            audios=staged.audios,
            relations=staged.relations,
            availability_producer=staged.availability_producer,
            availability_revision=staged.availability_revision,
            availability=staged.availability,
        )
    return ReferenceSetState(
        capacity=staged.capacity,
        revision=state.revision + 1,
        images=staged.images,
        videos=staged.videos,
        audios=staged.audios,
        relations=staged.relations,
        availability_producer=staged.availability_producer,
        availability_revision=staged.availability_revision,
        availability=staged.availability,
    )


def apply_availability_facts(
    state: ReferenceSetState, fact_set: AvailabilityFactSet
) -> tuple[ReferenceSetState, AvailabilityReceipt]:
    """The producer channel: the only path that moves availability, and it is not a command."""

    if not isinstance(state, ReferenceSetState):
        raise _reject("invalid_state", "state must be a ReferenceSetState")
    if not isinstance(fact_set, AvailabilityFactSet):
        raise _reject("invalid_facts", "fact_set must be an AvailabilityFactSet")
    if state.availability_producer is not None and state.availability_producer != fact_set.producer:
        raise _reject(
            "availability_producer_conflict",
            "availability facts already have a different named producer",
        )
    if fact_set.producer_revision <= state.availability_revision:
        raise _reject("stale_availability", "the fact set is not newer than the stored facts")
    video_ids = {entry.source_id for entry in state.videos}
    for fact in fact_set.facts:
        if fact.video_id not in video_ids:
            raise _reject("unknown_video", "an availability fact names an unknown video")
    replaced = {fact.video_id for fact in fact_set.facts}
    merged = tuple(f for f in state.availability if f.video_id not in replaced) + fact_set.facts
    next_state = ReferenceSetState(
        capacity=state.capacity,
        revision=_next_revision(state),
        images=state.images,
        videos=state.videos,
        audios=state.audios,
        relations=state.relations,
        availability_producer=fact_set.producer,
        availability_revision=fact_set.producer_revision,
        availability=merged,
    )
    receipt = AvailabilityReceipt(
        producer=fact_set.producer,
        producer_revision=fact_set.producer_revision,
        revision_before=state.revision,
        revision_after=next_state.revision,
        video_ids=tuple(fact.video_id for fact in fact_set.facts),
        state_fingerprint=canonical_fingerprint(next_state.to_wire()),
    )
    return next_state, receipt


__all__ = [
    "H3_BASE_AGGREGATE_REFERENCE_FILES",
    "LEGACY_PAIRED_AUDIO_DISPOSITION",
    "MAX_REVISION",
    "REFERENCE_SET_AUTHORING_SCHEMA",
    "AddSource",
    "AdmittedSourceInput",
    "AvailabilityFact",
    "AvailabilityFactSet",
    "AvailabilityReceipt",
    "CanonicalProjection",
    "CapacityProjection",
    "Command",
    "CommandKind",
    "CommandReceipt",
    "DerivedSoundtrackState",
    "ExcludeSoundtrack",
    "IncludeSoundtrack",
    "ProjectedAsset",
    "QueueBlocker",
    "ReferenceCapacityInput",
    "ReferenceEntry",
    "ReferenceSetAuthoringError",
    "ReferenceSetState",
    "RemoveSource",
    "ReorderSource",
    "RestoreSource",
    "SoundtrackAvailability",
    "SoundtrackIntent",
    "SoundtrackProjection",
    "SoundtrackRelation",
    "TimedReferenceLimits",
    "apply_availability_facts",
    "apply_command",
    "apply_video_source_batch",
    "build_h3_base_capacity",
    "canonical_projection",
    "capacity_projection",
    "create_reference_set",
    "derived_soundtrack_state",
    "relations_from_legacy_positional",
]
