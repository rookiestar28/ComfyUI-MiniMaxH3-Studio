"""Pure M17-11 audiovisual reconstruction planning and sole receipt contract.

This module deliberately owns no filesystem locator, media process, host API, or
frontend wire.  It validates exact predecessor truth and produces a deterministic
process-local plan.  Only :class:`AVReconstructionReceipt` crosses persistence.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import InitVar, dataclass, replace
from dataclasses import field as dataclass_field
from enum import Enum
from fractions import Fraction
from hashlib import sha256
from hmac import compare_digest, digest
from typing import cast

from .canonical import canonical_bytes, canonical_fingerprint
from .continuity_handoff import ContinuityBoundaryReceipt, ContinuityMode
from .generation_sequence import GenerationJobState, GenerationSequenceState
from .segment_artifacts import ArtifactLifecycleState, SegmentArtifactReceipt
from .selective_rerun import (
    SelectiveRerunApproval,
    SelectiveRerunDisposition,
    SelectiveRerunPlan,
)

AV_RECONSTRUCTION_RECEIPT_SCHEMA = "h3.context.av_reconstruction_receipt.v1"
MAX_AV_RECONSTRUCTION_RECEIPT_BYTES = 131_072

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9A-Fa-f]{64}\Z")


class AVReconstructionError(ValueError):
    """Raised when reconstruction truth cannot remain exact and fail-closed."""


class AVOperation(str, Enum):
    REJECT = "reject"
    PASSTHROUGH = "passthrough"
    REMUX = "remux"
    NORMALIZE_VIDEO = "normalize_video"
    NORMALIZE_AUDIO = "normalize_audio"
    INSERT_SILENCE = "insert_silence"
    DROP_LEADING = "drop_leading"
    DROP_TRAILING = "drop_trailing"


class AVOutputKind(str, Enum):
    RECONSTRUCTION_FULL = "reconstruction_full"
    SEGMENT_EXPORT = "segment_export"


class AVPublicationState(str, Enum):
    COMPLETE = "complete"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise AVReconstructionError(f"bounded_identifier:{field}")
    return value


def _handle(value: object, field: str) -> str:
    if type(value) is not str or _HANDLE.fullmatch(value) is None:
        raise AVReconstructionError(f"opaque_handle:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise AVReconstructionError(f"sha256_fingerprint:{field}")
    return value


def _positive(value: object, field: str, maximum: int = 9_999_999_999_999) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise AVReconstructionError(f"positive_integer:{field}")
    return value


def _nonnegative(value: object, field: str, maximum: int = 9_999_999_999_999) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise AVReconstructionError(f"nonnegative_integer:{field}")
    return value


def _bounded_text(value: object, field: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or len(value) > maximum or "\x00" in value:
        raise AVReconstructionError(f"bounded_text:{field}")
    return value


def _fingerprints(values: object, field: str, *, maximum: int = 64) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > maximum:
        raise AVReconstructionError(field)
    result = tuple(_fingerprint(value, field) for value in values)
    if len(result) != len(set(result)):
        raise AVReconstructionError(f"duplicate_{field}")
    return result


@dataclass(frozen=True, slots=True)
class AVRational:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        _nonnegative(self.numerator, "rational.numerator")
        _positive(self.denominator, "rational.denominator")
        value = Fraction(self.numerator, self.denominator)
        if value.numerator != self.numerator or value.denominator != self.denominator:
            raise AVReconstructionError("rational_not_reduced")

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def to_wire(self) -> dict[str, int]:
        return {"numerator": self.numerator, "denominator": self.denominator}


@dataclass(frozen=True, slots=True)
class AVVideoDescriptor:
    codec: str
    width: int
    height: int
    pixel_format: str
    color_range: str
    color_space: str
    color_primaries: str
    color_transfer: str
    rotation_degrees: int
    frame_rate: AVRational
    time_base: AVRational
    start_time: AVRational
    end_time: AVRational
    decoded_frame_count: int

    def __post_init__(self) -> None:
        _identifier(self.codec, "video.codec")
        _positive(self.width, "video.width", 16_384)
        _positive(self.height, "video.height", 16_384)
        _identifier(self.pixel_format, "video.pixel_format")
        for text_value, text_field in (
            (self.color_range, "video.color_range"),
            (self.color_space, "video.color_space"),
            (self.color_primaries, "video.color_primaries"),
            (self.color_transfer, "video.color_transfer"),
        ):
            _identifier(text_value, text_field)
        if type(self.rotation_degrees) is not int or self.rotation_degrees not in {
            0,
            90,
            180,
            270,
        }:
            raise AVReconstructionError("video.rotation_degrees")
        for rational_value, rational_field in (
            (self.frame_rate, "video.frame_rate"),
            (self.time_base, "video.time_base"),
            (self.start_time, "video.start_time"),
            (self.end_time, "video.end_time"),
        ):
            if type(rational_value) is not AVRational:
                raise AVReconstructionError(rational_field)
        if self.frame_rate.numerator == 0 or self.time_base.numerator == 0:
            raise AVReconstructionError("video_zero_rate")
        if self.end_time.fraction <= self.start_time.fraction:
            raise AVReconstructionError("video_timestamp_order")
        _positive(self.decoded_frame_count, "video.decoded_frame_count", 10_000_000)
        expected_frames = (
            self.end_time.fraction - self.start_time.fraction
        ) * self.frame_rate.fraction
        if (
            expected_frames.denominator != 1
            or expected_frames.numerator != self.decoded_frame_count
        ):
            raise AVReconstructionError("video_frame_rate_count_mismatch")

    def to_wire(self) -> dict[str, object]:
        return {
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "color_range": self.color_range,
            "color_space": self.color_space,
            "color_primaries": self.color_primaries,
            "color_transfer": self.color_transfer,
            "rotation_degrees": self.rotation_degrees,
            "frame_rate": self.frame_rate.to_wire(),
            "time_base": self.time_base.to_wire(),
            "start_time": self.start_time.to_wire(),
            "end_time": self.end_time.to_wire(),
            "decoded_frame_count": self.decoded_frame_count,
        }


@dataclass(frozen=True, slots=True)
class AVAudioDescriptor:
    codec: str
    sample_format: str
    sample_rate: int
    channels: int
    channel_layout: str
    time_base: AVRational
    start_time: AVRational
    end_time: AVRational
    decoded_sample_count: int

    def __post_init__(self) -> None:
        _identifier(self.codec, "audio.codec")
        _identifier(self.sample_format, "audio.sample_format")
        _positive(self.sample_rate, "audio.sample_rate", 384_000)
        _positive(self.channels, "audio.channels", 64)
        _identifier(self.channel_layout, "audio.channel_layout")
        for value, field in (
            (self.time_base, "audio.time_base"),
            (self.start_time, "audio.start_time"),
            (self.end_time, "audio.end_time"),
        ):
            if type(value) is not AVRational:
                raise AVReconstructionError(field)
        if self.time_base.numerator == 0:
            raise AVReconstructionError("audio_zero_time_base")
        if self.end_time.fraction <= self.start_time.fraction:
            raise AVReconstructionError("audio_timestamp_order")
        _positive(self.decoded_sample_count, "audio.decoded_sample_count", 1_000_000_000)
        expected_samples = (self.end_time.fraction - self.start_time.fraction) * self.sample_rate
        if (
            expected_samples.denominator != 1
            or expected_samples.numerator != self.decoded_sample_count
        ):
            raise AVReconstructionError("audio_rate_count_mismatch")

    def to_wire(self) -> dict[str, object]:
        return {
            "codec": self.codec,
            "sample_format": self.sample_format,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "time_base": self.time_base.to_wire(),
            "start_time": self.start_time.to_wire(),
            "end_time": self.end_time.to_wire(),
            "decoded_sample_count": self.decoded_sample_count,
        }


@dataclass(frozen=True, slots=True)
class AVMediaDescriptor:
    """Strict process-local normalized media facts; never a portable contract."""

    segment_id: str
    artifact_receipt_fingerprint: str
    artifact_output_fingerprint: str
    artifact_byte_length: int
    capability_fingerprint: str
    container: str
    stream_count: int
    video: AVVideoDescriptor | None
    audio: AVAudioDescriptor | None
    subtitle_stream_count: int
    data_stream_count: int
    attachment_stream_count: int
    inspected_at_ms: int
    expires_at_ms: int
    warning_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "media.segment_id")
        _fingerprint(self.artifact_receipt_fingerprint, "media.artifact_receipt")
        _fingerprint(self.artifact_output_fingerprint, "media.artifact_output")
        _positive(self.artifact_byte_length, "media.artifact_byte_length", 4_294_967_296)
        _fingerprint(self.capability_fingerprint, "media.capability")
        _identifier(self.container, "media.container")
        _positive(self.stream_count, "media.stream_count", 16)
        if self.video is not None and type(self.video) is not AVVideoDescriptor:
            raise AVReconstructionError("media.video")
        if self.audio is not None and type(self.audio) is not AVAudioDescriptor:
            raise AVReconstructionError("media.audio")
        extras = 0
        for value, field in (
            (self.subtitle_stream_count, "media.subtitle_stream_count"),
            (self.data_stream_count, "media.data_stream_count"),
            (self.attachment_stream_count, "media.attachment_stream_count"),
        ):
            extras += _nonnegative(value, field, 16)
        if self.stream_count != int(self.video is not None) + int(self.audio is not None) + extras:
            raise AVReconstructionError("media_stream_count_mismatch")
        _positive(self.inspected_at_ms, "media.inspected_at_ms")
        _positive(self.expires_at_ms, "media.expires_at_ms")
        if self.expires_at_ms <= self.inspected_at_ms:
            raise AVReconstructionError("inspection_expiry")
        if type(self.warning_codes) is not tuple or len(self.warning_codes) > 32:
            raise AVReconstructionError("media.warning_codes")
        for code in self.warning_codes:
            _identifier(code, "media.warning_code")
        if len(set(self.warning_codes)) != len(self.warning_codes):
            raise AVReconstructionError("duplicate_media_warning")

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "artifact_receipt_fingerprint": self.artifact_receipt_fingerprint,
            "artifact_output_fingerprint": self.artifact_output_fingerprint,
            "artifact_byte_length": self.artifact_byte_length,
            "capability_fingerprint": self.capability_fingerprint,
            "container": self.container,
            "stream_count": self.stream_count,
            "video": None if self.video is None else self.video.to_wire(),
            "audio": None if self.audio is None else self.audio.to_wire(),
            "subtitle_stream_count": self.subtitle_stream_count,
            "data_stream_count": self.data_stream_count,
            "attachment_stream_count": self.attachment_stream_count,
            "inspected_at_ms": self.inspected_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "warning_codes": list(self.warning_codes),
        }


@dataclass(frozen=True, slots=True)
class AVTargetProfile:
    container: str
    video_codec: str
    width: int
    height: int
    pixel_format: str
    color_range: str
    color_space: str
    color_primaries: str
    color_transfer: str
    rotation_degrees: int
    frame_rate: AVRational
    video_time_base: AVRational
    audio_required: bool
    audio_codec: str
    sample_format: str
    sample_rate: int
    channels: int
    channel_layout: str
    audio_time_base: AVRational

    def __post_init__(self) -> None:
        for value, field in (
            (self.container, "target.container"),
            (self.video_codec, "target.video_codec"),
            (self.pixel_format, "target.pixel_format"),
            (self.color_range, "target.color_range"),
            (self.color_space, "target.color_space"),
            (self.color_primaries, "target.color_primaries"),
            (self.color_transfer, "target.color_transfer"),
            (self.audio_codec, "target.audio_codec"),
            (self.sample_format, "target.sample_format"),
            (self.channel_layout, "target.channel_layout"),
        ):
            _identifier(value, field)
        _positive(self.width, "target.width", 16_384)
        _positive(self.height, "target.height", 16_384)
        if self.rotation_degrees != 0:
            raise AVReconstructionError("target.rotation_degrees")
        _positive(self.sample_rate, "target.sample_rate", 384_000)
        _positive(self.channels, "target.channels", 64)
        if type(self.audio_required) is not bool:
            raise AVReconstructionError("target.audio_required")
        if type(self.frame_rate) is not AVRational or self.frame_rate.numerator == 0:
            raise AVReconstructionError("target.frame_rate")
        if type(self.video_time_base) is not AVRational or self.video_time_base.numerator == 0:
            raise AVReconstructionError("target.video_time_base")
        if type(self.audio_time_base) is not AVRational or self.audio_time_base.numerator == 0:
            raise AVReconstructionError("target.audio_time_base")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "container": self.container,
            "video_codec": self.video_codec,
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "color_range": self.color_range,
            "color_space": self.color_space,
            "color_primaries": self.color_primaries,
            "color_transfer": self.color_transfer,
            "rotation_degrees": self.rotation_degrees,
            "frame_rate": self.frame_rate.to_wire(),
            "video_time_base": self.video_time_base.to_wire(),
            "audio_required": self.audio_required,
            "audio_codec": self.audio_codec,
            "sample_format": self.sample_format,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "audio_time_base": self.audio_time_base.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class AVReconstructionLimits:
    max_segments: int
    max_boundaries: int
    max_input_bytes_per_segment: int
    max_aggregate_input_bytes: int
    max_aggregate_output_bytes: int
    max_temp_bytes: int
    store_quota_bytes: int
    max_duration_ms_per_segment: int
    max_total_duration_ms: int
    max_width: int
    max_height: int
    max_input_fps: int
    target_fps: int
    max_video_frames: int
    max_input_sample_rate: int
    target_sample_rate: int
    max_input_channels: int
    target_channels: int
    max_audio_sample_frames: int
    max_probe_stdout_bytes: int
    max_probe_stderr_bytes: int
    max_process_stderr_bytes: int
    process_timeout_ms: int
    process_concurrency: int
    cancellation_poll_ms: int
    terminate_reap_timeout_ms: int
    cleanup_timeout_ms: int
    max_recovery_entries: int
    inspection_ttl_ms: int
    approval_ttl_ms: int

    def __post_init__(self) -> None:
        for field, value in self.to_wire().items():
            _positive(value, f"limits.{field}", 1 << 54)
        if self.max_segments > 64 or self.max_boundaries > 63:
            raise AVReconstructionError("limits.segment_ceiling")
        if self.max_boundaries != self.max_segments - 1:
            raise AVReconstructionError("limits.boundary_segment_relation")
        if self.target_fps > self.max_input_fps:
            raise AVReconstructionError("limits.target_fps")
        if self.target_sample_rate > self.max_input_sample_rate:
            raise AVReconstructionError("limits.target_sample_rate")
        if self.target_channels > self.max_input_channels:
            raise AVReconstructionError("limits.target_channels")
        if self.max_aggregate_output_bytes > self.store_quota_bytes:
            raise AVReconstructionError("limits.output_quota")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, int]:
        return {field: cast(int, getattr(self, field)) for field in self.__dataclass_fields__}


def qualified_av_limits() -> AVReconstructionLimits:
    """Return the finite Gate-0 M17-11 resource profile."""

    return AVReconstructionLimits(
        max_segments=64,
        max_boundaries=63,
        max_input_bytes_per_segment=64 * 1024 * 1024,
        max_aggregate_input_bytes=4 * 1024 * 1024 * 1024,
        max_aggregate_output_bytes=4 * 1024 * 1024 * 1024,
        max_temp_bytes=8 * 1024 * 1024 * 1024,
        store_quota_bytes=16 * 1024 * 1024 * 1024,
        max_duration_ms_per_segment=30_000,
        max_total_duration_ms=1_920_000,
        max_width=2048,
        max_height=2048,
        max_input_fps=60,
        target_fps=30,
        max_video_frames=32_768,
        max_input_sample_rate=96_000,
        target_sample_rate=48_000,
        max_input_channels=8,
        target_channels=2,
        max_audio_sample_frames=184_320_000,
        max_probe_stdout_bytes=2 * 1024 * 1024,
        max_probe_stderr_bytes=256 * 1024,
        max_process_stderr_bytes=1024 * 1024,
        process_timeout_ms=600_000,
        process_concurrency=1,
        cancellation_poll_ms=50,
        terminate_reap_timeout_ms=5_000,
        cleanup_timeout_ms=10_000,
        max_recovery_entries=256,
        inspection_ttl_ms=60_000,
        approval_ttl_ms=600_000,
    )


def qualified_av_target_profile() -> AVTargetProfile:
    return AVTargetProfile(
        container="mp4",
        video_codec="h264",
        width=512,
        height=512,
        pixel_format="yuv420p",
        color_range="tv",
        color_space="bt709",
        color_primaries="bt709",
        color_transfer="bt709",
        rotation_degrees=0,
        frame_rate=AVRational(30, 1),
        video_time_base=AVRational(1, 90_000),
        audio_required=True,
        audio_codec="aac",
        sample_format="fltp",
        sample_rate=48_000,
        channels=2,
        channel_layout="stereo",
        audio_time_base=AVRational(1, 48_000),
    )


@dataclass(frozen=True, slots=True)
class AVMediaCapability:
    distribution: str
    build_version: str
    license_expression: str
    ffmpeg_sha256: str
    ffprobe_sha256: str
    protocols: tuple[str, ...]
    input_containers: tuple[str, ...]
    input_pixel_formats: tuple[str, ...]
    input_sample_formats: tuple[str, ...]
    demuxers: tuple[str, ...]
    muxers: tuple[str, ...]
    video_decoders: tuple[str, ...]
    audio_decoders: tuple[str, ...]
    video_encoders: tuple[str, ...]
    audio_encoders: tuple[str, ...]
    filters: tuple[str, ...]
    operations: tuple[AVOperation, ...]
    target_profile_fingerprint: str

    def __post_init__(self) -> None:
        _bounded_text(self.distribution, "capability.distribution")
        _bounded_text(self.build_version, "capability.build_version")
        _bounded_text(self.license_expression, "capability.license_expression", 64)
        for value, field in (
            (self.ffmpeg_sha256, "capability.ffmpeg_sha256"),
            (self.ffprobe_sha256, "capability.ffprobe_sha256"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise AVReconstructionError(field)
        for field in (
            "protocols",
            "input_containers",
            "input_pixel_formats",
            "input_sample_formats",
            "demuxers",
            "muxers",
            "video_decoders",
            "audio_decoders",
            "video_encoders",
            "audio_encoders",
            "filters",
        ):
            values = getattr(self, field)
            if type(values) is not tuple or not values or len(values) > 64:
                raise AVReconstructionError(f"capability.{field}")
            for value in values:
                _identifier(value, f"capability.{field}")
            if tuple(sorted(set(values))) != values:
                raise AVReconstructionError(f"capability.{field}_canonical")
        if self.protocols != ("file",):
            raise AVReconstructionError("capability.protocol_scope")
        if type(self.operations) is not tuple or not self.operations:
            raise AVReconstructionError("capability.operations")
        if not all(type(item) is AVOperation for item in self.operations):
            raise AVReconstructionError("capability.operation_type")
        if len(set(self.operations)) != len(self.operations):
            raise AVReconstructionError("capability.operation_duplicate")
        _fingerprint(self.target_profile_fingerprint, "capability.target_profile")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "distribution": self.distribution,
            "build_version": self.build_version,
            "license_expression": self.license_expression,
            "ffmpeg_sha256": self.ffmpeg_sha256.lower(),
            "ffprobe_sha256": self.ffprobe_sha256.lower(),
            "protocols": list(self.protocols),
            "input_containers": list(self.input_containers),
            "input_pixel_formats": list(self.input_pixel_formats),
            "input_sample_formats": list(self.input_sample_formats),
            "demuxers": list(self.demuxers),
            "muxers": list(self.muxers),
            "video_decoders": list(self.video_decoders),
            "audio_decoders": list(self.audio_decoders),
            "video_encoders": list(self.video_encoders),
            "audio_encoders": list(self.audio_encoders),
            "filters": list(self.filters),
            "operations": [item.value for item in self.operations],
            "target_profile_fingerprint": self.target_profile_fingerprint,
        }


def qualified_ffmpeg_capability() -> AVMediaCapability:
    """Return content-free identity for the exact private Gate-0 tool pair.

    Executable locators are intentionally absent.  The adapter must receive and
    re-hash caller-authorized exact paths before use.
    """

    return AVMediaCapability(
        distribution="gyan.dev-full_build",
        build_version="2026-02-26-git-6695528af6",
        license_expression="GPL-3.0-or-later",
        ffmpeg_sha256=(
            "ABF5E1652DFDD3F8D7CFBB4900A4B590"  # pragma: allowlist secret
            "B7E8D192876164B7B7E9EFAEBE26D67F"  # pragma: allowlist secret
        ),
        ffprobe_sha256=(
            "FB81E32EA05D77049291D9CFFB0EC267"  # pragma: allowlist secret
            "7CFBE5E1CE2021BBCB9EB5ABDC6AC576"  # pragma: allowlist secret
        ),
        protocols=("file",),
        input_containers=("mp4",),
        input_pixel_formats=("yuv420p",),
        input_sample_formats=("fltp",),
        demuxers=("mov", "wav"),
        muxers=("mp4",),
        video_decoders=("h264",),
        audio_decoders=("aac", "pcm_s16le"),
        video_encoders=("libx264",),
        audio_encoders=("aac",),
        filters=(
            # M20-09: seam-only filters are declared for the explicit seam policy;
            # presence in the exact pinned pair is measured by a real-binary probe.
            "acrossfade",
            "aformat",
            "apad",
            "aresample",
            "asetpts",
            "asplit",
            "atrim",
            "concat",
            "format",
            "fps",
            "scale",
            "setpts",
            "settb",
            "split",
            "trim",
            "xfade",
        ),
        operations=(
            AVOperation.PASSTHROUGH,
            AVOperation.NORMALIZE_VIDEO,
            AVOperation.NORMALIZE_AUDIO,
            AVOperation.INSERT_SILENCE,
        ),
        target_profile_fingerprint=qualified_av_target_profile().fingerprint,
    )


@dataclass(frozen=True, slots=True)
class AVBoundaryEvidence:
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    predecessor_segment_id: str
    successor_segment_id: str
    receipt: ContinuityBoundaryReceipt

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, "boundary.workspace_id")
        _positive(self.workspace_revision, "boundary.workspace_revision", 1_000_000)
        _fingerprint(self.workspace_fingerprint, "boundary.workspace")
        _identifier(self.predecessor_segment_id, "boundary.predecessor_segment_id")
        _identifier(self.successor_segment_id, "boundary.successor_segment_id")
        if self.predecessor_segment_id == self.successor_segment_id:
            raise AVReconstructionError("boundary_self_reference")
        if type(self.receipt) is not ContinuityBoundaryReceipt:
            raise AVReconstructionError("boundary_receipt_type")
        if self.receipt.mode is ContinuityMode.NATIVE_FRAME_HANDOFF and (
            self.receipt.predecessor_segment_id != self.predecessor_segment_id
            or self.receipt.successor_segment_id != self.successor_segment_id
        ):
            raise AVReconstructionError("boundary_identity_mismatch")

    def to_wire(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "predecessor_segment_id": self.predecessor_segment_id,
            "successor_segment_id": self.successor_segment_id,
            "receipt_fingerprint": self.receipt.fingerprint,
            "continuity_mode": self.receipt.mode.value,
        }


@dataclass(frozen=True, slots=True)
class AVStreamAccounting:
    decoded_frames: int
    carried_frames: int
    dropped_frames: int
    generated_frames: int
    emitted_frames: int
    decoded_samples: int
    carried_samples: int
    dropped_samples: int
    inserted_samples: int
    emitted_samples: int

    def __post_init__(self) -> None:
        for field, value in self.to_wire().items():
            _nonnegative(value, f"accounting.{field}", 1_000_000_000)
        if self.decoded_frames != self.carried_frames + self.dropped_frames:
            raise AVReconstructionError("video_decode_accounting")
        if self.emitted_frames != self.carried_frames + self.generated_frames:
            raise AVReconstructionError("video_emit_accounting")
        if self.decoded_samples != self.carried_samples + self.dropped_samples:
            raise AVReconstructionError("audio_decode_accounting")
        if self.emitted_samples != self.carried_samples + self.inserted_samples:
            raise AVReconstructionError("audio_emit_accounting")

    def to_wire(self) -> dict[str, int]:
        return {field: cast(int, getattr(self, field)) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class AVSegmentPlan:
    segment_id: str
    disposition: SelectiveRerunDisposition
    artifact_receipt_fingerprint: str
    artifact_output_fingerprint: str
    descriptor: AVMediaDescriptor
    operations: tuple[AVOperation, ...]
    accounting: AVStreamAccounting
    output_start: AVRational
    output_end: AVRational

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "segment.segment_id")
        if type(self.disposition) is not SelectiveRerunDisposition:
            raise AVReconstructionError("segment.disposition")
        _fingerprint(self.artifact_receipt_fingerprint, "segment.artifact_receipt")
        _fingerprint(self.artifact_output_fingerprint, "segment.artifact_output")
        if type(self.descriptor) is not AVMediaDescriptor:
            raise AVReconstructionError("segment.descriptor")
        if type(self.operations) is not tuple or not self.operations:
            raise AVReconstructionError("segment.operations")
        if not all(type(item) is AVOperation for item in self.operations):
            raise AVReconstructionError("segment.operation_type")
        if len(set(self.operations)) != len(self.operations):
            raise AVReconstructionError("segment.operation_duplicate")
        if AVOperation.REJECT in self.operations:
            raise AVReconstructionError("operation_unsupported")
        if type(self.accounting) is not AVStreamAccounting:
            raise AVReconstructionError("segment.accounting")
        if type(self.output_start) is not AVRational or type(self.output_end) is not AVRational:
            raise AVReconstructionError("segment.output_span")
        if self.output_end.fraction <= self.output_start.fraction:
            raise AVReconstructionError("segment.output_span_order")

    @property
    def decision_fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "segment_id": self.segment_id,
                "operations": [item.value for item in self.operations],
                "accounting": self.accounting.to_wire(),
                "output_start": self.output_start.to_wire(),
                "output_end": self.output_end.to_wire(),
            }
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "disposition": self.disposition.value,
            "artifact_receipt_fingerprint": self.artifact_receipt_fingerprint,
            "artifact_output_fingerprint": self.artifact_output_fingerprint,
            "descriptor": self.descriptor.to_wire(),
            "operations": [item.value for item in self.operations],
            "accounting": self.accounting.to_wire(),
            "output_start": self.output_start.to_wire(),
            "output_end": self.output_end.to_wire(),
            "decision_fingerprint": self.decision_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class AVReconstructionPlan:
    """Factory-owned process-local authority produced by the exact predecessor join."""

    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    selective_plan_fingerprint: str
    selective_approval_fingerprint: str
    generation_plan_fingerprint: str
    generation_state_fingerprint: str
    manifest_fingerprints: tuple[str, ...]
    artifact_receipt_fingerprints: tuple[str, ...]
    boundary_receipt_fingerprints: tuple[str, ...]
    capability: AVMediaCapability
    limits: AVReconstructionLimits
    target_profile: AVTargetProfile
    segments: tuple[AVSegmentPlan, ...]
    boundaries: tuple[AVBoundaryEvidence, ...]
    planned_at_ms: int
    approval_deadline_ms: int
    plan_fingerprint: str | None = None
    _factory_token: InitVar[object | None] = None
    _factory_attestation: bytes = dataclass_field(init=False, repr=False, compare=False)

    def __post_init__(self, _factory_token: object | None) -> None:
        # SECURITY: asserted predecessor fingerprints cannot be re-authorized by direct
        # construction or dataclasses.replace(); only the exact owning join may mint this value.
        if _mint_plan_attestation(_factory_token, "factory-admission") is None:
            raise AVReconstructionError("plan_factory_required")
        _identifier(self.workspace_id, "plan.workspace_id")
        _positive(self.workspace_revision, "plan.workspace_revision", 1_000_000)
        for value, field in (
            (self.workspace_fingerprint, "plan.workspace"),
            (self.selective_plan_fingerprint, "plan.selective"),
            (self.selective_approval_fingerprint, "plan.selective_approval"),
            (self.generation_plan_fingerprint, "plan.generation_plan"),
            (self.generation_state_fingerprint, "plan.generation_state"),
        ):
            _fingerprint(value, field)
        _fingerprints(self.manifest_fingerprints, "plan.manifests")
        _fingerprints(self.artifact_receipt_fingerprints, "plan.artifact_receipts")
        _fingerprints(self.boundary_receipt_fingerprints, "plan.boundary_receipts", maximum=63)
        if type(self.capability) is not AVMediaCapability:
            raise AVReconstructionError("plan.capability")
        if type(self.limits) is not AVReconstructionLimits:
            raise AVReconstructionError("plan.limits")
        if type(self.target_profile) is not AVTargetProfile:
            raise AVReconstructionError("plan.target_profile")
        # SECURITY: this exported typed authority must preserve Gate-0 admission even when callers
        # construct or dataclasses.replace() a plan without using the owning builder.
        if self.capability != qualified_ffmpeg_capability():
            raise AVReconstructionError("adapter_capability_changed")
        if self.limits != qualified_av_limits():
            raise AVReconstructionError("resource_limit_profile_changed")
        if (
            self.target_profile != qualified_av_target_profile()
            or self.capability.target_profile_fingerprint != self.target_profile.fingerprint
        ):
            raise AVReconstructionError("adapter_capability_changed")
        if type(self.segments) is not tuple or not self.segments:
            raise AVReconstructionError("zero_segment_reconstruction")
        if not all(type(item) is AVSegmentPlan for item in self.segments):
            raise AVReconstructionError("plan.segment_type")
        if len(self.segments) > self.limits.max_segments:
            raise AVReconstructionError("resource_limit:segments")
        if len(self.manifest_fingerprints) != len(self.segments):
            raise AVReconstructionError("plan_manifest_order")
        if type(self.boundaries) is not tuple or not all(
            type(item) is AVBoundaryEvidence for item in self.boundaries
        ):
            raise AVReconstructionError("plan.boundary_type")
        if len(self.boundaries) != len(self.segments) - 1:
            raise AVReconstructionError("boundary_missing")
        if self.artifact_receipt_fingerprints != tuple(
            item.artifact_receipt_fingerprint for item in self.segments
        ):
            raise AVReconstructionError("plan_artifact_order")
        if self.boundary_receipt_fingerprints != tuple(
            item.receipt.fingerprint for item in self.boundaries
        ):
            raise AVReconstructionError("plan_boundary_order")
        for index, boundary in enumerate(self.boundaries):
            if (
                boundary.workspace_id != self.workspace_id
                or boundary.workspace_revision != self.workspace_revision
                or boundary.workspace_fingerprint != self.workspace_fingerprint
                or boundary.predecessor_segment_id != self.segments[index].segment_id
                or boundary.successor_segment_id != self.segments[index + 1].segment_id
            ):
                raise AVReconstructionError("boundary_identity_mismatch")
            if (
                boundary.receipt.mode is ContinuityMode.NATIVE_FRAME_HANDOFF
                and self.segments[index + 1].disposition is not SelectiveRerunDisposition.QUEUE
            ):
                raise AVReconstructionError("boundary_identity_mismatch")
        total_input_bytes = 0
        total_duration = Fraction(0, 1)
        total_frames = 0
        total_samples = 0
        _positive(self.planned_at_ms, "plan.planned_at_ms")
        _positive(self.approval_deadline_ms, "plan.approval_deadline_ms")
        descriptor_expiry = self.planned_at_ms + self.limits.approval_ttl_ms
        for index, segment in enumerate(self.segments):
            descriptor = segment.descriptor
            if not (
                descriptor.segment_id == segment.segment_id
                and descriptor.artifact_receipt_fingerprint == segment.artifact_receipt_fingerprint
                and descriptor.artifact_output_fingerprint == segment.artifact_output_fingerprint
                and descriptor.capability_fingerprint == self.capability.fingerprint
            ):
                raise AVReconstructionError("inspection_identity_mismatch")
            if descriptor.warning_codes:
                raise AVReconstructionError("inspection_invalid")
            if any(operation not in self.capability.operations for operation in segment.operations):
                raise AVReconstructionError("operation_unsupported")
            if AVOperation.PASSTHROUGH in segment.operations and len(segment.operations) != 1:
                raise AVReconstructionError("operation_unsupported")
            expected_operations, expected_accounting = _assess_segment(
                descriptor,
                self.target_profile,
                self.capability,
            )
            if (
                segment.operations != expected_operations
                or segment.accounting != expected_accounting
            ):
                raise AVReconstructionError("plan_segment_decision_mismatch")
            video = descriptor.video
            if video is None:  # pragma: no cover - rejected by _assess_segment above
                raise AVReconstructionError("input_video_missing")
            duration = video.end_time.fraction - video.start_time.fraction
            if segment.output_end.fraction - segment.output_start.fraction != duration:
                raise AVReconstructionError("segment_output_span_mismatch")
            if index == 0 and segment.output_start != AVRational(0, 1):
                raise AVReconstructionError("timeline_not_contiguous")
            if index and self.segments[index - 1].output_end != segment.output_start:
                raise AVReconstructionError("timeline_not_contiguous")
            if descriptor.artifact_byte_length > self.limits.max_input_bytes_per_segment:
                raise AVReconstructionError("resource_limit:segment_bytes")
            if video.width > self.limits.max_width or video.height > self.limits.max_height:
                raise AVReconstructionError("resource_limit:resolution")
            if video.frame_rate.fraction > self.limits.max_input_fps:
                raise AVReconstructionError("resource_limit:fps")
            if duration * 1000 > self.limits.max_duration_ms_per_segment:
                raise AVReconstructionError("resource_limit:segment_duration")
            if descriptor.audio is not None and (
                descriptor.audio.sample_rate > self.limits.max_input_sample_rate
                or descriptor.audio.channels > self.limits.max_input_channels
            ):
                raise AVReconstructionError("resource_limit:audio")
            if not descriptor.inspected_at_ms <= self.planned_at_ms < descriptor.expires_at_ms:
                raise AVReconstructionError("inspection_stale")
            if (
                descriptor.expires_at_ms - descriptor.inspected_at_ms
                > self.limits.inspection_ttl_ms
            ):
                raise AVReconstructionError("inspection_ttl_mismatch")
            total_input_bytes += descriptor.artifact_byte_length
            total_duration += duration
            total_frames += segment.accounting.emitted_frames
            total_samples += segment.accounting.emitted_samples
            descriptor_expiry = min(descriptor_expiry, descriptor.expires_at_ms)
        if total_input_bytes > self.limits.max_aggregate_input_bytes:
            raise AVReconstructionError("resource_limit:aggregate_input")
        if total_duration * 1000 > self.limits.max_total_duration_ms:
            raise AVReconstructionError("resource_limit:total_duration")
        if total_frames > self.limits.max_video_frames:
            raise AVReconstructionError("resource_limit:video_frames")
        if total_samples > self.limits.max_audio_sample_frames:
            raise AVReconstructionError("resource_limit:audio_samples")
        if (
            self.approval_deadline_ms <= self.planned_at_ms
            or self.approval_deadline_ms != descriptor_expiry
        ):
            raise AVReconstructionError("plan_approval_deadline")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.plan_fingerprint is None:
            object.__setattr__(self, "plan_fingerprint", expected)
        elif self.plan_fingerprint != expected:
            raise AVReconstructionError("plan_fingerprint_mismatch")
        attestation = _mint_plan_attestation(_factory_token, self.fingerprint)
        if attestation is None:  # pragma: no cover - admitted at the start of construction
            raise AVReconstructionError("plan_factory_required")
        object.__setattr__(self, "_factory_attestation", attestation)

    @property
    def fingerprint(self) -> str:
        if self.plan_fingerprint is None:  # pragma: no cover
            raise AVReconstructionError("plan_fingerprint_uninitialized")
        return self.plan_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "selective_plan_fingerprint": self.selective_plan_fingerprint,
            "selective_approval_fingerprint": self.selective_approval_fingerprint,
            "generation_plan_fingerprint": self.generation_plan_fingerprint,
            "generation_state_fingerprint": self.generation_state_fingerprint,
            "manifest_fingerprints": list(self.manifest_fingerprints),
            "artifact_receipt_fingerprints": list(self.artifact_receipt_fingerprints),
            "boundary_receipt_fingerprints": list(self.boundary_receipt_fingerprints),
            "capability": self.capability.to_wire(),
            "limits": self.limits.to_wire(),
            "target_profile": self.target_profile.to_wire(),
            "segments": [item.to_wire() for item in self.segments],
            "boundaries": [item.to_wire() for item in self.boundaries],
            "planned_at_ms": self.planned_at_ms,
            "approval_deadline_ms": self.approval_deadline_ms,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["plan_fingerprint"] = self.fingerprint
        return value


def _target_frame_count(video: AVVideoDescriptor, target: AVTargetProfile) -> int:
    frames = (video.end_time.fraction - video.start_time.fraction) * target.frame_rate.fraction
    if frames.denominator != 1 or frames.numerator <= 0:
        raise AVReconstructionError("non_integral_target_frame_count")
    return frames.numerator


def _target_sample_count(descriptor: AVMediaDescriptor, target: AVTargetProfile) -> int:
    if descriptor.video is None:
        raise AVReconstructionError("input_video_missing")
    duration = descriptor.video.end_time.fraction - descriptor.video.start_time.fraction
    samples = duration * target.sample_rate
    if samples.denominator != 1 or samples.numerator <= 0:
        raise AVReconstructionError("non_integral_target_sample_count")
    return samples.numerator


def _assess_segment(
    descriptor: AVMediaDescriptor,
    target: AVTargetProfile,
    capability: AVMediaCapability,
) -> tuple[tuple[AVOperation, ...], AVStreamAccounting]:
    video = descriptor.video
    if video is None:
        raise AVReconstructionError("input_video_missing")
    if descriptor.container not in capability.input_containers:
        raise AVReconstructionError("operation_unsupported:container")
    if video.codec not in capability.video_decoders:
        raise AVReconstructionError("operation_unsupported:video_codec")
    if video.pixel_format not in capability.input_pixel_formats:
        raise AVReconstructionError("operation_unsupported:pixel_format")
    if (
        video.color_range != target.color_range
        or video.color_space != target.color_space
        or video.color_primaries != target.color_primaries
        or video.color_transfer != target.color_transfer
        or video.rotation_degrees != target.rotation_degrees
    ):
        raise AVReconstructionError("operation_unsupported:color")
    if (
        descriptor.subtitle_stream_count
        or descriptor.data_stream_count
        or descriptor.attachment_stream_count
    ):
        raise AVReconstructionError("operation_unsupported:extra_stream")
    operations: list[AVOperation] = []
    if descriptor.container != target.container:
        operations.append(AVOperation.REMUX)
    video_exact = (
        video.codec == target.video_codec
        and video.width == target.width
        and video.height == target.height
        and video.pixel_format == target.pixel_format
        and video.color_range == target.color_range
        and video.color_space == target.color_space
        and video.color_primaries == target.color_primaries
        and video.color_transfer == target.color_transfer
        and video.rotation_degrees == target.rotation_degrees
        and video.frame_rate == target.frame_rate
        and video.time_base == target.video_time_base
        and video.start_time == AVRational(0, 1)
    )
    if not video_exact:
        operations.append(AVOperation.NORMALIZE_VIDEO)
    audio = descriptor.audio
    target_samples = _target_sample_count(descriptor, target)
    duration = video.end_time.fraction - video.start_time.fraction
    target_audio_end = AVRational(duration.numerator, duration.denominator)
    if audio is None:
        if target.audio_required:
            operations.append(AVOperation.INSERT_SILENCE)
    elif audio.codec not in capability.audio_decoders:
        raise AVReconstructionError("operation_unsupported:audio_codec")
    elif audio.sample_format not in capability.input_sample_formats:
        raise AVReconstructionError("operation_unsupported:sample_format")
    elif not (
        audio.codec == target.audio_codec
        and audio.sample_format == target.sample_format
        and audio.sample_rate == target.sample_rate
        and audio.channels == target.channels
        and audio.channel_layout == target.channel_layout
        and audio.time_base == target.audio_time_base
        and audio.start_time == AVRational(0, 1)
        and audio.end_time == target_audio_end
        and audio.decoded_sample_count == target_samples
    ):
        operations.append(AVOperation.NORMALIZE_AUDIO)
    if not operations:
        operations.append(AVOperation.PASSTHROUGH)
    unsupported = tuple(item for item in operations if item not in capability.operations)
    if unsupported:
        raise AVReconstructionError(f"operation_unsupported:{unsupported[0].value}")

    emitted_frames = _target_frame_count(video, target)
    carried_frames = min(video.decoded_frame_count, emitted_frames)
    dropped_frames = video.decoded_frame_count - carried_frames
    generated_frames = emitted_frames - carried_frames
    decoded_samples = 0 if audio is None else audio.decoded_sample_count
    carried_samples = min(decoded_samples, target_samples)
    dropped_samples = decoded_samples - carried_samples
    inserted_samples = target_samples - carried_samples
    return tuple(operations), AVStreamAccounting(
        decoded_frames=video.decoded_frame_count,
        carried_frames=carried_frames,
        dropped_frames=dropped_frames,
        generated_frames=generated_frames,
        emitted_frames=emitted_frames,
        decoded_samples=decoded_samples,
        carried_samples=carried_samples,
        dropped_samples=dropped_samples,
        inserted_samples=inserted_samples,
        emitted_samples=target_samples,
    )


def _build_av_reconstruction_plan(
    *,
    selective_plan: SelectiveRerunPlan,
    selective_approval: SelectiveRerunApproval,
    generation_state: GenerationSequenceState,
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
    media_descriptors: tuple[AVMediaDescriptor, ...],
    boundary_evidence: tuple[AVBoundaryEvidence, ...],
    accepted_boundary_receipt_fingerprints: tuple[str, ...],
    capability: AVMediaCapability,
    limits: AVReconstructionLimits,
    target_profile: AVTargetProfile,
    planned_at_ms: int,
    _factory_token: object,
) -> AVReconstructionPlan:
    """Join exact M17-09/08/07/10 truth into one deterministic local plan."""

    if type(selective_plan) is not SelectiveRerunPlan:
        raise AVReconstructionError("selective_plan_type")
    if type(selective_approval) is not SelectiveRerunApproval:
        raise AVReconstructionError("selective_approval_type")
    # SECURITY: predecessor fingerprints are claims until their canonical wire is recomputed.
    selective_plan_wire = selective_plan.to_wire()
    selective_plan_wire.pop("plan_fingerprint")
    if selective_plan.fingerprint != canonical_fingerprint(selective_plan_wire):
        raise AVReconstructionError("tampered_selective_plan")
    selective_approval_wire = selective_approval.to_wire()
    selective_approval_wire.pop("approval_fingerprint")
    if selective_approval.fingerprint != canonical_fingerprint(selective_approval_wire):
        raise AVReconstructionError("tampered_selective_approval")
    if selective_approval.plan_fingerprint != selective_plan.fingerprint:
        raise AVReconstructionError("selective_approval_plan_mismatch")
    if tuple(selective_approval.approved_segment_ids) != selective_plan.queued_segment_ids:
        raise AVReconstructionError("selective_approval_segment_mismatch")
    if selective_plan.blocked_segment_ids or selective_plan.requires_full_recompute:
        raise AVReconstructionError("predecessor_not_accepted")
    if type(generation_state) is not GenerationSequenceState:
        raise AVReconstructionError("generation_state_type")
    sequence = generation_state.plan
    approved_recompute = replace(
        selective_plan.recompute_plan,
        requested_segment_ids=selective_approval.approved_segment_ids,
        missing_required_segment_ids=(),
        selection_safe=True,
    )
    if not (
        sequence.workspace_id == selective_plan.workspace_id
        and sequence.workspace_revision == selective_plan.workspace_revision
        and sequence.workspace_fingerprint == selective_plan.workspace_fingerprint
        and sequence.manifest_fingerprints == selective_plan.manifest_fingerprints
        and sequence.recompute_plan_fingerprint
        == canonical_fingerprint(approved_recompute.to_public_dict())
        and sequence.clean_segment_ids == selective_plan.reused_segment_ids
        and tuple(item.segment_id for item in sequence.jobs) == selective_plan.queued_segment_ids
    ):
        raise AVReconstructionError("generation_plan_identity_mismatch")
    runtime_by_job = {item.job_id: item for item in generation_state.runtimes}
    if len(runtime_by_job) != len(generation_state.runtimes):
        raise AVReconstructionError("generation_runtime_duplicate")
    for job in sequence.jobs:
        runtime = runtime_by_job.get(job.job_id)
        if runtime is None or runtime.state is not GenerationJobState.SUCCEEDED:
            raise AVReconstructionError("input_not_complete")

    if type(capability) is not AVMediaCapability or type(limits) is not AVReconstructionLimits:
        raise AVReconstructionError("capability_or_limits_type")
    if type(target_profile) is not AVTargetProfile:
        raise AVReconstructionError("target_profile_type")
    # SECURITY: executable admission is the exact reviewed Gate-0 profile, not a self-consistent
    # caller-supplied widening of the same dataclass contracts.
    if capability != qualified_ffmpeg_capability():
        raise AVReconstructionError("adapter_capability_changed")
    if limits != qualified_av_limits():
        raise AVReconstructionError("resource_limit_profile_changed")
    if (
        target_profile != qualified_av_target_profile()
        or target_profile.fingerprint != capability.target_profile_fingerprint
    ):
        raise AVReconstructionError("adapter_capability_changed")
    if type(planned_at_ms) is not int:
        raise AVReconstructionError("planned_at_ms")
    _positive(planned_at_ms, "planned_at_ms")

    decisions = selective_plan.decisions
    if not decisions:
        raise AVReconstructionError("zero_segment_reconstruction")
    if len(decisions) > limits.max_segments:
        raise AVReconstructionError("resource_limit:segments")
    if type(artifact_receipts) is not tuple or len(artifact_receipts) != len(decisions):
        raise AVReconstructionError("input_receipt_count")
    if type(media_descriptors) is not tuple or len(media_descriptors) != len(decisions):
        raise AVReconstructionError("input_descriptor_count")
    if type(boundary_evidence) is not tuple:
        raise AVReconstructionError("boundary_evidence_type")
    if len(boundary_evidence) != len(decisions) - 1:
        raise AVReconstructionError("boundary_missing")
    if len(boundary_evidence) > limits.max_boundaries:
        raise AVReconstructionError("resource_limit:boundaries")
    accepted_boundary_receipt_fingerprints = _fingerprints(
        accepted_boundary_receipt_fingerprints,
        "plan.boundary_receipts",
        maximum=63,
    )
    if len(accepted_boundary_receipt_fingerprints) != len(boundary_evidence):
        raise AVReconstructionError("boundary_missing")

    receipt_ids = tuple(item.segment_id for item in artifact_receipts)
    decision_ids = tuple(item.segment_id for item in decisions)
    if receipt_ids != decision_ids or len(set(receipt_ids)) != len(receipt_ids):
        raise AVReconstructionError("input_identity_mismatch")
    if tuple(item.segment_id for item in media_descriptors) != decision_ids:
        raise AVReconstructionError("inspection_identity_mismatch")
    reusable_by_id = {item.segment_id: item for item in selective_plan.reusable_receipts}
    job_by_segment = {item.segment_id: item for item in sequence.jobs}
    manifest_by_segment = dict(zip(decision_ids, selective_plan.manifest_fingerprints, strict=True))

    total_input_bytes = 0
    total_duration = Fraction(0, 1)
    total_frames = 0
    total_samples = 0
    segment_plans: list[AVSegmentPlan] = []
    timeline = Fraction(0, 1)
    descriptor_expiry = planned_at_ms + limits.approval_ttl_ms
    for decision, receipt, descriptor in zip(
        decisions, artifact_receipts, media_descriptors, strict=True
    ):
        if (
            type(receipt) is not SegmentArtifactReceipt
            or receipt.state is not ArtifactLifecycleState.COMPLETE
        ):
            raise AVReconstructionError("input_not_complete")
        if receipt.output_fingerprint is None:
            raise AVReconstructionError("input_not_complete")
        if receipt.manifest_fingerprint != manifest_by_segment[receipt.segment_id]:
            # Reused receipts intentionally belong to the accepted predecessor revision, so their
            # manifest fingerprint can differ while the M17-09 exact decision remains authority.
            if decision.disposition is not SelectiveRerunDisposition.REUSE:
                raise AVReconstructionError("input_identity_mismatch")
        if decision.disposition is SelectiveRerunDisposition.REUSE:
            expected = reusable_by_id.get(decision.segment_id)
            if expected is None or expected.fingerprint != receipt.fingerprint:
                raise AVReconstructionError("input_identity_mismatch")
            if decision.reused_receipt_fingerprint != receipt.fingerprint:
                raise AVReconstructionError("input_identity_mismatch")
        elif decision.disposition is SelectiveRerunDisposition.QUEUE:
            found_job = job_by_segment.get(decision.segment_id)
            if found_job is None:
                raise AVReconstructionError("generation_plan_identity_mismatch")
            runtime = runtime_by_job[found_job.job_id]
            if (
                runtime.artifact_receipt_fingerprint != receipt.fingerprint
                or runtime.artifact_output_fingerprint != receipt.output_fingerprint
                or receipt.workspace_id != selective_plan.workspace_id
                or receipt.workspace_revision != selective_plan.workspace_revision
                or receipt.workspace_fingerprint != selective_plan.workspace_fingerprint
            ):
                raise AVReconstructionError("input_identity_mismatch")
        else:
            raise AVReconstructionError("predecessor_not_accepted")
        if not (
            type(descriptor) is AVMediaDescriptor
            and descriptor.artifact_receipt_fingerprint == receipt.fingerprint
            and descriptor.artifact_output_fingerprint == receipt.output_fingerprint
            and descriptor.artifact_byte_length == receipt.byte_length
            and descriptor.capability_fingerprint == capability.fingerprint
        ):
            raise AVReconstructionError("inspection_identity_mismatch")
        if not descriptor.inspected_at_ms <= planned_at_ms < descriptor.expires_at_ms:
            raise AVReconstructionError("inspection_stale")
        if descriptor.warning_codes:
            raise AVReconstructionError("inspection_invalid")
        if descriptor.expires_at_ms - descriptor.inspected_at_ms > limits.inspection_ttl_ms:
            raise AVReconstructionError("inspection_ttl_mismatch")
        descriptor_expiry = min(descriptor_expiry, descriptor.expires_at_ms)
        if receipt.byte_length > limits.max_input_bytes_per_segment:
            raise AVReconstructionError("resource_limit:segment_bytes")
        total_input_bytes += receipt.byte_length
        video = descriptor.video
        if video is None:
            raise AVReconstructionError("input_video_missing")
        if video.width > limits.max_width or video.height > limits.max_height:
            raise AVReconstructionError("resource_limit:resolution")
        if video.frame_rate.fraction > limits.max_input_fps:
            raise AVReconstructionError("resource_limit:fps")
        duration = video.end_time.fraction - video.start_time.fraction
        if duration * 1000 > limits.max_duration_ms_per_segment:
            raise AVReconstructionError("resource_limit:segment_duration")
        if descriptor.audio is not None:
            if descriptor.audio.sample_rate > limits.max_input_sample_rate:
                raise AVReconstructionError("resource_limit:sample_rate")
            if descriptor.audio.channels > limits.max_input_channels:
                raise AVReconstructionError("resource_limit:channels")
        operations, accounting = _assess_segment(descriptor, target_profile, capability)
        total_duration += duration
        total_frames += accounting.emitted_frames
        total_samples += accounting.emitted_samples
        start = AVRational(timeline.numerator, timeline.denominator)
        timeline += duration
        end = AVRational(timeline.numerator, timeline.denominator)
        segment_plans.append(
            AVSegmentPlan(
                segment_id=decision.segment_id,
                disposition=decision.disposition,
                artifact_receipt_fingerprint=receipt.fingerprint,
                artifact_output_fingerprint=receipt.output_fingerprint,
                descriptor=descriptor,
                operations=operations,
                accounting=accounting,
                output_start=start,
                output_end=end,
            )
        )
    if total_input_bytes > limits.max_aggregate_input_bytes:
        raise AVReconstructionError("resource_limit:aggregate_input")
    if total_duration * 1000 > limits.max_total_duration_ms:
        raise AVReconstructionError("resource_limit:total_duration")
    if total_frames > limits.max_video_frames:
        raise AVReconstructionError("resource_limit:video_frames")
    if total_samples > limits.max_audio_sample_frames:
        raise AVReconstructionError("resource_limit:audio_samples")

    for index, boundary in enumerate(boundary_evidence):
        if type(boundary) is not AVBoundaryEvidence:
            raise AVReconstructionError("boundary_evidence_type")
        predecessor = artifact_receipts[index]
        successor = artifact_receipts[index + 1]
        if (
            boundary.workspace_id != selective_plan.workspace_id
            or boundary.workspace_revision != selective_plan.workspace_revision
            or boundary.workspace_fingerprint != selective_plan.workspace_fingerprint
            or boundary.predecessor_segment_id != predecessor.segment_id
            or boundary.successor_segment_id != successor.segment_id
        ):
            raise AVReconstructionError("boundary_identity_mismatch")
        boundary_receipt = boundary.receipt
        # SECURITY: cut/restart receipts do not self-bind a segment pair in M17-10; the separate
        # accepted fingerprint mapping is therefore required before the process-local wrapper.
        if boundary_receipt.fingerprint != accepted_boundary_receipt_fingerprints[index]:
            raise AVReconstructionError("boundary_identity_mismatch")
        if boundary_receipt.mode is ContinuityMode.NATIVE_FRAME_HANDOFF and (
            boundary_receipt.predecessor_receipt_fingerprint != predecessor.fingerprint
            or boundary_receipt.predecessor_output_fingerprint != predecessor.output_fingerprint
        ):
            raise AVReconstructionError("boundary_identity_mismatch")
        if boundary_receipt.mode is ContinuityMode.NATIVE_FRAME_HANDOFF:
            successor_job = job_by_segment.get(successor.segment_id)
            if (
                successor_job is None
                or boundary_receipt.successor_job_id != successor_job.job_id
                or boundary_receipt.successor_graph_fingerprint != successor_job.graph_fingerprint
                or boundary_receipt.successor_graph_fingerprint != successor.graph_fingerprint
            ):
                raise AVReconstructionError("boundary_identity_mismatch")

    return AVReconstructionPlan(
        workspace_id=selective_plan.workspace_id,
        workspace_revision=selective_plan.workspace_revision,
        workspace_fingerprint=selective_plan.workspace_fingerprint,
        selective_plan_fingerprint=selective_plan.fingerprint,
        selective_approval_fingerprint=selective_approval.fingerprint,
        generation_plan_fingerprint=sequence.fingerprint,
        generation_state_fingerprint=generation_state.fingerprint,
        manifest_fingerprints=selective_plan.manifest_fingerprints,
        artifact_receipt_fingerprints=tuple(item.fingerprint for item in artifact_receipts),
        boundary_receipt_fingerprints=tuple(item.receipt.fingerprint for item in boundary_evidence),
        capability=capability,
        limits=limits,
        target_profile=target_profile,
        segments=tuple(segment_plans),
        boundaries=boundary_evidence,
        planned_at_ms=planned_at_ms,
        approval_deadline_ms=descriptor_expiry,
        _factory_token=_factory_token,
    )


def _build_m26_derived_av_reconstruction_plan(
    *,
    workspace_id: str,
    workspace_revision: int,
    workspace_fingerprint: str,
    assembly_authorization_fingerprint: str,
    generation_plan_fingerprint: str,
    generation_state_fingerprint: str,
    manifest_fingerprints: tuple[str, ...],
    derived_input_receipt_fingerprints: tuple[str, ...],
    source_contribution_fingerprints: tuple[str, ...],
    segment_dispositions: tuple[SelectiveRerunDisposition, ...],
    media_descriptors: tuple[AVMediaDescriptor, ...],
    boundary_evidence: tuple[AVBoundaryEvidence, ...],
    capability: AVMediaCapability,
    limits: AVReconstructionLimits,
    target_profile: AVTargetProfile,
    planned_at_ms: int,
    _factory_token: object,
) -> AVReconstructionPlan:
    """Mint the exact legacy executor plan for verified M26-derived inputs.

    The M26 outer authority retains original artifact identities.  This embedded plan names only
    the freshly cropped receipt/content identities so cropped bytes can never be mislabeled as an
    original generation artifact.
    """

    _identifier(workspace_id, "m26.workspace_id")
    _positive(workspace_revision, "m26.workspace_revision", 1_000_000)
    for value, field_name in (
        (workspace_fingerprint, "m26.workspace"),
        (assembly_authorization_fingerprint, "m26.authorization"),
        (generation_plan_fingerprint, "m26.generation_plan"),
        (generation_state_fingerprint, "m26.generation_state"),
    ):
        _fingerprint(value, field_name)
    manifests = _fingerprints(manifest_fingerprints, "m26.manifests")
    derived = _fingerprints(derived_input_receipt_fingerprints, "m26.derived_inputs")
    contributions = _fingerprints(
        source_contribution_fingerprints,
        "m26.source_contributions",
    )
    if (
        type(segment_dispositions) is not tuple
        or type(media_descriptors) is not tuple
        or type(boundary_evidence) is not tuple
        or not manifests
        or len(manifests) != len(derived)
        or len(derived) != len(contributions)
        or len(contributions) != len(segment_dispositions)
        or len(segment_dispositions) != len(media_descriptors)
        or len(boundary_evidence) != len(media_descriptors) - 1
        or not all(type(item) is SelectiveRerunDisposition for item in segment_dispositions)
        or not all(type(item) is AVMediaDescriptor for item in media_descriptors)
        or not all(type(item) is AVBoundaryEvidence for item in boundary_evidence)
    ):
        raise AVReconstructionError("m26_predecessor_order")
    if (
        type(capability) is not AVMediaCapability
        or type(limits) is not AVReconstructionLimits
        or type(target_profile) is not AVTargetProfile
        or capability != qualified_ffmpeg_capability()
        or limits != qualified_av_limits()
        or target_profile != qualified_av_target_profile()
    ):
        raise AVReconstructionError("adapter_capability_changed")
    _positive(planned_at_ms, "m26.planned_at_ms")

    segment_plans: list[AVSegmentPlan] = []
    output_cursor = Fraction(0, 1)
    descriptor_expiry = planned_at_ms + limits.approval_ttl_ms
    for receipt_fingerprint, disposition, descriptor in zip(
        derived,
        segment_dispositions,
        media_descriptors,
        strict=True,
    ):
        video = descriptor.video
        audio = descriptor.audio
        if (
            descriptor.artifact_receipt_fingerprint != receipt_fingerprint
            or descriptor.capability_fingerprint != capability.fingerprint
            or descriptor.container != "mp4"
            or descriptor.warning_codes
            or video is None
            or audio is None
            or video.frame_rate != AVRational(24, 1)
            or audio.sample_rate != 32_000
            or audio.channels != 2
            or audio.channel_layout != "stereo"
            or video.decoded_frame_count * 32_000 != audio.decoded_sample_count * 24
        ):
            raise AVReconstructionError("m26_derived_input_mismatch")
        operations, accounting = _assess_segment(descriptor, target_profile, capability)
        # IMPORTANT: M26 admits a complete 24/32 contribution before normalization.  Silence,
        # passthrough, remux or any undeclared transform would hide a short/malformed source.
        if (
            operations != (AVOperation.NORMALIZE_VIDEO, AVOperation.NORMALIZE_AUDIO)
            or accounting.decoded_frames != video.decoded_frame_count
            or accounting.carried_frames != video.decoded_frame_count
            or accounting.dropped_frames != 0
            or accounting.generated_frames != accounting.emitted_frames - video.decoded_frame_count
            or accounting.decoded_samples != audio.decoded_sample_count
            or accounting.carried_samples != audio.decoded_sample_count
            or accounting.dropped_samples != 0
            or accounting.inserted_samples
            != accounting.emitted_samples - audio.decoded_sample_count
        ):
            raise AVReconstructionError("m26_normalization_policy")
        duration = video.end_time.fraction - video.start_time.fraction
        segment_plans.append(
            AVSegmentPlan(
                segment_id=descriptor.segment_id,
                disposition=disposition,
                artifact_receipt_fingerprint=receipt_fingerprint,
                artifact_output_fingerprint=descriptor.artifact_output_fingerprint,
                descriptor=descriptor,
                operations=operations,
                accounting=accounting,
                output_start=AVRational(output_cursor.numerator, output_cursor.denominator),
                output_end=AVRational(
                    (output_cursor + duration).numerator,
                    (output_cursor + duration).denominator,
                ),
            )
        )
        output_cursor += duration
        descriptor_expiry = min(descriptor_expiry, descriptor.expires_at_ms)

    for index, boundary in enumerate(boundary_evidence):
        if (
            boundary.workspace_id != workspace_id
            or boundary.workspace_revision != workspace_revision
            or boundary.workspace_fingerprint != workspace_fingerprint
            or boundary.predecessor_segment_id != segment_plans[index].segment_id
            or boundary.successor_segment_id != segment_plans[index + 1].segment_id
            or boundary.receipt.mode is not ContinuityMode.CUT
        ):
            raise AVReconstructionError("m26_cut_boundary_mismatch")
    if descriptor_expiry <= planned_at_ms:
        raise AVReconstructionError("inspection_stale")
    derived_join_fingerprint = canonical_fingerprint(
        {
            "schema": "h3.context.m26.derived_av_join.v1",
            "authorization_fingerprint": assembly_authorization_fingerprint,
            "derived_input_receipt_fingerprints": list(derived),
            "source_contribution_fingerprints": list(contributions),
        }
    )
    return AVReconstructionPlan(
        workspace_id=workspace_id,
        workspace_revision=workspace_revision,
        workspace_fingerprint=workspace_fingerprint,
        selective_plan_fingerprint=derived_join_fingerprint,
        selective_approval_fingerprint=assembly_authorization_fingerprint,
        generation_plan_fingerprint=generation_plan_fingerprint,
        generation_state_fingerprint=generation_state_fingerprint,
        manifest_fingerprints=manifests,
        artifact_receipt_fingerprints=derived,
        boundary_receipt_fingerprints=tuple(item.receipt.fingerprint for item in boundary_evidence),
        capability=capability,
        limits=limits,
        target_profile=target_profile,
        segments=tuple(segment_plans),
        boundaries=boundary_evidence,
        planned_at_ms=planned_at_ms,
        approval_deadline_ms=descriptor_expiry,
        _factory_token=_factory_token,
    )


def _make_av_plan_authority() -> tuple[
    Callable[[object, str], bytes | None],
    Callable[[object], bool],
    Callable[..., AVReconstructionPlan],
    Callable[..., AVReconstructionPlan],
]:
    factory_token = object()
    attestation_key = secrets.token_bytes(32)

    def mint_attestation(candidate: object, plan_fingerprint: str) -> bytes | None:
        if candidate is not factory_token:
            return None
        return digest(
            attestation_key,
            plan_fingerprint.encode("ascii"),
            sha256,
        )

    def has_provenance(plan: object) -> bool:
        if type(plan) is not AVReconstructionPlan:
            return False
        try:
            # SECURITY: authenticate current canonical fields, not only the cached fingerprint;
            # this catches mutation even if frozen dataclass guards are bypassed.
            if plan.fingerprint != canonical_fingerprint(plan._wire_without_fingerprint()):
                return False
            expected = digest(
                attestation_key,
                plan.fingerprint.encode("ascii"),
                sha256,
            )
            return type(plan._factory_attestation) is bytes and compare_digest(
                plan._factory_attestation,
                expected,
            )
        except Exception:
            return False

    def build(
        *,
        selective_plan: SelectiveRerunPlan,
        selective_approval: SelectiveRerunApproval,
        generation_state: GenerationSequenceState,
        artifact_receipts: tuple[SegmentArtifactReceipt, ...],
        media_descriptors: tuple[AVMediaDescriptor, ...],
        boundary_evidence: tuple[AVBoundaryEvidence, ...],
        accepted_boundary_receipt_fingerprints: tuple[str, ...],
        capability: AVMediaCapability,
        limits: AVReconstructionLimits,
        target_profile: AVTargetProfile,
        planned_at_ms: int,
    ) -> AVReconstructionPlan:
        return _build_av_reconstruction_plan(
            selective_plan=selective_plan,
            selective_approval=selective_approval,
            generation_state=generation_state,
            artifact_receipts=artifact_receipts,
            media_descriptors=media_descriptors,
            boundary_evidence=boundary_evidence,
            accepted_boundary_receipt_fingerprints=accepted_boundary_receipt_fingerprints,
            capability=capability,
            limits=limits,
            target_profile=target_profile,
            planned_at_ms=planned_at_ms,
            _factory_token=factory_token,
        )

    def build_m26(
        *,
        workspace_id: str,
        workspace_revision: int,
        workspace_fingerprint: str,
        assembly_authorization_fingerprint: str,
        generation_plan_fingerprint: str,
        generation_state_fingerprint: str,
        manifest_fingerprints: tuple[str, ...],
        derived_input_receipt_fingerprints: tuple[str, ...],
        source_contribution_fingerprints: tuple[str, ...],
        segment_dispositions: tuple[SelectiveRerunDisposition, ...],
        media_descriptors: tuple[AVMediaDescriptor, ...],
        boundary_evidence: tuple[AVBoundaryEvidence, ...],
        capability: AVMediaCapability,
        limits: AVReconstructionLimits,
        target_profile: AVTargetProfile,
        planned_at_ms: int,
    ) -> AVReconstructionPlan:
        return _build_m26_derived_av_reconstruction_plan(
            workspace_id=workspace_id,
            workspace_revision=workspace_revision,
            workspace_fingerprint=workspace_fingerprint,
            assembly_authorization_fingerprint=assembly_authorization_fingerprint,
            generation_plan_fingerprint=generation_plan_fingerprint,
            generation_state_fingerprint=generation_state_fingerprint,
            manifest_fingerprints=manifest_fingerprints,
            derived_input_receipt_fingerprints=derived_input_receipt_fingerprints,
            source_contribution_fingerprints=source_contribution_fingerprints,
            segment_dispositions=segment_dispositions,
            media_descriptors=media_descriptors,
            boundary_evidence=boundary_evidence,
            capability=capability,
            limits=limits,
            target_profile=target_profile,
            planned_at_ms=planned_at_ms,
            _factory_token=factory_token,
        )

    return mint_attestation, has_provenance, build, build_m26


(
    _mint_plan_attestation,
    _has_plan_factory_provenance,
    build_av_reconstruction_plan,
    build_m26_derived_av_reconstruction_plan,
) = _make_av_plan_authority()
del _make_av_plan_authority


@dataclass(frozen=True, slots=True)
class AVReconstructionApproval:
    plan_fingerprint: str
    decision_fingerprints: tuple[str, ...]
    approved_at_ms: int
    expires_at_ms: int
    approval_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _fingerprint(self.plan_fingerprint, "approval.plan")
        _fingerprints(self.decision_fingerprints, "approval.decisions")
        _positive(self.approved_at_ms, "approval.approved_at_ms")
        _positive(self.expires_at_ms, "approval.expires_at_ms")
        if self.expires_at_ms <= self.approved_at_ms:
            raise AVReconstructionError("approval_expiry")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.approval_fingerprint is None:
            object.__setattr__(self, "approval_fingerprint", expected)
        elif self.approval_fingerprint != expected:
            raise AVReconstructionError("approval_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.approval_fingerprint is None:  # pragma: no cover
            raise AVReconstructionError("approval_fingerprint_uninitialized")
        return self.approval_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "plan_fingerprint": self.plan_fingerprint,
            "decision_fingerprints": list(self.decision_fingerprints),
            "approved_at_ms": self.approved_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }

    def assert_executable(self, plan: AVReconstructionPlan, *, now_ms: int) -> None:
        if (
            type(plan) is not AVReconstructionPlan
            or not _has_plan_factory_provenance(plan)
            or plan.fingerprint != self.plan_fingerprint
        ):
            raise AVReconstructionError("approval_plan_mismatch")
        if self.decision_fingerprints != tuple(item.decision_fingerprint for item in plan.segments):
            raise AVReconstructionError("approval_decision_mismatch")
        if type(now_ms) is not int or now_ms < self.approved_at_ms or now_ms >= self.expires_at_ms:
            raise AVReconstructionError("approval_stale")
        if self.expires_at_ms > plan.approval_deadline_ms:
            raise AVReconstructionError("approval_stale")


def approve_av_reconstruction_plan(
    plan: AVReconstructionPlan,
    *,
    approved_at_ms: int,
    expires_at_ms: int,
) -> AVReconstructionApproval:
    if type(plan) is not AVReconstructionPlan:
        raise AVReconstructionError("plan_type")
    if not _has_plan_factory_provenance(plan):
        raise AVReconstructionError("plan_factory_required")
    if approved_at_ms < plan.planned_at_ms or expires_at_ms > plan.approval_deadline_ms:
        raise AVReconstructionError("approval_stale")
    return AVReconstructionApproval(
        plan_fingerprint=plan.fingerprint,
        decision_fingerprints=tuple(item.decision_fingerprint for item in plan.segments),
        approved_at_ms=approved_at_ms,
        expires_at_ms=expires_at_ms,
    )


@dataclass(frozen=True, slots=True)
class AVReceiptOutput:
    handle: str
    kind: AVOutputKind
    byte_length: int
    content_fingerprint: str
    video_frame_count: int
    audio_sample_count: int
    duration: AVRational

    def __post_init__(self) -> None:
        _handle(self.handle, "output.handle")
        if type(self.kind) is not AVOutputKind:
            raise AVReconstructionError("output.kind")
        _positive(self.byte_length, "output.byte_length", 4_294_967_296)
        _fingerprint(self.content_fingerprint, "output.content")
        _positive(self.video_frame_count, "output.video_frame_count", 10_000_000)
        _nonnegative(self.audio_sample_count, "output.audio_sample_count", 1_000_000_000)
        if type(self.duration) is not AVRational or self.duration.numerator == 0:
            raise AVReconstructionError("output.duration")

    def to_wire(self) -> dict[str, object]:
        return {
            "handle": self.handle,
            "kind": self.kind.value,
            "byte_length": self.byte_length,
            "content_fingerprint": self.content_fingerprint,
            "video_frame_count": self.video_frame_count,
            "audio_sample_count": self.audio_sample_count,
            "duration": self.duration.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class AVReceiptSegmentResult:
    segment_id: str
    artifact_receipt_fingerprint: str
    operations: tuple[AVOperation, ...]
    accounting: AVStreamAccounting
    output_start: AVRational
    output_end: AVRational
    derived_output_handle: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "receipt_segment.segment_id")
        _fingerprint(
            self.artifact_receipt_fingerprint,
            "receipt_segment.artifact_receipt",
        )
        if type(self.operations) is not tuple or not self.operations:
            raise AVReconstructionError("receipt_segment.operations")
        if not all(type(item) is AVOperation for item in self.operations):
            raise AVReconstructionError("receipt_segment.operation_type")
        if (
            len(set(self.operations)) != len(self.operations)
            or AVOperation.REJECT in self.operations
        ):
            raise AVReconstructionError("receipt_segment.operation_set")
        if type(self.accounting) is not AVStreamAccounting:
            raise AVReconstructionError("receipt_segment.accounting")
        if type(self.output_start) is not AVRational or type(self.output_end) is not AVRational:
            raise AVReconstructionError("receipt_segment.output_span")
        if self.output_end.fraction <= self.output_start.fraction:
            raise AVReconstructionError("receipt_segment.output_span_order")
        if self.derived_output_handle is not None:
            _handle(self.derived_output_handle, "receipt_segment.derived_output_handle")
        if self.operations == (AVOperation.PASSTHROUGH,) and self.derived_output_handle is not None:
            raise AVReconstructionError("unchanged_segment_export")

    def to_wire(self) -> dict[str, object]:
        value: dict[str, object] = {
            "segment_id": self.segment_id,
            "artifact_receipt_fingerprint": self.artifact_receipt_fingerprint,
            "operations": [item.value for item in self.operations],
            "accounting": self.accounting.to_wire(),
            "output_start": self.output_start.to_wire(),
            "output_end": self.output_end.to_wire(),
        }
        if self.derived_output_handle is not None:
            value["derived_output_handle"] = self.derived_output_handle
        return value


@dataclass(frozen=True, slots=True)
class AVReconstructionReceipt:
    """The sole portable and persisted M17-11 contract."""

    transaction_id: str
    plan_fingerprint: str
    approval_fingerprint: str
    selective_plan_fingerprint: str
    generation_state_fingerprint: str
    artifact_receipt_fingerprints: tuple[str, ...]
    boundary_receipt_fingerprints: tuple[str, ...]
    capability_fingerprint: str
    execution_fingerprint: str
    segment_results: tuple[AVReceiptSegmentResult, ...]
    outputs: tuple[AVReceiptOutput, ...]
    publication_state: AVPublicationState
    completed_at_ms: int
    receipt_fingerprint: str | None = None
    schema: str = AV_RECONSTRUCTION_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != AV_RECONSTRUCTION_RECEIPT_SCHEMA:
            raise AVReconstructionError("unsupported_receipt_schema")
        _identifier(self.transaction_id, "receipt.transaction_id")
        for value, field in (
            (self.plan_fingerprint, "receipt.plan"),
            (self.approval_fingerprint, "receipt.approval"),
            (self.selective_plan_fingerprint, "receipt.selective"),
            (self.generation_state_fingerprint, "receipt.generation_state"),
            (self.capability_fingerprint, "receipt.capability"),
            (self.execution_fingerprint, "receipt.execution"),
        ):
            _fingerprint(value, field)
        artifacts = _fingerprints(self.artifact_receipt_fingerprints, "receipt.artifacts")
        if not artifacts:
            raise AVReconstructionError("receipt_artifacts_empty")
        boundaries = _fingerprints(
            self.boundary_receipt_fingerprints, "receipt.boundaries", maximum=63
        )
        if len(boundaries) != len(artifacts) - 1:
            raise AVReconstructionError("receipt_boundary_count")
        if (
            type(self.segment_results) is not tuple
            or len(self.segment_results) != len(artifacts)
            or not all(type(item) is AVReceiptSegmentResult for item in self.segment_results)
        ):
            raise AVReconstructionError("receipt_segment_results")
        if tuple(item.artifact_receipt_fingerprint for item in self.segment_results) != artifacts:
            raise AVReconstructionError("receipt_segment_artifact_order")
        if len({item.segment_id for item in self.segment_results}) != len(self.segment_results):
            raise AVReconstructionError("receipt_segment_duplicate")
        for index in range(1, len(self.segment_results)):
            if (
                self.segment_results[index - 1].output_end
                != self.segment_results[index].output_start
            ):
                raise AVReconstructionError("receipt_timeline_not_contiguous")
        if type(self.outputs) is not tuple or not self.outputs or len(self.outputs) > 65:
            raise AVReconstructionError("receipt_outputs")
        if not all(type(item) is AVReceiptOutput for item in self.outputs):
            raise AVReconstructionError("receipt_output_type")
        if len({item.handle for item in self.outputs}) != len(self.outputs):
            raise AVReconstructionError("duplicate_output_handle")
        if sum(item.kind is AVOutputKind.RECONSTRUCTION_FULL for item in self.outputs) != 1:
            raise AVReconstructionError("receipt_full_output_count")
        output_handles = {item.handle for item in self.outputs}
        segment_export_handles = {
            item.handle for item in self.outputs if item.kind is AVOutputKind.SEGMENT_EXPORT
        }
        referenced_segment_exports: set[str] = set()
        for result in self.segment_results:
            if result.derived_output_handle is not None:
                referenced_segment_exports.add(result.derived_output_handle)
                matches = tuple(
                    item
                    for item in self.outputs
                    if item.handle == result.derived_output_handle
                    and item.kind is AVOutputKind.SEGMENT_EXPORT
                )
                if len(matches) != 1:
                    raise AVReconstructionError("receipt_derived_output_missing")
        if segment_export_handles != referenced_segment_exports:
            raise AVReconstructionError("receipt_unreferenced_segment_export")
        if not output_handles:
            raise AVReconstructionError("receipt_outputs")
        full_output = next(
            item for item in self.outputs if item.kind is AVOutputKind.RECONSTRUCTION_FULL
        )
        if (
            full_output.video_frame_count
            != sum(item.accounting.emitted_frames for item in self.segment_results)
            or full_output.audio_sample_count
            != sum(item.accounting.emitted_samples for item in self.segment_results)
            or full_output.duration.fraction
            != self.segment_results[-1].output_end.fraction
            - self.segment_results[0].output_start.fraction
        ):
            raise AVReconstructionError("receipt_aggregate_accounting")
        if type(self.publication_state) is not AVPublicationState:
            raise AVReconstructionError("receipt_publication_state")
        _positive(self.completed_at_ms, "receipt.completed_at_ms")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise AVReconstructionError("receipt_fingerprint_mismatch")
        if len(self.to_wire_bytes()) > MAX_AV_RECONSTRUCTION_RECEIPT_BYTES:
            raise AVReconstructionError("receipt_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover
            raise AVReconstructionError("receipt_fingerprint_uninitialized")
        return self.receipt_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "plan_fingerprint": self.plan_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "selective_plan_fingerprint": self.selective_plan_fingerprint,
            "generation_state_fingerprint": self.generation_state_fingerprint,
            "artifact_receipt_fingerprints": list(self.artifact_receipt_fingerprints),
            "boundary_receipt_fingerprints": list(self.boundary_receipt_fingerprints),
            "capability_fingerprint": self.capability_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "segment_results": [item.to_wire() for item in self.segment_results],
            "outputs": [item.to_wire() for item in self.outputs],
            "publication_state": self.publication_state.value,
            "completed_at_ms": self.completed_at_ms,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["receipt_fingerprint"] = self.fingerprint
        return value

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "publication_state": self.publication_state.value,
            "completed_at_ms": self.completed_at_ms,
            "outputs": [
                {
                    "handle": item.handle,
                    "kind": item.kind.value,
                    "byte_length": item.byte_length,
                    "video_frame_count": item.video_frame_count,
                    "audio_sample_count": item.audio_sample_count,
                    "duration": item.duration.to_wire(),
                }
                for item in self.outputs
            ],
            "segments": [
                {
                    "segment_id": item.segment_id,
                    "operations": [operation.value for operation in item.operations],
                    "accounting": item.accounting.to_wire(),
                    "output_start": item.output_start.to_wire(),
                    "output_end": item.output_end.to_wire(),
                    **(
                        {}
                        if item.derived_output_handle is None
                        else {"derived_output_handle": item.derived_output_handle}
                    ),
                }
                for item in self.segment_results
            ],
        }


_RECEIPT_FIELDS = {
    "schema",
    "transaction_id",
    "plan_fingerprint",
    "approval_fingerprint",
    "selective_plan_fingerprint",
    "generation_state_fingerprint",
    "artifact_receipt_fingerprints",
    "boundary_receipt_fingerprints",
    "capability_fingerprint",
    "execution_fingerprint",
    "segment_results",
    "outputs",
    "publication_state",
    "completed_at_ms",
    "receipt_fingerprint",
}
_OUTPUT_FIELDS = {
    "handle",
    "kind",
    "byte_length",
    "content_fingerprint",
    "video_frame_count",
    "audio_sample_count",
    "duration",
}
_RATIONAL_FIELDS = {"numerator", "denominator"}
_SEGMENT_RESULT_REQUIRED_FIELDS = {
    "segment_id",
    "artifact_receipt_fingerprint",
    "operations",
    "accounting",
    "output_start",
    "output_end",
}
_ACCOUNTING_FIELDS = {
    "decoded_frames",
    "carried_frames",
    "dropped_frames",
    "generated_frames",
    "emitted_frames",
    "decoded_samples",
    "carried_samples",
    "dropped_samples",
    "inserted_samples",
    "emitted_samples",
}


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AVReconstructionError("duplicate_receipt_member")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> object:
    raise AVReconstructionError("nonfinite_receipt_value")


def _decode_rational(value: object, field: str) -> AVRational:
    if type(value) is not dict or set(value) != _RATIONAL_FIELDS:
        raise AVReconstructionError(f"receipt_members:{field}")
    return AVRational(value["numerator"], value["denominator"])


def decode_av_reconstruction_receipt(
    payload: str | bytes | bytearray,
) -> AVReconstructionReceipt:
    """Strictly decode canonical UTF-8 receipt bytes with duplicate rejection."""

    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeError:
            raise AVReconstructionError("receipt_utf8") from None
        text = payload
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError:
            raise AVReconstructionError("receipt_utf8") from None
    else:
        raise AVReconstructionError("receipt_payload_type")
    if len(encoded) > MAX_AV_RECONSTRUCTION_RECEIPT_BYTES:
        raise AVReconstructionError("receipt_wire_limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except AVReconstructionError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError):
        raise AVReconstructionError("receipt_json") from None
    if type(value) is not dict or set(value) != _RECEIPT_FIELDS:
        raise AVReconstructionError("receipt_members")
    outputs_value = value["outputs"]
    if type(outputs_value) is not list:
        raise AVReconstructionError("receipt_outputs")
    outputs: list[AVReceiptOutput] = []
    for item in outputs_value:
        if type(item) is not dict or set(item) != _OUTPUT_FIELDS:
            raise AVReconstructionError("receipt_members:output")
        try:
            kind = AVOutputKind(item["kind"])
        except (TypeError, ValueError):
            raise AVReconstructionError("receipt_output_kind") from None
        outputs.append(
            AVReceiptOutput(
                handle=item["handle"],
                kind=kind,
                byte_length=item["byte_length"],
                content_fingerprint=item["content_fingerprint"],
                video_frame_count=item["video_frame_count"],
                audio_sample_count=item["audio_sample_count"],
                duration=_decode_rational(item["duration"], "output.duration"),
            )
        )
    segment_values = value["segment_results"]
    if type(segment_values) is not list:
        raise AVReconstructionError("receipt_segment_results")
    segment_results: list[AVReceiptSegmentResult] = []
    for item in segment_values:
        if type(item) is not dict or not (
            _SEGMENT_RESULT_REQUIRED_FIELDS.issubset(item)
            and set(item).issubset(_SEGMENT_RESULT_REQUIRED_FIELDS | {"derived_output_handle"})
        ):
            raise AVReconstructionError("receipt_members:segment_result")
        operations = item["operations"]
        accounting = item["accounting"]
        if (
            type(operations) is not list
            or type(accounting) is not dict
            or set(accounting) != _ACCOUNTING_FIELDS
        ):
            raise AVReconstructionError("receipt_members:segment_result")
        try:
            decoded_operations = tuple(AVOperation(value) for value in operations)
        except (TypeError, ValueError):
            raise AVReconstructionError("receipt_segment_operation") from None
        segment_results.append(
            AVReceiptSegmentResult(
                segment_id=item["segment_id"],
                artifact_receipt_fingerprint=item["artifact_receipt_fingerprint"],
                operations=decoded_operations,
                accounting=AVStreamAccounting(
                    **cast(dict[str, int], accounting),
                ),
                output_start=_decode_rational(item["output_start"], "segment.output_start"),
                output_end=_decode_rational(item["output_end"], "segment.output_end"),
                derived_output_handle=item.get("derived_output_handle"),
            )
        )
    artifacts = value["artifact_receipt_fingerprints"]
    boundaries = value["boundary_receipt_fingerprints"]
    if type(artifacts) is not list or type(boundaries) is not list:
        raise AVReconstructionError("receipt_fingerprint_arrays")
    try:
        publication_state = AVPublicationState(value["publication_state"])
    except (TypeError, ValueError):
        raise AVReconstructionError("receipt_publication_state") from None
    receipt = AVReconstructionReceipt(
        transaction_id=value["transaction_id"],
        plan_fingerprint=value["plan_fingerprint"],
        approval_fingerprint=value["approval_fingerprint"],
        selective_plan_fingerprint=value["selective_plan_fingerprint"],
        generation_state_fingerprint=value["generation_state_fingerprint"],
        artifact_receipt_fingerprints=tuple(artifacts),
        boundary_receipt_fingerprints=tuple(boundaries),
        capability_fingerprint=value["capability_fingerprint"],
        execution_fingerprint=value["execution_fingerprint"],
        segment_results=tuple(segment_results),
        outputs=tuple(outputs),
        publication_state=publication_state,
        completed_at_ms=value["completed_at_ms"],
        receipt_fingerprint=value["receipt_fingerprint"],
        schema=value["schema"],
    )
    if encoded != receipt.to_wire_bytes():
        raise AVReconstructionError("receipt_not_canonical")
    return receipt
