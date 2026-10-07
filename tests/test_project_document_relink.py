"""Fresh retained factories, not file facts, authorize reopened project media."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_project_document import document_wire
from test_retained_asset_restore import restore, retained

from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.project_document_service import ProjectDocumentService
from comfyui_h3_context.adapters.retained_asset_store import RetainedAssetStore


def service_with_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProjectDocumentService, dict[str, Any], RetainedAssetStore, Mock]:
    store, _catalog, asset, adapter, _production, _projection = retained(tmp_path, monkeypatch)
    first = restore(store, asset, adapter, tmp_path)
    assert hasattr(first.source, "public_asset"), (
        "retained source needs an exact public facts projection"
    )
    wire = document_wire()
    wire["editor"]["assets"] = [first.source.public_asset("video.saved").to_wire()]
    wire["editor"]["clips"][0]["duration_frames"] = 2
    wire["editor"]["clips"][0].pop("audio")
    wire["editor"]["reference"]["sources"][0]["duration_milliseconds"] = (
        first.source.duration_milliseconds
    )
    wire["media"] = [
        {
            "asset_id": "video.saved",
            "content_fingerprint": first.source.facts.content_fingerprint,
            "byte_length": first.source.facts.byte_length,
            "retained_id": asset.asset_id,
        }
    ]
    first.release()
    port = Mock()
    port.restore_project_source.side_effect = lambda *args, **kwargs: restore(
        store, asset, adapter, tmp_path
    )
    port.adopt_project_source.side_effect = lambda use, project_id: store.adopt_project_use(
        use, project_id
    )
    service = ProjectDocumentService(
        ProductionWorkspaceRegistry(seed_claim=Mock()), AuthoringWorkspaceRegistry(), retained=port
    )
    return service, wire, store, port


def test_missing_open_is_data_only_then_explicit_relink_binds_fresh_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, wire, store, port = service_with_retained(tmp_path, monkeypatch)
    try:
        opened = service.open_document(wire)
        port.restore_project_source.assert_not_called()
        assert opened["missing_media"] == ["video.saved"]
        assert hasattr(service, "relink_document"), "explicit fresh relink port is required"
        response = service.relink_document(
            opened["owner"],
            "video.saved",
            wire["media"][0]["retained_id"],
            deadline=time.monotonic() + 10.0,
        )
        assert response["missing_media"] == []
        assert (
            response["owner"]["production_revision"] == opened["owner"]["production_revision"] + 1
        )
        assert (
            response["production"]["workspace_revision"] == response["owner"]["production_revision"]
        )
        with pytest.raises(ValueError, match="project_conflict"):
            service.snapshot_document(opened["owner"], wire["planning"], wire["title"])
        entry = service.authoring._entries[opened["owner"]["authoring_handle"]]
        assert entry.source_binding is not None and entry.initialized_sources is not None
        assert entry.initialized_sources.current()
        assert entry.timeline_history_v2 is not None
        assert entry.timeline_history_v2.authoring.clips[0].to_wire() == wire["editor"]["clips"][0]
        snapshot = service.snapshot_document(response["owner"], wire["planning"], wire["title"])
        assert snapshot["document"] == wire
        assert len(store._project_uses) == 1
        entry.source_binding.release()
        assert not store._uses and not store._project_uses
    finally:
        store.close()


@pytest.mark.parametrize("field", ("digest", "timing", "revision"))
def test_bad_relink_discards_only_new_use_preserving_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    service, wire, store, _port = service_with_retained(tmp_path, monkeypatch)
    try:
        if field == "digest":
            wire["media"][0]["content_fingerprint"] = "sha256:" + "f" * 64
        elif field == "timing":
            wire["editor"]["assets"][0]["landmarks"][-1]["duration_ticks"] += 1
        opened = service.open_document(wire)
        owner = dict(opened["owner"])
        if field == "revision":
            owner["timeline_revision"] += 1
        assert hasattr(service, "relink_document"), "explicit fresh relink port is required"
        with pytest.raises(ValueError):
            service.relink_document(
                owner,
                "video.saved",
                wire["media"][0]["retained_id"],
                deadline=time.monotonic() + 10.0,
            )
        assert not store._uses
        entry = service.authoring._entries[opened["owner"]["authoring_handle"]]
        assert entry.source_binding is None and entry.initialized_sources is None
        assert (
            service.snapshot_document(opened["owner"], wire["planning"], wire["title"])["document"]
            == wire
        )
    finally:
        store.close()
