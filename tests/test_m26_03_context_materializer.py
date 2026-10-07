"""M26-03 real canonical Context materialization and cleanup coverage."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarProductionSeed,
    SidebarWorkspaceRegistry,
)
from comfyui_h3_context.adapters.managed_sequence_service import (
    ManagedSegmentMaterializationClaimV1,
    _HeldMaterialization,
)
from comfyui_h3_context.adapters.production_context_materializer import (
    PRODUCTION_CANONICAL_LOWERING_REASON,
    CanonicalProductionMaterializer,
    _assert_source_join,
    _derive_canonical_lowering,
    _segment_constraints,
    claim_canonical_production_materialization,
)
from comfyui_h3_context.adapters.production_planning_source import (
    ProductionPlanningSourceSnapshot,
    build_production_planning_context,
)
from comfyui_h3_context.context_pipeline_nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextValidatorNode,
)
from comfyui_h3_context.context_request_nodes import H3ContextRequestNode
from comfyui_h3_context.core.canonical import fingerprint_context_report
from comfyui_h3_context.core.constraints import (
    ConstraintTransformation,
    ContentScope,
    DialogueSpeaker,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    RequiredContent,
    TimePoint,
    TimingConstraint,
    TransformationField,
)
from comfyui_h3_context.core.context_reporting import ContextReport
from comfyui_h3_context.core.contracts import (
    AssetRole,
    EvidenceLevel,
    MediaKind,
    ProviderIdentity,
    TaskMode,
)
from comfyui_h3_context.core.errors import SidebarWorkspaceError
from comfyui_h3_context.core.evidence import (
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSet,
    EvidenceSource,
    EvidenceSourceKind,
    Provenance,
    SupportStatus,
)
from comfyui_h3_context.core.intent_graph import IntentGraph, IntentSubject, TimelineSegment
from comfyui_h3_context.core.native_h3 import NativeH3Wiring, build_native_h3_wiring
from comfyui_h3_context.core.normalization import RawContextRequest
from comfyui_h3_context.core.production_duration import (
    ProductionDurationIntentV1,
    SegmentationPolicyV1,
)
from comfyui_h3_context.core.production_import import (
    ProductionImportError,
    derive_segment_context_materialization_receipt,
)
from comfyui_h3_context.core.production_storyboard import (
    ProductionPlanningContextV2,
    ProductionStoryboardAdmissionRequestV1,
    ProductionStoryboardAdmissionService,
    ProductionStoryboardAdmissionV2,
    ProposalSegmentV1,
    SegmentationProposalV2,
    StoryboardShotV1,
    StoryboardSourceKindV1,
    build_segmentation_proposal,
)
from comfyui_h3_context.core.registry import ReferenceAsset, build_reference_registry
from comfyui_h3_context.core.sidebar_workspace import SidebarWorkspaceProjection
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation
from comfyui_h3_context.review_nodes import H3ContextAuditOverrideNode


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _source(
    sidebar: SidebarWorkspaceRegistry,
    *,
    mode: TaskMode = TaskMode.T2VA,
    constraints: HardConstraintSet | None = None,
    evidence: EvidenceSet | None = None,
    user_intent: str = "A subject crosses the scene.",
    graph: IntentGraph | None = None,
) -> ProductionPlanningSourceSnapshot:
    constraints = HardConstraintSet() if constraints is None else constraints
    evidence = EvidenceSet.empty() if evidence is None else evidence
    registry = (
        build_reference_registry(
            (
                ReferenceAsset("asset_first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),
                ReferenceAsset("asset_last", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2),
            )
        )
        if mode is TaskMode.FL2VA
        else build_reference_registry(
            (ReferenceAsset("asset_ref", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
        )
        if mode is TaskMode.REF2VA
        else build_reference_registry(())
    )
    request = RawContextRequest(
        mode=mode,
        user_intent=user_intent,
        duration_seconds=8,
        assets=registry.to_asset_descriptors(),
        hard_constraints=constraints,
        reference_registry=registry,
        evidence=evidence,
    )
    plan = H3ContextPlanNode().build_plan(request, registry, graph)[0]
    document = H3ContextCompilerNode().compile(plan)[2]
    report = H3ContextValidatorNode().validate(plan, document)[1]
    projection = sidebar.publish(
        report,
        build_native_h3_wiring(report),
        ExecutionCorrelation("prompt.source", "node.source"),
    )
    return sidebar.snapshot_production_planning_source(
        projection.workspace_id,
        expected_report_revision=projection.report_revision,
        expected_report_fingerprint=projection.report_fingerprint,
    )


def _proposal(
    source: ProductionPlanningSourceSnapshot,
    *,
    parts: tuple[int, ...],
    texts: tuple[str, ...] | None = None,
) -> tuple[ProductionPlanningContextV2, SegmentationProposalV2]:
    target = sum(parts)
    context = build_production_planning_context(
        source,
        planning_context_id="planning.materializer",
        revision=1,
        duration_intent=ProductionDurationIntentV1(
            target_seconds=target,
            policy=SegmentationPolicyV1.AUTO_STORYBOARD,
            segment_min_seconds=min(parts),
            segment_max_seconds=max(parts),
        ),
    )
    rows: list[StoryboardShotV1] = []
    cursor = 0
    for ordinal, seconds in enumerate(parts, start=1):
        start = cursor * 1000
        cursor += seconds
        rows.append(
            StoryboardShotV1(
                shot_id=f"shot_{ordinal}",
                ordinal=ordinal,
                start_milliseconds=start,
                end_milliseconds=cursor * 1000,
                text=f"Action {ordinal}." if texts is None else texts[ordinal - 1],
                source_span=(start, cursor * 1000),
            )
        )
    request = ProductionStoryboardAdmissionRequestV1(
        request_id="storyboard.materializer",
        planning_context_id=context.planning_context_id,
        planning_context_revision=context.revision,
        planning_context_fingerprint=context.fingerprint,
        optimized_candidate_id=context.optimized_candidate_id,
        optimized_candidate_text_fingerprint=context.optimized_candidate_text_fingerprint,
        production_duration_intent_fingerprint=context.duration_intent_fingerprint,
        source_kind=StoryboardSourceKindV1.USER_REVIEWED_TYPED_ROWS,
        typed_rows=tuple(rows),
        user_reviewed=True,
    )
    admission = ProductionStoryboardAdmissionService().admit(context, request)
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(context, admission)
    assert isinstance(proposal, SegmentationProposalV2)
    return context, proposal


def test_materializer_runs_canonical_pipeline_one_workspace_at_a_time_and_releases() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(4, 4))
    materialize = CanonicalProductionMaterializer(source, workspace_registry=sidebar)

    first = materialize(context, proposal, proposal.segments[0])
    receipt = derive_segment_context_materialization_receipt(
        context, proposal, proposal.segments[0], first
    )
    assert receipt.segment_id == proposal.segments[0].segment_id
    assert first.report.validation.is_valid
    assert first.report.prompt_document.text == proposal.segments[0].local_prompt
    assert first.wiring.prompt == proposal.segments[0].local_prompt
    with pytest.raises(ProductionImportError, match="segment_context_materialization_active"):
        materialize(context, proposal, proposal.segments[1])

    first.release()
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.get(first.context_workspace_handle)
    second = materialize(context, proposal, proposal.segments[1])
    second.release()


def test_retained_materializer_uses_snapshot_content_after_source_workspace_expiry() -> None:
    clock = _Clock()
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60, clock=clock)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))
    materialize = CanonicalProductionMaterializer(source, workspace_registry=sidebar)

    clock.value = 61.0
    claim = materialize(context, proposal, proposal.segments[0])
    assert claim.report.request.user_intent == proposal.segments[0].local_prompt
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.snapshot_production_planning_source(
            source.workspace_id,
            expected_report_revision=source.report_revision,
            expected_report_fingerprint=source.report_fingerprint,
        )
    claim.release()


def test_post_publication_seed_failure_compensates_scratch_and_releases_lease() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))

    class _SeedFaultMaterializer(CanonicalProductionMaterializer):
        fail_seed = True
        published_workspace_id: str | None = None

        def _publish(
            self,
            report: ContextReport,
            wiring: NativeH3Wiring,
            correlation: ExecutionCorrelation,
        ) -> SidebarWorkspaceProjection:
            publication = super()._publish(report, wiring, correlation)
            self.published_workspace_id = publication.workspace_id
            return publication

        def _claim_materialization_seed(
            self,
            workspace_id: str,
            *,
            expected_report_revision: int,
            expected_report_fingerprint: str,
        ) -> SidebarProductionSeed:
            if self.fail_seed:
                raise RuntimeError("injected seed claim failure")
            return super()._claim_materialization_seed(
                workspace_id,
                expected_report_revision=expected_report_revision,
                expected_report_fingerprint=expected_report_fingerprint,
            )

    materialize = _SeedFaultMaterializer(source, workspace_registry=sidebar)
    with pytest.raises(RuntimeError, match="injected seed claim failure"):
        materialize(context, proposal, proposal.segments[0])
    assert materialize.published_workspace_id is not None
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.claim_production_seed(materialize.published_workspace_id)

    materialize.fail_seed = False
    retry = materialize(context, proposal, proposal.segments[0])
    retry.release()


def test_expired_pruned_scratch_release_is_terminal_and_materializer_is_reusable() -> None:
    clock = _Clock()
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60, clock=clock)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))
    materialize = CanonicalProductionMaterializer(source, workspace_registry=sidebar)
    claim = materialize(context, proposal, proposal.segments[0])

    clock.value = 61.0
    claim.release()
    assert claim.released
    retry = materialize(context, proposal, proposal.segments[0])
    retry.release()


def test_release_refuses_wrong_current_scratch_cas_without_removing_it() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))
    materialize = CanonicalProductionMaterializer(source, workspace_registry=sidebar)
    claim = materialize(context, proposal, proposal.segments[0])
    entry = sidebar._entries[claim.context_workspace_handle]
    changed = replace(entry.report, revision=entry.report.revision + 1)
    entry.report = changed
    entry.wiring = build_native_h3_wiring(changed)

    with pytest.raises(SidebarWorkspaceError, match="production_materialization_stale"):
        claim.release()
    assert claim.context_workspace_handle in sidebar._entries
    sidebar.release_production_materialization(
        claim.context_workspace_handle,
        expected_report_revision=changed.revision,
        expected_report_fingerprint=fingerprint_context_report(changed),
    )
    retry = materialize(context, proposal, proposal.segments[0])
    retry.release()


def test_materializer_preserves_full_reference_source_profile_and_exact_prompt() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar, mode=TaskMode.REF2VA)
    context, proposal = _proposal(source, parts=(8,))

    claim = CanonicalProductionMaterializer(source, workspace_registry=sidebar)(
        context, proposal, proposal.segments[0]
    )
    assert claim.report.request.profile == source.report.request.profile
    assert claim.report.plan.request.profile == source.report.plan.request.profile
    assert claim.report.prompt_document.text == proposal.segments[0].local_prompt
    assert claim.wiring.prompt == proposal.segments[0].local_prompt
    claim.release()


def test_canonical_lowering_rebuilds_the_actual_node_pipeline_exactly() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))
    segment = proposal.segments[0]
    claim = CanonicalProductionMaterializer(source, workspace_registry=sidebar)(
        context, proposal, segment
    )
    snapshot = claim_canonical_production_materialization(claim)
    lowering = snapshot.canonical_lowering

    assert lowering is not None
    rebuilt_request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        segment.local_prompt,
        (segment.duration.requested_seconds * 1_000) / 1_000,
    )[0]
    rebuilt_plan = H3ContextPlanNode().build_plan(rebuilt_request)[0]
    assert rebuilt_plan == claim.report.plan
    _prompt, base_report, _document = H3ContextCompilerNode().compile(rebuilt_plan)
    assert fingerprint_context_report(base_report) == lowering.base_report_fingerprint
    assert base_report.revision == lowering.base_report_revision
    _edited, _updated, _override, prompt_document = H3ContextAuditOverrideNode().apply(
        base_report,
        lowering.base_report_fingerprint,
        lowering.override_revision,
        lowering.reason,
        segment.local_prompt,
    )
    _validation, final_report = H3ContextValidatorNode().validate(rebuilt_plan, prompt_document)
    assert lowering.reason == PRODUCTION_CANONICAL_LOWERING_REASON
    assert final_report == claim.report
    assert build_native_h3_wiring(final_report) == claim.wiring
    assert claim.wiring.prompt == segment.local_prompt
    claim.release()


def test_canonical_lowering_refuses_constraints_with_transformation_history() -> None:
    constraints = HardConstraintSet(
        (RequiredContent("required.sparkle", ContentScope.ACTION, "Must sparkle"),)
    )
    transformed = HardConstraintSet(
        (RequiredContent("required.sparkle", ContentScope.ACTION, "Sparkle"),)
    ).apply_authorized_transformation(
        ConstraintTransformation(
            "transform.sparkle",
            "required.sparkle",
            TransformationField.CONTENT,
            "Sparkle",
            "Must sparkle",
            "user",
            "Preserve the approved wording.",
        )
    )
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar, constraints=constraints)
    context, proposal = _proposal(source, parts=(8,))
    claim = CanonicalProductionMaterializer(source, workspace_registry=sidebar)(
        context, proposal, proposal.segments[0]
    )

    assert claim_canonical_production_materialization(claim).canonical_lowering is None
    assert claim.report.request.hard_constraints == constraints

    _prompt, compiled_report, _document = H3ContextCompilerNode().compile(claim.report.plan)
    transformed_request = RawContextRequest(
        mode=TaskMode.T2VA,
        user_intent=proposal.segments[0].local_prompt,
        duration_seconds=8,
        hard_constraints=transformed,
    )
    assert (
        _derive_canonical_lowering(
            transformed_request,
            claim.report.plan,
            compiled_report,
            proposal.segments[0],
        )
        is None
    )
    claim.release()


def test_canonical_lowering_refuses_invisible_forbidden_content_with_same_prompt() -> None:
    baseline_sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    baseline_source = _source(baseline_sidebar)
    _baseline_context, baseline_proposal = _proposal(baseline_source, parts=(8,))
    forbidden = HardConstraintSet(
        (ForbiddenContent("forbidden.mark", ContentScope.VISUAL, "forbidden mark"),)
    )
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar, constraints=forbidden)
    context, proposal = _proposal(source, parts=(8,))

    assert proposal.segments[0].local_prompt == baseline_proposal.segments[0].local_prompt
    claim = CanonicalProductionMaterializer(source, workspace_registry=sidebar)(
        context, proposal, proposal.segments[0]
    )
    assert claim.report.prompt_document.text == baseline_proposal.segments[0].local_prompt
    assert claim.report.request.hard_constraints == forbidden
    assert claim_canonical_production_materialization(claim).canonical_lowering is None
    claim.release()


def test_canonical_lowering_refuses_evidence_media_and_long_intent() -> None:
    evidence = EvidenceSet(
        (
            EvidenceRecord(
                "evidence.user",
                "The user approved the subject.",
                EvidenceOrigin.USER_DECLARED,
                SupportStatus.SUPPORTED,
                Provenance(
                    EvidenceSource(EvidenceSourceKind.USER_INPUT, "request.user_intent"),
                    ProviderIdentity.MANUAL,
                    EvidenceLevel.COMMUNITY_RECOMMENDED,
                ),
            ),
        )
    )
    # A plain source's own prose is not carried into reviewed rows, so the over-long prompt is the
    # reviewed row itself.
    long_row = "A" * 5_000
    cases: tuple[dict[str, Any], ...] = (
        {"evidence": evidence},
        {"mode": TaskMode.REF2VA},
        {},
    )
    for values in cases:
        sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
        source = _source(sidebar, **values)
        context, proposal = _proposal(source, parts=(8,), texts=None if values else (long_row,))
        claim = CanonicalProductionMaterializer(source, workspace_registry=sidebar)(
            context, proposal, proposal.segments[0]
        )

        assert claim_canonical_production_materialization(claim).canonical_lowering is None
        if "evidence" in values:
            assert claim.report.evidence == evidence
        elif "mode" in values:
            assert claim.report.request.task_mode is TaskMode.REF2VA
            assert claim.report.request.reference_registry.assets
        else:
            assert len(proposal.segments[0].local_prompt) > 4_096
            assert claim.report.request.hard_constraints == HardConstraintSet.empty()
            assert claim.report.evidence == EvidenceSet.empty()
            assert claim.report.request.reference_registry == build_reference_registry(())
        claim.release()


def test_timing_range_at_half_open_cut_is_rebased_for_owning_segment() -> None:
    timing = TimingConstraint(
        "timing.action",
        TimePoint.from_text("4"),
        TimePoint.from_text("6"),
        "action",
    )
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar, constraints=HardConstraintSet((timing,)))
    context, proposal = _proposal(source, parts=(4, 4))

    first = _segment_constraints(
        source.report.request.hard_constraints, proposal, proposal.segments[0]
    )
    second = _segment_constraints(
        source.report.request.hard_constraints, proposal, proposal.segments[1]
    )
    assert first.timings == ()
    assert second.timings[0].start == TimePoint.from_text("0")
    assert second.timings[0].end == TimePoint.from_text("2")

    materialize = CanonicalProductionMaterializer(source, workspace_registry=sidebar)
    claim = materialize(context, proposal, proposal.segments[1])
    assert claim.report.request.hard_constraints.timings == second.timings
    assert claim.report.validation.is_valid
    claim.release()


def test_required_and_global_exact_constraints_reach_real_validator() -> None:
    constraints = HardConstraintSet(
        (
            ExactTextConstraint(
                "exact.global",
                ExactTextKind.DIALOGUE,
                "A subject crosses the scene.",
            ),
            RequiredContent("required.sparkle", ContentScope.ACTION, "Must sparkle"),
        )
    )
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar, constraints=constraints)
    context, proposal = _proposal(source, parts=(8,))
    claim = CanonicalProductionMaterializer(source, workspace_registry=sidebar)(
        context, proposal, proposal.segments[0]
    )
    assert claim.report.request.hard_constraints == constraints
    assert claim.report.validation.is_valid
    assert "Must sparkle" in claim.report.prompt_document.text
    claim.release()


def test_global_and_timed_exact_text_follow_verified_semantic_owners() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    # Ownership is read from each segment's own prompt: both reviewed rows state the global
    # line, only the second states the timed one.
    _context, proposal = _proposal(
        source,
        parts=(4, 4),
        texts=(
            "A subject crosses the scene. Action 1.",
            "A subject crosses the scene. Action 2.",
        ),
    )
    global_exact = ExactTextConstraint(
        "exact.global", ExactTextKind.DIALOGUE, "A subject crosses the scene."
    )
    timed_exact = ExactTextConstraint("exact.second", ExactTextKind.VISIBLE_TEXT, "Action 2.")
    constraints = HardConstraintSet((global_exact, timed_exact))

    first = _segment_constraints(constraints, proposal, proposal.segments[0])
    second = _segment_constraints(constraints, proposal, proposal.segments[1])
    assert first.exact_texts == (global_exact,)
    assert second.exact_texts == (global_exact, timed_exact)


@pytest.mark.parametrize("shared_words", [False, True])
def test_materializer_preserves_source_speaker_ids_and_rebases_owned_bindings(
    shared_words: bool,
) -> None:
    constraints = HardConstraintSet(
        (
            ExactTextConstraint(
                "line.first",
                ExactTextKind.DIALOGUE,
                "First line.",
                "English",
                speakers=(DialogueSpeaker("guide", "guide"),),
                segment_id="segment_1",
            ),
            ExactTextConstraint(
                "line.second",
                ExactTextKind.DIALOGUE,
                "First line." if shared_words else "Second line.",
                "English",
                speakers=(DialogueSpeaker("guest", "guest"),),
                segment_id="segment_2",
            ),
        )
    )
    graph = IntentGraph(
        TimePoint.from_text("8"),
        build_reference_registry(()),
        subjects=(IntentSubject("guide", "the guide"), IntentSubject("guest", "the guest")),
        segments=(
            TimelineSegment("segment_1", TimePoint.from_text("0"), TimePoint.from_text("4")),
            TimelineSegment("segment_2", TimePoint.from_text("4"), TimePoint.from_text("8")),
        ),
    )
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar, constraints=constraints, graph=graph)
    context, proposal = _proposal(source, parts=(4, 4))
    materializer = CanonicalProductionMaterializer(source, workspace_registry=sidebar)
    claim = materializer(context, proposal, proposal.segments[1])
    try:
        expected_words = "First line." if shared_words else "Second line."
        assert f"the guest (S2) says: <d>[English] {expected_words}</d>" in claim.wiring.prompt
        assert claim.report.validation.is_valid and not claim.report.has_errors
        assert claim.report.plan.intent_graph.subjects == graph.subjects
        assert claim.report.request.hard_constraints.exact_texts[0].segment_id == "segment_1"
    finally:
        claim.release()


def test_rebased_timing_with_source_transformation_refuses() -> None:
    timing = TimingConstraint(
        "timing.action",
        TimePoint.from_text("4"),
        TimePoint.from_text("6"),
        "action",
    )
    transformed = HardConstraintSet(
        (timing,),
        (
            ConstraintTransformation(
                "transform.timing",
                "timing.action",
                TransformationField.START,
                "3",
                "4",
                "user",
                "Move the action.",
            ),
        ),
    )
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar, constraints=HardConstraintSet((timing,)))
    _context, proposal = _proposal(source, parts=(4, 4))
    with pytest.raises(
        ProductionImportError,
        match="segment_context_timing_transformation_unsupported",
    ):
        _segment_constraints(transformed, proposal, proposal.segments[1])


def test_unaccounted_required_content_refuses_instead_of_being_dropped() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    _context, proposal = _proposal(source, parts=(8,))
    required = HardConstraintSet(
        (RequiredContent("required.unowned", ContentScope.ACTION, "never represented"),)
    )
    with pytest.raises(ProductionImportError, match="segment_context_constraint_unaccounted"):
        _segment_constraints(required, proposal, proposal.segments[0])


def test_asset_scoped_forbidden_constraint_only_reaches_owning_segment() -> None:
    forbidden = ForbiddenContent(
        "forbidden.first",
        ContentScope.ASSET,
        "forbidden mark",
        asset_id="asset_first",
    )
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar, mode=TaskMode.FL2VA, constraints=HardConstraintSet((forbidden,)))
    _context, proposal = _proposal(source, parts=(4, 4))
    first = _segment_constraints(
        source.report.request.hard_constraints, proposal, proposal.segments[0]
    )
    second = _segment_constraints(
        source.report.request.hard_constraints, proposal, proposal.segments[1]
    )
    assert first.forbidden_content == (forbidden,)
    assert second.forbidden_content == ()


def test_materializer_rejects_segment_outside_exact_proposal() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))
    materialize = CanonicalProductionMaterializer(source, workspace_registry=sidebar)
    foreign: ProposalSegmentV1 = replace(proposal.segments[0], segment_id="segment.foreign")
    with pytest.raises(ProductionImportError, match="segment_context_proposal_mismatch"):
        materialize(context, proposal, foreign)


def test_materializer_rejects_context_with_foreign_semantic_authority() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    context, _proposal_value = _proposal(source, parts=(8,))
    foreign_context = replace(context, subject_ids=("subject.foreign",))

    with pytest.raises(ProductionImportError, match="segment_context_source_mismatch"):
        _assert_source_join(source, foreign_context)


def test_managed_holder_does_not_retry_terminal_release_after_cleanup_failure() -> None:
    calls = 0

    def fail_release() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("cleanup failed")

    claim = ManagedSegmentMaterializationClaimV1(
        context_workspace_handle="ws_" + "a" * 32,
        segment_id="segment_1",
        materialization_receipt_fingerprint="sha256:" + "b" * 64,
        release=fail_release,
    )
    held = _HeldMaterialization(claim, None)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        held.release_once()
    held.release_once()
    assert held.released
    assert calls == 1
