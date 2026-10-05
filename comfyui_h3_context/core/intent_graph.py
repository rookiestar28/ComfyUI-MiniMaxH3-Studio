"""Immutable H3 intent ownership graph and audiovisual timeline contracts.

The graph is a provider-free intermediate representation.  It records caller-declared subjects,
scenes, actions, camera, style, audio, explicit copy/retention relationships, and bounded timeline
segments.  It does not inspect media or infer relationships from the fact that an asset exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

from .constraints import DirectiveTarget, HardConstraintSet, KeepChangeAction, TimePoint
from .contracts import (
    AssetRole,
    MediaKind,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import ContractValidationError, IntentGraphError
from .registry import ReferenceRegistry

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_TEXT_LENGTH = 65_536
_MAX_REFERENCE_IDS = 64
_MAX_GRAPH_ITEMS = 256
INTENT_GRAPH_SCHEMA = "h3.context.intent_graph.v2"


class AudioLayer(str, Enum):
    """Explicit audio ownership layers used by the prompt compiler."""

    DIEGETIC = "diegetic"
    AMBIENCE = "ambience"
    NON_DIEGETIC = "non_diegetic"
    DIALOGUE = "dialogue"
    MUSIC = "music"
    SFX = "sfx"


class AudioOwnership(str, Enum):
    """Which official output layer an audio item is authorized to enter.

    The pinned guide partitions sound three ways: the integrated body owns dialogue, singing and
    diegetic music; ``overall_soundscape`` owns ambience, physical action sound and non-verbal
    human sound; ``non_diegetic_music`` owns audience-only score. ``UNSPECIFIED`` means the author
    has not said, which is a real state and not a default section.
    """

    UNSPECIFIED = "unspecified"
    INTEGRATED = "integrated"
    SOUNDSCAPE = "soundscape"
    AUDIENCE_ONLY = "audience_only"


class AudioScope(str, Enum):
    """Whether an audio item is owned by the timeline or summarizes the whole video."""

    TIMELINE = "timeline"
    WHOLE_VIDEO = "whole_video"


class SoundscapeDisposition(str, Enum):
    """The whole-video sound claim.

    The guide permits ``overall_soundscape: N/A`` only for an explicit request for complete
    silence. An empty audio tuple is therefore ``UNSPECIFIED``: the author said nothing, which is
    not the same fact as the author asking for silence.
    """

    UNSPECIFIED = "unspecified"
    DESCRIBED = "described"
    EXPLICIT_COMPLETE_SILENCE = "explicit_complete_silence"


# GUARD: the closed layer/ownership compatibility matrix. An explicit ownership may only refine
# what its layer already means -- it may never contradict it, because the layer is the author's
# own statement about the sound. `DIEGETIC` and `MUSIC` are the two genuinely ambiguous layers
# (in-world sound vs in-world music; in-world music vs audience-only score), so they are the only
# ones with no derived default: an item on either layer stays unresolved, and therefore enters no
# output section, until the author declares which side it is on. Widening a row here silently
# authorizes a sound to appear in a guide layer that does not own it.
_AUDIO_OWNERSHIP_COMPATIBILITY: dict[AudioLayer, frozenset[AudioOwnership]] = {
    AudioLayer.DIALOGUE: frozenset({AudioOwnership.INTEGRATED}),
    AudioLayer.MUSIC: frozenset({AudioOwnership.INTEGRATED, AudioOwnership.AUDIENCE_ONLY}),
    AudioLayer.DIEGETIC: frozenset({AudioOwnership.INTEGRATED, AudioOwnership.SOUNDSCAPE}),
    AudioLayer.AMBIENCE: frozenset({AudioOwnership.INTEGRATED, AudioOwnership.SOUNDSCAPE}),
    AudioLayer.SFX: frozenset({AudioOwnership.INTEGRATED, AudioOwnership.SOUNDSCAPE}),
    AudioLayer.NON_DIEGETIC: frozenset({AudioOwnership.AUDIENCE_ONLY}),
}

_DERIVED_AUDIO_OWNERSHIP: dict[AudioLayer, AudioOwnership] = {
    AudioLayer.DIALOGUE: AudioOwnership.INTEGRATED,
    AudioLayer.AMBIENCE: AudioOwnership.SOUNDSCAPE,
    AudioLayer.SFX: AudioOwnership.SOUNDSCAPE,
    AudioLayer.NON_DIEGETIC: AudioOwnership.AUDIENCE_ONLY,
}


class EventCopyMode(str, Enum):
    """How an explicitly linked event is retained in a target segment."""

    FULL_COPY = "full_copy"
    PARTIAL_COPY = "partial_copy"
    REFERENCE = "reference"


class RetentionDomain(str, Enum):
    """Domain owned by an event or retention relation."""

    VISUAL = "visual"
    AUDIO = "audio"


class VisualRetentionMarker(str, Enum):
    """Closed visible-content retention markers from the Full-Reference guide."""

    FULLY_PRESERVED = "fully_preserved"
    PARTIALLY_PRESERVED = "partially_preserved"
    ATTRIBUTE_TRANSFER = "attribute_transfer"
    WEAK_REFERENCE = "weak_reference"


class AudioRetentionMarker(str, Enum):
    """Closed audio copy/reference markers from the Full-Reference guide."""

    FULLY_COPY = "fully_copy"
    PARTIALLY_COPY = "partially_copy"
    REFERENCE = "reference"
    WEAK_REFERENCE = "weak_reference"


RetentionMarker = VisualRetentionMarker | AudioRetentionMarker


class RetentionScope(str, Enum):
    """Declared output denotation; provenance alone does not establish this scope."""

    UNSPECIFIED = "unspecified"
    SUBJECT = "subject"
    PICTURE = "picture"
    VIDEO_STRUCTURE = "video_structure"
    AUDIO_LAYER = "audio_layer"
    COMPLETE_FINAL_AUDIO_TRACK = "complete_final_audio_track"


class SegmentDevelopment(str, Enum):
    """Caller declarations about owned anchor content, never inferred media observations."""

    UNSPECIFIED = "unspecified"
    ANCHOR_DEVELOPMENT = "anchor_development"
    ANCHOR_STATIC_HOLD = "anchor_static_hold"


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a bounded identifier")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT_LENGTH:
        raise ContractValidationError(f"{field} must be a non-empty bounded string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ContractValidationError(f"{field} contains an unsafe wire code point")
    return value


def _require_optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_identifier(value, field)


def _require_enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ContractValidationError(f"{field} must be a {expected.__name__}")


T = TypeVar("T")


def _require_tuple(values: object, expected: type[T], field: str, maximum: int) -> tuple[T, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ContractValidationError(f"{field} must be a tuple of at most {maximum} values")
    if not all(isinstance(value, expected) for value in values):
        raise ContractValidationError(f"{field} contains an invalid value")
    return values


def _require_id_tuple(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > _MAX_REFERENCE_IDS:
        raise ContractValidationError(
            f"{field} must be a tuple of at most {_MAX_REFERENCE_IDS} identifiers"
        )
    identifiers = tuple(_require_identifier(value, f"{field} item") for value in values)
    if len(identifiers) != len(set(identifiers)):
        raise ContractValidationError(f"{field} must not contain duplicate identifiers")
    return identifiers


def _wire_values(values: Iterable[object]) -> list[object]:
    return [value.to_wire() for value in values]  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class IntentSubject:
    """A named subject with explicit source-asset ownership."""

    subject_id: str
    label: str
    source_asset_ids: tuple[str, ...] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.subject_id, "subject_id")
        _require_text(self.label, "subject label")
        _require_id_tuple(self.source_asset_ids, "subject source_asset_ids")
        if self.description is not None:
            _require_text(self.description, "subject description")

    def to_wire(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "label": self.label,
            "source_asset_ids": list(self.source_asset_ids),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class IntentScene:
    """A declared scene and its subject/style ownership."""

    scene_id: str
    description: str
    subject_ids: tuple[str, ...] = ()
    style_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.scene_id, "scene_id")
        _require_text(self.description, "scene description")
        _require_id_tuple(self.subject_ids, "scene subject_ids")
        _require_optional_identifier(self.style_id, "scene style_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "scene_id": self.scene_id,
            "description": self.description,
            "subject_ids": list(self.subject_ids),
            "style_id": self.style_id,
        }


@dataclass(frozen=True, slots=True)
class IntentAction:
    """A declared action owned by subjects and optionally a scene."""

    action_id: str
    description: str
    subject_ids: tuple[str, ...] = ()
    scene_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.action_id, "action_id")
        _require_text(self.description, "action description")
        _require_id_tuple(self.subject_ids, "action subject_ids")
        _require_optional_identifier(self.scene_id, "action scene_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "description": self.description,
            "subject_ids": list(self.subject_ids),
            "scene_id": self.scene_id,
        }


@dataclass(frozen=True, slots=True)
class CameraIntent:
    """A camera movement/framing declaration with explicit scene ownership."""

    camera_id: str
    description: str
    scene_id: str | None = None
    subject_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.camera_id, "camera_id")
        _require_text(self.description, "camera description")
        _require_optional_identifier(self.scene_id, "camera scene_id")
        _require_id_tuple(self.subject_ids, "camera subject_ids")

    def to_wire(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "description": self.description,
            "scene_id": self.scene_id,
            "subject_ids": list(self.subject_ids),
        }


@dataclass(frozen=True, slots=True)
class StyleIntent:
    """A named visual style declaration."""

    style_id: str
    description: str

    def __post_init__(self) -> None:
        _require_identifier(self.style_id, "style_id")
        _require_text(self.description, "style description")

    def to_wire(self) -> dict[str, str]:
        return {"style_id": self.style_id, "description": self.description}


@dataclass(frozen=True, slots=True)
class AudioIntent:
    """A declared audio layer with optional source-asset and subject ownership."""

    audio_id: str
    layer: AudioLayer
    description: str
    source_asset_ids: tuple[str, ...] = ()
    subject_ids: tuple[str, ...] = ()
    ownership: AudioOwnership = AudioOwnership.UNSPECIFIED
    scope: AudioScope = AudioScope.TIMELINE

    def __post_init__(self) -> None:
        _require_identifier(self.audio_id, "audio_id")
        _require_enum(self.layer, AudioLayer, "audio layer")
        _require_text(self.description, "audio description")
        _require_id_tuple(self.source_asset_ids, "audio source_asset_ids")
        _require_id_tuple(self.subject_ids, "audio subject_ids")
        _require_enum(self.ownership, AudioOwnership, "audio ownership")
        _require_enum(self.scope, AudioScope, "audio scope")
        if (
            self.ownership is not AudioOwnership.UNSPECIFIED
            and self.ownership not in _AUDIO_OWNERSHIP_COMPATIBILITY[self.layer]
        ):
            raise IntentGraphError(
                f"audio ownership {self.ownership.value!r} contradicts layer {self.layer.value!r}"
            )
        if self.scope is AudioScope.WHOLE_VIDEO and self.resolved_ownership is (
            AudioOwnership.INTEGRATED
        ):
            # Whole-video scope exists to authorize a summary field without a shot link. Integrated
            # sound is written into the shot that carries it, so it has no whole-video form.
            raise IntentGraphError(
                "integrated audio is owned by a shot and cannot be scoped to the whole video"
            )

    @property
    def resolved_ownership(self) -> AudioOwnership | None:
        """The output layer this item owns, or ``None`` when the author has not decided.

        An unresolved item is not an error and not a default; it simply cannot enter an output
        section, because the renderer would otherwise have to guess between the integrated body
        and a summary field.
        """

        if self.ownership is not AudioOwnership.UNSPECIFIED:
            return self.ownership
        return _DERIVED_AUDIO_OWNERSHIP.get(self.layer)

    def to_wire(self) -> dict[str, object]:
        return {
            "audio_id": self.audio_id,
            "layer": self.layer.value,
            "description": self.description,
            "source_asset_ids": list(self.source_asset_ids),
            "subject_ids": list(self.subject_ids),
            "ownership": self.ownership.value,
            "scope": self.scope.value,
        }


@dataclass(frozen=True, slots=True)
class EventCopy:
    """An explicit media event-copy relationship targeting one timeline segment."""

    event_id: str
    domain: RetentionDomain
    source_asset_id: str
    target_segment_id: str
    mode: EventCopyMode

    def __post_init__(self) -> None:
        _require_identifier(self.event_id, "event_id")
        _require_enum(self.domain, RetentionDomain, "event domain")
        _require_identifier(self.source_asset_id, "event source_asset_id")
        _require_identifier(self.target_segment_id, "event target_segment_id")
        _require_enum(self.mode, EventCopyMode, "event copy mode")

    def to_wire(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "domain": self.domain.value,
            "source_asset_id": self.source_asset_id,
            "target_segment_id": self.target_segment_id,
            "mode": self.mode.value,
        }


@dataclass(frozen=True, slots=True)
class RetentionRelation:
    """An explicit source-to-target preservation relation in one closed domain."""

    relation_id: str
    domain: RetentionDomain
    source_asset_ids: tuple[str, ...]
    target_id: str
    marker: RetentionMarker
    scope: RetentionScope = RetentionScope.UNSPECIFIED

    def __post_init__(self) -> None:
        _require_identifier(self.relation_id, "retention relation_id")
        _require_enum(self.domain, RetentionDomain, "retention domain")
        _require_id_tuple(self.source_asset_ids, "retention source_asset_ids")
        if not self.source_asset_ids:
            raise ContractValidationError("retention source_asset_ids must not be empty")
        _require_identifier(self.target_id, "retention target_id")
        _require_enum(self.scope, RetentionScope, "retention scope")
        visual_scopes = {
            RetentionScope.SUBJECT,
            RetentionScope.PICTURE,
            RetentionScope.VIDEO_STRUCTURE,
        }
        audio_scopes = {RetentionScope.AUDIO_LAYER, RetentionScope.COMPLETE_FINAL_AUDIO_TRACK}
        if (self.domain is RetentionDomain.VISUAL and self.scope in audio_scopes) or (
            self.domain is RetentionDomain.AUDIO and self.scope in visual_scopes
        ):
            raise ContractValidationError("retention scope contradicts its domain")
        if self.domain is RetentionDomain.VISUAL and not isinstance(
            self.marker, VisualRetentionMarker
        ):
            raise ContractValidationError("visual retention requires a visual marker")
        if self.domain is RetentionDomain.AUDIO and not isinstance(
            self.marker, AudioRetentionMarker
        ):
            raise ContractValidationError("audio retention requires an audio marker")

    def to_wire(self) -> dict[str, object]:
        return {
            "relation_id": self.relation_id,
            "domain": self.domain.value,
            "source_asset_ids": list(self.source_asset_ids),
            "target_id": self.target_id,
            "marker": self.marker.value,
            "scope": self.scope.value,
        }


@dataclass(frozen=True, slots=True)
class TimelineSegment:
    """A half-open audiovisual interval with typed graph references."""

    segment_id: str
    start: TimePoint
    end: TimePoint
    scene_id: str | None = None
    subject_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    camera_id: str | None = None
    style_id: str | None = None
    audio_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    development: SegmentDevelopment = SegmentDevelopment.UNSPECIFIED

    def __post_init__(self) -> None:
        _require_identifier(self.segment_id, "segment_id")
        if not isinstance(self.start, TimePoint) or not isinstance(self.end, TimePoint):
            raise ContractValidationError("timeline bounds must be TimePoint values")
        if self.end.seconds <= self.start.seconds:
            raise ContractValidationError("timeline segment end must be after start")
        _require_optional_identifier(self.scene_id, "segment scene_id")
        _require_id_tuple(self.subject_ids, "segment subject_ids")
        _require_id_tuple(self.action_ids, "segment action_ids")
        _require_optional_identifier(self.camera_id, "segment camera_id")
        _require_optional_identifier(self.style_id, "segment style_id")
        _require_id_tuple(self.audio_ids, "segment audio_ids")
        _require_id_tuple(self.event_ids, "segment event_ids")
        _require_enum(self.development, SegmentDevelopment, "segment development")

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "scene_id": self.scene_id,
            "subject_ids": list(self.subject_ids),
            "action_ids": list(self.action_ids),
            "camera_id": self.camera_id,
            "style_id": self.style_id,
            "audio_ids": list(self.audio_ids),
            "event_ids": list(self.event_ids),
            "development": self.development.value,
        }


@dataclass(frozen=True, slots=True)
class IntentGraph:
    """Immutable typed intent graph before prompt rendering or provider execution."""

    effective_duration: TimePoint
    registry: ReferenceRegistry
    subjects: tuple[IntentSubject, ...] = ()
    scenes: tuple[IntentScene, ...] = ()
    actions: tuple[IntentAction, ...] = ()
    cameras: tuple[CameraIntent, ...] = ()
    styles: tuple[StyleIntent, ...] = ()
    audios: tuple[AudioIntent, ...] = ()
    events: tuple[EventCopy, ...] = ()
    retention: tuple[RetentionRelation, ...] = ()
    segments: tuple[TimelineSegment, ...] = ()
    allow_gaps: bool = False
    soundscape: SoundscapeDisposition = SoundscapeDisposition.UNSPECIFIED

    def __post_init__(self) -> None:
        if not isinstance(self.effective_duration, TimePoint):
            raise ContractValidationError("effective_duration must be a TimePoint")
        if self.effective_duration.seconds <= 0:
            raise ContractValidationError("effective_duration must be positive")
        if not isinstance(self.registry, ReferenceRegistry):
            raise ContractValidationError("registry must be a ReferenceRegistry")
        for field, expected in (
            ("subjects", IntentSubject),
            ("scenes", IntentScene),
            ("actions", IntentAction),
            ("cameras", CameraIntent),
            ("styles", StyleIntent),
            ("audios", AudioIntent),
            ("events", EventCopy),
            ("retention", RetentionRelation),
            ("segments", TimelineSegment),
        ):
            _require_tuple(getattr(self, field), expected, field, _MAX_GRAPH_ITEMS)
        if not isinstance(self.allow_gaps, bool):
            raise ContractValidationError("allow_gaps must be a bool")
        _require_enum(self.soundscape, SoundscapeDisposition, "graph soundscape")
        self._require_consistent_soundscape()

    def _require_consistent_soundscape(self) -> None:
        """Fail closed when the whole-video sound claim contradicts the declared audio.

        GUARD: silence is a claim, never an absence. `EXPLICIT_COMPLETE_SILENCE` is what
        authorizes `overall_soundscape: N/A` downstream, so it cannot coexist with a declared
        audible item, and `DESCRIBED` cannot be asserted without an item that actually reaches the
        soundscape. Relaxing either direction re-creates the M24-05 defect one layer lower, where
        the renderer can no longer tell an unstated soundscape from a requested silent one.
        """

        if self.soundscape is SoundscapeDisposition.EXPLICIT_COMPLETE_SILENCE and self.audios:
            raise IntentGraphError("explicit complete silence contradicts declared audible intents")
        if self.soundscape is SoundscapeDisposition.DESCRIBED and not any(
            audio.resolved_ownership is AudioOwnership.SOUNDSCAPE
            and self.contributes_to_summary(audio)
            for audio in self.audios
        ):
            raise IntentGraphError(
                "a described soundscape requires one owned soundscape contribution"
            )

    def contributes_to_summary(self, audio: AudioIntent) -> bool:
        """Whole-video items always contribute; timeline items must be linked to a segment."""

        if audio.scope is AudioScope.WHOLE_VIDEO:
            return True
        return any(audio.audio_id in segment.audio_ids for segment in self.segments)

    def complete_final_audio_track_is_consistent(
        self,
        relation_id: str,
        *,
        source_registry: ReferenceRegistry,
        hard_constraints: HardConstraintSet,
    ) -> bool:
        """Whether one typed relation proves unchanged complete final-track reuse."""

        final_relations = tuple(
            relation
            for relation in self.retention
            if relation.domain is RetentionDomain.AUDIO
            and relation.scope is RetentionScope.COMPLETE_FINAL_AUDIO_TRACK
        )
        relation = next((item for item in final_relations if item.relation_id == relation_id), None)
        if (
            relation is None
            or len(final_relations) != 1
            or relation.marker is not AudioRetentionMarker.FULLY_COPY
            or len(relation.source_asset_ids) != 1
        ):
            return False

        contributors = tuple(audio for audio in self.audios if self.contributes_to_summary(audio))
        if len(contributors) != 1:
            return False
        carrier = contributors[0]
        if (
            carrier.audio_id != relation.target_id
            or carrier.source_asset_ids != relation.source_asset_ids
        ):
            return False

        if carrier.scope is AudioScope.TIMELINE:
            if not self.segments or self.segments[0].start.seconds != 0:
                return False
            previous_end = self.segments[0].start.seconds
            for segment in self.segments:
                if (
                    segment.start.seconds != previous_end
                    or carrier.audio_id not in segment.audio_ids
                ):
                    return False
                previous_end = segment.end.seconds
            if previous_end != self.effective_duration.seconds:
                return False

        source = next(
            (
                asset
                for asset in source_registry.assets
                if asset.asset_id == relation.source_asset_ids[0]
            ),
            None,
        )
        if source is None or source.kind is not MediaKind.AUDIO:
            return False
        if (
            source.metadata is not None
            and source.metadata.duration_seconds is not None
            and source.metadata.duration_seconds != self.effective_duration.seconds
        ):
            return False
        if any(event.domain is RetentionDomain.AUDIO for event in self.events):
            return False
        if any(
            other.domain is RetentionDomain.AUDIO and other.relation_id != relation.relation_id
            for other in self.retention
        ):
            return False

        # GUARD: complete final-track reuse is an aggregate typed claim. An explicit audio,
        # dialogue or lyrics CHANGE contradicts 1:1 reuse; KEEP and visual changes do not. Generic
        # ASSET and required/forbidden prose lack a typed join, so guessing from their text would
        # silently rewrite caller authority and let rendering and fidelity disagree again.
        contradictory_targets = {
            DirectiveTarget.AUDIO,
            DirectiveTarget.DIALOGUE,
            DirectiveTarget.LYRICS,
        }
        return not any(
            directive.action is KeepChangeAction.CHANGE
            and directive.target in contradictory_targets
            for directive in hard_constraints.keep_change_directives
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": INTENT_GRAPH_SCHEMA,
            "effective_duration": self.effective_duration.to_wire(),
            "registry": self.registry.to_wire(),
            "subjects": _wire_values(self.subjects),
            "scenes": _wire_values(self.scenes),
            "actions": _wire_values(self.actions),
            "cameras": _wire_values(self.cameras),
            "styles": _wire_values(self.styles),
            "audios": _wire_values(self.audios),
            "events": _wire_values(self.events),
            "retention": _wire_values(self.retention),
            "segments": _wire_values(self.segments),
            "allow_gaps": self.allow_gaps,
            "soundscape": self.soundscape.value,
        }

    def validate(self) -> tuple[ValidationDiagnostic, ...]:
        """Return deterministic cross-reference and timeline diagnostics for this graph."""

        diagnostics = _validate_cross_references(self)
        diagnostics.extend(_validate_timeline(self))
        return tuple(diagnostics)


@dataclass(frozen=True, slots=True)
class IntentGraphResult:
    """Graph plus deterministic diagnostics; errors never return a plausible graph."""

    graph: IntentGraph | None
    diagnostics: tuple[ValidationDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.graph is not None and not isinstance(self.graph, IntentGraph):
            raise ContractValidationError("graph must be an IntentGraph or None")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(diagnostic, ValidationDiagnostic) for diagnostic in self.diagnostics
        ):
            raise ContractValidationError(
                "diagnostics must be a tuple of ValidationDiagnostic values"
            )

    @property
    def has_errors(self) -> bool:
        return any(
            diagnostic.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for diagnostic in self.diagnostics
        )

    @property
    def is_valid(self) -> bool:
        return self.graph is not None and not self.has_errors

    def to_wire(self) -> dict[str, object]:
        return {
            "graph": None if self.graph is None else self.graph.to_wire(),
            "diagnostics": [diagnostic.to_wire() for diagnostic in self.diagnostics],
        }


def _diagnostic(code: str, message: str, location: str | None = None) -> ValidationDiagnostic:
    return ValidationDiagnostic(ValidationSeverity.ERROR, code, message, location)


def _id_map(
    values: tuple[T, ...], field: str, diagnostics: list[ValidationDiagnostic]
) -> dict[str, T]:
    result: dict[str, T] = {}
    family = {
        "subjects": "subject",
        "scenes": "scene",
        "actions": "action",
        "cameras": "camera",
        "styles": "style",
        "audios": "audio",
        "events": "event",
        "segments": "segment",
        "retention relations": "retention",
    }.get(field, field.rstrip("s"))
    for value in values:
        identifier = _value_id(value)
        if identifier in result:
            diagnostics.append(
                _diagnostic(
                    f"duplicate_{family}_id",
                    f"{field} contain duplicate identifier {identifier!r}",
                    field,
                )
            )
        else:
            result[identifier] = value
    return result


def _value_id(value: object) -> str:
    if isinstance(value, IntentSubject):
        return value.subject_id
    if isinstance(value, IntentScene):
        return value.scene_id
    if isinstance(value, IntentAction):
        return value.action_id
    if isinstance(value, CameraIntent):
        return value.camera_id
    if isinstance(value, StyleIntent):
        return value.style_id
    if isinstance(value, AudioIntent):
        return value.audio_id
    if isinstance(value, EventCopy):
        return value.event_id
    if isinstance(value, RetentionRelation):
        return value.relation_id
    if isinstance(value, TimelineSegment):
        return value.segment_id
    raise IntentGraphError("unsupported intent graph value")


def _tuple_input(values: Iterable[T], field: str) -> tuple[T, ...]:
    try:
        result = tuple(values)
    except TypeError as exc:
        raise IntentGraphError(f"{field} must be iterable") from exc
    return result


def _append_unknown_ids(
    identifiers: Iterable[str],
    known: set[str],
    code: str,
    field: str,
    diagnostics: list[ValidationDiagnostic],
) -> None:
    for identifier in identifiers:
        if identifier not in known:
            diagnostics.append(
                _diagnostic(code, f"{field} references unknown identifier {identifier!r}", field)
            )


def _validate_asset_kind(
    asset_id: str,
    registry: ReferenceRegistry,
    domain: RetentionDomain,
    code: str,
    field: str,
    diagnostics: list[ValidationDiagnostic],
) -> None:
    asset = next((item for item in registry.assets if item.asset_id == asset_id), None)
    if asset is None:
        diagnostics.append(_diagnostic("unknown_asset", f"unknown asset {asset_id!r}", field))
        return
    valid_kinds = (
        {MediaKind.IMAGE, MediaKind.VIDEO}
        if domain is RetentionDomain.VISUAL
        else {MediaKind.AUDIO}
    )
    if asset.kind not in valid_kinds:
        diagnostics.append(
            _diagnostic(
                code,
                f"{field} asset {asset_id!r} has incompatible media kind {asset.kind.value!r}",
                field,
            )
        )


def _validate_cross_references(graph: IntentGraph) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    subject_map = _id_map(graph.subjects, "subjects", diagnostics)
    scene_map = _id_map(graph.scenes, "scenes", diagnostics)
    action_map = _id_map(graph.actions, "actions", diagnostics)
    camera_map = _id_map(graph.cameras, "cameras", diagnostics)
    style_map = _id_map(graph.styles, "styles", diagnostics)
    audio_map = _id_map(graph.audios, "audios", diagnostics)
    event_map = _id_map(graph.events, "events", diagnostics)
    segment_map = _id_map(graph.segments, "segments", diagnostics)
    _id_map(graph.retention, "retention relations", diagnostics)
    asset_ids = {asset.asset_id for asset in graph.registry.assets}

    for subject in graph.subjects:
        for asset_id in subject.source_asset_ids:
            if asset_id not in asset_ids:
                diagnostics.append(
                    _diagnostic(
                        "unknown_asset",
                        f"subject references unknown asset {asset_id!r}",
                        "subjects",
                    )
                )
    for scene in graph.scenes:
        _append_unknown_ids(
            scene.subject_ids, set(subject_map), "unknown_subject", "scenes", diagnostics
        )
        if scene.style_id is not None and scene.style_id not in style_map:
            diagnostics.append(
                _diagnostic(
                    "unknown_style", f"scene references unknown style {scene.style_id!r}", "scenes"
                )
            )
    for action in graph.actions:
        _append_unknown_ids(
            action.subject_ids, set(subject_map), "unknown_subject", "actions", diagnostics
        )
        if action.scene_id is not None and action.scene_id not in scene_map:
            diagnostics.append(
                _diagnostic(
                    "unknown_scene",
                    f"action references unknown scene {action.scene_id!r}",
                    "actions",
                )
            )
    for camera in graph.cameras:
        _append_unknown_ids(
            camera.subject_ids, set(subject_map), "unknown_subject", "cameras", diagnostics
        )
        if camera.scene_id is not None and camera.scene_id not in scene_map:
            diagnostics.append(
                _diagnostic(
                    "unknown_scene",
                    f"camera references unknown scene {camera.scene_id!r}",
                    "cameras",
                )
            )
    for audio in graph.audios:
        for asset_id in audio.source_asset_ids:
            if asset_id not in asset_ids:
                diagnostics.append(
                    _diagnostic(
                        "unknown_asset", f"audio references unknown asset {asset_id!r}", "audios"
                    )
                )
            else:
                asset = next(item for item in graph.registry.assets if item.asset_id == asset_id)
                if asset.kind is not MediaKind.AUDIO:
                    diagnostics.append(
                        _diagnostic(
                            "audio_asset_kind_mismatch",
                            f"audio source {asset_id!r} is not an audio asset",
                            "audios",
                        )
                    )
        _append_unknown_ids(
            audio.subject_ids, set(subject_map), "unknown_subject", "audios", diagnostics
        )
    for event in graph.events:
        if event.source_asset_id not in asset_ids:
            diagnostics.append(
                _diagnostic(
                    "unknown_asset",
                    f"event references unknown asset {event.source_asset_id!r}",
                    "events",
                )
            )
        else:
            _validate_asset_kind(
                event.source_asset_id,
                graph.registry,
                event.domain,
                "event_domain_mismatch",
                "events",
                diagnostics,
            )
        if event.target_segment_id not in segment_map:
            diagnostics.append(
                _diagnostic(
                    "unknown_segment",
                    f"event references unknown segment {event.target_segment_id!r}",
                    "events",
                )
            )
    for relation in graph.retention:
        for asset_id in relation.source_asset_ids:
            if asset_id not in asset_ids:
                diagnostics.append(
                    _diagnostic(
                        "unknown_asset",
                        f"retention relation references unknown asset {asset_id!r}",
                        "retention",
                    )
                )
            else:
                _validate_asset_kind(
                    asset_id,
                    graph.registry,
                    relation.domain,
                    "retention_domain_mismatch",
                    "retention",
                    diagnostics,
                )
        # GUARD: a standalone picture/video requires its own declared role. Subject provenance
        # cannot invent a keyframe or editing denotation, nor suppress a separately declared one.
        if relation.scope in {RetentionScope.PICTURE, RetentionScope.VIDEO_STRUCTURE}:
            target_asset = next(
                (item for item in graph.registry.assets if item.asset_id == relation.target_id),
                None,
            )
            if relation.scope is RetentionScope.PICTURE:
                qualified = (
                    target_asset is not None
                    and target_asset.kind is MediaKind.IMAGE
                    and target_asset.role
                    in {
                        AssetRole.FIRST_FRAME,
                        AssetRole.LAST_FRAME,
                    }
                )
            else:
                qualified = (
                    target_asset is not None
                    and target_asset.kind is MediaKind.VIDEO
                    and target_asset.role
                    in {
                        AssetRole.EDITING_SOURCE,
                        AssetRole.CONTINUATION_SOURCE,
                        AssetRole.MOTION_REFERENCE,
                        AssetRole.CAMERA_REFERENCE,
                    }
                )
            if not qualified:
                diagnostics.append(
                    _diagnostic(
                        "retention_target_scope_mismatch",
                        "retention denotation requires an explicitly role-qualified target_asset",
                        "retention",
                    )
                )
            continue
        target_map = subject_map if relation.domain is RetentionDomain.VISUAL else audio_map
        if relation.target_id not in target_map:
            target_name = "subject" if relation.domain is RetentionDomain.VISUAL else "audio"
            diagnostics.append(
                _diagnostic(
                    f"unknown_{target_name}",
                    f"retention relation references unknown {target_name} {relation.target_id!r}",
                    "retention",
                )
            )
    for segment in graph.segments:
        if segment.scene_id is not None and segment.scene_id not in scene_map:
            diagnostics.append(
                _diagnostic(
                    "unknown_scene",
                    f"segment references unknown scene {segment.scene_id!r}",
                    "segments",
                )
            )
        _append_unknown_ids(
            segment.subject_ids, set(subject_map), "unknown_subject", "segments", diagnostics
        )
        _append_unknown_ids(
            segment.action_ids, set(action_map), "unknown_action", "segments", diagnostics
        )
        if segment.camera_id is not None and segment.camera_id not in camera_map:
            diagnostics.append(
                _diagnostic(
                    "unknown_camera",
                    f"segment references unknown camera {segment.camera_id!r}",
                    "segments",
                )
            )
        if segment.style_id is not None and segment.style_id not in style_map:
            diagnostics.append(
                _diagnostic(
                    "unknown_style",
                    f"segment references unknown style {segment.style_id!r}",
                    "segments",
                )
            )
        _append_unknown_ids(
            segment.audio_ids, set(audio_map), "unknown_audio", "segments", diagnostics
        )
        _append_unknown_ids(
            segment.event_ids, set(event_map), "unknown_event", "segments", diagnostics
        )
    return diagnostics


def _validate_timeline(graph: IntentGraph) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    if not graph.segments:
        return [
            _diagnostic(
                "timeline_missing_segment", "timeline must contain at least one segment", "segments"
            )
        ]
    duration = graph.effective_duration.seconds
    first = graph.segments[0]
    if first.start.seconds != 0:
        diagnostics.append(
            _diagnostic(
                "timeline_must_start_at_zero", "first segment must start at zero", "segments"
            )
        )
    previous: TimelineSegment | None = None
    for segment in graph.segments:
        if segment.start.seconds < 0 or segment.end.seconds > duration:
            diagnostics.append(
                _diagnostic(
                    "timeline_out_of_bounds",
                    f"segment {segment.segment_id!r} lies outside effective duration",
                    "segments",
                )
            )
        if previous is not None:
            if segment.start.seconds < previous.start.seconds:
                diagnostics.append(
                    _diagnostic(
                        "timeline_out_of_order",
                        "timeline segments must be ordered by start time",
                        "segments",
                    )
                )
            elif segment.start.seconds < previous.end.seconds:
                diagnostics.append(
                    _diagnostic(
                        "timeline_overlap",
                        f"segment {segment.segment_id!r} overlaps {previous.segment_id!r}",
                        "segments",
                    )
                )
            elif segment.start.seconds > previous.end.seconds and not graph.allow_gaps:
                diagnostics.append(
                    _diagnostic(
                        "timeline_gap",
                        f"timeline gap before segment {segment.segment_id!r}",
                        "segments",
                    )
                )
        previous = segment
    if graph.segments[-1].end.seconds != duration:
        diagnostics.append(
            _diagnostic(
                "timeline_must_end_at_duration",
                "last segment must end at effective duration",
                "segments",
            )
        )
    return diagnostics


def build_intent_graph(
    *,
    effective_duration: TimePoint,
    registry: ReferenceRegistry,
    subjects: Iterable[IntentSubject] = (),
    scenes: Iterable[IntentScene] = (),
    actions: Iterable[IntentAction] = (),
    cameras: Iterable[CameraIntent] = (),
    styles: Iterable[StyleIntent] = (),
    audios: Iterable[AudioIntent] = (),
    events: Iterable[EventCopy] = (),
    retention: Iterable[RetentionRelation] = (),
    segments: Iterable[TimelineSegment] = (),
    allow_gaps: bool = False,
    soundscape: SoundscapeDisposition = SoundscapeDisposition.UNSPECIFIED,
) -> IntentGraphResult:
    """Build and validate a graph without inventing missing nodes or media observations."""

    if not isinstance(effective_duration, TimePoint):
        raise IntentGraphError("effective_duration must be a TimePoint")
    if not isinstance(registry, ReferenceRegistry):
        raise IntentGraphError("registry must be a ReferenceRegistry")
    if not isinstance(allow_gaps, bool):
        raise IntentGraphError("allow_gaps must be a bool")
    if not isinstance(soundscape, SoundscapeDisposition):
        raise IntentGraphError("soundscape must be a SoundscapeDisposition")
    graph = IntentGraph(
        effective_duration=effective_duration,
        registry=registry,
        subjects=_tuple_input(subjects, "subjects"),
        scenes=_tuple_input(scenes, "scenes"),
        actions=_tuple_input(actions, "actions"),
        cameras=_tuple_input(cameras, "cameras"),
        styles=_tuple_input(styles, "styles"),
        audios=_tuple_input(audios, "audios"),
        events=_tuple_input(events, "events"),
        retention=_tuple_input(retention, "retention"),
        segments=_tuple_input(segments, "segments"),
        allow_gaps=allow_gaps,
        soundscape=soundscape,
    )
    diagnostics = list(graph.validate())
    if diagnostics:
        return IntentGraphResult(graph=None, diagnostics=tuple(diagnostics))
    return IntentGraphResult(graph=graph, diagnostics=())


__all__ = [
    "AudioIntent",
    "AudioLayer",
    "AudioOwnership",
    "AudioRetentionMarker",
    "AudioScope",
    "CameraIntent",
    "EventCopy",
    "EventCopyMode",
    "IntentAction",
    "IntentGraph",
    "IntentGraphResult",
    "IntentScene",
    "IntentSubject",
    "RetentionDomain",
    "RetentionMarker",
    "RetentionRelation",
    "RetentionScope",
    "SegmentDevelopment",
    "INTENT_GRAPH_SCHEMA",
    "SoundscapeDisposition",
    "StyleIntent",
    "TimelineSegment",
    "VisualRetentionMarker",
    "build_intent_graph",
]
