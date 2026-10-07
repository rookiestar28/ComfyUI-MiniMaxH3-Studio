from __future__ import annotations

import hashlib
import os
import time
from abc import ABCMeta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from comfyui_h3_context.adapters import authoring_render_source as render_source
from comfyui_h3_context.adapters import av_reconstruction_media as av_media
from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    ComfyVideoInputTypeAuthority,
    PathBackedComfyVideoFromFileV1Factory,
    RuntimeComfySourceFactory,
    _PathBackedAuthoringVideoSource,
    claim_transferred_authoring_source,
)
from comfyui_h3_context.core.composition_contract import PublicCompositionSnapshot
from comfyui_h3_context.core.contracts import AssetRole, MediaKind
from comfyui_h3_context.core.registry import ReferenceAsset, build_reference_registry
from comfyui_h3_context.core.render_planner import RenderPlanV1, source_facts_fingerprint


@pytest.mark.parametrize("change", ["policy", "silent", "samples", "frames"])
def test_overlay_admission_keeps_complete_measured_asset_equality(
    tmp_path: Path, change: str
) -> None:
    from test_m25_render_job_leases import video_bound

    from comfyui_h3_context.core.composition_contract import (
        decode_public_snapshot,
        public_snapshot_fingerprint,
    )

    _path, receipt, bound = video_bound(tmp_path)
    try:
        assert bound._issued_snapshot is not None
        wire = bound._issued_snapshot.to_wire()
        tracks = cast(list[dict[str, object]], wire["tracks"])
        tracks[0]["kind"] = "video_overlay"
        tracks[0]["order"] = 1
        tracks.append(
            dict(
                track_id="empty-primary", kind="primary_video", order=0, enabled=True, locked=False
            )
        )
        assets = cast(list[dict[str, object]], wire["assets"])
        asset = assets[0]
        if change in {"policy", "silent"}:
            asset["embedded_audio"] = "excluded_overlay_policy" if change == "policy" else "absent"
            asset["source_sample_count"] = None
        elif change == "samples":
            asset["source_sample_count"] = cast(int, asset["source_sample_count"]) + 2000
        else:
            asset["source_frame_count"] = cast(int, asset["source_frame_count"]) + 1
        wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
        snapshot = decode_public_snapshot(wire)
        with pytest.raises(AuthoringSourceBindingError, match="source_facts_mismatch"):
            render_source.prepare_bound_render_plan(bound._history, snapshot, lambda: True)
        assert bound._history.sources[0].asset == bound._issued_snapshot.assets[0]
    finally:
        receipt.release()


def test_overlay_role_result_revalidates_released_and_same_stat_replaced_source(
    tmp_path: Path,
) -> None:
    from test_m25_render_job_leases import video_bound

    from comfyui_h3_context.core.composition_contract import (
        decode_public_snapshot,
        public_snapshot_fingerprint,
    )

    path, receipt, bound = video_bound(tmp_path)
    try:
        assert bound._issued_snapshot is not None
        wire = bound._issued_snapshot.to_wire()
        tracks = cast(list[dict[str, object]], wire["tracks"])
        tracks[0]["kind"] = "video_overlay"
        tracks[0]["order"] = 1
        tracks.append(
            dict(
                track_id="empty-primary", kind="primary_video", order=0, enabled=True, locked=False
            )
        )
        wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
        moved = decode_public_snapshot(wire)
        admitted = render_source.prepare_bound_render_plan(bound._history, moved, lambda: True)
        assert not admitted.plan.emits_audio_stream
        metadata = path.stat()
        path.write_bytes(b"x" * metadata.st_size)
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        with pytest.raises(AuthoringSourceBindingError, match="source_replaced"):
            admitted.confirm_currentness(deadline=time.monotonic() + 5)
        receipt.release()
        with pytest.raises(AuthoringSourceBindingError, match="source_stale"):
            render_source.prepare_bound_render_plan(bound._history, moved, lambda: True)
    finally:
        receipt.release()


VideoInput = ABCMeta(
    "VideoInput",
    (),
    {"__module__": "comfy_api.latest._input.video_types"},
)
VideoFromFile = type(
    "VideoFromFile",
    (VideoInput,),
    {"__module__": "comfy_api.latest._input_impl.video_types"},
)
VideoFromComponents = type(
    "VideoFromComponents",
    (VideoInput,),
    {"__module__": "comfy_api.latest._input_impl.video_types"},
)
TYPE_AUTHORITY = ComfyVideoInputTypeAuthority(
    video_input=VideoInput,
    video_from_file=VideoFromFile,
    video_from_components=VideoFromComponents,
)


def _file_video(value: str) -> object:
    video = VideoFromFile()
    object.__setattr__(video, "_VideoFromFile__file", value)
    return video


def _video_source(
    root: Path,
    body: bytes,
) -> tuple[
    Path,
    AuthoringSourceBindingReceipt,
    _PathBackedAuthoringVideoSource,
    render_source._AuthoringSourceVerification,
]:
    path = root / "source.bin"
    path.write_bytes(body)
    registry = build_reference_registry(
        (ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 1),)
    )
    receipt = PathBackedComfyVideoFromFileV1Factory(
        type_authority=TYPE_AUTHORITY,
        input_root_factory=lambda: root,
        max_source_bytes=1024 * 1024,
        duration_probe=lambda _source: 1000,
    ).capture(
        exact_registry=registry,
        generation=1,
        sources=(("video_1", MediaKind.VIDEO, _file_video(path.name)),),
    )
    source = cast(
        _PathBackedAuthoringVideoSource,
        claim_transferred_authoring_source(receipt, "video_1"),
    )
    expected = "sha256:" + hashlib.sha256(body).hexdigest()
    verification = render_source._mint_source_verification(source, expected)
    return path, receipt, source, verification


def test_video_rehash_rejects_changed_bytes_with_restored_stat_identity(tmp_path: Path) -> None:
    original = b"0123456789abcdef"
    path, _receipt, source, verification = _video_source(tmp_path, original)
    metadata = path.stat()
    verification.confirm(time.monotonic() + 5.0)

    path.write_bytes(b"x" * len(original))
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

    assert source.current()
    with pytest.raises(AuthoringSourceBindingError, match="source_replaced"):
        verification.confirm(time.monotonic() + 5.0)


def test_video_verification_checks_deadline_and_release_after_the_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, receipt, _source, verification = _video_source(tmp_path, b"bounded-video-body")

    with pytest.raises(AuthoringSourceBindingError, match="source_timeout"):
        verification.confirm(time.monotonic() - 1.0)
    with pytest.raises(AuthoringSourceBindingError, match="source_deadline_invalid"):
        verification.confirm(cast(float, object()))

    def timed_out_reader(
        _path: Path,
        _maximum_bytes: int,
        *,
        deadline: float,
    ) -> tuple[bytearray, str]:
        del deadline
        raise av_media.AVMediaAdapterError("preview_deadline")

    with monkeypatch.context() as patcher:
        patcher.setattr(av_media, "_read_regular_media_body", timed_out_reader)
        with pytest.raises(AuthoringSourceBindingError, match="source_timeout"):
            verification.confirm(time.monotonic() + 5.0)

    original = render_source._read_path_source_fingerprint

    def read_then_release(source: _PathBackedAuthoringVideoSource, deadline: float) -> str:
        fingerprint = original(source, deadline)
        receipt.release()
        return fingerprint

    monkeypatch.setattr(render_source, "_read_path_source_fingerprint", read_then_release)
    with pytest.raises(AuthoringSourceBindingError, match="source_stale"):
        verification.confirm(time.monotonic() + 5.0)


def test_immutable_image_claim_rehashes_and_uses_manifest_source_facts_formula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    registry = build_reference_registry(
        (ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
    )
    image = torch.full((1, 2, 3, 3), 0.25, dtype=torch.float32)
    receipt = RuntimeComfySourceFactory().capture(
        exact_registry=registry,
        generation=1,
        sources=(("image_1", MediaKind.IMAGE, image),),
    )
    claim = render_source.claim_render_source(receipt, "image_1")
    image.fill_(0.75)

    claim.verify_currentness(time.monotonic() + 5.0)
    assert claim.source_facts_fingerprint == source_facts_fingerprint(
        asset_id="image_1",
        source_id="image_1",
        origin=render_source.SourceOrigin.RUNTIME_IMAGE.value,
        width=claim.width,
        height=claim.height,
        size_bytes=claim.byte_count,
        duration_milliseconds=claim.duration_milliseconds,
        source_profile_fingerprint=claim.source_profile_fingerprint,
        color_facts_fingerprint=claim.color_facts_fingerprint,
    )

    confirmation = object()
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        render_source,
        "revalidate_render_plan_currentness",
        lambda *, plan, confirmation: calls.append((plan, confirmation)),
    )
    plan = cast(RenderPlanV1, SimpleNamespace(source_currentness_claim=confirmation))
    history = render_source.PreparedAuthoringHistory(
        snapshot=cast(PublicCompositionSnapshot, object()),
        generation=1,
        sources=(claim,),
        fonts=load_packaged_font_manifest(),
    )
    bound = render_source.BoundAuthoringRenderPlan(
        plan=plan,
        _history=history,
        _workspace_is_current=lambda: True,
    )

    assert bound.confirm_currentness(deadline=time.monotonic() + 5.0) is confirmation
    assert calls == [(plan, confirmation)]
    with pytest.raises(AuthoringSourceBindingError, match="source_timeout"):
        bound.confirm_currentness(deadline=time.monotonic() - 1.0)
