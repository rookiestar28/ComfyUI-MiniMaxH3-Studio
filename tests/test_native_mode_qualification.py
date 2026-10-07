"""M15-05 current-report, graph-edge, and product-surface qualification tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.core import (
    AssetRole,
    ContextReport,
    ExecutionCorrelation,
    MediaKind,
    MediaMetadata,
    NativeH3AdapterError,
    NativeH3Wiring,
    ProductShellProjection,
    RawContextRequest,
    ReferenceAsset,
    ReferenceRegistry,
    TaskMode,
    build_native_h3_wiring,
    build_product_shell_projection,
    build_reference_registry,
    canonical_fingerprint,
    fingerprint_context_report,
)
from comfyui_h3_context.core import native_mode_matrix as api
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextValidatorNode,
)
from comfyui_h3_context.product_shell_node import H3ContextProductShellNode

SURFACES = (
    "direct_node",
    "fixed_app_mode",
    "fixed_subgraph",
    "sidebar_normal_queue",
    "api_headless",
)
ROOT = Path(__file__).resolve().parents[1]


def _registry(mode: TaskMode) -> ReferenceRegistry:
    assets: tuple[ReferenceAsset, ...]
    if mode is TaskMode.T2VA:
        assets = ()
    elif mode is TaskMode.I2VA:
        assets = (ReferenceAsset("first_1", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),)
    elif mode is TaskMode.L2VA:
        assets = (ReferenceAsset("last_1", MediaKind.IMAGE, AssetRole.LAST_FRAME, 1),)
    elif mode is TaskMode.FL2VA:
        assets = (
            ReferenceAsset("first_1", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),
            ReferenceAsset("last_1", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2),
        )
    else:
        assets = (
            ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
            ReferenceAsset("image_2", MediaKind.IMAGE, AssetRole.REFERENCE, 2),
        )
    return build_reference_registry(assets) if assets else ReferenceRegistry.empty()


def _report(
    mode: TaskMode, *, user_intent: str = "Preserve the declared intent exactly."
) -> ContextReport:
    registry = _registry(mode)
    raw = RawContextRequest(
        mode=mode,
        user_intent=user_intent,
        duration_seconds=5.0,
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


def _edges(wiring: NativeH3Wiring) -> tuple[api.NativeGraphEdge, ...]:
    source_nodes = (
        tuple(str(index + 2) for index in range(len(wiring.bindings)))
        if wiring.task_mode is TaskMode.REF2VA
        else tuple(f"source_{index}" for index in range(len(wiring.bindings)))
    )
    return tuple(
        api.NativeGraphEdge(
            asset_id=binding.asset_id,
            kind=binding.kind.value,
            presentation_label=binding.label,
            presentation_ordinal=binding.ordinal,
            source_node=source_nodes[index],
            source_output=0,
            native_node=wiring.native_node_id,
            native_child_path=f"{wiring.native_node_id}.{binding.native_path}",
            connection_order=index,
            ownership="direct_visible_host_edge",
        )
        for index, binding in enumerate(wiring.bindings)
    )


def _source_authority(
    edges: tuple[api.NativeGraphEdge, ...],
) -> tuple[api.NativeGraphSourceAuthority, ...]:
    return tuple(
        api.NativeGraphSourceAuthority(
            asset_id=edge.asset_id,
            source_node=edge.source_node,
            source_output=edge.source_output,
            connection_order=edge.connection_order,
        )
        for edge in edges
    )


def _surface_artifact(
    report: ContextReport,
    wiring: NativeH3Wiring,
    edges: tuple[api.NativeGraphEdge, ...],
    *,
    surface: str = "direct_node",
    artifact_id: str | None = None,
    artifact_payload: object | None = None,
    source_authority: tuple[api.NativeGraphSourceAuthority, ...] | None = None,
) -> api.NativeSurfaceArtifact:
    artifact_kinds = {
        "direct_node": "product_shell_projection",
        "fixed_app_mode": "comfyui_api_prompt",
        "fixed_subgraph": "comfyui_subgraph_definition",
        "sidebar_normal_queue": "sidebar_workspace_projection",
        "api_headless": "product_shell_node_result",
    }
    return api.NativeSurfaceArtifact(
        surface=surface,
        artifact_kind=artifact_kinds[surface],
        artifact_id=artifact_id or f"test:{surface}:{report.report_id}",
        artifact_fingerprint=canonical_fingerprint(
            artifact_payload
            if artifact_payload is not None
            else {"surface": surface, "report_id": report.report_id}
        ),
        report_id=report.report_id,
        report_revision=report.revision,
        report_fingerprint=fingerprint_context_report(report),
        prompt_fingerprint=wiring.prompt_fingerprint,
        wiring_fingerprint=canonical_fingerprint(wiring.to_wire()),
        graph_fingerprint=canonical_fingerprint([edge.to_wire() for edge in edges]),
        source_authority=source_authority or _source_authority(edges),
        dynamic_reference_parent_visible=report.request.task_mode is TaskMode.REF2VA,
    )


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert type(value) is dict
    return value


def _file_artifact_identity(path: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
    }


def _fixed_app_mode_path(mode: TaskMode) -> Path | None:
    names = {
        TaskMode.T2VA: "m15_03_product_shell_base.json",
        TaskMode.REF2VA: "m15_03_product_shell_reference.json",
    }
    name = names.get(mode)
    return ROOT / "workflows" / name if name is not None else None


def _fixed_app_mode_document(mode: TaskMode) -> dict[str, object] | None:
    path = _fixed_app_mode_path(mode)
    return _load_json(path) if path is not None else None


def _artifact_intent(mode: TaskMode) -> str:
    document = _fixed_app_mode_document(mode)
    if document is None:
        return "Preserve the declared intent exactly."
    prompt = document["prompt"]
    assert type(prompt) is dict
    request = next(
        value
        for value in prompt.values()
        if type(value) is dict and value.get("class_type") == "comfyui_h3_context.H3Context.Request"
    )
    inputs = request["inputs"]
    assert type(inputs) is dict and inputs["task_mode"] == mode.value
    intent = inputs["user_intent"]
    assert type(intent) is str
    return intent


def _real_surface_artifacts(
    report: ContextReport,
    wiring: NativeH3Wiring,
    edges: tuple[api.NativeGraphEdge, ...],
) -> tuple[api.NativeSurfaceArtifact, ...]:
    mode = report.request.task_mode
    direct_result = H3ContextProductShellNode().emit(
        report,
        wiring,
        prompt_id=f"direct-{mode.value}",
        execution_node_id="direct-product-shell",
    )["result"]
    assert type(direct_result) is tuple and len(direct_result) == 2
    direct_projection = direct_result[1]
    assert type(direct_projection) is ProductShellProjection
    direct = _surface_artifact(
        report,
        wiring,
        edges,
        surface="direct_node",
        artifact_id=f"node:{H3ContextProductShellNode.NODE_ID}:{mode.value}",
        artifact_payload={
            "prompt": direct_result[0],
            "projection": direct_projection.to_wire(),
        },
    )

    registry = SidebarWorkspaceRegistry()
    workspace = registry.publish(
        report,
        wiring,
        ExecutionCorrelation(f"sidebar-{mode.value}", "sidebar-product-shell"),
    )
    assert registry.get(workspace.workspace_id) == workspace
    sidebar = _surface_artifact(
        report,
        wiring,
        edges,
        surface="sidebar_normal_queue",
        artifact_id=workspace.workspace_id,
        artifact_payload={
            "schema": workspace.schema,
            "workspace_id": workspace.workspace_id,
            "report_id": workspace.report_id,
            "report_revision": workspace.report_revision,
            "report_fingerprint": workspace.report_fingerprint,
            "prompt_fingerprint": workspace.prompt_fingerprint,
            "correlation": workspace.correlation.to_wire(),
            "task_mode": workspace.task_mode,
            "profile": workspace.profile,
            "bindings": [value.to_wire() for value in workspace.bindings],
        },
    )

    api_projection = build_product_shell_projection(
        report,
        wiring,
        ExecutionCorrelation(f"api-{mode.value}", "api-product-shell"),
    )
    api_artifact = _surface_artifact(
        report,
        wiring,
        edges,
        surface="api_headless",
        artifact_id=f"api-prompt:{mode.value}:{report.report_id}",
        artifact_payload=api_projection.to_wire(),
    )

    by_surface = {
        "direct_node": direct,
        "sidebar_normal_queue": sidebar,
        "api_headless": api_artifact,
    }
    app_document = _fixed_app_mode_document(mode)
    if app_document is not None:
        app_path = _fixed_app_mode_path(mode)
        assert app_path is not None
        prompt = app_document["prompt"]
        assert type(prompt) is dict
        request = next(
            value
            for value in prompt.values()
            if type(value) is dict
            and value.get("class_type") == "comfyui_h3_context.H3Context.Request"
        )
        inputs = request["inputs"]
        assert type(inputs) is dict
        assert inputs["task_mode"] == mode.value
        assert inputs["user_intent"] == report.request.user_intent
        if mode is TaskMode.REF2VA:
            reference = next(
                value
                for value in prompt.values()
                if type(value) is dict
                and value.get("class_type") == "comfyui_h3_context.H3Context.ReferenceRegistry"
            )
            reference_inputs = reference["inputs"]
            assert type(reference_inputs) is dict
            observed_sources = tuple(tuple(value) for value in reference_inputs["images"])
            expected_sources = tuple(
                (value.source_node, value.source_output) for value in _source_authority(edges)
            )
            assert observed_sources == expected_sources
        by_surface["fixed_app_mode"] = _surface_artifact(
            report,
            wiring,
            edges,
            surface="fixed_app_mode",
            artifact_id=f"workflow:{app_document['fixture_id']}",
            artifact_payload=_file_artifact_identity(app_path),
        )

    if mode is TaskMode.T2VA:
        subgraph_path = ROOT / "subgraphs" / "H3 Context Assistant - Base.json"
        subgraph = _load_json(subgraph_path)
        fixture = subgraph["h3_context_fixture"]
        assert type(fixture) is dict
        assert fixture["expanded_fixture"] == "workflows/m15_09_assistant_base.json"
        outer_nodes = subgraph["nodes"]
        assert type(outer_nodes) is list and len(outer_nodes) == 1
        outer = outer_nodes[0]
        assert type(outer) is dict
        widgets = outer["widgets_values"]
        assert type(widgets) is list
        assert widgets[0] == mode.value and widgets[1] == report.request.user_intent
        by_surface["fixed_subgraph"] = _surface_artifact(
            report,
            wiring,
            edges,
            surface="fixed_subgraph",
            artifact_id="subgraph:H3 Context Assistant - Base",
            artifact_payload=_file_artifact_identity(subgraph_path),
        )

    expected = next(
        value for value in api.build_default_native_mode_matrix().modes if value.task_mode is mode
    ).qualified_surfaces
    assert tuple(surface for surface in expected if surface in by_surface) == expected
    return tuple(by_surface[surface] for surface in expected)


def test_graph_edge_and_surface_results_are_immutable_contract_values() -> None:
    assert hasattr(api.NativeGraphEdge, "__dataclass_fields__")
    assert hasattr(api.NativeGraphSourceAuthority, "__dataclass_fields__")
    assert hasattr(api.NativeSurfaceArtifact, "__dataclass_fields__")
    assert hasattr(api.NativeSurfaceQualification, "__dataclass_fields__")


def test_source_endpoint_authority_rejects_source_only_swap() -> None:
    report = _report(TaskMode.FL2VA)
    wiring = build_native_h3_wiring(report)
    edges = _edges(wiring)
    artifact = _surface_artifact(report, wiring, edges)
    swapped = (
        replace(edges[0], source_node=edges[1].source_node),
        replace(edges[1], source_node=edges[0].source_node),
    )
    with pytest.raises(api.NativeModeMatrixError, match="source authority"):
        api.qualify_native_mode_surface(
            report,
            wiring,
            swapped,
            artifact=artifact,
        )


def test_mixed_reference_autogrow_source_authority_rejects_endpoint_swap() -> None:
    registry = build_reference_registry(
        (
            ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
            ReferenceAsset(
                "video_1",
                MediaKind.VIDEO,
                AssetRole.REFERENCE,
                2,
                metadata=MediaMetadata(duration_seconds=Decimal("5")),
            ),
            ReferenceAsset(
                "audio_1",
                MediaKind.AUDIO,
                AssetRole.AUDIO_SOURCE,
                3,
                metadata=MediaMetadata(duration_seconds=Decimal("5")),
            ),
        )
    )
    raw = RawContextRequest(
        mode=TaskMode.REF2VA,
        user_intent="Preserve mixed native references exactly.",
        duration_seconds=5.0,
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    wiring = build_native_h3_wiring(report)
    edges = _edges(wiring)
    assert tuple(value.kind for value in edges) == ("image", "video", "audio")
    artifact = _surface_artifact(report, wiring, edges)
    swapped = (
        replace(edges[0], source_node=edges[1].source_node),
        replace(edges[1], source_node=edges[0].source_node),
        edges[2],
    )
    with pytest.raises(api.NativeModeMatrixError, match="source authority"):
        api.qualify_native_mode_surface(report, wiring, swapped, artifact=artifact)


def test_mode_surface_inventory_does_not_claim_absent_fixed_artifacts() -> None:
    modes = {value.task_mode: value for value in api.build_default_native_mode_matrix().modes}
    runtime = ("direct_node", "sidebar_normal_queue", "api_headless")
    assert modes[TaskMode.T2VA].qualified_surfaces == SURFACES
    assert modes[TaskMode.I2VA].qualified_surfaces == runtime
    assert modes[TaskMode.FL2VA].qualified_surfaces == runtime
    assert modes[TaskMode.L2VA].qualified_surfaces == runtime
    assert modes[TaskMode.REF2VA].qualified_surfaces == (
        "direct_node",
        "fixed_app_mode",
        "sidebar_normal_queue",
        "api_headless",
    )
    for value in modes.values():
        assert set(value.qualified_surfaces).isdisjoint(value.unsupported_surfaces)
        assert set(value.qualified_surfaces) | set(value.unsupported_surfaces) == set(SURFACES)


@pytest.mark.parametrize("mode", tuple(TaskMode))
def test_all_five_modes_join_the_same_identity_across_product_surfaces(mode: TaskMode) -> None:
    report = _report(mode, user_intent=_artifact_intent(mode))
    wiring = build_native_h3_wiring(report)
    edges = _edges(wiring)
    artifacts = _real_surface_artifacts(report, wiring, edges)
    expected_surfaces = next(
        value for value in api.build_default_native_mode_matrix().modes if value.task_mode is mode
    ).qualified_surfaces
    assert tuple(value.surface for value in artifacts) == expected_surfaces
    assert len({value.artifact_id for value in artifacts}) == len(artifacts)
    assert len({value.artifact_fingerprint for value in artifacts}) == len(artifacts)
    qualifications = tuple(
        api.qualify_native_mode_surface(
            report,
            wiring,
            edges,
            artifact=artifact,
            expected_revision=report.revision,
            expected_report_fingerprint=fingerprint_context_report(report),
        )
        for artifact in artifacts
    )
    assert (
        api.assert_native_surface_equivalence(qualifications)
        == qualifications[0].identity_fingerprint
    )
    assert tuple(value.surface for value in qualifications) == expected_surfaces
    assert all(value.queue_ready and value.graph_manifest_verified for value in qualifications)
    assert all(value.task_mode is mode for value in qualifications)


def test_surface_equivalence_rejects_relabelled_shared_artifact_provenance() -> None:
    report = _report(TaskMode.T2VA, user_intent=_artifact_intent(TaskMode.T2VA))
    wiring = build_native_h3_wiring(report)
    artifacts = _real_surface_artifacts(report, wiring, ())
    qualifications = tuple(
        api.qualify_native_mode_surface(report, wiring, (), artifact=artifact)
        for artifact in artifacts
    )
    reused = (
        qualifications[0],
        replace(
            qualifications[1],
            artifact_id=qualifications[0].artifact_id,
            artifact_fingerprint=qualifications[0].artifact_fingerprint,
        ),
        *qualifications[2:],
    )
    with pytest.raises(api.NativeModeMatrixError, match="reuses one artifact provenance"):
        api.assert_native_surface_equivalence(reused)


def test_graph_manifest_omission_reorder_swap_duplicate_and_ownership_fail_closed() -> None:
    report = _report(TaskMode.FL2VA)
    wiring = build_native_h3_wiring(report)
    edges = _edges(wiring)
    hostile = (
        edges[:-1],
        tuple(reversed(edges)),
        (replace(edges[0], native_child_path=edges[1].native_child_path), edges[1]),
        (edges[0], replace(edges[1], asset_id=edges[0].asset_id)),
    )
    for candidate in hostile:
        with pytest.raises(api.NativeModeMatrixError):
            api.qualify_native_mode_surface(
                report,
                wiring,
                candidate,
                artifact=_surface_artifact(report, wiring, edges),
            )
    with pytest.raises(api.NativeModeMatrixError, match="ownership"):
        replace(edges[0], ownership="hidden_projection")


def test_stale_report_untrusted_wiring_and_unknown_surface_fail_closed() -> None:
    report = _report(TaskMode.I2VA)
    wiring = build_native_h3_wiring(report)
    edges = _edges(wiring)
    with pytest.raises(api.NativeModeMatrixError, match="current passed report"):
        api.qualify_native_mode_surface(
            report,
            wiring,
            edges,
            artifact=_surface_artifact(report, wiring, edges),
            expected_revision=report.revision + 1,
        )
    with pytest.raises(api.NativeModeMatrixError, match="runtime-issued wiring"):
        api.qualify_native_mode_surface(
            report,
            replace(wiring),
            edges,
            artifact=_surface_artifact(report, wiring, edges),
        )
    with pytest.raises(api.NativeModeMatrixError, match="public path"):
        replace(_surface_artifact(report, wiring, edges), surface="private_browser_state")


def test_ref2va_dynamic_reference_requires_parent_visible_boundary() -> None:
    report = _report(TaskMode.REF2VA)
    wiring = build_native_h3_wiring(report)
    edges = _edges(wiring)
    with pytest.raises(api.NativeModeMatrixError, match="parent-visible"):
        api.qualify_native_mode_surface(
            report,
            wiring,
            edges,
            artifact=replace(
                _surface_artifact(report, wiring, edges),
                dynamic_reference_parent_visible=False,
            ),
        )
    with pytest.raises(api.NativeModeMatrixError, match="unsupported"):
        api.qualify_native_mode_surface(
            report,
            wiring,
            edges,
            artifact=_surface_artifact(report, wiring, edges, surface="fixed_subgraph"),
        )


def test_structurally_equal_cross_report_wiring_cannot_be_reused() -> None:
    first = _report(TaskMode.T2VA)
    second = _report(TaskMode.T2VA)
    first_wiring = build_native_h3_wiring(first)
    with pytest.raises((api.NativeModeMatrixError, NativeH3AdapterError)):
        api.qualify_native_mode_surface(
            second,
            first_wiring,
            (),
            artifact=_surface_artifact(first, first_wiring, (), surface="api_headless"),
        )
