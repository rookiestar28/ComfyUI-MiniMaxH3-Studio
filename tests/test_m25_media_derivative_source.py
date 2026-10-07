from __future__ import annotations

import copy
import hashlib
import pickle
import time
from dataclasses import replace
from typing import Any, cast

import pytest
from test_m25_composition_contract import fixture_snapshot, resign

from comfyui_h3_context.adapters.authoring_derivative_generator import AuthoringDerivativeGenerator
from comfyui_h3_context.adapters.authoring_derivative_source import AuthoringDerivativeClaim
from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
from comfyui_h3_context.adapters.authoring_render_source import (
    PreparedAuthoringHistory,
    claim_render_source,
)
from comfyui_h3_context.adapters.authoring_source_binding import RuntimeComfySourceFactory
from comfyui_h3_context.core.authoring_media import (
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    DerivativeKind,
    MediaLeaseError,
    public_asset_manifest_fingerprint,
    validate_lease_snapshot,
)
from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
)
from comfyui_h3_context.core.contracts import AssetRole, MediaKind
from comfyui_h3_context.core.registry import ReferenceAsset, build_reference_registry


def request_for(
    snapshot: PublicCompositionSnapshot, clip_id: str, asset_id: str, kind: DerivativeKind
) -> CreateLeaseRequest:
    return CreateLeaseRequest(
        "source-test",
        snapshot.workspace_handle,
        snapshot.workspace_revision,
        snapshot.timeline_revision,
        snapshot.public_fingerprint,
        public_asset_manifest_fingerprint(snapshot),
        RUNTIME_PROFILE_FINGERPRINT,
        "clip",
        clip_id,
        asset_id,
        kind,
        "source-owner",
        1,
        0,
        1,
    )


@pytest.mark.parametrize("face_index", range(4))
def test_packaged_font_face_is_exact_verified_body_and_revocable(face_index: int) -> None:
    fonts = load_packaged_font_manifest()
    face = fonts.faces[face_index]
    wire = fixture_snapshot()
    font = next(asset for asset in wire["assets"] if asset["kind"] == "font")
    font["asset_id"] = face.font_asset_id
    text = next(clip for clip in wire["clips"] if clip["clip_id"] == "clip-title")["text"]
    text.update(font_asset_id=face.font_asset_id, weight=face.weight, style=face.style)
    snapshot = decode_public_snapshot(resign(wire))
    history = PreparedAuthoringHistory(snapshot, 1, (), fonts)
    request = request_for(snapshot, "clip-title", face.font_asset_id, "packaged_font_face")
    claim = AuthoringDerivativeClaim(request, snapshot, history, None, lambda: True, None, None)
    body, facts = claim.generate(time.monotonic() + 10, None)
    assert bytes(body) == face.read_verified_bytes()
    assert facts.derivative.derivative_fingerprint == "sha256:" + hashlib.sha256(body).hexdigest()
    assert facts.audio_disposition == "absent"
    assert getattr(facts, "geometry", None) is None
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(claim)
    history.discard()
    with pytest.raises(MediaLeaseError, match="stale"):
        claim.confirm(time.monotonic() + 5)
    body.clear()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "cancelled",
        "timeout",
        "unsupported",
        "resource_limit",
        "busy",
        "stale",
        "authority_mismatch",
    ],
)
def test_image_claim_uses_transferred_snapshot_and_rejects_released_source(
    failure: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    registry = build_reference_registry(
        (ReferenceAsset("img-overlay", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
    )
    image = torch.full((1, 2, 3, 3), 0.5, dtype=torch.float32)
    binding = RuntimeComfySourceFactory().capture(
        exact_registry=registry, generation=1, sources=(("img-overlay", MediaKind.IMAGE, image),)
    )
    original = claim_render_source(binding, "img-overlay")
    snapshot = decode_public_snapshot(fixture_snapshot())
    history = PreparedAuthoringHistory(snapshot, 1, (original,), load_packaged_font_manifest())
    request = request_for(snapshot, "clip-image", "img-overlay", "image_proxy")
    generator = AuthoringDerivativeGenerator.for_images()
    claim = AuthoringDerivativeClaim(
        request,
        snapshot,
        history,
        binding,
        lambda: True,
        generator,
        None,
    )
    image.fill_(1)
    if failure is not None:
        from comfyui_h3_context.adapters.authoring_derivative_generator import (
            AuthoringDerivativeGeneratorError,
        )

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AuthoringDerivativeGeneratorError(failure)

        monkeypatch.setattr(generator, "generate_image_proxy", refuse)
        try:
            with pytest.raises(MediaLeaseError, match=failure):
                claim.generate(time.monotonic() + 10, None)
        finally:
            binding.release()
        return
    body, facts = claim.generate(time.monotonic() + 10, None)
    from test_m25_media_derivative_generator import _png_rgb

    assert _png_rgb(body) == (3, 2, bytes([128] * 18))
    assert facts.source.source_fingerprint == original.source_fingerprint
    geometry = getattr(facts, "geometry", None)
    assert geometry is not None
    assert geometry.to_wire() == {
        "schema": "h3.authoring.media_geometry.v1",
        "sourceWidth": 3,
        "sourceHeight": 2,
        "derivativeWidth": 3,
        "derivativeHeight": 2,
    }
    binding.release()
    assert not claim.current()
    with pytest.raises(MediaLeaseError, match="stale"):
        claim.confirm(time.monotonic() + 5)
    body.clear()


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_revision", 999),
        ("timeline_revision", 999),
        ("public_fingerprint", "sha256:" + "9" * 64),
        ("manifest_fingerprint", "sha256:" + "9" * 64),
    ],
)
def test_request_snapshot_revisions_and_fingerprints_are_fenced(field: str, value: Any) -> None:
    snapshot = decode_public_snapshot(fixture_snapshot())
    request = request_for(snapshot, "clip-image", "img-overlay", "image_proxy")
    with pytest.raises(MediaLeaseError, match="stale"):
        validate_lease_snapshot(replace(request, **{field: value}), snapshot)


def test_cross_clip_asset_cannot_authorize_derivative() -> None:
    snapshot = decode_public_snapshot(fixture_snapshot())
    request = request_for(snapshot, "clip-title", "img-overlay", "image_proxy")
    with pytest.raises(MediaLeaseError, match="authority_mismatch"):
        validate_lease_snapshot(request, snapshot)


def test_application_claim_tracks_semantic_edits_and_workspace_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_m25_authoring_initialization import (
        action,
        image_workspace,
        initialize,
        insert_image_clip,
    )
    from test_m25_media_source_leases import command

    from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
    from comfyui_h3_context.core.nle_authoring_contract import (
        NLE_AUTHORING_PROFILE_ID,
        NLE_AUTHORING_SCHEMA,
        NLE_OPERATION_PROFILE_ID,
        TIMELINE_TRANSACTION_SCHEMA_V2,
    )

    workspace, projection, binding = image_workspace(monkeypatch)
    initialized = workspace.dispatch(initialize(projection))
    assert initialized.body is not None
    # IMPORTANT: this registry owns V2 authoring history; reading the retired V1 `snapshot`
    # field bypasses the accepted contract and fails before source-lifetime behavior is exercised.
    authoring_state = cast(dict[str, Any], initialized.body["authoring"])
    assert initialized.body["render_snapshot"] is None
    authoring_state = insert_image_clip(
        workspace,
        authoring_state,
        request_id="lease-insert",
        clip_id="clip-image",
    )
    history = workspace.dispatch(
        action(
            "lease-read",
            "read_timeline_history",
            workspace_handle=projection["workspace_handle"],
        )
    )
    assert history.body is not None and history.body["render_snapshot"] is not None
    public = decode_public_snapshot(cast(dict[str, Any], history.body["render_snapshot"]))

    from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringDispatchResult

    def edit(commands: list[dict[str, Any]], request_id: str) -> AuthoringDispatchResult:
        return workspace.dispatch(
            action(
                request_id,
                "apply_timeline_transaction",
                schema=TIMELINE_TRANSACTION_SCHEMA_V2,
                authoring_schema=NLE_AUTHORING_SCHEMA,
                profile_id=NLE_AUTHORING_PROFILE_ID,
                operation_profile_id=NLE_OPERATION_PROFILE_ID,
                request_id=request_id,
                transaction_id="tx-" + request_id,
                workspace_handle=projection["workspace_handle"],
                expected_workspace_revision=authoring_state["workspace_revision"],
                expected_timeline_revision=authoring_state["timeline_revision"],
                expected_timeline_fingerprint=authoring_state["timeline_fingerprint"],
                expected_authoring_fingerprint=authoring_state["authoring_fingerprint"],
                commands=commands,
            )
        )

    request = request_for(public, "clip-image", "image_1", "image_proxy")
    generator = AuthoringDerivativeGenerator.for_images()
    authority = MediaLeaseAuthority(
        lambda value: workspace.admit_media_derivative(
            value, generator=generator, media_adapter=None
        ),
        start_reaper=False,
    )
    try:
        receipt, capability = authority.create(request)
        body = authority.open(command(receipt, "open"), capability)
        assert body.read_chunk().startswith(b"\x89PNG")
        changed = edit(
            [{"kind": "set_clip_enabled", "payload": {"clip_id": "clip-image", "enabled": False}}],
            "lease-edit",
        )
        assert changed.status == 200
        authority.reap()
        assert authority.resources()["cacheBytes"] == 0
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            body.read_chunk()
        with pytest.raises(MediaLeaseError, match="stale"):
            authority.create(replace(request, request_id="retry-old-snapshot"))
        workspace.dispatch(
            action("release", "release_workspace", workspace_handle=projection["workspace_handle"])
        )
        assert binding is not None and binding.released
    finally:
        authority.close()
