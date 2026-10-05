"""Exact content-free facts for one file-backed Authoring VIDEO source."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import cast

from ..core.av_reconstruction import (
    qualified_av_limits,
    qualified_av_target_profile,
    qualified_ffmpeg_capability,
)
from ..core.composition_contract import MAX_LANDMARKS_PER_ASSET
from .av_reconstruction_media import (
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
)
from .media_subprocess import CancellationProbe

AUTHORING_VIDEO_FACTS_SCHEMA = "h3.authoring.video_source_facts.v2"
_MAX_INTEROPERABLE_INTEGER = (1 << 53) - 1
_MAX_STREAMS = 2
_MAX_AUDIO_FRAMES = 2_048
_MAX_SUPPRESSED_SIDE_DATA_ENTRIES = 16
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AuthoringVideoFactsError(RuntimeError):
    """Content-free source-fact blocker with one stable closed code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AuthoringVideoRational:
    num: int
    den: int

    def __post_init__(self) -> None:
        if (
            type(self.num) is not int
            or type(self.den) is not int
            or not 1 <= self.num <= _MAX_INTEROPERABLE_INTEGER
            or not 1 <= self.den <= _MAX_INTEROPERABLE_INTEGER
            or Fraction(self.num, self.den).numerator != self.num
            or Fraction(self.num, self.den).denominator != self.den
        ):
            raise AuthoringVideoFactsError("video_facts_invalid")

    def to_wire(self) -> dict[str, int]:
        return {"num": self.num, "den": self.den}


@dataclass(frozen=True, slots=True)
class AuthoringVideoLandmark:
    frame_index: int
    pts: int
    dts: int
    duration_ticks: int

    def __post_init__(self) -> None:
        if (
            type(self.frame_index) is not int
            or type(self.pts) is not int
            or type(self.dts) is not int
            or type(self.duration_ticks) is not int
            or self.frame_index < 0
            or self.pts < 0
            or self.dts < 0
            or self.duration_ticks <= 0
            or max(self.frame_index, self.pts, self.dts, self.duration_ticks)
            > _MAX_INTEROPERABLE_INTEGER
        ):
            raise AuthoringVideoFactsError("video_facts_invalid")

    def to_wire(self) -> dict[str, int]:
        return {
            "frame_index": self.frame_index,
            "pts": self.pts,
            "dts": self.dts,
            "duration_ticks": self.duration_ticks,
        }


@dataclass(frozen=True, slots=True)
class EmbeddedAudioFacts:
    disposition: str
    sample_rate: int | None
    channels: int | None
    channel_layout: str | None
    time_base: AuthoringVideoRational | None
    sample_count: int | None

    def __post_init__(self) -> None:
        if self.disposition == "absent":
            if any(
                item is not None
                for item in (
                    self.sample_rate,
                    self.channels,
                    self.channel_layout,
                    self.time_base,
                    self.sample_count,
                )
            ):
                raise AuthoringVideoFactsError("video_facts_invalid")
            return
        if (
            self.disposition != "present_bound"
            or type(self.sample_rate) is not int
            or type(self.channels) is not int
            or type(self.channel_layout) is not str
            or not self.channel_layout
            or type(self.time_base) is not AuthoringVideoRational
            or type(self.sample_count) is not int
            or self.sample_rate <= 0
            or self.channels <= 0
            or self.sample_count <= 0
            or self.sample_rate not in (32_000, 48_000)
            or self.channels > qualified_av_limits().max_input_channels
            or self.sample_count > qualified_av_limits().max_audio_sample_frames
            or self.sample_count * 48_000 // self.sample_rate
            > qualified_av_limits().max_audio_sample_frames
        ):
            raise AuthoringVideoFactsError("video_facts_invalid")

    @property
    def canonical_sample_count(self) -> int | None:
        if self.disposition == "absent":
            return None
        # IMPORTANT: public trim indices are 48 kHz, but these facts remain native.
        # Floor effective coverage; AAC padding and a resampler's rounded final
        # sample must not extend the accepted source interval past its true end.
        return cast(int, self.sample_count) * 48_000 // cast(int, self.sample_rate)

    def to_wire(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "time_base": None if self.time_base is None else self.time_base.to_wire(),
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class AuthoringVideoFacts:
    schema_version: str
    content_fingerprint: str
    byte_length: int
    width: int
    height: int
    pixel_format: str
    pixel_aspect_ratio: AuthoringVideoRational | None
    color_range: str
    color_space: str
    color_primaries: str
    color_transfer: str
    source_time_base: AuthoringVideoRational
    frame_count: int
    landmarks: tuple[AuthoringVideoLandmark, ...]
    embedded_audio: EmbeddedAudioFacts

    def __post_init__(self) -> None:
        limits = qualified_av_limits()
        target = qualified_av_target_profile()
        landmark_shape_is_valid = (
            type(self.landmarks) is tuple
            and len(self.landmarks) == self.frame_count
            and len(self.landmarks) > 0
            and all(type(item) is AuthoringVideoLandmark for item in self.landmarks)
        )
        landmark_order_is_valid = landmark_shape_is_valid and all(
            item.frame_index == index
            and (index == 0 or item.pts > self.landmarks[index - 1].pts)
            and (index == 0 or item.dts >= self.landmarks[index - 1].dts)
            for index, item in enumerate(self.landmarks)
        )
        if (
            self.schema_version != AUTHORING_VIDEO_FACTS_SCHEMA
            or type(self.content_fingerprint) is not str
            or _SHA256.fullmatch(self.content_fingerprint) is None
            or type(self.byte_length) is not int
            or not 1 <= self.byte_length <= 64 * 1024 * 1024
            or type(self.width) is not int
            or type(self.height) is not int
            or self.width <= 0
            or self.height <= 0
            or self.width > limits.max_width
            or self.height > limits.max_height
            or type(self.pixel_format) is not str
            or self.pixel_format not in qualified_ffmpeg_capability().input_pixel_formats
            or (
                self.pixel_aspect_ratio is not None
                and type(self.pixel_aspect_ratio) is not AuthoringVideoRational
            )
            or type(self.source_time_base) is not AuthoringVideoRational
            or type(self.frame_count) is not int
            or not 1 <= self.frame_count <= MAX_LANDMARKS_PER_ASSET
            or not landmark_shape_is_valid
            or not landmark_order_is_valid
            or self.landmarks[0].pts != 0
            or self.landmarks[0].dts != 0
            or type(self.embedded_audio) is not EmbeddedAudioFacts
            or (
                self.color_range,
                self.color_space,
                self.color_primaries,
                self.color_transfer,
            )
            != (
                target.color_range,
                target.color_space,
                target.color_primaries,
                target.color_transfer,
            )
        ):
            raise AuthoringVideoFactsError("video_facts_invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "content_fingerprint": self.content_fingerprint,
            "byte_length": self.byte_length,
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "pixel_aspect_ratio": (
                None if self.pixel_aspect_ratio is None else self.pixel_aspect_ratio.to_wire()
            ),
            "color_range": self.color_range,
            "color_space": self.color_space,
            "color_primaries": self.color_primaries,
            "color_transfer": self.color_transfer,
            "source_time_base": self.source_time_base.to_wire(),
            "frame_count": self.frame_count,
            "landmarks": [item.to_wire() for item in self.landmarks],
            "embedded_audio": self.embedded_audio.to_wire(),
        }


def _decode(payload: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AuthoringVideoFactsError("video_facts_invalid")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except AuthoringVideoFactsError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise AuthoringVideoFactsError("video_facts_invalid") from None
    if type(value) is not dict:
        raise AuthoringVideoFactsError("video_facts_invalid")
    return cast(dict[str, object], value)


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise AuthoringVideoFactsError("video_facts_invalid")
    return cast(dict[str, object], value)


def _sequence(value: object, *, maximum: int) -> list[object]:
    if type(value) is not list:
        raise AuthoringVideoFactsError("video_facts_invalid")
    result = cast(list[object], value)
    if len(result) > maximum:
        raise AuthoringVideoFactsError("resource_limit")
    return result


def _validate_suppressed_side_data(frame: dict[str, object]) -> None:
    if "side_data_list" not in frame:
        return
    entries = _sequence(frame["side_data_list"], maximum=_MAX_SUPPRESSED_SIDE_DATA_ENTRIES)
    # IMPORTANT: the qualified ffprobe leaves `[{}]` when `frame_side_data=` suppresses values.
    # Admit only bounded empty markers; accepting members would import untrusted side-data facts.
    if not entries or any(type(entry) is not dict or entry for entry in entries):
        raise AuthoringVideoFactsError("video_facts_invalid")


def _closed(
    value: dict[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = frozenset(value)
    if not required.issubset(keys):
        raise AuthoringVideoFactsError("video_facts_unavailable")
    if not keys.issubset(required | optional):
        raise AuthoringVideoFactsError("video_facts_invalid")


def _integer(value: object, *, maximum: int = _MAX_INTEROPERABLE_INTEGER) -> int:
    if type(value) is int:
        result = value
    elif type(value) is str and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise AuthoringVideoFactsError("video_facts_unavailable")
    if not 0 <= result <= maximum:
        raise AuthoringVideoFactsError("video_facts_unsupported")
    return result


def _text(value: object, *, maximum: int = 128) -> str:
    if type(value) is not str or not value or len(value) > maximum or not value.isascii():
        raise AuthoringVideoFactsError("video_facts_unavailable")
    return value


def _rational(value: object, *, separator: str = "/") -> tuple[AuthoringVideoRational, Fraction]:
    text = _text(value, maximum=64)
    parts = text.split(separator)
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise AuthoringVideoFactsError("video_facts_unavailable")
    numerator, denominator = (int(part) for part in parts)
    if numerator <= 0 or denominator <= 0:
        raise AuthoringVideoFactsError("video_facts_unsupported")
    fraction = Fraction(numerator, denominator)
    if (
        fraction.numerator > _MAX_INTEROPERABLE_INTEGER
        or fraction.denominator > _MAX_INTEROPERABLE_INTEGER
    ):
        raise AuthoringVideoFactsError("video_facts_unsupported")
    return AuthoringVideoRational(fraction.numerator, fraction.denominator), fraction


def _parse_audio(
    stream: dict[str, object] | None,
    frames: list[dict[str, object]],
) -> EmbeddedAudioFacts:
    if stream is None:
        if frames:
            raise AuthoringVideoFactsError("video_facts_invalid")
        return EmbeddedAudioFacts("absent", None, None, None, None, None)
    required = frozenset(
        {
            "index",
            "codec_type",
            "codec_name",
            "sample_fmt",
            "sample_rate",
            "channels",
            "channel_layout",
            "time_base",
            "start_pts",
        }
    )
    _closed(stream, required=required)
    capability = qualified_ffmpeg_capability()
    limits = qualified_av_limits()
    if (
        _text(stream["codec_name"]) not in capability.audio_decoders
        or _text(stream["sample_fmt"]) not in capability.input_sample_formats
    ):
        raise AuthoringVideoFactsError("video_facts_unsupported")
    index = _integer(stream["index"], maximum=15)
    sample_rate = _integer(stream["sample_rate"], maximum=limits.max_input_sample_rate)
    channels = _integer(stream["channels"], maximum=limits.max_input_channels)
    if sample_rate not in (32_000, 48_000) or channels < 1:
        raise AuthoringVideoFactsError("video_facts_unsupported")
    channel_layout = _text(stream["channel_layout"])
    time_base, time_base_fraction = _rational(stream["time_base"])
    if _integer(stream["start_pts"]) != 0:
        raise AuthoringVideoFactsError("video_facts_unsupported")
    if not frames:
        raise AuthoringVideoFactsError("video_facts_unavailable")

    sample_count = 0
    decoded_padding: list[int] = []
    previous_dts = -1
    for frame in frames:
        _closed(
            frame,
            required=frozenset(
                {"media_type", "stream_index", "pts", "pkt_dts", "duration", "nb_samples"}
            ),
            optional=frozenset({"side_data_list"}),
        )
        _validate_suppressed_side_data(frame)
        if _integer(frame["stream_index"], maximum=15) != index:
            raise AuthoringVideoFactsError("video_facts_invalid")
        pts = _integer(frame["pts"])
        dts = _integer(frame["pkt_dts"])
        duration = _integer(frame["duration"])
        decoded_samples = _integer(frame["nb_samples"], maximum=1_000_000)
        offset_ticks = Fraction(sample_count, sample_rate) / time_base_fraction
        if (
            duration <= 0
            or decoded_samples <= 0
            or offset_ticks.denominator != 1
            or pts != offset_ticks.numerator
            or dts < previous_dts
        ):
            raise AuthoringVideoFactsError("video_facts_unsupported")
        timeline_samples = Fraction(duration) * time_base_fraction * sample_rate
        if timeline_samples.denominator != 1 or timeline_samples <= 0:
            raise AuthoringVideoFactsError("video_facts_unsupported")
        effective_samples = timeline_samples.numerator
        padding = decoded_samples - effective_samples
        if not 0 <= padding < 1024:
            raise AuthoringVideoFactsError("video_facts_unsupported")
        decoded_padding.append(padding)
        sample_count += effective_samples
        previous_dts = dts
    if any(decoded_padding[:-1]):
        raise AuthoringVideoFactsError("video_facts_unsupported")
    if (
        sample_count > limits.max_audio_sample_frames
        or sample_count * 48_000 // sample_rate > limits.max_audio_sample_frames
    ):
        raise AuthoringVideoFactsError("resource_limit")
    return EmbeddedAudioFacts(
        "present_bound",
        sample_rate,
        channels,
        channel_layout,
        time_base,
        sample_count,
    )


def _parse_probe(
    payload: bytes,
    *,
    content_fingerprint: str,
    byte_length: int,
) -> AuthoringVideoFacts:
    root = _decode(payload)
    _closed(
        root,
        required=frozenset({"streams", "frames"}),
        optional=frozenset({"programs", "stream_groups"}),
    )
    for name in ("programs", "stream_groups"):
        if name in root and _sequence(root[name], maximum=0):
            raise AuthoringVideoFactsError("video_facts_invalid")
    streams = [_mapping(item) for item in _sequence(root["streams"], maximum=_MAX_STREAMS)]
    if not streams:
        raise AuthoringVideoFactsError("video_facts_unavailable")
    by_type: dict[str, list[dict[str, object]]] = {"video": [], "audio": []}
    for stream in streams:
        media_type = _text(stream.get("codec_type"), maximum=16)
        if media_type not in by_type:
            raise AuthoringVideoFactsError("video_facts_unsupported")
        by_type[media_type].append(stream)
    if len(by_type["video"]) != 1 or len(by_type["audio"]) > 1:
        raise AuthoringVideoFactsError("video_facts_unsupported")

    video = by_type["video"][0]
    video_required = frozenset(
        {
            "index",
            "codec_type",
            "codec_name",
            "width",
            "height",
            "pix_fmt",
            "color_range",
            "color_space",
            "color_primaries",
            "color_transfer",
            "time_base",
            "start_pts",
        }
    )
    _closed(video, required=video_required, optional=frozenset({"sample_aspect_ratio"}))
    capability = qualified_ffmpeg_capability()
    limits = qualified_av_limits()
    if (
        _text(video["codec_name"]) not in capability.video_decoders
        or _text(video["pix_fmt"]) not in capability.input_pixel_formats
    ):
        raise AuthoringVideoFactsError("video_facts_unsupported")
    width = _integer(video["width"], maximum=limits.max_width)
    height = _integer(video["height"], maximum=limits.max_height)
    if width < 1 or height < 1:
        raise AuthoringVideoFactsError("video_facts_unsupported")
    # IMPORTANT: stock video encoders may omit SAR. Preserve unknown as null; filling 1:1
    # would fabricate a source fact and alias its fingerprint with explicitly square pixels.
    pixel_aspect = (
        _rational(video["sample_aspect_ratio"], separator=":")[0]
        if "sample_aspect_ratio" in video
        else None
    )
    time_base, time_base_fraction = _rational(video["time_base"])
    target = qualified_av_target_profile()
    colors = (
        _text(video["color_range"]),
        _text(video["color_space"]),
        _text(video["color_primaries"]),
        _text(video["color_transfer"]),
    )
    if (
        colors
        != (
            target.color_range,
            target.color_space,
            target.color_primaries,
            target.color_transfer,
        )
        or _integer(video["start_pts"]) != 0
    ):
        raise AuthoringVideoFactsError("video_facts_unsupported")
    video_index = _integer(video["index"], maximum=15)

    raw_frames = _sequence(root["frames"], maximum=MAX_LANDMARKS_PER_ASSET + _MAX_AUDIO_FRAMES)
    video_frames: list[dict[str, object]] = []
    audio_frames: list[dict[str, object]] = []
    for item in raw_frames:
        frame = _mapping(item)
        media_type = _text(frame.get("media_type"), maximum=16)
        if media_type == "video":
            video_frames.append(frame)
        elif media_type == "audio":
            audio_frames.append(frame)
        else:
            raise AuthoringVideoFactsError("video_facts_unsupported")
    if not video_frames:
        raise AuthoringVideoFactsError("video_facts_unavailable")
    if len(video_frames) > MAX_LANDMARKS_PER_ASSET:
        raise AuthoringVideoFactsError("resource_limit")

    landmarks: list[AuthoringVideoLandmark] = []
    previous_pts = previous_dts = -1
    for index, frame in enumerate(video_frames):
        _closed(
            frame,
            required=frozenset({"media_type", "stream_index", "pts", "pkt_dts", "duration"}),
            optional=frozenset({"side_data_list"}),
        )
        _validate_suppressed_side_data(frame)
        if _integer(frame["stream_index"], maximum=15) != video_index:
            raise AuthoringVideoFactsError("video_facts_invalid")
        pts = _integer(frame["pts"])
        dts = _integer(frame["pkt_dts"])
        duration = _integer(frame["duration"])
        if (
            duration <= 0
            or (index == 0 and (pts != 0 or dts != 0))
            or pts <= previous_pts
            or dts < previous_dts
            or Fraction(duration) * time_base_fraction < Fraction(1, limits.max_input_fps)
        ):
            raise AuthoringVideoFactsError("video_facts_unsupported")
        landmarks.append(AuthoringVideoLandmark(index, pts, dts, duration))
        previous_pts, previous_dts = pts, dts
    source_end = Fraction(landmarks[-1].pts + landmarks[-1].duration_ticks) * time_base_fraction
    if source_end <= 0 or source_end * 1000 > limits.max_duration_ms_per_segment:
        raise AuthoringVideoFactsError("resource_limit")

    audio_stream = by_type["audio"][0] if by_type["audio"] else None
    embedded_audio = _parse_audio(audio_stream, audio_frames)
    return AuthoringVideoFacts(
        AUTHORING_VIDEO_FACTS_SCHEMA,
        content_fingerprint,
        byte_length,
        width,
        height,
        _text(video["pix_fmt"]),
        pixel_aspect,
        *colors,
        time_base,
        len(landmarks),
        tuple(landmarks),
        embedded_audio,
    )


def probe_authoring_video_facts(
    source_path: Path,
    adapter: QualifiedAVMediaAdapter,
    deadline: float,
    cancellation: CancellationProbe | None = None,
) -> AuthoringVideoFacts:
    """Probe an already-authorized private path without retaining or publishing its locator."""

    if (
        not isinstance(source_path, Path)
        or not source_path.is_absolute()
        or not isinstance(adapter, QualifiedAVMediaAdapter)
        or type(deadline) is not float
    ):
        raise AuthoringVideoFactsError("video_facts_invalid")
    try:
        raw = adapter.probe_authoring_video_source(
            source_path=source_path,
            deadline=deadline,
            cancellation=cancellation,
        )
    except AVMediaAdapterError as exc:
        raise AuthoringVideoFactsError(exc.code) from None
    if time.monotonic() >= deadline:
        raise AuthoringVideoFactsError("preview_deadline")
    facts = _parse_probe(
        raw.probe_payload,
        content_fingerprint=raw.content_fingerprint,
        byte_length=raw.byte_length,
    )
    if time.monotonic() >= deadline:
        raise AuthoringVideoFactsError("preview_deadline")
    return facts


__all__ = [
    "AUTHORING_VIDEO_FACTS_SCHEMA",
    "AuthoringVideoFacts",
    "AuthoringVideoFactsError",
    "AuthoringVideoLandmark",
    "AuthoringVideoRational",
    "EmbeddedAudioFacts",
    "probe_authoring_video_facts",
]
