"""Retention through the accepted modules: the workspace registry, its claims and its sources.

The doubles in `test_m25_74_derivative_retention` fix the authority's rules. These tests fix what
the product's own claim offers the authority: whose liveness keeps a body, which failure withdraws
it, and that a released workspace leaves nothing behind.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_m25_29_production_authoring_import import (
    _authoring_action,
    _import_request,
    _insert_asset_transaction,
    _registries_with_ready_output,
)
from test_m25_74_derivative_rework_counts import (
    FRAMES,
    HEIGHT,
    WIDTH,
    _create,
    _create_request,
    _encode_source,
    _product_authority,
)
from test_m25_authoring_audio_rate_compatibility import _qualified_tools
from test_m25_authoring_initialization import action, image_workspace, initialize, insert_image_clip
from test_m25_composition_contract import fixture_snapshot, resign
from test_m25_media_derivative_source import request_for

import comfyui_h3_context.adapters.authoring_media_leases as leases_module
from comfyui_h3_context.adapters.authoring_derivative_generator import AuthoringDerivativeGenerator
from comfyui_h3_context.adapters.authoring_derivative_source import (
    AuthoringDerivativeClaim,
    _packaged_font_is_resident,
)
from comfyui_h3_context.adapters.authoring_fonts import (
    AuthoringFontError,
    load_packaged_font_manifest,
)
from comfyui_h3_context.adapters.authoring_generated_source import GeneratedAuthoringVideoSource
from comfyui_h3_context.adapters.authoring_image_source import OwnedImageSource
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.adapters.authoring_render_source import PreparedAuthoringHistory
from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingError,
    _PathBackedAuthoringVideoSource,
    claim_transferred_authoring_source,
)
from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkspaceRegistry
from comfyui_h3_context.core import AcceptedIntentAuthority, revise_workspace
from comfyui_h3_context.core.authoring_media import (
    RETAINED_DERIVATIVE_IDLE_SECONDS,
    CreateLeaseRequest,
    LeaseCommand,
    MediaLeaseError,
)
from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_TRANSACTION_SCHEMA_V2,
)

EMPTY = {"leases": 0, "cacheEntries": 0, "cacheBytes": 0, "activeReads": 0}


class _ImageTimeline:
    """One image clip on a real authoring workspace, and the accepted edits made to it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace, projection, binding = image_workspace(monkeypatch)
        assert binding is not None
        self.workspace: AuthoringWorkspaceRegistry = workspace
        self.handle = cast(str, projection["workspace_handle"])
        self.binding = binding
        initialized = workspace.dispatch(initialize(projection))
        assert initialized.body is not None
        self._state = insert_image_clip(
            workspace,
            cast(dict[str, Any], initialized.body["authoring"]),
            request_id="retention-insert",
            clip_id="clip-image",
        )
        self._requests = iter(range(1, 1000))
        self.generator = AuthoringDerivativeGenerator.for_images()
        self.generated = 0
        original = self.generator.generate_image_proxy

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.generated += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(self.generator, "generate_image_proxy", counted)

    def snapshot(self) -> PublicCompositionSnapshot:
        history = self.workspace.dispatch(
            action(
                f"retention-read-{next(self._requests)}",
                "read_timeline_history",
                workspace_handle=self.handle,
            )
        )
        assert history.body is not None and history.body["render_snapshot"] is not None
        return decode_public_snapshot(cast(dict[str, Any], history.body["render_snapshot"]))

    def edit(self, kind: str, **payload: object) -> None:
        request_id = f"retention-edit-{next(self._requests)}"
        state = self._state
        result = self.workspace.dispatch(
            action(
                request_id,
                "apply_timeline_transaction",
                schema=TIMELINE_TRANSACTION_SCHEMA_V2,
                authoring_schema=NLE_AUTHORING_SCHEMA,
                profile_id=NLE_AUTHORING_PROFILE_ID,
                operation_profile_id=NLE_OPERATION_PROFILE_ID,
                request_id=request_id,
                transaction_id="tx-" + request_id,
                workspace_handle=self.handle,
                expected_workspace_revision=state["workspace_revision"],
                expected_timeline_revision=state["timeline_revision"],
                expected_timeline_fingerprint=state["timeline_fingerprint"],
                expected_authoring_fingerprint=state["authoring_fingerprint"],
                commands=[{"kind": kind, "payload": {"clip_id": "clip-image", **payload}}],
            )
        )
        assert result.status == 200 and result.body is not None, result.body
        self._state = cast(dict[str, Any], result.body["authoring"])

    def release(self) -> None:
        released = self.workspace.dispatch(
            action("retention-release", "release_workspace", workspace_handle=self.handle)
        )
        assert released.status == 204
        assert self.binding.released

    def admit(self, request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        return self.workspace.admit_media_derivative(
            request, generator=self.generator, media_adapter=None
        )

    def request(self, request_id: str) -> CreateLeaseRequest:
        return replace(
            request_for(self.snapshot(), "clip-image", "image_1", "image_proxy"),
            request_id=request_id,
        )

    def authority(self, *, start_reaper: bool) -> MediaLeaseAuthority:
        # The lease route's own composition, over this workspace instead of the process registry.
        return MediaLeaseAuthority(
            self.admit,
            retention_seconds=RETAINED_DERIVATIVE_IDLE_SECONDS,
            start_reaper=start_reaper,
        )


def _command(operation: Any, receipt: dict[str, object]) -> LeaseCommand:
    return LeaseCommand(
        operation,
        "retention-" + operation,
        cast(str, receipt["leaseId"]),
        cast(int, receipt["revision"]),
        cast(str, receipt["ownerId"]),
        cast(int, receipt["runtimeEpoch"]),
    )


def _open(receipt: dict[str, object]) -> LeaseCommand:
    return _command("open", receipt)


def test_an_accepted_edit_keeps_the_body_and_regenerates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = _ImageTimeline(monkeypatch)
    authority = timeline.authority(start_reaper=False)
    try:
        first, capability = authority.create(timeline.request("first"))
        read = authority.open(_open(first), capability)
        assert read.read_chunk().startswith(b"\x89PNG")
        assert timeline.generated == 1

        timeline.edit("set_opacity_blend", opacity_bp=7000, blend="screen")
        authority.reap()
        # The edit ended the lease and its stream, and nothing else.
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            read.read_chunk()
        assert authority.resources()["leases"] == 0
        assert authority.retained() == {"entries": 1, "bytes": first["byteCount"]}

        second, capability = authority.create(timeline.request("second"))
        assert timeline.generated == 1
        assert second["derivativeFingerprint"] == first["derivativeFingerprint"]
        assert second["leaseId"] != first["leaseId"]
        assert authority.retained() == {"entries": 0, "bytes": 0}
        again = authority.open(_open(second), capability)
        assert again.read_chunk().startswith(b"\x89PNG")
        again.discard()

        # A second edit while the lease is live, then a third before anything asks again.
        timeline.edit("set_clip_enabled", enabled=False)
        timeline.edit("set_clip_enabled", enabled=True)
        third, _ = authority.create(timeline.request("third"))
        assert timeline.generated == 1
        assert third["derivativeFingerprint"] == first["derivativeFingerprint"]
        assert authority.resources()["leases"] == 1
        assert authority.resources()["cacheEntries"] == 1
    finally:
        authority.close()
        timeline.release()
    assert authority.resources() == EMPTY


def test_a_superseded_request_never_receives_the_retained_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = _ImageTimeline(monkeypatch)
    authority = timeline.authority(start_reaper=False)
    try:
        stale_request = timeline.request("before-the-edit")
        receipt, capability = authority.create(stale_request)
        authority.release(_command("release", receipt), capability)
        assert authority.retained()["entries"] == 1

        timeline.edit("set_opacity_blend", opacity_bp=7000, blend="screen")
        # The body is resident, but a request for the superseded snapshot is refused before the
        # cache is even looked at, and the refusal leaves the body for the current snapshot.
        with pytest.raises(MediaLeaseError, match="stale"):
            authority.create(replace(stale_request, request_id="after-the-edit"))
        assert authority.resources()["leases"] == 0
        assert authority.retained()["entries"] == 1
        authority.create(timeline.request("current"))
        assert timeline.generated == 1
    finally:
        authority.close()
        timeline.release()


def test_the_probe_of_an_image_claim_is_its_transferred_sources_own_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = _ImageTimeline(monkeypatch)
    try:
        claim = timeline.admit(timeline.request("probe"))
        probe = claim.retention_probe()
        source = claim_transferred_authoring_source(timeline.binding, "image_1")
        assert type(source) is OwnedImageSource
        # Exactly the source object's bound method: no claim, snapshot or workspace is reachable
        # from it, so an idle body pins nothing that an accepted edit supersedes.
        assert getattr(probe, "__self__", None) is source
        assert getattr(probe, "__func__", None) is OwnedImageSource.current
        assert probe is not None and probe() is True

        timeline.edit("set_opacity_blend", opacity_bp=7000, blend="screen")
        # The edit ends the claim and leaves the source, which is what the body derives from.
        assert not claim.current()
        assert probe() is True
        after = timeline.admit(timeline.request("probe-after")).retention_probe()
        assert getattr(after, "__self__", None) is source
    finally:
        timeline.release()
    assert probe() is False


def test_a_released_workspace_leaves_no_body_within_two_reaper_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = _ImageTimeline(monkeypatch)
    authority = timeline.authority(start_reaper=True)
    try:
        authority.create(timeline.request("held"))
        timeline.edit("set_opacity_blend", opacity_bp=7000, blend="screen")
        receipt, capability = authority.create(timeline.request("held-after-the-edit"))
        assert timeline.generated == 1
        assert authority.resources()["cacheEntries"] == 1

        timeline.release()
        released = time.monotonic()
        bound = 2 * leases_module._IDLE_REAP_SECONDS
        while authority.resources() != EMPTY and time.monotonic() - released <= bound:
            time.sleep(0.01)
        elapsed = time.monotonic() - released
        # Nothing released a lease or closed the authority: the reaper found the claims stale and
        # the source ended, by itself, and in the pass that found it -- one period, not two.
        assert authority.resources() == EMPTY, elapsed
        assert elapsed <= bound
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            authority.open(_open(receipt), capability)
    finally:
        authority.close()


class _GeneratedTimeline:
    """One Production output imported into a real authoring workspace and placed on its timeline.

    Its source is generated media: a private copy of a stored Production artifact. The copy's
    authority ends with the Authoring workspace and with the Production state it was taken from.
    """

    def __init__(self, tmp_path: Path) -> None:
        ffmpeg, ffprobe, self.adapter = _qualified_tools(tmp_path)
        encoded = (tmp_path / "original.mp4").resolve()
        _encode_source(ffmpeg, encoded)
        self.generator = AuthoringDerivativeGenerator(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(tmp_path / "proxy-scratch").resolve(),
        )
        production, projection, workspace, before, history, _store = _registries_with_ready_output(
            tmp_path,
            frame_count=FRAMES,
            artifact_bodies=(encoded.read_bytes(),),
            width=WIDTH,
            height=HEIGHT,
        )
        imported = _import_request(projection, before, history)
        response = workspace.import_production_outputs(
            imported,
            production_registry=production,
            media_adapter=self.adapter,
            deadline=time.monotonic() + 300,
        )
        self.production = production
        self.production_handle = imported.production_workspace_handle
        self.workspace = workspace
        self.handle = imported.authoring_workspace_handle
        self.asset_id = response.receipt.rows[0].asset_id
        read = workspace.dispatch(
            _authoring_action(
                "generated-read", "read_timeline_history", workspace_handle=self.handle
            )
        )
        assert read.body is not None
        state = cast(dict[str, Any], read.body["authoring"])
        primary = next(track for track in state["tracks"] if track["kind"] == "primary_video")
        insertion = _authoring_action("generated", "apply_timeline_transaction")
        insertion["payload"] = _insert_asset_transaction(
            authoring_state=state,
            asset_id=self.asset_id,
            track_id=primary["track_id"],
            request_id="generated",
            duration_frames=FRAMES,
        )
        inserted = workspace.dispatch(insertion)
        assert inserted.status == 200 and inserted.body is not None, inserted.body
        self.snapshot = decode_public_snapshot(inserted.body["render_snapshot"])

    def staged_source(self) -> GeneratedAuthoringVideoSource:
        (output,) = self.workspace._entries[self.handle].imported_outputs.values()
        return output.source

    def admit(self, request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        return self.workspace.admit_media_derivative(
            request, generator=self.generator, media_adapter=self.adapter
        )

    def request(self, request_id: str) -> CreateLeaseRequest:
        return _create_request(
            self.snapshot, self.handle, self.asset_id, "clip.generated", "video_proxy", request_id
        )

    def release(self) -> None:
        released = self.workspace.dispatch(
            _authoring_action(
                "generated-release", "release_workspace", workspace_handle=self.handle
            )
        )
        assert released.status == 204

    def revoke_production(self) -> None:
        # A revised Production workspace no longer vouches for the output this source was copied
        # from. The Authoring workspace stays open and its private copy stays intact.
        entry = self.production._entries[self.production_handle]
        workspace = entry.workspace
        entry.workspace = revise_workspace(
            workspace,
            expected_workspace_fingerprint=workspace.fingerprint,
            accepted_intent_authorities=tuple(
                AcceptedIntentAuthority(segment.segment_id, segment.accepted_intent_fingerprint)
                for segment in workspace.segments
            ),
        )


def test_a_released_workspace_ends_the_idle_body_of_a_generated_source(tmp_path: Path) -> None:
    timeline = _GeneratedTimeline(tmp_path)
    probe = timeline.admit(timeline.request("probe")).retention_probe()
    staged = timeline.staged_source()
    # Exactly the staged source's own bound liveness, as for the other origins. A probe that
    # answered for anything longer-lived would keep a private proxy of Production media resident
    # for the whole idle bound after its workspace was released.
    assert type(staged) is GeneratedAuthoringVideoSource
    assert getattr(probe, "__self__", None) is staged
    assert getattr(probe, "__func__", None) is GeneratedAuthoringVideoSource.current
    assert probe is not None and probe() is True

    authority = _product_authority(timeline.admit)
    try:
        receipt, capability = _create(authority, timeline.request("held"))
        authority.release(_command("release", receipt), capability)
        assert authority.retained() == {"entries": 1, "bytes": receipt["byteCount"]}
        # The workspace still owns its source, so a pass keeps the body ...
        authority.reap()
        assert authority.retained()["entries"] == 1

        timeline.release()
        # ... and the first pass after the release ends it. Nothing asked for it again.
        assert probe() is False
        authority.reap()
        assert authority.resources() == EMPTY
    finally:
        authority.close()


def test_a_revoked_production_output_ends_the_idle_body_of_a_generated_source(
    tmp_path: Path,
) -> None:
    timeline = _GeneratedTimeline(tmp_path)
    authority = _product_authority(timeline.admit)
    try:
        receipt, capability = _create(authority, timeline.request("held"))
        authority.release(_command("release", receipt), capability)
        assert authority.retained()["entries"] == 1
        assert timeline.staged_source().current()

        timeline.revoke_production()
        assert not timeline.staged_source().current()
        authority.reap()
        assert authority.resources() == EMPTY
    finally:
        authority.close()
        timeline.release()


def _path_backed_claim(
    root: Path,
) -> tuple[Path, Any, PreparedAuthoringHistory, PublicCompositionSnapshot, CreateLeaseRequest]:
    from test_m25_render_job_leases import video_bound

    path, receipt, bound = video_bound(root)
    snapshot = bound._issued_snapshot
    assert snapshot is not None
    # A timing index is the one video derivative that needs no media tool, which keeps these
    # claim-level tests about the claim and its source instead of about an encoder.
    frames = snapshot.assets[0].source_frame_count
    assert frames is not None
    request = replace(
        request_for(snapshot, snapshot.clips[0].clip_id, "video_1", "frame_timing_index"),
        source_end_frame=frames,
    )
    return path, receipt, bound._history, snapshot, request


def _replace_keeping_identity(path: Path) -> None:
    metadata = path.stat()
    path.write_bytes(b"x" * metadata.st_size)
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    replaced = path.stat()
    assert (replaced.st_dev, replaced.st_ino, replaced.st_size, replaced.st_mtime_ns) == (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    ), "same-stat replacement fixture must preserve the admitted source identity"


def test_a_replaced_file_withdraws_the_probe_and_a_moved_workspace_keeps_it(
    tmp_path: Path,
) -> None:
    path, receipt, history, snapshot, request = _path_backed_claim(tmp_path)
    try:
        source = cast(
            _PathBackedAuthoringVideoSource, claim_transferred_authoring_source(receipt, "video_1")
        )
        assert type(source) is _PathBackedAuthoringVideoSource
        workspace_current = [True]
        moved = AuthoringDerivativeClaim(
            request, snapshot, history, receipt, lambda: workspace_current[0], None, None
        )
        probe = moved.retention_probe()
        assert getattr(probe, "__self__", None) is source
        assert getattr(probe, "__func__", None) is _PathBackedAuthoringVideoSource.current
        moved.confirm(time.monotonic() + 5)

        # An accepted edit: the claim is refused, the content it proved is still proven.
        workspace_current[0] = False
        with pytest.raises(MediaLeaseError, match="stale"):
            moved.confirm(time.monotonic() + 5)
        assert moved.retention_probe() == probe

        # The same bytes replaced in place with the timestamp restored: the source's own liveness
        # cannot see it, the confirmation's hash does, and only that withdraws retention.
        replaced = AuthoringDerivativeClaim(
            request, snapshot, history, receipt, lambda: True, None, None
        )
        _replace_keeping_identity(path)
        assert source.current() and replaced.current()
        assert replaced.retention_probe() == probe
        with pytest.raises(MediaLeaseError, match="stale"):
            replaced.confirm(time.monotonic() + 5)
        assert replaced.retention_probe() is None
        assert not replaced.current()
    finally:
        receipt.release()


@pytest.mark.parametrize(
    "failure",
    [
        OSError("the file is in use by another process"),
        AuthoringSourceBindingError("source_stale"),
    ],
    ids=["unreadable", "source_stale"],
)
def test_a_confirmation_that_fails_without_disproving_the_content_keeps_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    _path, receipt, history, snapshot, request = _path_backed_claim(tmp_path)
    try:
        claim = AuthoringDerivativeClaim(
            request, snapshot, history, receipt, lambda: True, None, None
        )
        probe = claim.retention_probe()
        assert probe is not None
        original = next(item for item in history.sources if item.asset.asset_id == request.asset_id)

        def refused(_self: object, _deadline: float) -> None:
            raise failure

        monkeypatch.setattr(type(original), "verify_currentness", refused)
        # The claim is refused and stays refused. Nothing showed that the bytes differ, though, so
        # the body derived from them is not withdrawn: only a failed content proof does that.
        with pytest.raises(MediaLeaseError, match="stale"):
            claim.confirm(time.monotonic() + 5)
        assert not claim.current()
        assert claim.retention_probe() == probe
    finally:
        receipt.release()


def _product_claims(
    root: Path,
) -> tuple[Path, Any, MediaLeaseAuthority, CreateLeaseRequest]:
    path, receipt, history, snapshot, request = _path_backed_claim(root)

    def admit(value: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        return AuthoringDerivativeClaim(value, snapshot, history, receipt, lambda: True, None, None)

    authority = MediaLeaseAuthority(
        admit, retention_seconds=RETAINED_DERIVATIVE_IDLE_SECONDS, start_reaper=False
    )
    return path, receipt, authority, request


def test_a_replaced_file_ends_the_idle_body_of_a_product_claim_at_once(tmp_path: Path) -> None:
    path, receipt, authority, request = _product_claims(tmp_path)
    try:
        first, capability = authority.create(request)
        authority.release(_command("release", first), capability)
        assert authority.retained() == {"entries": 1, "bytes": first["byteCount"]}

        _replace_keeping_identity(path)
        # The source's liveness cannot see the replacement, so the reaper keeps the body ...
        authority.reap()
        assert authority.retained()["entries"] == 1
        # ... and the first claim that proves the content ends it, without receiving it.
        with pytest.raises(MediaLeaseError, match="stale"):
            authority.create(replace(request, request_id="after-the-replacement"))
        assert authority.resources() == EMPTY
    finally:
        authority.close()
        receipt.release()


def test_a_replaced_file_ends_a_shared_body_of_product_claims_with_its_last_lease(
    tmp_path: Path,
) -> None:
    path, receipt, authority, request = _product_claims(tmp_path)
    try:
        first, first_capability = authority.create(request)
        second, second_capability = authority.create(
            replace(request, request_id="second", owner_id="other-owner")
        )
        assert second["derivativeFingerprint"] == first["derivativeFingerprint"]
        assert authority.resources()["cacheEntries"] == 1

        _replace_keeping_identity(path)
        with pytest.raises(MediaLeaseError, match="stale"):
            authority.renew(_command("renew", first), first_capability)
        # The other lease has not confirmed since and keeps the bytes it was granted; they were
        # derived from the content that was verified when they were generated.
        assert authority.resources()["leases"] == 1
        assert authority.resources()["cacheEntries"] == 1
        authority.release(_command("release", second), second_capability)
        assert authority.resources() == EMPTY
    finally:
        authority.close()
        receipt.release()


def _font_claim() -> tuple[AuthoringDerivativeClaim, Any, PreparedAuthoringHistory]:
    fonts = load_packaged_font_manifest()
    face = fonts.faces[0]
    wire = fixture_snapshot()
    font = next(asset for asset in wire["assets"] if asset["kind"] == "font")
    font["asset_id"] = face.font_asset_id
    text = next(clip for clip in wire["clips"] if clip["clip_id"] == "clip-title")["text"]
    text.update(font_asset_id=face.font_asset_id, weight=face.weight, style=face.style)
    snapshot = decode_public_snapshot(resign(wire))
    history = PreparedAuthoringHistory(snapshot, 1, (), fonts)
    request = request_for(snapshot, "clip-title", face.font_asset_id, "packaged_font_face")
    claim = AuthoringDerivativeClaim(request, snapshot, history, None, lambda: True, None, None)
    return claim, face, history


def test_a_packaged_font_body_is_retained_by_a_probe_that_references_nothing() -> None:
    claim, _face, history = _font_claim()
    probe = claim.retention_probe()
    assert probe is _packaged_font_is_resident
    assert getattr(probe, "__closure__", None) is None
    assert not hasattr(probe, "__self__")
    assert probe is not None and probe() is True
    # The history ends with the next accepted edit; the face file does not.
    history.discard()
    assert not claim.current()
    assert claim.retention_probe() is probe


def test_a_font_that_fails_its_proof_withdraws_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim, face, _history = _font_claim()
    claim.confirm(time.monotonic() + 5)
    assert claim.retention_probe() is _packaged_font_is_resident

    def changed(_self: object) -> bytes:
        raise AuthoringFontError(
            "font_artifact_hash_mismatch", "a packaged font differs from its manifest"
        )

    monkeypatch.setattr(type(face), "read_verified_bytes", changed)
    with pytest.raises(MediaLeaseError, match="stale"):
        claim.confirm(time.monotonic() + 5)
    assert claim.retention_probe() is None
