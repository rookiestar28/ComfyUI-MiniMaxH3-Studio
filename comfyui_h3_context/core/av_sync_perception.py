"""M12-05 source-owned audiovisual synchronization contracts.

Only bounded, redacted relation descriptors and injected benchmark metadata cross this seam.  Media
decoders, ComfyUI, Ollama, specialist runtimes, and network clients remain outside the pure core.
Source PTS is never replaced by an inferred frame rate or a hidden scalar synchronization score.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from .canonical import canonical_fingerprint
from .errors import AVSyncPerceptionError
from .media_admission import PresentationTimestamp

AV_SYNC_PERCEPTION_SCHEMA = "h3.av.sync.v1"
AV_SYNC_BENCHMARK_SCHEMA = "h3.av.sync_benchmark.v1"
MAX_AV_SYNC_RELATIONS = 96
MAX_AV_SYNC_FRAME_IDS = 16
MAX_AV_SYNC_UNCERTAINTIES = 8
MAX_AV_SYNC_DIAGNOSTICS = 32
MAX_AV_SYNC_OUTPUT_BYTES = 65_536
MAX_AV_SYNC_BENCHMARK_CASES = 8
MAX_AV_SYNC_BENCHMARK_THRESHOLDS = 64
MAX_AV_SYNC_BENCHMARK_CANDIDATES = 8
MAX_AV_SYNC_OFFSET_MILLIS = 120_000

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = (
    "http://",
    "https://",
    "file://",
    "token=",
    "authorization",
    "bearer ",
    "password",
    "secret",
)


class AVSyncStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class AVSyncRoute(str, Enum):
    STATIC_INJECTED = "static_injected"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class AVSyncCandidateFamily(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class AVSyncDisposition(str, Enum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class AVSyncMediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"


class AVSyncSourceRole(str, Enum):
    VIDEO = "video"
    LIP = "lip"
    VISIBLE_ACTION = "visible_action"
    EDIT = "edit"
    SPEECH = "speech"
    SOUND_EVENT = "sound_event"
    MUSIC = "music"
    SOUNDTRACK = "soundtrack"
    DUBBED_VOICE = "dubbed_voice"


class AVSyncRelationKind(str, Enum):
    SPEECH_LIP = "speech_lip"
    ACTION_SOUND = "action_sound"
    SOURCE_VISIBILITY = "source_visibility"
    MUSIC_EDIT = "music_edit"
    VIDEO_SOUNDTRACK = "video_soundtrack"
    DUBBING = "dubbing"


class AVSyncSupport(str, Enum):
    ALIGNED = "aligned"
    DESYNCHRONIZED = "desynchronized"
    UNCERTAIN = "uncertain"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class AVSyncVisibility(str, Enum):
    ON_SCREEN = "on_screen"
    OFF_SCREEN = "off_screen"
    UNKNOWN = "unknown"


class AVSyncUncertaintyKind(str, Enum):
    OFFSET_UNCERTAIN = "offset_uncertain"
    BOUNDARY_UNCERTAIN = "boundary_uncertain"
    CUT = "cut"
    DUBBING = "dubbing"
    OFF_SCREEN = "off_screen"
    OVERLAP = "overlap"
    NO_VISIBLE_SOURCE = "no_visible_source"
    CALIBRATION = "calibration"
    LOW_CONFIDENCE = "low_confidence"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"
    ABSENT = "absent"
    ADVERSARIAL = "adversarial"


class AVSyncCapability(str, Enum):
    SOURCE_PTS = "source_pts"
    TIME_BASE = "time_base"
    SIGNED_OFFSET = "signed_offset"
    OFFSET_INTERVAL = "offset_interval"
    SPEECH_LIP = "speech_lip"
    ACTION_SOUND = "action_sound"
    SOURCE_VISIBILITY = "source_visibility"
    MUSIC_EDIT = "music_edit"
    VIDEO_SOUNDTRACK = "video_soundtrack"
    CUT_BOUNDARY = "cut_boundary"
    DESYNCHRONIZATION = "desynchronization"
    DUBBING = "dubbing"
    OVERLAP = "overlap"
    CALIBRATION = "calibration"
    ABSTENTION = "abstention"
    CORRUPTION = "corruption"
    ADVERSARIAL_METADATA = "adversarial_metadata"


class AVSyncCaseKind(str, Enum):
    CLEAN_SPEECH_LIP = "clean_speech_lip"
    DELIBERATE_DESYNC = "deliberate_desync"
    CUT_BOUNDARY = "cut_boundary"
    OFFSCREEN_SOUND = "offscreen_sound"
    DUBBING = "dubbing"
    OVERLAP_AND_ABSTENTION = "overlap_and_abstention"


class AVSyncMetricKind(str, Enum):
    OFFSET_ERROR = "offset_error"
    BOUNDARY_ERROR = "boundary_error"
    DESYNC_RECALL = "desync_recall"
    VISIBILITY_ACCURACY = "visibility_accuracy"
    DUBBING_RECALL = "dubbing_recall"
    OVERLAP_RECALL = "overlap_recall"
    CALIBRATION_ERROR = "calibration_error"
    ABSTENTION_RATE = "abstention_rate"
    FABRICATION_VIOLATIONS = "fabrication_violations"


class AVSyncMetricUnit(str, Enum):
    MILLISECONDS = "milliseconds"
    BASIS_POINTS = "basis_points"
    COUNT = "count"


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AVSyncPerceptionError(f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in ("/", "\\", *_SENSITIVE)):
        raise AVSyncPerceptionError(f"{field} contains locator or sensitive material")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise AVSyncPerceptionError(f"{field} must be a lower-case bounded code")
    return value.casefold()


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise AVSyncPerceptionError(f"{field} must be a numeric version")
    return value


def _fp(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise AVSyncPerceptionError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AVSyncPerceptionError(f"{field} must be bounded non-empty text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise AVSyncPerceptionError(f"{field} contains a control character")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise AVSyncPerceptionError(f"{field} contains locator or sensitive material")
    return value


def _confidence(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
        raise AVSyncPerceptionError(f"{field} must be a Decimal between 0 and 1")
    return value


def _positive(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise AVSyncPerceptionError(f"{field} must be between 1 and {maximum}")
    return value


def _non_negative(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise AVSyncPerceptionError(f"{field} must be between 0 and {maximum}")
    return value


def _signed_offset(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AVSyncPerceptionError(f"{field} must be an integer")
    if not -MAX_AV_SYNC_OFFSET_MILLIS <= value <= MAX_AV_SYNC_OFFSET_MILLIS:
        raise AVSyncPerceptionError(f"{field} exceeds the finite offset limit")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> Enum:
    try:
        return value if isinstance(value, expected) else expected(value)
    except (TypeError, ValueError):
        raise AVSyncPerceptionError(f"{field} is unsupported") from None


def _span_valid(start: object, end: object, field: str) -> None:
    if not isinstance(start, PresentationTimestamp) or not isinstance(end, PresentationTimestamp):
        raise AVSyncPerceptionError(f"{field} endpoints must be PresentationTimestamp")
    if (start.time_base_num, start.time_base_den) != (end.time_base_num, end.time_base_den):
        raise AVSyncPerceptionError(f"{field} endpoints must share one time base")
    if end.ticks <= start.ticks:
        raise AVSyncPerceptionError(f"{field} end must be after start")


def _uncertainties(values: object, field: str) -> tuple[AVSyncUncertainty, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_AV_SYNC_UNCERTAINTIES:
        raise AVSyncPerceptionError(f"{field} must be a bounded tuple")
    if not all(isinstance(value, AVSyncUncertainty) for value in values):
        raise AVSyncPerceptionError(f"{field} contains an invalid value")
    kinds = tuple(value.kind for value in values)
    if len(kinds) != len(set(kinds)):
        raise AVSyncPerceptionError(f"{field} must not duplicate uncertainty kinds")
    return values


@dataclass(frozen=True, slots=True)
class AVSyncSourceSpan:
    """A source-owned half-open visual or audio interval with integer PTS endpoints."""

    asset_id: str
    source_id: str
    source_fingerprint: str
    media_kind: AVSyncMediaKind | str
    role: AVSyncSourceRole | str
    start: PresentationTimestamp
    end: PresentationTimestamp
    frame_ids: tuple[str, ...] = ()
    shot_id: str | None = None
    schema: str = AV_SYNC_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.asset_id, "span asset_id")
        _id(self.source_id, "span source_id")
        _fp(self.source_fingerprint, "span source_fingerprint")
        object.__setattr__(
            self, "media_kind", _enum(self.media_kind, AVSyncMediaKind, "span media_kind")
        )
        object.__setattr__(self, "role", _enum(self.role, AVSyncSourceRole, "span role"))
        _span_valid(self.start, self.end, "sync span")
        frame_ids = self.frame_ids
        if not isinstance(frame_ids, tuple) or len(frame_ids) > MAX_AV_SYNC_FRAME_IDS:
            raise AVSyncPerceptionError("span frame_ids must be a bounded tuple")
        for frame_id in frame_ids:
            _id(frame_id, "span frame_id")
        if len(set(frame_ids)) != len(frame_ids):
            raise AVSyncPerceptionError("span frame_ids must be unique")
        if self.shot_id is not None:
            _id(self.shot_id, "span shot_id")
        media_kind = cast(AVSyncMediaKind, self.media_kind)
        role = cast(AVSyncSourceRole, self.role)
        video_roles = {
            AVSyncSourceRole.VIDEO,
            AVSyncSourceRole.LIP,
            AVSyncSourceRole.VISIBLE_ACTION,
            AVSyncSourceRole.EDIT,
        }
        audio_roles = {
            AVSyncSourceRole.SPEECH,
            AVSyncSourceRole.SOUND_EVENT,
            AVSyncSourceRole.MUSIC,
            AVSyncSourceRole.SOUNDTRACK,
            AVSyncSourceRole.DUBBED_VOICE,
        }
        if (media_kind is AVSyncMediaKind.VIDEO and role not in video_roles) or (
            media_kind is AVSyncMediaKind.AUDIO and role not in audio_roles
        ):
            raise AVSyncPerceptionError("span media_kind and role are incompatible")
        if self.schema != AV_SYNC_PERCEPTION_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual sync schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "media_kind": cast(AVSyncMediaKind, self.media_kind).value,
            "role": cast(AVSyncSourceRole, self.role).value,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "frame_ids": list(self.frame_ids),
            "shot_id": self.shot_id,
        }


@dataclass(frozen=True, slots=True)
class AVSyncOffsetInterval:
    """Signed video-minus-audio offset in an explicit 1/1000 rational time base."""

    minimum_millis: int
    maximum_millis: int
    time_base_num: int = 1
    time_base_den: int = 1000
    direction: str = "video_minus_audio"
    schema: str = AV_SYNC_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _signed_offset(self.minimum_millis, "offset minimum_millis")
        _signed_offset(self.maximum_millis, "offset maximum_millis")
        if self.minimum_millis > self.maximum_millis:
            raise AVSyncPerceptionError("offset interval minimum must not exceed maximum")
        if self.time_base_num != 1 or self.time_base_den != 1000:
            raise AVSyncPerceptionError("offset interval must use the 1/1000 rational time base")
        if self.direction != "video_minus_audio":
            raise AVSyncPerceptionError("offset direction must be video_minus_audio")
        if self.schema != AV_SYNC_PERCEPTION_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual sync schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "minimum_millis": self.minimum_millis,
            "maximum_millis": self.maximum_millis,
            "time_base_num": self.time_base_num,
            "time_base_den": self.time_base_den,
            "direction": self.direction,
        }


@dataclass(frozen=True, slots=True)
class AVSyncRequest:
    video_asset_id: str
    video_source_id: str
    video_source_fingerprint: str
    video_duration_end: PresentationTimestamp
    audio_asset_id: str
    audio_source_id: str
    audio_source_fingerprint: str
    audio_duration_end: PresentationTimestamp
    preprocessing_fingerprint: str
    route: AVSyncRoute | str = AVSyncRoute.STATIC_INJECTED
    max_relations: int = MAX_AV_SYNC_RELATIONS
    schema: str = AV_SYNC_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.video_asset_id, "request video_asset_id")
        _id(self.video_source_id, "request video_source_id")
        _fp(self.video_source_fingerprint, "request video_source_fingerprint")
        _id(self.audio_asset_id, "request audio_asset_id")
        _id(self.audio_source_id, "request audio_source_id")
        _fp(self.audio_source_fingerprint, "request audio_source_fingerprint")
        _fp(self.preprocessing_fingerprint, "request preprocessing_fingerprint")
        for value, field in (
            (self.video_duration_end, "video_duration_end"),
            (self.audio_duration_end, "audio_duration_end"),
        ):
            if not isinstance(value, PresentationTimestamp) or value.ticks <= 0:
                raise AVSyncPerceptionError(f"request {field} must be positive")
        if (
            self.video_asset_id == self.audio_asset_id
            and self.video_source_id == self.audio_source_id
        ):
            raise AVSyncPerceptionError("video and audio request sources must be distinguishable")
        object.__setattr__(self, "route", _enum(self.route, AVSyncRoute, "request route"))
        _positive(self.max_relations, "request max_relations", MAX_AV_SYNC_RELATIONS)
        if self.schema != AV_SYNC_PERCEPTION_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual sync schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "video_asset_id": self.video_asset_id,
            "video_source_id": self.video_source_id,
            "video_source_fingerprint": self.video_source_fingerprint,
            "video_duration_end": self.video_duration_end.to_wire(),
            "audio_asset_id": self.audio_asset_id,
            "audio_source_id": self.audio_source_id,
            "audio_source_fingerprint": self.audio_source_fingerprint,
            "audio_duration_end": self.audio_duration_end.to_wire(),
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "route": cast(AVSyncRoute, self.route).value,
            "max_relations": self.max_relations,
        }


@dataclass(frozen=True, slots=True)
class AVSyncUncertainty:
    kind: AVSyncUncertaintyKind | str
    detail: str
    schema: str = AV_SYNC_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "kind", _enum(self.kind, AVSyncUncertaintyKind, "uncertainty kind")
        )
        _text(self.detail, "uncertainty detail")
        if self.schema != AV_SYNC_PERCEPTION_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual sync schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "kind": cast(AVSyncUncertaintyKind, self.kind).value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AVSyncRelation:
    """One explicit audiovisual relationship with source grounding and uncertainty."""

    relation_id: str
    kind: AVSyncRelationKind | str
    support: AVSyncSupport | str
    visual_span: AVSyncSourceSpan | None
    audio_span: AVSyncSourceSpan | None
    offset: AVSyncOffsetInterval | None
    confidence: object
    boundary_confidence: object
    visibility: AVSyncVisibility | str = AVSyncVisibility.UNKNOWN
    uncertainties: tuple[AVSyncUncertainty, ...] = ()
    overlap_group: str | None = None
    label: str | None = None
    schema: str = AV_SYNC_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.relation_id, "relation_id")
        object.__setattr__(self, "kind", _enum(self.kind, AVSyncRelationKind, "relation kind"))
        object.__setattr__(self, "support", _enum(self.support, AVSyncSupport, "relation support"))
        object.__setattr__(
            self, "visibility", _enum(self.visibility, AVSyncVisibility, "relation visibility")
        )
        if self.visual_span is not None and not isinstance(self.visual_span, AVSyncSourceSpan):
            raise AVSyncPerceptionError("visual_span is invalid")
        if self.audio_span is not None and not isinstance(self.audio_span, AVSyncSourceSpan):
            raise AVSyncPerceptionError("audio_span is invalid")
        if self.offset is not None and not isinstance(self.offset, AVSyncOffsetInterval):
            raise AVSyncPerceptionError("relation offset is invalid")
        _confidence(self.confidence, "relation confidence")
        _confidence(self.boundary_confidence, "relation boundary_confidence")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "relation uncertainties")
        )
        kind = cast(AVSyncRelationKind, self.kind)
        support = cast(AVSyncSupport, self.support)
        visibility = cast(AVSyncVisibility, self.visibility)
        if kind is not AVSyncRelationKind.SOURCE_VISIBILITY and (
            self.visual_span is None or self.audio_span is None
        ):
            raise AVSyncPerceptionError("this relation kind requires visual and audio spans")
        if (
            kind is AVSyncRelationKind.SOURCE_VISIBILITY
            and self.visual_span is None
            and self.audio_span is None
        ):
            raise AVSyncPerceptionError("source_visibility requires at least one source span")
        if support in {AVSyncSupport.ALIGNED, AVSyncSupport.DESYNCHRONIZED}:
            if self.visual_span is None or self.audio_span is None or self.offset is None:
                raise AVSyncPerceptionError(
                    "aligned/desynchronized relation requires two spans and offset"
                )
            if self.confidence is None:
                raise AVSyncPerceptionError("aligned/desynchronized relation requires confidence")
        if support in {AVSyncSupport.UNKNOWN, AVSyncSupport.ABSENT}:
            if self.offset is not None or self.label is not None:
                raise AVSyncPerceptionError(
                    "unknown/absent relation cannot fabricate alignment output"
                )
            required_kind = (
                AVSyncUncertaintyKind.UNKNOWN
                if support is AVSyncSupport.UNKNOWN
                else AVSyncUncertaintyKind.ABSENT
            )
            if not any(value.kind is required_kind for value in self.uncertainties):
                raise AVSyncPerceptionError("unknown/absent relation requires matching uncertainty")
        if self.offset is not None and (self.visual_span is None or self.audio_span is None):
            raise AVSyncPerceptionError("offset requires both visual and audio spans")
        if visibility is AVSyncVisibility.ON_SCREEN and self.visual_span is None:
            raise AVSyncPerceptionError("on-screen relation requires visual source")
        if visibility is AVSyncVisibility.OFF_SCREEN and self.visual_span is not None:
            raise AVSyncPerceptionError("off-screen relation cannot claim a visual source")
        if support is AVSyncSupport.DESYNCHRONIZED and self.offset is not None:
            if -100 < self.offset.minimum_millis and self.offset.maximum_millis < 100:
                raise AVSyncPerceptionError(
                    "desynchronized relation offset is within alignment tolerance"
                )
        if self.overlap_group is not None:
            _id(self.overlap_group, "relation overlap_group")
        if self.label is not None:
            _text(self.label, "relation label", 256)
        if kind is AVSyncRelationKind.DUBBING and not any(
            value.kind is AVSyncUncertaintyKind.DUBBING for value in self.uncertainties
        ):
            raise AVSyncPerceptionError("dubbing relation requires dubbing uncertainty")
        if self.schema != AV_SYNC_PERCEPTION_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual sync schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "relation_id": self.relation_id,
            "kind": cast(AVSyncRelationKind, self.kind).value,
            "support": cast(AVSyncSupport, self.support).value,
            "visual_span": None if self.visual_span is None else self.visual_span.to_wire(),
            "audio_span": None if self.audio_span is None else self.audio_span.to_wire(),
            "offset": None if self.offset is None else self.offset.to_wire(),
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "boundary_confidence": (
                None if self.boundary_confidence is None else format(self.boundary_confidence, "f")
            ),
            "visibility": cast(AVSyncVisibility, self.visibility).value,
            "uncertainties": [value.to_wire() for value in self.uncertainties],
            "overlap_group": self.overlap_group,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class AVSyncReceipt:
    route: AVSyncRoute | str
    adapter_id: str
    adapter_version: str
    model_id: str
    model_fingerprint: str
    video_source_fingerprint: str
    audio_source_fingerprint: str
    preprocessing_fingerprint: str
    network_contacted: bool = False
    decoder_started: bool = False
    schema: str = AV_SYNC_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _enum(self.route, AVSyncRoute, "receipt route"))
        _code(self.adapter_id, "receipt adapter_id")
        _version(self.adapter_version, "receipt adapter_version")
        _id(self.model_id, "receipt model_id")
        _fp(self.model_fingerprint, "receipt model_fingerprint")
        _fp(self.video_source_fingerprint, "receipt video_source_fingerprint")
        _fp(self.audio_source_fingerprint, "receipt audio_source_fingerprint")
        _fp(self.preprocessing_fingerprint, "receipt preprocessing_fingerprint")
        if not isinstance(self.network_contacted, bool) or not isinstance(
            self.decoder_started, bool
        ):
            raise AVSyncPerceptionError("receipt flags must be boolean")
        if cast(AVSyncRoute, self.route) is AVSyncRoute.STATIC_INJECTED and (
            self.network_contacted or self.decoder_started
        ):
            raise AVSyncPerceptionError(
                "static injected receipt cannot contact network or start decoder"
            )
        if self.schema != AV_SYNC_PERCEPTION_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual sync schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "route": cast(AVSyncRoute, self.route).value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "model_fingerprint": self.model_fingerprint,
            "video_source_fingerprint": self.video_source_fingerprint,
            "audio_source_fingerprint": self.audio_source_fingerprint,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "network_contacted": self.network_contacted,
            "decoder_started": self.decoder_started,
        }


@dataclass(frozen=True, slots=True)
class AVSyncDocument:
    document_id: str
    status: AVSyncStatus | str
    request: AVSyncRequest
    relations: tuple[AVSyncRelation, ...] = ()
    receipt: AVSyncReceipt | None = None
    diagnostics: tuple[str, ...] = ()
    schema: str = AV_SYNC_PERCEPTION_SCHEMA

    def __post_init__(self) -> None:
        _id(self.document_id, "document_id")
        object.__setattr__(self, "status", _enum(self.status, AVSyncStatus, "document status"))
        if not isinstance(self.request, AVSyncRequest):
            raise AVSyncPerceptionError("document request must be AVSyncRequest")
        if (
            not isinstance(self.relations, tuple)
            or len(self.relations) > self.request.max_relations
        ):
            raise AVSyncPerceptionError("document relations exceed request limit")
        if not all(isinstance(value, AVSyncRelation) for value in self.relations):
            raise AVSyncPerceptionError("document relations contain an invalid value")
        if len({value.relation_id for value in self.relations}) != len(self.relations):
            raise AVSyncPerceptionError("relation IDs must be unique")
        previous_key: tuple[Decimal, str] | None = None
        for relation in self.relations:
            self._validate_relation_sources(relation)
            current_key = self._relation_key(relation)
            if previous_key is not None and current_key < previous_key:
                raise AVSyncPerceptionError(
                    "relations must be deterministically ordered by source PTS"
                )
            previous_key = current_key
        for index, left in enumerate(self.relations):
            for right in self.relations[index + 1 :]:
                if self._overlaps(left.visual_span, right.visual_span) or self._overlaps(
                    left.audio_span, right.audio_span
                ):
                    if not left.overlap_group or left.overlap_group != right.overlap_group:
                        raise AVSyncPerceptionError(
                            "overlapping relation claims require one explicit group"
                        )
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > MAX_AV_SYNC_DIAGNOSTICS
        ):
            raise AVSyncPerceptionError("document diagnostics exceed the finite limit")
        for diagnostic in self.diagnostics:
            _text(diagnostic, "diagnostic")
        if self.receipt is not None and not isinstance(self.receipt, AVSyncReceipt):
            raise AVSyncPerceptionError("document receipt is invalid")
        terminal = {
            AVSyncStatus.EMPTY,
            AVSyncStatus.CORRUPT,
            AVSyncStatus.UNSUPPORTED,
            AVSyncStatus.CANCELLED,
        }
        if self.status in terminal and (self.relations or self.receipt is not None):
            raise AVSyncPerceptionError(
                "terminal document cannot contain relation output or receipt"
            )
        if self.status is AVSyncStatus.COMPLETE:
            if not self.relations or self.receipt is None:
                raise AVSyncPerceptionError("complete sync document requires relations and receipt")
            if cast(AVSyncRoute, self.receipt.route) is not cast(AVSyncRoute, self.request.route):
                raise AVSyncPerceptionError("receipt route does not match request")
            if self.receipt.video_source_fingerprint != self.request.video_source_fingerprint:
                raise AVSyncPerceptionError("receipt video fingerprint does not match request")
            if self.receipt.audio_source_fingerprint != self.request.audio_source_fingerprint:
                raise AVSyncPerceptionError("receipt audio fingerprint does not match request")
            if self.receipt.preprocessing_fingerprint != self.request.preprocessing_fingerprint:
                raise AVSyncPerceptionError(
                    "receipt preprocessing fingerprint does not match request"
                )
        if self.schema != AV_SYNC_PERCEPTION_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual sync schema")
        if len(self.to_wire_bytes()) > MAX_AV_SYNC_OUTPUT_BYTES:
            raise AVSyncPerceptionError("audiovisual sync document exceeds output limit")

    @staticmethod
    def _relation_key(relation: AVSyncRelation) -> tuple[Decimal, str]:
        span = relation.visual_span or relation.audio_span
        if span is None:
            return Decimal("0"), relation.relation_id
        return span.start.seconds, relation.relation_id

    @staticmethod
    def _overlaps(left: AVSyncSourceSpan | None, right: AVSyncSourceSpan | None) -> bool:
        if left is None or right is None:
            return False
        if (left.asset_id, left.source_id) != (right.asset_id, right.source_id):
            return False
        if (left.start.time_base_num, left.start.time_base_den) != (
            right.start.time_base_num,
            right.start.time_base_den,
        ):
            return left.start.seconds < right.end.seconds and right.start.seconds < left.end.seconds
        return left.start.ticks < right.end.ticks and right.start.ticks < left.end.ticks

    def _validate_relation_sources(self, relation: AVSyncRelation) -> None:
        for span in (relation.visual_span, relation.audio_span):
            if span is None:
                continue
            if span.media_kind is AVSyncMediaKind.VIDEO:
                if (span.asset_id, span.source_id, span.source_fingerprint) != (
                    self.request.video_asset_id,
                    self.request.video_source_id,
                    self.request.video_source_fingerprint,
                ):
                    raise AVSyncPerceptionError("visual source ownership differs from request")
                if span.end.ticks > self.request.video_duration_end.ticks:
                    raise AVSyncPerceptionError("visual span exceeds request duration")
                if (span.end.time_base_num, span.end.time_base_den) != (
                    self.request.video_duration_end.time_base_num,
                    self.request.video_duration_end.time_base_den,
                ):
                    raise AVSyncPerceptionError("visual span time base differs from request")
            else:
                if (span.asset_id, span.source_id, span.source_fingerprint) != (
                    self.request.audio_asset_id,
                    self.request.audio_source_id,
                    self.request.audio_source_fingerprint,
                ):
                    raise AVSyncPerceptionError("audio source ownership differs from request")
                if span.end.ticks > self.request.audio_duration_end.ticks:
                    raise AVSyncPerceptionError("audio span exceeds request duration")
                if (span.end.time_base_num, span.end.time_base_den) != (
                    self.request.audio_duration_end.time_base_num,
                    self.request.audio_duration_end.time_base_den,
                ):
                    raise AVSyncPerceptionError("audio span time base differs from request")
        if (
            relation.visual_span is not None
            and relation.visual_span.media_kind is not AVSyncMediaKind.VIDEO
        ):
            raise AVSyncPerceptionError("visual_span must be video")
        if (
            relation.audio_span is not None
            and relation.audio_span.media_kind is not AVSyncMediaKind.AUDIO
        ):
            raise AVSyncPerceptionError("audio_span must be audio")

    @property
    def complete(self) -> bool:
        return self.status is AVSyncStatus.COMPLETE

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": cast(AVSyncStatus, self.status).value,
            "request": self.request.to_wire(),
            "relations": [value.to_wire() for value in self.relations],
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()


@runtime_checkable
class AVSyncCancellationProbe(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the caller requested cancellation."""


AVSyncProducer = Callable[[AVSyncRequest], AVSyncDocument]


def execute_av_sync(
    producer: AVSyncProducer,
    request: AVSyncRequest,
    *,
    cancellation_probe: AVSyncCancellationProbe | None = None,
) -> AVSyncDocument:
    """Run one explicitly injected producer without process/network/provider discovery."""

    if not callable(producer):
        raise AVSyncPerceptionError("audiovisual sync producer must be callable")
    if not isinstance(request, AVSyncRequest):
        raise AVSyncPerceptionError("request must be AVSyncRequest")
    if cancellation_probe is not None and (
        not isinstance(cancellation_probe, AVSyncCancellationProbe)
        or cancellation_probe.is_cancelled()
    ):
        raise AVSyncPerceptionError("audiovisual sync execution cancelled")
    document = producer(request)
    if not isinstance(document, AVSyncDocument):
        raise AVSyncPerceptionError("producer returned an invalid audiovisual sync document")
    if cancellation_probe is not None and cancellation_probe.is_cancelled():
        raise AVSyncPerceptionError("audiovisual sync execution cancelled")
    return document


def build_av_sync_abstention(
    request: AVSyncRequest, status: AVSyncStatus | str, diagnostic: str
) -> AVSyncDocument:
    status_value = _enum(status, AVSyncStatus, "abstention status")
    if status_value not in {
        AVSyncStatus.EMPTY,
        AVSyncStatus.CORRUPT,
        AVSyncStatus.UNSUPPORTED,
        AVSyncStatus.CANCELLED,
    }:
        raise AVSyncPerceptionError("abstention status must be terminal")
    if not isinstance(request, AVSyncRequest):
        raise AVSyncPerceptionError("request must be AVSyncRequest")
    return AVSyncDocument(
        f"abstention.{cast(AVSyncStatus, status_value).value}",
        cast(AVSyncStatus, status_value),
        request,
        diagnostics=(diagnostic,),
    )


@dataclass(frozen=True, slots=True)
class AVSyncBenchmarkFixture:
    case_id: str
    kind: AVSyncCaseKind | str
    capabilities: tuple[AVSyncCapability, ...]
    source_fingerprint: str
    annotation_fingerprint: str
    expected_status: AVSyncStatus | str
    should_abstain: bool
    offset_tolerance_millis: int
    boundary_tolerance_millis: int
    tags: tuple[str, ...] = ()
    schema: str = AV_SYNC_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.case_id, "fixture case_id")
        object.__setattr__(self, "kind", _enum(self.kind, AVSyncCaseKind, "fixture kind"))
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise AVSyncPerceptionError("fixture capabilities must be non-empty")
        if not all(isinstance(value, AVSyncCapability) for value in self.capabilities):
            raise AVSyncPerceptionError("fixture capabilities contain an invalid value")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise AVSyncPerceptionError("fixture capabilities must be unique")
        _fp(self.source_fingerprint, "fixture source_fingerprint")
        _fp(self.annotation_fingerprint, "fixture annotation_fingerprint")
        object.__setattr__(
            self, "expected_status", _enum(self.expected_status, AVSyncStatus, "fixture status")
        )
        if not isinstance(self.should_abstain, bool):
            raise AVSyncPerceptionError("fixture should_abstain must be boolean")
        _non_negative(
            self.offset_tolerance_millis,
            "fixture offset_tolerance_millis",
            MAX_AV_SYNC_OFFSET_MILLIS,
        )
        _non_negative(
            self.boundary_tolerance_millis,
            "fixture boundary_tolerance_millis",
            MAX_AV_SYNC_OFFSET_MILLIS,
        )
        if not isinstance(self.tags, tuple) or len(self.tags) > 16:
            raise AVSyncPerceptionError("fixture tags must be bounded")
        for tag in self.tags:
            _code(tag, "fixture tag")
        if self.schema != AV_SYNC_BENCHMARK_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "kind": cast(AVSyncCaseKind, self.kind).value,
            "capabilities": [value.value for value in self.capabilities],
            "source_fingerprint": self.source_fingerprint,
            "annotation_fingerprint": self.annotation_fingerprint,
            "expected_status": cast(AVSyncStatus, self.expected_status).value,
            "should_abstain": self.should_abstain,
            "offset_tolerance_millis": self.offset_tolerance_millis,
            "boundary_tolerance_millis": self.boundary_tolerance_millis,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class AVSyncBenchmarkThreshold:
    metric_id: str
    metric: AVSyncMetricKind | str
    capability: AVSyncCapability
    unit: AVSyncMetricUnit | str
    minimum: int | None = None
    maximum: int | None = None
    schema: str = AV_SYNC_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.metric_id, "threshold metric_id")
        object.__setattr__(self, "metric", _enum(self.metric, AVSyncMetricKind, "threshold metric"))
        if not isinstance(self.capability, AVSyncCapability):
            raise AVSyncPerceptionError("threshold capability must be AVSyncCapability")
        object.__setattr__(self, "unit", _enum(self.unit, AVSyncMetricUnit, "threshold unit"))
        if (self.minimum is None) == (self.maximum is None):
            raise AVSyncPerceptionError("threshold requires exactly one bound")
        value = self.minimum if self.minimum is not None else self.maximum
        if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AVSyncPerceptionError("threshold bound must be a non-negative integer")
        if self.schema != AV_SYNC_BENCHMARK_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "metric_id": self.metric_id,
            "metric": cast(AVSyncMetricKind, self.metric).value,
            "capability": self.capability.value,
            "unit": cast(AVSyncMetricUnit, self.unit).value,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class AVSyncCandidateProfile:
    candidate_id: str
    family: AVSyncCandidateFamily | str
    adapter_id: str
    adapter_version: str
    model_id: str
    rights_status: str
    capabilities: tuple[AVSyncCapability, ...]
    requires_network: bool
    supports_determinism: bool
    supports_cancellation_cleanup: bool
    disposition: AVSyncDisposition | str
    disposition_reason: str
    schema: str = AV_SYNC_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.candidate_id, "candidate_id")
        object.__setattr__(
            self, "family", _enum(self.family, AVSyncCandidateFamily, "candidate family")
        )
        _code(self.adapter_id, "candidate adapter_id")
        _version(self.adapter_version, "candidate adapter_version")
        _id(self.model_id, "candidate model_id")
        _code(self.rights_status, "candidate rights_status")
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise AVSyncPerceptionError("candidate capabilities must be non-empty")
        if not all(isinstance(value, AVSyncCapability) for value in self.capabilities):
            raise AVSyncPerceptionError("candidate capabilities contain an invalid value")
        if not all(
            isinstance(value, bool)
            for value in (
                self.requires_network,
                self.supports_determinism,
                self.supports_cancellation_cleanup,
            )
        ):
            raise AVSyncPerceptionError("candidate flags must be boolean")
        object.__setattr__(
            self, "disposition", _enum(self.disposition, AVSyncDisposition, "candidate disposition")
        )
        _text(self.disposition_reason, "candidate disposition_reason")
        if self.family is AVSyncCandidateFamily.OLLAMA and not self.requires_network:
            raise AVSyncPerceptionError("Ollama candidate must disclose loopback transport")
        if self.schema != AV_SYNC_BENCHMARK_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "family": cast(AVSyncCandidateFamily, self.family).value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "rights_status": self.rights_status,
            "capabilities": [value.value for value in self.capabilities],
            "requires_network": self.requires_network,
            "supports_determinism": self.supports_determinism,
            "supports_cancellation_cleanup": self.supports_cancellation_cleanup,
            "disposition": cast(AVSyncDisposition, self.disposition).value,
            "disposition_reason": self.disposition_reason,
        }


@dataclass(frozen=True, slots=True)
class AVSyncBenchmarkLimits:
    max_cases: int
    max_relations: int
    max_spans: int
    max_wall_time_seconds: int
    max_total_compute_seconds: int
    max_peak_vram_mb: int
    max_peak_ram_mb: int
    max_output_bytes: int
    max_concurrency: int
    network_allowed: bool = False
    media_upload_allowed: bool = False
    schema: str = AV_SYNC_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        for value, field in (
            (self.max_cases, "limits max_cases"),
            (self.max_relations, "limits max_relations"),
            (self.max_spans, "limits max_spans"),
            (self.max_wall_time_seconds, "limits max_wall_time_seconds"),
            (self.max_total_compute_seconds, "limits max_total_compute_seconds"),
            (self.max_peak_vram_mb, "limits max_peak_vram_mb"),
            (self.max_peak_ram_mb, "limits max_peak_ram_mb"),
            (self.max_output_bytes, "limits max_output_bytes"),
            (self.max_concurrency, "limits max_concurrency"),
        ):
            _positive(value, field, 1_000_000_000)
        if self.network_allowed or self.media_upload_allowed:
            raise AVSyncPerceptionError(
                "offline audiovisual benchmark cannot allow network or upload"
            )
        if self.schema != AV_SYNC_BENCHMARK_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_cases": self.max_cases,
            "max_relations": self.max_relations,
            "max_spans": self.max_spans,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_total_compute_seconds": self.max_total_compute_seconds,
            "max_peak_vram_mb": self.max_peak_vram_mb,
            "max_peak_ram_mb": self.max_peak_ram_mb,
            "max_output_bytes": self.max_output_bytes,
            "max_concurrency": self.max_concurrency,
            "network_allowed": self.network_allowed,
            "media_upload_allowed": self.media_upload_allowed,
        }


@dataclass(frozen=True, slots=True)
class AVSyncRoutingPolicy:
    preference_order: tuple[AVSyncCandidateFamily, ...]
    automatic_fallback: bool = False
    explicit_selection_required: bool = True
    schema: str = AV_SYNC_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.preference_order, tuple) or tuple(self.preference_order) != tuple(
            AVSyncCandidateFamily
        ):
            raise AVSyncPerceptionError(
                "routing must disclose native, Ollama, and specialist order"
            )
        if self.automatic_fallback or not self.explicit_selection_required:
            raise AVSyncPerceptionError(
                "audiovisual routing requires explicit selection and no fallback"
            )
        if self.schema != AV_SYNC_BENCHMARK_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "preference_order": [value.value for value in self.preference_order],
            "automatic_fallback": self.automatic_fallback,
            "explicit_selection_required": self.explicit_selection_required,
        }


@dataclass(frozen=True, slots=True)
class AVSyncBenchmarkPlan:
    plan_id: str
    plan_version: str
    fixtures: tuple[AVSyncBenchmarkFixture, ...]
    thresholds: tuple[AVSyncBenchmarkThreshold, ...]
    candidates: tuple[AVSyncCandidateProfile, ...]
    limits: AVSyncBenchmarkLimits
    routing: AVSyncRoutingPolicy
    schema: str = AV_SYNC_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _id(self.plan_id, "plan_id")
        _version(self.plan_version, "plan_version")
        if (
            not isinstance(self.fixtures, tuple)
            or not self.fixtures
            or len(self.fixtures) > MAX_AV_SYNC_BENCHMARK_CASES
        ):
            raise AVSyncPerceptionError("plan fixtures must be bounded and non-empty")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or len(self.thresholds) > MAX_AV_SYNC_BENCHMARK_THRESHOLDS
        ):
            raise AVSyncPerceptionError("plan thresholds must be bounded and non-empty")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or len(self.candidates) > MAX_AV_SYNC_BENCHMARK_CANDIDATES
        ):
            raise AVSyncPerceptionError("plan candidates must be bounded and non-empty")
        if not all(isinstance(value, AVSyncBenchmarkFixture) for value in self.fixtures):
            raise AVSyncPerceptionError("plan fixtures contain an invalid value")
        if not all(isinstance(value, AVSyncBenchmarkThreshold) for value in self.thresholds):
            raise AVSyncPerceptionError("plan thresholds contain an invalid value")
        if not all(isinstance(value, AVSyncCandidateProfile) for value in self.candidates):
            raise AVSyncPerceptionError("plan candidates contain an invalid value")
        if len({value.case_id for value in self.fixtures}) != len(self.fixtures):
            raise AVSyncPerceptionError("fixture IDs must be unique")
        if len({value.metric_id for value in self.thresholds}) != len(self.thresholds):
            raise AVSyncPerceptionError("threshold IDs must be unique")
        if len({value.candidate_id for value in self.candidates}) != len(self.candidates):
            raise AVSyncPerceptionError("candidate IDs must be unique")
        covered = {capability for fixture in self.fixtures for capability in fixture.capabilities}
        if covered != set(AVSyncCapability):
            raise AVSyncPerceptionError("audiovisual fixture capability coverage is incomplete")
        threshold_capabilities = {value.capability for value in self.thresholds}
        if threshold_capabilities != set(AVSyncCapability):
            raise AVSyncPerceptionError("every audiovisual capability requires a frozen threshold")
        if len(self.fixtures) > self.limits.max_cases:
            raise AVSyncPerceptionError("fixtures exceed frozen limits")
        if {value.family for value in self.candidates} != set(AVSyncCandidateFamily):
            raise AVSyncPerceptionError(
                "plan must represent native, Ollama, and specialist families"
            )
        if self.schema != AV_SYNC_BENCHMARK_SCHEMA:
            raise AVSyncPerceptionError("unsupported audiovisual benchmark schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def executable_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            value.candidate_id
            for value in self.candidates
            if value.disposition is AVSyncDisposition.QUALIFIED
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "fixtures": [value.to_wire() for value in self.fixtures],
            "thresholds": [value.to_wire() for value in self.thresholds],
            "candidates": [value.to_wire() for value in self.candidates],
            "limits": self.limits.to_wire(),
            "routing": self.routing.to_wire(),
        }

    def to_public_summary(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.fingerprint,
            "case_count": len(self.fixtures),
            "capability_count": len(AVSyncCapability),
            "threshold_count": len(self.thresholds),
            "candidate_count": len(self.candidates),
            "executable_candidate_ids": list(self.executable_candidate_ids),
            "dispositions": {
                disposition.value: sum(
                    value.disposition is disposition for value in self.candidates
                )
                for disposition in AVSyncDisposition
            },
            "automatic_fallback": self.routing.automatic_fallback,
            "claim_ceiling": "structural_only",
        }


def _seed(value: str) -> str:
    return canonical_fingerprint({"av_sync_fixture": value})


def build_default_av_sync_benchmark_plan() -> AVSyncBenchmarkPlan:
    """Build the frozen metadata-only audiovisual synchronization benchmark."""

    capability_groups = (
        (
            AVSyncCaseKind.CLEAN_SPEECH_LIP,
            (
                AVSyncCapability.SOURCE_PTS,
                AVSyncCapability.TIME_BASE,
                AVSyncCapability.SIGNED_OFFSET,
                AVSyncCapability.OFFSET_INTERVAL,
                AVSyncCapability.SPEECH_LIP,
            ),
            ("speech", "lip", "aligned"),
        ),
        (
            AVSyncCaseKind.DELIBERATE_DESYNC,
            (AVSyncCapability.DESYNCHRONIZATION, AVSyncCapability.CALIBRATION),
            ("desync", "negative_offset"),
        ),
        (
            AVSyncCaseKind.CUT_BOUNDARY,
            (
                AVSyncCapability.CUT_BOUNDARY,
                AVSyncCapability.ACTION_SOUND,
                AVSyncCapability.MUSIC_EDIT,
            ),
            ("cut", "action", "edit"),
        ),
        (
            AVSyncCaseKind.OFFSCREEN_SOUND,
            (AVSyncCapability.SOURCE_VISIBILITY, AVSyncCapability.VIDEO_SOUNDTRACK),
            ("off_screen", "soundtrack"),
        ),
        (
            AVSyncCaseKind.DUBBING,
            (AVSyncCapability.DUBBING,),
            ("dubbing", "voice"),
        ),
        (
            AVSyncCaseKind.OVERLAP_AND_ABSTENTION,
            (
                AVSyncCapability.OVERLAP,
                AVSyncCapability.ABSTENTION,
                AVSyncCapability.CORRUPTION,
                AVSyncCapability.ADVERSARIAL_METADATA,
            ),
            ("overlap", "unknown", "corrupt"),
        ),
    )
    fixtures = tuple(
        AVSyncBenchmarkFixture(
            f"av_sync.{kind.value}",
            kind,
            capabilities,
            _seed(f"{kind.value}.source"),
            _seed(f"{kind.value}.annotation"),
            AVSyncStatus.PARTIAL
            if kind is AVSyncCaseKind.OVERLAP_AND_ABSTENTION
            else AVSyncStatus.COMPLETE,
            kind is AVSyncCaseKind.OVERLAP_AND_ABSTENTION,
            50 if kind is AVSyncCaseKind.CLEAN_SPEECH_LIP else 100,
            80 if kind is AVSyncCaseKind.CUT_BOUNDARY else 50,
            tags=tags,
        )
        for kind, capabilities, tags in capability_groups
    )
    threshold_metric = {
        AVSyncCapability.SOURCE_PTS: (
            AVSyncMetricKind.OFFSET_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            0,
            None,
        ),
        AVSyncCapability.TIME_BASE: (
            AVSyncMetricKind.OFFSET_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            0,
            None,
        ),
        AVSyncCapability.SIGNED_OFFSET: (
            AVSyncMetricKind.OFFSET_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            None,
            50,
        ),
        AVSyncCapability.OFFSET_INTERVAL: (
            AVSyncMetricKind.OFFSET_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            None,
            100,
        ),
        AVSyncCapability.SPEECH_LIP: (
            AVSyncMetricKind.OFFSET_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            None,
            100,
        ),
        AVSyncCapability.ACTION_SOUND: (
            AVSyncMetricKind.OFFSET_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            None,
            120,
        ),
        AVSyncCapability.SOURCE_VISIBILITY: (
            AVSyncMetricKind.VISIBILITY_ACCURACY,
            AVSyncMetricUnit.BASIS_POINTS,
            9_000,
            None,
        ),
        AVSyncCapability.MUSIC_EDIT: (
            AVSyncMetricKind.BOUNDARY_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            None,
            160,
        ),
        AVSyncCapability.VIDEO_SOUNDTRACK: (
            AVSyncMetricKind.OFFSET_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            None,
            120,
        ),
        AVSyncCapability.CUT_BOUNDARY: (
            AVSyncMetricKind.BOUNDARY_ERROR,
            AVSyncMetricUnit.MILLISECONDS,
            None,
            100,
        ),
        AVSyncCapability.DESYNCHRONIZATION: (
            AVSyncMetricKind.DESYNC_RECALL,
            AVSyncMetricUnit.BASIS_POINTS,
            9_000,
            None,
        ),
        AVSyncCapability.DUBBING: (
            AVSyncMetricKind.DUBBING_RECALL,
            AVSyncMetricUnit.BASIS_POINTS,
            8_000,
            None,
        ),
        AVSyncCapability.OVERLAP: (
            AVSyncMetricKind.OVERLAP_RECALL,
            AVSyncMetricUnit.BASIS_POINTS,
            8_000,
            None,
        ),
        AVSyncCapability.CALIBRATION: (
            AVSyncMetricKind.CALIBRATION_ERROR,
            AVSyncMetricUnit.BASIS_POINTS,
            None,
            1_000,
        ),
        AVSyncCapability.ABSTENTION: (
            AVSyncMetricKind.ABSTENTION_RATE,
            AVSyncMetricUnit.BASIS_POINTS,
            9_000,
            None,
        ),
        AVSyncCapability.CORRUPTION: (
            AVSyncMetricKind.FABRICATION_VIOLATIONS,
            AVSyncMetricUnit.COUNT,
            None,
            0,
        ),
        AVSyncCapability.ADVERSARIAL_METADATA: (
            AVSyncMetricKind.FABRICATION_VIOLATIONS,
            AVSyncMetricUnit.COUNT,
            None,
            0,
        ),
    }
    thresholds = tuple(
        AVSyncBenchmarkThreshold(
            f"av_sync.threshold.{capability.value}",
            metric,
            capability,
            unit,
            minimum=minimum,
            maximum=maximum,
        )
        for capability in AVSyncCapability
        for metric, unit, minimum, maximum in (threshold_metric[capability],)
    )
    capabilities = tuple(AVSyncCapability)
    candidates = (
        AVSyncCandidateProfile(
            "av_sync.candidate.native",
            AVSyncCandidateFamily.COMFYUI_NATIVE,
            "comfyui_native_av_sync",
            "1.0.0",
            "comfyui-native-sync-unqualified",
            "review_required",
            capabilities,
            False,
            True,
            True,
            AVSyncDisposition.UNAVAILABLE,
            "pinned host/model/media synchronization execution was not authorized",
        ),
        AVSyncCandidateProfile(
            "av_sync.candidate.ollama",
            AVSyncCandidateFamily.OLLAMA,
            "ollama_av_sync",
            "1.0.0",
            "ollama-sync-unqualified",
            "review_required",
            capabilities,
            True,
            True,
            True,
            AVSyncDisposition.UNSUPPORTED,
            "loopback Ollama fallback was not contacted or qualified",
        ),
        AVSyncCandidateProfile(
            "av_sync.candidate.specialist",
            AVSyncCandidateFamily.SPECIALIST,
            "specialist_av_sync",
            "1.0.0",
            "specialist-sync-unqualified",
            "review_required",
            capabilities,
            False,
            True,
            True,
            AVSyncDisposition.UNSUPPORTED,
            "specialist checkpoint bake-off remains metadata-only research",
        ),
    )
    return AVSyncBenchmarkPlan(
        "av_sync.benchmark",
        "1.0.0",
        fixtures,
        thresholds,
        candidates,
        AVSyncBenchmarkLimits(6, 96, 192, 120, 600, 16_384, 1024, MAX_AV_SYNC_OUTPUT_BYTES, 1),
        AVSyncRoutingPolicy(tuple(AVSyncCandidateFamily)),
    )
