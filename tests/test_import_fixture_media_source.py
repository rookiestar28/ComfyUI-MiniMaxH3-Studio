"""Single-original fixture reads retain the real import/source authorities."""

from __future__ import annotations

import base64
import hashlib
import os
import time
from collections.abc import Generator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
import test_m25_29_production_authoring_import as accepted

from comfyui_h3_context.adapters.authoring_generated_source import GeneratedAuthoringVideoSource
from comfyui_h3_context.adapters.authoring_source_binding import claim_transferred_authoring_source
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.comfyui_production_workspace import PRODUCTION_ACTION_SCHEMA
from comfyui_h3_context.adapters.segment_artifact_store import _is_link_or_reparse
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from scripts import m25_16_import_fixture as fixture

ImportedFixture = tuple[fixture._Service, list[str], Any, GeneratedAuthoringVideoSource]


@pytest.fixture
def imported(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Generator[ImportedFixture]:
    # Bootstrap installs the content-free probe seam; restore it after this owned service.
    monkeypatch.setattr(
        QualifiedAVMediaAdapter,
        "probe_authoring_video_source",
        QualifiedAVMediaAdapter.probe_authoring_video_source,
    )
    service = fixture._Service()
    try:
        boot = service.bootstrap(2)
        assert boot["status"] == 200
        assert service._production is not None
        assert service._authoring is not None
        assert service._authoring_workspace_handle is not None
        projection = service._production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "fixture.source.read",
                "action": "read_projection",
                "payload": {"workspace_handle": service._workspace_handle},
            }
        ).projection
        assert isinstance(projection, ProductionWorkbenchProjection)
        import_request = accepted._import_request(projection, boot["authoring"], boot["history"])
        response = service.import_outputs(import_request.to_wire())
        assert response["status"] == 200
        assets = [row["asset_id"] for row in response["body"]["receipt"]["rows"]]
        placed = () if getattr(request, "param", None) == "asset-only" else assets[:1]
        for index, asset in enumerate(placed):
            accepted._place_imported_asset_for_render(
                service._authoring,
                workspace_handle=service._authoring_workspace_handle,
                asset_id=asset,
                request_id=f"fixture.source.place.{index}",
            )
        entry = service._authoring._entries[service._authoring_workspace_handle]
        assert entry.initialized_sources is not None
        claim = next(x for x in entry.initialized_sources.sources if x.asset.asset_id == assets[0])
        source = claim_transferred_authoring_source(claim._receipt, assets[0])
        assert isinstance(source, GeneratedAuthoringVideoSource)
        yield service, assets, entry, source
        assert source._borrowers == 0
    finally:
        service.close()


def test_single_imported_body_does_not_prepare_a_composition(imported: ImportedFixture) -> None:
    service, assets, _entry, source = imported
    with patch.object(
        service._authoring, "prepare_render_plan", side_effect=AssertionError("whole composition")
    ):
        reply = service.media_source(assets[0])
    assert reply["status"] == 200
    body = base64.b64decode(reply["media_base64"], validate=True)
    assert body == source.lease.path.read_bytes()
    assert reply["byte_length"] == len(body)
    assert reply["source_fingerprint"] == "sha256:" + hashlib.sha256(body).hexdigest()
    assert source._borrowers == 0


def test_foreign_and_missing_assets_are_refused(imported: ImportedFixture) -> None:
    service, _assets, _entry, _source = imported
    for asset in ("missing", "foreign-asset"):
        assert service.media_source(asset) == {"status": 404, "error": "source_not_found"}
    for invalid_asset in (None, "", "a" * 257, 1):
        assert service.media_source(invalid_asset)["status"] == 400


def test_catalog_asset_need_not_have_a_placed_clip(imported: ImportedFixture) -> None:
    service, assets, _entry, _source = imported
    assert service.media_source(assets[1])["status"] == 200


@pytest.mark.parametrize("imported", ["asset-only"], indirect=True)
def test_asset_only_requests_keep_render_boundary(imported: ImportedFixture) -> None:
    service, assets, _entry, _source = imported
    assert service.media_source(assets[0]) == {
        "status": 409,
        "error": "render_snapshot_unavailable",
    }


@pytest.mark.parametrize("change", ["history", "revision", "binding", "release", "production"])
def test_authority_change_during_read_refuses_body(imported: ImportedFixture, change: str) -> None:
    service, assets, entry, source = imported
    from comfyui_h3_context.adapters import av_reconstruction_media as reader

    original = reader._read_regular_media_body
    changed = False

    def read(*args: Any, **kwargs: Any) -> tuple[bytearray, str]:
        nonlocal changed
        result = original(*args, **kwargs)
        if args[0] == source.lease.path and not changed:
            changed = True
            if change == "history":
                entry.timeline_history_v2 = replace(entry.timeline_history_v2)
            elif change == "revision":
                entry.timeline = replace(entry.timeline, revision=entry.timeline.revision + 1)
            elif change == "binding":
                entry.source_binding = None
            elif change == "release":
                entry.source_binding.release()
            else:
                service.touch_production()
        return result

    with patch.object(reader, "_read_regular_media_body", side_effect=read):
        reply = service.media_source(assets[0])
    assert changed
    assert reply["status"] != 200
    assert "media_base64" not in reply
    assert source._borrowers == 0


def test_same_stat_body_corruption_is_refused(imported: ImportedFixture) -> None:
    service, assets, _entry, source = imported
    path = source.lease.path
    metadata = path.stat()
    body = path.read_bytes()
    path.write_bytes(bytes([body[0] ^ 1]) + body[1:])
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert path.stat().st_size == metadata.st_size
    reply = service.media_source(assets[0])
    assert reply["status"] != 200 and "media_base64" not in reply


def test_hardlinked_source_is_refused(imported: ImportedFixture) -> None:
    service, assets, _entry, source = imported
    link = source.lease.path.with_name("fixture-linked-source")
    os.link(source.lease.path, link)
    try:
        assert service.media_source(assets[0])["status"] != 200
    finally:
        link.unlink()


def test_symlinked_source_is_refused(imported: ImportedFixture) -> None:
    service, assets, _entry, source = imported
    path = source.lease.path
    target = path.with_name("fixture-symlink-target")
    path.rename(target)
    try:
        try:
            path.symlink_to(target.name)
        except OSError as error:
            if getattr(error, "winerror", None) != 1314:
                raise
            # Windows may deny symlink creation; still exercise the real reader's
            # link/reparse refusal, rather than skip the security-negative case.
            target.rename(path)
            from comfyui_h3_context.adapters import av_reconstruction_media as reader

            original = _is_link_or_reparse
            with patch.object(
                reader,
                "_is_link_or_reparse",
                side_effect=lambda candidate, metadata: (
                    candidate == path or original(candidate, metadata)
                ),
            ):
                assert service.media_source(assets[0])["status"] != 200
            path.rename(target)
        else:
            assert service.media_source(assets[0])["status"] != 200
    finally:
        path.unlink(missing_ok=True)
        target.rename(path)


def test_deadline_after_read_is_refused_and_borrower_released(
    imported: ImportedFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, assets, _entry, source = imported
    from comfyui_h3_context.adapters import av_reconstruction_media as reader

    original = reader._read_regular_media_body
    reads = 0

    def read(*args: Any, **kwargs: Any) -> tuple[bytearray, str]:
        nonlocal reads
        result = original(*args, **kwargs)
        if args[0] == source.lease.path:
            reads += 1
            if reads == 2:
                monkeypatch.setattr(time, "monotonic", lambda: kwargs["deadline"] + 1)
        return result

    with patch.object(reader, "_read_regular_media_body", side_effect=read):
        assert service.media_source(assets[0]) == {"status": 409, "error": "source_expired"}
    assert source._borrowers == 0


def test_expired_workspace_is_refused(
    imported: ImportedFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, assets, _entry, _source = imported
    assert service._authoring is not None
    clock = service._authoring._clock
    monkeypatch.setattr(service._authoring, "_clock", lambda: clock() + 100_000)
    assert service.media_source(assets[0])["status"] == 410


def test_body_and_encoded_caps_are_refused_and_cleaned(
    imported: ImportedFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, assets, _entry, source = imported
    monkeypatch.setattr(fixture, "MAX_IMPORTED_MEDIA_BYTES", 1)
    assert service.media_source(assets[0])["status"] == 413
    assert source._borrowers == 0
    monkeypatch.setattr(fixture, "MAX_IMPORTED_MEDIA_BYTES", 1_000_000)
    monkeypatch.setattr(fixture, "MAX_IMPORTED_MEDIA_BASE64_BYTES", 1)
    assert service.media_source(assets[0])["status"] == 413
    assert source._borrowers == 0


def test_teardown_cannot_serve_an_original(imported: ImportedFixture) -> None:
    service, assets, _entry, source = imported
    service.close()
    assert service.media_source(assets[0])["status"] != 200
    assert source._borrowers == 0
