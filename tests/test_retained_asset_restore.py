"""Explicit current probe and fresh exact receipt issuance survive loss of Production state."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, NoReturn

import pytest
from test_m25_29_production_authoring_import import (
    _qualified_test_adapter,
    _registries_with_ready_output,
    _video_probe_wire,
)
from test_retained_asset_source import OWNER, ready_claim
from test_retained_asset_store import owner_directory, store_module

from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingError,
    claim_exact_authoring_source,
    claim_transferred_authoring_source,
    execute_transferred_authoring_preview,
)
from comfyui_h3_context.adapters.authoring_video_facts import AuthoringVideoFacts
from comfyui_h3_context.adapters.av_reconstruction_media import (
    AuthoringVideoProbePayload,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.retained_asset_source import stage_retention_source
from comfyui_h3_context.adapters.retained_asset_store import RetainedAssetStore
from comfyui_h3_context.adapters.retained_asset_use import RetainedAssetUse, RetainedVideoSource
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.composition_contract import composition_contract_fingerprint
from comfyui_h3_context.core.errors import CanonicalizationError
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.registry import ReferenceRegistry
from comfyui_h3_context.core.retained_assets import (
    RetainedAsset,
    RetainedAssetError,
    RetainedCatalog,
)


def retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    RetainedAssetStore,
    RetainedCatalog,
    RetainedAsset,
    QualifiedAVMediaAdapter,
    ProductionWorkspaceRegistry,
    ProductionWorkbenchProjection,
]:
    module = store_module()
    claim, production, projection, _ = ready_claim(tmp_path)
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    source = stage_retention_source(
        claim=claim,
        owner_id=OWNER,
        media_adapter=adapter,
        scratch_root=tmp_path / "authoring-scratch",
        deadline=time.monotonic() + 10.0,
    )
    store = module.RetainedAssetStore(tmp_path / "private", OWNER)
    store.set_enabled(True, expected_revision=0)
    try:
        catalog, asset = store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
    finally:
        source.release()
    return store, catalog, asset, adapter, production, projection


def restore(
    store: RetainedAssetStore,
    asset: RetainedAsset,
    adapter: QualifiedAVMediaAdapter,
    tmp_path: Path,
) -> RetainedAssetUse:
    return store.restore(
        asset.asset_id,
        media_adapter=adapter,
        scratch_root=tmp_path / "authoring-scratch",
        deadline=time.monotonic() + 10.0,
    )


@pytest.mark.parametrize("frame_count", (256, 257, 362, 512))
def test_retained_full_qualified_landmark_budget_round_trips_exact_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame_count: int
) -> None:
    production, projection, *_ = _registries_with_ready_output(tmp_path, frame_count=frame_count)
    segment = projection.outputs[0].segment_id
    assert segment is not None
    claim = production.claim_authoring_output_batch(
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        expected_workspace_revision=projection.workspace_revision,
        expected_workspace_fingerprint=projection.workspace_fingerprint,
        pairs=((segment, projection.outputs[0].output_handle),),
    )[0]
    adapter = _qualified_test_adapter(tmp_path, monkeypatch, frame_count=frame_count)
    source = stage_retention_source(
        claim=claim,
        owner_id=OWNER,
        media_adapter=adapter,
        scratch_root=tmp_path / "authoring-scratch",
        deadline=time.monotonic() + 10.0,
    )
    store = RetainedAssetStore(tmp_path / "private", OWNER)
    store.set_enabled(True, expected_revision=0)
    facts_wire = source.facts.to_wire()
    try:
        landmarks = facts_wire["landmarks"]
        assert isinstance(landmarks, list) and len(landmarks) == frame_count
        expected = composition_contract_fingerprint(facts_wire)
        if frame_count <= 256:
            assert expected == canonical_fingerprint(facts_wire)
        else:
            with pytest.raises(CanonicalizationError):
                canonical_fingerprint(facts_wire)
        _, asset = store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        assert asset.facts_fingerprint == expected
        source.release()
        store.close()
        store = RetainedAssetStore(tmp_path / "private", OWNER)
        use = restore(store, asset, adapter, tmp_path)
        assert use.source.facts.to_wire() == facts_wire
        assert use.source.current() and use.source.facts.frame_count == frame_count
        use.release()
        changed = copy.deepcopy(facts_wire)
        changed_landmarks = changed["landmarks"]
        assert isinstance(changed_landmarks, list)
        changed_landmarks[-1]["duration_ticks"] += 1
        assert composition_contract_fingerprint(changed) != expected
        probe = json.loads(_video_probe_wire(frame_count=frame_count))
        probe["frames"][-1]["duration"] += 1

        def changed_probe(
            _self: QualifiedAVMediaAdapter,
            *,
            source_path: Path,
            deadline: float,
            cancellation: object | None = None,
        ) -> AuthoringVideoProbePayload:
            body = source_path.read_bytes()
            return AuthoringVideoProbePayload(
                json.dumps(probe).encode(), "sha256:" + hashlib.sha256(body).hexdigest(), len(body)
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "probe_authoring_video_source", changed_probe)
        with pytest.raises(RetainedAssetError, match="media_unqualified"):
            restore(store, asset, adapter, tmp_path)
        assert tuple((tmp_path / "authoring-scratch").iterdir()) == ()
    finally:
        source.release()
        store.close()


def test_restore_reprobes_mints_fresh_exact_receipt_and_does_not_preview_implicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    calls: list[str] = []
    probes: list[Path] = []
    module = store_module()
    current_probe = module.probe_authoring_video_facts

    def probe(path: Path, *args: Any, **kwargs: Any) -> AuthoringVideoFacts:
        probes.append(path)
        assert path != owner_directory(tmp_path) / (asset.asset_id + ".mp4")
        assert path.parent.parent == tmp_path / "authoring-scratch"
        facts: object = current_probe(path, *args, **kwargs)
        assert isinstance(facts, AuthoringVideoFacts)
        return facts

    monkeypatch.setattr(module, "probe_authoring_video_facts", probe)

    def preview(_self: QualifiedAVMediaAdapter, **kwargs: Any) -> tuple[bytearray, str]:
        assert kwargs["source_path"].read_bytes() == b"bounded-generated-video-1"
        calls.append("explicit-preview")
        return bytearray(b"bounded-preview"), "none"

    monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_authoring_preview", preview)
    try:
        use = restore(store, asset, adapter, tmp_path)
        assert len(probes) == 1 and probes[0] == use.source.lease.path
        assert calls == []
        assert use.asset_id == asset.asset_id and use.use_handle.startswith("retained_")
        source = claim_transferred_authoring_source(use.receipt, source_id=use.source_id)
        assert isinstance(source, RetainedVideoSource)
        assert source.current()
        with pytest.raises(AuthoringSourceBindingError, match="registry_mismatch"):
            claim_exact_authoring_source(
                use.receipt,
                exact_registry=ReferenceRegistry.empty(),
                expected_generation=use.receipt.generation,
                source_id=use.source_id,
            )
        body, disposition = execute_transferred_authoring_preview(
            source,
            adapter,
            source_start_frame=0,
            frames=1,
            source_fps=24,
            deadline=time.monotonic() + 10.0,
        )
        assert body == b"bounded-preview" and disposition == "none"
        assert calls == ["explicit-preview"]
        assert store.claim_use(use.use_handle) is use
        store.release_use(use.use_handle)
        assert not source.current()
        with pytest.raises(RetainedAssetError, match="lease_invalid"):
            store.claim_use(use.use_handle)
        assert not list((tmp_path / "authoring-scratch").iterdir())
        assert (owner_directory(tmp_path) / (asset.asset_id + ".mp4")).exists()
    finally:
        store.close()


def test_current_probe_refusal_cannot_be_replaced_with_catalog_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.authoring_video_facts import AuthoringVideoFactsError

    store, _, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    path = owner_directory(tmp_path) / (asset.asset_id + ".mp4")
    original = path.read_bytes()

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise AuthoringVideoFactsError("video_profile_unsupported")

    monkeypatch.setattr(store_module(), "probe_authoring_video_facts", refuse)
    try:
        with pytest.raises(RetainedAssetError, match="media_unqualified"):
            restore(store, asset, adapter, tmp_path)
        assert path.read_bytes() == original and not store._uses
        assert not list((tmp_path / "authoring-scratch").iterdir())
    finally:
        store.close()


def test_restart_invalidates_old_use_but_fresh_restore_does_not_require_old_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.comfyui_production_workspace import PRODUCTION_ACTION_SCHEMA

    store, _, asset, adapter, production, projection = retained(tmp_path, monkeypatch)
    old = restore(store, asset, adapter, tmp_path)
    store.close()
    production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "retention.origin.release",
            "action": "release_workspace",
            "payload": {
                "workspace_handle": projection.workspace_handle,
                "expected_workspace_revision": projection.workspace_revision,
                "expected_workspace_fingerprint": projection.workspace_fingerprint,
            },
        }
    )
    restarted = store_module().RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        with pytest.raises(RetainedAssetError, match="lease_invalid"):
            restarted.claim_use(old.use_handle)
        assert old.receipt.released
        with pytest.raises(AuthoringSourceBindingError, match="invalid_receipt"):
            claim_transferred_authoring_source(old.receipt, source_id=old.source_id)
        fresh = restore(restarted, asset, adapter, tmp_path)
        assert fresh.use_handle != old.use_handle and fresh.source_id != old.source_id
        assert fresh.receipt is not old.receipt
        source = claim_transferred_authoring_source(fresh.receipt, source_id=fresh.source_id)
        assert isinstance(source, RetainedVideoSource)
        assert source.current()
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "damage, code", (("change", "asset_changed"), ("remove", "asset_unavailable"))
)
def test_changed_or_missing_retained_media_cannot_issue_any_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
    code: str,
) -> None:
    store, _, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    path = owner_directory(tmp_path) / (asset.asset_id + ".mp4")
    if damage == "change":
        path.write_bytes(b"x" * asset.byte_length)
    else:
        path.unlink()
    try:
        with pytest.raises(RetainedAssetError, match=code):
            restore(store, asset, adapter, tmp_path)
        assert not list((tmp_path / "authoring-scratch").iterdir())
        assert store.read().assets == (asset,)
    finally:
        store.close()


def test_fresh_use_identity_is_immutable_and_borrowers_cannot_be_copied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, catalog, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    borrower = None
    try:
        use = restore(store, asset, adapter, tmp_path)
        for target, field, value in (
            (use, "use_handle", "retained_" + "a" * 32),
            (use, "source_id", "retained.forged"),
            (use.source, "facts", use.source.facts),
            (use.source, "lease", use.source.lease),
        ):
            with pytest.raises(AttributeError):
                setattr(target, field, value)
        borrower = use.source.borrow_for_render()
        with pytest.raises(TypeError):
            copy.copy(borrower)
        store.release_use(use.use_handle)
        assert borrower.current()
        assert store.clear(expected_revision=catalog.revision).protected == 1
        borrower.release()
        assert store.clear(expected_revision=catalog.revision).removed == 1
    finally:
        if borrower is not None:
            borrower.release()
        store.close()


def test_live_fresh_use_protects_clear_until_explicit_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, catalog, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    try:
        use = restore(store, asset, adapter, tmp_path)
        protected = store.clear(expected_revision=catalog.revision)
        assert protected.removed == 0 and protected.protected == 1
        store.release_use(use.use_handle)
        removed = store.clear(expected_revision=catalog.revision)
        assert removed.removed == 1 and removed.protected == 0 and removed.catalog.assets == ()
    finally:
        store.close()


def test_failed_owned_use_cleanup_stays_protected_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.media_subprocess import OwnedOutputLease
    from comfyui_h3_context.core.errors import MediaProcessError

    store, catalog, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    use = restore(store, asset, adapter, tmp_path)
    release = OwnedOutputLease.release
    failures = [True]

    def fail_once(lease: OwnedOutputLease) -> None:
        if lease is use.source.lease and failures[0]:
            failures[0] = False
            raise MediaProcessError("owned cleanup unavailable")
        release(lease)

    monkeypatch.setattr(OwnedOutputLease, "release", fail_once)
    try:
        with pytest.raises(RetainedAssetError, match="storage_unavailable"):
            store.release_use(use.use_handle)
        assert use.receipt.released and not use.source.lease.released
        assert store.clear(expected_revision=catalog.revision).protected == 1
        store.release_use(use.use_handle)
        assert use.source.lease.released
        assert store.clear(expected_revision=catalog.revision).removed == 1
    finally:
        use.source.release()
        store.close()


def test_close_attempts_all_fresh_uses_and_retains_owner_only_for_failed_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.media_subprocess import OwnedOutputLease
    from comfyui_h3_context.core.errors import MediaProcessError

    store, _, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    first, second = (restore(store, asset, adapter, tmp_path) for _ in range(2))
    release = OwnedOutputLease.release
    failures = [True]

    def fail_once(lease: OwnedOutputLease) -> None:
        if lease is first.source.lease and failures[0]:
            failures[0] = False
            raise MediaProcessError("owned cleanup unavailable")
        release(lease)

    monkeypatch.setattr(OwnedOutputLease, "release", fail_once)
    try:
        with pytest.raises(RetainedAssetError, match="storage_unavailable"):
            store.close()
        assert second.source.lease.released and not first.source.lease.released
        assert store._lease is not None
        store.close()
        assert first.source.lease.released and store._lease is None
    finally:
        first.source.release()
        second.source.release()
        store.close()


def test_expired_cleanup_failure_is_finite_attempts_other_uses_and_stays_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.media_subprocess import OwnedOutputLease
    from comfyui_h3_context.core.errors import MediaProcessError

    store, _, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    first, second = (restore(store, asset, adapter, tmp_path) for _ in range(2))
    release = OwnedOutputLease.release
    failures = [True]

    def fail_once(lease: OwnedOutputLease) -> None:
        if lease is first.source.lease and failures[0]:
            failures[0] = False
            raise MediaProcessError("owned cleanup unavailable")
        release(lease)

    monkeypatch.setattr(OwnedOutputLease, "release", fail_once)
    try:
        with monkeypatch.context() as expired:
            expired.setattr(time, "monotonic", lambda: second.expires_at + 1.0)
            with pytest.raises(RetainedAssetError, match="storage_unavailable"):
                store.claim_use(first.use_handle)
        assert first.receipt.released and not first.source.lease.released
        assert second.source.lease.released
        assert store._lease is not None
        store.release_use(first.use_handle)
        assert first.source.lease.released
    finally:
        first.source.release()
        second.source.release()
        store.close()


def test_disabling_keeps_files_but_blocks_existing_and_new_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, catalog, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    try:
        use = restore(store, asset, adapter, tmp_path)
        source = claim_transferred_authoring_source(use.receipt, source_id=use.source_id)
        assert isinstance(source, RetainedVideoSource)
        store.set_enabled(False, expected_revision=catalog.revision)
        assert not source.current()
        with pytest.raises(RetainedAssetError, match="recovery_disabled"):
            restore(store, asset, adapter, tmp_path)
        store.release_use(use.use_handle)
        assert (owner_directory(tmp_path) / (asset.asset_id + ".mp4")).exists()
    finally:
        store.close()


def test_substitution_during_explicit_preview_discards_the_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    returned = bytearray(b"must-not-escape")
    path = owner_directory(tmp_path) / (asset.asset_id + ".mp4")

    def preview(_self: QualifiedAVMediaAdapter, **_kwargs: Any) -> tuple[bytearray, str]:
        path.write_bytes(b"x" * asset.byte_length)
        return returned, "none"

    monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_authoring_preview", preview)
    try:
        use = restore(store, asset, adapter, tmp_path)
        source = claim_transferred_authoring_source(use.receipt, source_id=use.source_id)
        with pytest.raises(AuthoringSourceBindingError, match="source_stale"):
            execute_transferred_authoring_preview(
                source,
                adapter,
                source_start_frame=0,
                frames=1,
                source_fps=24,
                deadline=time.monotonic() + 10.0,
            )
        assert returned == b""
    finally:
        store.close()
