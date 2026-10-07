from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import pytest
from test_m25_nle_authoring_state import state_for

from comfyui_h3_context.core.authoring_asset_manifest import (
    NLE_AUTHORING_ASSET_MANIFEST_SCHEMA,
    build_nle_authoring_asset_manifest,
    nle_authoring_asset_manifest_fingerprint,
)
from comfyui_h3_context.core.authoring_media import (
    NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
    CreateLeaseRequest,
    MediaLeaseError,
    decode_lease_request,
)
from comfyui_h3_context.core.composition_contract import (
    TimingLandmark,
    composition_contract_fingerprint,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    NleAuthoringState,
    create_nle_authoring_state,
    materialize_render_snapshot,
)


def _request_wire(state: NleAuthoringState, manifest: dict[str, object]) -> dict[str, object]:
    return {
        "schema": NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
        "operation": "create",
        "requestId": "nle-asset-request-1",
        "authoringSchema": state.schema,
        "profileId": state.profile_id,
        "workspaceHandle": state.workspace_handle,
        "workspaceRevision": state.workspace_revision,
        "timelineRevision": state.timeline_revision,
        "authoringFingerprint": state.authoring_fingerprint,
        "manifestFingerprint": manifest["manifestFingerprint"],
        "profileFingerprint": manifest["profileFingerprint"],
        "assetId": state.assets[0].asset_id,
        "derivativeKind": "thumbnail",
        "ownerId": "nle-thumbnail-owner-1",
        "runtimeEpoch": 1,
        "sourceStartFrame": 0,
        "sourceEndFrame": 1,
    }


def test_empty_authoring_manifest_binds_assets_and_long_timing_rows() -> None:
    state = state_for(())
    long_asset = state.assets[0]
    landmarks = tuple(TimingLandmark(frame, frame * 512, frame * 512, 512) for frame in range(512))
    state = create_nle_authoring_state(
        project_id=state.project_id,
        workspace_handle=state.workspace_handle,
        workspace_revision=state.workspace_revision,
        timeline_revision=state.timeline_revision,
        edit_capacity_frames=state.edit_capacity_frames,
        assets=(
            replace(long_asset, source_frame_count=512, landmarks=landmarks),
            *state.assets[1:],
        ),
        tracks=state.tracks,
        clips=(),
        audio_extension=state.audio_extension,
        blockers=state.blockers,
    )

    manifest = build_nle_authoring_asset_manifest(state)
    payload = {key: value for key, value in manifest.items() if key != "manifestFingerprint"}

    assert materialize_render_snapshot(state) is None
    assert manifest["schema"] == NLE_AUTHORING_ASSET_MANIFEST_SCHEMA
    assert manifest["authoringFingerprint"] == state.authoring_fingerprint
    asset_rows = cast(list[dict[str, object]], manifest["assets"])
    assert asset_rows[0]["sourceFrameCount"] == 512
    assert manifest["manifestFingerprint"] == composition_contract_fingerprint(payload)
    assert nle_authoring_asset_manifest_fingerprint(state) == manifest["manifestFingerprint"]


def test_authoring_asset_lease_create_is_closed_and_keeps_v1_unmodified() -> None:
    state = state_for(())
    manifest = build_nle_authoring_asset_manifest(state)
    wire = _request_wire(state, manifest)
    decoded = decode_lease_request(json.dumps(wire).encode())

    assert isinstance(decoded, CreateLeaseRequest)
    assert decoded.scope == "asset" and decoded.clip_id is None
    assert decoded.authoring_fingerprint == state.authoring_fingerprint
    assert decoded.to_wire() == wire

    with pytest.raises(MediaLeaseError):
        decode_lease_request(
            json.dumps({**wire, "publicFingerprint": "sha256:" + "0" * 64}).encode()
        )
    with pytest.raises(MediaLeaseError):
        decode_lease_request(json.dumps({**wire, "scope": "clip"}).encode())
