"""Preparation of an asset's playback derivatives: what is admitted and what stays refused.

A video asset's proxy and audio preview used to be generated when the first clip of the asset was
acquired for playback, which is on the user's path to a playable monitor. They may now be asked
for without a clip, through the authoring-bound asset request, so that the clip finds them already
generated. These tests fix the admission (one request form, a video asset, two kinds, the whole
source), every refusal that must remain, the rule that lets the clip find the prepared body (the
key of a playback kind does not name the scope), and that a preparation stays in the decoration
class of the lease pool. The route's one worker is fixed from both sides: clip playback takes it
from a preparation the page has aborted, and a preparation nobody aborts keeps it, so that an
owner's renewal, open or transfer is refused meanwhile and a clip's create waits, for five
seconds and no longer; behind another clip's work a create does not wait at all. Two bounds of
the lease authority that preparation leans on harder are fixed here as well: a create that cannot
fit discards nothing, and one failed pass neither ends the thread that enforces the idle bound
nor goes unreported, and its report holds no request while it is written.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_m25_29_production_authoring_import import (
    _authoring_action,
    _import_request,
    _insert_asset_transaction,
    _qualified_test_adapter,
    _registries_with_ready_output,
)
from test_m25_45_lease_cross_boundary import lease_request, release_command
from test_m25_74_derivative_retention import (  # noqa: F401 -- `retaining` is a fixture
    NOTHING_IDLE,
    Setup,
    retaining,
)
from test_m25_media_derivative_contract import create_wire
from test_m25_media_source_leases import Claim
from test_m25_nle_authoring_state import state_for

import comfyui_h3_context.adapters.authoring_derivative_source as claim_module
import comfyui_h3_context.adapters.authoring_media_leases as leases_module
from comfyui_h3_context.adapters.authoring_derivative_generator import AuthoringDerivativeGenerator
from comfyui_h3_context.adapters.authoring_derivative_source import AuthoringDerivativeClaim
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.core.authoring_asset_manifest import (
    build_nle_authoring_asset_manifest,
    validate_nle_authoring_asset_lease_request,
)
from comfyui_h3_context.core.authoring_media import (
    KINDS,
    MAX_CACHE_BYTES,
    NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    DerivativeKind,
    MediaLeaseError,
    VerifiedDerivative,
    decode_lease_request,
    derivative_byte_limit,
    public_asset_manifest_fingerprint,
)
from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    TimingLandmark,
    decode_public_snapshot,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NleAuthoringState,
    create_nle_authoring_state,
)

FRAMES = 12
# What this file expects, stated here and not read from the product: a request without a clip
# may name a decoration or, in the authoring-bound form, a playback kind; never the other three.
PLAYBACK_KINDS = ("video_proxy", "audio_preview")
DECORATION_KINDS = ("thumbnail", "filmstrip", "audio_peaks")
CLIP_ONLY_KINDS = ("frame_timing_index", "image_proxy", "packaged_font_face")
# How long a clip's create waits for the route's worker behind work that names no clip, before it
# is refused. In seconds.
HANDOFF_SECONDS = 5.0


def _authoring_wire(
    state: NleAuthoringState,
    asset_id: str,
    kind: str,
    end: int,
    *,
    start: int = 0,
    request_id: str = "prepare-1",
) -> dict[str, object]:
    manifest = build_nle_authoring_asset_manifest(state)
    return {
        "schema": NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
        "operation": "create",
        "requestId": request_id,
        "authoringSchema": state.schema,
        "profileId": state.profile_id,
        "workspaceHandle": state.workspace_handle,
        "workspaceRevision": state.workspace_revision,
        "timelineRevision": state.timeline_revision,
        "authoringFingerprint": state.authoring_fingerprint,
        "manifestFingerprint": manifest["manifestFingerprint"],
        "profileFingerprint": manifest["profileFingerprint"],
        "assetId": asset_id,
        "derivativeKind": kind,
        "ownerId": "nle-preparation-1-0",
        "runtimeEpoch": 1,
        "sourceStartFrame": start,
        "sourceEndFrame": end,
    }


def _decode(wire: dict[str, object]) -> CreateLeaseRequest:
    value = decode_lease_request(json.dumps(wire).encode())
    assert isinstance(value, CreateLeaseRequest)
    return value


def _with_assets(state: NleAuthoringState, **changes: Any) -> NleAuthoringState:
    """The fixture state with its first asset replaced; everything else as it was."""

    return create_nle_authoring_state(
        project_id=state.project_id,
        workspace_handle=state.workspace_handle,
        workspace_revision=state.workspace_revision,
        timeline_revision=state.timeline_revision,
        edit_capacity_frames=state.edit_capacity_frames,
        assets=(replace(state.assets[0], **changes), *state.assets[1:]),
        tracks=state.tracks,
        clips=(),
        audio_extension=state.audio_extension,
        blockers=state.blockers,
    )


# -- the request and its validator ---------------------------------------------------------------


def test_the_kinds_a_request_may_name_without_a_clip_are_two_closed_sets() -> None:
    from comfyui_h3_context.core import authoring_media as contract

    assert contract.ASSET_DECORATION_KINDS == set(DECORATION_KINDS)
    assert contract.ASSET_PREPARATION_KINDS == set(PLAYBACK_KINDS)
    # Every kind is in exactly one of the three groups this file names.
    assert sorted(KINDS) == sorted(DECORATION_KINDS + PLAYBACK_KINDS + CLIP_ONLY_KINDS)


@pytest.mark.parametrize("kind", ["video_proxy", "audio_preview"])
def test_the_authoring_request_admits_a_playback_kind_for_a_video_asset(kind: str) -> None:
    state = state_for(())
    wire = _authoring_wire(state, "vid-primary", kind, 48)
    request = _decode(wire)

    assert request.scope == "asset" and request.clip_id is None
    assert request.to_wire() == wire
    asset = validate_nle_authoring_asset_lease_request(request, state)
    assert asset.asset_id == "vid-primary" and asset is state.assets[0]


def test_a_video_asset_without_bound_audio_is_prepared_for_picture_only() -> None:
    state = state_for(())
    video = _decode(_authoring_wire(state, "vid-overlay", "video_proxy", 12))
    assert validate_nle_authoring_asset_lease_request(video, state).asset_id == "vid-overlay"
    audio = _decode(_authoring_wire(state, "vid-overlay", "audio_preview", 12))
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_nle_authoring_asset_lease_request(audio, state)


def test_bound_audio_without_a_sample_count_has_no_audio_preparation() -> None:
    whole = state_for(())
    # The authoring contract refuses this asset when a state is decoded. The request validator
    # does not rely on that: it is given the state directly, as a caller with a typed state can.
    state = replace(
        whole, assets=(replace(whole.assets[0], source_sample_count=None), *whole.assets[1:])
    )
    assert state.assets[0].embedded_audio == "present_bound"
    audio = _decode(_authoring_wire(state, "vid-primary", "audio_preview", 48))
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_nle_authoring_asset_lease_request(audio, state)
    video = _decode(_authoring_wire(state, "vid-primary", "video_proxy", 48))
    assert validate_nle_authoring_asset_lease_request(video, state).asset_id == "vid-primary"


@pytest.mark.parametrize("kind", ["audio_peaks", "audio_preview"])
@pytest.mark.parametrize("missing", ["the binding", "the sample count"])
def test_neither_audio_kind_is_made_for_an_asset_that_lacks_one_of_the_two(
    kind: str, missing: str
) -> None:
    whole = state_for(())
    # Each state lacks one thing only, so that each of the two conditions refuses alone. The
    # decoder makes neither state; the validator is handed a typed state and checks both.
    changed = (
        replace(whole.assets[0], embedded_audio="absent")
        if missing == "the binding"
        else replace(whole.assets[0], source_sample_count=None)
    )
    assert (changed.embedded_audio == "present_bound") != (changed.source_sample_count is not None)
    state = replace(whole, assets=(changed, *whole.assets[1:]))
    request = _decode(_authoring_wire(state, "vid-primary", kind, 48))
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_nle_authoring_asset_lease_request(request, state)
    # The picture of the same asset is not refused with it.
    for other in ("filmstrip", "video_proxy"):
        admitted = _decode(_authoring_wire(state, "vid-primary", other, 48))
        assert validate_nle_authoring_asset_lease_request(admitted, state) is changed


@pytest.mark.parametrize("kind", ["video_proxy", "audio_preview"])
@pytest.mark.parametrize("asset_id", ["img-overlay", "font-main"])
def test_an_image_and_a_font_have_no_playback_preparation(kind: str, asset_id: str) -> None:
    state = state_for(())
    request = _decode(_authoring_wire(state, asset_id, kind, 1))
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_nle_authoring_asset_lease_request(request, state)


@pytest.mark.parametrize("kind", DECORATION_KINDS + PLAYBACK_KINDS)
def test_a_font_is_refused_every_kind_a_request_may_name_without_a_clip(kind: str) -> None:
    state = state_for(())
    # A thumbnail is the one kind nothing but the asset's being a font refuses: an image has one.
    request = _decode(_authoring_wire(state, "font-main", kind, 1))
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_nle_authoring_asset_lease_request(request, state)
    if kind == "thumbnail":
        image = _decode(_authoring_wire(state, "img-overlay", kind, 1))
        assert validate_nle_authoring_asset_lease_request(image, state).asset_id == "img-overlay"


@pytest.mark.parametrize("kind", CLIP_ONLY_KINDS)
def test_the_validator_refuses_a_kind_the_request_cannot_carry(kind: str) -> None:
    state = state_for(())
    forged = _decode(_authoring_wire(state, "vid-primary", "video_proxy", 48))
    assert validate_nle_authoring_asset_lease_request(forged, state).asset_id == "vid-primary"
    # The request refuses these kinds when it is built (see below). The validator is handed a
    # request object and does not rely on that: the same request under another kind is refused.
    object.__setattr__(forged, "derivative_kind", kind)
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_nle_authoring_asset_lease_request(forged, state)


@pytest.mark.parametrize("kind", ["video_proxy", "audio_preview"])
def test_a_video_asset_without_one_timing_row_per_frame_is_not_prepared(kind: str) -> None:
    rows = tuple(TimingLandmark(frame, frame * 512, frame * 512, 512) for frame in range(47))
    state = _with_assets(state_for(()), landmarks=rows)
    assert state.assets[0].source_frame_count == 48
    request = _decode(_authoring_wire(state, "vid-primary", kind, 48))
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_nle_authoring_asset_lease_request(request, state)


@pytest.mark.parametrize("kind", ["video_proxy", "audio_preview"])
@pytest.mark.parametrize(("start", "end"), [(0, 1), (0, 47), (1, 48), (0, 49)])
def test_a_preparation_names_the_whole_source_and_nothing_else(
    kind: str, start: int, end: int
) -> None:
    state = state_for(())
    request = _decode(_authoring_wire(state, "vid-primary", kind, end, start=start))
    with pytest.raises(MediaLeaseError, match="authority_mismatch"):
        validate_nle_authoring_asset_lease_request(request, state)


@pytest.mark.parametrize("kind", CLIP_ONLY_KINDS)
def test_the_authoring_request_still_refuses_every_other_kind(kind: str) -> None:
    state = state_for(())
    with pytest.raises(MediaLeaseError, match="unsupported"):
        _decode(_authoring_wire(state, "vid-primary", kind, 48))


def test_a_preparation_for_an_earlier_authoring_state_is_stale() -> None:
    state = state_for(())
    wire = _authoring_wire(state, "vid-primary", "video_proxy", 48)
    wire["authoringFingerprint"] = "sha256:" + "7" * 64
    with pytest.raises(MediaLeaseError, match="stale"):
        validate_nle_authoring_asset_lease_request(_decode(wire), state)


@pytest.mark.parametrize("kind", ["video_proxy", "audio_preview"])
def test_the_snapshot_request_asset_scope_still_refuses_a_playback_kind(kind: str) -> None:
    wire = create_wire()
    wire.update(scope="asset", clipId=None, derivativeKind=kind)
    with pytest.raises(MediaLeaseError, match="unsupported"):
        _decode(wire)
    # The three decorations are what that form names without a clip, as before.
    wire.update(derivativeKind="thumbnail", sourceEndFrame=1)
    assert _decode(wire).scope == "asset"


# -- the claim: real workspace, a generated source, no media tools -------------------------------


class _Workspace:
    """One imported video asset in the real registries; the media adapter's probe is a fixture."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        production, projection, self.authoring, before, history, _store = (
            _registries_with_ready_output(
                tmp_path, frame_count=FRAMES, initial_source_binding=False
            )
        )
        self.adapter = _qualified_test_adapter(tmp_path, monkeypatch, frame_count=FRAMES)
        request = _import_request(projection, before, history)
        imported = self.authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=self.adapter,
            deadline=time.monotonic() + 5.0,
        )
        self.handle = request.authoring_workspace_handle
        self.asset_id = imported.receipt.rows[0].asset_id
        self.generator = AuthoringDerivativeGenerator.for_images()
        self._sequence = 0

    def state(self) -> NleAuthoringState:
        history = self.authoring._entries[self.handle].timeline_history_v2
        assert history is not None
        return history.authoring

    def insert(self, track_kind: str) -> tuple[PublicCompositionSnapshot, str]:
        self._sequence += 1
        request_id = f"insert.{self._sequence}"
        reply = self.authoring.dispatch(
            _authoring_action(
                f"read.{self._sequence}", "read_timeline_history", workspace_handle=self.handle
            )
        )
        assert reply.body is not None
        state = cast(dict[str, Any], reply.body["authoring"])
        track_id = next(
            (row["track_id"] for row in state["tracks"] if row["kind"] == track_kind), None
        )
        action = _authoring_action(request_id, "apply_timeline_transaction")
        transaction = _insert_asset_transaction(
            authoring_state=state,
            asset_id=self.asset_id,
            track_id=track_id or "track." + request_id,
            request_id=request_id,
        )
        if track_id is None:
            # A new workspace has the primary track only; an overlay clip brings its track.
            cast(list[dict[str, object]], transaction["commands"]).insert(
                0,
                {
                    "kind": "create_track",
                    "payload": {
                        "track_id": "track." + request_id,
                        "kind": track_kind,
                        "order": 1 + max(row["order"] for row in state["tracks"]),
                    },
                },
            )
        action["payload"] = transaction
        accepted = self.authoring.dispatch(action)
        assert accepted.status == 200 and accepted.body is not None, accepted.body
        return decode_public_snapshot(accepted.body["render_snapshot"]), "clip." + request_id

    def asset_request(self, kind: str, *, end: int = FRAMES) -> CreateLeaseRequest:
        return _decode(_authoring_wire(self.state(), self.asset_id, kind, end))

    def clip_request(
        self, snapshot: PublicCompositionSnapshot, clip_id: str, kind: DerivativeKind, end: int
    ) -> CreateLeaseRequest:
        return CreateLeaseRequest(
            f"clip-{kind}-{clip_id}",
            self.handle,
            snapshot.workspace_revision,
            snapshot.timeline_revision,
            snapshot.public_fingerprint,
            public_asset_manifest_fingerprint(snapshot),
            RUNTIME_PROFILE_FINGERPRINT,
            "clip",
            clip_id,
            self.asset_id,
            kind,
            clip_id,
            1,
            0,
            end,
        )

    def admit(self, request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        return self.authoring.admit_media_derivative(
            request, generator=self.generator, media_adapter=self.adapter
        )


def test_a_prepared_playback_body_has_the_key_of_the_clip_that_uses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _Workspace(tmp_path, monkeypatch)
    prepared = workspace.admit(workspace.asset_request("video_proxy"))
    assert prepared.current()

    snapshot, clip_id = workspace.insert("primary_video")
    # The asset was prepared for the state before the clip existed; that claim has ended with it.
    assert not prepared.current()
    used = workspace.admit(workspace.clip_request(snapshot, clip_id, "video_proxy", FRAMES))
    assert used.cache_key == prepared.cache_key

    # The asset is prepared again under the state that has the clip, and nothing about the key
    # depends on which state asked.
    again = workspace.admit(workspace.asset_request("video_proxy"))
    assert again.current() and again.cache_key == prepared.cache_key


def test_an_asset_without_audio_shares_its_prepared_picture_with_an_overlay_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _Workspace(tmp_path, monkeypatch)
    assert workspace.state().assets[0].embedded_audio == "absent"
    prepared = workspace.admit(workspace.asset_request("video_proxy"))
    snapshot, clip_id = workspace.insert("video_overlay")
    overlay = workspace.admit(workspace.clip_request(snapshot, clip_id, "video_proxy", FRAMES))
    assert overlay.cache_key == prepared.cache_key


def test_a_decoration_key_still_names_its_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _Workspace(tmp_path, monkeypatch)
    snapshot, clip_id = workspace.insert("primary_video")
    for_the_asset = workspace.admit(workspace.asset_request("thumbnail", end=1))
    for_the_clip = workspace.admit(workspace.clip_request(snapshot, clip_id, "thumbnail", 1))
    # Same source, same frame, same profile: only the scope differs, and it is in the key.
    assert for_the_asset.cache_key != for_the_clip.cache_key


def test_the_workspace_refuses_an_audio_preparation_for_an_asset_without_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _Workspace(tmp_path, monkeypatch)
    with pytest.raises(MediaLeaseError, match="unsupported"):
        workspace.admit(workspace.asset_request("audio_preview"))


def test_a_playback_body_without_a_clip_is_refused_outside_the_authoring_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _Workspace(tmp_path, monkeypatch)
    snapshot, clip_id = workspace.insert("primary_video")
    forged = workspace.clip_request(snapshot, clip_id, "video_proxy", FRAMES)
    object.__setattr__(forged, "scope", "asset")
    object.__setattr__(forged, "clip_id", None)
    # The request and the snapshot validator both refuse this form. The claim does not rely on
    # them: with the validator out of the way it still refuses a playback body without a clip.
    monkeypatch.setattr(
        claim_module,
        "validate_lease_snapshot",
        lambda request, issued: next(
            asset for asset in issued.assets if asset.asset_id == request.asset_id
        ),
    )
    with pytest.raises(MediaLeaseError, match="authority_mismatch"):
        workspace.admit(forged)


# -- the lease pool: a preparation is a decoration -----------------------------------------------


class _PoolClaim(Claim):
    def __init__(self, request: CreateLeaseRequest, builds: list[str]) -> None:
        super().__init__()
        self._request = request
        self._builds = builds
        self.cache_key = f"{request.scope}:{request.derivative_kind}:{request.owner_id}"

    def generate(
        self, deadline: float, cancellation: object
    ) -> tuple[bytearray, VerifiedDerivative]:
        _, facts = super().generate(deadline, cancellation)
        kind = self._request.derivative_kind
        body = bytearray(f"body-{self._request.request_id}".encode())
        self._builds.append(self._request.request_id)
        return body, replace(
            facts,
            kind=kind,
            byte_count=len(body),
            audio_disposition="present_bound" if kind == "audio_preview" else "absent",
            derivative=replace(
                facts.derivative,
                derivative_fingerprint="sha256:" + hashlib.sha256(body).hexdigest(),
            ),
        )


def _pool() -> tuple[MediaLeaseAuthority, list[str]]:
    builds: list[str] = []
    return (
        MediaLeaseAuthority(lambda request: _PoolClaim(request, builds), start_reaper=False),
        builds,
    )


def _clip(kind: str, index: int) -> CreateLeaseRequest:
    wire = create_wire()
    wire.update(
        requestId=f"clip-{index}",
        ownerId=f"clip-{index}",
        derivativeKind=kind,
        sourceEndFrame=12 if kind in PLAYBACK_KINDS else 1,
    )
    return _decode(wire)


def _asset(kind: str, index: int) -> CreateLeaseRequest:
    """An authoring-bound asset request for the pool's workspace; a preparation or a decoration."""

    return CreateLeaseRequest(
        request_id=f"asset-{index}",
        workspace_handle=cast(str, create_wire()["workspaceHandle"]),
        workspace_revision=1,
        timeline_revision=1,
        public_fingerprint=None,
        manifest_fingerprint="sha256:" + "2" * 64,
        profile_fingerprint=RUNTIME_PROFILE_FINGERPRINT,
        scope="asset",
        clip_id=None,
        asset_id="asset-1",
        derivative_kind=cast(DerivativeKind, kind),
        owner_id=f"asset-{index}",
        runtime_epoch=1,
        source_start_frame=0,
        source_end_frame=12 if kind in PLAYBACK_KINDS else 1,
        request_schema=NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
        authoring_schema=NLE_AUTHORING_SCHEMA,
        authoring_profile_id=NLE_AUTHORING_PROFILE_ID,
        authoring_fingerprint="sha256:" + "4" * 64,
    )


@pytest.mark.parametrize("kind", ["video_proxy", "audio_preview"])
def test_a_preparation_leaves_two_slots_of_the_workspace_to_playback(kind: str) -> None:
    authority, builds = _pool()
    try:
        for index in range(1, 7):
            authority.create(_clip("thumbnail", index))
        before = list(builds)
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_asset(kind, 7))
        # Refused before anything was generated for it, and playback still has its two slots.
        assert builds == before
        authority.create(_clip("thumbnail", 8))
        authority.create(_clip("thumbnail", 9))
        assert authority.resources()["leases"] == 8
    finally:
        authority.close()


def test_a_preparation_is_one_of_the_two_decoration_leases() -> None:
    authority, _ = _pool()
    try:
        prepared, capability = authority.create(_asset("video_proxy", 1))
        authority.create(_asset("thumbnail", 2))
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_asset("audio_preview", 3))
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_asset("thumbnail", 4))
        # Released, it gives its slot back: a preparation holds a lease for one round trip.
        authority.release(release_command(prepared), capability)
        authority.create(_asset("audio_preview", 5))
    finally:
        authority.close()


def test_a_video_preparation_is_counted_among_the_three_video_leases() -> None:
    authority, _ = _pool()
    try:
        for index in range(1, 4):
            authority.create(_clip("video_proxy", index))
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_asset("video_proxy", 4))
        # The cap is about video bodies; the audio of the same asset is still prepared.
        authority.create(_asset("audio_preview", 5))
    finally:
        authority.close()


def test_a_released_video_preparation_gives_its_video_lease_back() -> None:
    authority, _ = _pool()
    try:
        authority.create(_clip("video_proxy", 1))
        authority.create(_clip("video_proxy", 2))
        prepared, capability = authority.create(_asset("video_proxy", 3))
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(_clip("video_proxy", 4))
        authority.release(release_command(prepared), capability)
        authority.create(_clip("video_proxy", 5))
    finally:
        authority.close()


# -- the route: playback takes the worker from a preparation -------------------------------------


def test_clip_playback_takes_the_worker_from_an_abandoned_preparation() -> None:
    web = pytest.importorskip("aiohttp.web")
    http_test = pytest.importorskip("aiohttp.test_utils")
    from test_m25_media_derivative_routes import same_origin

    from comfyui_h3_context.adapters.comfyui_authoring_media_leases import (
        LEASE_ROUTE,
        MediaLeaseRouteService,
    )

    async def run() -> None:
        started = threading.Event()
        cancelled = threading.Event()
        allow_release = threading.Event()

        class PreparingClaim(Claim):
            def generate(
                self, deadline: float, cancellation: Any
            ) -> tuple[bytearray, VerifiedDerivative]:
                started.set()
                while not cancellation.is_cancelled() and time.monotonic() < deadline:
                    time.sleep(0.01)
                cancelled.set()
                while not allow_release.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return super().generate(deadline, cancellation)

        def factory(request: CreateLeaseRequest) -> Claim:
            return PreparingClaim() if request.scope == "asset" else Claim()

        authority = MediaLeaseAuthority(factory, start_reaper=False)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        async with http_test.TestClient(http_test.TestServer(app)) as client:
            headers = same_origin(client)
            preparation = _asset("video_proxy", 1).to_wire()
            pending = asyncio.create_task(
                client.post(LEASE_ROUTE, json=preparation, headers=headers)
            )
            cutoff = time.monotonic() + 3
            while not started.is_set() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert started.is_set()
            # The page aborts its preparation when playback needs the worker.
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            playback = create_wire()
            playback["requestId"] = "playback-request"
            waiting = asyncio.create_task(client.post(LEASE_ROUTE, json=playback, headers=headers))
            cutoff = time.monotonic() + 3
            while not service._playback_handoff.locked() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            # The clip waits for the worker instead of being refused, and the preparation was
            # told to stop.
            assert service._playback_handoff.locked()
            while not cancelled.is_set() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert cancelled.is_set()

            allow_release.set()
            admitted = await waiting
            assert admitted.status == 200
            assert (await admitted.json())["requestId"] == "playback-request"
            # The clip holds the only lease: the abandoned preparation left none behind.
            assert authority.resources()["leases"] == 1
        service.close()

    asyncio.run(run())


def test_a_preparation_nobody_aborts_keeps_the_worker_until_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = pytest.importorskip("aiohttp.web")
    http_test = pytest.importorskip("aiohttp.test_utils")
    from test_m25_media_derivative_routes import same_origin

    import comfyui_h3_context.adapters.comfyui_authoring_media_leases as route_module
    from comfyui_h3_context.adapters.comfyui_authoring_media_leases import (
        CAPABILITY_HEADER,
        LEASE_ROUTE,
        MediaLeaseRouteService,
    )
    from comfyui_h3_context.core.authoring_media import REQUEST_SCHEMA

    # The product's bound is the stated one. One request below is given a shorter bound, so that
    # its refusal is not waited for; no behaviour here tells five seconds from five hundred, and
    # this line does.
    assert route_module._PLAYBACK_HANDOFF_SECONDS == HANDOFF_SECONDS
    shortened_bound = 0.4

    async def run() -> None:
        started = threading.Event()
        told_to_stop = threading.Event()
        finish = threading.Event()
        builds: list[str] = []

        class PreparingClaim(_PoolClaim):
            def generate(
                self, deadline: float, cancellation: Any
            ) -> tuple[bytearray, VerifiedDerivative]:
                started.set()
                while not finish.is_set() and time.monotonic() < deadline:
                    if cancellation.is_cancelled():
                        told_to_stop.set()
                    time.sleep(0.01)
                return super().generate(deadline, cancellation)

        def factory(request: CreateLeaseRequest) -> Claim:
            double = PreparingClaim if request.scope == "asset" else _PoolClaim
            return double(request, builds)

        authority = MediaLeaseAuthority(factory, start_reaper=False)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        app.router.add_post(LEASE_ROUTE + "/open", service.open)

        async def exercise(client: Any) -> None:
            headers = same_origin(client)
            # A lease an owner holds, as the monitor holds its picture: it is renewed on a timer
            # that knows nothing of what else the worker is doing.
            made = await client.post(
                LEASE_ROUTE, json=_clip("video_proxy", 1).to_wire(), headers=headers
            )
            assert made.status == 200, await made.text()
            held = await made.json()
            owner = {**headers, CAPABILITY_HEADER: made.headers[CAPABILITY_HEADER]}
            handed_on: dict[str, object] = {"nextOwnerId": "clip-1-moved", "nextRuntimeEpoch": 2}

            def command(operation: str, request_id: str, **more: object) -> dict[str, object]:
                return {
                    "schema": REQUEST_SCHEMA,
                    "operation": operation,
                    "requestId": request_id,
                    **{
                        key: held[key] for key in ("leaseId", "revision", "ownerId", "runtimeEpoch")
                    },
                    **more,
                }

            pending = asyncio.create_task(
                client.post(LEASE_ROUTE, json=_asset("video_proxy", 1).to_wire(), headers=headers)
            )
            cutoff = time.monotonic() + 3
            while not started.is_set() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert started.is_set()

            # An owner's renewal, open and transfer are answered at once, and refused: behind a
            # preparation only a clip's `create` waits for the worker. The page has to ask again.
            owner_commands: tuple[tuple[str, str, dict[str, object]], ...] = (
                ("renew", LEASE_ROUTE, {}),
                ("open", LEASE_ROUTE + "/open", {}),
                ("transfer", LEASE_ROUTE, handed_on),
            )
            for operation, route, more in owner_commands:
                asked = time.monotonic()
                refused = await client.post(
                    route, json=command(operation, f"{operation}-refused", **more), headers=owner
                )
                assert refused.status == 429, operation
                assert (await refused.json())["reason"] == "busy", operation
                assert time.monotonic() - asked < HANDOFF_SECONDS / 2, operation

            # A clip's create that the preparation outlasts waits for the bound and is refused
            # after it, and neither its wait nor its refusal tells the preparation to stop.
            with monkeypatch.context() as patched:
                patched.setattr(route_module, "_PLAYBACK_HANDOFF_SECONDS", shortened_bound)
                asked = time.monotonic()
                outlasted = await asyncio.wait_for(
                    client.post(
                        LEASE_ROUTE, json=_clip("audio_preview", 3).to_wire(), headers=headers
                    ),
                    HANDOFF_SECONDS / 2,
                )
                waited = time.monotonic() - asked
            assert outlasted.status == 429, await outlasted.text()
            assert (await outlasted.json())["reason"] == "busy"
            assert waited >= shortened_bound
            assert not pending.done() and not told_to_stop.is_set()
            assert builds == ["clip-1"]

            # A clip asks for its sound without the page having aborted the preparation. It is
            # not refused and the preparation is not told to stop: the clip waits for it.
            waiting = asyncio.create_task(
                client.post(LEASE_ROUTE, json=_clip("audio_preview", 2).to_wire(), headers=headers)
            )
            cutoff = time.monotonic() + 3
            while not service._playback_handoff.locked() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert service._playback_handoff.locked()
            await asyncio.sleep(0.1)
            assert not waiting.done() and not pending.done()
            assert not told_to_stop.is_set()
            assert builds == ["clip-1"]

            finish.set()
            prepared = await pending
            assert prepared.status == 200, await prepared.text()
            assert (await prepared.json())["requestId"] == "asset-1"
            admitted = await waiting
            assert admitted.status == 200, await admitted.text()
            assert (await admitted.json())["requestId"] == "clip-2"
            # The preparation was generated to its end, and the clip's sound only after it.
            assert builds == ["clip-1", "asset-1", "clip-2"]

            # The refusals changed nothing. The open and the renewal are accepted now as they were
            # sent, from the same owner under the same capability; a transfer of the revision the
            # renewal made then hands the lease on under another one.
            opened = await client.post(
                LEASE_ROUTE + "/open", json=command("open", "open-again"), headers=owner
            )
            assert opened.status == 200, await opened.text()
            assert await opened.read() == b"body-clip-1"
            renewed = await client.post(
                LEASE_ROUTE, json=command("renew", "renew-again"), headers=owner
            )
            assert renewed.status == 200, await renewed.text()
            renewal = await renewed.json()
            assert renewal["leaseId"] == held["leaseId"]
            moved = await client.post(
                LEASE_ROUTE,
                json={
                    **command("transfer", "transfer-again", **handed_on),
                    "revision": renewal["revision"],
                },
                headers=owner,
            )
            assert moved.status == 200, await moved.text()
            receipt = await moved.json()
            assert (receipt["ownerId"], receipt["runtimeEpoch"]) == ("clip-1-moved", 2)
            assert moved.headers[CAPABILITY_HEADER] != owner[CAPABILITY_HEADER]
            # The clip that was refused left no lease and built nothing.
            assert authority.resources()["leases"] == 3
            assert builds == ["clip-1", "asset-1", "clip-2"]

        try:
            async with http_test.TestClient(http_test.TestServer(app)) as client:
                await exercise(client)
        finally:
            # A failed assertion must not leave the worker waiting out the request's deadline.
            finish.set()
            service.close()

    asyncio.run(run())


def test_a_clip_create_does_not_wait_behind_another_clips_work() -> None:
    """The worker is waited for only behind work that names no clip.

    Behind a clip's own generation a second clip's create is refused at once, as every other
    request is.
    """

    web = pytest.importorskip("aiohttp.web")
    http_test = pytest.importorskip("aiohttp.test_utils")
    from test_m25_media_derivative_routes import same_origin

    from comfyui_h3_context.adapters.comfyui_authoring_media_leases import (
        LEASE_ROUTE,
        MediaLeaseRouteService,
    )

    async def run() -> None:
        started = threading.Event()
        finish = threading.Event()
        builds: list[str] = []

        class GeneratingClaim(_PoolClaim):
            def generate(
                self, deadline: float, cancellation: Any
            ) -> tuple[bytearray, VerifiedDerivative]:
                started.set()
                while not finish.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return super().generate(deadline, cancellation)

        def factory(request: CreateLeaseRequest) -> Claim:
            double = GeneratingClaim if request.owner_id == "clip-1" else _PoolClaim
            return double(request, builds)

        authority = MediaLeaseAuthority(factory, start_reaper=False)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)

        async def exercise(client: Any) -> None:
            headers = same_origin(client)
            pending = asyncio.create_task(
                client.post(LEASE_ROUTE, json=_clip("video_proxy", 1).to_wire(), headers=headers)
            )
            cutoff = time.monotonic() + 3
            while not started.is_set() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert started.is_set()

            asked = time.monotonic()
            refused = await client.post(
                LEASE_ROUTE, json=_clip("audio_preview", 2).to_wire(), headers=headers
            )
            assert refused.status == 429, await refused.text()
            assert (await refused.json())["reason"] == "busy"
            assert time.monotonic() - asked < HANDOFF_SECONDS / 2
            assert not pending.done()

            finish.set()
            made = await pending
            assert made.status == 200, await made.text()
            assert builds == ["clip-1"]
            assert authority.resources()["leases"] == 1

        try:
            async with http_test.TestClient(http_test.TestServer(app)) as client:
                await exercise(client)
        finally:
            finish.set()
            service.close()

    asyncio.run(run())


# -- two bounds of the authority that preparation leans on ---------------------------------------


def test_a_create_that_cannot_fit_beside_the_leased_bodies_ends_no_idle_body(
    retaining: Setup,  # noqa: F811
) -> None:
    store, workspace, _ = retaining
    video = derivative_byte_limit("video_proxy")
    image = derivative_byte_limit("image_proxy")
    assert 2 * video + 5 * image == MAX_CACHE_BYTES
    plan: list[tuple[str, DerivativeKind, int]] = [
        ("clip-video-1", "video_proxy", video),
        ("clip-video-2", "video_proxy", video),
        ("clip-image-1", "image_proxy", image),
        ("clip-image-2", "image_proxy", image),
        ("clip-image-3", "image_proxy", image),
        ("clip-image-4", "image_proxy", image),
        ("clip-image-5", "image_proxy", image),
        ("clip-video-3", "video_proxy", video),
    ]
    workspace.assets = {owner: (owner, size) for owner, _kind, size in plan}
    held = [store.create(lease_request(kind, owner)) for owner, kind, _size in plan[:7]]
    receipt, capability = held.pop()
    store.release(release_command(receipt), capability)
    assert store.retained() == {"entries": 1, "bytes": image}
    idle_body = store._cache["image_proxy-clip-image-5"].body

    # A third video body does not fit beside what is leased, with or without the idle body. It
    # is refused, and the idle body, which a later lease would otherwise generate again, stays.
    with pytest.raises(MediaLeaseError, match="resource_limit"):
        store.create(lease_request("video_proxy", "clip-video-3"))
    assert store.retained() == {"entries": 1, "bytes": image}
    assert len(idle_body) == image


def test_a_create_that_fits_exactly_once_the_idle_body_is_gone_evicts_it(
    retaining: Setup,  # noqa: F811
) -> None:
    store, workspace, _ = retaining
    video = derivative_byte_limit("video_proxy")
    image = derivative_byte_limit("image_proxy")
    plan: list[tuple[str, DerivativeKind, int]] = [
        ("clip-video-1", "video_proxy", video),
        ("clip-video-2", "video_proxy", video),
        ("clip-image-1", "image_proxy", image),
        ("clip-image-2", "image_proxy", image),
        ("clip-image-3", "image_proxy", image),
        ("clip-image-4", "image_proxy", image),
        ("clip-image-5", "image_proxy", image),
        ("clip-image-6", "image_proxy", image),
    ]
    workspace.assets = {owner: (owner, size) for owner, _kind, size in plan}
    held = [store.create(lease_request(kind, owner)) for owner, kind, _size in plan[:7]]
    receipt, capability = held.pop()
    store.release(release_command(receipt), capability)
    idle_body = store._cache["image_proxy-clip-image-5"].body

    # Leased bytes plus the new body are the budget to the byte: it fits, and only without the
    # idle body, so that one is what makes room.
    store.create(lease_request("image_proxy", "clip-image-6"))
    assert store.resources()["cacheBytes"] == MAX_CACHE_BYTES
    assert store.retained() == NOTHING_IDLE
    assert len(idle_body) == 0


def test_one_failed_pass_does_not_end_the_reaper_thread() -> None:
    authority = MediaLeaseAuthority(lambda _request: Claim())
    passes: list[int] = []
    later = threading.Event()
    reap = authority.reap

    def failing_once() -> None:
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("a pass failed")
        later.set()
        reap()

    try:
        thread = authority._thread
        assert thread is not None and thread.is_alive()
        authority.reap = failing_once  # type: ignore[method-assign]
        cutoff = time.monotonic() + 5
        while not later.is_set() and time.monotonic() < cutoff:
            authority._wake.set()
            time.sleep(0.01)
        # The pass that raised was counted, and the same thread went on to the next one.
        assert later.is_set()
        assert thread.is_alive() and authority._thread is thread
        assert authority._reaper_faults == 1
    finally:
        authority.reap = reap  # type: ignore[method-assign]
        authority.close()
    assert not thread.is_alive()


def _failed_passes(authority: MediaLeaseAuthority, wanted: int) -> None:
    """Make the next `wanted` passes of the authority's own thread fail, and wait for them."""

    reap = authority.reap
    passed = threading.Event()

    def failing() -> None:
        if authority._reaper_faults < wanted:
            # What a fault of the filesystem looks like: the message names a private place.
            raise OSError("X:/private-session/take-1.mp4 could not be read")
        passed.set()

    authority.reap = failing  # type: ignore[method-assign]
    try:
        cutoff = time.monotonic() + 10
        while not passed.is_set() and time.monotonic() < cutoff:
            authority._wake.set()
            time.sleep(0.005)
        assert passed.is_set() and authority._reaper_faults == wanted
    finally:
        authority.reap = reap  # type: ignore[method-assign]


def test_a_failed_pass_is_reported_by_its_type_and_its_count_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(leases_module, "_REAPER_FAULT_REPORT_EVERY", 3)
    caplog.set_level(logging.DEBUG, logger=leases_module.__name__)
    authority = MediaLeaseAuthority(lambda _request: Claim())
    try:
        _failed_passes(authority, 7)
    finally:
        authority.close()

    reported = [record for record in caplog.records if record.name == leases_module.__name__]
    # The first failed pass and one in every three after it; the passes between are counted only.
    assert [record.getMessage() for record in reported] == [
        f"H3 Context media lease expiry pass failed: kind=OSError count={count}"
        for count in (1, 4, 7)
    ]
    assert {record.levelno for record in reported} == {logging.WARNING}
    # Nothing of the fault's own text: neither in the line nor as a traceback beside it.
    assert all(record.exc_info is None and record.stack_info is None for record in reported)
    assert "private-session" not in caplog.text and "take-1" not in caplog.text


def test_a_fault_that_persists_is_reported_at_least_every_ten_minutes() -> None:
    # One pass a second while nothing streams: the period between two reports, in seconds.
    period = leases_module._REAPER_FAULT_REPORT_EVERY * leases_module._IDLE_REAP_SECONDS
    assert 60 <= period <= 600


def test_a_report_that_fails_does_not_end_the_reaper_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[object, ...]] = []

    class Refusing:
        def warning(self, *arguments: object) -> None:
            asked.append(arguments)
            raise ValueError("a logging filter of the host raised")

    monkeypatch.setattr(leases_module, "_LOGGER", Refusing())
    authority = MediaLeaseAuthority(lambda _request: Claim())
    try:
        thread = authority._thread
        assert thread is not None
        _failed_passes(authority, 2)
        # The report was attempted for the first failed pass and raised; the thread ran on.
        assert len(asked) == 1 and asked[0][1:] == ("OSError", 1)
        assert thread.is_alive() and authority._thread is thread
    finally:
        authority.close()


def test_a_report_being_written_holds_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A log write can wait: a console whose text is being selected holds every write to it."""

    writing = threading.Event()
    written = threading.Event()

    class Waiting:
        def warning(self, *_arguments: object) -> None:
            writing.set()
            written.wait(10)

    def failing() -> None:
        raise OSError("a pass failed")

    monkeypatch.setattr(leases_module, "_LOGGER", Waiting())
    authority = MediaLeaseAuthority(lambda _request: Claim())
    reap = authority.reap
    answers: list[dict[str, int]] = []
    asking = threading.Thread(target=lambda: answers.append(authority.resources()), daemon=True)
    try:
        authority.reap = failing  # type: ignore[method-assign]
        authority._wake.set()
        assert writing.wait(5), "the failed pass was never reported"
        # The expiry thread is inside the write. Every request takes the authority's lock: held
        # across the write, it would stop them all for as long as the write waits.
        asking.start()
        asking.join(5)
        assert len(answers) == 1, "a request waited behind the report being written"
    finally:
        written.set()
        authority.reap = reap  # type: ignore[method-assign]
        authority.close()
