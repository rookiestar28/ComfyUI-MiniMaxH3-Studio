"""Faults must preserve uncertain media, exact ownership and currently referenced resources."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from test_retained_asset_restore import restore, retained
from test_retained_asset_source import OWNER
from test_retained_asset_store import live_source, owner_directory, store_module

from comfyui_h3_context.core import private_storage_layout as layout
from comfyui_h3_context.core.retained_assets import RetainedAssetError


def test_completed_orphan_substituted_with_identical_bytes_is_not_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = store_module()
    source = live_source(tmp_path, monkeypatch)
    store = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        store.set_enabled(True, expected_revision=0)
        with monkeypatch.context() as faults:

            def failed(*_args: object, **_kwargs: object) -> None:
                raise OSError("synthetic interrupted catalog")

            faults.setattr(module, "_replace_manifest", failed)
            with pytest.raises(RetainedAssetError):
                store.admit(source, expected_revision=1, deadline=time.monotonic() + 10.0)
        media = next(owner_directory(tmp_path).glob("asset_*.mp4"))
        original = media.stat()
        replacement = tmp_path / "replacement.mp4"
        replacement.write_bytes(media.read_bytes())
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, media)
        assert media.stat().st_ino != original.st_ino
        store.collect(expected_revision=1)
        assert media.exists(), "a matching digest must not erase a substituted file identity"
        assert list(owner_directory(tmp_path).glob("intent-*.json"))
        assert list(owner_directory(tmp_path).glob("verified-*.json"))
    finally:
        source.release()
        store.close()


def test_missing_owner_lease_is_not_recreated_on_read_or_existing_data_enable(
    tmp_path: Path,
) -> None:
    module = store_module()
    store = module.RetainedAssetStore(tmp_path / "private", OWNER)
    store.set_enabled(True, expected_revision=0)
    store.close()
    lease = owner_directory(tmp_path) / ".owner.lock"
    lease.unlink()
    catalog = (owner_directory(tmp_path) / "manifest.json").read_bytes()
    reopened = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        for operation in (reopened.read, lambda: reopened.set_enabled(True, expected_revision=1)):
            with pytest.raises(RetainedAssetError, match="storage_unverified"):
                operation()
        assert not lease.exists()
        assert (owner_directory(tmp_path) / "manifest.json").read_bytes() == catalog
    finally:
        reopened.close()


def test_family_marker_corruption_invalidates_a_fresh_use_without_deleting_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    try:
        use = restore(store, asset, adapter, tmp_path)
        marker = owner_directory(tmp_path).parent / ".h3-retained-assets-v1"
        marker.write_bytes(b"unknown-family")
        assert not use.source.current()
        assert (owner_directory(tmp_path) / (asset.asset_id + ".mp4")).exists()
    finally:
        store.close()


@pytest.mark.parametrize(
    "field, value", (("width", 128), ("height", 128), ("frame_count", 4), ("duration_ms", 1000))
)
def test_catalog_scalar_facts_cannot_alias_a_current_probe_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: int,
) -> None:
    store, _, asset, adapter, _, _ = retained(tmp_path, monkeypatch)
    try:
        path = owner_directory(tmp_path) / "manifest.json"
        body = json.loads(path.read_bytes())
        body["assets"][0][field] = value
        path.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(RetainedAssetError, match="media_unqualified"):
            restore(store, asset, adapter, tmp_path)
        assert not list((tmp_path / "authoring-scratch").iterdir())
    finally:
        store.close()


def test_absent_reference_removal_does_not_renew_a_closed_assets_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, catalog, asset, _, _, _ = retained(tmp_path, monkeypatch)
    try:
        store.clock_ms = lambda: asset.created_at_ms + 1000
        result = store.set_project_reference(
            asset.asset_id, "project_" + "b" * 32, False, expected_revision=catalog.revision
        )
        assert result == catalog
    finally:
        store.close()


def test_retained_layout_is_active_but_reserved_project_owner_and_total_caps_are_unchanged() -> (
    None
):
    assert not next(
        row for row in layout.PRIVATE_STORAGE_SUBTREES if row.name == "recovery-assets"
    ).reserved
    assert layout.RESERVED_DURABLE_BYTES == 640 * layout.MIB
    assert layout.DURABLE_BYTES == 1680 * layout.MIB
