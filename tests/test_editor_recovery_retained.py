"""Recovery references require fresh owned media authority and preserve other projects."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_retained_asset_service import enable_and_retain, service_fixture

from comfyui_h3_context.core.retained_assets import RetainedAssetError


def test_reference_ids_alone_never_grant_and_fresh_use_pins_only_owned_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, command, _owner, _adapter, _dependencies = service_fixture(tmp_path, monkeypatch)
    project, foreign = "project_" + "a" * 32, "project_" + "b" * 32
    try:
        _response, asset = enable_and_retain(service, command)
        assert service._store is not None
        sync = getattr(service, "sync_recovery_references", None)
        assert callable(sync), "missing qualified recovery reference port"
        before = service._store.read()
        with pytest.raises(RetainedAssetError):
            sync(project, (asset,))
        assert service._store.read() == before
        use = service.restore_project_source(asset, deadline=time.monotonic() + 5)
        borrower = use.source.borrow_for_render()
        try:
            sync(project, (asset,), uses=(use,))
            row = service._store.read().assets[0]
            assert row.project_references == (project,)
            catalog = service._store.set_project_reference(
                asset, foreign, present=True, expected_revision=service._store.read().revision
            )
            sync(project, (asset,))  # Known journal reconciliation can keep an existing pin.
            assert service._store.read() == catalog
            sync(project, ())
            assert service._store.read().assets[0].project_references == (foreign,)
            assert service._store.inventory().protected == 1
            with pytest.raises(RetainedAssetError):
                sync(project, (asset,), uses=(object(),))
            assert service._store.read().assets[0].project_references == (foreign,)
        finally:
            borrower.release()
            use.release()
    finally:
        service.close()


def test_released_use_cannot_create_recovery_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, command, _owner, _adapter, _dependencies = service_fixture(tmp_path, monkeypatch)
    try:
        _response, asset = enable_and_retain(service, command)
        assert service._store is not None
        sync = getattr(service, "sync_recovery_references", None)
        assert callable(sync), "missing qualified recovery reference port"
        use = service.restore_project_source(asset, deadline=time.monotonic() + 5)
        use.release()
        before = service._store.read()
        with pytest.raises(RetainedAssetError):
            sync("project_" + "c" * 32, (asset,), uses=(use,))
        assert service._store.read() == before
    finally:
        service.close()
