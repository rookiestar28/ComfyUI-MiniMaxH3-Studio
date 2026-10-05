"""Bounded generators for Authoring browser media derivatives."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import NoReturn, SupportsIndex, cast

from ..core.authoring_audio_peaks import (
    AUDIO_PEAKS_MAX_PCM_BYTES,
    AUDIO_PEAKS_SAMPLE_RATE,
    AudioPeaksError,
    decode_audio_peaks_envelope,
    encode_audio_peaks_envelope,
)
from ..core.av_reconstruction import qualified_ffmpeg_capability
from ..core.errors import MediaProcessError
from ..core.safe_paths import ensure_directory
from .authoring_derivative_jpeg import FilmstripJpegError, probe_filmstrip_jpeg
from .authoring_derivative_png import (
    PNG_IMAGE_MAX_BYTES,
    PNG_MAX_EDGE,
    PNG_MAX_PIXELS,
    PNG_MEDIA_TYPE,
    PNG_THUMBNAIL_MAX_BYTES,
    THUMBNAIL_MAX_EDGE,
    RGB8PNGError,
    encode_rgb8_png,
    png_generator_profile_fingerprint,
    probe_rgb8_png,
)
from .authoring_image_source import OwnedImageSource
from .authoring_video_facts import (
    AuthoringVideoFacts,
    AuthoringVideoRational,
    probe_authoring_video_facts,
)
from .av_reconstruction_media import (
    ENCODER_THREAD_RULE,
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
    _pin_exact_executable,
    _read_regular_media_body,
    encoder_thread_count,
)
from .media_subprocess import (
    CancellationProbe,
    MediaProcessInvocation,
    OwnedOutputLease,
    ProcessStatus,
    SubprocessMediaRunner,
    create_output_lease,
)
from .segment_artifact_store import ArtifactStoreError, _write_new_file

IMAGE_PROXY_PROFILE_ID = "h3.authoring.image_proxy.png.v1"
THUMBNAIL_PROFILE_ID = "h3.authoring.thumbnail.png.v1"
FILMSTRIP_PROFILE_ID = "h3.authoring.filmstrip.jpeg.v1"
AUDIO_PEAKS_PROFILE_ID = "h3.authoring.audio_peaks.v1"
AUDIO_PREVIEW_PROFILE_ID = "h3.authoring.audio_preview.wav.v1"
VIDEO_PROXY_PROFILE_ID = "h3.authoring.video_proxy.mp4.v1"
DERIVATIVE_GENERATOR_ID = "h3.repo.authoring_derivative_generator.v1"
VIDEO_PROXY_MEDIA_TYPE = "video/mp4"
FILMSTRIP_MEDIA_TYPE = "image/jpeg"
AUDIO_PEAKS_MEDIA_TYPE = "application/octet-stream"
AUDIO_PREVIEW_MEDIA_TYPE = "audio/wav"
AUDIO_PREVIEW_SAMPLE_RATE = 48_000
AUDIO_PREVIEW_MAX_BYTES = 8 * 1024 * 1024
AUDIO_PREVIEW_WAV_HEADER_BYTES = 44
FILMSTRIP_MAX_BYTES = 512 * 1024
FILMSTRIP_TILE_HEIGHT = 48
FILMSTRIP_MIN_TILES = 4
FILMSTRIP_MAX_TILES = 64
VIDEO_PROXY_MAX_BYTES = 24 * 1024 * 1024
VIDEO_SOURCE_MAX_BYTES = 64 * 1024 * 1024
VIDEO_MAX_EDGE = 2048
VIDEO_PROXY_MAX_EDGE = 1280
#: The video target is `min(VIDEO_PROXY_MAX_BITS_PER_SECOND, floor(0.9 * ceiling_bytes * 8 / d))`
#: bits per second for a source of duration `d` seconds. The 0.9 leaves room for container
#: overhead and rate-control variation; the measured output byte count, not this target, is what
#: the owned output lease actually enforces.
VIDEO_PROXY_MAX_BITS_PER_SECOND = 6_000_000
VIDEO_PROXY_GOP = 12
#: The encoder's speed setting. It trades compression efficiency for time under the same rate
#: cap, so the proxy's size stays where the bitrate rule puts it; it changes no timestamp.
VIDEO_PROXY_PRESET = "veryfast"

VIDEO_RAW_FRAME_MAX_BYTES = VIDEO_MAX_EDGE * VIDEO_MAX_EDGE * 3
GENERATOR_TRANSIENT_MAX_BYTES = 192 * 1024 * 1024
GENERATOR_WORK_SECONDS = 43.0
GENERATOR_PROCESS_SECONDS = 30.0

_SHA256_PREFIXED_LENGTH = len("sha256:") + 64
_GENERATOR_GATE = threading.Lock()


class AuthoringDerivativeGeneratorError(RuntimeError):
    """One privacy-safe closed generator failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _video_proxy_bitrate(facts: AuthoringVideoFacts, ceiling_bytes: int) -> int:
    """The proxy's video target in bits per second, in exact integer arithmetic.

    IMPORTANT (M25-45): the ceiling is a byte budget for the WHOLE proxy, so the target has to
    fall as the source lengthens; a fixed rate that fits a five-second clip overruns a fifteen
    second one and the owned output lease refuses the result after paying for the encode. The
    duration comes from the source's own last landmark, never from a wall clock or a declared
    frame rate, and the arithmetic stays integral so the same source always produces the same
    invocation.
    """

    last = facts.landmarks[-1]
    ticks = last.pts + last.duration_ticks
    if ticks <= 0:
        raise AuthoringDerivativeGeneratorError("unsupported")
    duration = Fraction(ticks * facts.source_time_base.num, facts.source_time_base.den)
    target = (9 * ceiling_bytes * 8 * duration.denominator) // (10 * duration.numerator)
    return max(1, min(VIDEO_PROXY_MAX_BITS_PER_SECOND, target))


def filmstrip_tile_count(*, ticks: int, time_base_num: int, time_base_den: int) -> int:
    """Two samples per second, rounded upward and clamped without floating point."""

    if (
        type(ticks) is not int
        or type(time_base_num) is not int
        or type(time_base_den) is not int
        or ticks < 1
        or time_base_num < 1
        or time_base_den < 1
    ):
        raise AuthoringDerivativeGeneratorError("invalid_request")
    requested = (2 * ticks * time_base_num + time_base_den - 1) // time_base_den
    return max(FILMSTRIP_MIN_TILES, min(FILMSTRIP_MAX_TILES, requested))


@dataclass(frozen=True, slots=True)
class AuthoringDerivativeBinaryCapability:
    distribution: str
    build_version: str
    license_expression: str
    ffmpeg_sha256: str
    ffprobe_sha256: str
    protocols: tuple[str, ...]
    input_demuxers: tuple[str, ...]
    input_codecs: tuple[str, ...]
    output_muxers: tuple[str, ...]
    output_encoders: tuple[str, ...]
    pixel_formats: tuple[str, ...]
    filters: tuple[str, ...]
    operations: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        payload = {
            "distribution": self.distribution,
            "build_version": self.build_version,
            "license_expression": self.license_expression,
            "ffmpeg_sha256": self.ffmpeg_sha256,
            "ffprobe_sha256": self.ffprobe_sha256,
            "protocols": list(self.protocols),
            "input_demuxers": list(self.input_demuxers),
            "input_codecs": list(self.input_codecs),
            "output_muxers": list(self.output_muxers),
            "output_encoders": list(self.output_encoders),
            "pixel_formats": list(self.pixel_formats),
            "filters": list(self.filters),
            "operations": list(self.operations),
        }
        return _fingerprint(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        )


def qualified_authoring_derivative_binary_capability() -> AuthoringDerivativeBinaryCapability:
    """Name only the newly qualified derivative operations on the accepted exact binaries."""

    base = qualified_ffmpeg_capability()
    return AuthoringDerivativeBinaryCapability(
        distribution=base.distribution,
        build_version=base.build_version,
        license_expression=base.license_expression,
        ffmpeg_sha256=base.ffmpeg_sha256.lower(),
        ffprobe_sha256=base.ffprobe_sha256.lower(),
        protocols=("file", "pipe"),
        input_demuxers=("mov",),
        input_codecs=("h264", "aac"),
        output_muxers=("mp4", "rawvideo", "image2", "s16le"),
        output_encoders=("libx264", "rawvideo", "mjpeg", "pcm_s16le"),
        pixel_formats=("yuv420p", "rgb24", "yuvj420p"),
        filters=("format", "fps", "scale", "setsar", "tile", "trim"),
        operations=(
            "full_source_pts_video_proxy",
            "source_frame_rgb24_thumbnail",
            "whole_asset_tiled_jpeg_filmstrip",
            "whole_asset_mono_s16le_audio_peaks",
            "whole_asset_channel_preserving_s16le_audio_preview",
        ),
    )


@dataclass(slots=True, repr=False)
class GeneratedDerivativeBody:
    """Single-owner generated bytes plus content-free verified metadata."""

    body: bytearray = field(repr=False, compare=False)
    profile_id: str
    media_type: str
    width: int
    height: int
    byte_count: int
    content_fingerprint: str
    generator_fingerprint: str
    source_frame: int | None
    audio_disposition: str

    def __post_init__(self) -> None:
        if type(self.body) is not bytearray or self.byte_count != len(self.body):
            raise AuthoringDerivativeGeneratorError("internal_failure")

    def clear(self) -> None:
        self.body.clear()
        self.byte_count = 0

    def take(self) -> bytearray:
        """Transfer the exact owned buffer and leave this result empty."""

        if not self.body or self.byte_count != len(self.body):
            raise AuthoringDerivativeGeneratorError("internal_failure")
        owned = self.body
        self.body = bytearray()
        self.byte_count = 0
        return owned

    def __repr__(self) -> str:
        return "<GeneratedDerivativeBody opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("generated derivative bodies are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("generated derivative bodies are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("generated derivative bodies are not serializable")


@dataclass(slots=True, repr=False)
class GeneratedAudioPeaksBody:
    """Single-owner non-visual audio-peaks bytes and verified metadata."""

    body: bytearray = field(repr=False, compare=False)
    profile_id: str
    media_type: str
    byte_count: int
    content_fingerprint: str
    generator_fingerprint: str
    source_frame: None
    audio_disposition: str

    def __post_init__(self) -> None:
        if type(self.body) is not bytearray or self.byte_count != len(self.body):
            raise AuthoringDerivativeGeneratorError("internal_failure")

    def clear(self) -> None:
        self.body.clear()
        self.byte_count = 0

    def take(self) -> bytearray:
        if not self.body or self.byte_count != len(self.body):
            raise AuthoringDerivativeGeneratorError("internal_failure")
        owned = self.body
        self.body = bytearray()
        self.byte_count = 0
        return owned

    def __repr__(self) -> str:
        return "<GeneratedAudioPeaksBody opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("generated audio peaks bodies are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("generated audio peaks bodies are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("generated audio peaks bodies are not serializable")


@dataclass(slots=True, repr=False)
class GeneratedAudioPreviewBody:
    """Single-owner canonical WAV bytes plus content-free verified metadata."""

    body: bytearray = field(repr=False, compare=False)
    profile_id: str
    media_type: str
    byte_count: int
    content_fingerprint: str
    generator_fingerprint: str
    source_frame: None
    audio_disposition: str

    def __post_init__(self) -> None:
        if type(self.body) is not bytearray or self.byte_count != len(self.body):
            raise AuthoringDerivativeGeneratorError("internal_failure")

    def clear(self) -> None:
        self.body.clear()
        self.byte_count = 0

    def take(self) -> bytearray:
        if not self.body or self.byte_count != len(self.body):
            raise AuthoringDerivativeGeneratorError("internal_failure")
        owned = self.body
        self.body = bytearray()
        self.byte_count = 0
        return owned

    def __repr__(self) -> str:
        return "<GeneratedAudioPreviewBody opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("generated audio preview bodies are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("generated audio preview bodies are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("generated audio preview bodies are not serializable")


def _fingerprint(payload: bytes | bytearray) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _valid_fingerprint(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == _SHA256_PREFIXED_LENGTH
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def authoring_derivative_profile_fingerprint() -> str:
    """Bind cache identity to every explicit generator limit and implementation pin."""

    capability = qualified_authoring_derivative_binary_capability()
    payload = {
        "algorithm": DERIVATIVE_GENERATOR_ID,
        "binary_capability_fingerprint": capability.fingerprint,
        "budgets": {
            "concurrent_generators": 1,
            "process_seconds": format(GENERATOR_PROCESS_SECONDS, ".1f"),
            "transient_bytes": GENERATOR_TRANSIENT_MAX_BYTES,
            "work_seconds": format(GENERATOR_WORK_SECONDS, ".1f"),
        },
        "image_proxy": {
            "max_bytes": PNG_IMAGE_MAX_BYTES,
            "max_edge": PNG_MAX_EDGE,
            "max_pixels": PNG_MAX_PIXELS,
            "output": "png/rgb8/truecolor/no-alpha/no-interlace/no-metadata",
            "profile_id": IMAGE_PROXY_PROFILE_ID,
            "quantization": "stdlib.struct.float32_rows.floor(value*255+0.5).v1",
        },
        "filmstrip": {
            "encoder": "mjpeg/single_baseline_image/threads_1/no_metadata",
            "max_bytes": FILMSTRIP_MAX_BYTES,
            "max_tiles": FILMSTRIP_MAX_TILES,
            "min_tiles": FILMSTRIP_MIN_TILES,
            "profile_id": FILMSTRIP_PROFILE_ID,
            "sampling": "ceil(duration_seconds*2)_clamped_4_64/uniform_fps",
            "scale": "source_aspect/even_width/lanczos",
            "tile_height": FILMSTRIP_TILE_HEIGHT,
        },
        "audio_peaks": {
            "decoded_sample_rate": AUDIO_PEAKS_SAMPLE_RATE,
            "envelope": AUDIO_PEAKS_PROFILE_ID,
            "max_pcm_bytes": AUDIO_PEAKS_MAX_PCM_BYTES,
            "output": "mono/s16le/100_pairs_per_second",
            "profile_id": AUDIO_PEAKS_PROFILE_ID,
        },
        "audio_preview": {
            "channels": "preserve_admitted_1_to_8",
            "max_bytes": AUDIO_PREVIEW_MAX_BYTES,
            "output": "riff_wave/pcm_s16le/no_metadata/canonical_44_byte_header",
            "profile_id": AUDIO_PREVIEW_PROFILE_ID,
            "sample_rate": AUDIO_PREVIEW_SAMPLE_RATE,
            "sample_coverage": "exact_admitted_canonical_samples",
        },
        "png_profile_fingerprint": png_generator_profile_fingerprint(),
        "python": ".".join(str(value) for value in sys.version_info[:3]),
        "thumbnail": {
            "decoder_threads": 1,
            "image_selection": "center_nearest_no_upscale.v1",
            "max_bytes": PNG_THUMBNAIL_MAX_BYTES,
            "max_edge": THUMBNAIL_MAX_EDGE,
            "output": "png/rgb8",
            "profile_id": THUMBNAIL_PROFILE_ID,
            "video_selection": "trim_start_frame/rawvideo/rgb24/exact_source_frame",
        },
        "video_proxy": {
            "audio": "aac_copy_or_absent",
            "b_frames": 0,
            "bitrate": "min_bits_per_second_or_ninety_percent_of_byte_ceiling",
            "buffer_size": "two_times_bitrate",
            "encoder_threads": {"rule": ENCODER_THREAD_RULE, "value": encoder_thread_count()},
            "gop": VIDEO_PROXY_GOP,
            "fps_mode": "passthrough",
            "max_bits_per_second": VIDEO_PROXY_MAX_BITS_PER_SECOND,
            "max_bytes": VIDEO_PROXY_MAX_BYTES,
            "max_edge": VIDEO_PROXY_MAX_EDGE,
            "max_rate": "equal_to_bitrate",
            "no_timing_rewrite": True,
            "output": "mp4/libx264/yuv420p/faststart",
            "preset": VIDEO_PROXY_PRESET,
            "profile_id": VIDEO_PROXY_PROFILE_ID,
            "scale_filter": "lanczos",
            "source_pixel_aspect": "declared_square_or_unknown_coded_raster.v1",
            "output_pixel_aspect": {"num": 1, "den": 1},
            "timestamp_input": "copyts",
            "track_and_encoder_timebase": "exact_source_time_base",
            "timing": "exact_source_rational_pts_dts_duration_order_count",
        },
    }
    return _fingerprint(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii"))


def _profile_fingerprint(profile_id: str, *, binary: bool) -> str:
    payload: dict[str, object] = {
        "algorithm": DERIVATIVE_GENERATOR_ID,
        "complete_profile_fingerprint": authoring_derivative_profile_fingerprint(),
        "max_image_bytes": PNG_IMAGE_MAX_BYTES,
        "max_thumbnail_bytes": PNG_THUMBNAIL_MAX_BYTES,
        "max_thumbnail_edge": THUMBNAIL_MAX_EDGE,
        "profile_id": profile_id,
        "python": ".".join(str(value) for value in sys.version_info[:3]),
    }
    if profile_id in {IMAGE_PROXY_PROFILE_ID, THUMBNAIL_PROFILE_ID}:
        payload["png_profile_fingerprint"] = png_generator_profile_fingerprint()
        payload["quantization"] = "floor_float32_times_255_plus_half.v1"
    if binary:
        capability = qualified_authoring_derivative_binary_capability()
        payload.update(
            {
                "binary_capability_fingerprint": capability.fingerprint,
                "process_seconds": format(GENERATOR_PROCESS_SECONDS, ".1f"),
                "proxy": {
                    "audio": "aac_copy_or_absent",
                    "b_frames": 0,
                    "gop": VIDEO_PROXY_GOP,
                    "max_bits_per_second": VIDEO_PROXY_MAX_BITS_PER_SECOND,
                    "max_bytes": VIDEO_PROXY_MAX_BYTES,
                    "max_edge": VIDEO_PROXY_MAX_EDGE,
                    "output": "mp4/libx264/yuv420p",
                    "timing": "exact_source_rational_pts_dts_duration",
                },
                "thumbnail": {
                    "max_bytes": PNG_THUMBNAIL_MAX_BYTES,
                    "max_edge": THUMBNAIL_MAX_EDGE,
                    "stage": "rawvideo/rgb24/exact_source_frame",
                },
                "filmstrip": {
                    "max_bytes": FILMSTRIP_MAX_BYTES,
                    "max_tiles": FILMSTRIP_MAX_TILES,
                    "min_tiles": FILMSTRIP_MIN_TILES,
                    "profile_id": FILMSTRIP_PROFILE_ID,
                    "tile_height": FILMSTRIP_TILE_HEIGHT,
                },
                "audio_peaks": {
                    "decoded_sample_rate": AUDIO_PEAKS_SAMPLE_RATE,
                    "max_pcm_bytes": AUDIO_PEAKS_MAX_PCM_BYTES,
                    "profile_id": AUDIO_PEAKS_PROFILE_ID,
                },
                "audio_preview": {
                    "channels": "preserve_admitted_1_to_8",
                    "max_bytes": AUDIO_PREVIEW_MAX_BYTES,
                    "profile_id": AUDIO_PREVIEW_PROFILE_ID,
                    "sample_rate": AUDIO_PREVIEW_SAMPLE_RATE,
                },
            }
        )
    return _fingerprint(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii"))


def _closed_guard_failure(exc: BaseException) -> AuthoringDerivativeGeneratorError:
    code = getattr(exc, "code", "")
    if code in {"cancelled", "source_cancelled"}:
        return AuthoringDerivativeGeneratorError("cancelled")
    if code in {"timeout", "source_timeout", "preview_deadline", "process_timeout"}:
        return AuthoringDerivativeGeneratorError("timeout")
    if code in {"stale", "source_stale", "source_replaced", "receipt_released"}:
        return AuthoringDerivativeGeneratorError("stale")
    if code in {"authority_mismatch", "registry_mismatch", "generation_mismatch"}:
        return AuthoringDerivativeGeneratorError("authority_mismatch")
    if code in {"resource_limit", "generation_failed", "invalid_request", "unsupported", "busy"}:
        return AuthoringDerivativeGeneratorError(code)
    return AuthoringDerivativeGeneratorError("internal_failure")


def _check(
    deadline: float,
    cancellation: CancellationProbe | None,
    guard: Callable[[], None] | None,
) -> None:
    if type(deadline) is not float or not math.isfinite(deadline):
        raise AuthoringDerivativeGeneratorError("invalid_request")
    if time.monotonic() >= deadline:
        raise AuthoringDerivativeGeneratorError("timeout")
    if cancellation is not None:
        try:
            if cancellation.is_cancelled():
                raise AuthoringDerivativeGeneratorError("cancelled")
        except AuthoringDerivativeGeneratorError:
            raise
        except Exception:
            raise AuthoringDerivativeGeneratorError("internal_failure") from None
    if guard is not None:
        if not callable(guard):
            raise AuthoringDerivativeGeneratorError("invalid_request")
        try:
            guard()
        except AuthoringDerivativeGeneratorError:
            raise
        except Exception as exc:
            raise _closed_guard_failure(exc) from None


def _quantize_row(source: bytes, offset: int, width: int) -> bytearray:
    result = bytearray(width * 3)
    for index in range(width * 3):
        value = struct.unpack_from("<f", source, offset + index * 4)[0]
        if not math.isfinite(value) or value < 0.0 or value > 1.0:
            result.clear()
            raise AuthoringDerivativeGeneratorError("authority_mismatch")
        result[index] = math.floor(value * 255.0 + 0.5)
    return result


def _quantize_image(
    source: bytes,
    width: int,
    height: int,
    *,
    deadline: float,
    cancellation: CancellationProbe | None,
    guard: Callable[[], None] | None,
) -> bytearray:
    result = bytearray(width * height * 3)
    row_source_bytes = width * 3 * 4
    row_output_bytes = width * 3
    try:
        for row_index in range(height):
            _check(deadline, cancellation, guard)
            row = _quantize_row(source, row_index * row_source_bytes, width)
            start = row_index * row_output_bytes
            result[start : start + row_output_bytes] = row
            row.clear()
        return result
    except BaseException:
        result.clear()
        raise


def _thumbnail_dimensions(width: int, height: int) -> tuple[int, int]:
    longest = max(width, height)
    if longest <= THUMBNAIL_MAX_EDGE:
        return width, height
    if width >= height:
        return THUMBNAIL_MAX_EDGE, max(1, height * THUMBNAIL_MAX_EDGE // width)
    return max(1, width * THUMBNAIL_MAX_EDGE // height), THUMBNAIL_MAX_EDGE


def _resize_rgb8_nearest(
    source: bytes | bytearray,
    source_width: int,
    source_height: int,
    output_width: int,
    output_height: int,
    *,
    deadline: float,
    cancellation: CancellationProbe | None,
    guard: Callable[[], None] | None,
) -> bytearray:
    output = bytearray(output_width * output_height * 3)
    try:
        for output_y in range(output_height):
            _check(deadline, cancellation, guard)
            source_y = min(
                source_height - 1,
                ((2 * output_y + 1) * source_height) // (2 * output_height),
            )
            for output_x in range(output_width):
                source_x = min(
                    source_width - 1,
                    ((2 * output_x + 1) * source_width) // (2 * output_width),
                )
                source_at = (source_y * source_width + source_x) * 3
                output_at = (output_y * output_width + output_x) * 3
                output[output_at : output_at + 3] = source[source_at : source_at + 3]
        return output
    except BaseException:
        output.clear()
        raise


def _image_thumbnail_rgb(
    source: bytes,
    source_width: int,
    source_height: int,
    output_width: int,
    output_height: int,
    *,
    deadline: float,
    cancellation: CancellationProbe | None,
    guard: Callable[[], None] | None,
) -> bytearray:
    output = bytearray(output_width * output_height * 3)
    try:
        for output_y in range(output_height):
            _check(deadline, cancellation, guard)
            source_y = min(
                source_height - 1,
                ((2 * output_y + 1) * source_height) // (2 * output_height),
            )
            for output_x in range(output_width):
                source_x = min(
                    source_width - 1,
                    ((2 * output_x + 1) * source_width) // (2 * output_width),
                )
                source_at = (source_y * source_width + source_x) * 12
                output_at = (output_y * output_width + output_x) * 3
                for channel in range(3):
                    value = struct.unpack_from("<f", source, source_at + channel * 4)[0]
                    if not math.isfinite(value) or value < 0.0 or value > 1.0:
                        raise AuthoringDerivativeGeneratorError("authority_mismatch")
                    output[output_at + channel] = math.floor(value * 255.0 + 0.5)
        return output
    except BaseException:
        output.clear()
        raise


def _proxy_dimensions(width: int, height: int) -> tuple[int, int]:
    if min(width, height) < 2:
        raise AuthoringDerivativeGeneratorError("unsupported")
    longest = max(width, height)
    scale_denominator = max(longest, VIDEO_PROXY_MAX_EDGE)
    scaled_width = width * VIDEO_PROXY_MAX_EDGE // scale_denominator
    scaled_height = height * VIDEO_PROXY_MAX_EDGE // scale_denominator
    output_width = scaled_width - scaled_width % 2
    output_height = scaled_height - scaled_height % 2
    if output_width < 2 or output_height < 2:
        raise AuthoringDerivativeGeneratorError("unsupported")
    return output_width, output_height


class AuthoringDerivativeGenerator:
    """One fail-closed IMAGE/VIDEO derivative generator with no locator projection."""

    def __init__(
        self,
        *,
        ffmpeg_path: Path,
        ffprobe_path: Path,
        scratch_root: Path,
    ) -> None:
        if (
            not isinstance(ffmpeg_path, Path)
            or not isinstance(ffprobe_path, Path)
            or not isinstance(scratch_root, Path)
            or not ffmpeg_path.is_absolute()
            or not ffprobe_path.is_absolute()
            or not scratch_root.is_absolute()
        ):
            raise AuthoringDerivativeGeneratorError("unsupported")
        capability = qualified_ffmpeg_capability()
        try:
            with _pin_exact_executable(ffmpeg_path, capability.ffmpeg_sha256):
                pass
            with _pin_exact_executable(ffprobe_path, capability.ffprobe_sha256):
                pass
            self._scratch_root: Path | None = ensure_directory(scratch_root)
        except (AVMediaAdapterError, OSError, RuntimeError):
            raise AuthoringDerivativeGeneratorError("unsupported") from None
        self._ffmpeg_path: Path | None = ffmpeg_path
        self._ffprobe_path: Path | None = ffprobe_path
        self._runner = SubprocessMediaRunner()
        self._probe_adapter: QualifiedAVMediaAdapter | None = None

    @classmethod
    def for_images(cls) -> AuthoringDerivativeGenerator:
        result = object.__new__(cls)
        result._scratch_root = None
        result._ffmpeg_path = None
        result._ffprobe_path = None
        result._runner = SubprocessMediaRunner()
        result._probe_adapter = None
        return result

    @property
    def profile_fingerprint(self) -> str:
        return authoring_derivative_profile_fingerprint()

    def _work_guard(
        self,
        deadline: float,
        cancellation: CancellationProbe | None,
        guard: Callable[[], None] | None,
    ) -> Callable[[], None]:
        return lambda: _check(deadline, cancellation, guard)

    @staticmethod
    def _claim_image(source: OwnedImageSource, expected_source_fingerprint: str) -> bytes:
        if type(source) is not OwnedImageSource or not _valid_fingerprint(
            expected_source_fingerprint
        ):
            raise AuthoringDerivativeGeneratorError("invalid_request")
        if not source.current() or "sha256:" + source.fingerprint != expected_source_fingerprint:
            raise AuthoringDerivativeGeneratorError("stale")
        pixels = source.read_bytes()
        if len(pixels) != source.width * source.height * 12:
            raise AuthoringDerivativeGeneratorError("authority_mismatch")
        return pixels

    @staticmethod
    def _result(
        body: bytearray,
        *,
        profile_id: str,
        media_type: str,
        width: int,
        height: int,
        source_frame: int | None,
        audio_disposition: str,
        binary: bool,
    ) -> GeneratedDerivativeBody:
        return GeneratedDerivativeBody(
            body=body,
            profile_id=profile_id,
            media_type=media_type,
            width=width,
            height=height,
            byte_count=len(body),
            content_fingerprint=_fingerprint(body),
            generator_fingerprint=_profile_fingerprint(profile_id, binary=binary),
            source_frame=source_frame,
            audio_disposition=audio_disposition,
        )

    @staticmethod
    def _audio_peaks_result(body: bytearray) -> GeneratedAudioPeaksBody:
        return GeneratedAudioPeaksBody(
            body=body,
            profile_id=AUDIO_PEAKS_PROFILE_ID,
            media_type=AUDIO_PEAKS_MEDIA_TYPE,
            byte_count=len(body),
            content_fingerprint=_fingerprint(body),
            generator_fingerprint=_profile_fingerprint(AUDIO_PEAKS_PROFILE_ID, binary=True),
            source_frame=None,
            audio_disposition="present_bound",
        )

    @staticmethod
    def _audio_preview_result(body: bytearray) -> GeneratedAudioPreviewBody:
        return GeneratedAudioPreviewBody(
            body=body,
            profile_id=AUDIO_PREVIEW_PROFILE_ID,
            media_type=AUDIO_PREVIEW_MEDIA_TYPE,
            byte_count=len(body),
            content_fingerprint=_fingerprint(body),
            generator_fingerprint=_profile_fingerprint(AUDIO_PREVIEW_PROFILE_ID, binary=True),
            source_frame=None,
            audio_disposition="present_bound",
        )

    def generate_image_proxy(
        self,
        source: OwnedImageSource,
        *,
        expected_source_fingerprint: str,
        deadline: float,
        cancellation: CancellationProbe | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GeneratedDerivativeBody:
        _check(deadline, cancellation, guard)
        if not _GENERATOR_GATE.acquire(blocking=False):
            raise AuthoringDerivativeGeneratorError("busy")
        rgb = bytearray()
        body = bytearray()
        try:
            pixels = self._claim_image(source, expected_source_fingerprint)
            rgb = _quantize_image(
                pixels,
                source.width,
                source.height,
                deadline=deadline,
                cancellation=cancellation,
                guard=guard,
            )
            _check(deadline, cancellation, guard)
            if not source.current():
                raise AuthoringDerivativeGeneratorError("stale")
            body = encode_rgb8_png(
                rgb,
                width=source.width,
                height=source.height,
                maximum_bytes=PNG_IMAGE_MAX_BYTES,
                guard=self._work_guard(deadline, cancellation, guard),
            )
            probe = probe_rgb8_png(
                body,
                maximum_width=source.width,
                maximum_height=source.height,
                maximum_pixels=source.width * source.height,
                maximum_bytes=PNG_IMAGE_MAX_BYTES,
            )
            if probe.rgb_fingerprint != _fingerprint(rgb):
                raise AuthoringDerivativeGeneratorError("generation_failed")
            _check(deadline, cancellation, guard)
            if not source.current():
                raise AuthoringDerivativeGeneratorError("stale")
            result = self._result(
                body,
                profile_id=IMAGE_PROXY_PROFILE_ID,
                media_type=PNG_MEDIA_TYPE,
                width=source.width,
                height=source.height,
                source_frame=None,
                audio_disposition="not_applicable",
                binary=False,
            )
            body = bytearray()
            return result
        except RGB8PNGError as exc:
            raise _closed_guard_failure(exc) from None
        except AuthoringDerivativeGeneratorError:
            raise
        except (MemoryError, OverflowError, RuntimeError, struct.error) as exc:
            raise _closed_guard_failure(exc) from None
        finally:
            rgb.clear()
            body.clear()
            _GENERATOR_GATE.release()

    def generate_image_thumbnail(
        self,
        source: OwnedImageSource,
        *,
        expected_source_fingerprint: str,
        deadline: float,
        cancellation: CancellationProbe | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GeneratedDerivativeBody:
        _check(deadline, cancellation, guard)
        if not _GENERATOR_GATE.acquire(blocking=False):
            raise AuthoringDerivativeGeneratorError("busy")
        rgb = bytearray()
        body = bytearray()
        try:
            pixels = self._claim_image(source, expected_source_fingerprint)
            width, height = _thumbnail_dimensions(source.width, source.height)
            rgb = _image_thumbnail_rgb(
                pixels,
                source.width,
                source.height,
                width,
                height,
                deadline=deadline,
                cancellation=cancellation,
                guard=guard,
            )
            _check(deadline, cancellation, guard)
            if not source.current():
                raise AuthoringDerivativeGeneratorError("stale")
            body = encode_rgb8_png(
                rgb,
                width=width,
                height=height,
                maximum_bytes=PNG_THUMBNAIL_MAX_BYTES,
                guard=self._work_guard(deadline, cancellation, guard),
            )
            probe = probe_rgb8_png(
                body,
                maximum_width=THUMBNAIL_MAX_EDGE,
                maximum_height=THUMBNAIL_MAX_EDGE,
                maximum_pixels=THUMBNAIL_MAX_EDGE * THUMBNAIL_MAX_EDGE,
                maximum_bytes=PNG_THUMBNAIL_MAX_BYTES,
            )
            if probe.rgb_fingerprint != _fingerprint(rgb):
                raise AuthoringDerivativeGeneratorError("generation_failed")
            _check(deadline, cancellation, guard)
            if not source.current():
                raise AuthoringDerivativeGeneratorError("stale")
            result = self._result(
                body,
                profile_id=THUMBNAIL_PROFILE_ID,
                media_type=PNG_MEDIA_TYPE,
                width=width,
                height=height,
                source_frame=None,
                audio_disposition="not_applicable",
                binary=False,
            )
            body = bytearray()
            return result
        except RGB8PNGError as exc:
            raise _closed_guard_failure(exc) from None
        except AuthoringDerivativeGeneratorError:
            raise
        except (MemoryError, OverflowError, RuntimeError, struct.error) as exc:
            raise _closed_guard_failure(exc) from None
        finally:
            rgb.clear()
            body.clear()
            _GENERATOR_GATE.release()

    def _require_video_runtime(self) -> tuple[Path, Path, Path]:
        if self._ffmpeg_path is None or self._ffprobe_path is None or self._scratch_root is None:
            raise AuthoringDerivativeGeneratorError("unsupported")
        return self._ffmpeg_path, self._ffprobe_path, self._scratch_root

    def _remaining(self, deadline: float) -> Decimal:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AuthoringDerivativeGeneratorError("timeout")
        return Decimal(str(min(GENERATOR_PROCESS_SECONDS, remaining)))

    def _stage_video(
        self,
        source_path: Path,
        facts: AuthoringVideoFacts,
        expected_source_fingerprint: str,
        *,
        deadline: float,
        cancellation: CancellationProbe | None,
        guard: Callable[[], None] | None,
    ) -> OwnedOutputLease:
        _ffmpeg, _ffprobe, scratch = self._require_video_runtime()
        if (
            not isinstance(source_path, Path)
            or not source_path.is_absolute()
            or type(facts) is not AuthoringVideoFacts
            or not _valid_fingerprint(expected_source_fingerprint)
            or facts.content_fingerprint != expected_source_fingerprint
        ):
            raise AuthoringDerivativeGeneratorError("invalid_request")
        _check(deadline, cancellation, guard)
        lease = create_output_lease(scratch, suffix=".mp4")
        body = bytearray()
        try:
            body, observed = _read_regular_media_body(
                source_path,
                VIDEO_SOURCE_MAX_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            if (
                observed != expected_source_fingerprint
                or len(body) != facts.byte_length
                or len(body) > GENERATOR_TRANSIENT_MAX_BYTES
            ):
                raise AuthoringDerivativeGeneratorError("stale")
            _write_new_file(lease.path, bytes(body))
            _check(deadline, cancellation, guard)
            return lease
        except BaseException:
            lease.release()
            raise
        finally:
            body.clear()

    def _run_ffmpeg(
        self,
        invocation: MediaProcessInvocation,
        cancellation: CancellationProbe | None,
    ) -> bytes:
        ffmpeg, _ffprobe, _scratch = self._require_video_runtime()
        capability = qualified_ffmpeg_capability()
        try:
            with _pin_exact_executable(ffmpeg, capability.ffmpeg_sha256):
                capture = self._runner.run(invocation, cancellation=cancellation)
        except AVMediaAdapterError as exc:
            raise _closed_guard_failure(exc) from None
        status = cast(ProcessStatus, capture.status)
        if status is ProcessStatus.SUCCEEDED and capture.cleanup_succeeded:
            return capture.stdout
        if status in {ProcessStatus.CANCELLED, ProcessStatus.CANCELLATION_FAILED}:
            code = "cancelled"
        elif status is ProcessStatus.TIMED_OUT:
            code = "timeout"
        elif status is ProcessStatus.OUTPUT_LIMIT:
            code = "resource_limit"
        else:
            code = "generation_failed"
        raise AuthoringDerivativeGeneratorError(code)

    def _audio_peaks_invocation(
        self,
        source: OwnedOutputLease,
        expected_samples: int,
        deadline: float,
    ) -> MediaProcessInvocation:
        ffmpeg, _ffprobe, _scratch = self._require_video_runtime()
        codecs = ("h264", "aac", "pcm_s16le")
        argv = (
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-codec_whitelist",
            ",".join(codecs),
            "-i",
            str(source.path),
            "-map",
            "0:a:0",
            # IMPORTANT: AAC decode exposes encoder padding past the admitted effective sample
            # count; without this exact bound a valid source fails the one-sample coverage guard.
            "-t",
            format(Decimal(expected_samples) / Decimal(AUDIO_PEAKS_SAMPLE_RATE), "f"),
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_PEAKS_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-f",
            "s16le",
            "pipe:1",
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=str(ffmpeg),
            argv=argv,
            protocol_whitelist=("file",),
            format_whitelist=("mov",),
            codec_whitelist=codecs,
            timeout_seconds=self._remaining(deadline),
            max_stdout_bytes=AUDIO_PEAKS_MAX_PCM_BYTES,
            max_stderr_bytes=256 * 1024,
            output_leases=(),
            max_owned_output_bytes=1,
        )

    def generate_video_audio_peaks(
        self,
        source_path: Path,
        facts: AuthoringVideoFacts,
        *,
        expected_source_fingerprint: str,
        deadline: float,
        cancellation: CancellationProbe | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GeneratedAudioPeaksBody:
        """Decode only admitted bound audio and publish one closed peaks envelope."""

        _check(deadline, cancellation, guard)
        audio = facts.embedded_audio if type(facts) is AuthoringVideoFacts else None
        if (
            audio is None
            or audio.disposition != "present_bound"
            or type(audio.sample_rate) is not int
            or type(audio.sample_count) is not int
        ):
            raise AuthoringDerivativeGeneratorError("unsupported")
        expected_samples = audio.sample_count * AUDIO_PEAKS_SAMPLE_RATE // audio.sample_rate
        if not 1 <= expected_samples <= AUDIO_PEAKS_MAX_PCM_BYTES // 2:
            raise AuthoringDerivativeGeneratorError("resource_limit")
        if not _GENERATOR_GATE.acquire(blocking=False):
            raise AuthoringDerivativeGeneratorError("busy")
        staged: OwnedOutputLease | None = None
        pcm = bytearray()
        body = bytearray()
        try:
            staged = self._stage_video(
                source_path,
                facts,
                expected_source_fingerprint,
                deadline=deadline,
                cancellation=cancellation,
                guard=guard,
            )
            invocation = self._audio_peaks_invocation(staged, expected_samples, deadline)
            pcm = bytearray(self._run_ffmpeg(invocation, cancellation))
            # IMPORTANT: decoded coverage stays tied to admitted bound-audio facts. Accept only
            # the one-sample endpoint variance allowed by the pinned resampler; never pad silence.
            actual_samples = len(pcm) // 2
            if len(pcm) % 2 or abs(actual_samples - expected_samples) > 1:
                raise AuthoringDerivativeGeneratorError("generation_failed")
            body = encode_audio_peaks_envelope(
                pcm,
                guard=self._work_guard(deadline, cancellation, guard),
            )
            decoded = decode_audio_peaks_envelope(body)
            if abs(decoded.sample_count - expected_samples) > 1:
                raise AuthoringDerivativeGeneratorError("generation_failed")
            _check(deadline, cancellation, guard)
            result = self._audio_peaks_result(body)
            body = bytearray()
            return result
        except AudioPeaksError as exc:
            raise _closed_guard_failure(exc) from None
        except AuthoringDerivativeGeneratorError:
            raise
        except (AVMediaAdapterError, ArtifactStoreError, OSError, RuntimeError):
            raise AuthoringDerivativeGeneratorError("generation_failed") from None
        finally:
            cleanup_failed = False
            pcm.clear()
            body.clear()
            if staged is not None:
                try:
                    staged.release()
                except (MediaProcessError, OSError, RuntimeError):
                    cleanup_failed = True
            _GENERATOR_GATE.release()
            if cleanup_failed:
                raise AuthoringDerivativeGeneratorError("internal_failure") from None

    def _audio_preview_invocation(
        self,
        source: OwnedOutputLease,
        expected_samples: int,
        deadline: float,
    ) -> MediaProcessInvocation:
        ffmpeg, _ffprobe, _scratch = self._require_video_runtime()
        codecs = ("h264", "aac", "pcm_s16le")
        argv = (
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-codec_whitelist",
            ",".join(codecs),
            "-i",
            str(source.path),
            "-map",
            "0:a:0",
            "-t",
            format(Decimal(expected_samples) / Decimal(AUDIO_PREVIEW_SAMPLE_RATE), "f"),
            "-vn",
            "-sn",
            "-dn",
            "-ar",
            str(AUDIO_PREVIEW_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-f",
            "s16le",
            "pipe:1",
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=str(ffmpeg),
            argv=argv,
            protocol_whitelist=("file",),
            format_whitelist=("mov",),
            codec_whitelist=codecs,
            timeout_seconds=self._remaining(deadline),
            max_stdout_bytes=AUDIO_PREVIEW_MAX_BYTES - AUDIO_PREVIEW_WAV_HEADER_BYTES,
            max_stderr_bytes=256 * 1024,
            output_leases=(),
            max_owned_output_bytes=1,
        )

    def generate_video_audio_preview(
        self,
        source_path: Path,
        facts: AuthoringVideoFacts,
        *,
        expected_source_fingerprint: str,
        deadline: float,
        cancellation: CancellationProbe | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GeneratedAudioPreviewBody:
        """Decode one admitted embedded stream into a canonical bounded PCM WAV body."""

        _check(deadline, cancellation, guard)
        audio = facts.embedded_audio if type(facts) is AuthoringVideoFacts else None
        if (
            audio is None
            or audio.disposition != "present_bound"
            or type(audio.sample_rate) is not int
            or type(audio.sample_count) is not int
            or type(audio.channels) is not int
            or not 1 <= audio.channels <= 8
        ):
            raise AuthoringDerivativeGeneratorError("unsupported")
        expected_samples = audio.sample_count * AUDIO_PREVIEW_SAMPLE_RATE // audio.sample_rate
        pcm_bytes = expected_samples * audio.channels * 2
        if (
            not 1 <= expected_samples
            or pcm_bytes + AUDIO_PREVIEW_WAV_HEADER_BYTES > AUDIO_PREVIEW_MAX_BYTES
        ):
            raise AuthoringDerivativeGeneratorError("resource_limit")
        if not _GENERATOR_GATE.acquire(blocking=False):
            raise AuthoringDerivativeGeneratorError("busy")
        staged: OwnedOutputLease | None = None
        pcm = bytearray()
        body = bytearray()
        try:
            staged = self._stage_video(
                source_path,
                facts,
                expected_source_fingerprint,
                deadline=deadline,
                cancellation=cancellation,
                guard=guard,
            )
            invocation = self._audio_preview_invocation(staged, expected_samples, deadline)
            pcm = bytearray(self._run_ffmpeg(invocation, cancellation))
            # CRITICAL: the WAV header is browser authority for channels and allocation. It may
            # describe only the exact verified PCM byte count; never pad a short decode with
            # silence.
            if len(pcm) != pcm_bytes:
                raise AuthoringDerivativeGeneratorError("generation_failed")
            byte_rate = AUDIO_PREVIEW_SAMPLE_RATE * audio.channels * 2
            body = bytearray(
                struct.pack(
                    "<4sI4s4sIHHIIHH4sI",
                    b"RIFF",
                    36 + pcm_bytes,
                    b"WAVE",
                    b"fmt ",
                    16,
                    1,
                    audio.channels,
                    AUDIO_PREVIEW_SAMPLE_RATE,
                    byte_rate,
                    audio.channels * 2,
                    16,
                    b"data",
                    pcm_bytes,
                )
            )
            body.extend(pcm)
            _check(deadline, cancellation, guard)
            result = self._audio_preview_result(body)
            body = bytearray()
            return result
        except AuthoringDerivativeGeneratorError:
            raise
        except (AVMediaAdapterError, ArtifactStoreError, OSError, RuntimeError, struct.error):
            raise AuthoringDerivativeGeneratorError("generation_failed") from None
        finally:
            cleanup_failed = False
            pcm.clear()
            body.clear()
            if staged is not None:
                try:
                    staged.release()
                except (MediaProcessError, OSError, RuntimeError):
                    cleanup_failed = True
            _GENERATOR_GATE.release()
            if cleanup_failed:
                raise AuthoringDerivativeGeneratorError("internal_failure") from None

    def _thumbnail_invocation(
        self,
        source: OwnedOutputLease,
        output: OwnedOutputLease,
        facts: AuthoringVideoFacts,
        source_frame: int,
        deadline: float,
    ) -> MediaProcessInvocation:
        ffmpeg, _ffprobe, _scratch = self._require_video_runtime()
        filter_graph = (
            f"[0:v:0]trim=start_frame={source_frame}:end_frame={source_frame + 1},format=rgb24[v]"
        )
        codecs = ("h264", "rawvideo")
        argv = (
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-codec_whitelist",
            ",".join(codecs),
            "-i",
            str(source.path),
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-f",
            "rawvideo",
            "-n",
            str(output.path),
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=str(ffmpeg),
            argv=argv,
            protocol_whitelist=("file",),
            format_whitelist=("mov",),
            codec_whitelist=codecs,
            timeout_seconds=self._remaining(deadline),
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=256 * 1024,
            output_leases=(output,),
            max_owned_output_bytes=facts.width * facts.height * 3,
        )

    def generate_video_thumbnail(
        self,
        source_path: Path,
        facts: AuthoringVideoFacts,
        *,
        expected_source_fingerprint: str,
        source_frame: int,
        deadline: float,
        cancellation: CancellationProbe | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GeneratedDerivativeBody:
        _check(deadline, cancellation, guard)
        if (
            type(facts) is not AuthoringVideoFacts
            or type(source_frame) is not int
            or not 0 <= source_frame < facts.frame_count
        ):
            raise AuthoringDerivativeGeneratorError("invalid_request")
        if not _GENERATOR_GATE.acquire(blocking=False):
            raise AuthoringDerivativeGeneratorError("busy")
        staged: OwnedOutputLease | None = None
        raw_lease: OwnedOutputLease | None = None
        raw = bytearray()
        resized = bytearray()
        body = bytearray()
        try:
            _ffmpeg, _ffprobe, scratch = self._require_video_runtime()
            staged = self._stage_video(
                source_path,
                facts,
                expected_source_fingerprint,
                deadline=deadline,
                cancellation=cancellation,
                guard=guard,
            )
            raw_lease = create_output_lease(scratch, suffix=".rgb")
            invocation = self._thumbnail_invocation(
                staged, raw_lease, facts, source_frame, deadline
            )
            self._run_ffmpeg(invocation, cancellation)
            expected_raw_bytes = facts.width * facts.height * 3
            raw, _raw_fingerprint = _read_regular_media_body(
                raw_lease.path,
                expected_raw_bytes,
                deadline=deadline,
                cancellation=cancellation,
            )
            if len(raw) != expected_raw_bytes:
                raise AuthoringDerivativeGeneratorError("generation_failed")
            width, height = _thumbnail_dimensions(facts.width, facts.height)
            resized = _resize_rgb8_nearest(
                raw,
                facts.width,
                facts.height,
                width,
                height,
                deadline=deadline,
                cancellation=cancellation,
                guard=guard,
            )
            body = encode_rgb8_png(
                resized,
                width=width,
                height=height,
                maximum_bytes=PNG_THUMBNAIL_MAX_BYTES,
                guard=self._work_guard(deadline, cancellation, guard),
            )
            probe = probe_rgb8_png(
                body,
                maximum_width=THUMBNAIL_MAX_EDGE,
                maximum_height=THUMBNAIL_MAX_EDGE,
                maximum_pixels=THUMBNAIL_MAX_EDGE * THUMBNAIL_MAX_EDGE,
                maximum_bytes=PNG_THUMBNAIL_MAX_BYTES,
            )
            if probe.rgb_fingerprint != _fingerprint(resized):
                raise AuthoringDerivativeGeneratorError("generation_failed")
            _check(deadline, cancellation, guard)
            result = self._result(
                body,
                profile_id=THUMBNAIL_PROFILE_ID,
                media_type=PNG_MEDIA_TYPE,
                width=width,
                height=height,
                source_frame=source_frame,
                audio_disposition="absent",
                binary=True,
            )
            body = bytearray()
            return result
        except RGB8PNGError as exc:
            raise _closed_guard_failure(exc) from None
        except AuthoringDerivativeGeneratorError:
            raise
        except (AVMediaAdapterError, ArtifactStoreError, OSError, RuntimeError):
            raise AuthoringDerivativeGeneratorError("generation_failed") from None
        finally:
            cleanup_failed = False
            raw.clear()
            resized.clear()
            body.clear()
            for lease in (raw_lease, staged):
                if lease is not None:
                    try:
                        lease.release()
                    except (MediaProcessError, OSError, RuntimeError):
                        cleanup_failed = True
            _GENERATOR_GATE.release()
            if cleanup_failed:
                raise AuthoringDerivativeGeneratorError("internal_failure") from None

    def _filmstrip_invocation(
        self,
        source: OwnedOutputLease,
        output: OwnedOutputLease,
        facts: AuthoringVideoFacts,
        *,
        tile_count: int,
        deadline: float,
    ) -> MediaProcessInvocation:
        ffmpeg, _ffprobe, _scratch = self._require_video_runtime()
        last = facts.landmarks[-1]
        duration_ticks = last.pts + last.duration_ticks
        rate = Fraction(
            tile_count * facts.source_time_base.den,
            duration_ticks * facts.source_time_base.num,
        )
        filter_graph = (
            f"[0:v:0]fps=fps={rate.numerator}/{rate.denominator}:round=near,"
            f"scale=-2:{FILMSTRIP_TILE_HEIGHT}:flags=lanczos,"
            f"tile={tile_count}x1:nb_frames={tile_count}:padding=0:margin=0[v]"
        )
        codecs = ("h264", "mjpeg")
        argv = (
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-codec_whitelist",
            ",".join(codecs),
            "-i",
            str(source.path),
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-c:v",
            "mjpeg",
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "5",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-f",
            "image2",
            "-n",
            str(output.path),
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=str(ffmpeg),
            argv=argv,
            protocol_whitelist=("file",),
            format_whitelist=("mov",),
            codec_whitelist=codecs,
            timeout_seconds=self._remaining(deadline),
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=256 * 1024,
            output_leases=(output,),
            max_owned_output_bytes=FILMSTRIP_MAX_BYTES,
        )

    def generate_video_filmstrip(
        self,
        source_path: Path,
        facts: AuthoringVideoFacts,
        *,
        expected_source_fingerprint: str,
        deadline: float,
        cancellation: CancellationProbe | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GeneratedDerivativeBody:
        _check(deadline, cancellation, guard)
        if type(facts) is not AuthoringVideoFacts:
            raise AuthoringDerivativeGeneratorError("invalid_request")
        if not _GENERATOR_GATE.acquire(blocking=False):
            raise AuthoringDerivativeGeneratorError("busy")
        staged: OwnedOutputLease | None = None
        output: OwnedOutputLease | None = None
        body = bytearray()
        try:
            _ffmpeg, _ffprobe, scratch = self._require_video_runtime()
            last = facts.landmarks[-1]
            duration_ticks = last.pts + last.duration_ticks
            tile_count = filmstrip_tile_count(
                ticks=duration_ticks,
                time_base_num=facts.source_time_base.num,
                time_base_den=facts.source_time_base.den,
            )
            staged = self._stage_video(
                source_path,
                facts,
                expected_source_fingerprint,
                deadline=deadline,
                cancellation=cancellation,
                guard=guard,
            )
            output = create_output_lease(scratch, suffix=".jpg")
            invocation = self._filmstrip_invocation(
                staged,
                output,
                facts,
                tile_count=tile_count,
                deadline=deadline,
            )
            self._run_ffmpeg(invocation, cancellation)
            body, output_fingerprint = _read_regular_media_body(
                output.path,
                FILMSTRIP_MAX_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            if output_fingerprint == expected_source_fingerprint:
                raise AuthoringDerivativeGeneratorError("generation_failed")
            width, height = probe_filmstrip_jpeg(body, maximum_bytes=FILMSTRIP_MAX_BYTES)
            if (
                height != FILMSTRIP_TILE_HEIGHT
                or width > 16_384
                or width % tile_count != 0
                or abs((width // tile_count) * facts.height - facts.width * FILMSTRIP_TILE_HEIGHT)
                > 2 * facts.height
            ):
                raise AuthoringDerivativeGeneratorError("generation_failed")
            _check(deadline, cancellation, guard)
            result = self._result(
                body,
                profile_id=FILMSTRIP_PROFILE_ID,
                media_type=FILMSTRIP_MEDIA_TYPE,
                width=width,
                height=height,
                source_frame=None,
                audio_disposition="absent",
                binary=True,
            )
            body = bytearray()
            return result
        except FilmstripJpegError:
            raise AuthoringDerivativeGeneratorError("generation_failed") from None
        except AuthoringDerivativeGeneratorError:
            raise
        except (AVMediaAdapterError, ArtifactStoreError, OSError, RuntimeError):
            raise AuthoringDerivativeGeneratorError("generation_failed") from None
        finally:
            cleanup_failed = False
            body.clear()
            for lease in (output, staged):
                if lease is not None:
                    try:
                        lease.release()
                    except (MediaProcessError, OSError, RuntimeError):
                        cleanup_failed = True
            _GENERATOR_GATE.release()
            if cleanup_failed:
                raise AuthoringDerivativeGeneratorError("internal_failure") from None

    def _proxy_invocation(
        self,
        source: OwnedOutputLease,
        output: OwnedOutputLease,
        facts: AuthoringVideoFacts,
        *,
        width: int,
        height: int,
        include_audio: bool,
        deadline: float,
    ) -> MediaProcessInvocation:
        ffmpeg, _ffprobe, _scratch = self._require_video_runtime()
        if facts.pixel_aspect_ratio not in (None, AuthoringVideoRational(1, 1)):
            raise AuthoringDerivativeGeneratorError("unsupported")
        if facts.source_time_base.num != 1 or facts.source_time_base.den > 2_147_483_647:
            raise AuthoringDerivativeGeneratorError("unsupported")
        codecs = ("h264", "aac", "libx264")
        bitrate = _video_proxy_bitrate(facts, VIDEO_PROXY_MAX_BYTES)
        # IMPORTANT: the proxy displays coded raster pixels when source SAR is unspecified.
        # Set its own square-pixel output explicitly without relabeling the original source.
        filter_graph = f"[0:v:0]scale={width}:{height}:flags=lanczos,format=yuv420p,setsar=1[v]"
        argv: list[str] = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-codec_whitelist",
            ",".join(codecs),
            "-copyts",
            "-i",
            str(source.path),
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            VIDEO_PROXY_PRESET,
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-g",
            str(VIDEO_PROXY_GOP),
            "-b:v",
            str(bitrate),
            "-maxrate",
            str(bitrate),
            "-bufsize",
            str(2 * bitrate),
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            f"{facts.source_time_base.num}:{facts.source_time_base.den}",
            "-video_track_timescale",
            str(facts.source_time_base.den),
            # The count the profile fingerprint records; see `ENCODER_THREAD_RULE`.
            "-threads",
            str(encoder_thread_count()),
        ]
        if include_audio:
            argv.extend(("-map", "0:a:0", "-c:a", "copy"))
        else:
            argv.append("-an")
        argv.extend(
            (
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-sn",
                "-dn",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                "-n",
                str(output.path),
            )
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=str(ffmpeg),
            argv=tuple(argv),
            protocol_whitelist=("file",),
            format_whitelist=("mov",),
            codec_whitelist=codecs,
            timeout_seconds=self._remaining(deadline),
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=256 * 1024,
            output_leases=(output,),
            max_owned_output_bytes=VIDEO_PROXY_MAX_BYTES,
        )

    def _video_probe_adapter(self) -> QualifiedAVMediaAdapter:
        ffmpeg, ffprobe, scratch = self._require_video_runtime()
        if self._probe_adapter is None:
            self._probe_adapter = QualifiedAVMediaAdapter(
                ffmpeg_path=ffmpeg,
                ffprobe_path=ffprobe,
                scratch_root=scratch,
                clock_ms=lambda: max(1, int(time.time() * 1000)),
            )
        return self._probe_adapter

    @staticmethod
    def _validate_proxy_facts(
        source: AuthoringVideoFacts,
        output: AuthoringVideoFacts,
        *,
        width: int,
        height: int,
        include_audio: bool,
    ) -> None:
        if (
            (output.width, output.height, output.frame_count) != (width, height, source.frame_count)
            or output.pixel_format != "yuv420p"
            or output.pixel_aspect_ratio != AuthoringVideoRational(1, 1)
            or len(output.landmarks) != len(source.landmarks)
        ):
            raise AuthoringDerivativeGeneratorError("unsupported")
        source_tb = Fraction(source.source_time_base.num, source.source_time_base.den)
        output_tb = Fraction(output.source_time_base.num, output.source_time_base.den)
        for original, derived in zip(source.landmarks, output.landmarks, strict=True):
            if (
                original.frame_index != derived.frame_index
                or original.pts * source_tb != derived.pts * output_tb
                or original.dts * source_tb != derived.dts * output_tb
                or original.duration_ticks * source_tb != derived.duration_ticks * output_tb
            ):
                raise AuthoringDerivativeGeneratorError("unsupported")
        expected_audio = (
            source.embedded_audio.disposition
            if include_audio and source.embedded_audio.disposition == "present_bound"
            else "absent"
        )
        if output.embedded_audio.disposition != expected_audio:
            raise AuthoringDerivativeGeneratorError("unsupported")
        if expected_audio == "present_bound" and (
            output.embedded_audio.sample_rate != source.embedded_audio.sample_rate
            or output.embedded_audio.channels != source.embedded_audio.channels
            or output.embedded_audio.channel_layout != source.embedded_audio.channel_layout
            or output.embedded_audio.time_base != source.embedded_audio.time_base
            or output.embedded_audio.sample_count != source.embedded_audio.sample_count
        ):
            raise AuthoringDerivativeGeneratorError("unsupported")

    def generate_video_proxy(
        self,
        source_path: Path,
        facts: AuthoringVideoFacts,
        *,
        expected_source_fingerprint: str,
        include_embedded_audio: bool,
        deadline: float,
        cancellation: CancellationProbe | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GeneratedDerivativeBody:
        _check(deadline, cancellation, guard)
        if type(facts) is not AuthoringVideoFacts or type(include_embedded_audio) is not bool:
            raise AuthoringDerivativeGeneratorError("invalid_request")
        if not _GENERATOR_GATE.acquire(blocking=False):
            raise AuthoringDerivativeGeneratorError("busy")
        staged: OwnedOutputLease | None = None
        output: OwnedOutputLease | None = None
        body = bytearray()
        try:
            _ffmpeg, _ffprobe, scratch = self._require_video_runtime()
            width, height = _proxy_dimensions(facts.width, facts.height)
            staged = self._stage_video(
                source_path,
                facts,
                expected_source_fingerprint,
                deadline=deadline,
                cancellation=cancellation,
                guard=guard,
            )
            output = create_output_lease(scratch, suffix=".mp4")
            include_audio = (
                include_embedded_audio and facts.embedded_audio.disposition == "present_bound"
            )
            invocation = self._proxy_invocation(
                staged,
                output,
                facts,
                width=width,
                height=height,
                include_audio=include_audio,
                deadline=deadline,
            )
            self._run_ffmpeg(invocation, cancellation)
            output_facts = probe_authoring_video_facts(
                output.path,
                self._video_probe_adapter(),
                deadline,
                cancellation,
            )
            self._validate_proxy_facts(
                facts,
                output_facts,
                width=width,
                height=height,
                include_audio=include_audio,
            )
            body, output_fingerprint = _read_regular_media_body(
                output.path,
                VIDEO_PROXY_MAX_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            if output_fingerprint == expected_source_fingerprint:
                raise AuthoringDerivativeGeneratorError("generation_failed")
            _check(deadline, cancellation, guard)
            result = self._result(
                body,
                profile_id=VIDEO_PROXY_PROFILE_ID,
                media_type=VIDEO_PROXY_MEDIA_TYPE,
                width=width,
                height=height,
                source_frame=None,
                audio_disposition=("present_bound" if include_audio else "absent"),
                binary=True,
            )
            body = bytearray()
            return result
        except AuthoringDerivativeGeneratorError:
            raise
        except (AVMediaAdapterError, ArtifactStoreError, OSError, RuntimeError):
            raise AuthoringDerivativeGeneratorError("generation_failed") from None
        finally:
            cleanup_failed = False
            body.clear()
            for lease in (output, staged):
                if lease is not None:
                    try:
                        lease.release()
                    except (MediaProcessError, OSError, RuntimeError):
                        cleanup_failed = True
            _GENERATOR_GATE.release()
            if cleanup_failed:
                raise AuthoringDerivativeGeneratorError("internal_failure") from None


__all__ = [
    "AUDIO_PEAKS_MEDIA_TYPE",
    "AUDIO_PEAKS_PROFILE_ID",
    "DERIVATIVE_GENERATOR_ID",
    "FILMSTRIP_MAX_BYTES",
    "FILMSTRIP_MEDIA_TYPE",
    "FILMSTRIP_PROFILE_ID",
    "FILMSTRIP_TILE_HEIGHT",
    "GENERATOR_PROCESS_SECONDS",
    "GENERATOR_TRANSIENT_MAX_BYTES",
    "GENERATOR_WORK_SECONDS",
    "IMAGE_PROXY_PROFILE_ID",
    "THUMBNAIL_PROFILE_ID",
    "VIDEO_PROXY_GOP",
    "VIDEO_PROXY_MAX_BITS_PER_SECOND",
    "VIDEO_PROXY_MAX_BYTES",
    "VIDEO_PROXY_MEDIA_TYPE",
    "VIDEO_PROXY_PRESET",
    "VIDEO_PROXY_PROFILE_ID",
    "AuthoringDerivativeGenerator",
    "AuthoringDerivativeBinaryCapability",
    "AuthoringDerivativeGeneratorError",
    "GeneratedDerivativeBody",
    "GeneratedAudioPeaksBody",
    "authoring_derivative_profile_fingerprint",
    "filmstrip_tile_count",
    "qualified_authoring_derivative_binary_capability",
]
