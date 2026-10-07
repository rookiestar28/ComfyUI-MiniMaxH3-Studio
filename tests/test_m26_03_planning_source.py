from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.production_planning_source import (
    ProductionPlanningSourceSnapshot,
    build_production_planning_context,
    fingerprint_production_source_request,
)
from comfyui_h3_context.core import (
    ContextReport,
    ExecutionCorrelation,
    NativeH3Wiring,
    SidebarWorkspaceError,
    TaskMode,
    build_native_h3_wiring,
    canonical_fingerprint,
)
from comfyui_h3_context.core.constraints import (
    ContentScope,
    DirectiveTarget,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    KeepChangeAction,
    KeepChangeDirective,
    RequiredContent,
    TimePoint,
    TimingConstraint,
)
from comfyui_h3_context.core.production_duration import ProductionDurationIntentV1
from comfyui_h3_context.core.production_semantics import derive_production_semantic_authority
from comfyui_h3_context.core.production_storyboard import fingerprint_prompt_text
from comfyui_h3_context.core.sidebar_workspace import SidebarWorkspaceProjection
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3ReferenceRegistryNode,
)


def _authority(
    *,
    with_references: bool = False,
    hard_constraints: HardConstraintSet | None = None,
) -> tuple[ContextReport, NativeH3Wiring]:
    request = H3ContextRequestNode().build_request(
        TaskMode.REF2VA if with_references else TaskMode.T2VA,
        "An operator-owned subject crosses the room while the camera tracks steadily.",
        duration_seconds=5.0,
        hard_constraints=hard_constraints,
    )[0]
    if with_references:
        registry = H3ReferenceRegistryNode().build_registry(
            images=[object(), object()],
            videos=[object()],
            audios=[object()],
        )[0]
        plan = H3ContextPlanNode().build_plan(request, registry)[0]
    else:
        plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    return report, build_native_h3_wiring(report)


def _registry(
    *,
    clock: Callable[[], float] = lambda: 1,
    max_entries: int = 4,
) -> SidebarWorkspaceRegistry:
    return SidebarWorkspaceRegistry(
        max_entries=max_entries,
        ttl_seconds=5,
        clock=clock,
        source_binding_claim=lambda _registry: None,
    )


def _publish(
    registry: SidebarWorkspaceRegistry,
    *,
    with_references: bool = False,
) -> tuple[ContextReport, NativeH3Wiring, SidebarWorkspaceProjection]:
    report, wiring = _authority(with_references=with_references)
    projection = registry.publish(report, wiring, ExecutionCorrelation("prompt-1", "17"))
    return report, wiring, projection


def _snapshot(
    registry: SidebarWorkspaceRegistry,
    projection: SidebarWorkspaceProjection,
) -> ProductionPlanningSourceSnapshot:
    return registry.snapshot_production_planning_source(
        projection.workspace_id,
        expected_report_revision=projection.report_revision,
        expected_report_fingerprint=projection.report_fingerprint,
    )


def _stage_action(projection: SidebarWorkspaceProjection) -> dict[str, object]:
    return {
        "schema": "h3.context.sidebar.action.v2",
        "workspace_id": projection.workspace_id,
        "expected_revision": projection.report_revision,
        "expected_report_fingerprint": projection.report_fingerprint,
        "action": "stage_prompt",
        "payload": {
            "reason": "Clarify movement",
            "prompt_text": projection.prompt_text.replace(
                "crosses the room",
                "crosses the quiet room",
            ),
        },
    }


def test_builder_derives_v2_context_from_exact_retained_report() -> None:
    registry = _registry()
    report, _wiring, projection = _publish(registry, with_references=True)
    snapshot = _snapshot(registry, projection)

    context = build_production_planning_context(
        snapshot,
        planning_context_id="planning_context_1",
        revision=1,
        duration_intent=ProductionDurationIntentV1(target_seconds=20),
    )

    assert context.optimized_candidate_id == report.report_id
    assert context.optimized_candidate_text_fingerprint == fingerprint_prompt_text(
        report.prompt_document.text
    )
    assert context.source_request_fingerprint == fingerprint_production_source_request(
        report.request
    )
    assert context.source_intent_graph_fingerprint == canonical_fingerprint(
        report.plan.intent_graph.to_wire()
    )
    assert context.source_profile_fingerprint == canonical_fingerprint(
        report.request.profile.to_wire()
    )
    assert context.reference_registry_fingerprint == canonical_fingerprint(
        report.request.reference_registry.to_wire()
    )
    assert context.source_context_duration_seconds == 5
    assert context.production_target_duration_seconds == 20
    assert tuple(asset.asset_id for asset in context.assets) == tuple(
        asset.asset_id for asset in report.request.reference_registry.assets
    )
    assert tuple(asset.role for asset in context.assets) == tuple(
        asset.role for asset in report.request.reference_registry.assets
    )
    assert context.reference_ids == tuple(asset.asset_id for asset in context.assets)
    assert context.subject_ids == tuple(
        subject.subject_id for subject in report.plan.intent_graph.subjects
    )
    assert context.semantic_authority == derive_production_semantic_authority(
        report.plan,
        report.prompt_document,
    )
    assert context.hard_constraints == ()


def test_snapshot_is_fixed_expiry_and_never_slides_on_source_reads() -> None:
    now = [1.0]
    registry = _registry(clock=lambda: now[0])
    _report, _wiring, projection = _publish(registry)
    snapshot = _snapshot(registry, projection)
    assert snapshot.expires_at_monotonic == 6.0

    now[0] = 3.0
    repeated = _snapshot(registry, projection)
    assert repeated.expires_at_monotonic == snapshot.expires_at_monotonic
    assert repeated.source_fingerprint == snapshot.source_fingerprint
    registry.get(projection.workspace_id)  # The user workspace itself now remains live until t=8.

    now[0] = 5.0
    with registry.guard_production_planning_source(snapshot):
        pass
    now[0] = 6.0
    with pytest.raises(SidebarWorkspaceError, match="planning_source_stale"):
        with registry.guard_production_planning_source(snapshot):
            pass
    assert registry.get(projection.workspace_id).workspace_id == projection.workspace_id


def test_builder_preserves_supported_constraints_and_retains_forbidden_source_fact() -> None:
    constraints = HardConstraintSet(
        constraints=(
            ExactTextConstraint(
                "dialogue_1",
                ExactTextKind.DIALOGUE,
                "Hold position.",
                language="en",
            ),
            RequiredContent("required_1", ContentScope.SCENE, "A brass doorway"),
            ForbiddenContent("forbidden_1", ContentScope.VISUAL, "private emblem"),
            KeepChangeDirective(
                "keep_1",
                KeepChangeAction.KEEP,
                DirectiveTarget.CAMERA,
                "steady tracking",
            ),
            TimingConstraint(
                "timing_1",
                TimePoint.from_text("1"),
                TimePoint.from_text("2"),
                "doorway crossing",
            ),
        )
    )
    registry = _registry()
    report, wiring = _authority(hard_constraints=constraints)
    projection = registry.publish(report, wiring, ExecutionCorrelation("prompt-3", "21"))
    snapshot = _snapshot(registry, projection)
    context = build_production_planning_context(
        snapshot,
        planning_context_id="planning_context_constraints",
        revision=1,
        duration_intent=ProductionDurationIntentV1(target_seconds=20),
    )

    retained_text = "\n".join(
        (
            *(field.text for field in context.semantic_authority.global_fields),
            *(span.text for span in context.semantic_authority.timed_spans),
        )
    )
    assert "Hold position." in retained_text
    assert "A brass doorway" in retained_text
    assert "steady tracking" in retained_text
    assert context.semantic_authority.forbidden_cut_intervals_milliseconds == ((1_000, 2_000),)
    assert "private emblem" not in report.prompt_document.text
    assert snapshot.report.request.hard_constraints is constraints
    assert context.hard_constraints == ()


def test_guard_holds_source_lock_through_publication_window() -> None:
    registry = _registry()
    _report, _wiring, projection = _publish(registry)
    snapshot = _snapshot(registry, projection)
    attempted = threading.Event()
    completed = threading.Event()

    def mutate_source() -> None:
        attempted.set()
        registry.dispatch(_stage_action(projection))
        completed.set()

    worker = threading.Thread(target=mutate_source)
    with registry.guard_production_planning_source(snapshot):
        worker.start()
        assert attempted.wait(timeout=1)
        assert not completed.wait(timeout=0.05)
    assert completed.wait(timeout=1)
    worker.join(timeout=1)
    with pytest.raises(SidebarWorkspaceError, match="planning_source_stale"):
        with registry.guard_production_planning_source(snapshot):
            pass


def test_unvalidated_and_stale_sources_fail_without_snapshot_fallback() -> None:
    registry = _registry()
    _report, _wiring, projection = _publish(registry)
    with pytest.raises(SidebarWorkspaceError, match="planning_source_stale"):
        registry.snapshot_production_planning_source(
            projection.workspace_id,
            expected_report_revision=projection.report_revision,
            expected_report_fingerprint="sha256:" + "0" * 64,
        )

    staged = registry.dispatch(_stage_action(projection))
    assert isinstance(staged, SidebarWorkspaceProjection)
    with pytest.raises(KeyError, match="workspace is unavailable"):
        registry.snapshot_production_planning_source(
            projection.workspace_id,
            expected_report_revision=staged.report_revision,
            expected_report_fingerprint=staged.report_fingerprint,
        )


def test_ephemeral_materialization_is_exact_bounded_and_not_a_planning_source() -> None:
    report, wiring = _authority()
    full = _registry(max_entries=1)
    retained = full.publish(report, wiring, ExecutionCorrelation("prompt-1", "17"))
    with pytest.raises(SidebarWorkspaceError, match="production_materialization_capacity"):
        full.publish_production_materialization(
            report,
            wiring,
            ExecutionCorrelation("scratch-1", "18"),
        )
    assert full.get(retained.workspace_id).workspace_id == retained.workspace_id

    registry = _registry(max_entries=2)
    retained = registry.publish(report, wiring, ExecutionCorrelation("prompt-2", "19"))
    scratch = registry.publish_production_materialization(
        report,
        wiring,
        ExecutionCorrelation("scratch-2", "20"),
    )
    seed = registry.claim_production_seed(scratch.workspace_id)
    assert seed.source_id == report.report_id
    assert (
        registry.claim_production_materialization_seed(
            scratch.workspace_id,
            expected_report_revision=scratch.report_revision,
            expected_report_fingerprint=scratch.report_fingerprint,
        )
        == seed
    )
    with pytest.raises(KeyError, match="workspace is unavailable"):
        _snapshot(registry, scratch)
    with pytest.raises(KeyError, match="workspace is unavailable"):
        registry.get(scratch.workspace_id)
    with pytest.raises(KeyError, match="workspace is unavailable"):
        registry.dispatch(_stage_action(scratch))
    with pytest.raises(SidebarWorkspaceError, match="production_materialization_stale"):
        registry.release_production_materialization(
            scratch.workspace_id,
            expected_report_revision=scratch.report_revision,
            expected_report_fingerprint="sha256:" + "0" * 64,
        )
    assert (
        registry.claim_production_materialization_seed(
            scratch.workspace_id,
            expected_report_revision=scratch.report_revision,
            expected_report_fingerprint=scratch.report_fingerprint,
        )
        == seed
    )
    registry.release_production_materialization(
        scratch.workspace_id,
        expected_report_revision=scratch.report_revision,
        expected_report_fingerprint=scratch.report_fingerprint,
    )
    with pytest.raises(KeyError, match="workspace is unavailable"):
        registry.get(scratch.workspace_id)
    with pytest.raises(KeyError, match="workspace is unavailable"):
        registry.release_production_materialization(
            retained.workspace_id,
            expected_report_revision=retained.report_revision,
            expected_report_fingerprint=retained.report_fingerprint,
        )
    assert registry.get(retained.workspace_id).workspace_id == retained.workspace_id
