from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_m25_29_production_authoring_import import (
    _authoring_action,
    _import_request,
    _insert_asset_transaction,
    _qualified_test_adapter,
    _registries_with_ready_output,
)
from test_m25_composition_contract import fixture_snapshot

from comfyui_h3_context.adapters.authoring_derivative_generator import (
    AuthoringDerivativeGenerator,
    GeneratedDerivativeBody,
)
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.adapters.authoring_source_binding import AuthoringSourceBindingError
from comfyui_h3_context.adapters.comfyui_production_workspace import PRODUCTION_ACTION_SCHEMA
from comfyui_h3_context.core.authoring_media import (
    REQUEST_SCHEMA,
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    MediaLeaseError,
    decode_lease_request,
    public_asset_manifest_fingerprint,
    public_runtime_asset_wire,
)
from comfyui_h3_context.core.composition_contract import (
    ENGINE_PROFILE_ID,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _imported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frames: int,
    native: tuple[Any, bytes] | None = None,
) -> tuple[Any, ...]:
    return _imported_with_owner(tmp_path, monkeypatch, frames, native)[2:]


def _imported_with_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frames: int,
    native: tuple[Any, bytes] | None = None,
) -> tuple[Any, ...]:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path,
        frame_count=frames,
        artifact_bodies=None if native is None else (native[1],),
        width=320 if native is None else 64,
        height=240 if native is None else 64,
    )
    adapter = (
        _qualified_test_adapter(tmp_path, monkeypatch, frame_count=frames)
        if native is None
        else native[0]
    )
    request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 20,
    )
    handle = request.authoring_workspace_handle
    asset_id = response.receipt.rows[0].asset_id
    initial = authoring.dispatch(
        _authoring_action("read.before", "read_timeline_history", workspace_handle=handle)
    )
    assert initial.body is not None
    # IMPORTANT: the shared import fixture owns V2 history. Reading the retired V1 `snapshot`
    # shape here bypasses the accepted authoring contract and makes every derivative test fail
    # before it reaches the source-lifetime behavior it is meant to prove.
    authoring_state = cast(dict[str, Any], initial.body["authoring"])
    primary = next(track for track in authoring_state["tracks"] if track["kind"] == "primary_video")
    action = _authoring_action("generated", "apply_timeline_transaction")
    action["payload"] = _insert_asset_transaction(
        authoring_state=authoring_state,
        asset_id=asset_id,
        track_id=primary["track_id"],
        request_id="generated",
        duration_frames=1,
    )
    inserted = authoring.dispatch(action)
    assert inserted.status == 200 and inserted.body is not None
    render_snapshot = inserted.body["render_snapshot"]
    assert render_snapshot is not None
    snapshot = decode_public_snapshot(render_snapshot)
    entry = authoring._entries[handle]
    assert entry.timeline_history_v2 is not None
    source = next(iter(entry.imported_outputs.values())).source
    manifest = dict(
        schema="h3.context.public_media_asset_manifest.v1",
        profileId=ENGINE_PROFILE_ID,
        profileFingerprint=RUNTIME_PROFILE_FINGERPRINT,
        publicFingerprint=snapshot.public_fingerprint,
        workspaceRevision=snapshot.workspace_revision,
        timelineRevision=snapshot.timeline_revision,
        assets=[public_runtime_asset_wire(asset) for asset in snapshot.assets],
    )
    wire = dict(
        schema=REQUEST_SCHEMA,
        operation="create",
        requestId="lease.generated",
        workspaceHandle=handle,
        workspaceRevision=snapshot.workspace_revision,
        timelineRevision=snapshot.timeline_revision,
        publicFingerprint=snapshot.public_fingerprint,
        manifestFingerprint="sha256:" + hashlib.sha256(_json_bytes(manifest)).hexdigest(),
        profileFingerprint=RUNTIME_PROFILE_FINGERPRINT,
        scope="clip",
        clipId="clip.generated",
        assetId=asset_id,
        derivativeKind="video_proxy",
        ownerId="clip.generated",
        runtimeEpoch=1,
        sourceStartFrame=0,
        sourceEndFrame=frames,
    )
    return production, projection, authoring, adapter, source, snapshot, wire


def test_production_release_after_import_refuses_new_lease_and_render_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, adapter, source, _snapshot, wire = _imported_with_owner(
        tmp_path, monkeypatch, 64
    )
    wire["derivativeKind"] = "frame_timing_index"
    try:
        production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "production.release.after.import",
                "action": "release_workspace",
                "payload": {
                    "workspace_handle": projection.workspace_handle,
                    "expected_workspace_revision": projection.workspace_revision,
                    "expected_workspace_fingerprint": projection.workspace_fingerprint,
                },
            }
        )
        assert not source.current() and not source.lease.released
        request = decode_lease_request(_json_bytes(wire))
        assert isinstance(request, CreateLeaseRequest)
        # The claim and the route authority both refuse a revoked source as stale, never as a
        # generation failure the browser would retry.
        with pytest.raises(MediaLeaseError) as claimed:
            authoring.admit_media_derivative(request, generator=None, media_adapter=adapter)
        assert claimed.value.reason == "stale"
        authority = MediaLeaseAuthority(
            lambda value: authoring.admit_media_derivative(
                value, generator=None, media_adapter=adapter
            ),
            start_reaper=False,
        )
        try:
            with pytest.raises(MediaLeaseError) as created:
                authority.create(request)
            assert created.value.reason == "stale"
        finally:
            authority.close()
        # The render path meets the same revocation as the planner refusal the output service types.
        with pytest.raises(AuthoringSourceBindingError, match="^source_stale$"):
            authoring.prepare_render_plan(wire["workspaceHandle"])
    finally:
        authoring.dispatch(
            _authoring_action(
                "release.final", "release_workspace", workspace_handle=wire["workspaceHandle"]
            )
        )


@pytest.mark.parametrize("frames", [64, 256, 257, 362, 512])
def test_complete_manifest_matches_browser_canonical_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frames: int
) -> None:
    authoring, _adapter, _source, snapshot, wire = _imported(tmp_path, monkeypatch, frames)
    try:
        assert public_asset_manifest_fingerprint(snapshot) == wire["manifestFingerprint"]
    finally:
        authoring.dispatch(
            _authoring_action(
                "release", "release_workspace", workspace_handle=wire["workspaceHandle"]
            )
        )


@pytest.mark.parametrize("frames", [3, 64, 65, 256, 257, 362, 512])
def test_exact_generated_source_enters_derivative_and_remains_revocable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frames: int
) -> None:
    authoring, adapter, source, snapshot, wire = _imported(tmp_path, monkeypatch, frames)
    original = source.lease.path.read_bytes()
    generated: list[GeneratedDerivativeBody] = []

    def proxy(path: Path, facts: Any, **kwargs: Any) -> GeneratedDerivativeBody:
        assert path == source.lease.path
        assert path.read_bytes() == original
        assert facts.frame_count == frames and len(facts.landmarks) == frames
        assert (
            kwargs["expected_source_fingerprint"]
            == "sha256:" + hashlib.sha256(original).hexdigest()
        )
        body = bytearray(b"focused generator boundary")
        result = GeneratedDerivativeBody(
            body,
            "h3.authoring.media_derivatives.v5",
            "video/mp4",
            320,
            240,
            len(body),
            "sha256:" + hashlib.sha256(body).hexdigest(),
            "sha256:" + "a" * 64,
            None,
            "absent",
        )
        generated.append(result)
        return result

    generator = cast(
        AuthoringDerivativeGenerator,
        SimpleNamespace(
            profile_fingerprint="sha256:" + "a" * 64,
            generate_video_proxy=proxy,
        ),
    )
    try:
        request = decode_lease_request(_json_bytes(wire))
        assert isinstance(request, CreateLeaseRequest)
        claim = authoring.admit_media_derivative(
            request, generator=generator, media_adapter=adapter
        )
        body, verified = claim.generate(
            time.monotonic() + 20, SimpleNamespace(is_cancelled=lambda: False)
        )
        assert bytes(body) == b"focused generator boundary"
        asset = next(asset for asset in snapshot.assets if asset.asset_id == wire["assetId"])
        assert (
            verified.asset_fingerprint
            == "sha256:" + hashlib.sha256(_json_bytes(public_runtime_asset_wire(asset))).hexdigest()
        )
        assert source.lease.path.read_bytes() == original
        assert len(generated) == 1 and not generated[0].body
        body.clear()
        authoring.dispatch(
            _authoring_action(
                "release", "release_workspace", workspace_handle=wire["workspaceHandle"]
            )
        )
        assert not claim.current()
        with pytest.raises(MediaLeaseError, match="stale"):
            claim.generate(time.monotonic() + 20, None)
        assert len(generated) == 1
    finally:
        if not source.lease.released:
            authoring.dispatch(
                _authoring_action(
                    "release.final", "release_workspace", workspace_handle=wire["workspaceHandle"]
                )
            )


@pytest.mark.parametrize("frames", [64, 512])
def test_complete_timing_index_keeps_its_independent_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frames: int
) -> None:
    authoring, adapter, _source, _snapshot, wire = _imported(tmp_path, monkeypatch, frames)
    wire["derivativeKind"] = "frame_timing_index"
    try:
        request = decode_lease_request(_json_bytes(wire))
        assert isinstance(request, CreateLeaseRequest)
        claim = authoring.admit_media_derivative(request, generator=None, media_adapter=adapter)
        if frames == 512:
            with pytest.raises(MediaLeaseError, match="resource_limit"):
                claim.generate(time.monotonic() + 20, None)
        else:
            body, verified = claim.generate(time.monotonic() + 20, None)
            assert verified.byte_count == len(body) <= 16 * 1024
            assert len(json.loads(body)["timing"]["landmarks"]) == frames
            body.clear()
    finally:
        authoring.dispatch(
            _authoring_action(
                "release", "release_workspace", workspace_handle=wire["workspaceHandle"]
            )
        )


@pytest.mark.parametrize("mutation", ["release", "tamper"])
def test_generated_revocation_during_generation_discards_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    authoring, adapter, source, _snapshot, wire = _imported(tmp_path, monkeypatch, 65)
    produced: list[GeneratedDerivativeBody] = []
    buffers: list[bytearray] = []

    def proxy(_path: Path, _facts: Any, **_kwargs: Any) -> GeneratedDerivativeBody:
        if mutation == "release":
            authoring.dispatch(
                _authoring_action(
                    "release", "release_workspace", workspace_handle=wire["workspaceHandle"]
                )
            )
        else:
            path = source.lease.path
            stat = path.stat()
            path.write_bytes(b"x" * stat.st_size)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        body = bytearray(b"must never escape revoked owner")
        buffers.append(body)
        result = GeneratedDerivativeBody(
            body,
            "h3.authoring.media_derivatives.v5",
            "video/mp4",
            320,
            240,
            len(body),
            "sha256:" + hashlib.sha256(body).hexdigest(),
            "sha256:" + "a" * 64,
            None,
            "absent",
        )
        produced.append(result)
        return result

    try:
        request = decode_lease_request(_json_bytes(wire))
        assert isinstance(request, CreateLeaseRequest)
        generator = cast(
            AuthoringDerivativeGenerator,
            SimpleNamespace(
                profile_fingerprint="sha256:" + "a" * 64,
                generate_video_proxy=proxy,
            ),
        )
        claim = authoring.admit_media_derivative(
            request, generator=generator, media_adapter=adapter
        )
        with pytest.raises(MediaLeaseError, match="stale"):
            claim.generate(time.monotonic() + 20, SimpleNamespace(is_cancelled=lambda: False))
        assert len(produced) == 1 and not produced[0].body and produced[0].byte_count == 0
        assert len(buffers) == 1 and not buffers[0]
    finally:
        if not source.lease.released:
            authoring.dispatch(
                _authoring_action(
                    "release.final", "release_workspace", workspace_handle=wire["workspaceHandle"]
                )
            )


@pytest.mark.parametrize("origin", ["generated", "path_backed"])
@pytest.mark.parametrize("frames", [362, 512])
def test_native_complete_source_derivative_preserves_original_and_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str, frames: int
) -> None:
    from test_m25_authoring_audio_rate_compatibility import _ffmpeg, _qualified_tools
    from test_m25_render_source_currentness import _video_source

    from comfyui_h3_context.adapters.authoring_derivative_source import AuthoringDerivativeClaim
    from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
    from comfyui_h3_context.adapters.authoring_render_source import (
        PreparedAuthoringHistory,
        claim_render_source,
    )
    from comfyui_h3_context.adapters.authoring_video_facts import (
        AuthoringVideoRational,
        probe_authoring_video_facts,
    )

    ffmpeg, ffprobe, adapter = _qualified_tools(tmp_path)
    encoded = (tmp_path / "original.mp4").resolve()
    duration = str(frames / 24)
    _ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=64x64:rate=24:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=701:sample_rate=32000:duration={duration}",
        "-c:v",
        "libx264",
        "-bf",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709,setsar=0",
        "-color_range",
        "tv",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-c:a",
        "aac",
        "-ar",
        "32000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-y",
        str(encoded),
    )
    original = encoded.read_bytes()
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "proxy-scratch").resolve(),
    )
    facts = probe_authoring_video_facts(encoded, adapter, time.monotonic() + 30)
    assert facts.frame_count == frames and facts.pixel_aspect_ratio is None
    assert facts.embedded_audio.sample_rate == 32000 and facts.embedded_audio.channels == 2
    if origin == "generated":
        authoring, adapter, source, snapshot, wire = _imported(
            tmp_path, monkeypatch, frames, (adapter, original)
        )
        request = decode_lease_request(_json_bytes(wire))
        assert isinstance(request, CreateLeaseRequest)
        claim = authoring.admit_media_derivative(
            request, generator=generator, media_adapter=adapter
        )
        original_path = source.lease.path

        def release() -> None:
            authoring.dispatch(
                _authoring_action(
                    "release", "release_workspace", workspace_handle=wire["workspaceHandle"]
                )
            )
    else:
        monkeypatch.setattr(
            "comfyui_h3_context.adapters.comfyui_authoring_media_preview.current_authoring_media_preview_adapter",
            lambda: adapter,
        )
        original_path, binding, _source, _verification = _video_source(tmp_path, original)
        source_claim = claim_render_source(binding, "video_1")
        snapshot_wire = fixture_snapshot()
        snapshot_wire["assets"] = [source_claim.asset.to_wire()]
        snapshot_wire["tracks"] = [snapshot_wire["tracks"][0]]
        clip = snapshot_wire["clips"][0]
        clip.update(asset_id="video_1", duration_frames=1, source_start_frame=0)
        snapshot_wire["clips"] = [clip]
        snapshot_wire["public_fingerprint"] = public_snapshot_fingerprint(snapshot_wire)
        snapshot = decode_public_snapshot(snapshot_wire)
        history = PreparedAuthoringHistory(
            snapshot, binding.generation, (source_claim,), load_packaged_font_manifest()
        )
        request = CreateLeaseRequest(
            "native.path",
            snapshot.workspace_handle,
            snapshot.workspace_revision,
            snapshot.timeline_revision,
            snapshot.public_fingerprint,
            public_asset_manifest_fingerprint(snapshot),
            RUNTIME_PROFILE_FINGERPRINT,
            "clip",
            clip["clip_id"],
            "video_1",
            "video_proxy",
            clip["clip_id"],
            1,
            0,
            frames,
        )
        claim = AuthoringDerivativeClaim(
            request, snapshot, history, binding, lambda: True, generator, adapter
        )
        release = binding.release
    try:
        body, verified = claim.generate(
            time.monotonic() + 60, SimpleNamespace(is_cancelled=lambda: False)
        )
        try:
            proxy = tmp_path / "proxy.mp4"
            proxy.write_bytes(body)
            observed = probe_authoring_video_facts(proxy.resolve(), adapter, time.monotonic() + 30)
            assert observed.frame_count == frames
            assert (
                observed.landmarks == facts.landmarks
                and observed.source_time_base == facts.source_time_base
            )
            assert observed.pixel_aspect_ratio == AuthoringVideoRational(1, 1)
            assert observed.embedded_audio == facts.embedded_audio
            assert verified.audio_disposition == "present_bound"
            assert original_path.read_bytes() == original == encoded.read_bytes()
        finally:
            body.clear()
        release()
        assert not claim.current()
        with pytest.raises(MediaLeaseError, match="stale"):
            claim.confirm(time.monotonic() + 10)
    finally:
        release()
