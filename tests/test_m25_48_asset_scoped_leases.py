from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_m25_29_production_authoring_import import (
    _authoring_action,
    _import_request,
    _qualified_test_adapter,
    _registries_with_ready_output,
)
from test_m25_composition_contract import fixture_snapshot
from test_m25_media_derivative_contract import create_wire
from test_m25_media_source_leases import Claim

from comfyui_h3_context.adapters.authoring_derivative_generator import AuthoringDerivativeGenerator
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.core.authoring_asset_manifest import build_nle_authoring_asset_manifest
from comfyui_h3_context.core.authoring_media import (
    NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    LeaseCommand,
    MediaLeaseError,
    VerifiedDerivative,
    decode_lease_request,
    public_asset_manifest_fingerprint,
    validate_lease_snapshot,
)
from comfyui_h3_context.core.composition_contract import decode_public_snapshot


def _wire(scope: str = "clip", *, owner: str = "owner-1") -> dict[str, object]:
    wire = create_wire()
    wire.update(scope=scope, ownerId=owner, requestId=f"request-{owner}")
    if scope == "asset":
        wire.update(
            clipId=None,
            assetId="img-overlay",
            derivativeKind="thumbnail",
            sourceStartFrame=0,
            sourceEndFrame=1,
        )
    return wire


def _decode(wire: dict[str, object]) -> CreateLeaseRequest:
    value = decode_lease_request(json.dumps(wire).encode())
    assert isinstance(value, CreateLeaseRequest)
    return value


def _snapshot_request(scope: str = "asset") -> tuple[CreateLeaseRequest, Any]:
    snapshot = decode_public_snapshot(fixture_snapshot())
    wire = _wire(scope)
    wire.update(
        workspaceHandle=snapshot.workspace_handle,
        workspaceRevision=snapshot.workspace_revision,
        timelineRevision=snapshot.timeline_revision,
        publicFingerprint=snapshot.public_fingerprint,
        manifestFingerprint=public_asset_manifest_fingerprint(snapshot),
    )
    if scope == "clip":
        wire.update(
            clipId="clip-image",
            assetId="img-overlay",
            derivativeKind="thumbnail",
            sourceStartFrame=0,
            sourceEndFrame=1,
        )
    return _decode(wire), snapshot


def test_scope_is_explicit_closed_and_clip_id_is_nullable_only_for_asset_scope() -> None:
    clip = _decode(_wire())
    assert clip.scope == "clip" and clip.clip_id == "clip-1"
    asset = _decode(_wire("asset"))
    assert asset.scope == "asset" and asset.clip_id is None
    assert asset.to_wire() == _wire("asset")

    invalid = []
    missing = _wire()
    missing.pop("scope")
    invalid.append(missing)
    invalid.append({**_wire(), "scope": "decoration"})
    invalid.append({**_wire("asset"), "clipId": "clip-1"})
    invalid.append({**_wire(), "clipId": None})
    for candidate in invalid:
        with pytest.raises(MediaLeaseError, match="invalid_request"):
            _decode(candidate)


def test_asset_scope_validation_table_and_clip_scope_remain_distinct() -> None:
    asset_request, snapshot = _snapshot_request()
    assert validate_lease_snapshot(asset_request, snapshot).asset_id == "img-overlay"

    for kind in ("video_proxy", "frame_timing_index", "image_proxy", "packaged_font_face"):
        with pytest.raises(MediaLeaseError, match="unsupported"):
            validate_lease_snapshot(replace(asset_request, derivative_kind=kind), snapshot)
    with pytest.raises(MediaLeaseError, match="authority_mismatch"):
        validate_lease_snapshot(replace(asset_request, asset_id="absent-asset"), snapshot)
    with pytest.raises(MediaLeaseError, match="authority_mismatch"):
        validate_lease_snapshot(replace(asset_request, source_end_frame=2), snapshot)

    font_id = next(asset.asset_id for asset in snapshot.assets if asset.kind == "font")
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_lease_snapshot(replace(asset_request, asset_id=font_id), snapshot)

    clip_request, _ = _snapshot_request("clip")
    assert validate_lease_snapshot(clip_request, snapshot).asset_id == "img-overlay"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: replace(request, workspace_handle="other-workspace"),
        lambda request: replace(request, workspace_revision=999),
        lambda request: replace(request, timeline_revision=999),
        lambda request: replace(request, public_fingerprint="sha256:" + "8" * 64),
        lambda request: replace(request, manifest_fingerprint="sha256:" + "9" * 64),
    ],
)
def test_asset_scope_preserves_all_five_workspace_currentness_fields(
    mutate: Callable[[CreateLeaseRequest], CreateLeaseRequest],
) -> None:
    request, snapshot = _snapshot_request()
    with pytest.raises(MediaLeaseError, match="stale"):
        validate_lease_snapshot(mutate(request), snapshot)


class _LeaseClaim(Claim):
    def __init__(self, request: CreateLeaseRequest, generated: list[bytearray]) -> None:
        super().__init__()
        self.request = request
        self.cache_key = f"{request.scope}:{request.owner_id}:{request.request_id}"
        self.generated = generated

    def generate(
        self, deadline: float, cancellation: object
    ) -> tuple[bytearray, VerifiedDerivative]:
        body, facts = super().generate(deadline, cancellation)
        body = bytearray(f"body-{self.request.request_id}".encode())
        generated = replace(
            facts,
            kind=self.request.derivative_kind,
            source=replace(facts.source, asset_id=self.request.asset_id),
            byte_count=len(body),
            derivative=replace(
                facts.derivative,
                asset_id=self.request.asset_id,
                derivative_fingerprint="sha256:" + hashlib.sha256(body).hexdigest(),
            ),
        )
        self.generated.append(body)
        return body, generated


def _authority_request(scope: str, index: int) -> CreateLeaseRequest:
    wire = _wire(scope, owner=f"owner-{index}")
    wire.update(requestId=f"request-{index}")
    if scope == "clip":
        wire.update(
            derivativeKind="thumbnail",
            sourceStartFrame=0,
            sourceEndFrame=1,
        )
    return _decode(wire)


def test_decoration_cap_and_two_slot_playback_reserve_do_not_reduce_playback_capacity() -> None:
    generated: list[bytearray] = []
    authority = MediaLeaseAuthority(
        lambda request: _LeaseClaim(request, generated), start_reaper=False
    )
    try:
        authority.create(_authority_request("asset", 1))
        for index in range(2, 7):
            authority.create(_authority_request("clip", index))
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_authority_request("asset", 7))
        # The refused decoration leaves both reserved slots available to playback.
        authority.create(_authority_request("clip", 8))
        authority.create(_authority_request("clip", 9))
        assert authority.resources()["leases"] == 8
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_authority_request("clip", 10))
    finally:
        authority.close()


def test_two_decorations_are_the_hard_class_cap_before_generation() -> None:
    generated: list[bytearray] = []
    authority = MediaLeaseAuthority(
        lambda request: _LeaseClaim(request, generated), start_reaper=False
    )
    try:
        authority.create(_authority_request("asset", 1))
        authority.create(_authority_request("asset", 2))
        before = len(generated)
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_authority_request("asset", 3))
        assert len(generated) == before
    finally:
        authority.close()


def test_generation_race_is_refused_by_the_second_capacity_check_and_body_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated: list[bytearray] = []
    authority = MediaLeaseAuthority(
        lambda request: _LeaseClaim(request, generated), start_reaper=False
    )
    original = authority._capacity
    calls = 0

    def raced(request: CreateLeaseRequest) -> None:
        nonlocal calls
        calls += 1
        original(request)
        if calls == 2:
            raise MediaLeaseError("resource_limit")

    monkeypatch.setattr(authority, "_capacity", raced)
    try:
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_authority_request("asset", 1))
        assert calls == 2
        assert generated == [bytearray()]
        assert authority.resources() == {
            "leases": 0,
            "cacheEntries": 0,
            "cacheBytes": 0,
            "activeReads": 0,
        }
    finally:
        authority.close()


def test_scope_is_part_of_authority_cache_identity() -> None:
    clip = _authority_request("clip", 1)
    asset = replace(clip, request_id="request-asset", scope="asset", clip_id=None)
    generated: list[bytearray] = []
    clip_claim = _LeaseClaim(clip, generated)
    asset_claim = _LeaseClaim(asset, generated)
    assert clip_claim.cache_key != asset_claim.cache_key


def test_asset_source_revocation_during_generation_cannot_publish() -> None:
    generated: list[bytearray] = []
    request = _authority_request("asset", 1)
    claim = _LeaseClaim(request, generated)
    original = claim.generate

    def revoked(deadline: float, cancellation: object) -> tuple[bytearray, VerifiedDerivative]:
        result = original(deadline, cancellation)
        claim.live = False
        return result

    claim.generate = revoked  # type: ignore[method-assign]
    authority = MediaLeaseAuthority(lambda _: claim, start_reaper=False)
    try:
        with pytest.raises(MediaLeaseError, match="stale"):
            authority.create(request)
        assert generated == [bytearray()]
        assert authority.resources()["cacheBytes"] == 0
    finally:
        authority.close()


def test_imported_production_asset_admits_current_thumbnail_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, production_projection, authoring, before, history, _store = (
        _registries_with_ready_output(tmp_path, frame_count=192, initial_source_binding=False)
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch, frame_count=192)
    request = _import_request(production_projection, before, history)
    imported = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.timeline_history_v2 is not None
    state = entry.timeline_history_v2.authoring
    manifest = build_nle_authoring_asset_manifest(state)
    asset_id = imported.receipt.rows[0].asset_id
    lease_request = CreateLeaseRequest(
        request_id="imported-thumbnail-request",
        workspace_handle=state.workspace_handle,
        workspace_revision=state.workspace_revision,
        timeline_revision=state.timeline_revision,
        public_fingerprint=None,
        manifest_fingerprint=str(manifest["manifestFingerprint"]),
        profile_fingerprint=RUNTIME_PROFILE_FINGERPRINT,
        scope="asset",
        clip_id=None,
        asset_id=asset_id,
        derivative_kind="thumbnail",
        owner_id="imported-thumbnail-owner",
        runtime_epoch=1,
        source_start_frame=0,
        source_end_frame=1,
        request_schema=NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
        authoring_schema=state.schema,
        authoring_profile_id=state.profile_id,
        authoring_fingerprint=state.authoring_fingerprint,
    )

    claim = authoring.admit_media_derivative(
        lease_request,
        generator=AuthoringDerivativeGenerator.for_images(),
        media_adapter=adapter,
    )
    history_after_import = authoring.dispatch(
        _authoring_action(
            "read-after-import",
            "read_timeline_history",
            workspace_handle=request.authoring_workspace_handle,
        )
    )
    assert history_after_import.body is not None
    replay = authoring.import_production_outputs(
        _import_request(
            production_projection,
            imported.authoring_projection,
            history_after_import.body,
            request_id="import.integration.no-op",
        ),
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )

    assert replay.receipt.disposition == "already_imported"
    assert claim.current()


def test_real_imported_production_asset_generates_thumbnail_with_current_source(
    tmp_path: Path,
) -> None:
    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg = Path(ffmpeg_value).resolve(strict=True)
    ffprobe = Path(ffprobe_value).resolve(strict=True)
    fixture = Path(__file__).parents[1] / "scripts" / "fixtures" / "m25_48_192_frame_mp4.b64"
    body = base64.b64decode("".join(fixture.read_text(encoding="ascii").split()), validate=True)
    production, production_projection, authoring, before, history, _store = (
        _registries_with_ready_output(
            tmp_path,
            artifact_bodies=(body,),
            frame_count=192,
            width=64,
            height=64,
            initial_source_binding=False,
        )
    )
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "adapter-scratch").resolve(),
        clock_ms=lambda: 1,
    )
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "derivative-scratch").resolve(),
    )
    request = _import_request(production_projection, before, history)
    imported = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 30.0,
    )
    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.timeline_history_v2 is not None
    state = entry.timeline_history_v2.authoring
    assert state.content_end_exclusive == 0
    manifest = build_nle_authoring_asset_manifest(state)
    asset_id = imported.receipt.rows[0].asset_id
    lease_request = CreateLeaseRequest(
        request_id="real-imported-thumbnail-request",
        workspace_handle=state.workspace_handle,
        workspace_revision=state.workspace_revision,
        timeline_revision=state.timeline_revision,
        public_fingerprint=None,
        manifest_fingerprint=str(manifest["manifestFingerprint"]),
        profile_fingerprint=RUNTIME_PROFILE_FINGERPRINT,
        scope="asset",
        clip_id=None,
        asset_id=asset_id,
        derivative_kind="thumbnail",
        owner_id="real-imported-thumbnail-owner",
        runtime_epoch=1,
        source_start_frame=0,
        source_end_frame=1,
        request_schema=NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
        authoring_schema=state.schema,
        authoring_profile_id=state.profile_id,
        authoring_fingerprint=state.authoring_fingerprint,
    )
    authority = MediaLeaseAuthority(
        lambda value: authoring.admit_media_derivative(
            value, generator=generator, media_adapter=adapter
        ),
        start_reaper=False,
    )
    try:
        receipt, capability = authority.create(lease_request)
        opened = authority.open(
            LeaseCommand(
                "open",
                cast(str, receipt["requestId"]),
                cast(str, receipt["leaseId"]),
                cast(int, receipt["revision"]),
                cast(str, receipt["ownerId"]),
                cast(int, receipt["runtimeEpoch"]),
            ),
            capability,
        )
        assert opened.read_chunk().startswith(b"\x89PNG")
    finally:
        authority.close()
