"""Decode a generated segment into the small buffers the seam metrics read.

The decode runs the exact pinned FFmpeg build, with a capability narrowed from the derivative
generator's: the same executable digests and the same ``pipe`` output route, limited to raw gray
and rgb24 frames and mono 16 kHz PCM. Every decode's standard output is bounded by the exact
number of bytes the caller's frame or sample count implies, and a short or long read is refused,
so a truncated decode can never be measured as a shorter segment.

This adapter is used by the continuation route study harness. It is not on any product path.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from ..core.av_reconstruction import qualified_ffmpeg_capability
from .authoring_derivative_generator import qualified_authoring_derivative_binary_capability
from .executable_admission import ExecutableAdmissionError, pin_exact_executable
from .media_subprocess import (
    MediaProcessInvocation,
    ProcessStatus,
    SubprocessMediaRunner,
)

#: Seam metric decode geometry: luma at 96 px wide, colour statistics at 32 px wide.
LUMA_WIDTH: Final = 96
RGB_WIDTH: Final = 32
AUDIO_RATE: Final = 16_000
_INPUT_CODECS: Final = ("h264", "aac")
_TIMEOUT_SECONDS: Final = Decimal(300)
_MAX_STDERR: Final = 256 * 1024
#: The largest decode the study needs: 362 frames of rgb24 at 96 px stays far below this.
_MAX_DECODE_BYTES: Final = 64 * 1024 * 1024


class SeamObservationMediaError(RuntimeError):
    """A closed decode failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SeamObservationBinaryCapability:
    """The derivative generator's exact binaries, narrowed to the seam decode operations."""

    ffmpeg_sha256: str
    protocols: tuple[str, ...]
    input_demuxers: tuple[str, ...]
    input_codecs: tuple[str, ...]
    output_muxers: tuple[str, ...]
    output_encoders: tuple[str, ...]
    pixel_formats: tuple[str, ...]
    filters: tuple[str, ...]


def seam_observation_binary_capability() -> SeamObservationBinaryCapability:
    base = qualified_authoring_derivative_binary_capability()
    _require(base.ffmpeg_sha256 == qualified_ffmpeg_capability().ffmpeg_sha256.lower(), "pin")
    _require({"pipe", "file"} <= set(base.protocols), "capability")
    _require({"rawvideo", "s16le"} <= set(base.output_muxers), "capability")
    _require({"rawvideo", "pcm_s16le"} <= set(base.output_encoders), "capability")
    return SeamObservationBinaryCapability(
        ffmpeg_sha256=base.ffmpeg_sha256,
        protocols=("file", "pipe"),
        input_demuxers=base.input_demuxers,
        input_codecs=_INPUT_CODECS,
        output_muxers=("rawvideo", "s16le"),
        output_encoders=("pcm_s16le", "rawvideo"),
        # gray is the one member the derivative generator does not already name; rgb24 is shared.
        pixel_formats=("gray", "rgb24"),
        filters=("format", "scale"),
    )


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SeamObservationMediaError(code)


def scaled_height(source_width: int, source_height: int, width: int) -> int:
    """The even output height that keeps the source aspect at ``width``."""

    _require(source_width > 0 and source_height > 0 and width > 0, "geometry")
    height = 2 * round(width * source_height / (2 * source_width))
    return max(2, height)


@dataclass(frozen=True, slots=True)
class DecodedFrames:
    width: int
    height: int
    frames: tuple[bytes, ...]


class SeamObservationDecoder:
    """Decode exact frame and sample counts from one MP4 through the pinned FFmpeg."""

    def __init__(self, ffmpeg_path: Path) -> None:
        _require(isinstance(ffmpeg_path, Path) and ffmpeg_path.is_absolute(), "unsupported")
        self._capability = seam_observation_binary_capability()
        try:
            with pin_exact_executable(ffmpeg_path, self._capability.ffmpeg_sha256):
                pass
        except (ExecutableAdmissionError, OSError):
            raise SeamObservationMediaError("unsupported") from None
        self._ffmpeg = ffmpeg_path
        self._runner = SubprocessMediaRunner()

    def luma_frames(
        self, source: Path, *, source_width: int, source_height: int, frames: int
    ) -> DecodedFrames:
        height = scaled_height(source_width, source_height, LUMA_WIDTH)
        return self._frames(source, LUMA_WIDTH, height, "gray", 1, frames)

    def rgb_frames(
        self, source: Path, *, source_width: int, source_height: int, frames: int
    ) -> DecodedFrames:
        height = scaled_height(source_width, source_height, RGB_WIDTH)
        return self._frames(source, RGB_WIDTH, height, "rgb24", 3, frames)

    def mono_audio(self, source: Path, *, samples: int) -> array[int]:
        """``samples`` mono 16 kHz signed 16-bit samples from the source's first audio stream."""

        _require(0 < samples and samples * 2 <= _MAX_DECODE_BYTES, "extent")
        argv = (
            *self._input(source),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        )
        body = self._run(argv, samples * 2, codecs=(*_INPUT_CODECS, "pcm_s16le"), formats=("mov",))
        # The decoder may append encoder padding past the declared count; read the exact prefix.
        _require(len(body) >= samples * 2, "short_audio")
        decoded = array("h")
        decoded.frombytes(body[: samples * 2])
        return decoded

    def _frames(
        self, source: Path, width: int, height: int, pixel_format: str, channels: int, frames: int
    ) -> DecodedFrames:
        frame_bytes = width * height * channels
        _require(0 < frames and frames * frame_bytes <= _MAX_DECODE_BYTES, "extent")
        argv = (
            *self._input(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"scale={width}:{height}:flags=area,format={pixel_format}",
            "-c:v",
            "rawvideo",
            "-f",
            "rawvideo",
            "pipe:1",
        )
        body = self._run(
            argv, frames * frame_bytes, codecs=(*_INPUT_CODECS, "rawvideo"), formats=("mov",)
        )
        # IMPORTANT: an exact length, not a lower bound. A decode that drops or adds frames would
        # otherwise shift every seam index and still produce plausible numbers.
        _require(len(body) == frames * frame_bytes, "frame_count")
        return DecodedFrames(
            width=width,
            height=height,
            frames=tuple(
                body[index * frame_bytes : (index + 1) * frame_bytes] for index in range(frames)
            ),
        )

    def _input(self, source: Path) -> tuple[str, ...]:
        _require(isinstance(source, Path) and source.is_absolute() and source.is_file(), "source")
        return (
            str(self._ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-i",
            str(source),
        )

    def _run(
        self,
        argv: tuple[str, ...],
        expected_bytes: int,
        *,
        codecs: tuple[str, ...],
        formats: tuple[str, ...],
    ) -> bytes:
        invocation = MediaProcessInvocation(
            tool="ffmpeg",
            executable=str(self._ffmpeg),
            argv=argv,
            protocol_whitelist=("file",),
            format_whitelist=formats,
            codec_whitelist=codecs,
            timeout_seconds=_TIMEOUT_SECONDS,
            # Audio may carry a few padding samples beyond the declared count; one second of
            # headroom bounds that without admitting an unbounded read.
            max_stdout_bytes=expected_bytes + 2 * AUDIO_RATE,
            max_stderr_bytes=_MAX_STDERR,
        )
        try:
            with pin_exact_executable(self._ffmpeg, self._capability.ffmpeg_sha256):
                capture = self._runner.run(invocation)
        except (ExecutableAdmissionError, OSError):
            raise SeamObservationMediaError("unsupported") from None
        if capture.status is ProcessStatus.OUTPUT_LIMIT:
            raise SeamObservationMediaError("frame_count")
        if capture.status is not ProcessStatus.SUCCEEDED or not capture.cleanup_succeeded:
            raise SeamObservationMediaError("decode_failed")
        return capture.stdout
