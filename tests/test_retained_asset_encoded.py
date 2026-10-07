"""Actual explicitly supplied qualified tools exercise retained bytes and fresh MP4 preview."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest
from test_m25_29_production_authoring_import import _registries_with_ready_output

from comfyui_h3_context.adapters.av_reconstruction_media import (
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.recovery_owner import RecoveryOwner, RecoveryOwnerPort
from comfyui_h3_context.adapters.retained_asset_service import RetainedAssetService
from comfyui_h3_context.core.retained_assets import RetainedAssetError


@pytest.mark.parametrize("frame_count,fps", ((24, 12), (362, 24), (512, 30)))
def test_native_encoded_copy_restart_current_probe_and_explicit_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame_count: int, fps: int
) -> None:
    ffmpeg_value, ffprobe_value = (
        os.environ.get(name)
        for name in (
            "H3_CONTEXT_AUTHORIZED_FFMPEG_PATH",
            "H3_CONTEXT_AUTHORIZED_FFPROBE_PATH",
        )
    )
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg, ffprobe = (
        Path(ffmpeg_value).resolve(strict=True),
        Path(ffprobe_value).resolve(strict=True),
    )
    original = tmp_path / "actual-primary.mp4"
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
            f"testsrc2=size=320x240:rate={fps}",
            "-frames:v",
            str(frame_count),
            "-c:v",
            "libx264",
            "-bf",
            "0",
            "-vf",
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-an",
            "-movflags",
            "+faststart",
            "-n",
            str(original),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    production, projection, _, _, _, artifact_store = _registries_with_ready_output(
        tmp_path / "origin",
        artifact_bodies=(original.read_bytes(),),
        frame_count=frame_count,
        width=320,
        height=240,
    )
    segment_id = projection.outputs[0].segment_id
    assert segment_id is not None
    claim = production.claim_authoring_output_batch(
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        expected_workspace_revision=projection.workspace_revision,
        expected_workspace_fingerprint=projection.workspace_fingerprint,
        pairs=((segment_id, projection.outputs[0].output_handle),),
    )[0]
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=tmp_path / "scratch",
        clock_ms=lambda: 1,
    )
    counted_invocations: list[tuple[str, ...]] = []
    original_run = adapter._runner.run

    def run(invocation: Any, **kwargs: Any) -> Any:
        if "-count_frames" in invocation.argv:
            argv = invocation.argv
            assert invocation.tool == "ffprobe" and invocation.protocol_whitelist == ("file",)
            assert argv[argv.index("-select_streams") + 1] == "v:0"
            assert argv[argv.index("-show_entries") + 1] == "stream=nb_read_frames"
            assert invocation.max_stdout_bytes <= 64 * 1024
            counted_invocations.append(tuple(argv))
        return original_run(invocation, **kwargs)

    monkeypatch.setattr(adapter._runner, "run", run)
    owner = RecoveryOwner("owner_" + "a" * 32, tmp_path / "private")
    # Owner qualification belongs to route tests; this case measures actual encoded media.
    port = cast(RecoveryOwnerPort, SimpleNamespace(resolve=lambda: owner))
    service = RetainedAssetService(
        owner_port=port,
        production=lambda: production,
        media_runtime=lambda: adapter,
        lease=nullcontext,
    )

    def dispatch(target: RetainedAssetService, action: dict[str, Any]) -> dict[str, Any]:
        return target.dispatch(action, deadline=time.monotonic() + 30.0)

    dispatch(service, {"intent": "set_enabled", "enabled": True, "expected_revision": 0})
    admitted = dispatch(
        service,
        {
            "intent": "retain",
            "expected_revision": 1,
            "workspace_handle": projection.workspace_handle,
            "workspace_id": projection.workspace_id,
            "expected_workspace_revision": projection.workspace_revision,
            "expected_workspace_fingerprint": projection.workspace_fingerprint,
            "segment_id": projection.outputs[0].segment_id,
            "output_handle": projection.outputs[0].output_handle,
        },
    )
    identifier = admitted["retained_id"]
    retained_path = (
        owner.private_root / "recovery-assets/v1" / owner.owner_id / (identifier + ".mp4")
    )
    assert retained_path.read_bytes() == original.read_bytes()
    before = dispatch(
        service, {"intent": "restore", "asset_id": identifier, "expected_revision": 2}
    )
    old_handle = before["restored"]["use_handle"]
    service.close()
    assert tuple(adapter._scratch_root.iterdir()) == ()

    def no_original_owner() -> NoReturn:
        pytest.fail("restart restore cannot consult the former Production owner")

    restarted = RetainedAssetService(
        owner_port=port,
        production=no_original_owner,
        media_runtime=lambda: adapter,
        lease=nullcontext,
    )
    try:
        with pytest.raises(RetainedAssetError, match="lease_invalid"):
            restarted.preview(old_handle, deadline=time.monotonic() + 30.0)
        after = dispatch(
            restarted, {"intent": "restore", "asset_id": identifier, "expected_revision": 2}
        )
        assert after["restored"]["frame_count"] == frame_count
        assert after["restored"]["use_handle"] != old_handle
        body, audio = restarted.preview(
            after["restored"]["use_handle"], deadline=time.monotonic() + 30.0
        )
        assert audio == "absent" and 1 <= len(body) <= 4 * 1024 * 1024
        assert len(counted_invocations) == 1
        preview = tmp_path / "actual-preview.mp4"
        preview.write_bytes(body)
        probe = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(preview),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        streams = json.loads(probe.stdout)["streams"]
        assert len(streams) == 1 and streams[0]["codec_name"] == "h264"
        assert max(streams[0]["width"], streams[0]["height"]) <= 320
        with pytest.raises(AVMediaAdapterError, match="preview_source_unsupported"):
            adapter.execute_authoring_preview(
                source_path=retained_path.resolve(),
                source_start_frame=0,
                frames=frame_count + 1,
                source_fps=fps,
                deadline=time.monotonic() + 30.0,
            )
        assert len(counted_invocations) == 2
        assert artifact_store.inspect(claim.receipt).status.value == "reusable"
        assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
        dispatch(restarted, {"intent": "release", "use_handle": after["restored"]["use_handle"]})
        assert tuple(adapter._scratch_root.iterdir()) == ()
        assert retained_path.read_bytes() == original.read_bytes()
    finally:
        restarted.close()
