"""M25-10: closed public composition contract and pure frame resolver.

This module deliberately owns values only.  It imports no ComfyUI, media, HTTP, provider, GPU, or
browser runtime and never accepts a source path or URL.  Later runtime/render items consume its
content-free public identity and join private source authority on the backend.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from .canonical import (
    MAX_CANONICAL_BYTES,
    MAX_CANONICAL_DEPTH,
    MAX_CANONICAL_ITEMS,
    MAX_CANONICAL_STRING_LENGTH,
    MAX_INTEROPERABLE_INTEGER,
    canonical_bytes,
)
from .errors import CanonicalizationError, ContractValidationError

PUBLIC_SNAPSHOT_SCHEMA = "h3.context.public_composition_snapshot.v1"
RESOLVED_SCENE_SCHEMA = "h3.context.resolved_scene.v1"
OPERATION_PROFILE_ID: Final = "h3.authoring.nle_operation.v1"
ENGINE_PROFILE_ID: Final = "h3.native_media_canvas_backend.v1"
OUTPUT_PROFILE_ID: Final = "h3.authoring.output.h264_aac_24fps.v1"
RENDER_VOCABULARY_SCHEMA = "h3.context.authoring_render_vocabulary.v1"
PRIVATE_SOURCE_MANIFEST_SCHEMA = "h3.context.private_source_manifest.v1"
DERIVATIVE_MANIFEST_SCHEMA = "h3.context.derivative_manifest.v1"
AUDIO_EXTENSION_SCHEMA = "h3.authoring.independent_audio_extension.v1"
INDEPENDENT_AUDIO_COMMAND_NAMESPACE: Final = "h3.authoring.audio.command.v1"

MAX_ASSETS: Final = 128
MAX_TRACKS: Final = 8
MAX_CLIPS: Final = 128
# CRITICAL: keep this aligned with Production's admitted per-segment frame ceiling. Narrowing it
# makes an otherwise valid generated output impossible to import into Authoring.
MAX_LANDMARKS_PER_ASSET: Final = 512
MAX_LANDMARKS: Final = 2_048
MAX_BLOCKERS: Final = 128
MAX_RESOLVED_LAYERS: Final = 128
MAX_RESOLVER_WORK_UNITS: Final = 4_096
MAX_RESOLVED_SCENE_BYTES: Final = 131_072
MAX_EXTENT_FRAMES: Final = 1_000_000
# Clip audio: gain in millibels (-60 dB to +12 dB) and each fade in output frames.
CLIP_AUDIO_GAIN_MIN_MB: Final = -6_000
CLIP_AUDIO_GAIN_MAX_MB: Final = 1_200
CLIP_AUDIO_FADE_MAX_FRAMES: Final = 240

BLOCKER_CODES: Final = (
    "unsupported_profile",
    "unsupported_output_profile",
    "operation_not_in_profile",
    "audio_editing_deferred",
    "private_field",
    "negative_timestamp",
    "invalid_timing",
    "timing_unavailable",
    "source_range_unavailable",
    "embedded_audio_unavailable",
    "stale_snapshot",
    "resource_limit",
    "invalid_contract",
)
NLE_OPERATION_IDS: Final = (
    "create_track",
    "remove_track",
    "reorder_track",
    "set_track_enabled",
    "set_track_locked",
    "insert_asset_clip",
    "insert_title_clip",
    "replace_clip_asset",
    "remove_clip",
    "move_clip",
    "move_group",
    "trim_clip",
    "split_clip",
    "merge_clips",
    "insert_range",
    "overwrite_range",
    "ripple_delete",
    "ripple_trim",
    "roll_edit",
    "slip_clip",
    "slide_clip",
    "set_clip_enabled",
    "set_visual_transform",
    "set_crop",
    "set_opacity_blend",
    "set_text_content",
    "set_text_style",
    "set_transition",
    "set_effect",
    # A video clip's own gain, mute and fades. Not an independent-audio command: those names, and
    # the reserved namespace, stay refused as `audio_editing_deferred`.
    "set_clip_audio",
    "select_clips",
    "undo",
    "redo",
    "rebase_transaction",
)
RENDER_JOB_STATES: Final = (
    "queued",
    "probing",
    "preparing",
    "rendering",
    "encoding",
    "muxing",
    "validating",
    "succeeded",
    "failed",
    "cancelled",
)
RENDER_TERMINAL_STATES: Final = ("succeeded", "failed", "cancelled")
RENDER_TERMINAL_REASONS: Final = (
    "unsupported_profile",
    "source_unavailable",
    "source_replaced",
    "stale_snapshot",
    "cancelled",
    "timeout",
    "renderer_failed",
    "validation_failed",
    "internal_failure",
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PRIVATE_FIELDS = frozenset(
    {
        "private_source_manifest",
        "derivative_manifest",
        "runtime_identity",
        "source_path",
        "source_url",
        "blob_url",
        "lease",
        "credential",
    }
)


class CompositionContractError(ContractValidationError):
    """Content-free typed rejection at the public contract boundary."""

    def __init__(self, code: str, message: str) -> None:
        if code not in BLOCKER_CODES:
            code = "invalid_contract"
        super().__init__(f"{code}: {message}")
        self.code = code


def _reject(code: str, message: str) -> CompositionContractError:
    return CompositionContractError(code, message)


def _mapping(value: object, keys: Sequence[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _reject("invalid_contract", f"{name} must be an object")
    actual = set(value)
    private = actual & _PRIVATE_FIELDS
    if private:
        raise _reject("private_field", f"{name} contains a reserved private field")
    if actual != set(keys):
        raise _reject("invalid_contract", f"{name} must be closed")
    if not all(isinstance(key, str) for key in value):
        raise _reject("invalid_contract", f"{name} keys must be strings")
    return value


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject("invalid_contract", f"{name} must be an integer")
    if value < minimum or value > maximum or abs(value) > MAX_INTEROPERABLE_INTEGER:
        raise _reject("invalid_contract", f"{name} is outside its closed bounds")
    return value


def _resource_integer(value: object, name: str, minimum: int, maximum: int) -> int:
    try:
        return _integer(value, name, minimum, maximum)
    except CompositionContractError as exc:
        raise _reject("resource_limit", f"{name} exceeds the resource profile") from exc


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _reject("invalid_contract", f"{name} must be a bounded identifier")
    return value


def _fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise _reject("invalid_contract", f"{name} must be a sha256 fingerprint")
    return value


def _enum(value: object, allowed: Sequence[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _reject("invalid_contract", f"{name} is outside the closed vocabulary")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise _reject("invalid_contract", f"{name} must be a boolean")
    return value


def _optional_identifier(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


def _tuple(value: object, name: str, maximum: int) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise _reject("invalid_contract", f"{name} must be an array")
    if len(value) > maximum:
        raise _reject("resource_limit", f"{name} exceeds the resource profile")
    return tuple(value)


def _reject_private_recursive(value: object, name: str, depth: int = 0) -> None:
    if depth > 32:
        raise _reject("resource_limit", f"{name} exceeds the nesting profile")
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise _reject("resource_limit", f"{name} exceeds the object profile")
        for key, item in value.items():
            if not isinstance(key, str):
                raise _reject("invalid_contract", f"{name} contains a non-string key")
            if key in _PRIVATE_FIELDS:
                raise _reject("private_field", f"{name} contains a reserved private field")
            _reject_private_recursive(item, f"{name}.{key}", depth + 1)
    elif isinstance(value, list):
        if len(value) > 256:
            raise _reject("resource_limit", f"{name} exceeds the array profile")
        for index, item in enumerate(value):
            _reject_private_recursive(item, f"{name}[{index}]", depth + 1)


@dataclass(frozen=True, slots=True)
class Rational:
    num: int
    den: int

    def to_wire(self) -> dict[str, int]:
        return {"num": self.num, "den": self.den}


def _rational(
    value: object,
    name: str,
    *,
    maximum_numerator: int = 240_000,
    maximum_denominator: int = 1_001,
) -> Rational:
    wire = _mapping(value, ("num", "den"), name)
    num = _integer(wire["num"], f"{name}.num", 1, maximum_numerator)
    den = _integer(wire["den"], f"{name}.den", 1, maximum_denominator)
    # Closed profiles require reduced ratios so equal timing has one identity.
    a, b = num, den
    while b:
        a, b = b, a % b
    if a != 1:
        raise _reject("invalid_timing", f"{name} must be reduced")
    return Rational(num, den)


def _source_time_base(value: object, name: str) -> Rational:
    # IMPORTANT: source clocks are observed media facts, not output-rate controls. Keep their
    # exact-integer bound separate or the accepted 1/12288 M25-09 corpus is rejected as invalid.
    return _rational(
        value,
        name,
        maximum_numerator=MAX_INTEROPERABLE_INTEGER,
        maximum_denominator=MAX_INTEROPERABLE_INTEGER,
    )


@dataclass(frozen=True, slots=True)
class TimingLandmark:
    frame_index: int
    pts: int
    dts: int
    duration_ticks: int

    def to_wire(self) -> dict[str, int]:
        return {
            "frame_index": self.frame_index,
            "pts": self.pts,
            "dts": self.dts,
            "duration_ticks": self.duration_ticks,
        }


@dataclass(frozen=True, slots=True)
class PublicAsset:
    asset_id: str
    kind: str
    source_time_base: Rational | None
    source_frame_count: int | None
    source_sample_count: int | None
    embedded_audio: str
    timestamp_policy: str
    landmarks: tuple[TimingLandmark, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "source_time_base": None
            if self.source_time_base is None
            else self.source_time_base.to_wire(),
            "source_frame_count": self.source_frame_count,
            "source_sample_count": self.source_sample_count,
            "embedded_audio": self.embedded_audio,
            "timestamp_policy": self.timestamp_policy,
            "landmarks": [item.to_wire() for item in self.landmarks],
        }


def _asset(value: object, index: int) -> PublicAsset:
    name = f"assets[{index}]"
    wire = _mapping(
        value,
        (
            "asset_id",
            "kind",
            "source_time_base",
            "source_frame_count",
            "source_sample_count",
            "embedded_audio",
            "timestamp_policy",
            "landmarks",
        ),
        name,
    )
    kind = _enum(wire["kind"], ("video", "image", "font"), f"{name}.kind")
    rows = _tuple(wire["landmarks"], f"{name}.landmarks", MAX_LANDMARKS_PER_ASSET)
    landmarks: list[TimingLandmark] = []
    previous_frame = previous_pts = -1
    previous_dts = -1
    for row_index, row in enumerate(rows):
        row_name = f"{name}.landmarks[{row_index}]"
        timing = _mapping(row, ("frame_index", "pts", "dts", "duration_ticks"), row_name)
        for member in ("frame_index", "pts", "dts"):
            raw = timing[member]
            if isinstance(raw, int) and not isinstance(raw, bool) and raw < 0:
                raise _reject("negative_timestamp", f"{row_name}.{member} cannot be negative")
        frame_index = _integer(
            timing["frame_index"], f"{row_name}.frame_index", 0, MAX_EXTENT_FRAMES
        )
        pts = _integer(timing["pts"], f"{row_name}.pts", 0, MAX_INTEROPERABLE_INTEGER)
        dts = _integer(timing["dts"], f"{row_name}.dts", 0, MAX_INTEROPERABLE_INTEGER)
        duration_ticks = _integer(
            timing["duration_ticks"], f"{row_name}.duration_ticks", 1, MAX_INTEROPERABLE_INTEGER
        )
        if row_index == 0 and (frame_index != 0 or pts != 0):
            raise _reject("invalid_timing", f"{name} timing must begin at frame and PTS zero")
        if frame_index <= previous_frame or pts <= previous_pts or dts < previous_dts:
            raise _reject("invalid_timing", f"{name} timing must be monotonic")
        landmarks.append(TimingLandmark(frame_index, pts, dts, duration_ticks))
        previous_frame, previous_pts, previous_dts = frame_index, pts, dts

    if kind == "video":
        if wire["source_time_base"] is None:
            raise _reject("invalid_timing", f"{name} video requires a source timebase")
        time_base = _source_time_base(wire["source_time_base"], f"{name}.source_time_base")
        frame_count = _integer(
            wire["source_frame_count"], f"{name}.source_frame_count", 1, MAX_EXTENT_FRAMES
        )
        embedded = _enum(
            wire["embedded_audio"],
            ("present_bound", "absent", "unavailable", "excluded_overlay_policy"),
            f"{name}.embedded_audio",
        )
        sample_raw = wire["source_sample_count"]
        sample_count = (
            None
            if sample_raw is None
            else _integer(sample_raw, f"{name}.source_sample_count", 1, MAX_INTEROPERABLE_INTEGER)
        )
        if embedded == "present_bound" and sample_count is None:
            raise _reject("invalid_timing", f"{name} bound audio requires a sample count")
        if any(landmark.frame_index >= frame_count for landmark in landmarks):
            raise _reject("invalid_timing", f"{name} landmark exceeds the admitted frame count")
        policy = _enum(
            wire["timestamp_policy"], ("nonnegative_monotonic_v1",), f"{name}.timestamp_policy"
        )
    else:
        if (
            any(
                wire[field] is not None
                for field in ("source_time_base", "source_frame_count", "source_sample_count")
            )
            or rows
        ):
            raise _reject("invalid_timing", f"{name} untimed asset carries timing")
        if wire["embedded_audio"] != "absent" or wire["timestamp_policy"] != "not_applicable":
            raise _reject("invalid_contract", f"{name} untimed capability is invalid")
        time_base = None
        frame_count = None
        sample_count = None
        embedded = "absent"
        policy = "not_applicable"
    return PublicAsset(
        _identifier(wire["asset_id"], f"{name}.asset_id"),
        kind,
        time_base,
        frame_count,
        sample_count,
        embedded,
        policy,
        tuple(landmarks),
    )


@dataclass(frozen=True, slots=True)
class CompositionTrack:
    track_id: str
    kind: str
    order: int
    enabled: bool
    locked: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "kind": self.kind,
            "order": self.order,
            "enabled": self.enabled,
            "locked": self.locked,
        }


def _track(value: object, index: int) -> CompositionTrack:
    name = f"tracks[{index}]"
    wire = _mapping(value, ("track_id", "kind", "order", "enabled", "locked"), name)
    return CompositionTrack(
        _identifier(wire["track_id"], f"{name}.track_id"),
        _enum(
            wire["kind"],
            ("primary_video", "video_overlay", "image_overlay", "text_overlay"),
            f"{name}.kind",
        ),
        _integer(wire["order"], f"{name}.order", 0, 7),
        _boolean(wire["enabled"], f"{name}.enabled"),
        _boolean(wire["locked"], f"{name}.locked"),
    )


@dataclass(frozen=True, slots=True)
class Transform2D:
    anchor_x_bp: int
    anchor_y_bp: int
    position_x_bp: int
    position_y_bp: int
    scale_x_bp: int
    scale_y_bp: int
    rotation_mdeg: int

    def to_wire(self) -> dict[str, int]:
        return {
            "anchor_x_bp": self.anchor_x_bp,
            "anchor_y_bp": self.anchor_y_bp,
            "position_x_bp": self.position_x_bp,
            "position_y_bp": self.position_y_bp,
            "scale_x_bp": self.scale_x_bp,
            "scale_y_bp": self.scale_y_bp,
            "rotation_mdeg": self.rotation_mdeg,
        }


def _transform(value: object, name: str) -> Transform2D:
    wire = _mapping(
        value,
        (
            "anchor_x_bp",
            "anchor_y_bp",
            "position_x_bp",
            "position_y_bp",
            "scale_x_bp",
            "scale_y_bp",
            "rotation_mdeg",
        ),
        name,
    )
    return Transform2D(
        _integer(wire["anchor_x_bp"], f"{name}.anchor_x_bp", 0, 10_000),
        _integer(wire["anchor_y_bp"], f"{name}.anchor_y_bp", 0, 10_000),
        _integer(wire["position_x_bp"], f"{name}.position_x_bp", -40_000, 40_000),
        _integer(wire["position_y_bp"], f"{name}.position_y_bp", -40_000, 40_000),
        _integer(wire["scale_x_bp"], f"{name}.scale_x_bp", 1, 80_000),
        _integer(wire["scale_y_bp"], f"{name}.scale_y_bp", 1, 80_000),
        _integer(wire["rotation_mdeg"], f"{name}.rotation_mdeg", -180_000, 180_000),
    )


@dataclass(frozen=True, slots=True)
class Crop:
    left_bp: int
    top_bp: int
    right_bp: int
    bottom_bp: int

    def to_wire(self) -> dict[str, int]:
        return {
            "left_bp": self.left_bp,
            "top_bp": self.top_bp,
            "right_bp": self.right_bp,
            "bottom_bp": self.bottom_bp,
        }


def _crop(value: object, name: str) -> Crop:
    wire = _mapping(value, ("left_bp", "top_bp", "right_bp", "bottom_bp"), name)
    crop = Crop(
        *(
            _integer(wire[key], f"{name}.{key}", 0, 9_999)
            for key in ("left_bp", "top_bp", "right_bp", "bottom_bp")
        )
    )
    if crop.left_bp + crop.right_bp >= 10_000 or crop.top_bp + crop.bottom_bp >= 10_000:
        raise _reject("invalid_contract", f"{name} removes the full image")
    return crop


def _rgba(value: object, name: str, nullable: bool = False) -> tuple[int, int, int, int] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise _reject("invalid_contract", f"{name} must be RGBA8")
    return tuple(_integer(member, f"{name}[{index}]", 0, 255) for index, member in enumerate(value))  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class TextStyle:
    content: str
    font_asset_id: str
    size_px: int
    weight: int
    style: str
    align: str
    line_height_bp: int
    fill_rgba: tuple[int, int, int, int]
    background_rgba: tuple[int, int, int, int] | None

    def to_wire(self) -> dict[str, object]:
        return {
            "content": self.content,
            "font_asset_id": self.font_asset_id,
            "size_px": self.size_px,
            "weight": self.weight,
            "style": self.style,
            "align": self.align,
            "line_height_bp": self.line_height_bp,
            "fill_rgba": list(self.fill_rgba),
            "background_rgba": None if self.background_rgba is None else list(self.background_rgba),
        }


def _text(value: object, name: str) -> TextStyle | None:
    if value is None:
        return None
    wire = _mapping(
        value,
        (
            "content",
            "font_asset_id",
            "size_px",
            "weight",
            "style",
            "align",
            "line_height_bp",
            "fill_rgba",
            "background_rgba",
        ),
        name,
    )
    content = wire["content"]
    if (
        not isinstance(content, str)
        or not 1 <= len(content) <= 2_048
        or len(content.splitlines()) > 32
    ):
        raise _reject("invalid_contract", f"{name}.content is outside text bounds")
    if any(
        ord(character) == 0 or (ord(character) < 32 and character not in "\n\t")
        for character in content
    ):
        raise _reject("invalid_contract", f"{name}.content contains a disallowed control")
    if unicodedata.normalize("NFC", content) != content:
        raise _reject("invalid_contract", f"{name}.content must already be NFC")
    weight = _integer(wire["weight"], f"{name}.weight", 400, 700)
    if weight not in (400, 700):
        raise _reject("invalid_contract", f"{name}.weight is unsupported")
    fill = _rgba(wire["fill_rgba"], f"{name}.fill_rgba")
    if fill is None:
        raise _reject("invalid_contract", f"{name}.fill_rgba is required")
    return TextStyle(
        content,
        _identifier(wire["font_asset_id"], f"{name}.font_asset_id"),
        _integer(wire["size_px"], f"{name}.size_px", 8, 512),
        weight,
        _enum(wire["style"], ("normal", "italic"), f"{name}.style"),
        _enum(wire["align"], ("left", "center", "right"), f"{name}.align"),
        _integer(wire["line_height_bp"], f"{name}.line_height_bp", 7_500, 30_000),
        fill,
        _rgba(wire["background_rgba"], f"{name}.background_rgba", nullable=True),
    )


@dataclass(frozen=True, slots=True)
class Transition:
    kind: str
    duration_frames: int

    def to_wire(self) -> dict[str, object]:
        return {"kind": self.kind, "duration_frames": self.duration_frames}


@dataclass(frozen=True, slots=True)
class Effect:
    kind: str
    brightness_permille: int
    contrast_permille: int
    saturation_permille: int

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "brightness_permille": self.brightness_permille,
            "contrast_permille": self.contrast_permille,
            "saturation_permille": self.saturation_permille,
        }


@dataclass(frozen=True, slots=True)
class ClipAudio:
    """Gain, mute and fades applied to a video clip's embedded audio.

    `muted` is kept apart from the gain so that unmuting restores it. The identity value is the
    absence of the member on the wire: `CompositionClip.to_wire` never writes it and `_clip`
    refuses it written out, so a composition made before the member existed keeps its bytes.
    """

    gain_mb: int
    muted: bool
    fade_in_frames: int
    fade_out_frames: int

    def to_wire(self) -> dict[str, object]:
        return {
            "gain_mb": self.gain_mb,
            "muted": self.muted,
            "fade_in_frames": self.fade_in_frames,
            "fade_out_frames": self.fade_out_frames,
        }


IDENTITY_CLIP_AUDIO: Final = ClipAudio(0, False, 0, 0)


def _clip_audio(value: object, name: str, duration_frames: int) -> ClipAudio:
    wire = _mapping(value, ("gain_mb", "muted", "fade_in_frames", "fade_out_frames"), name)
    audio = ClipAudio(
        _integer(
            wire["gain_mb"], f"{name}.gain_mb", CLIP_AUDIO_GAIN_MIN_MB, CLIP_AUDIO_GAIN_MAX_MB
        ),
        _boolean(wire["muted"], f"{name}.muted"),
        _integer(wire["fade_in_frames"], f"{name}.fade_in_frames", 0, CLIP_AUDIO_FADE_MAX_FRAMES),
        _integer(wire["fade_out_frames"], f"{name}.fade_out_frames", 0, CLIP_AUDIO_FADE_MAX_FRAMES),
    )
    if audio.fade_in_frames + audio.fade_out_frames > duration_frames:
        raise _reject("invalid_contract", f"{name} fades exceed the clip duration")
    # CRITICAL: one value, one wire. Admitting the identity written out gives a clip two encodings
    # and two public fingerprints for the same edit.
    if audio == IDENTITY_CLIP_AUDIO:
        raise _reject("invalid_contract", f"{name} identity must be omitted")
    return audio


@dataclass(frozen=True, slots=True)
class CompositionClip:
    clip_id: str
    asset_id: str | None
    track_id: str
    start_frame: int
    duration_frames: int
    source_start_frame: int
    enabled: bool
    transform: Transform2D
    crop: Crop
    opacity_bp: int
    blend: str
    text: TextStyle | None
    transition: Transition
    effect: Effect
    audio: ClipAudio = IDENTITY_CLIP_AUDIO

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "clip_id": self.clip_id,
            "asset_id": self.asset_id,
            "track_id": self.track_id,
            "start_frame": self.start_frame,
            "duration_frames": self.duration_frames,
            "source_start_frame": self.source_start_frame,
            "enabled": self.enabled,
            "transform": self.transform.to_wire(),
            "crop": self.crop.to_wire(),
            "opacity_bp": self.opacity_bp,
            "blend": self.blend,
            "text": None if self.text is None else self.text.to_wire(),
            "transition": self.transition.to_wire(),
            "effect": self.effect.to_wire(),
        }
        if self.audio != IDENTITY_CLIP_AUDIO:
            wire["audio"] = self.audio.to_wire()
        return wire


_CLIP_KEYS: Final = (
    "clip_id",
    "asset_id",
    "track_id",
    "start_frame",
    "duration_frames",
    "source_start_frame",
    "enabled",
    "transform",
    "crop",
    "opacity_bp",
    "blend",
    "text",
    "transition",
    "effect",
)


def _clip(value: object, index: int) -> CompositionClip:
    name = f"clips[{index}]"
    # `audio` is the one optional member: its absence is the identity value (see `ClipAudio`).
    with_audio = isinstance(value, Mapping) and "audio" in value
    wire = _mapping(value, (*_CLIP_KEYS, "audio") if with_audio else _CLIP_KEYS, name)
    duration = _integer(wire["duration_frames"], f"{name}.duration_frames", 1, MAX_EXTENT_FRAMES)
    transition_wire = _mapping(
        wire["transition"], ("kind", "duration_frames"), f"{name}.transition"
    )
    transition_kind = _enum(
        transition_wire["kind"], ("none", "cross_dissolve_v1"), f"{name}.transition.kind"
    )
    transition_duration = _integer(
        transition_wire["duration_frames"], f"{name}.transition.duration_frames", 0, 300
    )
    if (transition_kind == "none" and transition_duration != 0) or (
        transition_kind == "cross_dissolve_v1" and not 1 <= transition_duration <= duration
    ):
        raise _reject("invalid_contract", f"{name}.transition duration is invalid")
    effect_wire = _mapping(
        wire["effect"],
        ("kind", "brightness_permille", "contrast_permille", "saturation_permille"),
        f"{name}.effect",
    )
    effect = Effect(
        _enum(effect_wire["kind"], ("none", "color_adjust_v1"), f"{name}.effect.kind"),
        _integer(effect_wire["brightness_permille"], f"{name}.effect.brightness", -1_000, 1_000),
        _integer(effect_wire["contrast_permille"], f"{name}.effect.contrast", 0, 2_000),
        _integer(effect_wire["saturation_permille"], f"{name}.effect.saturation", 0, 2_000),
    )
    if effect.kind == "none" and (
        effect.brightness_permille,
        effect.contrast_permille,
        effect.saturation_permille,
    ) != (0, 1_000, 1_000):
        raise _reject("invalid_contract", f"{name}.effect none must use identity values")
    return CompositionClip(
        _identifier(wire["clip_id"], f"{name}.clip_id"),
        _optional_identifier(wire["asset_id"], f"{name}.asset_id"),
        _identifier(wire["track_id"], f"{name}.track_id"),
        _integer(wire["start_frame"], f"{name}.start_frame", 0, MAX_EXTENT_FRAMES - 1),
        duration,
        _integer(
            wire["source_start_frame"], f"{name}.source_start_frame", 0, MAX_EXTENT_FRAMES - 1
        ),
        _boolean(wire["enabled"], f"{name}.enabled"),
        _transform(wire["transform"], f"{name}.transform"),
        _crop(wire["crop"], f"{name}.crop"),
        _integer(wire["opacity_bp"], f"{name}.opacity_bp", 0, 10_000),
        _enum(wire["blend"], ("normal", "multiply", "screen"), f"{name}.blend"),
        _text(wire["text"], f"{name}.text"),
        Transition(transition_kind, transition_duration),
        effect,
        _clip_audio(wire["audio"], f"{name}.audio", duration)
        if with_audio
        else IDENTITY_CLIP_AUDIO,
    )


@dataclass(frozen=True, slots=True)
class OutputProfile:
    profile_id: str
    frame_rate: Rational
    time_base: Rational
    width: int
    height: int
    duration_frames: int
    container: str
    video_codec: str
    pixel_format: str
    pixel_aspect: Rational
    color_policy: str
    audio_policy: str
    audio_codec: str
    sample_rate: int
    channels: int
    preview_max_av_drift_samples: int
    final_impulse_tolerance_samples: int

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "frame_rate": self.frame_rate.to_wire(),
            "time_base": self.time_base.to_wire(),
            "width": self.width,
            "height": self.height,
            "duration_frames": self.duration_frames,
            "container": self.container,
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "pixel_aspect": self.pixel_aspect.to_wire(),
            "color_policy": self.color_policy,
            "audio_policy": self.audio_policy,
            "audio_codec": self.audio_codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "preview_max_av_drift_samples": self.preview_max_av_drift_samples,
            "final_impulse_tolerance_samples": self.final_impulse_tolerance_samples,
        }


_OUTPUT_KEYS = (
    "profile_id",
    "frame_rate",
    "time_base",
    "width",
    "height",
    "duration_frames",
    "container",
    "video_codec",
    "pixel_format",
    "pixel_aspect",
    "color_policy",
    "audio_policy",
    "audio_codec",
    "sample_rate",
    "channels",
    "preview_max_av_drift_samples",
    "final_impulse_tolerance_samples",
)


def _output(value: object) -> OutputProfile:
    wire = _mapping(value, _OUTPUT_KEYS, "output")
    if wire["profile_id"] != OUTPUT_PROFILE_ID:
        raise _reject("unsupported_output_profile", "output profile is unsupported")
    frame_rate = _rational(wire["frame_rate"], "output.frame_rate")
    time_base = _rational(wire["time_base"], "output.time_base")
    pixel_aspect = _rational(wire["pixel_aspect"], "output.pixel_aspect")
    if (
        frame_rate != Rational(24, 1)
        or time_base != Rational(1, 24)
        or pixel_aspect != Rational(1, 1)
    ):
        raise _reject("unsupported_output_profile", "output rational profile drifted")
    width = _integer(wire["width"], "output.width", 16, 1_920)
    height = _integer(wire["height"], "output.height", 16, 1_080)
    if width % 2 or height % 2 or width * height > 2_073_600:
        raise _reject("unsupported_output_profile", "output dimensions are unsupported")
    exact = {
        "container": "mp4",
        "video_codec": "h264",
        "pixel_format": "yuv420p",
        "color_policy": "bt709_sdr_limited_v1",
        "audio_policy": "primary_embedded_follow_video_v1",
        "audio_codec": "aac",
        "sample_rate": 48_000,
        "channels": 1,
        "preview_max_av_drift_samples": 2_000,
        "final_impulse_tolerance_samples": 2_048,
    }
    if any(wire[key] != expected for key, expected in exact.items()):
        raise _reject("unsupported_output_profile", "output profile metadata drifted")
    return OutputProfile(
        OUTPUT_PROFILE_ID,
        frame_rate,
        time_base,
        width,
        height,
        _integer(wire["duration_frames"], "output.duration_frames", 1, MAX_EXTENT_FRAMES),
        "mp4",
        "h264",
        "yuv420p",
        pixel_aspect,
        "bt709_sdr_limited_v1",
        "primary_embedded_follow_video_v1",
        "aac",
        48_000,
        1,
        2_000,
        2_048,
    )


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    engine_profile_id: str
    input_containers: tuple[str, ...]
    input_video_codecs: tuple[str, ...]
    input_audio_codecs: tuple[str, ...]
    input_pixel_formats: tuple[str, ...]
    media_transport: str
    frame_observer: str
    frame_event_fallbacks: tuple[str, ...]
    visual_compositor: str
    embedded_audio_policy: str
    cross_origin_isolation_required: bool
    mse_required: bool
    webcodecs_required: bool
    network_policy: str
    active_video_limit: int
    warm_video_limit: int
    canvas_limit: int
    pending_rvfc_limit: int
    cancel_deadline_ms: int
    teardown_deadline_ms: int
    decision_receipt_fingerprints: tuple[str, ...]
    observation_corpus_ids: tuple[str, ...]
    observation_corpus_fingerprints: tuple[str, ...]
    probe_root_keys: tuple[str, ...]
    probe_format_keys: tuple[str, ...]
    probe_video_stream_keys: tuple[str, ...]
    probe_audio_stream_keys: tuple[str, ...]
    probe_empty_array_keys: tuple[str, ...]
    audio_stream_rate_sentinel: str

    def to_wire(self) -> dict[str, object]:
        return {
            "engine_profile_id": self.engine_profile_id,
            "input_containers": list(self.input_containers),
            "input_video_codecs": list(self.input_video_codecs),
            "input_audio_codecs": list(self.input_audio_codecs),
            "input_pixel_formats": list(self.input_pixel_formats),
            "media_transport": self.media_transport,
            "frame_observer": self.frame_observer,
            "frame_event_fallbacks": list(self.frame_event_fallbacks),
            "visual_compositor": self.visual_compositor,
            "embedded_audio_policy": self.embedded_audio_policy,
            "cross_origin_isolation_required": self.cross_origin_isolation_required,
            "mse_required": self.mse_required,
            "webcodecs_required": self.webcodecs_required,
            "network_policy": self.network_policy,
            "active_video_limit": self.active_video_limit,
            "warm_video_limit": self.warm_video_limit,
            "canvas_limit": self.canvas_limit,
            "pending_rvfc_limit": self.pending_rvfc_limit,
            "cancel_deadline_ms": self.cancel_deadline_ms,
            "teardown_deadline_ms": self.teardown_deadline_ms,
            "decision_receipt_fingerprints": list(self.decision_receipt_fingerprints),
            "observation_corpus_ids": list(self.observation_corpus_ids),
            "observation_corpus_fingerprints": list(self.observation_corpus_fingerprints),
            "probe_root_keys": list(self.probe_root_keys),
            "probe_format_keys": list(self.probe_format_keys),
            "probe_video_stream_keys": list(self.probe_video_stream_keys),
            "probe_audio_stream_keys": list(self.probe_audio_stream_keys),
            "probe_empty_array_keys": list(self.probe_empty_array_keys),
            "audio_stream_rate_sentinel": self.audio_stream_rate_sentinel,
        }


def _selected_capability() -> CapabilityProfile:
    return CapabilityProfile(
        engine_profile_id=ENGINE_PROFILE_ID,
        input_containers=("mp4",),
        input_video_codecs=("h264",),
        input_audio_codecs=("aac",),
        input_pixel_formats=("yuv420p",),
        media_transport="intrinsic_html_media_element_v1",
        frame_observer="request_video_frame_callback_v1",
        frame_event_fallbacks=("seeked", "timeupdate"),
        visual_compositor="canvas2d_ladder_v2",
        embedded_audio_policy="primary_embedded_follow_video_v1",
        cross_origin_isolation_required=False,
        mse_required=False,
        webcodecs_required=False,
        network_policy="same_origin_bounded_body_only_v1",
        active_video_limit=2,
        warm_video_limit=1,
        canvas_limit=1,
        pending_rvfc_limit=2,
        cancel_deadline_ms=250,
        teardown_deadline_ms=500,
        decision_receipt_fingerprints=(
            "sha256:3cd21f23932be62a4dfca721ce4cf2f93d78df7ce2210412c5155c159ddea797",
            "sha256:635892d0b5a513ec19980bf573493d1ba558c6d007cc8b8ac4884ef59c0e8e4b",
        ),
        observation_corpus_ids=("cfr", "invalid", "lane", "mse", "truncated", "vfr"),
        observation_corpus_fingerprints=(
            "sha256:1b77cf5e99d63696f613e9be79ef29c0d99fd8914da111b631bfdb69958cb603",
            "sha256:f7941036b79edad65b72beff2b81e9160624c1c759da2c8e875645992e504aae",
            "sha256:159d4c24e722ca8d085c2212fd9c9ffe92422b7413443dce20923a0bf3cb7c9c",
            "sha256:80173360d0925747b3ccdebce453eb2c13f8a748ae619214e94462d541d13fb9",
            "sha256:b24953b14f1255a49917b20f1680d2adfe2c056e99859396df4ab691c6a2f754",
            "sha256:e17aae228364b5d73d8eee1e5b0b068de176c6031d1c17f1b4ba5ff392985e16",
        ),
        probe_root_keys=("format", "programs", "stream_groups", "streams"),
        probe_format_keys=("duration", "format_name"),
        probe_video_stream_keys=(
            "avg_frame_rate",
            "codec_name",
            "codec_type",
            "height",
            "pix_fmt",
            "width",
        ),
        probe_audio_stream_keys=(
            "avg_frame_rate",
            "channel_layout",
            "channels",
            "codec_name",
            "codec_type",
            "sample_rate",
        ),
        probe_empty_array_keys=("programs", "stream_groups"),
        audio_stream_rate_sentinel="0/0",
    )


def _capability(value: object) -> CapabilityProfile:
    selected = _selected_capability()
    expected = selected.to_wire()
    wire = _mapping(value, tuple(expected), "capability")
    try:
        exact_match = canonical_bytes(dict(wire)) == canonical_bytes(expected)
    except ContractValidationError:
        exact_match = False
    if not exact_match:
        raise _reject("unsupported_profile", "capability profile drifted")
    return selected


@dataclass(frozen=True, slots=True)
class CompositionBlocker:
    code: str
    subject_id: str | None

    def to_wire(self) -> dict[str, object]:
        return {"code": self.code, "subject_id": self.subject_id}


def _blocker(value: object, index: int) -> CompositionBlocker:
    wire = _mapping(value, ("code", "subject_id"), f"blockers[{index}]")
    return CompositionBlocker(
        _enum(wire["code"], BLOCKER_CODES, f"blockers[{index}].code"),
        _optional_identifier(wire["subject_id"], f"blockers[{index}].subject_id"),
    )


@dataclass(frozen=True, slots=True)
class AudioExtension:
    schema: str
    track_profile: str
    command_namespace: str
    command_members: tuple[str, ...]
    preview_edit_capability: str
    final_render_edit_capability: str
    embedded_renderer_variant: str
    independent_audio_renderer_variant: str
    reason: str

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "track_profile": self.track_profile,
            "command_namespace": self.command_namespace,
            "command_members": list(self.command_members),
            "preview_edit_capability": self.preview_edit_capability,
            "final_render_edit_capability": self.final_render_edit_capability,
            "embedded_renderer_variant": self.embedded_renderer_variant,
            "independent_audio_renderer_variant": self.independent_audio_renderer_variant,
            "reason": self.reason,
        }


def _audio_extension(value: object) -> AudioExtension:
    keys = (
        "schema",
        "track_profile",
        "command_namespace",
        "command_members",
        "preview_edit_capability",
        "final_render_edit_capability",
        "embedded_renderer_variant",
        "independent_audio_renderer_variant",
        "reason",
    )
    wire = _mapping(value, keys, "audio_extension")
    exact: tuple[object, ...] = (
        AUDIO_EXTENSION_SCHEMA,
        "none_v1",
        INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
        [],
        "unsupported",
        "unsupported",
        "EmbeddedAudioSpanV1",
        "none_v1",
        "audio_editing_deferred",
    )
    if tuple(wire[key] for key in keys) != exact:
        raise _reject("audio_editing_deferred", "independent audio extension is closed")
    return AudioExtension(
        AUDIO_EXTENSION_SCHEMA,
        "none_v1",
        INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
        (),
        "unsupported",
        "unsupported",
        "EmbeddedAudioSpanV1",
        "none_v1",
        "audio_editing_deferred",
    )


@dataclass(frozen=True, slots=True)
class RenderVocabulary:
    schema: str
    request_schema: str
    job_schema: str
    receipt_schema: str
    states: tuple[str, ...]
    terminal_states: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_schema": self.request_schema,
            "job_schema": self.job_schema,
            "receipt_schema": self.receipt_schema,
            "states": list(self.states),
            "terminal_states": list(self.terminal_states),
            "reasons": list(self.reasons),
        }


def _render_vocabulary(value: object) -> RenderVocabulary:
    keys = (
        "schema",
        "request_schema",
        "job_schema",
        "receipt_schema",
        "states",
        "terminal_states",
        "reasons",
    )
    wire = _mapping(value, keys, "render_vocabulary")
    expected = RenderVocabulary(
        RENDER_VOCABULARY_SCHEMA,
        "h3.context.authoring_render_request.v1",
        "h3.context.authoring_render_job.v1",
        "h3.context.authoring_render_receipt.v1",
        RENDER_JOB_STATES,
        RENDER_TERMINAL_STATES,
        RENDER_TERMINAL_REASONS,
    )
    try:
        actual = RenderVocabulary(
            str(wire["schema"]),
            str(wire["request_schema"]),
            str(wire["job_schema"]),
            str(wire["receipt_schema"]),
            tuple(wire["states"]),  # type: ignore[arg-type]
            tuple(wire["terminal_states"]),  # type: ignore[arg-type]
            tuple(wire["reasons"]),  # type: ignore[arg-type]
        )
    except TypeError as exc:
        raise _reject("invalid_contract", "render vocabulary arrays are invalid") from exc
    if actual != expected:
        raise _reject("unsupported_profile", "render vocabulary drifted")
    return expected


@dataclass(frozen=True, slots=True)
class PublicCompositionSnapshot:
    schema: str
    profile_id: str
    operation_profile_id: str
    project_id: str
    workspace_handle: str
    workspace_revision: int
    workspace_fingerprint: str
    timeline_revision: int
    timeline_fingerprint: str
    public_fingerprint: str
    output: OutputProfile
    capability: CapabilityProfile
    assets: tuple[PublicAsset, ...]
    tracks: tuple[CompositionTrack, ...]
    clips: tuple[CompositionClip, ...]
    audio_extension: AudioExtension
    blockers: tuple[CompositionBlocker, ...]
    render_vocabulary: RenderVocabulary

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "operation_profile_id": self.operation_profile_id,
            "project_id": self.project_id,
            "workspace_handle": self.workspace_handle,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "timeline_revision": self.timeline_revision,
            "timeline_fingerprint": self.timeline_fingerprint,
            "public_fingerprint": self.public_fingerprint,
            "output": self.output.to_wire(),
            "capability": self.capability.to_wire(),
            "assets": [item.to_wire() for item in self.assets],
            "tracks": [item.to_wire() for item in self.tracks],
            "clips": [item.to_wire() for item in self.clips],
            "audio_extension": self.audio_extension.to_wire(),
            "blockers": [item.to_wire() for item in self.blockers],
            "render_vocabulary": self.render_vocabulary.to_wire(),
        }


_SNAPSHOT_KEYS = (
    "schema",
    "profile_id",
    "operation_profile_id",
    "project_id",
    "workspace_handle",
    "workspace_revision",
    "workspace_fingerprint",
    "timeline_revision",
    "timeline_fingerprint",
    "public_fingerprint",
    "output",
    "capability",
    "assets",
    "tracks",
    "clips",
    "audio_extension",
    "blockers",
    "render_vocabulary",
)


def _normalize_composition_fingerprint_value(
    value: object, depth: int, field: str = "value"
) -> object:
    """Canonicalize composition material without widening the shared global array budget."""

    if depth > MAX_CANONICAL_DEPTH:
        raise CanonicalizationError("canonical value exceeds maximum nesting depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_INTEROPERABLE_INTEGER <= value <= MAX_INTEROPERABLE_INTEGER:
            raise CanonicalizationError(f"{field} integer exceeds interoperable range")
        return value
    if isinstance(value, float):
        raise CanonicalizationError(
            f"{field} contains a raw float; encode it with an explicit binary token"
        )
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        utf16_units = len(normalized) + sum(ord(character) > 0xFFFF for character in normalized)
        if (
            len(normalized) > MAX_CANONICAL_STRING_LENGTH
            or utf16_units > MAX_CANONICAL_STRING_LENGTH
        ):
            raise CanonicalizationError(f"{field} exceeds canonical string limit")
        if any(
            ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in normalized
        ):
            raise CanonicalizationError(f"{field} contains an unsafe Unicode code point")
        return normalized
    if isinstance(value, Mapping):
        if len(value) > MAX_CANONICAL_ITEMS:
            raise CanonicalizationError(f"{field} mapping exceeds collection limit")
        normalized_mapping: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _IDENTIFIER.fullmatch(key) is None:
                raise CanonicalizationError(
                    "canonical object keys must be controlled ASCII identifiers"
                )
            normalized_mapping[key] = _normalize_composition_fingerprint_value(
                item, depth + 1, f"{field}.{key}"
            )
        return {key: normalized_mapping[key] for key in sorted(normalized_mapping)}
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LANDMARKS:
            raise CanonicalizationError(f"{field} array exceeds composition collection limit")
        return [
            _normalize_composition_fingerprint_value(item, depth + 1, f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    raise CanonicalizationError(f"{field} contains unsupported type {type(value).__name__}")


def composition_contract_fingerprint(value: object) -> str:
    """Fingerprint exact composition material under its already-closed 2,048-row budget."""

    # CRITICAL: Production can contribute 512 exact timing rows per imported asset. Keep the
    # general canonicalizer at 256; only this contract-specific path may reach MAX_LANDMARKS.
    normalized = _normalize_composition_fingerprint_value(value, 0)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CanonicalizationError(
            "composition value cannot be encoded as canonical UTF-8 JSON"
        ) from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise CanonicalizationError("canonical composition JSON exceeds byte limit")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def public_snapshot_fingerprint(value: Mapping[str, object]) -> str:
    """Fingerprint all public fields except the fingerprint member itself."""

    if not isinstance(value, Mapping):
        raise _reject("invalid_contract", "snapshot must be an object")
    # CRITICAL: exclude only this self-referential public member.  Excluding any other field lets
    # browser and backend consume semantically different edits under one render identity.
    material = {key: item for key, item in value.items() if key != "public_fingerprint"}
    try:
        return composition_contract_fingerprint(material)
    except ContractValidationError as exc:
        raise _reject("resource_limit", "snapshot exceeds canonical bounds") from exc


def decode_public_snapshot(value: object) -> PublicCompositionSnapshot:
    wire = _mapping(value, _SNAPSHOT_KEYS, "snapshot")
    if (
        wire["schema"] == PRIVATE_SOURCE_MANIFEST_SCHEMA
        or wire["schema"] == DERIVATIVE_MANIFEST_SCHEMA
    ):
        raise _reject("private_field", "private manifests cannot cross the public boundary")
    if wire["schema"] != PUBLIC_SNAPSHOT_SCHEMA or wire["profile_id"] != ENGINE_PROFILE_ID:
        raise _reject("unsupported_profile", "public composition profile is unsupported")
    if wire["operation_profile_id"] != OPERATION_PROFILE_ID:
        raise _reject("unsupported_profile", "operation profile is unsupported")
    output = _output(wire["output"])
    capability = _capability(wire["capability"])
    asset_rows = _tuple(wire["assets"], "assets", MAX_ASSETS)
    track_rows = _tuple(wire["tracks"], "tracks", MAX_TRACKS)
    clip_rows = _tuple(wire["clips"], "clips", MAX_CLIPS)
    blocker_rows = _tuple(wire["blockers"], "blockers", MAX_BLOCKERS)
    assets = tuple(_asset(item, index) for index, item in enumerate(asset_rows))
    if sum(len(asset.landmarks) for asset in assets) > MAX_LANDMARKS:
        raise _reject("resource_limit", "snapshot landmark total exceeds the profile")
    tracks = tuple(_track(item, index) for index, item in enumerate(track_rows))
    clips = tuple(_clip(item, index) for index, item in enumerate(clip_rows))
    blockers = tuple(_blocker(item, index) for index, item in enumerate(blocker_rows))

    asset_by_id = {asset.asset_id: asset for asset in assets}
    track_by_id = {track.track_id: track for track in tracks}
    if (
        len(asset_by_id) != len(assets)
        or len(track_by_id) != len(tracks)
        or len({clip.clip_id for clip in clips}) != len(clips)
    ):
        raise _reject("invalid_contract", "asset, track and clip IDs must be unique")
    if len({track.order for track in tracks}) != len(tracks):
        raise _reject("invalid_contract", "track order must be unique")
    if sum(track.kind == "primary_video" for track in tracks) != 1:
        raise _reject("invalid_contract", "exactly one primary video track is required")
    # CRITICAL: validate every track reference before transition traversal. A later malformed clip
    # must not escape the closed rejection contract as a raw mapping KeyError.
    if any(clip.track_id not in track_by_id for clip in clips):
        raise _reject("invalid_contract", "clip references an unknown track")
    for clip in clips:
        track = track_by_id[clip.track_id]
        if clip.start_frame + clip.duration_frames > output.duration_frames:
            raise _reject("source_range_unavailable", "clip exceeds output duration")
        bound_audio = False
        if track.kind == "text_overlay":
            if clip.asset_id is not None or clip.text is None or clip.source_start_frame != 0:
                raise _reject("invalid_contract", "text clip shape is invalid")
            font = asset_by_id.get(clip.text.font_asset_id)
            if font is None or font.kind != "font":
                raise _reject("invalid_contract", "text clip font is not admitted")
        else:
            if clip.text is not None or clip.asset_id is None:
                raise _reject("invalid_contract", "media clip shape is invalid")
            asset = asset_by_id.get(clip.asset_id)
            required_kind = "video" if track.kind in ("primary_video", "video_overlay") else "image"
            if asset is None or asset.kind != required_kind:
                raise _reject("invalid_contract", "clip asset kind does not match its track")
            # CRITICAL: overlay exclusion is track policy, not measured source metadata. Requiring
            # a relabelled asset rejects real sources and makes primary/overlay reuse contradictory.
            # Legacy policy labels stay readable; new render admission requires measured facts.
            if asset.kind == "video":
                if asset.source_frame_count is None:
                    raise _reject("invalid_timing", "video source frame count is missing")
                if clip.source_start_frame >= asset.source_frame_count:
                    raise _reject("source_range_unavailable", "clip exceeds admitted source range")
            elif clip.source_start_frame != 0:
                raise _reject(
                    "source_range_unavailable", "untimed media cannot carry a source offset"
                )
            # Only a video asset can declare bound audio (`_asset`).
            bound_audio = asset.embedded_audio == "present_bound"
        # A clip's audio member scales its own source's embedded audio, so it is admitted only
        # where there is bound audio to scale. Track placement does not decide it.
        if clip.audio != IDENTITY_CLIP_AUDIO and not bound_audio:
            raise _reject("invalid_contract", "clip audio adjustments need bound audio")
        if clip.transition.kind == "cross_dissolve_v1":
            lower_participants = (
                other
                for other in clips
                if other.clip_id != clip.clip_id
                and other.enabled
                and track_by_id[other.track_id].enabled
                and track_by_id[other.track_id].order < track.order
                and other.start_frame <= clip.start_frame
                and other.start_frame + other.duration_frames
                >= clip.start_frame + clip.transition.duration_frames
            )
            if next(lower_participants, None) is None:
                raise _reject(
                    "invalid_contract",
                    "cross dissolve requires a lower layer for its full duration",
                )

    video_clips = tuple(
        clip
        for clip in clips
        if clip.enabled
        and track_by_id[clip.track_id].enabled
        and track_by_id[clip.track_id].kind in ("primary_video", "video_overlay")
    )
    # IMPORTANT: this is an admission bound from the accepted engine profile, not a runtime hint.
    # Allowing a third overlapping VIDEO here would force M25-12 either to hide a layer or allocate
    # outside its qualified owner budget, making the public fingerprint describe an
    # unrenderable scene.
    if any(
        sum(
            candidate.start_frame
            <= clip.start_frame
            < candidate.start_frame + candidate.duration_frames
            for candidate in video_clips
        )
        > capability.active_video_limit
        for clip in video_clips
    ):
        raise _reject("resource_limit", "active video layers exceed the engine profile")

    fingerprint = _fingerprint(wire["public_fingerprint"], "snapshot.public_fingerprint")
    expected = public_snapshot_fingerprint(wire)
    if fingerprint != expected:
        raise _reject("stale_snapshot", "public fingerprint does not match the snapshot")
    snapshot = PublicCompositionSnapshot(
        PUBLIC_SNAPSHOT_SCHEMA,
        ENGINE_PROFILE_ID,
        OPERATION_PROFILE_ID,
        _identifier(wire["project_id"], "snapshot.project_id"),
        _identifier(wire["workspace_handle"], "snapshot.workspace_handle"),
        _integer(wire["workspace_revision"], "snapshot.workspace_revision", 0, MAX_EXTENT_FRAMES),
        _fingerprint(wire["workspace_fingerprint"], "snapshot.workspace_fingerprint"),
        _integer(wire["timeline_revision"], "snapshot.timeline_revision", 0, MAX_EXTENT_FRAMES),
        _fingerprint(wire["timeline_fingerprint"], "snapshot.timeline_fingerprint"),
        fingerprint,
        output,
        capability,
        assets,
        tracks,
        clips,
        _audio_extension(wire["audio_extension"]),
        blockers,
        _render_vocabulary(wire["render_vocabulary"]),
    )
    if snapshot.to_wire() != value:
        raise _reject("invalid_contract", "snapshot is not in its canonical typed shape")
    return snapshot


def decode_public_snapshot_json(value: str) -> PublicCompositionSnapshot:
    if not isinstance(value, str):
        raise _reject("invalid_contract", "snapshot JSON must be text")

    def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise _reject("invalid_contract", "snapshot JSON contains a duplicate member")
            result[key] = item
        return result

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=closed_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                _reject("invalid_contract", "non-finite JSON number")
            ),
        )
    except CompositionContractError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise _reject("invalid_contract", "snapshot JSON is invalid") from exc
    return decode_public_snapshot(decoded)


@dataclass(frozen=True, slots=True)
class ResolvedLayer:
    clip_id: str
    asset_id: str | None
    track_id: str
    source_frame: int | None
    source_pts: int | None
    transition_elapsed_frames: int | None
    operation_ids: tuple[str, ...]
    transform: Transform2D
    crop: Crop
    opacity_bp: int
    blend: str
    text: TextStyle | None
    effect: Effect

    def to_wire(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "asset_id": self.asset_id,
            "track_id": self.track_id,
            "source_frame": self.source_frame,
            "source_pts": self.source_pts,
            "transition_elapsed_frames": self.transition_elapsed_frames,
            "operation_ids": list(self.operation_ids),
            "transform": self.transform.to_wire(),
            "crop": self.crop.to_wire(),
            "opacity_bp": self.opacity_bp,
            "blend": self.blend,
            "text": None if self.text is None else self.text.to_wire(),
            "effect": self.effect.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class EmbeddedAudioSpan:
    asset_id: str
    clip_id: str
    output_start_sample: int
    output_end_sample: int
    source_start_sample: int
    source_end_sample: int

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "clip_id": self.clip_id,
            "output_start_sample": self.output_start_sample,
            "output_end_sample": self.output_end_sample,
            "source_start_sample": self.source_start_sample,
            "source_end_sample": self.source_end_sample,
        }


@dataclass(frozen=True, slots=True)
class ResolvedScene:
    schema: str
    profile_id: str
    public_fingerprint: str
    frame: int
    layers: tuple[ResolvedLayer, ...]
    audio_span: EmbeddedAudioSpan | None
    blockers: tuple[CompositionBlocker, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "public_fingerprint": self.public_fingerprint,
            "frame": self.frame,
            "layers": [item.to_wire() for item in self.layers],
            "audio_span": None if self.audio_span is None else self.audio_span.to_wire(),
            "blockers": [item.to_wire() for item in self.blockers],
        }

    def to_canonical_bytes(self) -> bytes:
        encoded = canonical_bytes(self.to_wire())
        if len(encoded) > MAX_RESOLVED_SCENE_BYTES:
            raise _reject("resource_limit", "resolved scene exceeds its byte budget")
        return encoded


def _source_target_tick_exact(
    asset: PublicAsset,
    source_start_frame: int,
    elapsed_frames: int,
    output_frame_rate: Rational,
    *,
    allow_source_end: bool = False,
) -> Fraction | None:
    if asset.source_time_base is None:
        raise _reject("invalid_timing", "video source timebase is missing")
    base = next(
        (item for item in asset.landmarks if item.frame_index == source_start_frame),
        None,
    )
    if base is None:
        return None
    # CRITICAL: edit boundaries and coverage endpoints remain exact rational source time.
    # Flooring here admits fractional overruns and lets output-frame deltas invent source frames.
    target_tick = Fraction(base.pts) + Fraction(
        elapsed_frames * output_frame_rate.den * asset.source_time_base.den,
        output_frame_rate.num * asset.source_time_base.num,
    )
    source_end = Fraction(asset.landmarks[-1].pts + asset.landmarks[-1].duration_ticks)
    if target_tick > source_end or (target_tick == source_end and not allow_source_end):
        raise _reject("source_range_unavailable", "source target exceeds declared timing")
    return target_tick


def _source_target_tick(
    asset: PublicAsset,
    source_start_frame: int,
    elapsed_frames: int,
    output_frame_rate: Rational,
    *,
    allow_source_end: bool = False,
) -> int | None:
    # IMPORTANT: source_start_frame is a frame identity, not a PTS tick. Sampling deliberately
    # floors the shared exact source-time authority to select the preceding admitted packet.
    exact = _source_target_tick_exact(
        asset,
        source_start_frame,
        elapsed_frames,
        output_frame_rate,
        allow_source_end=allow_source_end,
    )
    if exact is None:
        return None
    return exact.numerator // exact.denominator


def resolve_source_interval_coverage(
    asset: PublicAsset,
    source_start_frame: int,
    duration_frames: int,
    output_frame_rate: Rational,
) -> tuple[Fraction, Fraction]:
    """Resolve a half-open output interval onto one admitted source PTS range.

    The returned pair is ``(source_start_tick, source_end_tick)``. The start must name an exact
    admitted landmark, and the elapsed output duration must remain inside the declared source end.
    """

    if not isinstance(asset, PublicAsset) or asset.kind != "video":
        raise _reject("invalid_contract", "source interval requires an admitted video asset")
    if not isinstance(output_frame_rate, Rational):
        raise _reject("invalid_contract", "source interval requires a rational output frame rate")
    _integer(source_start_frame, "source_start_frame", 0, MAX_EXTENT_FRAMES - 1)
    _integer(duration_frames, "duration_frames", 1, MAX_EXTENT_FRAMES)
    if output_frame_rate.num < 1 or output_frame_rate.den < 1:
        raise _reject("invalid_timing", "output frame rate must be positive")
    start_tick = _source_target_tick_exact(asset, source_start_frame, 0, output_frame_rate)
    end_tick = _source_target_tick_exact(
        asset,
        source_start_frame,
        duration_frames,
        output_frame_rate,
        allow_source_end=True,
    )
    if start_tick is None or end_tick is None:
        raise _reject(
            "source_range_unavailable",
            "source interval lacks an exact admitted timing landmark",
        )
    return start_tick, end_tick


def resolve_source_landmark_after_elapsed(
    asset: PublicAsset,
    source_start_frame: int,
    elapsed_frames: int,
    output_frame_rate: Rational,
) -> int:
    """Return the admitted source-frame identity at one exact output-time boundary."""

    if not isinstance(asset, PublicAsset) or asset.kind != "video":
        raise _reject("invalid_contract", "source boundary requires an admitted video asset")
    if not isinstance(output_frame_rate, Rational):
        raise _reject("invalid_contract", "source boundary requires a rational output frame rate")
    _integer(source_start_frame, "source_start_frame", 0, MAX_EXTENT_FRAMES - 1)
    _integer(elapsed_frames, "elapsed_frames", -MAX_EXTENT_FRAMES, MAX_EXTENT_FRAMES)
    target_tick = _source_target_tick_exact(
        asset,
        source_start_frame,
        elapsed_frames,
        output_frame_rate,
    )
    if target_tick is not None:
        landmark = next(
            (item for item in asset.landmarks if Fraction(item.pts) == target_tick),
            None,
        )
        if landmark is not None:
            return landmark.frame_index
    raise _reject(
        "source_range_unavailable",
        "source boundary lacks an exact admitted timing landmark",
    )


def source_intervals_are_contiguous(
    asset: PublicAsset,
    left_source_start_frame: int,
    left_duration_frames: int,
    right_source_start_frame: int,
    output_frame_rate: Rational,
) -> bool:
    """Compare a left interval end with a right exact landmark in source time."""

    if not isinstance(asset, PublicAsset) or asset.kind != "video":
        raise _reject("invalid_contract", "source continuity requires an admitted video asset")
    if not isinstance(output_frame_rate, Rational):
        raise _reject("invalid_contract", "source continuity requires a rational output frame rate")
    _integer(left_source_start_frame, "left_source_start_frame", 0, MAX_EXTENT_FRAMES - 1)
    _integer(left_duration_frames, "left_duration_frames", 1, MAX_EXTENT_FRAMES)
    _integer(right_source_start_frame, "right_source_start_frame", 0, MAX_EXTENT_FRAMES - 1)
    left_end = _source_target_tick_exact(
        asset,
        left_source_start_frame,
        left_duration_frames,
        output_frame_rate,
        allow_source_end=True,
    )
    right_start = _source_target_tick_exact(
        asset,
        right_source_start_frame,
        0,
        output_frame_rate,
    )
    if left_end is None or right_start is None:
        raise _reject(
            "source_range_unavailable",
            "source continuity lacks an exact admitted timing landmark",
        )
    return left_end == right_start


def _source_at_frame(
    asset: PublicAsset,
    source_start_frame: int,
    elapsed_frames: int,
    output_frame_rate: Rational,
) -> tuple[int | None, int | None, int | None]:
    target_tick = _source_target_tick(asset, source_start_frame, elapsed_frames, output_frame_rate)
    if target_tick is None:
        return None, None, None
    selected: TimingLandmark | None = None
    for landmark in asset.landmarks:
        if landmark.pts > target_tick:
            break
        selected = landmark
    if selected is None:
        return None, None, target_tick
    return selected.frame_index, selected.pts, target_tick


def resolve_composition(snapshot: PublicCompositionSnapshot, frame: int) -> ResolvedScene:
    if not isinstance(snapshot, PublicCompositionSnapshot):
        raise _reject("invalid_contract", "resolver requires a typed public snapshot")
    if (
        isinstance(frame, bool)
        or not isinstance(frame, int)
        or not 0 <= frame < snapshot.output.duration_frames
    ):
        raise _reject("source_range_unavailable", "output frame is outside the composition")
    track_by_id = {track.track_id: track for track in snapshot.tracks}
    asset_by_id = {asset.asset_id: asset for asset in snapshot.assets}
    active = [
        clip
        for clip in snapshot.clips
        if clip.enabled
        and track_by_id[clip.track_id].enabled
        and clip.start_frame <= frame < clip.start_frame + clip.duration_frames
    ]
    work_units = len(snapshot.clips) + len(active) * 3
    if work_units > MAX_RESOLVER_WORK_UNITS or len(active) > MAX_RESOLVED_LAYERS:
        raise _reject("resource_limit", "resolver work exceeds the closed budget")
    active.sort(key=lambda clip: (track_by_id[clip.track_id].order, clip.start_frame, clip.clip_id))
    layers: list[ResolvedLayer] = []
    blockers = list(snapshot.blockers)
    timing_by_clip: dict[str, tuple[int | None, int | None, int | None]] = {}
    for clip in active:
        track = track_by_id[clip.track_id]
        local_frame = frame - clip.start_frame
        source_frame = source_pts = source_target_tick = None
        if clip.asset_id is not None and asset_by_id[clip.asset_id].kind == "video":
            asset = asset_by_id[clip.asset_id]
            source_frame, source_pts, source_target_tick = _source_at_frame(
                asset,
                clip.source_start_frame,
                local_frame,
                snapshot.output.frame_rate,
            )
            if source_frame is None:
                blockers.append(CompositionBlocker("timing_unavailable", clip.clip_id))
        timing_by_clip[clip.clip_id] = (source_frame, source_pts, source_target_tick)
        operation_ids = ["SelectSourceRangeV1", "CropV1", "Transform2DV1"]
        if clip.effect.kind != "none":
            operation_ids.append("ColorAdjustV1")
        operation_ids.extend(("OpacityV1", "BlendV1"))
        transition_elapsed = None
        if (
            clip.transition.kind == "cross_dissolve_v1"
            and local_frame < clip.transition.duration_frames
        ):
            transition_elapsed = local_frame
            operation_ids.append("CrossDissolveV1")
        if track.kind == "text_overlay":
            operation_ids.append("DrawTextV1")
        layers.append(
            ResolvedLayer(
                clip.clip_id,
                clip.asset_id,
                clip.track_id,
                source_frame,
                source_pts,
                transition_elapsed,
                tuple(operation_ids),
                clip.transform,
                clip.crop,
                clip.opacity_bp,
                clip.blend,
                clip.text,
                clip.effect,
            )
        )

    # CRITICAL: only track role selects audio owners; an audible overlay (including the same
    # asset as a primary clip) must never add a second span or rewrite the source's audio facts.
    primary = [clip for clip in active if track_by_id[clip.track_id].kind == "primary_video"]
    owner = max(primary, key=lambda clip: (clip.start_frame, clip.clip_id), default=None)
    audio_span = None
    if owner is not None and owner.asset_id is not None:
        asset = asset_by_id[owner.asset_id]
        if asset.embedded_audio == "present_bound":
            source_frame, _, source_target_tick = timing_by_clip[owner.clip_id]
            if (
                source_frame is None
                or source_target_tick is None
                or asset.source_sample_count is None
                or asset.source_time_base is None
            ):
                blockers.append(CompositionBlocker("embedded_audio_unavailable", owner.clip_id))
            else:
                output_start = (frame * 48_000) // 24
                output_end = ((frame + 1) * 48_000) // 24
                source_end_tick = _source_target_tick(
                    asset,
                    owner.source_start_frame,
                    frame - owner.start_frame + 1,
                    snapshot.output.frame_rate,
                    allow_source_end=True,
                )
                if source_end_tick is None:
                    blockers.append(CompositionBlocker("embedded_audio_unavailable", owner.clip_id))
                    source_end_tick = source_target_tick
                source_start = (
                    source_target_tick * asset.source_time_base.num * 48_000
                ) // asset.source_time_base.den
                source_end = (
                    source_end_tick * asset.source_time_base.num * 48_000
                ) // asset.source_time_base.den
                # IMPORTANT: adjacent output frames can quantize to one coarse source tick. Never
                # emit a zero-length span that the browser correctly rejects as invalid timing.
                if source_end <= source_start or source_end > asset.source_sample_count:
                    blockers.append(CompositionBlocker("embedded_audio_unavailable", owner.clip_id))
                else:
                    audio_span = EmbeddedAudioSpan(
                        asset.asset_id,
                        owner.clip_id,
                        output_start,
                        output_end,
                        source_start,
                        source_end,
                    )
        elif asset.embedded_audio == "unavailable":
            blockers.append(CompositionBlocker("embedded_audio_unavailable", owner.clip_id))
    unique_blockers = tuple(dict.fromkeys(blockers))
    scene = ResolvedScene(
        RESOLVED_SCENE_SCHEMA,
        ENGINE_PROFILE_ID,
        snapshot.public_fingerprint,
        frame,
        tuple(layers),
        audio_span,
        unique_blockers,
    )
    scene.to_canonical_bytes()
    return scene


def operation_disposition(operation_id: str) -> str:
    if not isinstance(operation_id, str):
        raise _reject("operation_not_in_profile", "operation ID is invalid")
    if operation_id.startswith(INDEPENDENT_AUDIO_COMMAND_NAMESPACE + "."):
        raise _reject("audio_editing_deferred", "independent audio editing is deferred")
    if operation_id not in NLE_OPERATION_IDS:
        raise _reject("operation_not_in_profile", "operation ID is outside the profile")
    return "accepted"


@dataclass(frozen=True, slots=True)
class PrivateSourceManifest:
    schema: str
    asset_id: str
    generation: int
    source_fingerprint: str
    runtime_identity: str

    def __post_init__(self) -> None:
        if self.schema != PRIVATE_SOURCE_MANIFEST_SCHEMA:
            raise _reject("invalid_contract", "private source manifest schema is unsupported")
        _identifier(self.asset_id, "asset_id")
        _integer(self.generation, "generation", 1, MAX_EXTENT_FRAMES)
        _fingerprint(self.source_fingerprint, "source_fingerprint")
        _identifier(self.runtime_identity, "runtime_identity")


@dataclass(frozen=True, slots=True)
class DerivativeManifest:
    schema: str
    asset_id: str
    original_fingerprint: str
    generator_profile_id: str
    derivative_fingerprint: str
    generation: int

    def __post_init__(self) -> None:
        if self.schema != DERIVATIVE_MANIFEST_SCHEMA:
            raise _reject("invalid_contract", "derivative manifest schema is unsupported")
        _identifier(self.asset_id, "asset_id")
        _fingerprint(self.original_fingerprint, "original_fingerprint")
        _identifier(self.generator_profile_id, "generator_profile_id")
        _fingerprint(self.derivative_fingerprint, "derivative_fingerprint")
        _integer(self.generation, "generation", 1, MAX_EXTENT_FRAMES)


def _default_transform() -> dict[str, int]:
    return {
        "anchor_x_bp": 5_000,
        "anchor_y_bp": 5_000,
        "position_x_bp": 0,
        "position_y_bp": 0,
        "scale_x_bp": 10_000,
        "scale_y_bp": 10_000,
        "rotation_mdeg": 0,
    }


def adapt_authoring_projection(
    projection: Mapping[str, object],
    *,
    project_id: str,
    workspace_revision: int,
    workspace_fingerprint: str,
    output: object,
    asset_timings: Mapping[str, object],
) -> PublicCompositionSnapshot:
    projection_wire = _mapping(
        projection,
        (
            "schema",
            "workspace_handle",
            "context_source_id",
            "task_mode",
            "registry_fingerprint",
            "reference",
            "availability",
            "timeline",
            "rejection",
        ),
        "legacy_projection",
    )
    _reject_private_recursive(projection_wire, "legacy_projection")
    if projection_wire["schema"] != "h3.context.authoring_workbench.projection.v1":
        raise _reject("unsupported_profile", "legacy projection schema is unsupported")
    if projection_wire["rejection"] is not None:
        raise _reject("invalid_contract", "rejected legacy projection cannot be adapted")
    if not isinstance(projection_wire["reference"], Mapping) or not isinstance(
        projection_wire["availability"], Mapping
    ):
        raise _reject("invalid_contract", "legacy projection sections are invalid")
    timeline = _mapping(
        projection_wire["timeline"],
        (
            "revision",
            "content_fingerprint",
            "profile",
            "clips",
            "links",
            "selection",
            "blockers",
        ),
        "legacy_projection.timeline",
    )
    if not isinstance(timeline["profile"], Mapping) or not isinstance(timeline["selection"], list):
        raise _reject("invalid_contract", "legacy timeline metadata is invalid")
    clips_value = timeline["clips"]
    links = timeline["links"]
    blockers = timeline["blockers"]
    if (
        not isinstance(clips_value, list)
        or not isinstance(links, list)
        or not isinstance(blockers, list)
    ):
        raise _reject("invalid_contract", "legacy timeline arrays are invalid")
    if links:
        raise _reject("audio_editing_deferred", "legacy audiovisual links are not in this profile")
    if blockers:
        raise _reject("invalid_contract", "blocked legacy projection cannot be adapted")
    clips: list[dict[str, object]] = []
    used_assets: dict[str, object] = {}
    for index, item in enumerate(clips_value):
        if not isinstance(item, Mapping):
            raise _reject("invalid_contract", "legacy clip is invalid")
        if item.get("kind") != "video" or item.get("lane") != 0:
            raise _reject("audio_editing_deferred", "only lane-zero legacy video can be adapted")
        if item.get("envelope") != []:
            raise _reject("audio_editing_deferred", "legacy envelopes are not in this profile")
        asset_id = _identifier(item.get("asset_id"), f"legacy.clips[{index}].asset_id")
        timing = asset_timings.get(asset_id)
        if timing is None:
            raise _reject("timing_unavailable", "caller did not supply validated asset timing")
        used_assets.setdefault(asset_id, timing)
        clips.append(
            {
                "clip_id": item.get("clip_id"),
                "asset_id": asset_id,
                "track_id": "legacy.primary_video",
                "start_frame": item.get("start_frame"),
                "duration_frames": item.get("frames"),
                "source_start_frame": item.get("source_start_frame"),
                "enabled": True,
                "transform": _default_transform(),
                "crop": {"left_bp": 0, "top_bp": 0, "right_bp": 0, "bottom_bp": 0},
                "opacity_bp": 10_000,
                "blend": "normal",
                "text": None,
                "transition": {"kind": "none", "duration_frames": 0},
                "effect": {
                    "kind": "none",
                    "brightness_permille": 0,
                    "contrast_permille": 1_000,
                    "saturation_permille": 1_000,
                },
            }
        )
    output_value = _output(output)
    wire: dict[str, object] = {
        "schema": PUBLIC_SNAPSHOT_SCHEMA,
        "profile_id": ENGINE_PROFILE_ID,
        "operation_profile_id": OPERATION_PROFILE_ID,
        "project_id": project_id,
        "workspace_handle": projection_wire["workspace_handle"],
        "workspace_revision": workspace_revision,
        "workspace_fingerprint": workspace_fingerprint,
        "timeline_revision": timeline.get("revision"),
        "timeline_fingerprint": timeline.get("content_fingerprint"),
        "public_fingerprint": "sha256:" + "0" * 64,
        "output": output_value.to_wire(),
        "capability": _selected_capability().to_wire(),
        "assets": list(used_assets.values()),
        "tracks": [
            {
                "track_id": "legacy.primary_video",
                "kind": "primary_video",
                "order": 0,
                "enabled": True,
                "locked": False,
            }
        ],
        "clips": clips,
        "audio_extension": AudioExtension(
            AUDIO_EXTENSION_SCHEMA,
            "none_v1",
            INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
            (),
            "unsupported",
            "unsupported",
            "EmbeddedAudioSpanV1",
            "none_v1",
            "audio_editing_deferred",
        ).to_wire(),
        "blockers": [],
        "render_vocabulary": RenderVocabulary(
            RENDER_VOCABULARY_SCHEMA,
            "h3.context.authoring_render_request.v1",
            "h3.context.authoring_render_job.v1",
            "h3.context.authoring_render_receipt.v1",
            RENDER_JOB_STATES,
            RENDER_TERMINAL_STATES,
            RENDER_TERMINAL_REASONS,
        ).to_wire(),
    }
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


__all__ = [
    "AUDIO_EXTENSION_SCHEMA",
    "BLOCKER_CODES",
    "DERIVATIVE_MANIFEST_SCHEMA",
    "ENGINE_PROFILE_ID",
    "INDEPENDENT_AUDIO_COMMAND_NAMESPACE",
    "NLE_OPERATION_IDS",
    "OPERATION_PROFILE_ID",
    "OUTPUT_PROFILE_ID",
    "PRIVATE_SOURCE_MANIFEST_SCHEMA",
    "PUBLIC_SNAPSHOT_SCHEMA",
    "RENDER_JOB_STATES",
    "RENDER_TERMINAL_REASONS",
    "CompositionContractError",
    "DerivativeManifest",
    "PrivateSourceManifest",
    "PublicCompositionSnapshot",
    "ResolvedScene",
    "adapt_authoring_projection",
    "decode_public_snapshot",
    "decode_public_snapshot_json",
    "composition_contract_fingerprint",
    "operation_disposition",
    "public_snapshot_fingerprint",
    "resolve_composition",
    "resolve_source_interval_coverage",
    "resolve_source_landmark_after_elapsed",
    "source_intervals_are_contiguous",
]
