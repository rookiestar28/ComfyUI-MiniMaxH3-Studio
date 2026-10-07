"""Real Production claims and byte leases gate optional retention, not serialized facts."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import pickle
import time
from pathlib import Path
from types import ModuleType

import pytest
from test_m25_29_production_authoring_import import (
    _qualified_test_adapter,
    _registries_with_ready_output,
)

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionAuthoringOutputClaim,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.retained_asset_source import VerifiedRetentionSource
from comfyui_h3_context.adapters.segment_artifact_store import PrivateSegmentArtifactStore
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.retained_assets import RetainedAssetError

MODULE = "comfyui_h3_context.adapters.retained_asset_source"
OWNER = "owner_" + "a" * 32


def source_module() -> ModuleType:
    assert importlib.util.find_spec(MODULE) is not None, "missing factory-owned retained source"
    return importlib.import_module(MODULE)


def ready_claim(
    tmp_path: Path,
    *,
    artifact_bodies: tuple[bytes, ...] | None = None,
) -> tuple[
    ProductionAuthoringOutputClaim,
    ProductionWorkspaceRegistry,
    ProductionWorkbenchProjection,
    PrivateSegmentArtifactStore,
]:
    production, projection, _, _, _, store = _registries_with_ready_output(
        tmp_path, artifact_bodies=artifact_bodies
    )
    segment_id = projection.outputs[0].segment_id
    assert segment_id is not None
    claim = production.claim_authoring_output_batch(
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        expected_workspace_revision=projection.workspace_revision,
        expected_workspace_fingerprint=projection.workspace_fingerprint,
        pairs=((segment_id, projection.outputs[0].output_handle),),
    )[0]
    return claim, production, projection, store


def stage(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    VerifiedRetentionSource,
    ProductionAuthoringOutputClaim,
    ProductionWorkspaceRegistry,
    ProductionWorkbenchProjection,
    PrivateSegmentArtifactStore,
]:
    claim, production, projection, store = ready_claim(tmp_path)
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    source = module.stage_retention_source(
        claim=claim,
        owner_id=OWNER,
        media_adapter=adapter,
        scratch_root=tmp_path / "authoring-scratch",
        deadline=time.monotonic() + 10.0,
    )
    return source, claim, production, projection, store


def test_live_claim_streams_exact_admitted_bytes_and_releases_its_owned_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = source_module()
    source, claim, _, _, store = stage(module, tmp_path, monkeypatch)
    destination = tmp_path / "retained.mp4"
    try:
        assert module.require_retention_source(source, owner_id=OWNER) is source
        size, digest = source.copy_into(destination, deadline=time.monotonic() + 10.0)
        body = destination.read_bytes()
        assert body == b"bounded-generated-video-1"
        assert size == len(body) == source.facts.byte_length
        assert (
            digest
            == source.facts.content_fingerprint
            == "sha256:" + hashlib.sha256(body).hexdigest()
        )
        assert source.current()
        assert store.inspect(claim.receipt).status.value == "reusable"
    finally:
        source.release()
    assert not source.current()
    assert not list((tmp_path / "authoring-scratch").iterdir())
    assert destination.read_bytes() == b"bounded-generated-video-1"


def test_paths_metadata_receipts_and_unissued_objects_cannot_select_media(tmp_path: Path) -> None:
    module = source_module()
    for value in (
        tmp_path / "source.mp4",
        {"content_fingerprint": "sha256:" + "a" * 64},
        {"receipt": "old"},
        object.__new__(module.VerifiedRetentionSource),
    ):
        with pytest.raises(RetainedAssetError, match="source_unavailable"):
            module.require_retention_source(value, owner_id=OWNER)
    with pytest.raises(RetainedAssetError):
        module.stage_retention_source(
            claim={"path": str(tmp_path)},
            owner_id=OWNER,
            media_adapter=None,
            scratch_root=tmp_path,
            deadline=time.monotonic() + 10.0,
        )


def test_cross_owner_copy_and_serialization_are_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = source_module()
    source, _, _, _, _ = stage(module, tmp_path, monkeypatch)
    try:
        with pytest.raises(RetainedAssetError, match="owner_mismatch"):
            module.require_retention_source(source, owner_id="owner_" + "b" * 32)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError):
                operation(source)
    finally:
        source.release()
    with pytest.raises(RetainedAssetError, match="source_stale"):
        module.require_retention_source(source, owner_id=OWNER)


def test_revoked_production_claim_cannot_copy_or_remain_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = source_module()
    source, _, production, projection, _ = stage(module, tmp_path, monkeypatch)
    try:
        production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "retention.release",
                "action": "release_workspace",
                "payload": {
                    "workspace_handle": projection.workspace_handle,
                    "expected_workspace_revision": projection.workspace_revision,
                    "expected_workspace_fingerprint": projection.workspace_fingerprint,
                },
            }
        )
        assert not source.current()
        with pytest.raises(RetainedAssetError, match="source_stale"):
            source.copy_into(tmp_path / "never.mp4", deadline=time.monotonic() + 10.0)
        assert not (tmp_path / "never.mp4").exists()
    finally:
        source.release()


@pytest.mark.parametrize(
    "cancelled, deadline, code",
    (
        (True, 10.0, "cancelled"),
        (False, -1.0, "timed_out"),
    ),
)
def test_copy_cancellation_and_deadline_leave_no_new_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
    deadline: float,
    code: str,
) -> None:
    module = source_module()
    source, _, _, _, _ = stage(module, tmp_path, monkeypatch)
    try:
        with pytest.raises(RetainedAssetError, match=code):
            source.copy_into(
                tmp_path / "never.mp4",
                deadline=time.monotonic() + deadline,
                cancelled=lambda: cancelled,
            )
        assert not (tmp_path / "never.mp4").exists()
    finally:
        source.release()


def test_changed_leased_bytes_do_not_inherit_the_original_receipt_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = source_module()
    source, claim, _, _, store = stage(module, tmp_path, monkeypatch)
    try:
        leased = next((tmp_path / "authoring-scratch").rglob("artifact.mp4"))
        leased.write_bytes(b"x" * source.facts.byte_length)
        with pytest.raises(RetainedAssetError, match="asset_changed"):
            source.copy_into(tmp_path / "unadmitted.mp4", deadline=time.monotonic() + 10.0)
        assert store.inspect(claim.receipt).status.value == "reusable"
    finally:
        source.release()
