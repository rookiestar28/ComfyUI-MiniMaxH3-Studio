"""M25-40 original-owner preparation regressions on real Production registries."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn, cast

import pytest
from test_m25_35_production_membership import _Harness

from comfyui_h3_context.adapters.comfyui_authoring_workspace import (
    AUTHORING_ACTION_SCHEMA,
    AuthoringWorkbenchError,
    AuthoringWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarAuthoringSeed,
    SidebarAuthoringSource,
    SidebarProductionSeed,
)
from comfyui_h3_context.core import SegmentDuration, canonical_fingerprint
from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection


def test_verified_managed_project_retains_original_seed_after_context_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, terminal = harness.start_v2("cold-import")
    assert terminal.disposition == "succeeded"
    assert project.workspace is not None and len(project.workspace.outputs) == 1

    def expired_context(_handle: str) -> NoReturn:
        raise KeyError("original Context expired")

    harness.production._seed_claim = expired_context
    seed = harness.production.claim_authoring_seed_for_project(
        project.workspace_handle, project.workspace_id
    )
    assert seed.task_mode is TaskMode.T2VA
    assert seed.sources == ()
    assert seed.lineage_token is harness.production._entries[project.workspace_handle].lineage_token
    with pytest.raises(ProductionWorkbenchError) as foreign:
        harness.production.claim_authoring_seed_for_project(
            project.workspace_handle, "workspace.foreign"
        )
    assert (foreign.value.status, foreign.value.code) == (409, "destination_mismatch")


def test_reference_project_keeps_original_identity_without_claiming_context_media() -> None:
    lineage = object()
    fingerprint = canonical_fingerprint({"original": "video-1"})
    authoring_seed = SidebarAuthoringSeed(
        source_id="report.original",
        task_mode=TaskMode.T2VA,
        registry_fingerprint=fingerprint,
        sources=(
            SidebarAuthoringSource(
                asset_id="video-1",
                kind=MediaKind.VIDEO,
                duration_milliseconds=4_000,
                paired_video_id=None,
                connection_order=1,
                identity_fingerprint=canonical_fingerprint({"video": 1}),
            ),
        ),
        lineage_token=lineage,
    )
    seed = SidebarProductionSeed(
        task_mode=TaskMode.T2VA,
        source_id=authoring_seed.source_id,
        reference_ids=("video-1",),
        duration=SegmentDuration.from_frame_count(124),
        accepted_intent_fingerprint=canonical_fingerprint({"intent": 1}),
        profile_fingerprint=canonical_fingerprint({"profile": 1}),
        reference_registry_fingerprint=fingerprint,
        native_binding_fingerprint=canonical_fingerprint({"native": 1}),
        producer_settings_fingerprint=canonical_fingerprint({"settings": 1}),
        seed_fingerprint=canonical_fingerprint({"seed": 1}),
        lineage_token=lineage,
        authoring_seed=authoring_seed,
    )
    source = [seed]
    production = ProductionWorkspaceRegistry(seed_claim=lambda _handle: source[0])
    project = production.dispatch(
        {
            "schema": "h3.context.production_workbench.action.v1",
            "request_id": "create.original",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": "ws_" + "c" * 32},
        }
    ).projection
    assert isinstance(project, ProductionWorkbenchProjection)
    source.clear()
    retained = production.claim_authoring_seed_for_project(
        project.workspace_handle, project.workspace_id
    )
    assert retained is authoring_seed
    authoring_now = [1_000.0]
    authoring = AuthoringWorkspaceRegistry(clock=lambda: authoring_now[0], ttl_seconds=900)
    target = authoring.ensure_from_production(
        production,
        workspace_handle=project.workspace_handle,
        workspace_id=project.workspace_id,
        preferred_authoring_handle=None,
    )
    assert target.status == 201
    assert target.body is not None
    assert target.body["context_source_id"] == "report.original"
    target_reference = cast(dict[str, object], target.body["reference"])
    target_sources = cast(list[dict[str, object]], target_reference["sources"])
    assert target_sources[0]["reason"] == "source_not_bound"
    target_timeline = cast(dict[str, object], target.body["timeline"])
    initialized = authoring.dispatch(
        {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "initialize.original",
            "action": "initialize_timeline_history",
            "payload": {
                "workspace_handle": target.body["workspace_handle"],
                "expected_reference_revision": target_reference["revision"],
                "expected_timeline_revision": target_timeline["revision"],
                "authoring_schema": NLE_AUTHORING_SCHEMA,
                "profile_id": NLE_AUTHORING_PROFILE_ID,
                "operation_profile_id": NLE_OPERATION_PROFILE_ID,
            },
        }
    )
    assert initialized.status == 200
    authoring_now[0] += 901.0
    replacement = authoring.ensure_from_production(
        production,
        workspace_handle=project.workspace_handle,
        workspace_id=project.workspace_id,
        preferred_authoring_handle=None,
    )
    assert replacement.status == 201
    assert replacement.body is not None
    assert replacement.body["workspace_handle"] != target.body["workspace_handle"]
    replacement_reference = cast(dict[str, object], replacement.body["reference"])
    with pytest.raises(AuthoringWorkbenchError) as unbound:
        authoring.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "add.original",
                "action": "add_source",
                "payload": {
                    "workspace_handle": replacement.body["workspace_handle"],
                    "expected_reference_revision": replacement_reference["revision"],
                    "source_id": "video-1",
                },
            }
        )
    assert (unbound.value.status, unbound.value.code) == (422, "source_not_bound")
    authoring.dispatch(
        {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "release.original",
            "action": "release_workspace",
            "payload": {"workspace_handle": replacement.body["workspace_handle"]},
        }
    )
    with pytest.raises(AuthoringWorkbenchError) as released:
        authoring.ensure_from_production(
            production,
            workspace_handle=project.workspace_handle,
            workspace_id=project.workspace_id,
            preferred_authoring_handle=None,
        )
    assert (released.value.status, released.value.code) == (410, "associated_editor_gone")
