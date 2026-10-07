from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_m25_composition_contract import fixture_snapshot
from test_m25_media_derivative_contract import create_wire
from test_m25_media_derivative_generator import _fingerprint, _video_facts

import comfyui_h3_context.adapters.authoring_derivative_generator as generator_module
from comfyui_h3_context.adapters.authoring_derivative_generator import (
    FILMSTRIP_MAX_BYTES,
    FILMSTRIP_MEDIA_TYPE,
    FILMSTRIP_PROFILE_ID,
    FILMSTRIP_TILE_HEIGHT,
    AuthoringDerivativeGenerator,
    AuthoringDerivativeGeneratorError,
    filmstrip_tile_count,
    qualified_authoring_derivative_binary_capability,
)
from comfyui_h3_context.adapters.authoring_derivative_jpeg import (
    FilmstripJpegError,
    probe_filmstrip_jpeg,
)
from comfyui_h3_context.adapters.authoring_video_facts import (
    AuthoringVideoLandmark,
    AuthoringVideoRational,
)
from comfyui_h3_context.adapters.media_subprocess import (
    ProcessCapture,
    ProcessStatus,
    SubprocessMediaRunner,
)
from comfyui_h3_context.core.authoring_media import (
    CreateLeaseRequest,
    MediaGeometry,
    MediaLeaseError,
    decode_lease_request,
    derivative_byte_limit,
    derivative_media_type,
    public_asset_manifest_fingerprint,
    validate_lease_snapshot,
)
from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
)


def _jpeg(width: int, height: int, *, marker: int = 0xC0) -> bytearray:
    # Minimal marker stream for the bounded structural probe. Browser decode is an independent
    # admission step; the probe deliberately does not become another JPEG decoder.
    return bytearray(
        b"\xff\xd8"
        + b"\xff\xe0\x00\x02"
        + bytes((0xFF, marker, 0x00, 0x11, 0x08))
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        + b"\xff\xda\x00\x0c\x03\x01\x00\x02\x00\x03\x00\x00\x3f\x00"
        + b"\x00"
        + b"\xff\xd9"
    )


@pytest.mark.parametrize(
    ("ticks", "num", "den", "expected"),
    [
        (1, 1, 1000, 4),
        (2_001, 1, 1000, 5),
        (5, 1, 1, 10),
        (10_000, 1, 1000, 20),
        (100, 1, 1, 64),
    ],
)
def test_tile_count_is_two_per_second_ceil_clamped_to_profile(
    ticks: int, num: int, den: int, expected: int
) -> None:
    assert filmstrip_tile_count(ticks=ticks, time_base_num=num, time_base_den=den) == expected


def test_baseline_jpeg_probe_is_bounded_and_rejects_clipped_or_progressive_bodies() -> None:
    body = _jpeg(48 * 7, FILMSTRIP_TILE_HEIGHT)
    assert probe_filmstrip_jpeg(body, maximum_bytes=FILMSTRIP_MAX_BYTES) == (48 * 7, 48)

    for candidate in (
        body[:-1],
        _jpeg(48 * 7, 48, marker=0xC2),
        bytearray(b"not-a-jpeg"),
        body + bytearray(FILMSTRIP_MAX_BYTES),
    ):
        with pytest.raises(FilmstripJpegError):
            probe_filmstrip_jpeg(candidate, maximum_bytes=FILMSTRIP_MAX_BYTES)


def _filmstrip_request() -> tuple[CreateLeaseRequest, PublicCompositionSnapshot]:
    snapshot = decode_public_snapshot(fixture_snapshot())
    video = next(asset for asset in snapshot.assets if asset.kind == "video")
    video = replace(video, source_frame_count=len(video.landmarks))
    snapshot = replace(
        snapshot,
        assets=tuple(
            video if asset.asset_id == video.asset_id else asset for asset in snapshot.assets
        ),
        clips=(),
    )
    wire = create_wire()
    wire.update(
        workspaceHandle=snapshot.workspace_handle,
        workspaceRevision=snapshot.workspace_revision,
        timelineRevision=snapshot.timeline_revision,
        publicFingerprint=snapshot.public_fingerprint,
        manifestFingerprint=public_asset_manifest_fingerprint(snapshot),
        scope="asset",
        clipId=None,
        assetId=video.asset_id,
        derivativeKind="filmstrip",
        sourceStartFrame=0,
        sourceEndFrame=video.source_frame_count,
    )
    request = decode_lease_request(json.dumps(wire).encode())
    assert isinstance(request, CreateLeaseRequest)
    return request, snapshot


def test_filmstrip_contract_is_jpeg_video_asset_scope_and_whole_asset_only() -> None:
    request, snapshot = _filmstrip_request()
    asset = validate_lease_snapshot(request, snapshot)
    assert asset.kind == "video"
    assert derivative_media_type("filmstrip") == FILMSTRIP_MEDIA_TYPE
    assert derivative_byte_limit("filmstrip") == FILMSTRIP_MAX_BYTES

    image = next(item for item in snapshot.assets if item.kind == "image")
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_lease_snapshot(
            replace(request, asset_id=image.asset_id, source_end_frame=1),
            snapshot,
        )
    clip_snapshot = decode_public_snapshot(fixture_snapshot())
    clip = next(item for item in clip_snapshot.clips if item.asset_id == request.asset_id)
    clip_request = replace(
        request,
        scope="clip",
        clip_id=clip.clip_id,
        manifest_fingerprint=public_asset_manifest_fingerprint(clip_snapshot),
        source_end_frame=72,
    )
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_lease_snapshot(clip_request, clip_snapshot)
    with pytest.raises(MediaLeaseError, match="authority_mismatch"):
        validate_lease_snapshot(replace(request, source_end_frame=1), snapshot)


def test_filmstrip_geometry_and_binary_capability_are_explicit() -> None:
    capability = qualified_authoring_derivative_binary_capability()
    assert capability.output_muxers == ("mp4", "rawvideo", "image2", "s16le")
    assert capability.output_encoders == ("libx264", "rawvideo", "mjpeg", "pcm_s16le")
    assert capability.filters == ("format", "fps", "scale", "setsar", "tile", "trim")
    assert "whole_asset_tiled_jpeg_filmstrip" in capability.operations

    # Filmstrip is visual geometry, but a sprite wider than the common geometry bound is refused.
    geometry = MediaGeometry(1920, 1080, 16_384, FILMSTRIP_TILE_HEIGHT)
    assert geometry.derivative_height == 48


class _JpegRunner:
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


def test_generator_uses_one_bounded_tiled_jpeg_and_cleans_every_lease(
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
    landmarks = tuple(AuthoringVideoLandmark(index, index, index, 1) for index in range(5))
    facts = _video_facts(
        source_body,
        time_base=AuthoringVideoRational(1, 1),
        landmarks=landmarks,
    )
    runner = _JpegRunner(bytes(_jpeg(48 * 10, 48)))
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )
    generator._runner = cast(SubprocessMediaRunner, runner)

    result = generator.generate_video_filmstrip(
        source,
        facts,
        expected_source_fingerprint=_fingerprint(source_body),
        deadline=time.monotonic() + 5.0,
    )

    assert result.profile_id == FILMSTRIP_PROFILE_ID
    assert result.media_type == FILMSTRIP_MEDIA_TYPE
    assert (result.width, result.height, result.source_frame) == (480, 48, None)
    filter_graph = runner.argv[runner.argv.index("-filter_complex") + 1]
    assert "fps=fps=2/1:round=near" in filter_graph
    assert "scale=-2:48:flags=lanczos" in filter_graph
    assert "tile=10x1:nb_frames=10" in filter_graph
    assert runner.argv[runner.argv.index("-c:v") + 1] == "mjpeg"
    assert runner.argv[runner.argv.index("-f") + 1] == "image2"
    assert not any((tmp_path / "scratch").glob("h3-media-*"))
    result.clear()

    runner.body = bytes(_jpeg(479, 48))
    with pytest.raises(AuthoringDerivativeGeneratorError, match="generation_failed"):
        generator.generate_video_filmstrip(
            source,
            facts,
            expected_source_fingerprint=_fingerprint(source_body),
            deadline=time.monotonic() + 5.0,
        )
    assert not any((tmp_path / "scratch").glob("h3-media-*"))
