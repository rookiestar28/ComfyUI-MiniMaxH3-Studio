from __future__ import annotations

import hashlib
import struct
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import comfyui_h3_context.adapters.authoring_derivative_generator as generator_module
from comfyui_h3_context.adapters.authoring_derivative_generator import (
    IMAGE_PROXY_PROFILE_ID,
    THUMBNAIL_PROFILE_ID,
    AuthoringDerivativeGenerator,
    AuthoringDerivativeGeneratorError,
    authoring_derivative_profile_fingerprint,
    qualified_authoring_derivative_binary_capability,
)
from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool, OwnedImageSource
from comfyui_h3_context.adapters.authoring_video_facts import (
    AUTHORING_VIDEO_FACTS_SCHEMA,
    AuthoringVideoFacts,
    AuthoringVideoLandmark,
    AuthoringVideoRational,
    EmbeddedAudioFacts,
)
from comfyui_h3_context.adapters.media_subprocess import (
    ProcessCapture,
    ProcessStatus,
    SubprocessMediaRunner,
    create_output_lease,
)


def _png_rgb(payload: bytearray) -> tuple[int, int, bytes]:
    width, height = struct.unpack(">II", payload[16:24])
    offset = 8
    compressed = b""
    while offset < len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        name = bytes(payload[offset + 4 : offset + 8])
        if name == b"IDAT":
            compressed += bytes(payload[offset + 8 : offset + 8 + length])
        offset += 12 + length
    raw = zlib.decompress(compressed)
    row_bytes = width * 3
    rgb = b"".join(
        raw[row * (row_bytes + 1) + 1 : (row + 1) * (row_bytes + 1)] for row in range(height)
    )
    return width, height, rgb


def _fingerprint(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _owned_image(
    width: int, height: int, values: list[float]
) -> tuple[ImageSourcePool, OwnedImageSource]:
    pixels = struct.pack(f"<{len(values)}f", *values)
    pool = ImageSourcePool()
    source = OwnedImageSource(pool, pixels, width, height, time.monotonic() + 30)
    with pool._lock:
        pool._sources[id(source)] = source
        pool._live_bytes = len(pixels)
    return pool, source


def _video_facts(
    payload: bytes,
    *,
    width: int = 2,
    height: int = 2,
    time_base: AuthoringVideoRational | None = None,
    landmarks: tuple[AuthoringVideoLandmark, ...] | None = None,
    audio: EmbeddedAudioFacts | None = None,
) -> AuthoringVideoFacts:
    rows = landmarks or (AuthoringVideoLandmark(0, 0, 0, 3_000),)
    return AuthoringVideoFacts(
        AUTHORING_VIDEO_FACTS_SCHEMA,
        _fingerprint(payload),
        len(payload),
        width,
        height,
        "yuv420p",
        AuthoringVideoRational(1, 1),
        "tv",
        "bt709",
        "bt709",
        "bt709",
        time_base or AuthoringVideoRational(1, 90_000),
        len(rows),
        rows,
        audio or EmbeddedAudioFacts("absent", None, None, None, None, None),
    )


def test_image_proxy_quantizes_rows_and_transfers_owned_result() -> None:
    pool, source = _owned_image(
        2,
        1,
        [0.0, 0.5, 1.0, 1 / 255, 127.49 / 255, 127.5 / 255],
    )
    generator = AuthoringDerivativeGenerator.for_images()
    guard_count = 0

    def guard() -> None:
        nonlocal guard_count
        guard_count += 1

    result = generator.generate_image_proxy(
        source,
        expected_source_fingerprint="sha256:" + source.fingerprint,
        deadline=time.monotonic() + 5.0,
        guard=guard,
    )

    assert result.profile_id == IMAGE_PROXY_PROFILE_ID
    assert result.media_type == "image/png"
    assert result.source_frame is None
    assert result.audio_disposition == "not_applicable"
    assert _png_rgb(result.body) == (2, 1, bytes((0, 128, 255, 1, 127, 128)))
    assert result.byte_count == len(result.body)
    assert result.content_fingerprint == _fingerprint(bytes(result.body))
    assert result.generator_fingerprint.startswith("sha256:")
    assert repr(result) == "<GeneratedDerivativeBody opaque>"
    assert guard_count >= 3

    owned = result.take()
    assert result.body == bytearray()
    assert result.byte_count == 0
    assert _png_rgb(owned) == (2, 1, bytes((0, 128, 255, 1, 127, 128)))
    with pytest.raises(AuthoringDerivativeGeneratorError, match="^internal_failure$"):
        result.take()
    result.clear()
    pool.close()


def test_generator_profile_identity_binds_exact_capability_and_is_stable() -> None:
    capability = qualified_authoring_derivative_binary_capability()
    generator = AuthoringDerivativeGenerator.for_images()

    assert capability.build_version == "2026-02-26-git-6695528af6"
    assert (
        capability.ffmpeg_sha256
        == "abf5e1652dfdd3f8d7cfbb4900a4b590b7e8d192876164b7b7e9efaebe26d67f"  # noqa: E501  # pragma: allowlist secret
    )
    assert (
        capability.ffprobe_sha256
        == "fb81e32ea05d77049291d9cffb0ec2677cfbe5e1ce2021bbcb9eb5abdc6ac576"  # noqa: E501  # pragma: allowlist secret
    )
    assert capability.operations == (
        "full_source_pts_video_proxy",
        "source_frame_rgb24_thumbnail",
        "whole_asset_tiled_jpeg_filmstrip",
        "whole_asset_mono_s16le_audio_peaks",
        "whole_asset_channel_preserving_s16le_audio_preview",
    )
    assert generator.profile_fingerprint == authoring_derivative_profile_fingerprint()
    assert generator.profile_fingerprint.startswith("sha256:")

    pool, source = _owned_image(1, 1, [0.0, 0.0, 0.0])
    result = generator.generate_image_proxy(
        source,
        expected_source_fingerprint="sha256:" + source.fingerprint,
        deadline=time.monotonic() + 5.0,
    )
    assert generator.profile_fingerprint != result.generator_fingerprint
    result.clear()
    pool.close()


def test_image_thumbnail_uses_exact_center_nearest_rule_without_upscale() -> None:
    values: list[float] = []
    for _row in range(2):
        for column in range(640):
            values.extend((column / 639, 0.0, 0.0))
    pool, source = _owned_image(640, 2, values)

    result = AuthoringDerivativeGenerator.for_images().generate_image_thumbnail(
        source,
        expected_source_fingerprint="sha256:" + source.fingerprint,
        deadline=time.monotonic() + 5.0,
    )

    width, height, rgb = _png_rgb(result.body)
    assert result.profile_id == THUMBNAIL_PROFILE_ID
    assert (width, height) == (320, 1)
    assert rgb[:6] == bytes((0, 0, 0, 1, 0, 0))
    assert rgb[-3:] == bytes((255, 0, 0))
    result.clear()
    pool.close()


class _RawFrameRunner:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.argv: tuple[str, ...] = ()

    def run(self, invocation, cancellation=None):  # type: ignore[no-untyped-def]
        assert cancellation is None
        self.argv = invocation.argv
        lease = invocation.output_leases[0]
        lease.path.write_bytes(self.body)
        return ProcessCapture(
            status=ProcessStatus.SUCCEEDED,
            owned_output_bytes=len(self.body),
            artifacts=(lease,),
        )


def test_video_thumbnail_uses_owned_rawvideo_frame_and_png_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_body = b"bounded-private-video"
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(source_body)
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    @contextmanager
    def pin(_path: Path, _fingerprint: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(generator_module, "_pin_exact_executable", pin)
    runner = _RawFrameRunner(bytes((1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)))
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )
    generator._runner = cast(SubprocessMediaRunner, runner)

    result = generator.generate_video_thumbnail(
        source,
        _video_facts(source_body),
        expected_source_fingerprint=_fingerprint(source_body),
        source_frame=0,
        deadline=time.monotonic() + 5.0,
    )

    assert _png_rgb(result.body) == (2, 2, runner.body)
    assert result.profile_id == THUMBNAIL_PROFILE_ID
    assert result.source_frame == 0
    assert any(
        "trim=start_frame=0:end_frame=1,format=rgb24" in argument for argument in runner.argv
    )
    assert runner.argv[runner.argv.index("-c:v") + 1] == "rawvideo"
    assert runner.argv[runner.argv.index("-f") + 1] == "rawvideo"
    assert not any((tmp_path / "scratch").glob("h3-media-*"))
    result.clear()


def test_generator_rejects_wrong_identity_busy_and_cancelled_without_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, source = _owned_image(1, 1, [0.0, 0.0, 0.0])
    generator = AuthoringDerivativeGenerator.for_images()
    with pytest.raises(AuthoringDerivativeGeneratorError, match="^stale$"):
        generator.generate_image_proxy(
            source,
            expected_source_fingerprint="sha256:" + "0" * 64,
            deadline=time.monotonic() + 5.0,
        )

    assert generator_module._GENERATOR_GATE.acquire(blocking=False)
    try:
        with pytest.raises(AuthoringDerivativeGeneratorError, match="^busy$"):
            generator.generate_image_proxy(
                source,
                expected_source_fingerprint="sha256:" + source.fingerprint,
                deadline=time.monotonic() + 5.0,
            )
    finally:
        generator_module._GENERATOR_GATE.release()

    class Cancelled:
        def is_cancelled(self) -> bool:
            return True

    with pytest.raises(AuthoringDerivativeGeneratorError, match="^cancelled$"):
        generator.generate_image_proxy(
            source,
            expected_source_fingerprint="sha256:" + source.fingerprint,
            deadline=time.monotonic() + 5.0,
            cancellation=Cancelled(),
        )
    pool.close()


def test_proxy_facts_require_exact_rational_timing_and_audio_policy() -> None:
    audio = EmbeddedAudioFacts(
        "present_bound",
        48_000,
        1,
        "mono",
        AuthoringVideoRational(1, 48_000),
        1_536,
    )
    source = _video_facts(
        b"source",
        width=320,
        height=240,
        landmarks=(
            AuthoringVideoLandmark(0, 0, 0, 3_000),
            AuthoringVideoLandmark(1, 3_000, 3_000, 6_000),
        ),
        audio=audio,
    )
    equivalent = _video_facts(
        b"proxy",
        width=320,
        height=240,
        time_base=AuthoringVideoRational(1, 45_000),
        landmarks=(
            AuthoringVideoLandmark(0, 0, 0, 1_500),
            AuthoringVideoLandmark(1, 1_500, 1_500, 3_000),
        ),
        audio=audio,
    )

    AuthoringDerivativeGenerator._validate_proxy_facts(
        source,
        equivalent,
        width=320,
        height=240,
        include_audio=True,
    )

    wrong_duration = _video_facts(
        b"proxy",
        width=320,
        height=240,
        time_base=AuthoringVideoRational(1, 45_000),
        landmarks=(
            AuthoringVideoLandmark(0, 0, 0, 1_499),
            AuthoringVideoLandmark(1, 1_500, 1_500, 3_000),
        ),
        audio=audio,
    )
    with pytest.raises(AuthoringDerivativeGeneratorError, match="^unsupported$"):
        AuthoringDerivativeGenerator._validate_proxy_facts(
            source,
            wrong_duration,
            width=320,
            height=240,
            include_audio=True,
        )

    no_audio = _video_facts(
        b"proxy",
        width=320,
        height=240,
        time_base=AuthoringVideoRational(1, 45_000),
        landmarks=(
            AuthoringVideoLandmark(0, 0, 0, 1_500),
            AuthoringVideoLandmark(1, 1_500, 1_500, 3_000),
        ),
    )
    with pytest.raises(AuthoringDerivativeGeneratorError, match="^unsupported$"):
        AuthoringDerivativeGenerator._validate_proxy_facts(
            source,
            no_audio,
            width=320,
            height=240,
            include_audio=True,
        )


@pytest.mark.parametrize(
    "aspect", [None, AuthoringVideoRational(1, 1), AuthoringVideoRational(4, 3)]
)
def test_proxy_invocation_preserves_pts_and_has_no_cadence_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aspect: AuthoringVideoRational | None
) -> None:
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    @contextmanager
    def pin(_path: Path, _fingerprint: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(generator_module, "_pin_exact_executable", pin)
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )
    scratch_root = generator._scratch_root
    assert scratch_root is not None
    source = create_output_lease(scratch_root, suffix=".mp4")
    output = create_output_lease(scratch_root, suffix=".mp4")
    try:
        facts = replace(_video_facts(b"source", width=320, height=240), pixel_aspect_ratio=aspect)
        if aspect == AuthoringVideoRational(4, 3):
            with pytest.raises(AuthoringDerivativeGeneratorError, match="^unsupported$"):
                generator._proxy_invocation(
                    source,
                    output,
                    facts,
                    width=320,
                    height=240,
                    include_audio=False,
                    deadline=time.monotonic() + 5,
                )
            return
        invocation = generator._proxy_invocation(
            source,
            output,
            facts,
            width=320,
            height=240,
            include_audio=False,
            deadline=time.monotonic() + 5,
        )
        assert "-copyts" in invocation.argv
        assert invocation.argv[invocation.argv.index("-fps_mode") + 1] == "passthrough"
        assert "-an" in invocation.argv
        filter_graph = invocation.argv[invocation.argv.index("-filter_complex") + 1]
        assert "setpts" not in filter_graph
        assert "fps=" not in filter_graph
        assert ",setsar=1" in filter_graph
    finally:
        output.release()
        source.release()


@pytest.mark.parametrize("aspect", [None, AuthoringVideoRational(4, 3)])
def test_proxy_output_must_declare_square_pixel_aspect(
    aspect: AuthoringVideoRational | None,
) -> None:
    source = _video_facts(b"source", width=320, height=240)
    output = replace(_video_facts(b"output", width=320, height=240), pixel_aspect_ratio=aspect)
    with pytest.raises(AuthoringDerivativeGeneratorError, match="^unsupported$"):
        AuthoringDerivativeGenerator._validate_proxy_facts(
            source, output, width=320, height=240, include_audio=False
        )
