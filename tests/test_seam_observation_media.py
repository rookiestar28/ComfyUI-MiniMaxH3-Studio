"""The seam decode adapter: narrowed capability, exact extents, and a real synthetic MP4."""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.adapters import seam_observation_media as media
from comfyui_h3_context.adapters.authoring_derivative_generator import (
    qualified_authoring_derivative_binary_capability,
)
from comfyui_h3_context.adapters.media_subprocess import ProcessCapture, ProcessStatus
from comfyui_h3_context.core.av_reconstruction import qualified_ffmpeg_capability


def test_capability_keeps_the_pinned_binary_and_narrows_the_operations() -> None:
    capability = media.seam_observation_binary_capability()
    derivative = qualified_authoring_derivative_binary_capability()
    assert capability.ffmpeg_sha256 == qualified_ffmpeg_capability().ffmpeg_sha256.lower()
    assert capability.ffmpeg_sha256 == derivative.ffmpeg_sha256
    assert set(capability.output_muxers) <= set(derivative.output_muxers)
    assert set(capability.output_encoders) <= set(derivative.output_encoders)
    assert capability.output_muxers == ("rawvideo", "s16le")
    assert capability.pixel_formats == ("gray", "rgb24")


@pytest.mark.parametrize(
    ("source", "width", "expected"),
    [((864, 480), 96, 54), ((1344, 768), 96, 54), ((768, 1344), 96, 168), ((864, 480), 32, 18)],
)
def test_scaled_height_is_even_and_keeps_the_aspect(
    source: tuple[int, int], width: int, expected: int
) -> None:
    assert media.scaled_height(source[0], source[1], width) == expected


class _FakeRunner:
    def __init__(self, stdout: bytes, status: ProcessStatus = ProcessStatus.SUCCEEDED) -> None:
        self.stdout = stdout
        self.status = status
        self.invocations: list[Any] = []

    def run(self, invocation: Any, cancellation: Any = None) -> ProcessCapture:
        self.invocations.append(invocation)
        return ProcessCapture(status=self.status, stdout=self.stdout)


@contextlib.contextmanager
def _no_pin(path: Path, expected: str, **_: Any) -> Iterator[None]:
    yield


def _decoder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runner: _FakeRunner) -> Any:
    monkeypatch.setattr(media, "pin_exact_executable", _no_pin)
    binary = tmp_path / "ffmpeg.exe"
    binary.write_bytes(b"x")
    decoder = media.SeamObservationDecoder(binary)
    decoder._runner = runner  # type: ignore[assignment]
    return decoder


def test_frame_decode_requires_the_exact_byte_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "segment.mp4"
    source.write_bytes(b"not decoded by the fake")
    size = 96 * 54
    exact = _decoder(monkeypatch, tmp_path, _FakeRunner(b"\x10" * size * 3))
    decoded = exact.luma_frames(source, source_width=864, source_height=480, frames=3)
    assert len(decoded.frames) == 3 and all(len(f) == size for f in decoded.frames)
    invocation = exact._runner.invocations[0]
    assert invocation.max_stdout_bytes >= size * 3
    assert "pipe:1" in invocation.argv and "scale=96:54:flags=area,format=gray" in invocation.argv

    short = _decoder(monkeypatch, tmp_path, _FakeRunner(b"\x10" * (size * 3 - 1)))
    with pytest.raises(media.SeamObservationMediaError, match="frame_count"):
        short.luma_frames(source, source_width=864, source_height=480, frames=3)
    overflow = _decoder(monkeypatch, tmp_path, _FakeRunner(b"", ProcessStatus.OUTPUT_LIMIT))
    with pytest.raises(media.SeamObservationMediaError, match="frame_count"):
        overflow.luma_frames(source, source_width=864, source_height=480, frames=3)


def test_audio_decode_reads_the_declared_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "segment.mp4"
    source.write_bytes(b"x")
    padded = _decoder(monkeypatch, tmp_path, _FakeRunner(b"\x01\x00" * 1_010))
    assert list(padded.mono_audio(source, samples=1_000)) == [1] * 1_000
    short = _decoder(monkeypatch, tmp_path, _FakeRunner(b"\x01\x00" * 999))
    with pytest.raises(media.SeamObservationMediaError, match="short_audio"):
        short.mono_audio(source, samples=1_000)


def test_an_unpinned_executable_is_refused(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg.exe"
    binary.write_bytes(b"not the pinned build")
    with pytest.raises(media.SeamObservationMediaError, match="unsupported"):
        media.SeamObservationDecoder(binary)


def test_real_synthetic_mp4_decodes_exact_frames_and_samples(tmp_path: Path) -> None:
    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    if not ffmpeg_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg = Path(ffmpeg_value).resolve(strict=True)
    source = tmp_path / "segment.mp4"
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=864x480:rate=24:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=32000:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-threads",
            "1",
            "-n",
            str(source),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    decoder = media.SeamObservationDecoder(ffmpeg)
    luma = decoder.luma_frames(source, source_width=864, source_height=480, frames=24)
    assert (luma.width, luma.height, len(luma.frames)) == (96, 54, 24)
    assert len({frame for frame in luma.frames}) > 1
    rgb = decoder.rgb_frames(source, source_width=864, source_height=480, frames=24)
    assert (rgb.width, rgb.height) == (32, 18) and len(rgb.frames[0]) == 32 * 18 * 3
    audio = decoder.mono_audio(source, samples=15_000)
    assert len(audio) == 15_000 and max(abs(x) for x in audio) > 1_000
    with pytest.raises(media.SeamObservationMediaError, match="frame_count"):
        decoder.luma_frames(source, source_width=864, source_height=480, frames=25)
