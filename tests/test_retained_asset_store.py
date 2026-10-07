"""Native retained copies, atomic catalogs and charged remnants use real private files."""

from __future__ import annotations

import importlib
import importlib.util
import time
from pathlib import Path
from types import ModuleType

import pytest
from test_m25_29_production_authoring_import import _qualified_test_adapter
from test_retained_asset_source import OWNER, ready_claim

from comfyui_h3_context.adapters.retained_asset_source import (
    VerifiedRetentionSource,
    stage_retention_source,
)
from comfyui_h3_context.core.retained_assets import CLOSED_RETENTION_MS, RetainedAssetError

MODULE = "comfyui_h3_context.adapters.retained_asset_store"


def store_module() -> ModuleType:
    assert importlib.util.find_spec(MODULE) is not None, "missing durable retained-asset owner"
    return importlib.import_module(MODULE)


def live_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, owner_id: str = OWNER
) -> VerifiedRetentionSource:
    claim, _, _, _ = ready_claim(tmp_path)
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    return stage_retention_source(
        claim=claim,
        owner_id=owner_id,
        media_adapter=adapter,
        scratch_root=tmp_path / "authoring-scratch",
        deadline=time.monotonic() + 10.0,
    )


def owner_directory(tmp_path: Path, owner: str = OWNER) -> Path:
    return tmp_path / "private" / "recovery-assets" / "v1" / owner


def test_default_disabled_has_no_namespace_and_cannot_admit(tmp_path: Path) -> None:
    module = store_module()
    store = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        assert store.read().to_wire() == {
            "schema": "h3.context.retained_asset_catalog.v1",
            "owner_id": OWNER,
            "revision": 0,
            "enabled": False,
            "assets": [],
        }
        assert store.set_enabled(False, expected_revision=0).revision == 0
        with pytest.raises(RetainedAssetError, match="recovery_disabled"):
            store.admit(object(), expected_revision=0, deadline=time.monotonic() + 10.0)
        assert not (tmp_path / "private").exists()
    finally:
        store.close()


def test_exact_copy_is_visible_only_in_committed_catalog_and_survives_new_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = store_module()
    source = live_source(tmp_path, monkeypatch)
    store = module.RetainedAssetStore(tmp_path / "private", OWNER, clock_ms=lambda: 1000)
    try:
        assert store.set_enabled(True, expected_revision=0).revision == 1
        catalog, asset = store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        assert catalog.revision == 2 and catalog.assets == (asset,)
        assert asset.closed_at_ms == 1000 and asset.project_references == ()
        assert (
            owner_directory(tmp_path) / (asset.asset_id + ".mp4")
        ).read_bytes() == b"bounded-generated-video-1"
        assert not list(owner_directory(tmp_path).glob("intent-*.json"))
        assert not (owner_directory(tmp_path) / "manifest.next.json").exists()
    finally:
        source.release()
        store.close()
    restarted = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        assert restarted.read() == catalog
    finally:
        restarted.close()


def test_disable_preserves_bytes_and_revision_cas_refuses_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = store_module()
    source = live_source(tmp_path, monkeypatch)
    store = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        store.set_enabled(True, expected_revision=0)
        catalog, asset = store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        body = (owner_directory(tmp_path) / (asset.asset_id + ".mp4")).read_bytes()
        with pytest.raises(RetainedAssetError, match="revision_conflict"):
            store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        disabled = store.set_enabled(False, expected_revision=catalog.revision)
        assert disabled.assets == catalog.assets and not disabled.enabled
        with pytest.raises(RetainedAssetError, match="recovery_disabled"):
            store.admit(
                source, expected_revision=disabled.revision, deadline=time.monotonic() + 10.0
            )
        assert (owner_directory(tmp_path) / (asset.asset_id + ".mp4")).read_bytes() == body
    finally:
        source.release()
        store.close()


def test_catalog_failure_never_acknowledges_copy_and_verified_orphan_can_be_collected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = store_module()
    source = live_source(tmp_path, monkeypatch)
    store = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        enabled = store.set_enabled(True, expected_revision=0)
        before = (owner_directory(tmp_path) / "manifest.json").read_bytes()
        with monkeypatch.context() as faults:

            def failure(*_args: object, **_kwargs: object) -> None:
                raise OSError("synthetic manifest failure")

            faults.setattr(module, "_replace_manifest", failure)
            # Native directory pinning conservatively projects interrupted IO as unsafe.
            with pytest.raises(RetainedAssetError, match="storage_unsafe"):
                store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        assert (owner_directory(tmp_path) / "manifest.json").read_bytes() == before
        assert store.read() == enabled
        assert len(list(owner_directory(tmp_path).glob("asset_*.mp4"))) == 1
        assert len(list(owner_directory(tmp_path).glob("intent-*.json"))) == 1
        collected = store.collect(expected_revision=1)
        assert collected.catalog == enabled and collected.removed == 0
        assert not list(owner_directory(tmp_path).glob("asset_*.mp4"))
        assert not list(owner_directory(tmp_path).glob("intent-*.json"))
        assert not (owner_directory(tmp_path) / "manifest.next.json").exists()
    finally:
        source.release()
        store.close()


def test_unproven_or_changed_orphans_remain_charged_not_age_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = store_module()
    source = live_source(tmp_path, monkeypatch)
    store = module.RetainedAssetStore(
        tmp_path / "private", OWNER, limits=module.RetainedStoreLimits(max_owner_assets=1)
    )
    try:
        store.set_enabled(True, expected_revision=0)
        orphan = owner_directory(tmp_path) / ("asset_" + "f" * 32 + ".mp4")
        orphan.write_bytes(b"unknown")
        store.collect(expected_revision=1)
        assert orphan.read_bytes() == b"unknown"
        with pytest.raises(RetainedAssetError, match="quota_assets"):
            store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        assert len(list(owner_directory(tmp_path).glob("asset_*.mp4"))) == 1
    finally:
        source.release()
        store.close()


def test_references_protect_clear_and_last_removal_starts_a_fresh_retention_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = store_module()
    now = [1000]
    source = live_source(tmp_path, monkeypatch)
    store = module.RetainedAssetStore(tmp_path / "private", OWNER, clock_ms=lambda: now[0])
    project = "project_" + "b" * 32
    try:
        store.set_enabled(True, expected_revision=0)
        catalog, asset = store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        protected = store.set_project_reference(asset.asset_id, project, True, expected_revision=2)
        assert protected.assets[0].closed_at_ms is None
        now[0] += CLOSED_RETENTION_MS + 1
        cleared = store.clear(expected_revision=protected.revision)
        assert cleared.catalog == protected and cleared.protected == 1 and cleared.removed == 0
        assert store.collect(expected_revision=protected.revision).removed == 0
        closed = store.set_project_reference(
            asset.asset_id, project, False, expected_revision=protected.revision
        )
        assert closed.assets[0].closed_at_ms == now[0]
        assert store.collect(expected_revision=closed.revision).removed == 0
        now[0] += CLOSED_RETENTION_MS
        collected = store.collect(expected_revision=closed.revision)
        assert collected.removed == 1 and collected.catalog.assets == ()
        assert not (owner_directory(tmp_path) / (asset.asset_id + ".mp4")).exists()
    finally:
        source.release()
        store.close()


def test_native_owner_lease_prevents_a_second_writer_until_close(tmp_path: Path) -> None:
    module = store_module()
    first = module.RetainedAssetStore(tmp_path / "private", OWNER)
    second = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        first.set_enabled(True, expected_revision=0)
        with pytest.raises(RetainedAssetError, match="owner_busy"):
            second.read()
        first.close()
        assert second.read().enabled
    finally:
        first.close()
        second.close()


def test_foreign_owner_artifact_counts_against_global_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = store_module()
    first = module.RetainedAssetStore(
        tmp_path / "private", OWNER, limits=module.RetainedStoreLimits(max_global_assets=1)
    )
    other = "owner_" + "b" * 32
    second = module.RetainedAssetStore(
        tmp_path / "private", other, limits=module.RetainedStoreLimits(max_global_assets=1)
    )
    source = live_source(tmp_path, monkeypatch, owner_id=other)
    try:
        first.set_enabled(True, expected_revision=0)
        orphan = owner_directory(tmp_path) / ("asset_" + "e" * 32 + ".mp4")
        orphan.write_bytes(b"foreign retained remnant")
        second.set_enabled(True, expected_revision=0)
        with pytest.raises(RetainedAssetError, match="quota_assets"):
            second.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        assert orphan.read_bytes() == b"foreign retained remnant"
        assert second.read().assets == ()
    finally:
        source.release()
        first.close()
        second.close()


def test_catalog_unknown_fields_refuse_and_preserve_every_file(tmp_path: Path) -> None:
    module = store_module()
    store = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        store.set_enabled(True, expected_revision=0)
        catalog = owner_directory(tmp_path) / "manifest.json"
        body = catalog.read_bytes().replace(b'"assets":[]', b'"assets":[],"path":"untrusted"')
        catalog.write_bytes(body)
        with pytest.raises(RetainedAssetError, match="shape_invalid"):
            store.read()
        assert catalog.read_bytes() == body
    finally:
        store.close()
