"""V2 asset-only derivative manifest bound to accepted NLE authoring identity."""

from __future__ import annotations

from .authoring_media import (
    ASSET_DECORATION_KINDS,
    ASSET_PREPARATION_KINDS,
    MAX_SOURCE_FRAMES,
    NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    MediaLeaseError,
    public_runtime_asset_wire,
)
from .composition_contract import PublicAsset, composition_contract_fingerprint
from .nle_authoring_contract import NleAuthoringState

NLE_AUTHORING_ASSET_MANIFEST_SCHEMA = "h3.context.nle_asset_manifest.v1"


def nle_authoring_asset_manifest_payload(state: NleAuthoringState) -> dict[str, object]:
    if not isinstance(state, NleAuthoringState):
        raise ValueError("invalid_contract: authoring asset manifest requires typed V2 state")
    return {
        "schema": NLE_AUTHORING_ASSET_MANIFEST_SCHEMA,
        "authoringSchema": state.schema,
        "profileId": state.profile_id,
        "profileFingerprint": RUNTIME_PROFILE_FINGERPRINT,
        "workspaceHandle": state.workspace_handle,
        "workspaceRevision": state.workspace_revision,
        "timelineRevision": state.timeline_revision,
        "authoringFingerprint": state.authoring_fingerprint,
        "assets": [public_runtime_asset_wire(asset) for asset in state.assets],
    }


def nle_authoring_asset_manifest_fingerprint(state: NleAuthoringState) -> str:
    return composition_contract_fingerprint(nle_authoring_asset_manifest_payload(state))


def build_nle_authoring_asset_manifest(state: NleAuthoringState) -> dict[str, object]:
    payload = nle_authoring_asset_manifest_payload(state)
    return {**payload, "manifestFingerprint": composition_contract_fingerprint(payload)}


def validate_nle_authoring_asset_lease_request(
    request: CreateLeaseRequest,
    state: NleAuthoringState,
) -> PublicAsset:
    """Bind asset-only derivative authority to one current authoring state and catalog."""

    if (
        type(request) is not CreateLeaseRequest
        or request.request_schema != NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA
        or type(state) is not NleAuthoringState
    ):
        raise MediaLeaseError("authority_mismatch")
    if (
        request.authoring_schema != state.schema
        or request.authoring_profile_id != state.profile_id
        or request.workspace_handle != state.workspace_handle
        or request.workspace_revision != state.workspace_revision
        or request.timeline_revision != state.timeline_revision
        or request.authoring_fingerprint != state.authoring_fingerprint
        or request.manifest_fingerprint != nle_authoring_asset_manifest_fingerprint(state)
    ):
        raise MediaLeaseError("stale")
    asset = next((item for item in state.assets if item.asset_id == request.asset_id), None)
    if type(asset) is not PublicAsset:
        raise MediaLeaseError("authority_mismatch")
    if asset.kind == "font" or request.derivative_kind not in (
        ASSET_DECORATION_KINDS | ASSET_PREPARATION_KINDS
    ):
        raise MediaLeaseError("unsupported")
    if request.derivative_kind == "thumbnail":
        span = (0, 1)
    else:
        if (
            asset.kind != "video"
            or asset.source_frame_count != len(asset.landmarks)
            or not 1 <= len(asset.landmarks) <= MAX_SOURCE_FRAMES
        ):
            raise MediaLeaseError("unsupported")
        # Both audio kinds are derived from the asset's own bound stream; neither is generated as
        # silence for an asset that has none.
        if request.derivative_kind in {"audio_peaks", "audio_preview"} and (
            asset.embedded_audio != "present_bound" or asset.source_sample_count is None
        ):
            raise MediaLeaseError("unsupported")
        span = (0, asset.source_frame_count)
    if (request.source_start_frame, request.source_end_frame) != span:
        raise MediaLeaseError("authority_mismatch")
    return asset


__all__ = [
    "NLE_AUTHORING_ASSET_MANIFEST_SCHEMA",
    "build_nle_authoring_asset_manifest",
    "nle_authoring_asset_manifest_fingerprint",
    "nle_authoring_asset_manifest_payload",
    "validate_nle_authoring_asset_lease_request",
]
