"""Managed readiness follows the real canonical Production import and root wiring."""

from pathlib import Path
from typing import Any

import pytest
from native_source_doubles import composition_observation, observed_source
from test_m26_03_planning_service import action, environment, prepared_proposal, selectors

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from scripts.hc_09_host_seam_test_double import host_nodes_module


@pytest.mark.parametrize(
    "scenario",
    [
        "i2va",
        "fl2va",
        "l2va",
        "visual_ref2va",
        "text_audio",
        "image_audio",
        "paired_video",
        "mixed_audio",
    ],
)
def test_real_media_plans_do_not_claim_missing_managed_host_bindings(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    from dataclasses import replace
    from decimal import Decimal

    from test_generation_profile import observation
    from test_native_h3_adapter import (
        _audio_only_reference_report,
        _keyframe_report,
        _paired_reference_report,
        _registry_report,
        _single_keyframe_report,
    )

    from comfyui_h3_context.adapters import comfyui_generation_profile
    from comfyui_h3_context.adapters.comfyui_production_workspace import (
        ProductionWorkspaceRegistry,
    )
    from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
    from comfyui_h3_context.core import (
        AssetRole,
        MediaKind,
        MediaMetadata,
        ReferenceAsset,
        TaskMode,
        build_reference_registry,
    )
    from comfyui_h3_context.core.native_h3 import build_native_h3_wiring
    from comfyui_h3_context.core.ui_projection import ExecutionCorrelation

    image = ReferenceAsset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1)
    audio = ReferenceAsset(
        "audio",
        MediaKind.AUDIO,
        AssetRole.AUDIO_SOURCE,
        2,
        metadata=MediaMetadata(duration_seconds=Decimal("5")),
    )
    if scenario in ("i2va", "l2va"):
        report = _single_keyframe_report(TaskMode(scenario))
    elif scenario == "fl2va":
        report = _keyframe_report()
    elif scenario == "text_audio":
        report = _audio_only_reference_report()
    elif scenario == "paired_video":
        report = _paired_reference_report()
    else:
        assets: tuple[ReferenceAsset, ...] = (
            (image,) if scenario == "visual_ref2va" else (image, audio)
        )
        if scenario == "mixed_audio":
            assets = (
                image,
                ReferenceAsset("video", MediaKind.VIDEO, AssetRole.REFERENCE, 2),
                replace(audio, connection_order=3),
            )
        report = _registry_report(build_reference_registry(assets))
    sidebar = SidebarWorkspaceRegistry(clock=lambda: 10.0)
    source = sidebar.publish(
        report, build_native_h3_wiring(report), ExecutionCorrelation("source", "node")
    )
    production = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed, clock=lambda: 10.0
    )
    monkeypatch.setattr(
        composition_root,
        "_INSTANCES",
        {
            composition_root.SIDEBAR_WORKSPACE: sidebar,
            composition_root.PRODUCTION_WORKSPACE: production,
        },
    )
    created = production.dispatch(
        {
            "schema": "h3.context.production_workbench.action.v1",
            "request_id": "create",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": source.workspace_id},
        }
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    planning = composition_root.get(composition_root.PRODUCTION_PLANNING)
    monkeypatch.setattr(planning, "_clock", lambda: 10.0)
    host = observation()
    _install_loader_inventory(monkeypatch)
    monkeypatch.setattr(comfyui_generation_profile, "observe_host", lambda *_args: host)
    monkeypatch.setattr(
        comfyui_generation_profile,
        "_observe_native_composition",
        lambda: composition_observation(host, observed_source()),
    )
    prepare = {
        "workspace_handle": created.workspace_handle,
        "expected_workspace_revision": created.workspace_revision,
        "expected_workspace_fingerprint": created.workspace_fingerprint,
        "context_workspace_handle": source.workspace_id,
        "expected_report_revision": source.report_revision,
        "expected_report_fingerprint": source.report_fingerprint,
        "expected_planning_revision": 0,
        "target_seconds": 20,
        "policy": "fixed_10",
    }
    # A single-frame source plans like every other media source: its alignment sentence is derived
    # per segment, so no semantic review stands between the proposal and the import. What must
    # still hold for it is the readiness outcome asserted below.
    _, proposal = prepared_proposal(planning, prepare)
    if scenario in ("i2va", "l2va"):
        modes = [row["task_mode"] for row in proposal["proposal"]["segments"]]
        assert modes == (["i2va", "t2va"] if scenario == "i2va" else ["t2va", "l2va"])
    imported = planning.dispatch(
        action(
            "import_plan",
            {**selectors(proposal), "proposal_id": proposal["proposal"]["proposal_id"]},
            "import.media",
        )
    ).to_wire()
    registry = composition_root.get(composition_root.MANAGED_MODE_QUALIFICATION)
    monkeypatch.setattr(registry, "_clock", lambda: 10.0)
    monkeypatch.setattr(registry._producer, "_clock", lambda: 10.0)
    before = set(sidebar._entries)
    result = planning.dispatch(
        action(
            "prepare_managed_readiness",
            {
                "workspace_handle": created.workspace_handle,
                "expected_workspace_revision": imported["workspace_revision"],
                "expected_workspace_fingerprint": imported["workspace_fingerprint"],
                "expected_plan_fingerprint": imported["plan_fingerprint"],
            },
            "ready.media",
        )
    ).to_wire()
    assert result["status"] == "held"
    assert result["reason"] in (
        "qualification_host_bindings_unavailable",
        "qualification_composition_unqualified",
    )
    assert result["qualification"] is None and registry._current is None
    assert set(sidebar._entries) == before
    assert composition_root.MANAGED_SEQUENCE not in composition_root._INSTANCES


def _install_loader_inventory(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, list[str]]]:
    import json
    from types import SimpleNamespace

    from comfyui_h3_context.adapters import comfyui_generation_profile

    manifest = json.loads(comfyui_generation_profile._OFFICIAL_ASSET_MANIFEST_PATH.read_text())
    inventories: dict[str, dict[str, list[str]]] = {}
    for row in manifest["slots"]:
        inventories.setdefault(row["loader_type"], {}).setdefault(row["widget_name"], []).append(
            row["template_default"]
        )
    # Observable host metadata is the sole injected fixture. The real inventory observer,
    # composition qualifier, canonical materializer and publication path all execute unchanged.
    nodes = {
        name: SimpleNamespace(
            INPUT_TYPES=lambda widgets=widgets: {
                "required": {key: (values,) for key, values in widgets.items()}
            }
        )
        for name, widgets in inventories.items()
    }
    host_nodes = host_nodes_module()
    host_nodes.NODE_CLASS_MAPPINGS.update(nodes)
    original = comfyui_generation_profile._host_module
    monkeypatch.setattr(
        comfyui_generation_profile,
        "_host_module",
        lambda name: host_nodes if name == "nodes" else original(name),
    )
    return inventories


def _root_ready_environment(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    from test_generation_profile import observation

    from comfyui_h3_context.adapters import comfyui_generation_profile

    _service, sidebar, production, _source, prepare, clock = environment()
    monkeypatch.setattr(
        composition_root,
        "_INSTANCES",
        {
            composition_root.SIDEBAR_WORKSPACE: sidebar,
            composition_root.PRODUCTION_WORKSPACE: production,
        },
    )
    service = composition_root.get(composition_root.PRODUCTION_PLANNING)
    monkeypatch.setattr(service, "_clock", lambda: clock[0])
    # host[0] is the observed host, host[1] the loaded native source observation.
    host: list[Any] = [observation(), observed_source()]
    _install_loader_inventory(monkeypatch)
    monkeypatch.setattr(comfyui_generation_profile, "observe_host", lambda *_args: host[0])
    monkeypatch.setattr(
        comfyui_generation_profile,
        "_observe_native_composition",
        lambda: composition_observation(host[0], host[1]),
    )
    _, proposed = prepared_proposal(service, prepare)
    imported = service.dispatch(
        action(
            "import_plan",
            {**selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
            "import.readiness",
        )
    ).to_wire()
    registry = composition_root.get(composition_root.MANAGED_MODE_QUALIFICATION)
    monkeypatch.setattr(registry, "_clock", lambda: clock[0])
    monkeypatch.setattr(registry._producer, "_clock", lambda: clock[0])
    selection = {
        "workspace_handle": prepare["workspace_handle"],
        "expected_workspace_revision": imported["workspace_revision"],
        "expected_workspace_fingerprint": imported["workspace_fingerprint"],
        "expected_plan_fingerprint": imported["plan_fingerprint"],
    }
    return service, registry, selection, clock, host, sidebar


def test_root_prepare_read_and_replay_keep_original_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, registry, selection, clock, _host, sidebar = _root_ready_environment(monkeypatch)
    before = set(sidebar._entries)
    request = action("prepare_managed_readiness", selection, "prepare.once")
    prepared = service.dispatch(request).to_wire()
    assert prepared["status"] == "ready"
    assert prepared["qualification"]["guide_readiness"] == ["incomplete", "incomplete"]
    qualification = registry._current
    assert qualification.observed_at == 10
    assert qualification.expires_at == 70
    clock[0] = 20
    assert service.dispatch(request).to_wire() == prepared
    read = service.dispatch(
        action(
            "read_managed_readiness",
            {**selection, "qualification_fingerprint": qualification.fingerprint},
            "read.once",
        )
    ).to_wire()
    assert read["qualification"] == prepared["qualification"]
    assert registry._current is qualification
    assert set(sidebar._entries) == before
    clock[0] = 70
    expired = service.dispatch(request).to_wire()
    assert expired["status"] == "held"
    assert expired["qualification"] is None
    assert registry._current is None
    assert set(sidebar._entries) == before


def test_current_host_drift_invalidates_without_reacquiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_generation_profile import observation

    service, registry, selection, _clock, host, sidebar = _root_ready_environment(monkeypatch)
    request = action("prepare_managed_readiness", selection, "prepare.host")
    assert service.dispatch(request).to_wire()["status"] == "ready"
    before = set(sidebar._entries)
    host[0] = observation(anchors=frozenset())
    assert service.dispatch(request).to_wire()["status"] == "held"
    host[0] = observation()
    assert service.dispatch(request).to_wire()["status"] == "held"
    assert registry._current is None
    assert set(sidebar._entries) == before


def test_root_qualification_starts_real_parent_and_releases_actual_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from comfyui_h3_context.adapters.comfyui_sequence_coordinator import SequenceCoordinatorRegistry
    from comfyui_h3_context.adapters.managed_sequence_service import (
        AuthorizeManagedSequenceRequestV1,
        ManagedSequenceMutationAuthorityV1,
        PrepareManagedSequenceChildRequestV1,
        ReadManagedPreparedContextRequestV1,
        StartManagedSequenceRequestV1,
    )

    planning, registry, selection, clock, _host, sidebar = _root_ready_environment(monkeypatch)
    production = composition_root.get(composition_root.PRODUCTION_WORKSPACE)
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production,
        output_root_factory=lambda: tmp_path / "output",
        private_root_factory=lambda: tmp_path / "private",
        clock=lambda: clock[0],
    )
    composition_root.install(composition_root.SEQUENCE_COORDINATOR, coordinator)
    managed = composition_root.get(composition_root.MANAGED_SEQUENCE)
    monkeypatch.setattr(managed, "_clock", lambda: clock[0])
    ready = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.parent")
    ).to_wire()
    assert ready["status"] == "ready"
    qualification = registry.claim(ready["qualification_fingerprint"])
    assert qualification is not None
    authority = production.claim_automatic_plan_authority(
        selection["workspace_handle"],
        **{key: value for key, value in selection.items() if key != "workspace_handle"},
    )
    authorized = managed.authorize(
        AuthorizeManagedSequenceRequestV1(
            request_id="authorize.parent",
            **selection,
            generation_plan_fingerprint=authority.proposal.fingerprint,
            compiler_fingerprint=qualification.baseline.compiler_fingerprint,
            host_capability_fingerprint=qualification.baseline.host_capability_fingerprint,
            explicit_intent="generate_approved_sequence",
        )
    )
    started = managed.start(
        StartManagedSequenceRequestV1(
            request_id="start.parent",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=authorized.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        qualification=qualification,
    )
    assert started.state.value == "active"
    assert len(coordinator._managed_start_resources) == 1
    prepared = managed.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.child",
            parent_sequence_id=started.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=started.authorization_fingerprint,
            segment_id=authority.proposal.segments[0].segment_id,
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None
    # Consume exactly the authority handed to the real browser resolver, not a
    # separately selected revision that can conceal a stale execution receipt.
    assert prepared.execution.slot_revision == prepared.revision
    report = managed.read_prepared_child_context(
        ReadManagedPreparedContextRequestV1(
            request_id="read.child",
            parent_sequence_id=prepared.parent_sequence_id,
            expected_revision=prepared.execution.slot_revision,
            authorization_fingerprint=prepared.authorization_fingerprint,
            segment_id=authority.proposal.segments[0].segment_id,
            eligible_execution_fingerprint=prepared.execution.fingerprint,
            materialization_receipt_fingerprint=authority.materialization_receipts[0].fingerprint,
            context_workspace_handle=prepared.context_workspace_handle,
        )
    ).to_wire()
    assert report["canonical_prompt"] == authority.proposal.segments[0].local_prompt
    cancelled = managed.cancel(
        ManagedSequenceMutationAuthorityV1(
            request_id="cancel.parent",
            parent_sequence_id=prepared.parent_sequence_id,
            expected_revision=prepared.revision,
            authorization_fingerprint=prepared.authorization_fingerprint,
        )
    )
    assert cancelled.state.value == "cancelled"
    released = coordinator._managed_start_resources[started.parent_sequence_id]
    assert released.ledger_released
    assert released.child_slot_released
    assert released.artifact_capacity_released
    assert prepared.context_workspace_handle not in sidebar._entries


def test_plan_replacement_between_observation_and_publication_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.comfyui_production_workspace import PRODUCTION_ACTION_SCHEMA

    planning, registry, selection, _clock, _host, _sidebar = _root_ready_environment(monkeypatch)
    production = composition_root.get(composition_root.PRODUCTION_WORKSPACE)
    original = registry._producer.observe

    def release_after_observation(**kwargs: Any) -> Any:
        qualification = original(**kwargs)
        production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "release.during.prepare",
                "action": "release_workspace",
                "payload": {
                    key: value
                    for key, value in selection.items()
                    if key != "expected_plan_fingerprint"
                },
            }
        )
        return qualification

    monkeypatch.setattr(registry._producer, "observe", release_after_observation)
    result = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.race")
    ).to_wire()
    assert result["status"] == "held"
    assert result["qualification"] is None
    assert registry._current is None


def test_cleanup_failure_is_bounded_and_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planning, registry, selection, _clock, _host, sidebar = _root_ready_environment(monkeypatch)
    production = composition_root.get(composition_root.PRODUCTION_WORKSPACE)
    original = production.materialize_automatic_plan_segment
    before = set(sidebar._entries)

    def fail_release(*args: Any, **kwargs: Any) -> Any:
        claim, receipt = original(*args, **kwargs)
        release = claim._release

        def cleanup() -> None:
            release()
            raise RuntimeError("private cleanup details must never reach a response")

        monkeypatch.setattr(claim, "_release", cleanup)
        return claim, receipt

    monkeypatch.setattr(production, "materialize_automatic_plan_segment", fail_release)
    result = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.cleanup")
    ).to_wire()
    assert result["status"] == "held"
    assert "private" not in str(result)
    assert registry._current is None
    assert set(sidebar._entries) == before


@pytest.mark.parametrize("change", ["compiler", "receipt_order", "plan", "mode"])
def test_changed_evidence_cannot_be_consumed(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    from dataclasses import replace

    from comfyui_h3_context.adapters import managed_mode_qualification
    from comfyui_h3_context.core.contracts import TaskMode
    from comfyui_h3_context.core.managed_sequence import ManagedSequenceError

    planning, registry, selection, _clock, _host, sidebar = _root_ready_environment(monkeypatch)
    before = set(sidebar._entries)
    ready = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.changed")
    ).to_wire()
    qualification = registry._current
    assert qualification is not None
    if change == "compiler":
        monkeypatch.setattr(
            managed_mode_qualification, "compiler_identity", lambda: "sha256:" + "0" * 64
        )
        assert registry.claim(ready["qualification_fingerprint"]) is None
        assert registry._current is None
    else:
        baseline = qualification.baseline
        if change == "receipt_order":
            changed = replace(
                qualification,
                baseline=replace(
                    baseline,
                    qualified_materialization_receipts=tuple(
                        reversed(baseline.qualified_materialization_receipts)
                    ),
                ),
            )
        elif change == "plan":
            changed = replace(qualification, production_plan_fingerprint="sha256:" + "0" * 64)
        else:
            changed = replace(
                qualification,
                baseline=replace(baseline, qualified_global_modes=(TaskMode.T2VA,)),
            )
        from comfyui_h3_context.adapters.comfyui_production_workspace import (
            ProductionWorkbenchError,
        )

        with pytest.raises((ProductionWorkbenchError, ManagedSequenceError)):
            registry._producer.observe(**selection, previous=changed)
    assert set(sidebar._entries) == before


def test_request_conflict_capacity_and_busy_do_not_materialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkbenchError

    planning, registry, selection, clock, _host, sidebar = _root_ready_environment(monkeypatch)
    request = action("prepare_managed_readiness", selection, "prepare.bounded")
    assert planning.dispatch(request).to_wire()["status"] == "ready"
    before = set(sidebar._entries)
    original = registry._current
    with pytest.raises(ProductionWorkbenchError, match="request_id_conflict"):
        planning.dispatch(
            action(
                "prepare_managed_readiness",
                {**selection, "expected_plan_fingerprint": "sha256:" + "0" * 64},
                "prepare.bounded",
            )
        )
    registry._readiness_lock.acquire()
    try:
        with pytest.raises(ProductionWorkbenchError, match="qualification_busy"):
            planning.dispatch(request)
    finally:
        registry._readiness_lock.release()
    registry._readiness_requests = {
        f"request.{index}": ("digest", None, clock[0] + 900) for index in range(256)
    }
    with pytest.raises(ProductionWorkbenchError, match="qualification_request_capacity"):
        planning.dispatch(request)
    assert registry._current is original
    assert set(sidebar._entries) == before


@pytest.mark.parametrize("missing", ["video_vae", "audio_vae"])
def test_missing_native_codec_names_do_not_hold_materialization(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    from test_generation_profile import observation

    from comfyui_h3_context.core.generation_profile import AssetSlot

    planning, registry, selection, _clock, host, sidebar = _root_ready_environment(monkeypatch)
    host[0] = observation(present={slot for slot in AssetSlot if slot.value != missing})
    before = set(sidebar._entries)
    result = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.missing")
    ).to_wire()
    assert result["status"] == "ready"
    assert registry._current is not None
    assert registry.claim(result["qualification_fingerprint"]) is not None
    assert set(sidebar._entries) == before


def test_root_cannot_consume_legacy_publication_as_product_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planning, registry, selection, _clock, _host, sidebar = _root_ready_environment(monkeypatch)
    before = set(sidebar._entries)
    ready = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.legacy")
    ).to_wire()
    current = registry._current
    assert current is not None
    registry.publish(current.baseline)
    assert registry.claim(current.baseline.fingerprint) is None
    assert registry.claim(ready["qualification_fingerprint"]) is None
    assert set(sidebar._entries) == before


def test_relocated_official_roles_are_observed_without_relabeling_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from comfyui_h3_context.adapters import comfyui_generation_profile
    from comfyui_h3_context.core.generation_profile import FamilyDisposition, SlotDisposition

    planning, registry, selection, _clock, host, sidebar = _root_ready_environment(monkeypatch)
    inventory = _install_loader_inventory(monkeypatch)
    for widgets in inventory.values():
        for values in widgets.values():
            values[:] = ["models/" + value for value in values]
    host[0] = replace(
        host[0],
        slots=tuple(replace(slot, disposition=SlotDisposition.RELOCATED) for slot in host[0].slots),
    )
    before = set(sidebar._entries)
    result = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.relocated")
    ).to_wire()
    assert result["status"] == "ready"
    assert all(
        family.disposition is FamilyDisposition.ASSET_RELOCATED
        for family in comfyui_generation_profile.build_generation_profile().families
    )
    assert "models/" not in str(result)
    assert registry.claim(result["qualification_fingerprint"]) is not None
    # A second valid inventory is still a different qualification subject.
    for widgets in inventory.values():
        for values in widgets.values():
            values[:] = [value.replace("models/", "relocated/") for value in values]
    assert registry.claim(result["qualification_fingerprint"]) is None
    assert set(sidebar._entries) == before


@pytest.mark.parametrize("change", ["capacity"])
def test_loader_inventory_refusal_does_not_publish(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    planning, registry, selection, _clock, _host, _sidebar = _root_ready_environment(monkeypatch)
    inventory = _install_loader_inventory(monkeypatch)
    values = next(iter(inventory["LoraLoaderModelOnly"].values()))
    values[:] = {
        "missing_lora": [],
        "unsafe": ["../private.safetensors"],
        "unofficial": ["unqualified.safetensors"],
        "capacity": ["entry.safetensors"] * 4097,
    }[change]
    result = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.inventory")
    ).to_wire()
    assert result["status"] == "held"
    assert result["reason"] == "qualification_assets_unresolved"
    assert registry._current is None


@pytest.mark.parametrize("values", [[], ["user-renamed.safetensors"], ["../unsafe.safetensors"]])
def test_unresolved_official_names_are_advisory_for_real_managed_readiness(
    monkeypatch: pytest.MonkeyPatch, values: list[str]
) -> None:
    from comfyui_h3_context.adapters.managed_asset_resolution import build_managed_asset_resolution

    planning, registry, selection, _clock, _host, sidebar = _root_ready_environment(monkeypatch)
    inventory = _install_loader_inventory(monkeypatch)
    for widgets in inventory.values():
        for entries in widgets.values():
            entries[:] = values
    before = set(sidebar._entries)
    resolution = build_managed_asset_resolution()
    assert resolution.resolved_roles == ()
    result = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.advisory.inventory")
    ).to_wire()
    assert result["status"] == "ready"
    assert registry.claim(result["qualification_fingerprint"]) is not None
    assert set(sidebar._entries) == before
    assert "user-renamed" not in str(result)


def test_missing_loras_report_only_verified_roles_without_holding_managed_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.managed_asset_resolution import build_managed_asset_resolution
    from comfyui_h3_context.core.native_asset_resolution import MATERIALIZATION_ASSET_ROLES

    planning, registry, selection, _clock, _host, _sidebar = _root_ready_environment(monkeypatch)
    inventory = _install_loader_inventory(monkeypatch)
    for entries in inventory["LoraLoaderModelOnly"].values():
        entries[:] = []
    resolution = build_managed_asset_resolution()
    assert resolution.resolved_roles == MATERIALIZATION_ASSET_ROLES[:-2]
    result = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.partial.inventory")
    ).to_wire()
    assert result["status"] == "ready"
    assert registry.claim(result["qualification_fingerprint"]) is not None


def test_supplied_host_sized_inventory_is_bounded_and_all_entries_invalidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planning, registry, selection, _clock, _host, sidebar = _root_ready_environment(monkeypatch)
    inventory = _install_loader_inventory(monkeypatch)
    values = next(iter(inventory["LoraLoaderModelOnly"].values()))
    values.extend(f"other_{index}.safetensors" for index in range(2910 - len(values)))
    before = set(sidebar._entries)
    request = action("prepare_managed_readiness", selection, "prepare.large.inventory")
    ready = planning.dispatch(request).to_wire()
    assert ready["status"] == "ready"
    assert registry._current is not None
    values[-1] = "changed_unselected.safetensors"
    assert planning.dispatch(request).to_wire()["status"] == "held"
    assert registry._current is None
    assert set(sidebar._entries) == before
    assert set(sidebar._entries) == before


@pytest.mark.parametrize("drift", ["host", "native_source"])
def test_drift_after_real_resource_reservation_compensates_before_parent_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drift: str
) -> None:
    from test_generation_profile import observation

    from comfyui_h3_context.adapters.comfyui_sequence_coordinator import SequenceCoordinatorRegistry
    from comfyui_h3_context.adapters.managed_sequence_service import (
        AuthorizeManagedSequenceRequestV1,
        ManagedSequenceServiceError,
        StartManagedSequenceRequestV1,
    )

    planning, registry, selection, clock, host, _sidebar = _root_ready_environment(monkeypatch)
    production = composition_root.get(composition_root.PRODUCTION_WORKSPACE)
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production,
        output_root_factory=lambda: tmp_path / "output",
        private_root_factory=lambda: tmp_path / "private",
        clock=lambda: clock[0],
    )
    composition_root.install(composition_root.SEQUENCE_COORDINATOR, coordinator)
    managed = composition_root.get(composition_root.MANAGED_SEQUENCE)
    monkeypatch.setattr(managed, "_clock", lambda: clock[0])
    ready = planning.dispatch(
        action("prepare_managed_readiness", selection, "prepare.reserve.drift")
    ).to_wire()
    qualification = registry.claim(ready["qualification_fingerprint"])
    assert qualification is not None
    authorized = managed.authorize(
        AuthorizeManagedSequenceRequestV1(
            request_id="authorize.reserve.drift",
            **selection,
            generation_plan_fingerprint="sha256:" + "0" * 64,
            compiler_fingerprint=qualification.baseline.compiler_fingerprint,
            host_capability_fingerprint=qualification.baseline.host_capability_fingerprint,
            explicit_intent="generate_approved_sequence",
        )
    )
    reserve = managed._reserve_start_resources

    def changed_after_reserve(request: Any) -> Any:
        claim = reserve(request)
        if drift == "host":
            host[0] = observation(anchors=frozenset())
        else:
            # A benign byte edit of the loaded source still invalidates before parent commit.
            host[1] = observed_source("2" * 40)
        return claim

    monkeypatch.setattr(managed, "_reserve_start_resources", changed_after_reserve)
    with pytest.raises(ManagedSequenceServiceError, match="qualification_claim_drift"):
        managed.start(
            StartManagedSequenceRequestV1(
                request_id="start.reserve.drift",
                parent_sequence_id=authorized.parent_sequence_id,
                expected_revision=authorized.revision,
                authorization_fingerprint=authorized.authorization_fingerprint,
            ),
            qualification=qualification,
        )
    assert managed._entry.sequence.state == authorized.state
    resource = coordinator._managed_start_resources[authorized.parent_sequence_id]
    assert (
        resource.ledger_released
        and resource.child_slot_released
        and resource.artifact_capacity_released
    )


def test_normal_root_exposes_explicit_readiness_without_a_test_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, sidebar, production, _source, prepare, _clock = environment()
    monkeypatch.setattr(
        composition_root,
        "_INSTANCES",
        {
            composition_root.SIDEBAR_WORKSPACE: sidebar,
            composition_root.PRODUCTION_WORKSPACE: production,
        },
    )
    service = composition_root.get(composition_root.PRODUCTION_PLANNING)
    monkeypatch.setattr(service, "_clock", lambda: _clock[0])
    _, proposed = prepared_proposal(service, prepare)
    imported: dict[str, Any] = service.dispatch(
        action(
            "import_plan",
            {**selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
            "import.for.readiness",
        )
    ).to_wire()

    # No host is installed in this test process. The real explicit product action must
    # explain that hold; an unknown action or a test-published positive is not readiness.
    result = service.dispatch(
        action(
            "prepare_managed_readiness",
            {
                "workspace_handle": prepare["workspace_handle"],
                "expected_workspace_revision": imported["workspace_revision"],
                "expected_workspace_fingerprint": imported["workspace_fingerprint"],
                "expected_plan_fingerprint": imported["plan_fingerprint"],
            },
            "prepare.readiness",
        )
    ).to_wire()
    assert result["schema"] == "h3.context.managed_readiness.v1"
    assert result["status"] == "held"
    assert result["qualification_fingerprint"] is None
    registry = composition_root.get(composition_root.MANAGED_MODE_QUALIFICATION)
    assert registry._current is None


@pytest.mark.parametrize(
    "extra", ["qualified_global_modes", "compiler_fingerprint", "ready", "media"]
)
def test_readiness_rejects_client_qualification_authority(extra: str) -> None:
    from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkbenchError
    from comfyui_h3_context.adapters.production_planning_service import (
        decode_production_planning_action,
    )

    _service, _sidebar, _production, _source, prepare, _clock = environment()
    payload = {
        "workspace_handle": prepare["workspace_handle"],
        "expected_workspace_revision": prepare["expected_workspace_revision"],
        "expected_workspace_fingerprint": prepare["expected_workspace_fingerprint"],
        "expected_plan_fingerprint": "sha256:" + "a" * 64,
        extra: True,
    }
    with pytest.raises(ProductionWorkbenchError, match="invalid_planning_action"):
        decode_production_planning_action(action("prepare_managed_readiness", payload, "refuse"))


def test_observed_host_reaches_real_canonical_materializer_without_optimistic_guide_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_generation_profile import observation

    from comfyui_h3_context.adapters import comfyui_generation_profile
    from comfyui_h3_context.adapters.managed_mode_qualification import ManagedQualificationProducer
    from comfyui_h3_context.core.guide_conformance import GuideReadiness

    service, sidebar, production, _source, prepare, clock = environment()
    _, proposed = prepared_proposal(service, prepare)
    imported = service.dispatch(
        action(
            "import_plan",
            {**selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
            "import.producer",
        )
    ).to_wire()
    host = observation()
    _install_loader_inventory(monkeypatch)
    monkeypatch.setattr(comfyui_generation_profile, "observe_host", lambda *_args: host)
    monkeypatch.setattr(
        comfyui_generation_profile,
        "_observe_native_composition",
        lambda: composition_observation(host, observed_source()),
    )
    producer = ManagedQualificationProducer(production, clock=lambda: clock[0])
    before = set(sidebar._entries)
    selection = {
        "workspace_handle": prepare["workspace_handle"],
        "expected_workspace_revision": imported["workspace_revision"],
        "expected_workspace_fingerprint": imported["workspace_fingerprint"],
        "expected_plan_fingerprint": imported["plan_fingerprint"],
    }
    qualification = producer.observe(**selection)
    assert len(qualification.composition_fingerprints) == 2
    assert qualification.guide_readiness == (GuideReadiness.INCOMPLETE,) * 2
    assert set(sidebar._entries) == before
    clock[0] += 1
    assert producer.observe(**selection, previous=qualification) == qualification
    assert set(sidebar._entries) == before
