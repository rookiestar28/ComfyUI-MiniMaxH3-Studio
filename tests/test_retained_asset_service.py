"""Finite owner-qualified commands operate actual files without reviving old authority."""

from __future__ import annotations

import importlib
import importlib.util
import json
import time
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn, cast

import pytest
from test_m25_29_production_authoring_import import _qualified_test_adapter
from test_retained_asset_source import OWNER, ready_claim

from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.recovery_owner import RecoveryOwner, RecoveryOwnerPort
from comfyui_h3_context.adapters.retained_asset_service import RetainedAssetService
from comfyui_h3_context.core.retained_assets import RetainedAssetError

MODULE = "comfyui_h3_context.adapters.retained_asset_service"


def module() -> ModuleType:
    assert importlib.util.find_spec(MODULE) is not None, "missing explicit retained service"
    return importlib.import_module(MODULE)


def service_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    artifact_bodies: tuple[bytes, ...] | None = None,
) -> tuple[RetainedAssetService, dict[str, Any], RecoveryOwner, QualifiedAVMediaAdapter, list[str]]:
    _, production, projection, _ = ready_claim(tmp_path, artifact_bodies=artifact_bodies)
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    owner = RecoveryOwner(OWNER, tmp_path / "private")
    dependencies: list[str] = []

    def runtime() -> QualifiedAVMediaAdapter:
        dependencies.append("media")
        return adapter

    # This service-only double fixes owner resolution; route tests qualify the real port.
    owner_port = cast(RecoveryOwnerPort, SimpleNamespace(resolve=lambda: owner))
    service = module().RetainedAssetService(
        owner_port=owner_port,
        production=lambda: production,
        media_runtime=runtime,
        lease=nullcontext,
    )
    command = {
        "intent": "retain",
        "expected_revision": 1,
        "workspace_handle": projection.workspace_handle,
        "workspace_id": projection.workspace_id,
        "expected_workspace_revision": projection.workspace_revision,
        "expected_workspace_fingerprint": projection.workspace_fingerprint,
        "segment_id": projection.outputs[0].segment_id,
        "output_handle": projection.outputs[0].output_handle,
    }
    return service, command, owner, adapter, dependencies


def dispatch(
    service: RetainedAssetService, action: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    return service.dispatch(action, deadline=time.monotonic() + 10.0, **kwargs)


def enable_and_retain(
    service: RetainedAssetService, command: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    dispatch(service, {"intent": "set_enabled", "enabled": True, "expected_revision": 0})
    response = dispatch(service, command)
    identifier = response["retained_id"]
    assert isinstance(identifier, str)
    return response, identifier


def test_decoder_is_closed_bounded_and_refuses_caller_authority() -> None:
    decode = module().decode_retained_action
    assert decode(b'{"intent":"status"}') == {"intent": "status"}
    malformed = [
        b'{"intent":"status","intent":"list"}',
        b"[]",
        b'"status"',
        b'{"intent":"status","path":"private"}',
        b'{"intent":"set_enabled","enabled":1,"expected_revision":0}',
        b'{"intent":"clear","expected_revision":true}',
        b'{"intent":"restore","asset_id":"asset_forged","expected_revision":1}',
        b'{"intent":"release","use_handle":"asset_' + b"a" * 32 + b'"}',
        b'{"intent":"preview"}',
        b'{"intent":"status"}' + b" " * 8192,
        b'{"intent":"status","extra":{"a":{"b":{"c":{"d":1}}}}}',
    ]
    for body in malformed:
        with pytest.raises(RetainedAssetError):
            decode(body)
    for field in ("owner_id", "project_id", "path", "url", "receipt", "facts"):
        with pytest.raises(RetainedAssetError):
            decode(json.dumps({"intent": "status", field: "private"}).encode())
    with pytest.raises(RetainedAssetError):
        module().decode_retained_preview(
            b'{"use_handle":"retained_' + b"a" * 32 + b'","source_id":"old"}'
        )


def test_default_off_status_does_not_touch_disk_production_or_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, owner, _, dependencies = service_fixture(tmp_path, monkeypatch)
    try:
        result = dispatch(service, {"intent": "status"})
        assert result["projection"]["enabled"] is False
        assert result["projection"]["assets"] == []
        assert result["projection"]["charged_bytes"] == 0
        assert dependencies == [] and not owner.private_root.exists()
    finally:
        service.close()


def test_current_retention_restore_and_explicit_preview_are_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, command, owner, _, dependencies = service_fixture(tmp_path, monkeypatch)
    previews = []

    def preview(_self: QualifiedAVMediaAdapter, **kwargs: Any) -> tuple[bytearray, str]:
        previews.append((kwargs["source_start_frame"], kwargs["frames"], kwargs["source_fps"]))
        return bytearray(b"bounded-explicit-preview"), "none"

    monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_authoring_preview", preview)
    try:
        result, identifier = enable_and_retain(service, command)
        assert result["projection"]["count"] == 1 and previews == []
        assert (
            result["projection"]["charged_bytes"] > result["projection"]["assets"][0]["byte_length"]
        )
        restored = dispatch(
            service,
            {
                "intent": "restore",
                "asset_id": identifier,
                "expected_revision": 2,
            },
        )
        handle = restored["restored"]["use_handle"]
        assert previews == [] and dependencies == ["media", "media"]
        assert not any(
            word in json.dumps(restored)
            for word in (
                str(owner.private_root),
                "source_id",
                "receipt",
                "workspace_handle",
                "source_uri",
            )
        )
        body, audio = service.preview(handle, deadline=time.monotonic() + 10.0)
        assert body == b"bounded-explicit-preview" and audio == "absent"
        assert previews == [(0, 3, 30)]
        dispatch(service, {"intent": "release", "use_handle": handle})
        with pytest.raises(RetainedAssetError, match="lease_invalid"):
            service.preview(handle, deadline=time.monotonic() + 10.0)
        assert dispatch(service, {"intent": "list"})["projection"]["count"] == 1
    finally:
        service.close()


def test_stale_catalog_selection_and_owner_cannot_run_media_or_modify_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, command, owner, _, dependencies = service_fixture(tmp_path, monkeypatch)
    try:
        dispatch(service, {"intent": "set_enabled", "enabled": True, "expected_revision": 0})
        with pytest.raises(RetainedAssetError, match="revision_conflict"):
            dispatch(service, dict(command, expected_revision=0))
        assert dependencies == []
        changed = dict(
            command, expected_workspace_revision=command["expected_workspace_revision"] + 1
        )
        with pytest.raises(RetainedAssetError, match="source_stale"):
            dispatch(service, changed)
        with pytest.raises(RetainedAssetError, match="owner_changed"):
            dispatch(
                service,
                {"intent": "status"},
                admitted_owner=RecoveryOwner("owner_" + "b" * 32, owner.private_root),
            )
        assert dispatch(service, {"intent": "status"})["projection"]["count"] == 0
    finally:
        service.close()


def test_missing_runtime_and_cancelled_restore_preserve_retained_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, command, _, adapter, _ = service_fixture(tmp_path, monkeypatch)
    try:
        _, identifier = enable_and_retain(service, command)
        service._media_runtime = lambda: None
        with pytest.raises(RetainedAssetError, match="media_unqualified"):
            dispatch(service, {"intent": "restore", "asset_id": identifier, "expected_revision": 2})
        service._media_runtime = lambda: adapter
        with pytest.raises(RetainedAssetError, match="cancelled"):
            dispatch(
                service,
                {"intent": "restore", "asset_id": identifier, "expected_revision": 2},
                cancelled=lambda: True,
            )
        assert dispatch(service, {"intent": "status"})["projection"]["count"] == 1
        assert not list(adapter._scratch_root.iterdir())
    finally:
        service.close()


def test_abandoned_restore_ack_releases_only_fresh_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, command, _, adapter, _ = service_fixture(tmp_path, monkeypatch)
    try:
        _, identifier = enable_and_retain(service, command)
        response = dispatch(
            service, {"intent": "restore", "asset_id": identifier, "expected_revision": 2}
        )
        service.discard_response(response)
        assert not list(adapter._scratch_root.iterdir())
        assert dispatch(service, {"intent": "list"})["projection"]["count"] == 1
        assert dispatch(service, {"intent": "clear", "expected_revision": 2})["cleanup"] == {
            "removed": 1,
            "protected": 0,
        }
    finally:
        service.close()


def test_inventory_reports_charged_remnants_without_implicitly_cleaning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, command, owner, _, _ = service_fixture(tmp_path, monkeypatch)
    try:
        response, _ = enable_and_retain(service, command)
        orphan = (
            owner.private_root
            / "recovery-assets/v1"
            / owner.owner_id
            / ("asset_" + "b" * 32 + ".mp4")
        )
        orphan.write_bytes(b"unproven-media")
        status = dispatch(service, {"intent": "status"})["projection"]
        assert status["charged_bytes"] == response["projection"]["charged_bytes"] + len(
            b"unproven-media"
        )
        assert status["remnant_count"] == 1 and status["count"] == 1
        assert orphan.read_bytes() == b"unproven-media"
    finally:
        service.close()


@pytest.mark.parametrize("fault", ["missing", "truncated"])
def test_missing_or_truncated_copy_reports_physical_charge_without_losing_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    original = b"synthetic-generated-video" * 1024
    service, command, owner, adapter, _ = service_fixture(
        tmp_path, monkeypatch, artifact_bodies=(original,)
    )
    try:
        response, identifier = enable_and_retain(service, command)
        directory = owner.private_root / "recovery-assets/v1" / owner.owner_id
        copy = directory / (identifier + ".mp4")
        catalog = directory / "manifest.json"
        manifest = catalog.read_bytes()
        assert copy.read_bytes() == original
        original_files = tuple(path for path in tmp_path.rglob("*.bin") if path.is_file())
        assert original_files
        originals = {path: path.read_bytes() for path in original_files}
        assert all(body == original for body in originals.values())
        if fault == "missing":
            copy.unlink()
            remaining = 0
        else:
            copy.write_bytes(b"truncated")
            remaining = len(b"truncated")
        for intent in ("status", "list"):
            current = dispatch(service, {"intent": intent})["projection"]
            assert current["assets"] == response["projection"]["assets"]
            assert current["revision"] == 2 and current["count"] == 1
            assert current["charged_bytes"] == (
                response["projection"]["charged_bytes"] - len(original) + remaining
            )
            assert current["charged_bytes"] < current["assets"][0]["byte_length"]
            assert current["protected"] == 0
        with pytest.raises(
            RetainedAssetError, match="asset_unavailable" if fault == "missing" else "asset_changed"
        ):
            dispatch(service, {"intent": "restore", "asset_id": identifier, "expected_revision": 2})
        assert catalog.read_bytes() == manifest
        assert not tuple(adapter._scratch_root.iterdir())
        if fault == "missing":
            cleared = dispatch(service, {"intent": "clear", "expected_revision": 2})
            assert cleared["cleanup"] == {"removed": 1, "protected": 0}
            assert cleared["projection"]["count"] == 0
            assert cleared["projection"]["revision"] == 3
        else:
            with pytest.raises(RetainedAssetError, match="asset_changed"):
                dispatch(service, {"intent": "clear", "expected_revision": 2})
            assert copy.read_bytes() == b"truncated" and catalog.read_bytes() == manifest
        assert {path: path.read_bytes() for path in originals} == originals
    finally:
        service.close()


def test_abandoned_restore_serialization_failure_drops_fresh_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters import comfyui_retained_assets as edge

    service, command, owner, adapter, _ = service_fixture(tmp_path, monkeypatch)
    try:
        _, identifier = enable_and_retain(service, command)

        def unavailable(_value: object) -> NoReturn:
            raise ValueError("serialization unavailable")

        monkeypatch.setattr(edge, "json_bytes", unavailable)
        cancellation = edge._Cancellation(
            SimpleNamespace(transport=SimpleNamespace(is_closing=lambda: False))
        )
        with pytest.raises(ValueError, match="serialization unavailable"):
            edge._work(
                service,
                owner,
                {"intent": "restore", "asset_id": identifier, "expected_revision": 2},
                time.monotonic() + 10.0,
                cancellation,
            )
        assert not tuple(adapter._scratch_root.iterdir())
        assert dispatch(service, {"intent": "list"})["projection"]["count"] == 1
    finally:
        service.close()


def test_revocation_does_not_depend_on_readable_retained_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, command, owner, adapter, _ = service_fixture(tmp_path, monkeypatch)
    try:
        _, identifier = enable_and_retain(service, command)
        restored = dispatch(
            service, {"intent": "restore", "asset_id": identifier, "expected_revision": 2}
        )
        catalog = owner.private_root / "recovery-assets/v1" / owner.owner_id / "manifest.json"
        catalog.write_bytes(b"corrupt-catalog")
        with pytest.raises(RetainedAssetError):
            dispatch(
                service, {"intent": "release", "use_handle": restored["restored"]["use_handle"]}
            )
        assert tuple(adapter._scratch_root.iterdir()) == ()
        assert catalog.read_bytes() == b"corrupt-catalog"
    finally:
        service.close()


def test_nonuniform_frame_timing_is_restorable_but_never_guessed_for_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import test_m25_29_production_authoring_import as fixtures
    from test_m25_authoring_video_facts import _probe_wire

    monkeypatch.setattr(fixtures, "_video_probe_wire", lambda **_kwargs: _probe_wire(audio=False))
    service, command, _, _, _ = service_fixture(tmp_path, monkeypatch)
    try:
        _, identifier = enable_and_retain(service, command)
        restored = dispatch(
            service, {"intent": "restore", "asset_id": identifier, "expected_revision": 2}
        )
        assert restored["restored"]["preview_available"] is False
        with pytest.raises(RetainedAssetError, match="media_unqualified"):
            service.preview(restored["restored"]["use_handle"], deadline=time.monotonic() + 10.0)
        assert dispatch(service, {"intent": "list"})["projection"]["count"] == 1
    finally:
        service.close()
