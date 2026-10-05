"""Source-PTS video decode and frame-batch contracts for M11-04.

This module records bounded decoder output without opening media or importing a decoder.  Integer
source PTS and its rational time base are authoritative; fixed-FPS or synthesized timestamps are
never accepted as a substitute.  Frame pixels and decoder paths remain runtime-only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from .canonical import canonical_fingerprint
from .constraints import TimePoint
from .contracts import MediaKind, TaskMode
from .errors import VideoDecodeError
from .evidence import Uncertainty
from .local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterResult,
    LocalAdapterRuntime,
    LocalBudgetGuard,
    LocalCancellationProbe,
    LocalDeviceKind,
    LocalDeviceSpec,
    run_local_adapter,
)
from .video_analysis import VideoAnalysisRequest, VideoOrientation

VIDEO_DECODE_SCHEMA = "h3.video.decode.v1"
MAX_DECODE_ASSETS = 3
MAX_DECODE_FRAMES = 512
MAX_DECODE_KEYFRAMES = 64
MAX_DECODE_SHOTS = 32
MAX_DECODE_BATCHES = 4
MAX_DECODE_BOUNDARIES = 64
MAX_DECODE_CORPUS_CASES = 32

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class VideoDecodeStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"


class VideoTimestampPolicy(str, Enum):
    SOURCE_PTS_ONLY = "source_pts_only"


class FrameBatchRoute(str, Enum):
    COMFYUI_NATIVE_VLM = "comfyui_native_vlm"
    OLLAMA_VLM = "ollama_vlm"
    SPECIALIST = "specialist"


class ShotBoundaryKind(str, Enum):
    HARD_CUT = "hard_cut"
    FADE = "fade"
    DISSOLVE = "dissolve"
    STATIC = "static"
    UNKNOWN = "unknown"


class DecodeCaseKind(str, Enum):
    VFR = "vfr"
    CONSTANT_FPS = "constant_fps"
    HARD_CUT = "hard_cut"
    FADE = "fade"
    DISSOLVE = "dissolve"
    STATIC = "static"
    SHORT_LONG = "short_long"
    ROTATED_SILENT = "rotated_silent"
    CORRUPT_UNSUPPORTED = "corrupt_unsupported"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise VideoDecodeError(f"{field} must be a bounded identifier")
    return value


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise VideoDecodeError(f"{field} must be a numeric version")
    return value


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise VideoDecodeError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _positive_int(value: object, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise VideoDecodeError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise VideoDecodeError(f"{field} exceeds the finite limit")
    return value


def _non_negative_int(value: object, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VideoDecodeError(f"{field} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise VideoDecodeError(f"{field} exceeds the finite limit")
    return value


def _decimal(value: object, field: str, *, maximum: Decimal | None = None) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise VideoDecodeError(f"{field} must be a non-negative finite Decimal")
    if maximum is not None and value > maximum:
        raise VideoDecodeError(f"{field} exceeds the finite limit")
    return value


def _uncertainties(values: object, field: str) -> tuple[Uncertainty, ...]:
    if (
        not isinstance(values, tuple)
        or len(values) > 8
        or not all(isinstance(value, Uncertainty) for value in values)
    ):
        raise VideoDecodeError(f"{field} must contain bounded Uncertainty values")
    return values


@dataclass(frozen=True, slots=True)
class SourcePTS:
    """Integer source PTS and rational time base; timestamp is checked against the ratio."""

    pts: int
    time_base_num: int
    time_base_den: int
    timestamp: TimePoint
    source_authoritative: bool = True

    def __post_init__(self) -> None:
        _non_negative_int(self.pts, "pts")
        _positive_int(self.time_base_num, "time_base_num")
        _positive_int(self.time_base_den, "time_base_den")
        if not isinstance(self.timestamp, TimePoint):
            raise VideoDecodeError("timestamp must be a TimePoint")
        try:
            expected = Decimal(self.pts) * Decimal(self.time_base_num) / Decimal(self.time_base_den)
        except (InvalidOperation, ZeroDivisionError) as exc:
            raise VideoDecodeError("source PTS rational conversion failed") from exc
        if expected != self.timestamp.seconds:
            raise VideoDecodeError("timestamp does not equal source PTS/time base")
        if self.source_authoritative is not True:
            raise VideoDecodeError("source_authoritative must remain true")

    def to_wire(self) -> dict[str, object]:
        return {
            "pts": self.pts,
            "time_base_num": self.time_base_num,
            "time_base_den": self.time_base_den,
            "timestamp": self.timestamp.to_wire(),
            "source_authoritative": self.source_authoritative,
        }


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    frame_id: str
    asset_id: str
    source_id: str
    source_pts: SourcePTS
    width: int
    height: int
    content_fingerprint: str
    keyframe: bool = False
    frame_index: int | None = None
    orientation: VideoOrientation | str = VideoOrientation.UP

    def __post_init__(self) -> None:
        _identifier(self.frame_id, "frame_id")
        _identifier(self.asset_id, "frame asset_id")
        _identifier(self.source_id, "frame source_id")
        if not isinstance(self.source_pts, SourcePTS):
            raise VideoDecodeError("source_pts must be SourcePTS")
        _positive_int(self.width, "frame width")
        _positive_int(self.height, "frame height")
        _fingerprint(self.content_fingerprint, "content_fingerprint")
        if not isinstance(self.keyframe, bool):
            raise VideoDecodeError("keyframe must be boolean")
        if self.frame_index is not None:
            _non_negative_int(self.frame_index, "frame_index")
        try:
            orientation = (
                self.orientation
                if isinstance(self.orientation, VideoOrientation)
                else VideoOrientation(self.orientation)
            )
        except (TypeError, ValueError):
            raise VideoDecodeError("frame orientation is unsupported") from None
        object.__setattr__(self, "orientation", orientation)

    def to_wire(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_pts": self.source_pts.to_wire(),
            "width": self.width,
            "height": self.height,
            "content_fingerprint": self.content_fingerprint,
            "keyframe": self.keyframe,
            "frame_index": self.frame_index,
            "orientation": cast(VideoOrientation, self.orientation).value,
        }


@dataclass(frozen=True, slots=True)
class FrameReference:
    frame_id: str
    source_pts: SourcePTS

    def __post_init__(self) -> None:
        _identifier(self.frame_id, "frame reference frame_id")
        if not isinstance(self.source_pts, SourcePTS):
            raise VideoDecodeError("frame reference source_pts must be SourcePTS")

    def to_wire(self) -> dict[str, object]:
        return {"frame_id": self.frame_id, "source_pts": self.source_pts.to_wire()}


@dataclass(frozen=True, slots=True)
class VideoFrameBatch:
    batch_id: str
    asset_id: str
    source_id: str
    route: FrameBatchRoute | str
    adapter_id: str
    frames: tuple[FrameReference, ...]
    source_authoritative: bool = True

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "batch_id")
        _identifier(self.asset_id, "batch asset_id")
        _identifier(self.source_id, "batch source_id")
        try:
            route = (
                self.route
                if isinstance(self.route, FrameBatchRoute)
                else FrameBatchRoute(self.route)
            )
        except (TypeError, ValueError):
            raise VideoDecodeError("frame batch route is unsupported") from None
        object.__setattr__(self, "route", route)
        _identifier(self.adapter_id, "batch adapter_id")
        if (
            not isinstance(self.frames, tuple)
            or not self.frames
            or len(self.frames) > MAX_DECODE_FRAMES
        ):
            raise VideoDecodeError("frame batch must contain bounded frames")
        if not all(isinstance(item, FrameReference) for item in self.frames):
            raise VideoDecodeError("frame batch frames are invalid")
        ids = [item.frame_id for item in self.frames]
        if len(ids) != len(set(ids)):
            raise VideoDecodeError("frame batch frame IDs must be unique")
        for previous, current in zip(self.frames, self.frames[1:], strict=False):
            if current.source_pts.timestamp.seconds < previous.source_pts.timestamp.seconds:
                raise VideoDecodeError("frame batch timestamps must be monotonic")
        if self.source_authoritative is not True:
            raise VideoDecodeError("frame batch source_authoritative must remain true")

    def to_wire(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "route": cast(FrameBatchRoute, self.route).value,
            "adapter_id": self.adapter_id,
            "frames": [item.to_wire() for item in self.frames],
            "source_authoritative": self.source_authoritative,
        }


@dataclass(frozen=True, slots=True)
class ShotBoundary:
    boundary_id: str
    asset_id: str
    source_id: str
    source_pts: SourcePTS
    left_frame_id: str
    right_frame_id: str
    kind: ShotBoundaryKind | str
    confidence: Decimal
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.boundary_id, "boundary_id")
        _identifier(self.asset_id, "boundary asset_id")
        _identifier(self.source_id, "boundary source_id")
        if not isinstance(self.source_pts, SourcePTS):
            raise VideoDecodeError("boundary source_pts must be SourcePTS")
        _identifier(self.left_frame_id, "left_frame_id")
        _identifier(self.right_frame_id, "right_frame_id")
        if self.left_frame_id == self.right_frame_id:
            raise VideoDecodeError("boundary left and right frames must differ")
        try:
            kind = (
                self.kind
                if isinstance(self.kind, ShotBoundaryKind)
                else ShotBoundaryKind(self.kind)
            )
        except (TypeError, ValueError):
            raise VideoDecodeError("boundary kind is unsupported") from None
        object.__setattr__(self, "kind", kind)
        confidence = _decimal(self.confidence, "boundary confidence", maximum=Decimal("1"))
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "boundary uncertainties")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "boundary_id": self.boundary_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_pts": self.source_pts.to_wire(),
            "left_frame_id": self.left_frame_id,
            "right_frame_id": self.right_frame_id,
            "kind": cast(ShotBoundaryKind, self.kind).value,
            "confidence": format(self.confidence, "f"),
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class DecodedShot:
    shot_id: str
    asset_id: str
    source_id: str
    start: SourcePTS
    end: SourcePTS
    frame_ids: tuple[str, ...]
    keyframe_ids: tuple[str, ...]
    boundary_ids: tuple[str, ...] = ()
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.shot_id, "shot_id")
        _identifier(self.asset_id, "shot asset_id")
        _identifier(self.source_id, "shot source_id")
        if not isinstance(self.start, SourcePTS) or not isinstance(self.end, SourcePTS):
            raise VideoDecodeError("shot bounds must be SourcePTS")
        if (
            self.start.time_base_num != self.end.time_base_num
            or self.start.time_base_den != self.end.time_base_den
        ):
            raise VideoDecodeError("shot bounds must use one source time base")
        if self.end.timestamp.seconds <= self.start.timestamp.seconds:
            raise VideoDecodeError("shot end must be after start")
        for values, field, maximum in (
            (self.frame_ids, "frame_ids", MAX_DECODE_FRAMES),
            (self.keyframe_ids, "keyframe_ids", MAX_DECODE_KEYFRAMES),
            (self.boundary_ids, "boundary_ids", MAX_DECODE_BOUNDARIES),
        ):
            if not isinstance(values, tuple) or len(values) > maximum:
                raise VideoDecodeError(f"{field} is outside the finite limit")
            for value in values:
                _identifier(value, f"{field} item")
            if len(values) != len(set(values)):
                raise VideoDecodeError(f"{field} must be unique")
        if not self.frame_ids or not set(self.keyframe_ids).issubset(set(self.frame_ids)):
            raise VideoDecodeError("keyframes must be owned by shot frames")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "shot uncertainties")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "shot_id": self.shot_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "frame_ids": list(self.frame_ids),
            "keyframe_ids": list(self.keyframe_ids),
            "boundary_ids": list(self.boundary_ids),
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class VideoDecodeReceipt:
    decoder_id: str
    decoder_version: str
    decode_profile: str
    source_fingerprint: str
    output_fingerprint: str
    timestamp_policy: VideoTimestampPolicy

    def __post_init__(self) -> None:
        _identifier(self.decoder_id, "decoder_id")
        _version(self.decoder_version, "decoder_version")
        _identifier(self.decode_profile, "decode_profile")
        _fingerprint(self.source_fingerprint, "source_fingerprint")
        _fingerprint(self.output_fingerprint, "output_fingerprint")
        if self.timestamp_policy is not VideoTimestampPolicy.SOURCE_PTS_ONLY:
            raise VideoDecodeError("only source PTS timestamp policy is supported")

    def to_wire(self) -> dict[str, object]:
        return {
            "decoder_id": self.decoder_id,
            "decoder_version": self.decoder_version,
            "decode_profile": self.decode_profile,
            "source_fingerprint": self.source_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "timestamp_policy": self.timestamp_policy.value,
        }


@dataclass(frozen=True, slots=True)
class VideoDecodeRequest:
    video_request: VideoAnalysisRequest
    video_payloads: tuple[bytes, ...]
    source_fingerprints: tuple[str, ...]
    decoder_id: str = "injected_media_decode"
    decoder_version: str = "1.0.0"
    decode_profile: str = "vfr_source_pts"
    max_frames: int = MAX_DECODE_FRAMES
    max_keyframes: int = MAX_DECODE_KEYFRAMES
    max_shots: int = MAX_DECODE_SHOTS

    def __post_init__(self) -> None:
        if not isinstance(self.video_request, VideoAnalysisRequest):
            raise VideoDecodeError("video_request must be VideoAnalysisRequest")
        count = len(self.video_request.selections)
        if not isinstance(self.video_payloads, tuple) or len(self.video_payloads) != count:
            raise VideoDecodeError("video_payloads must match selected video count")
        for payload in self.video_payloads:
            if not isinstance(payload, bytes) or not payload or len(payload) > 128 * 1024 * 1024:
                raise VideoDecodeError("video payload is outside the bounded limit")
        if (
            not isinstance(self.source_fingerprints, tuple)
            or len(self.source_fingerprints) != count
        ):
            raise VideoDecodeError("source_fingerprints must match selected video count")
        for value in self.source_fingerprints:
            _fingerprint(value, "source fingerprint")
        _identifier(self.decoder_id, "decoder_id")
        _version(self.decoder_version, "decoder_version")
        _identifier(self.decode_profile, "decode_profile")
        _positive_int(self.max_frames, "max_frames", MAX_DECODE_FRAMES)
        _positive_int(self.max_keyframes, "max_keyframes", MAX_DECODE_KEYFRAMES)
        _positive_int(self.max_shots, "max_shots", MAX_DECODE_SHOTS)

    @property
    def selected_asset_ids(self) -> tuple[str, ...]:
        return self.video_request.selected_asset_ids

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": VIDEO_DECODE_SCHEMA,
            "selected_asset_ids": list(self.selected_asset_ids),
            "source_fingerprints": list(self.source_fingerprints),
            "decoder_id": self.decoder_id,
            "decoder_version": self.decoder_version,
            "decode_profile": self.decode_profile,
            "max_frames": self.max_frames,
            "max_keyframes": self.max_keyframes,
            "max_shots": self.max_shots,
        }


@dataclass(frozen=True, slots=True)
class VideoDecodeDocument:
    document_id: str
    schema: str
    status: VideoDecodeStatus
    selected_asset_ids: tuple[str, ...]
    timestamp_policy: VideoTimestampPolicy
    frames: tuple[DecodedFrame, ...] = ()
    batches: tuple[VideoFrameBatch, ...] = ()
    boundaries: tuple[ShotBoundary, ...] = ()
    shots: tuple[DecodedShot, ...] = ()
    receipt: VideoDecodeReceipt | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.document_id, "document_id")
        if self.schema != VIDEO_DECODE_SCHEMA:
            raise VideoDecodeError("unsupported video decode schema")
        if not isinstance(self.status, VideoDecodeStatus):
            raise VideoDecodeError("status must be VideoDecodeStatus")
        if self.timestamp_policy is not VideoTimestampPolicy.SOURCE_PTS_ONLY:
            raise VideoDecodeError("document timestamp policy must be source PTS only")
        if (
            not isinstance(self.selected_asset_ids, tuple)
            or not 1 <= len(self.selected_asset_ids) <= MAX_DECODE_ASSETS
            or len(set(self.selected_asset_ids)) != len(self.selected_asset_ids)
        ):
            raise VideoDecodeError("selected_asset_ids must contain unique bounded assets")
        for asset_id in self.selected_asset_ids:
            _identifier(asset_id, "selected asset_id")
        for values, field, maximum, expected in (
            (self.frames, "frames", MAX_DECODE_FRAMES, DecodedFrame),
            (self.batches, "batches", MAX_DECODE_BATCHES, VideoFrameBatch),
            (self.boundaries, "boundaries", MAX_DECODE_BOUNDARIES, ShotBoundary),
            (self.shots, "shots", MAX_DECODE_SHOTS, DecodedShot),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > maximum
                or not all(isinstance(item, expected) for item in values)
            ):
                raise VideoDecodeError(f"{field} is outside the finite limit")
            identifier_field = {
                "frames": "frame_id",
                "batches": "batch_id",
                "boundaries": "boundary_id",
                "shots": "shot_id",
            }[field]
            identifiers = [getattr(item, identifier_field) for item in values]
            if len(identifiers) != len(set(identifiers)):
                raise VideoDecodeError(f"{field} IDs must be unique")
        if self.receipt is not None and not isinstance(self.receipt, VideoDecodeReceipt):
            raise VideoDecodeError("receipt is invalid")
        if self.receipt is not None and self.receipt.timestamp_policy is not self.timestamp_policy:
            raise VideoDecodeError("receipt timestamp policy differs from document")
        frame_map = {item.frame_id: item for item in self.frames}
        asset_sources: dict[str, str] = {}
        asset_time_bases: dict[str, tuple[int, int]] = {}
        for frame in self.frames:
            if frame.asset_id not in self.selected_asset_ids:
                raise VideoDecodeError("frame asset is not selected")
            prior_source = asset_sources.get(frame.asset_id)
            if prior_source is not None and prior_source != frame.source_id:
                raise VideoDecodeError("one asset cannot have multiple source IDs")
            asset_sources[frame.asset_id] = frame.source_id
            time_base = (frame.source_pts.time_base_num, frame.source_pts.time_base_den)
            prior_time_base = asset_time_bases.get(frame.asset_id)
            if prior_time_base is not None and prior_time_base != time_base:
                raise VideoDecodeError("one asset cannot have multiple source time bases")
            asset_time_bases[frame.asset_id] = time_base
        for asset_id in self.selected_asset_ids:
            source_frames = [item for item in self.frames if item.asset_id == asset_id]
            for previous, current in zip(source_frames, source_frames[1:], strict=False):
                if current.source_pts.timestamp.seconds < previous.source_pts.timestamp.seconds:
                    raise VideoDecodeError("frames must be monotonic by source PTS")
        for batch in self.batches:
            for reference in batch.frames:
                candidate = frame_map.get(reference.frame_id)
                if candidate is None:
                    raise VideoDecodeError("frame batch references an unknown frame")
                if candidate.asset_id != batch.asset_id or candidate.source_id != batch.source_id:
                    raise VideoDecodeError("frame batch source ownership does not match frame")
                if candidate.source_pts != reference.source_pts:
                    raise VideoDecodeError("frame batch PTS differs from decoded frame PTS")
        boundary_map = {item.boundary_id: item for item in self.boundaries}
        for boundary in self.boundaries:
            if boundary.asset_id not in self.selected_asset_ids:
                raise VideoDecodeError("boundary asset is not selected")
            for frame_id in (boundary.left_frame_id, boundary.right_frame_id):
                if frame_id not in frame_map:
                    raise VideoDecodeError("boundary references an unknown frame")
            left = frame_map[boundary.left_frame_id]
            right = frame_map[boundary.right_frame_id]
            if (
                left.asset_id != boundary.asset_id
                or right.asset_id != boundary.asset_id
                or left.source_id != boundary.source_id
                or right.source_id != boundary.source_id
            ):
                raise VideoDecodeError("boundary source ownership does not match frames")
            if (
                left.source_pts.time_base_num != boundary.source_pts.time_base_num
                or left.source_pts.time_base_den != boundary.source_pts.time_base_den
                or right.source_pts.time_base_num != boundary.source_pts.time_base_num
                or right.source_pts.time_base_den != boundary.source_pts.time_base_den
            ):
                raise VideoDecodeError("boundary time base differs from source frames")
            if not (
                left.source_pts.timestamp.seconds
                <= boundary.source_pts.timestamp.seconds
                <= right.source_pts.timestamp.seconds
            ):
                raise VideoDecodeError("boundary PTS is outside adjacent source frames")
        previous_end: dict[str, Decimal] = {}
        for shot in self.shots:
            if shot.asset_id not in self.selected_asset_ids:
                raise VideoDecodeError("shot asset is not selected")
            if any(frame_map.get(frame_id) is None for frame_id in shot.frame_ids):
                raise VideoDecodeError("shot references an unknown frame")
            if any(frame_map[frame_id].asset_id != shot.asset_id for frame_id in shot.frame_ids):
                raise VideoDecodeError("shot frame source does not match shot")
            if asset_sources.get(shot.asset_id) != shot.source_id:
                raise VideoDecodeError("shot source does not match selected frames")
            shot_frames = tuple(frame_map[frame_id] for frame_id in shot.frame_ids)
            if any(
                not (
                    shot.start.timestamp.seconds
                    <= frame.source_pts.timestamp.seconds
                    < shot.end.timestamp.seconds
                )
                for frame in shot_frames
            ):
                raise VideoDecodeError("shot frame PTS is outside its half-open interval")
            for previous, current in zip(shot_frames, shot_frames[1:], strict=False):
                if current.source_pts.timestamp.seconds < previous.source_pts.timestamp.seconds:
                    raise VideoDecodeError("shot frames must be monotonic by source PTS")
            if any(not frame_map[keyframe_id].keyframe for keyframe_id in shot.keyframe_ids):
                raise VideoDecodeError("shot keyframe IDs must identify keyframes")
            if any(boundary_map.get(boundary_id) is None for boundary_id in shot.boundary_ids):
                raise VideoDecodeError("shot references an unknown boundary")
            if any(
                boundary_map[boundary_id].asset_id != shot.asset_id
                or boundary_map[boundary_id].source_id != shot.source_id
                for boundary_id in shot.boundary_ids
            ):
                raise VideoDecodeError("shot boundary source does not match shot")
            prior = previous_end.get(shot.asset_id)
            if prior is not None and shot.start.timestamp.seconds < prior:
                raise VideoDecodeError("shots must not overlap per source")
            previous_end[shot.asset_id] = shot.end.timestamp.seconds
        if self.status is VideoDecodeStatus.COMPLETE:
            if (
                not self.frames
                or not self.batches
                or not self.shots
                or not isinstance(self.receipt, VideoDecodeReceipt)
            ):
                raise VideoDecodeError(
                    "complete decode requires frames, batches, shots, and receipt"
                )
        elif self.status in {
            VideoDecodeStatus.EMPTY,
            VideoDecodeStatus.CORRUPT,
            VideoDecodeStatus.UNSUPPORTED,
        } and (
            self.frames or self.batches or self.boundaries or self.shots or self.receipt is not None
        ):
            raise VideoDecodeError("degraded decode cannot contain output")
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > 16
            or not all(isinstance(item, str) and item for item in self.diagnostics)
        ):
            raise VideoDecodeError("diagnostics are outside the finite limit")

    @property
    def complete(self) -> bool:
        return self.status is VideoDecodeStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": self.status.value,
            "complete": self.complete,
            "selected_asset_ids": list(self.selected_asset_ids),
            "timestamp_policy": self.timestamp_policy.value,
            "frames": [item.to_wire() for item in self.frames],
            "batches": [item.to_wire() for item in self.batches],
            "boundaries": [item.to_wire() for item in self.boundaries],
            "shots": [item.to_wire() for item in self.shots],
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "status": self.status.value,
            "complete": self.complete,
            "selected_asset_ids": list(self.selected_asset_ids),
            "timestamp_policy": self.timestamp_policy.value,
            "frame_count": len(self.frames),
            "batch_count": len(self.batches),
            "boundary_count": len(self.boundaries),
            "shot_count": len(self.shots),
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class DecodeCorpusCase:
    case_id: str
    kind: DecodeCaseKind
    source_fingerprint: str
    expected_boundaries: int
    expected_keyframes: int
    should_abstain: bool = False

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        if not isinstance(self.kind, DecodeCaseKind):
            raise VideoDecodeError("case kind is unsupported")
        _fingerprint(self.source_fingerprint, "source_fingerprint")
        _non_negative_int(self.expected_boundaries, "expected_boundaries", MAX_DECODE_BOUNDARIES)
        _non_negative_int(self.expected_keyframes, "expected_keyframes", MAX_DECODE_KEYFRAMES)
        if not isinstance(self.should_abstain, bool):
            raise VideoDecodeError("should_abstain must be boolean")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "kind": self.kind.value,
            "source_fingerprint": self.source_fingerprint,
            "expected_boundaries": self.expected_boundaries,
            "expected_keyframes": self.expected_keyframes,
            "should_abstain": self.should_abstain,
        }


@dataclass(frozen=True, slots=True)
class DecodeMetricThreshold:
    metric: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        _identifier(self.metric, "metric")
        if (self.minimum is None) == (self.maximum is None):
            raise VideoDecodeError("exactly one metric bound is required")
        value = self.minimum if self.minimum is not None else self.maximum
        if value is None or not value.is_finite() or value < 0 or value > 1:
            raise VideoDecodeError("metric threshold must be between 0 and 1")

    def to_wire(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "minimum": None if self.minimum is None else format(self.minimum, "f"),
            "maximum": None if self.maximum is None else format(self.maximum, "f"),
        }


@dataclass(frozen=True, slots=True)
class VideoDecodeBenchmarkPlan:
    cases: tuple[DecodeCorpusCase, ...]
    thresholds: tuple[DecodeMetricThreshold, ...]
    schema: str = "h3.video.decode.benchmark.v1"
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or len(self.cases) > MAX_DECODE_CORPUS_CASES
        ):
            raise VideoDecodeError("decode cases are outside the finite limit")
        if not all(isinstance(item, DecodeCorpusCase) for item in self.cases):
            raise VideoDecodeError("decode cases are invalid")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise VideoDecodeError("decode case IDs must be unique")
        if (
            not isinstance(self.thresholds, tuple)
            or not self.thresholds
            or not all(isinstance(item, DecodeMetricThreshold) for item in self.thresholds)
        ):
            raise VideoDecodeError("decode thresholds are invalid")
        if len({item.metric for item in self.thresholds}) != len(self.thresholds):
            raise VideoDecodeError("decode metrics must be unique")
        expected = canonical_fingerprint(
            {
                "schema": self.schema,
                "cases": [item.to_wire() for item in self.cases],
                "thresholds": [item.to_wire() for item in self.thresholds],
            }
        )
        if self.fingerprint and self.fingerprint != expected:
            raise VideoDecodeError("decode benchmark fingerprint does not match contents")
        object.__setattr__(self, "fingerprint", expected)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fingerprint": self.fingerprint,
            "cases": [item.to_wire() for item in self.cases],
            "thresholds": [item.to_wire() for item in self.thresholds],
        }


def build_default_video_decode_benchmark_plan() -> VideoDecodeBenchmarkPlan:
    def case(
        case_id: str, kind: DecodeCaseKind, boundaries: int, keyframes: int, abstain: bool = False
    ) -> DecodeCorpusCase:
        return DecodeCorpusCase(
            case_id,
            kind,
            canonical_fingerprint({"case_id": case_id}),
            boundaries,
            keyframes,
            abstain,
        )

    cases = (
        case("decode.vfr", DecodeCaseKind.VFR, 2, 3),
        case("decode.constant_fps", DecodeCaseKind.CONSTANT_FPS, 1, 2),
        case("decode.hard_cut", DecodeCaseKind.HARD_CUT, 2, 3),
        case("decode.fade", DecodeCaseKind.FADE, 1, 2),
        case("decode.dissolve", DecodeCaseKind.DISSOLVE, 1, 2),
        case("decode.static", DecodeCaseKind.STATIC, 0, 1),
        case("decode.short_long", DecodeCaseKind.SHORT_LONG, 1, 3),
        case("decode.rotated_silent", DecodeCaseKind.ROTATED_SILENT, 0, 1),
        case("decode.corrupt", DecodeCaseKind.CORRUPT_UNSUPPORTED, 0, 0, True),
    )
    thresholds = (
        DecodeMetricThreshold("boundary_recall", minimum=Decimal("0.90")),
        DecodeMetricThreshold("boundary_time_tolerance", maximum=Decimal("0.04")),
        DecodeMetricThreshold("keyframe_recall", minimum=Decimal("0.90")),
        DecodeMetricThreshold("source_pts_mapping_accuracy", minimum=Decimal("1.0")),
        DecodeMetricThreshold("timestamp_drift", maximum=Decimal("0")),
    )
    return VideoDecodeBenchmarkPlan(cases=cases, thresholds=thresholds)


@runtime_checkable
class VideoDecodeAdapter(Protocol):
    @property
    def descriptor(self) -> LocalAdapterDescriptor: ...

    def decode(
        self, request: VideoDecodeRequest, guard: LocalBudgetGuard
    ) -> VideoDecodeDocument: ...


class _VideoDecodeBridge:
    def __init__(self, adapter: VideoDecodeAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self, request: LocalAdapterExecutionRequest, guard: LocalBudgetGuard
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, VideoDecodeRequest):
            raise VideoDecodeError("decode adapter input is not VideoDecodeRequest")
        document = self._adapter.decode(request.input_value, guard)
        if not isinstance(document, VideoDecodeDocument):
            raise VideoDecodeError("decode adapter returned an invalid document")
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=document,
            output_bytes=len(document.to_wire().__repr__().encode("utf-8")),
            output_items=len(document.frames) + len(document.shots),
        )


def _validate_document(document: VideoDecodeDocument, request: VideoDecodeRequest) -> None:
    if document.selected_asset_ids != request.selected_asset_ids:
        raise VideoDecodeError("decode document selection does not match request")
    if len(document.frames) > request.max_frames:
        raise VideoDecodeError("decode document exceeds frame limit")
    if len(document.shots) > request.max_shots:
        raise VideoDecodeError("decode document exceeds shot limit")
    if sum(1 for frame in document.frames if frame.keyframe) > request.max_keyframes:
        raise VideoDecodeError("decode document exceeds keyframe limit")
    source_map = {
        selection.asset_id: selection.source_id for selection in request.video_request.selections
    }
    for frame in document.frames:
        if frame.source_id != source_map.get(frame.asset_id):
            raise VideoDecodeError("decoded frame source does not match request")
    if (
        document.receipt is not None
        and document.receipt.source_fingerprint not in request.source_fingerprints
    ):
        raise VideoDecodeError("decode receipt source fingerprint is not admitted")
    if (
        document.receipt is not None
        and document.receipt.timestamp_policy is not document.timestamp_policy
    ):
        raise VideoDecodeError("decode receipt timestamp policy differs from document")


def execute_video_decode(
    adapter: VideoDecodeAdapter,
    request: VideoDecodeRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> VideoDecodeDocument:
    """Execute one explicitly selected decoder through the bounded local runtime seam."""

    if not isinstance(adapter, VideoDecodeAdapter):
        raise VideoDecodeError("adapter must implement VideoDecodeAdapter")
    if not isinstance(request, VideoDecodeRequest):
        raise VideoDecodeError("request must be VideoDecodeRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise VideoDecodeError("device must be LocalDeviceSpec")
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=TaskMode.REF2VA,
        media_kinds=(MediaKind.VIDEO,),
        reference_count=len(request.video_request.selections),
        device=device_value,
        estimated_memory_bytes=sum(len(payload) for payload in request.video_payloads),
        estimated_output_bytes=adapter.descriptor.limits.max_output_bytes,
        deterministic_required=True,
        seed=request.video_request.sampling.seed,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _VideoDecodeBridge(adapter)
    if clock is not None:
        result = run_local_adapter(
            bridge,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            clock=clock,
            memory_meter=memory_meter,
        )
    else:
        result = run_local_adapter(
            bridge,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            memory_meter=memory_meter,
        )
    if not isinstance(result.value, VideoDecodeDocument):
        raise VideoDecodeError("decode result did not contain a document")
    _validate_document(result.value, request)
    return result.value


def build_video_decode_abstention(
    request: VideoDecodeRequest, status: VideoDecodeStatus, diagnostic: str
) -> VideoDecodeDocument:
    if status is VideoDecodeStatus.COMPLETE:
        raise VideoDecodeError("complete status requires decoded output")
    return VideoDecodeDocument(
        document_id=canonical_fingerprint(
            {"assets": request.selected_asset_ids, "status": status.value}
        )[7:39],
        schema=VIDEO_DECODE_SCHEMA,
        status=status,
        selected_asset_ids=request.selected_asset_ids,
        timestamp_policy=VideoTimestampPolicy.SOURCE_PTS_ONLY,
        diagnostics=(diagnostic,),
    )


__all__ = [
    "VIDEO_DECODE_SCHEMA",
    "MAX_DECODE_ASSETS",
    "MAX_DECODE_FRAMES",
    "MAX_DECODE_KEYFRAMES",
    "MAX_DECODE_SHOTS",
    "MAX_DECODE_BATCHES",
    "VideoDecodeStatus",
    "VideoTimestampPolicy",
    "FrameBatchRoute",
    "ShotBoundaryKind",
    "DecodeCaseKind",
    "SourcePTS",
    "DecodedFrame",
    "FrameReference",
    "VideoFrameBatch",
    "ShotBoundary",
    "DecodedShot",
    "VideoDecodeReceipt",
    "VideoDecodeRequest",
    "VideoDecodeDocument",
    "DecodeCorpusCase",
    "DecodeMetricThreshold",
    "VideoDecodeBenchmarkPlan",
    "VideoDecodeAdapter",
    "build_default_video_decode_benchmark_plan",
    "execute_video_decode",
    "build_video_decode_abstention",
]
