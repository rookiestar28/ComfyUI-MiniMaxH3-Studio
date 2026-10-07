"""M26-03 atomic storyboard-proposal import into Production."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import fields, replace
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest

import comfyui_h3_context.adapters.comfyui_production_workspace as production_workspace_module
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.av_reconstruction_store import (
    AVStoreError,
    AVStoreInspectionStatus,
    AVStorePolicy,
    PrivateAVReconstructionStore,
)
from comfyui_h3_context.adapters.av_reconstruction_transport import AVSegmentSource
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionAuthoringOutputClaim,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.m26_production_assembly import (
    M26ProductionAssemblyError,
    M26ProductionAssemblyExecution,
    M26ProductionAssemblyRuntime,
    execute_m26_production_assembly,
)
from comfyui_h3_context.adapters.managed_sequence_service import (
    build_managed_sequence_authorization,
)
from comfyui_h3_context.adapters.media_subprocess import CancellationProbe
from comfyui_h3_context.adapters.segment_artifact_store import PrivateSegmentArtifactStore
from comfyui_h3_context.core.av_reconstruction import (
    AVAudioDescriptor,
    AVMediaDescriptor,
    AVOperation,
    AVOutputKind,
    AVPublicationState,
    AVRational,
    AVReceiptOutput,
    AVReceiptSegmentResult,
    AVReconstructionApproval,
    AVReconstructionError,
    AVReconstructionPlan,
    AVReconstructionReceipt,
    AVVideoDescriptor,
    qualified_ffmpeg_capability,
)
from comfyui_h3_context.core.canonical import (
    canonical_bytes,
    canonical_fingerprint,
    fingerprint_context_report,
)
from comfyui_h3_context.core.context_reporting import ContextReport
from comfyui_h3_context.core.contracts import AssetRole, MediaKind, TaskMode
from comfyui_h3_context.core.generation_sequence import (
    FingerprintDomain,
    GenerationJobSpec,
    GenerationSequenceProjection,
    build_generation_sequence_plan,
    build_generation_sequence_projection,
    create_generation_sequence_state,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
)
from comfyui_h3_context.core.m26_assembly import (
    M26AssemblyError,
    ProductionAssemblyReceiptV1,
    build_production_assembly_capability,
    build_production_assembly_receipt,
    decode_production_assembly_receipt,
    mint_m26_assembly_authorization,
)
from comfyui_h3_context.core.native_h3 import build_native_h3_wiring
from comfyui_h3_context.core.normalization import RawContextRequest
from comfyui_h3_context.core.production_duration import (
    ProductionDurationIntentV1,
    SegmentationPolicyV1,
)
from comfyui_h3_context.core.production_import import (
    PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_V2_SCHEMA,
    ProductionAutomaticPlanAuthorityV1,
    ProductionAutomaticPlanAuthorityV2,
    ProductionImportError,
    ProductionImportRequestV1,
    SegmentContextMaterializationClaim,
    SegmentContextMaterializationReceiptV1,
    build_production_automatic_plan_authority,
    derive_legacy_segment_context_materialization_receipt,
    derive_segment_context_materialization_receipt,
    is_production_proposal_importable,
    project_production_automatic_plan,
)
from comfyui_h3_context.core.production_semantics import (
    ProductionSemanticAuthorityV1,
    SemanticFieldV1,
)
from comfyui_h3_context.core.production_storyboard import (
    ManagedExecutionQualificationV1,
    PlanningAssetV1,
    ProductionPlanningContextV1,
    ProductionPlanningContextV2,
    ProductionStoryboardAdmissionRequestV1,
    ProductionStoryboardAdmissionService,
    ProductionStoryboardAdmissionV2,
    ProductionStoryboardError,
    ProposalSegmentV1,
    SegmentationProposalV1,
    SegmentationProposalV2,
    StoryboardShotV1,
    StoryboardSourceKindV1,
    build_segmentation_proposal,
    fingerprint_prompt_text,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.recompute_closure import plan_recompute
from comfyui_h3_context.core.registry import (
    ReferenceAsset,
    ReferenceRegistry,
    build_reference_registry,
)
from comfyui_h3_context.core.segment_artifacts import (
    SegmentArtifactReceipt,
    begin_segment_artifact_receipt,
    complete_segment_artifact_receipt,
)
from comfyui_h3_context.core.segment_workspace import (
    AcceptedIntentAuthority,
    create_workspace,
    derive_segment_manifests,
)
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextValidatorNode,
)


def _planning_assets(mode: TaskMode) -> tuple[PlanningAssetV1, ...]:
    if mode is TaskMode.I2VA:
        return (PlanningAssetV1("asset_first", AssetRole.FIRST_FRAME),)
    if mode is TaskMode.L2VA:
        return (PlanningAssetV1("asset_last", AssetRole.LAST_FRAME),)
    if mode is TaskMode.FL2VA:
        return (
            PlanningAssetV1("asset_first", AssetRole.FIRST_FRAME),
            PlanningAssetV1("asset_last", AssetRole.LAST_FRAME),
        )
    if mode is TaskMode.REF2VA:
        return (
            PlanningAssetV1("asset_ref_1", AssetRole.REFERENCE),
            PlanningAssetV1("asset_ref_2", AssetRole.SUBJECT_REFERENCE),
        )
    return ()


def _registry_for(assets: tuple[PlanningAssetV1, ...]) -> ReferenceRegistry:
    if not assets:
        return ReferenceRegistry.empty()
    return build_reference_registry(
        tuple(
            ReferenceAsset(row.asset_id, MediaKind.IMAGE, row.role, index)
            for index, row in enumerate(assets, start=1)
        )
    )


def _report(
    mode: TaskMode,
    *,
    duration_seconds: int,
    user_intent: str,
    assets: tuple[PlanningAssetV1, ...],
) -> ContextReport:
    registry = _registry_for(assets)
    raw = RawContextRequest(
        mode=mode,
        user_intent=user_intent,
        duration_seconds=float(duration_seconds),
        assets=registry.to_asset_descriptors(),
        reference_registry=registry,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


def _parts(policy: SegmentationPolicyV1) -> tuple[int, ...]:
    return {
        SegmentationPolicyV1.FIXED_5: (5,) * 12,
        SegmentationPolicyV1.FIXED_10: (10,) * 6,
        SegmentationPolicyV1.FIXED_12: (12,) * 5,
        SegmentationPolicyV1.FIXED_15: (15,) * 4,
        SegmentationPolicyV1.AUTO_STORYBOARD: (12,) * 5,
    }[policy]


def _storyboard(parts: tuple[int, ...]) -> tuple[StoryboardShotV1, ...]:
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
                text=f"Action {ordinal}.",
                subject_ids=("subject_1",),
                exact_dialogue=("<d>[English] Exact line.</d>",) if ordinal == 1 else (),
                visible_text=("SIGN",) if ordinal == len(parts) else (),
                source_span=(start, cursor * 1000),
            )
        )
    return tuple(rows)


def _authorities(
    *,
    policy: SegmentationPolicyV1 = SegmentationPolicyV1.FIXED_15,
    mode: TaskMode = TaskMode.T2VA,
    qualification: ManagedExecutionQualificationV1 = ManagedExecutionQualificationV1.PENDING,
    parts: tuple[int, ...] | None = None,
    segment_min_seconds: int = 4,
    segment_max_seconds: int = 15,
    hard_constraints: tuple[str, ...] = ("Preserve subject_1 identity.",),
) -> tuple[ProductionPlanningContextV2, ProductionStoryboardAdmissionV2, SegmentationProposalV2]:
    assets = _planning_assets(mode)
    source = _report(
        mode,
        duration_seconds=10,
        user_intent="Source planning context.",
        assets=assets,
    )
    context = ProductionPlanningContextV2(
        planning_context_id="planning_" + "a" * 24,
        revision=3,
        optimized_candidate_id="candidate_" + "b" * 24,
        optimized_candidate_text_fingerprint=fingerprint_prompt_text("Reviewed storyboard."),
        source_request_fingerprint=canonical_fingerprint(
            {
                "schema": "test.production_source_request.v1",
                "report_fingerprint": fingerprint_context_report(source),
            }
        ),
        source_intent_graph_fingerprint=canonical_fingerprint(source.plan.intent_graph.to_wire()),
        source_profile_fingerprint=canonical_fingerprint(source.request.profile.to_wire()),
        reference_registry_fingerprint=canonical_fingerprint(
            source.request.reference_registry.to_wire()
        ),
        source_context_duration_seconds=10,
        source_context_duration_provenance="context_report.effective_duration",
        production_duration_intent=ProductionDurationIntentV1(
            target_seconds=sum(parts) if parts is not None else 60,
            policy=policy,
            segment_min_seconds=segment_min_seconds,
            segment_max_seconds=segment_max_seconds,
        ),
        global_task_mode=mode,
        assets=assets,
        subject_ids=("subject_1",),
        reference_ids=tuple(row.asset_id for row in assets),
        hard_constraints=hard_constraints,
        managed_execution_qualification=qualification,
    )
    rows = _storyboard(_parts(policy) if parts is None else parts)
    request = ProductionStoryboardAdmissionRequestV1(
        request_id="storyboard_" + "c" * 24,
        planning_context_id=context.planning_context_id,
        planning_context_revision=context.revision,
        planning_context_fingerprint=context.fingerprint,
        optimized_candidate_id=context.optimized_candidate_id,
        optimized_candidate_text_fingerprint=context.optimized_candidate_text_fingerprint,
        production_duration_intent_fingerprint=context.duration_intent_fingerprint,
        source_kind=StoryboardSourceKindV1.USER_REVIEWED_TYPED_ROWS,
        typed_rows=rows,
        user_reviewed=True,
    )
    admission = ProductionStoryboardAdmissionService().admit(context, request)
    assert isinstance(admission, ProductionStoryboardAdmissionV2)
    proposal = build_segmentation_proposal(context, admission)
    assert isinstance(proposal, SegmentationProposalV2)
    return context, admission, proposal


def _production(
    context: ProductionPlanningContextV2,
    *,
    max_ledger_entries: int = 256,
) -> tuple[SidebarWorkspaceRegistry, ProductionWorkspaceRegistry, ProductionWorkbenchProjection]:
    source = _report(
        context.global_task_mode,
        duration_seconds=10,
        user_intent="Initial manual workspace.",
        assets=context.assets,
    )
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    sidebar_projection = sidebar.publish(
        source,
        build_native_h3_wiring(source),
        ExecutionCorrelation("prompt.initial", "node.initial"),
    )
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_ledger_entries=max_ledger_entries,
    )
    created = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.create.initial",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": sidebar_projection.workspace_id},
        }
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    return sidebar, registry, created


def _request(
    created: ProductionWorkbenchProjection,
    context: ProductionPlanningContextV2,
    proposal: SegmentationProposalV2,
) -> ProductionImportRequestV1:
    return ProductionImportRequestV1(
        request_id="production.import.proposal",
        workspace_handle=created.workspace_handle,
        workspace_id=created.workspace_id,
        expected_workspace_revision=created.workspace_revision,
        expected_workspace_fingerprint=created.workspace_fingerprint,
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.revision,
        proposal_fingerprint=proposal.fingerprint,
        planning_context_fingerprint=context.fingerprint,
        source_request_fingerprint=context.source_request_fingerprint,
        source_profile_fingerprint=context.source_profile_fingerprint,
    )


class _Materializer:
    def __init__(self, context: ProductionPlanningContextV2, *, fail_at: int | None = None) -> None:
        self.context = context
        self.fail_at = fail_at
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.released: list[str] = []

    def __call__(
        self,
        _context: ProductionPlanningContextV2,
        _proposal: SegmentationProposalV2,
        segment: ProposalSegmentV1,
    ) -> SegmentContextMaterializationClaim:
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("private materializer failure")
        assets = tuple(row for row in self.context.assets if row.asset_id in segment.asset_ids)
        report = _report(
            segment.task_mode,
            duration_seconds=segment.duration.requested_seconds,
            user_intent=segment.local_prompt,
            assets=assets,
        )
        handle = "ws_" + f"{self.calls:032d}"
        self.active += 1
        self.max_active = max(self.max_active, self.active)

        def release() -> None:
            self.active -= 1
            self.released.append(handle)

        return SegmentContextMaterializationClaim(
            context_workspace_handle=handle,
            report=report,
            wiring=build_native_h3_wiring(report),
            release=release,
        )


def _materialization_receipts(
    context: ProductionPlanningContextV2,
    proposal: SegmentationProposalV2,
) -> tuple[SegmentContextMaterializationReceiptV1, ...]:
    materializer = _Materializer(context)
    receipts: list[SegmentContextMaterializationReceiptV1] = []
    for segment in proposal.segments:
        claim = materializer(context, proposal, segment)
        try:
            receipts.append(
                derive_segment_context_materialization_receipt(
                    context,
                    proposal,
                    segment,
                    claim,
                )
            )
        finally:
            claim.release()
    return tuple(receipts)


def _legacy_context(context: ProductionPlanningContextV2) -> ProductionPlanningContextV1:
    return ProductionPlanningContextV1(
        **{
            item.name: getattr(context, item.name)
            for item in fields(ProductionPlanningContextV1)
            if item.init
        }
    )


def _legacy_proposal(
    context: ProductionPlanningContextV1, proposal: SegmentationProposalV2
) -> SegmentationProposalV1:
    return SegmentationProposalV1(
        proposal_id=proposal.proposal_id,
        revision=proposal.revision,
        planning_context_fingerprint=context.fingerprint,
        storyboard_admission_fingerprint=proposal.storyboard_admission_fingerprint,
        duration_intent_fingerprint=context.duration_intent_fingerprint,
        duration_plan=proposal.duration_plan,
        segments=proposal.segments,
        blocker_codes=proposal.blocker_codes,
        start_hold_codes=proposal.start_hold_codes,
    )


@pytest.mark.parametrize(
    "policy",
    [
        SegmentationPolicyV1.FIXED_5,
        SegmentationPolicyV1.FIXED_10,
        SegmentationPolicyV1.FIXED_12,
        SegmentationPolicyV1.FIXED_15,
        SegmentationPolicyV1.AUTO_STORYBOARD,
    ],
)
def test_atomic_import_materializes_all_presets_one_at_a_time(policy: SegmentationPolicyV1) -> None:
    context, _admission, proposal = _authorities(policy=policy)
    _sidebar, registry, created = _production(context)
    materializer = _Materializer(context)

    result = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=materializer,
    )

    assert result.status == 200
    assert result.replayed is False
    assert result.projection.workspace_revision == created.workspace_revision + 1
    assert result.projection.segment_ids == tuple(row.segment_id for row in proposal.segments)
    assert result.projection.reconstruction_order == result.projection.segment_ids
    assert result.projection.start_hold_codes == ("managed_execution_qualification_pending",)
    assert result.projection.startable is False
    assert materializer.calls == len(proposal.segments)
    assert materializer.max_active == 1
    assert materializer.active == 0
    assert len(materializer.released) == len(proposal.segments)

    authority = registry.claim_automatic_plan_authority(
        created.workspace_handle,
        expected_workspace_revision=result.projection.workspace_revision,
        expected_workspace_fingerprint=result.projection.workspace_fingerprint,
        expected_plan_fingerprint=result.projection.plan_fingerprint,
    )
    assert isinstance(authority, ProductionAutomaticPlanAuthorityV2)
    assert authority.schema == PRODUCTION_AUTOMATIC_PLAN_AUTHORITY_V2_SCHEMA
    assert authority.proposal is proposal
    assert tuple(row.local_prompt for row in authority.proposal.segments) == tuple(
        row.local_prompt for row in proposal.segments
    )
    manifests = derive_segment_manifests(authority.workspace)
    assert tuple(row.segment_id for row in manifests) == result.projection.segment_ids
    assert tuple(row.reference_ids for row in manifests) == tuple(
        row.asset_ids for row in proposal.segments
    )
    assert tuple(row.duration.frame_count for row in manifests) == tuple(
        row.duration.frame_count for row in proposal.segments
    )
    assert all(row.dependency_segment_ids == () for row in manifests)
    assert tuple(
        (row.predecessor_segment_id, row.successor_segment_id, row.boundary_milliseconds)
        for row in result.projection.cut_boundary_receipts
    ) == tuple(
        (
            proposal.segments[index - 1].segment_id,
            segment.segment_id,
            segment.global_start_milliseconds,
        )
        for index, segment in enumerate(proposal.segments[1:], start=1)
    )
    public_wire = result.projection.to_wire()
    assert "local_prompt" not in str(public_wire)
    assert "context_workspace_handle" not in str(public_wire)
    assert all(segment.local_prompt not in str(public_wire) for segment in proposal.segments)


def test_legacy_v1_authority_is_retained_but_current_import_gates_require_v2() -> None:
    context, _admission, proposal = _authorities()
    legacy_context = _legacy_context(context)
    legacy_proposal = _legacy_proposal(legacy_context, proposal)
    _sidebar, registry, created = _production(context)
    workspace = registry.claim_workspace_authority(
        created.workspace_handle,
        expected_workspace_revision=created.workspace_revision,
        expected_workspace_fingerprint=created.workspace_fingerprint,
    )
    receipts = _materialization_receipts(context, proposal)
    current = build_production_automatic_plan_authority(workspace, context, proposal, receipts)
    legacy_receipts = tuple(
        replace(
            receipt,
            proposal_fingerprint=legacy_proposal.fingerprint,
            receipt_fingerprint=None,
        )
        for receipt in current.materialization_receipts
    )
    legacy = ProductionAutomaticPlanAuthorityV1(
        workspace=current.workspace,
        proposal=legacy_proposal,
        planning_context_fingerprint=legacy_context.fingerprint,
        source_request_fingerprint=legacy_context.source_request_fingerprint,
        source_profile_fingerprint=legacy_context.source_profile_fingerprint,
        materialization_receipts=legacy_receipts,
        cut_boundary_receipts=current.cut_boundary_receipts,
        manifest_fingerprints=current.manifest_fingerprints,
        reconstruction_order=current.reconstruction_order,
        start_hold_codes=legacy_proposal.start_hold_codes,
    )

    assert legacy.schema == "h3.context.production_automatic_plan_authority.v1"
    assert type(legacy.proposal) is SegmentationProposalV1
    assert SegmentationProposalV1.from_wire(legacy.proposal.to_wire()) == legacy.proposal
    assert project_production_automatic_plan("legacy.retained", legacy).plan_fingerprint == (
        legacy.fingerprint
    )
    assert is_production_proposal_importable(proposal)
    with pytest.raises(ProductionImportError, match="production_proposal_authority"):
        is_production_proposal_importable(legacy_proposal)  # type: ignore[arg-type]
    with pytest.raises(ProductionImportError, match="production_import_authority_type"):
        build_production_automatic_plan_authority(
            workspace,
            legacy_context,  # type: ignore[arg-type]
            proposal,
            receipts,
        )
    with pytest.raises(ProductionImportError, match="production_import_authority_type"):
        build_production_automatic_plan_authority(
            workspace,
            context,
            legacy_proposal,  # type: ignore[arg-type]
            legacy_receipts,
        )
    claim = _Materializer(context)(context, proposal, proposal.segments[0])
    try:
        legacy_receipt = derive_legacy_segment_context_materialization_receipt(
            legacy_context,
            legacy_proposal,
            legacy_proposal.segments[0],
            claim,
        )
        assert legacy_receipt.proposal_fingerprint == legacy_proposal.fingerprint
        with pytest.raises(
            ProductionImportError, match="segment_context_materialization_authority"
        ):
            derive_segment_context_materialization_receipt(
                legacy_context,  # type: ignore[arg-type]
                proposal,
                proposal.segments[0],
                claim,
            )
    finally:
        claim.release()


def test_current_import_joins_proposal_semantics_to_context_before_materialization() -> None:
    context, _admission, proposal = _authorities()
    changed_context = replace(
        context,
        semantic_authority=ProductionSemanticAuthorityV1(
            global_fields=(
                SemanticFieldV1(
                    field_id="late_context_field",
                    heading="overall_soundscape",
                    text="This semantic authority was not present when the proposal was built.",
                ),
            )
        ),
    )
    rebound_proposal = replace(
        proposal,
        planning_context_fingerprint=changed_context.fingerprint,
    )
    claim = _Materializer(changed_context)(
        changed_context,
        rebound_proposal,
        rebound_proposal.segments[0],
    )
    try:
        with pytest.raises(ProductionStoryboardError, match="current_proposal_semantic_source"):
            derive_segment_context_materialization_receipt(
                changed_context,
                rebound_proposal,
                rebound_proposal.segments[0],
                claim,
            )
    finally:
        claim.release()

    _sidebar, registry, created = _production(changed_context)
    workspace = registry.claim_workspace_authority(
        created.workspace_handle,
        expected_workspace_revision=created.workspace_revision,
        expected_workspace_fingerprint=created.workspace_fingerprint,
    )
    with pytest.raises(ProductionStoryboardError, match="current_proposal_semantic_source"):
        build_production_automatic_plan_authority(
            workspace,
            changed_context,
            rebound_proposal,
            (),
        )


def test_exact_fifteen_segment_limit_is_importable_without_retaining_handles() -> None:
    context, _admission, proposal = _authorities(
        policy=SegmentationPolicyV1.AUTO_STORYBOARD,
        parts=(4,) * 15,
        segment_min_seconds=4,
        segment_max_seconds=4,
    )
    assert len(proposal.segments) == 15
    _sidebar, registry, created = _production(context)
    materializer = _Materializer(context)

    result = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=materializer,
    )

    assert len(result.projection.segment_ids) == 15
    assert materializer.calls == 15
    assert materializer.max_active == 1
    assert materializer.active == 0


def test_imported_authority_rematerializes_one_exact_segment_on_eligibility() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    materializer = _Materializer(context)
    imported = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=materializer,
    )
    authority = registry.claim_automatic_plan_authority(
        created.workspace_handle,
        expected_workspace_revision=imported.projection.workspace_revision,
        expected_workspace_fingerprint=imported.projection.workspace_fingerprint,
        expected_plan_fingerprint=imported.projection.plan_fingerprint,
    )
    initial_calls = materializer.calls

    claim, receipt = registry.materialize_automatic_plan_segment(
        authority,
        proposal.segments[0].segment_id,
    )

    assert materializer.calls == initial_calls + 1
    assert materializer.active == 1
    assert receipt == authority.materialization_receipts[0]
    assert claim.context_workspace_handle not in str(authority.to_wire())
    claim.release()
    assert materializer.active == 0

    with pytest.raises(ProductionWorkbenchError, match="automatic_plan_segment_unavailable"):
        registry.materialize_automatic_plan_segment(authority, "segment.foreign")


def test_imported_authority_builds_exact_content_free_managed_parent() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    imported = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=_Materializer(context),
    )
    authority = registry.claim_automatic_plan_authority(
        created.workspace_handle,
        expected_workspace_revision=imported.projection.workspace_revision,
        expected_workspace_fingerprint=imported.projection.workspace_fingerprint,
        expected_plan_fingerprint=imported.projection.plan_fingerprint,
    )
    assert isinstance(authority, ProductionAutomaticPlanAuthorityV2)

    parent = build_managed_sequence_authorization(
        authority,
        sequence_id="managed.sequence.production.import",
        generation_plan_fingerprint=canonical_fingerprint(
            {"schema": "test.managed_generation_plan.v1"}
        ),
        compiler_fingerprint=canonical_fingerprint({"schema": "test.managed_compiler.v1"}),
        host_capability_fingerprint=authority.materialization_receipts[0].capability_fingerprint,
    )

    assert parent.workspace_id == authority.workspace.workspace_id
    assert parent.workspace_revision == authority.workspace.revision
    assert parent.workspace_fingerprint == authority.workspace.fingerprint
    assert parent.production_plan_fingerprint == authority.fingerprint
    assert parent.proposal_fingerprint == authority.proposal.fingerprint
    assert tuple(row.segment_id for row in parent.segments) == authority.reconstruction_order
    assert tuple(row.manifest_fingerprint for row in parent.segments) == (
        authority.manifest_fingerprints
    )
    assert tuple(row.materialization_receipt_fingerprint for row in parent.segments) == tuple(
        row.fingerprint for row in authority.materialization_receipts
    )
    assert parent.segments[0].predecessor_segment_id is None
    assert tuple(row.predecessor_segment_id for row in parent.segments[1:]) == tuple(
        row.segment_id for row in parent.segments[:-1]
    )
    assert all(row.local_prompt not in str(parent.to_wire()) for row in proposal.segments)


@pytest.mark.parametrize("fail_at", [1, 2, 4])
def test_any_materialization_failure_releases_claim_and_leaves_no_partial_rows(
    fail_at: int,
) -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    materializer = _Materializer(context, fail_at=fail_at)

    with pytest.raises(ProductionWorkbenchError) as caught:
        registry.import_automatic_plan(
            _request(created, context, proposal),
            planning_context=context,
            proposal=proposal,
            materialize=materializer,
        )
    assert caught.value.code == "segment_context_materialization_failed"
    assert materializer.active == 0
    assert len(materializer.released) == fail_at - 1

    reread = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": f"read.after.failure.{fail_at}",
            "action": "read_projection",
            "payload": {"workspace_handle": created.workspace_handle},
        }
    ).projection
    assert reread == created
    with pytest.raises(ProductionWorkbenchError, match="automatic_plan_unavailable"):
        registry.claim_automatic_plan_authority(
            created.workspace_handle,
            expected_workspace_revision=created.workspace_revision,
            expected_workspace_fingerprint=created.workspace_fingerprint,
            expected_plan_fingerprint="sha256:" + "0" * 64,
        )


def test_exact_retry_skips_materialization_while_conflict_and_stale_requests_do_not_mutate() -> (
    None
):
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    request = _request(created, context, proposal)
    materializer = _Materializer(context)
    first = registry.import_automatic_plan(
        request,
        planning_context=context,
        proposal=proposal,
        materialize=materializer,
    )
    calls = materializer.calls
    replay = registry.import_automatic_plan(
        request,
        planning_context=context,
        proposal=proposal,
        materialize=materializer,
    )
    assert replay.replayed is True
    assert replay.projection == first.projection
    assert materializer.calls == calls

    with pytest.raises(ProductionWorkbenchError, match="request_id_conflict"):
        registry.import_automatic_plan(
            replace(request, proposal_fingerprint="sha256:" + "f" * 64),
            planning_context=context,
            proposal=proposal,
            materialize=materializer,
        )
    with pytest.raises(ProductionWorkbenchError, match="stale_workspace"):
        registry.import_automatic_plan(
            replace(request, request_id="production.import.stale"),
            planning_context=context,
            proposal=proposal,
            materialize=materializer,
        )
    assert materializer.calls == calls


def test_receipt_identity_excludes_transient_context_handle_and_round_trips() -> None:
    context, _admission, proposal = _authorities(
        qualification=ManagedExecutionQualificationV1.QUALIFIED
    )
    segment = proposal.segments[0]
    report = _report(
        segment.task_mode,
        duration_seconds=segment.duration.requested_seconds,
        user_intent=segment.local_prompt,
        assets=(),
    )
    first_report = replace(report, report_id="report.ephemeral.a")
    second_report = replace(report, report_id="report.ephemeral.b")
    first_wiring = build_native_h3_wiring(first_report)
    second_wiring = build_native_h3_wiring(second_report)
    first = derive_segment_context_materialization_receipt(
        context,
        proposal,
        segment,
        SegmentContextMaterializationClaim(
            context_workspace_handle="ws_" + "a" * 32,
            report=first_report,
            wiring=first_wiring,
            release=lambda: None,
        ),
    )
    second = derive_segment_context_materialization_receipt(
        context,
        proposal,
        segment,
        SegmentContextMaterializationClaim(
            context_workspace_handle="ws_" + "b" * 32,
            report=second_report,
            wiring=second_wiring,
            release=lambda: None,
        ),
    )
    assert first == second
    assert first.fingerprint == second.fingerprint
    assert "ws_" not in repr(first)
    wire = first.to_wire()
    assert "local_prompt" not in wire
    assert segment.local_prompt not in str(wire)
    assert "report_id" not in str(wire)
    assert SegmentContextMaterializationReceiptV1.from_wire(wire) == first


def test_released_materialization_claim_is_terminal_authority() -> None:
    context, _admission, proposal = _authorities()
    segment = proposal.segments[0]
    report = _report(
        segment.task_mode,
        duration_seconds=segment.duration.requested_seconds,
        user_intent=segment.local_prompt,
        assets=(),
    )
    claim = SegmentContextMaterializationClaim(
        context_workspace_handle="ws_" + "c" * 32,
        report=report,
        wiring=build_native_h3_wiring(report),
        release=lambda: None,
    )
    claim.release()

    with pytest.raises(ProductionImportError, match="segment_context_materialization_authority"):
        derive_segment_context_materialization_receipt(context, proposal, segment, claim)


def test_release_failure_is_redacted_and_leaves_production_unchanged() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    claims: list[SegmentContextMaterializationClaim] = []

    def release_fails(
        _context: ProductionPlanningContextV2,
        _proposal: SegmentationProposalV2,
        segment: ProposalSegmentV1,
    ) -> SegmentContextMaterializationClaim:
        report = _report(
            segment.task_mode,
            duration_seconds=segment.duration.requested_seconds,
            user_intent=segment.local_prompt,
            assets=(),
        )

        def fail() -> None:
            raise RuntimeError("private cleanup path")

        claim = SegmentContextMaterializationClaim(
            context_workspace_handle="ws_" + "e" * 32,
            report=report,
            wiring=build_native_h3_wiring(report),
            release=fail,
        )
        claims.append(claim)
        return claim

    with pytest.raises(ProductionWorkbenchError) as caught:
        registry.import_automatic_plan(
            _request(created, context, proposal),
            planning_context=context,
            proposal=proposal,
            materialize=release_fails,
        )
    assert caught.value.code == "segment_context_release_failed"
    assert str(caught.value) == "segment_context_release_failed"
    assert claims[0].released is True
    reread = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "read.after.release.failure",
            "action": "read_projection",
            "payload": {"workspace_handle": created.workspace_handle},
        }
    ).projection
    assert reread == created


def test_materialization_drift_refuses_before_production_commit() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)

    def drifted(
        _context: ProductionPlanningContextV2,
        _proposal: SegmentationProposalV2,
        segment: ProposalSegmentV1,
    ) -> SegmentContextMaterializationClaim:
        report = _report(
            segment.task_mode,
            duration_seconds=segment.duration.requested_seconds,
            user_intent=segment.local_prompt + " changed",
            assets=(),
        )
        return SegmentContextMaterializationClaim(
            context_workspace_handle="ws_" + "d" * 32,
            report=report,
            wiring=build_native_h3_wiring(report),
            release=lambda: None,
        )

    with pytest.raises(ProductionWorkbenchError, match="segment_context_materialization_mismatch"):
        registry.import_automatic_plan(
            _request(created, context, proposal),
            planning_context=context,
            proposal=proposal,
            materialize=drifted,
        )


def test_reference_role_drift_refuses_before_production_commit() -> None:
    context, _admission, proposal = _authorities(mode=TaskMode.REF2VA)
    _sidebar, registry, created = _production(context)

    def role_drifted(
        _context: ProductionPlanningContextV2,
        _proposal: SegmentationProposalV2,
        segment: ProposalSegmentV1,
    ) -> SegmentContextMaterializationClaim:
        swapped_assets = (
            PlanningAssetV1("asset_ref_1", AssetRole.SUBJECT_REFERENCE),
            PlanningAssetV1("asset_ref_2", AssetRole.REFERENCE),
        )
        report = _report(
            segment.task_mode,
            duration_seconds=segment.duration.requested_seconds,
            user_intent=segment.local_prompt,
            assets=swapped_assets,
        )
        return SegmentContextMaterializationClaim(
            context_workspace_handle="ws_" + "r" * 32,
            report=report,
            wiring=build_native_h3_wiring(report),
            release=lambda: None,
        )

    with pytest.raises(ProductionWorkbenchError, match="segment_context_materialization_mismatch"):
        registry.import_automatic_plan(
            _request(created, context, proposal),
            planning_context=context,
            proposal=proposal,
            materialize=role_drifted,
        )


def test_known_unsupported_managed_execution_refuses_without_materialization() -> None:
    context, _admission, proposal = _authorities(
        qualification=ManagedExecutionQualificationV1.UNSUPPORTED
    )
    assert proposal.importable
    _sidebar, registry, created = _production(context)
    materializer = _Materializer(context)

    with pytest.raises(ProductionWorkbenchError, match="automatic_plan_not_importable"):
        registry.import_automatic_plan(
            _request(created, context, proposal),
            planning_context=context,
            proposal=proposal,
            materialize=materializer,
        )
    assert materializer.calls == 0


def test_builder_rejects_receipt_whose_prompt_fingerprint_does_not_match_proposal() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    workspace = registry.claim_workspace_authority(
        created.workspace_handle,
        expected_workspace_revision=created.workspace_revision,
        expected_workspace_fingerprint=created.workspace_fingerprint,
    )
    receipts = _materialization_receipts(context, proposal)
    changed_receipts = (
        replace(
            receipts[0],
            local_prompt_fingerprint=fingerprint_prompt_text("changed private prompt"),
            receipt_fingerprint=None,
        ),
        *receipts[1:],
    )

    with pytest.raises(ProductionImportError, match="production_import_receipt_mismatch"):
        build_production_automatic_plan_authority(
            workspace,
            context,
            proposal,
            changed_receipts,
        )


def test_private_automatic_plan_authority_enforces_aggregate_wire_limit() -> None:
    context, _admission, proposal = _authorities(
        hard_constraints=tuple(f"{index}" + "x" * 7_999 for index in range(7))
    )
    _sidebar, registry, created = _production(context)
    workspace = registry.claim_workspace_authority(
        created.workspace_handle,
        expected_workspace_revision=created.workspace_revision,
        expected_workspace_fingerprint=created.workspace_fingerprint,
    )
    receipts = _materialization_receipts(context, proposal)

    with pytest.raises(ProductionImportError, match="production_plan_wire_limit"):
        build_production_automatic_plan_authority(
            workspace,
            context,
            proposal,
            receipts,
        )


def test_mutation_claim_prevents_a_manual_edit_from_racing_the_import_commit() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    entered = Event()
    resume = Event()
    materializer = _Materializer(context)

    def blocked_materializer(
        inner_context: ProductionPlanningContextV2,
        inner_proposal: SegmentationProposalV2,
        segment: ProposalSegmentV1,
    ) -> SegmentContextMaterializationClaim:
        claim = materializer(inner_context, inner_proposal, segment)
        if materializer.calls == 1:
            entered.set()
            assert resume.wait(timeout=2)
        return claim

    outcomes: list[object] = []

    def execute() -> None:
        outcomes.append(
            registry.import_automatic_plan(
                _request(created, context, proposal),
                planning_context=context,
                proposal=proposal,
                materialize=blocked_materializer,
            )
        )

    worker = Thread(target=execute)
    worker.start()
    assert entered.wait(timeout=2)
    with pytest.raises(ProductionWorkbenchError, match="workspace_busy"):
        registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "production.manual.race",
                "action": "set_selection",
                "payload": {
                    "workspace_handle": created.workspace_handle,
                    "expected_workspace_revision": created.workspace_revision,
                    "expected_workspace_fingerprint": created.workspace_fingerprint,
                    "segment_ids": [],
                },
            }
        )
    resume.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcomes) == 1


def test_import_rechecks_global_ledger_capacity_after_materialization() -> None:
    context, _admission, proposal = _authorities()
    sidebar, registry, created = _production(context, max_ledger_entries=2)
    entered = Event()
    resume = Event()
    materializer = _Materializer(context)

    def blocked_materializer(
        inner_context: ProductionPlanningContextV2,
        inner_proposal: SegmentationProposalV2,
        segment: ProposalSegmentV1,
    ) -> SegmentContextMaterializationClaim:
        claim = materializer(inner_context, inner_proposal, segment)
        if materializer.calls == 1:
            entered.set()
            assert resume.wait(timeout=2)
        return claim

    outcomes: list[ProductionWorkbenchError] = []

    def execute() -> None:
        try:
            registry.import_automatic_plan(
                _request(created, context, proposal),
                planning_context=context,
                proposal=proposal,
                materialize=blocked_materializer,
            )
        except ProductionWorkbenchError as exc:
            outcomes.append(exc)

    worker = Thread(target=execute)
    worker.start()
    assert entered.wait(timeout=2)
    competing_source = _report(
        context.global_task_mode,
        duration_seconds=10,
        user_intent="Competing workspace fills the final ledger slot.",
        assets=context.assets,
    )
    competing_sidebar = sidebar.publish(
        competing_source,
        build_native_h3_wiring(competing_source),
        ExecutionCorrelation("prompt.competing", "node.competing"),
    )
    competing_created = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.create.capacity.race",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": competing_sidebar.workspace_id},
        }
    ).projection
    assert competing_created is not None
    resume.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert outcomes[0].code == "request_capacity"
    assert materializer.active == 0
    reread = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.read.capacity.race",
            "action": "read_projection",
            "payload": {"workspace_handle": created.workspace_handle},
        }
    ).projection
    assert reread == created


def test_manual_revision_invalidates_automatic_plan_and_supersedes_replay() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    request = _request(created, context, proposal)
    imported = registry.import_automatic_plan(
        request,
        planning_context=context,
        proposal=proposal,
        materialize=_Materializer(context),
    )
    revised = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.manual.after.import",
            "action": "set_selection",
            "payload": {
                "workspace_handle": created.workspace_handle,
                "expected_workspace_revision": imported.projection.workspace_revision,
                "expected_workspace_fingerprint": imported.projection.workspace_fingerprint,
                "segment_ids": [imported.projection.segment_ids[0]],
            },
        }
    ).projection
    assert isinstance(revised, ProductionWorkbenchProjection)

    with pytest.raises(ProductionWorkbenchError, match="automatic_plan_unavailable"):
        registry.claim_automatic_plan_authority(
            created.workspace_handle,
            expected_workspace_revision=revised.workspace_revision,
            expected_workspace_fingerprint=revised.workspace_fingerprint,
            expected_plan_fingerprint=imported.projection.plan_fingerprint,
        )
    with pytest.raises(ProductionWorkbenchError, match="replay_superseded"):
        registry.import_automatic_plan(
            request,
            planning_context=context,
            proposal=proposal,
            materialize=_Materializer(context),
        )


def test_public_production_action_contract_does_not_mount_internal_import() -> None:
    context, _admission, _proposal = _authorities()
    _sidebar, registry, _created = _production(context)
    with pytest.raises(ValueError, match="production action is unsupported"):
        registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "production.public.import.rejected",
                "action": "import_automatic_plan",
                "payload": {},
            }
        )


def test_closed_request_rejects_unknown_members_and_recomputed_outer_fingerprint() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, _registry, created = _production(context)
    request = _request(created, context, proposal)
    wire = request.to_wire()
    assert ProductionImportRequestV1.from_wire(wire) == request
    wire["unexpected"] = True
    with pytest.raises(ValueError, match="production_import_request_wire"):
        ProductionImportRequestV1.from_wire(wire)


class _M26Clock:
    def __init__(self) -> None:
        self.value = 2_000

    def __call__(self) -> int:
        self.value += 1
        return self.value


def _m26_ready_sequence(
    authority: ProductionAutomaticPlanAuthorityV2,
    *,
    artifact_store: PrivateSegmentArtifactStore | None = None,
) -> tuple[GenerationSequenceProjection, tuple[SegmentArtifactReceipt, ...]]:
    workspace = authority.workspace
    manifests = derive_segment_manifests(workspace)
    previous_segments = tuple(
        replace(
            item,
            producer_settings_fingerprint=canonical_fingerprint(
                {"schema": "test.m26.previous_settings.v1", "segment_id": item.segment_id}
            ),
        )
        for item in workspace.segments
    )
    previous = create_workspace(
        workspace.workspace_id,
        previous_segments,
        accepted_intent_authorities=tuple(
            AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
            for item in previous_segments
        ),
        selected_segment_ids=workspace.selected_segment_ids,
    )
    recompute = plan_recompute(derive_segment_manifests(previous), manifests)
    jobs = tuple(
        GenerationJobSpec(
            segment_id=manifest.segment_id,
            job_id=f"job.m26.{index}",
            graph_fingerprint=canonical_fingerprint(
                {"schema": "test.m26.graph.v1", "segment_id": manifest.segment_id}
            ),
            compiled_prompt_fingerprint=canonical_fingerprint(
                {"schema": "test.m26.prompt.v1", "segment_id": manifest.segment_id}
            ),
            model_fingerprint=canonical_fingerprint({"schema": "test.m26.model.v1"}),
            runtime_fingerprint=canonical_fingerprint({"schema": "test.m26.runtime.v1"}),
            expected_format="mp4",
            expected_shape=(manifest.duration.frame_count, 512, 512, 3),
            timeout_ms=60_000,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )
        for index, manifest in enumerate(manifests, start=1)
    )
    plan = build_generation_sequence_plan(
        workspace,
        manifests,
        recompute,
        jobs,
        sequence_id="sequence.m26.production",
    )
    state = create_generation_sequence_state(plan)
    receipts: list[SegmentArtifactReceipt] = []
    outputs_by_segment: dict[str, str] = {}
    manifest_by_segment = {item.segment_id: item for item in manifests}
    for index, job in enumerate(jobs, start=1):
        state = record_generation_projection(
            state,
            job.job_id,
            transaction_id=f"transaction.m26.{index}",
            graph_fingerprint=job.graph_fingerprint,
            compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )
        state = record_generation_submission(
            state,
            job.job_id,
            queue_prompt_id=f"prompt.m26.{index}",
        )
        state = record_generation_running(
            state,
            job.job_id,
            host_owner_id="host.m26.test",
        )
        transaction = state.runtime_for(job.job_id).transaction
        assert transaction is not None
        manifest = manifest_by_segment[job.segment_id]
        predecessor = (
            None
            if not manifest.dependency_segment_ids
            else outputs_by_segment[manifest.dependency_segment_ids[-1]]
        )
        partial = begin_segment_artifact_receipt(
            manifest=manifest,
            transaction=transaction,
            artifact_id=f"artifact.m26.{index}",
            model_fingerprint=job.model_fingerprint,
            runtime_fingerprint=job.runtime_fingerprint,
            execution_fingerprint=canonical_fingerprint(
                {"schema": "test.m26.execution.v1", "segment_id": job.segment_id}
            ),
            predecessor_artifact_fingerprint=predecessor,
            format_label=job.expected_format,
            shape=job.expected_shape,
            created_at_ms=10_000 + index if artifact_store is None else 1_900,
            expires_at_ms=600_000,
        )
        receipt = (
            complete_segment_artifact_receipt(
                partial,
                output_fingerprint=canonical_fingerprint(
                    {"schema": "test.m26.output.v1", "segment_id": job.segment_id}
                ),
                byte_length=8_192 + index,
            )
            if artifact_store is None
            else artifact_store.commit(
                artifact_store.begin(partial), f"bounded-m26-original-{index}".encode()
            )
        )
        state = record_generation_success(
            state,
            job.job_id,
            result_fingerprint=canonical_fingerprint(
                {"schema": "test.m26.result.v1", "segment_id": job.segment_id}
            ),
            receipt=receipt,
        )
        receipts.append(receipt)
        assert receipt.output_fingerprint is not None
        outputs_by_segment[job.segment_id] = receipt.output_fingerprint
    return (
        build_generation_sequence_projection(
            state,
            ExecutionCorrelation("prompt.m26.sequence", "node.m26.sequence"),
        ),
        tuple(receipts),
    )


def _m26_subject(
    policy: SegmentationPolicyV1,
    *,
    artifact_store: PrivateSegmentArtifactStore | None = None,
    parts: tuple[int, ...] | None = None,
) -> tuple[
    ProductionPlanningContextV2,
    SegmentationProposalV2,
    ProductionWorkspaceRegistry,
    ProductionWorkbenchProjection,
    ProductionAutomaticPlanAuthorityV2,
    GenerationSequenceProjection,
    tuple[SegmentArtifactReceipt, ...],
]:
    context, _admission, proposal = _authorities(
        policy=policy,
        qualification=ManagedExecutionQualificationV1.QUALIFIED,
        parts=parts,
    )
    _sidebar, registry, created = _production(context)
    imported = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=_Materializer(context),
    )
    authority = registry.claim_automatic_plan_authority(
        created.workspace_handle,
        expected_workspace_revision=imported.projection.workspace_revision,
        expected_workspace_fingerprint=imported.projection.workspace_fingerprint,
        expected_plan_fingerprint=imported.projection.plan_fingerprint,
    )
    assert isinstance(authority, ProductionAutomaticPlanAuthorityV2)
    workbench = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": f"production.read.m26.{policy.value}",
            "action": "read_projection",
            "payload": {"workspace_handle": created.workspace_handle},
        }
    ).projection
    assert isinstance(workbench, ProductionWorkbenchProjection)
    sequence, receipts = _m26_ready_sequence(authority, artifact_store=artifact_store)
    return (
        context,
        proposal,
        registry,
        workbench,
        authority,
        sequence,
        receipts,
    )


def _rational(value: Fraction) -> AVRational:
    return AVRational(value.numerator, value.denominator)


def _m26_video(frames: int) -> AVVideoDescriptor:
    return AVVideoDescriptor(
        codec="h264",
        width=512,
        height=512,
        pixel_format="yuv420p",
        color_range="tv",
        color_space="bt709",
        color_primaries="bt709",
        color_transfer="bt709",
        rotation_degrees=0,
        frame_rate=AVRational(24, 1),
        time_base=AVRational(1, 24),
        start_time=AVRational(0, 1),
        end_time=_rational(Fraction(frames, 24)),
        decoded_frame_count=frames,
    )


def _m26_audio(samples: int) -> AVAudioDescriptor:
    return AVAudioDescriptor(
        codec="aac",
        sample_format="fltp",
        sample_rate=32_000,
        channels=2,
        channel_layout="stereo",
        time_base=AVRational(1, 32_000),
        start_time=AVRational(0, 1),
        end_time=_rational(Fraction(samples, 32_000)),
        decoded_sample_count=samples,
    )


class _M26OriginalSource:
    def __init__(self, receipt: SegmentArtifactReceipt) -> None:
        self.receipt = receipt

    @property
    def byte_length(self) -> int:
        return self.receipt.byte_length

    @property
    def content_fingerprint(self) -> str:
        return cast(str, self.receipt.output_fingerprint)

    def stream_into(
        self,
        destination: Path,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[int, str]:
        del destination, cancellation
        raise AssertionError("the injected media bridge must own the original stream")


class _M26DerivedLease:
    def __init__(
        self,
        *,
        original_descriptor: AVMediaDescriptor,
        contribution_frames: int,
        contribution_samples: int,
        mode: str,
    ) -> None:
        self.original_descriptor = original_descriptor
        self.byte_length = 4_096
        self.content_fingerprint = canonical_fingerprint(
            {
                "schema": "test.m26.derived_content.v1",
                "segment_id": original_descriptor.segment_id,
            }
        )
        self.contribution_frames = contribution_frames
        self.contribution_samples = contribution_samples
        self.mode = mode
        self.released = False

    @property
    def source(self) -> AVSegmentSource:
        return self

    def stream_into(
        self,
        destination: Path,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[int, str]:
        del destination, cancellation
        raise AssertionError("the injected low-level executor must not read media")

    def inspect(
        self,
        *,
        segment_id: str,
        derived_receipt_fingerprint: str,
        cancellation: CancellationProbe | None = None,
    ) -> AVMediaDescriptor:
        del cancellation
        frames = self.contribution_frames - (1 if self.mode == "short_video" else 0)
        audio = None if self.mode == "missing_audio" else _m26_audio(self.contribution_samples)
        return AVMediaDescriptor(
            segment_id=segment_id,
            artifact_receipt_fingerprint=(
                canonical_fingerprint({"schema": "test.m26.wrong_derived_receipt.v1"})
                if self.mode == "wrong_identity"
                else derived_receipt_fingerprint
            ),
            artifact_output_fingerprint=self.content_fingerprint,
            artifact_byte_length=self.byte_length,
            capability_fingerprint=qualified_ffmpeg_capability().fingerprint,
            container="mp4",
            stream_count=1 if audio is None else 2,
            video=_m26_video(frames),
            audio=audio,
            subtitle_stream_count=0,
            data_stream_count=0,
            attachment_stream_count=0,
            inspected_at_ms=1_500,
            expires_at_ms=61_500,
            warning_codes=(),
        )

    def release(self) -> None:
        self.released = True


class _M26MediaBridge:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.leases: list[_M26DerivedLease] = []

    def derive_m26_input(
        self,
        *,
        segment_id: str,
        original_receipt_fingerprint: str,
        original_source: AVSegmentSource,
        contribution_frames: int,
        contribution_samples: int,
        cancellation: CancellationProbe | None = None,
    ) -> _M26DerivedLease:
        del segment_id, original_receipt_fingerprint, cancellation
        if not isinstance(original_source, _M26OriginalSource):
            raise AssertionError("unexpected original source")
        receipt = original_source.receipt
        original = AVMediaDescriptor(
            segment_id=receipt.segment_id,
            artifact_receipt_fingerprint=receipt.fingerprint,
            artifact_output_fingerprint=cast(str, receipt.output_fingerprint),
            artifact_byte_length=receipt.byte_length,
            capability_fingerprint=qualified_ffmpeg_capability().fingerprint,
            container="mp4",
            stream_count=2,
            video=_m26_video(receipt.shape[0]),
            audio=_m26_audio(contribution_samples + 320),
            subtitle_stream_count=0,
            data_stream_count=0,
            attachment_stream_count=0,
            inspected_at_ms=1_000,
            expires_at_ms=61_000,
            warning_codes=(),
        )
        lease = _M26DerivedLease(
            original_descriptor=original,
            contribution_frames=contribution_frames,
            contribution_samples=contribution_samples,
            mode=self.mode,
        )
        self.leases.append(lease)
        return lease


def _m26_runtime(
    tmp_path: Path,
    artifact_store: PrivateSegmentArtifactStore,
    *,
    bridge_mode: str = "ok",
) -> tuple[M26ProductionAssemblyRuntime, _M26MediaBridge]:
    bridge = _M26MediaBridge(bridge_mode)
    reconstruction_store = PrivateAVReconstructionStore(
        tmp_path / "reconstruction",
        policy=AVStorePolicy(
            max_member_bytes=64 << 20,
            max_total_bytes=256 << 20,
            max_transactions=8,
            max_members_per_transaction=65,
            max_recovery_entries=1_024,
            max_concurrent_writes=1,
            transaction_ttl_ms=600_000,
        ),
        clock_ms=lambda: 2_000,
    )
    runtime = M26ProductionAssemblyRuntime(
        artifact_store=artifact_store,
        reconstruction_store=reconstruction_store,
        media_bridge=bridge,
        low_level_adapter=object.__new__(QualifiedAVMediaAdapter),
        capability=build_production_assembly_capability(
            store_identity_fingerprint=canonical_fingerprint(
                {"schema": "test.m26.private_store.v1"}
            )
        ),
    )
    return runtime, bridge


def _m26_successful_low_level_executor(**kwargs: object) -> AVReconstructionReceipt:
    plan = cast(AVReconstructionPlan, kwargs["plan"])
    approval = cast(AVReconstructionApproval, kwargs["approval"])
    clock_ms = cast(Callable[[], int], kwargs["clock_ms"])
    transaction_id = cast(str, kwargs["transaction_id"])
    results = tuple(
        AVReceiptSegmentResult(
            segment_id=item.segment_id,
            artifact_receipt_fingerprint=item.artifact_receipt_fingerprint,
            operations=item.operations,
            accounting=item.accounting,
            output_start=item.output_start,
            output_end=item.output_end,
        )
        for item in plan.segments
    )
    return AVReconstructionReceipt(
        transaction_id=transaction_id,
        plan_fingerprint=plan.fingerprint,
        approval_fingerprint=approval.fingerprint,
        selective_plan_fingerprint=plan.selective_plan_fingerprint,
        generation_state_fingerprint=plan.generation_state_fingerprint,
        artifact_receipt_fingerprints=plan.artifact_receipt_fingerprints,
        boundary_receipt_fingerprints=plan.boundary_receipt_fingerprints,
        capability_fingerprint=plan.capability.fingerprint,
        execution_fingerprint=canonical_fingerprint(
            {"schema": "test.m26.low_level_execution.v1", "transaction_id": transaction_id}
        ),
        segment_results=results,
        outputs=(
            AVReceiptOutput(
                handle="m26_full_output_0001",
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                byte_length=32_768,
                content_fingerprint=canonical_fingerprint(
                    {"schema": "test.m26.final_output.v1", "transaction_id": transaction_id}
                ),
                video_frame_count=sum(item.accounting.emitted_frames for item in plan.segments),
                audio_sample_count=sum(item.accounting.emitted_samples for item in plan.segments),
                duration=plan.segments[-1].output_end,
            ),
        ),
        publication_state=AVPublicationState.COMPLETE,
        completed_at_ms=clock_ms(),
    )


def _m26_execute(
    *,
    runtime: M26ProductionAssemblyRuntime,
    authority: ProductionAutomaticPlanAuthorityV2,
    sequence: GenerationSequenceProjection,
    receipts: tuple[SegmentArtifactReceipt, ...],
    clock: _M26Clock,
    executor: Callable[..., AVReconstructionReceipt] = _m26_successful_low_level_executor,
) -> M26ProductionAssemblyExecution:
    authorization = mint_m26_assembly_authorization(
        authorization_id="m26auth.test.execution",
        automatic_plan=authority,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        cut_boundary_receipts=authority.cut_boundary_receipts,
        capability=runtime.capability,
        issued_at_ms=1_900,
        expires_at_ms=60_000,
    )
    return execute_m26_production_assembly(
        runtime=runtime,
        transaction_id="m26tx.test.execution",
        authorization=authorization,
        automatic_plan=authority,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        cut_boundary_receipts=authority.cut_boundary_receipts,
        clock_ms=clock,
        low_level_executor=executor,
        source_factory=_M26OriginalSource,
    )


@pytest.mark.parametrize(
    ("policy", "sampled_total_milliseconds"),
    [
        (SegmentationPolicyV1.FIXED_5, 62_004),
        (SegmentationPolicyV1.FIXED_10, 60_750),
        (SegmentationPolicyV1.FIXED_12, 61_250),
        (SegmentationPolicyV1.FIXED_15, 60_332),
    ],
)
def test_m26_assembly_crops_all_presets_to_exact_source_and_legacy_output(
    policy: SegmentationPolicyV1,
    sampled_total_milliseconds: int,
    tmp_path: Path,
) -> None:
    _context, proposal, _registry, _projection, authority, sequence, receipts = _m26_subject(policy)
    artifact_store = PrivateSegmentArtifactStore(
        tmp_path / "artifacts",
        clock_ms=lambda: 2_000,
    )
    runtime, bridge = _m26_runtime(tmp_path, artifact_store)
    execution = _m26_execute(
        runtime=runtime,
        authority=authority,
        sequence=sequence,
        receipts=receipts,
        clock=_M26Clock(),
    )

    assembly = execution.assembly_receipt
    assert sum(item.duration.delivered_milliseconds for item in proposal.segments) == (
        sampled_total_milliseconds
    )
    assert (assembly.source_total_frames, assembly.source_total_samples) == (1_440, 1_920_000)
    assert (assembly.output_total_frames, assembly.output_total_samples) == (1_800, 2_880_000)
    assert (
        sum(
            item.normalization_generated_frames
            for item in execution.outer_plan.source_contributions
        )
        == 360
    )
    assert (
        sum(
            item.normalization_inserted_samples
            for item in execution.outer_plan.source_contributions
        )
        == 960_000
    )
    assert tuple(
        item.original_decoded_frame_count for item in execution.outer_plan.derived_input_receipts
    ) == tuple(item.shape[0] for item in receipts)
    assert all(
        item.operations == (AVOperation.NORMALIZE_VIDEO.value, AVOperation.NORMALIZE_AUDIO.value)
        and item.join_policy == "cut"
        and item.continuity_prefix_frames == 0
        and item.continuity_prefix_samples == 0
        and item.burn_in_frames == 0
        and item.overlap_frames == 0
        for item in execution.outer_plan.source_contributions
    )
    assert all(lease.released for lease in bridge.leases)
    assert len(bridge.leases) == len(receipts)
    assert decode_production_assembly_receipt(assembly.to_wire_bytes()) == assembly
    portable = assembly.to_wire_bytes().lower()
    assert str(tmp_path).encode("utf-8").lower() not in portable
    assert all(
        token not in portable for token in (b'"path"', b'"url"', b'"locator"', b'"approval"')
    )

    with pytest.raises(AVReconstructionError, match="plan_factory_required"):
        replace(
            execution.embedded_plan,
            planned_at_ms=execution.embedded_plan.planned_at_ms + 1,
            plan_fingerprint=None,
        )
    tampered = assembly.to_wire()
    tampered["completed_at_ms"] = assembly.completed_at_ms + 1
    with pytest.raises(M26AssemblyError, match="assembly_receipt_fingerprint_mismatch"):
        decode_production_assembly_receipt(canonical_bytes(tampered))
    with pytest.raises(M26AssemblyError, match="assembly_receipt_not_canonical"):
        decode_production_assembly_receipt(b" " + assembly.to_wire_bytes())
    forged_reconstruction = replace(
        execution.reconstruction_receipt,
        boundary_receipt_fingerprints=tuple(
            canonical_fingerprint({"schema": "test.m26.forged_boundary.v1", "index": index})
            for index in range(len(execution.reconstruction_receipt.boundary_receipt_fingerprints))
        ),
        receipt_fingerprint=None,
    )
    with pytest.raises(M26AssemblyError, match="assembly_receipt_reconstruction_mismatch"):
        build_production_assembly_receipt(
            transaction_id=assembly.transaction_id,
            outer_plan=execution.outer_plan,
            reconstruction_receipt=forged_reconstruction,
            completed_at_ms=assembly.completed_at_ms,
        )


def _m26_receipt_at(completed_at_ms: int) -> ProductionAssemblyReceiptV1:
    fingerprint = canonical_fingerprint({"schema": "test.m26.portable_receipt.v1"})
    return ProductionAssemblyReceiptV1(
        transaction_id="m26tx.portable.receipt",
        workspace_id="m26.portable.workspace",
        workspace_revision=1,
        workspace_fingerprint=fingerprint,
        authorization_fingerprint=fingerprint,
        original_artifact_receipt_fingerprints=(fingerprint,),
        derived_input_receipt_fingerprints=(fingerprint,),
        source_contribution_fingerprints=(fingerprint,),
        outer_plan_fingerprint=fingerprint,
        embedded_plan_fingerprint=fingerprint,
        embedded_approval_fingerprint=fingerprint,
        reconstruction_receipt_fingerprint=fingerprint,
        source_total_frames=360,
        source_total_samples=480_000,
        output_total_frames=450,
        output_total_samples=720_000,
        completed_at_ms=completed_at_ms,
    )


@pytest.mark.parametrize(
    "completed_at_ms", [1, 1_000_000_000, 1_000_000_001, 1_789_270_800_000, 9_999_999_999_999]
)
def test_m26_receipt_preserves_epoch_timestamp_without_changing_media_totals(
    completed_at_ms: int,
) -> None:
    receipt = _m26_receipt_at(completed_at_ms)
    decoded = decode_production_assembly_receipt(receipt.to_wire_bytes())
    assert decoded == receipt
    assert decoded.completed_at_ms == completed_at_ms
    assert (decoded.source_total_frames, decoded.source_total_samples) == (360, 480_000)
    assert (decoded.output_total_frames, decoded.output_total_samples) == (450, 720_000)
    assert decoded.fingerprint != _m26_receipt_at(2_000).fingerprint


@pytest.mark.parametrize("invalid", [0, -1, True, 2_000.0, "2000", None, 10_000_000_000_000])
def test_m26_receipt_timestamp_refuses_invalid_values_at_both_boundaries(invalid: object) -> None:
    receipt = _m26_receipt_at(2_000)
    with pytest.raises(M26AssemblyError, match="positive_integer:assembly_receipt.completed_at"):
        replace(receipt, completed_at_ms=cast(int, invalid), receipt_fingerprint=None)
    wire = receipt.to_wire()
    wire["completed_at_ms"] = invalid
    with pytest.raises(M26AssemblyError, match="positive_integer:assembly_receipt.completed_at"):
        decode_production_assembly_receipt(json.dumps(wire))


@pytest.mark.parametrize(
    ("field_name", "error_name"),
    [
        ("source_total_frames", "source_frames"),
        ("source_total_samples", "source_samples"),
        ("output_total_frames", "output_frames"),
        ("output_total_samples", "output_samples"),
    ],
)
def test_m26_receipt_media_count_ceiling_remains_independent(
    field_name: str, error_name: str
) -> None:
    receipt = _m26_receipt_at(2_000)
    expected = f"positive_integer:assembly_receipt.{error_name}"
    with pytest.raises(M26AssemblyError, match=expected):
        replace(receipt, **{field_name: 1_000_000_001, "receipt_fingerprint": None})  # type: ignore[arg-type]
    wire = receipt.to_wire()
    wire[field_name] = 1_000_000_001
    with pytest.raises(M26AssemblyError, match=expected):
        decode_production_assembly_receipt(canonical_bytes(wire))


@pytest.mark.parametrize("bridge_mode", ["missing_audio", "short_video", "wrong_identity"])
def test_m26_assembly_rejects_derived_descriptor_tamper_and_releases_owned_lease(
    bridge_mode: str,
    tmp_path: Path,
) -> None:
    _context, _proposal, _registry, _projection, authority, sequence, receipts = _m26_subject(
        SegmentationPolicyV1.FIXED_15
    )
    artifact_store = PrivateSegmentArtifactStore(
        tmp_path / "artifacts",
        clock_ms=lambda: 2_000,
    )
    runtime, bridge = _m26_runtime(tmp_path, artifact_store, bridge_mode=bridge_mode)
    low_level_calls = 0

    def forbidden_executor(**_kwargs: object) -> AVReconstructionReceipt:
        nonlocal low_level_calls
        low_level_calls += 1
        raise AssertionError("low-level executor must not receive a tampered derived descriptor")

    with pytest.raises(M26ProductionAssemblyError, match="assembly_derived_descriptor_mismatch"):
        _m26_execute(
            runtime=runtime,
            authority=authority,
            sequence=sequence,
            receipts=receipts,
            clock=_M26Clock(),
            executor=forbidden_executor,
        )
    assert low_level_calls == 0
    assert bridge.leases
    assert all(lease.released for lease in bridge.leases)


def test_m26_assembly_rejects_undeclared_low_level_operation_and_cleans_all_leases(
    tmp_path: Path,
) -> None:
    _context, _proposal, _registry, _projection, authority, sequence, receipts = _m26_subject(
        SegmentationPolicyV1.FIXED_15
    )
    artifact_store = PrivateSegmentArtifactStore(
        tmp_path / "artifacts",
        clock_ms=lambda: 2_000,
    )
    runtime, bridge = _m26_runtime(tmp_path, artifact_store)

    def forged_executor(**kwargs: object) -> AVReconstructionReceipt:
        receipt = _m26_successful_low_level_executor(**kwargs)
        forged = replace(
            receipt.segment_results[0],
            operations=(AVOperation.INSERT_SILENCE,),
        )
        return replace(
            receipt,
            segment_results=(forged, *receipt.segment_results[1:]),
            receipt_fingerprint=None,
        )

    with pytest.raises(M26ProductionAssemblyError, match="assembly_execution_failed"):
        _m26_execute(
            runtime=runtime,
            authority=authority,
            sequence=sequence,
            receipts=receipts,
            clock=_M26Clock(),
            executor=forged_executor,
        )
    assert len(bridge.leases) == len(receipts)
    assert all(lease.released for lease in bridge.leases)


def _m26_assemble_action(
    projection: ProductionWorkbenchProjection,
    request_id: str,
) -> dict[str, object]:
    assembly = projection.assembly
    return {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": request_id,
        "action": "assemble_sequence",
        "payload": {
            "workspace_handle": projection.workspace_handle,
            "workspace_id": projection.workspace_id,
            "expected_workspace_revision": projection.workspace_revision,
            "expected_workspace_fingerprint": projection.workspace_fingerprint,
            "managed_sequence_fingerprint": assembly.managed_sequence_fingerprint,
            "artifact_receipt_fingerprints": list(assembly.artifact_receipt_fingerprints),
            "cut_boundary_receipt_fingerprints": list(assembly.cut_boundary_receipt_fingerprints),
            "assembly_capability_fingerprint": assembly.capability_fingerprint,
            "output_profile_id": assembly.output_profile_id,
        },
    }


def _m26_cancel_or_retry_action(
    projection: ProductionWorkbenchProjection,
    request_id: str,
    action: str,
) -> dict[str, object]:
    return {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": request_id,
        "action": action,
        "payload": {
            "workspace_handle": projection.workspace_handle,
            "workspace_id": projection.workspace_id,
            "expected_workspace_revision": projection.workspace_revision,
            "expected_workspace_fingerprint": projection.workspace_fingerprint,
            "expected_assembly_fingerprint": projection.assembly.fingerprint,
        },
    }


def test_m26_production_action_is_closed_idempotent_cas_bound_and_cancel_retry_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context, _proposal, registry, imported, _authority, sequence, receipts = _m26_subject(
        SegmentationPolicyV1.FIXED_15
    )
    callbacks: list[Callable[[], None]] = []
    artifact_store = PrivateSegmentArtifactStore(
        tmp_path / "artifacts",
        clock_ms=lambda: 2_000,
    )

    def runtime_provider(store: PrivateSegmentArtifactStore) -> M26ProductionAssemblyRuntime:
        runtime, _bridge = _m26_runtime(tmp_path, store)
        return runtime

    registry._assembly_runtime_provider = runtime_provider
    registry._assembly_submitter = callbacks.append
    ready = registry.replace_generation_authority(
        imported.workspace_handle,
        expected_workspace_revision=imported.workspace_revision,
        expected_workspace_fingerprint=imported.workspace_fingerprint,
        expected_sequence_state_fingerprint=None,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        artifact_store=artifact_store,
    )
    assert ready.assembly.failure_code is None
    assert ready.assembly.state == "unavailable"
    assert "assemble_sequence" in ready.allowed_actions
    assert callbacks == []

    unsafe = _m26_assemble_action(ready, "request.m26.unsafe")
    cast(dict[str, object], unsafe["payload"])["path"] = "private/output.mp4"
    with pytest.raises(ValueError, match="payload is not closed"):
        registry.dispatch(unsafe)
    assert callbacks == []

    action = _m26_assemble_action(ready, "request.m26.assemble")
    planned = registry.dispatch(action).projection
    assert isinstance(planned, ProductionWorkbenchProjection)
    assert planned.assembly.state == "planned"
    assert len(callbacks) == 1
    replay = registry.dispatch(action).projection
    assert replay == planned
    assert len(callbacks) == 1

    conflict = _m26_assemble_action(ready, "request.m26.assemble")
    cast(dict[str, object], conflict["payload"])["assembly_capability_fingerprint"] = (
        canonical_fingerprint({"schema": "test.m26.conflicting_capability.v1"})
    )
    with pytest.raises(ProductionWorkbenchError, match="request_id_conflict"):
        registry.dispatch(conflict)
    stale = _m26_assemble_action(ready, "request.m26.stale")
    cast(dict[str, object], stale["payload"])["expected_workspace_revision"] = (
        ready.workspace_revision - 1
    )
    with pytest.raises(ProductionWorkbenchError, match="stale_workspace"):
        registry.dispatch(stale)
    assert len(callbacks) == 1

    cancelled = registry.dispatch(
        _m26_cancel_or_retry_action(planned, "request.m26.cancel", "cancel_assembly")
    ).projection
    assert isinstance(cancelled, ProductionWorkbenchProjection)
    assert cancelled.assembly.state == "cancelling"
    callbacks.pop(0)()
    failed = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.m26.read.cancelled",
            "action": "read_projection",
            "payload": {"workspace_handle": ready.workspace_handle},
        }
    ).projection
    assert isinstance(failed, ProductionWorkbenchProjection)
    assert (failed.assembly.state, failed.assembly.failure_code) == ("failed", "cancelled")

    def fail_execution(**_kwargs: object) -> M26ProductionAssemblyExecution:
        raise M26ProductionAssemblyError("synthetic_execution_failure")

    monkeypatch.setattr(
        production_workspace_module,
        "execute_m26_production_assembly",
        fail_execution,
    )
    retried = registry.dispatch(
        _m26_cancel_or_retry_action(failed, "request.m26.retry", "retry_assembly")
    ).projection
    assert isinstance(retried, ProductionWorkbenchProjection)
    assert retried.assembly.state == "planned"
    assert len(callbacks) == 1
    callbacks.pop(0)()
    retry_failed = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.m26.read.retry_failed",
            "action": "read_projection",
            "payload": {"workspace_handle": ready.workspace_handle},
        }
    ).projection
    assert isinstance(retry_failed, ProductionWorkbenchProjection)
    assert (retry_failed.assembly.state, retry_failed.assembly.failure_code) == (
        "failed",
        "synthetic_execution_failure",
    )


def _m26_store_backed_low_level_executor(**kwargs: object) -> AVReconstructionReceipt:
    """Persist controlled output bytes; codec execution is qualified in the separate runtime row."""

    store = cast(PrivateAVReconstructionStore, kwargs["store"])
    clock = cast(Callable[[], int], kwargs["clock_ms"])
    receipt = _m26_successful_low_level_executor(**kwargs)
    transaction = store.begin(
        transaction_id=receipt.transaction_id,
        plan_fingerprint=receipt.plan_fingerprint,
        approval_fingerprint=receipt.approval_fingerprint,
        expires_at_ms=60_000,
    )
    body = b"bounded-m26-reconstruction-output"
    output = replace(
        receipt.outputs[0],
        byte_length=len(body),
        content_fingerprint="sha256:" + sha256(body).hexdigest(),
    )
    lease = store.allocate_output(transaction, handle=output.handle, kind=output.kind)
    lease.path.write_bytes(body)
    committed = replace(
        receipt, outputs=(output,), completed_at_ms=clock(), receipt_fingerprint=None
    )
    result = store.commit(transaction, committed, output_leases=((output.handle, lease),))
    assert isinstance(result, AVReconstructionReceipt)
    return result


def _m26_store_backed_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    original_previews: bool = True,
    target_seconds: int = 30,
) -> tuple[
    ProductionWorkspaceRegistry,
    ProductionWorkbenchProjection,
    list[Callable[[], None]],
    tuple[SegmentArtifactReceipt, ...],
]:
    from comfyui_h3_context.adapters import comfyui_media_preview

    clock = _M26Clock()
    store = PrivateSegmentArtifactStore(tmp_path / "artifacts", clock_ms=clock)
    _context, _proposal, registry, imported, _authority, sequence, receipts = _m26_subject(
        SegmentationPolicyV1.FIXED_15,
        artifact_store=store,
        parts=(15,) * (target_seconds // 15),
    )
    callbacks: list[Callable[[], None]] = []

    def runtime_provider(artifacts: PrivateSegmentArtifactStore) -> M26ProductionAssemblyRuntime:
        runtime, _bridge = _m26_runtime(tmp_path, artifacts)
        runtime.reconstruction_store._clock_ms = clock
        return runtime

    def controlled_execution(**kwargs: object) -> M26ProductionAssemblyExecution:
        try:
            return execute_m26_production_assembly(
                **kwargs,  # type: ignore[arg-type]
                low_level_executor=_m26_store_backed_low_level_executor,
                source_factory=_M26OriginalSource,
            )
        except Exception as exc:
            pytest.fail(f"controlled assembly fixture failed before publication: {exc}")

    registry._clock_ms = clock
    registry._assembly_runtime_provider = runtime_provider
    registry._assembly_submitter = callbacks.append
    monkeypatch.setattr(
        production_workspace_module, "execute_m26_production_assembly", controlled_execution
    )
    monkeypatch.setattr(
        comfyui_media_preview, "ensure_media_preview_route_registered", lambda: original_previews
    )
    ready = registry.replace_generation_authority(
        imported.workspace_handle,
        expected_workspace_revision=imported.workspace_revision,
        expected_workspace_fingerprint=imported.workspace_fingerprint,
        expected_sequence_state_fingerprint=None,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        artifact_store=store,
    )
    assert len(ready.outputs) == len(receipts)
    assert all(output.preview is original_previews for output in ready.outputs)
    monkeypatch.setattr(
        comfyui_media_preview, "ensure_media_preview_route_registered", lambda: True
    )
    assert "assemble_sequence" in ready.allowed_actions
    assert "import_production_outputs_to_authoring" in ready.allowed_actions
    return registry, ready, callbacks, receipts


def _m26_read(
    registry: ProductionWorkspaceRegistry, projection: ProductionWorkbenchProjection
) -> ProductionWorkbenchProjection:
    result = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.m26.read.store_backed",
            "action": "read_projection",
            "payload": {"workspace_handle": projection.workspace_handle},
        }
    ).projection
    assert isinstance(result, ProductionWorkbenchProjection)
    return result


@pytest.mark.parametrize("target_seconds", [30, 60])
def test_m26_store_backed_assembly_preserves_original_and_aggregate_preview_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_seconds: int
) -> None:
    registry, ready, callbacks, receipts = _m26_store_backed_publication(
        tmp_path, monkeypatch, target_seconds=target_seconds
    )
    entry = registry._entries[ready.workspace_handle]
    original_previews = dict(entry.preview_sources)
    assert len(original_previews) == len(receipts)
    original_workspace = entry.workspace
    original_lineage = entry.lineage_token
    original_store = entry.artifact_store

    registry.dispatch(_m26_assemble_action(ready, "request.m26.store_backed.assemble"))
    assert len(callbacks) == 1
    callbacks.pop()()
    succeeded = _m26_read(registry, ready)

    assert succeeded.assembly.state == "succeeded"
    assert succeeded.reconstruction_state == "complete"
    assert succeeded.outputs[:-1] == ready.outputs
    aggregate = succeeded.outputs[-1]
    assert aggregate.segment_id is None
    assert aggregate.preview is (target_seconds <= 30)
    assert aggregate.output_handle not in original_previews
    assert len(succeeded.outputs) == len(receipts) + 1
    assert set(entry.preview_sources) == {
        output.output_handle for output in succeeded.outputs if output.preview
    }
    assert all(
        entry.preview_sources[handle] is source for handle, source in original_previews.items()
    )
    assert entry.workspace is original_workspace
    assert entry.lineage_token is original_lineage
    assert entry.artifact_store is original_store
    assert all(
        retained is original
        for retained, original in zip(
            entry.accepted_authorities.artifact_receipts, receipts, strict=True
        )
    )
    assert "import_production_outputs_to_authoring" in succeeded.allowed_actions


def test_m26_assembly_retires_unreachable_reconstructions_from_a_full_persistent_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reconstruction root outlives every process-local workspace, so receipts committed by
    # earlier assemblies must not keep the transaction quota full for all later assemblies.
    from test_av_reconstruction_store import _committed

    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    entry = registry._entries[ready.workspace_handle]
    assert entry.artifact_store is not None
    seeding_runtime, _bridge = _m26_runtime(tmp_path, entry.artifact_store)
    seeding_store = seeding_runtime.reconstruction_store
    # Earlier assemblies, from this process or a previous one, completed before this request.
    seeding_store._clock_ms = lambda: 100
    stale = tuple(
        _committed(seeding_store, f"m26tx_stale{index}", f"avout_stale{index:027d}", b"stale")
        for index in range(8)
    )
    with pytest.raises(AVStoreError, match="transaction_quota_exceeded"):
        seeding_store.begin(
            transaction_id="m26tx_probe",
            plan_fingerprint=canonical_fingerprint({"probe": "plan"}),
            approval_fingerprint=canonical_fingerprint({"probe": "approval"}),
            expires_at_ms=60_000,
        )

    registry.dispatch(_m26_assemble_action(ready, "request.m26.retirement.assemble"))
    callbacks.pop()()
    succeeded = _m26_read(registry, ready)

    assert (succeeded.assembly.state, succeeded.assembly.failure_code) == ("succeeded", None)
    assert all(
        seeding_store.inspect(item).status is AVStoreInspectionStatus.MISSING for item in stale
    )
    execution = entry.accepted_authorities.assembly_execution
    assert execution is not None
    live = registry._live_reconstruction_transaction_ids()
    assert execution.reconstruction_receipt.transaction_id in live
    assert (
        seeding_store.retire_unreferenced(keep_transaction_ids=live, completed_before_ms=1 << 60)
        == 0
    )
    assert (
        seeding_store.inspect(execution.reconstruction_receipt).status
        is AVStoreInspectionStatus.COMPLETE
    )


def test_m26_store_backed_original_claims_remain_current_and_admissible_after_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The route becomes available after generation: isolate original claim lifetime from Q05's
    # nonempty-preview publication failure without replacing any receipt or store authority.
    registry, ready, callbacks, receipts = _m26_store_backed_publication(
        tmp_path, monkeypatch, original_previews=False
    )
    entry = registry._entries[ready.workspace_handle]
    pairs = ((receipts[0].segment_id, ready.outputs[0].output_handle),)

    def claim(selected: tuple[tuple[str, str], ...]) -> tuple[ProductionAuthoringOutputClaim, ...]:
        return registry.claim_authoring_output_batch(
            workspace_handle=ready.workspace_handle,
            workspace_id=ready.workspace_id,
            expected_workspace_revision=ready.workspace_revision,
            expected_workspace_fingerprint=ready.workspace_fingerprint,
            pairs=selected,
        )

    before = claim(pairs)
    assert before[0].current()
    registry.dispatch(_m26_assemble_action(ready, "request.m26.claims.assemble"))
    callbacks.pop()()
    assert before[0].current(), "assembly must not revoke unchanged original ownership"
    after = claim(pairs)
    assert after[0].receipt is before[0].receipt is receipts[0]
    assert after[0].store is before[0].store is entry.artifact_store
    assert after[0].lineage_token is before[0].lineage_token is entry.lineage_token
    assert after[0].output_handle == before[0].output_handle == pairs[0][1]
    with registry.authoring_output_commit_guard(before + after) as current:
        assert current
    succeeded = _m26_read(registry, ready)
    aggregate = next(output for output in succeeded.outputs if output.segment_id is None)
    with pytest.raises(ProductionWorkbenchError):
        claim(((receipts[0].segment_id, aggregate.output_handle),))
    entry.lineage_token = object()
    assert not before[0].current()
    assert not after[0].current()


def test_m26_store_backed_selection_preserves_assembly_but_requires_new_original_claim_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, ready, callbacks, receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    registry.dispatch(_m26_assemble_action(ready, "request.m26.selection.assemble"))
    callbacks.pop()()
    succeeded = _m26_read(registry, ready)
    entry = registry._entries[ready.workspace_handle]
    accepted_execution = entry.accepted_authorities.assembly_execution
    store = entry.artifact_store
    original = succeeded.outputs[0]
    old_claim = registry.claim_authoring_output_batch(
        workspace_handle=ready.workspace_handle,
        workspace_id=ready.workspace_id,
        expected_workspace_revision=succeeded.workspace_revision,
        expected_workspace_fingerprint=succeeded.workspace_fingerprint,
        pairs=((receipts[0].segment_id, original.output_handle),),
    )[0]
    assert old_claim.current()

    selected = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.m26.selection.select_original",
            "action": "set_selection",
            "payload": {
                "workspace_handle": ready.workspace_handle,
                "expected_workspace_revision": succeeded.workspace_revision,
                "expected_workspace_fingerprint": succeeded.workspace_fingerprint,
                "segment_ids": [receipts[0].segment_id],
            },
        }
    ).projection
    assert isinstance(selected, ProductionWorkbenchProjection)
    assert selected.workspace_revision == succeeded.workspace_revision + 1
    assert selected.workspace_fingerprint != succeeded.workspace_fingerprint
    assert selected.outputs == succeeded.outputs
    assert selected.assembly == succeeded.assembly
    assert selected.reconstruction_state == "complete"
    assert "import_production_outputs_to_authoring" in selected.allowed_actions
    assert not {"assemble_sequence", "retry_assembly"}.intersection(selected.allowed_actions)
    assert entry.accepted_authorities.assembly_execution is accepted_execution
    assert entry.artifact_store is store
    assert not old_claim.current()
    fresh = registry.claim_authoring_output_batch(
        workspace_handle=selected.workspace_handle,
        workspace_id=selected.workspace_id,
        expected_workspace_revision=selected.workspace_revision,
        expected_workspace_fingerprint=selected.workspace_fingerprint,
        pairs=((receipts[0].segment_id, original.output_handle),),
    )[0]
    assert fresh.current()
    assert fresh.receipt is old_claim.receipt is receipts[0]
    assert fresh.store is old_claim.store is store
    assert fresh.lineage_token is old_claim.lineage_token
    with pytest.raises(ProductionWorkbenchError):
        registry.claim_authoring_output_batch(
            workspace_handle=selected.workspace_handle,
            workspace_id=selected.workspace_id,
            expected_workspace_revision=succeeded.workspace_revision,
            expected_workspace_fingerprint=succeeded.workspace_fingerprint,
            pairs=((receipts[0].segment_id, original.output_handle),),
        )


@pytest.mark.parametrize("cancel", [False, True])
def test_m26_store_backed_failed_or_cancelled_execution_keeps_original_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    entry = registry._entries[ready.workspace_handle]
    original_authority = entry.accepted_authorities
    original_previews = entry.preview_sources

    def fail_execution(**_kwargs: object) -> M26ProductionAssemblyExecution:
        raise M26ProductionAssemblyError("controlled_execution_failure")

    if not cancel:
        monkeypatch.setattr(
            production_workspace_module, "execute_m26_production_assembly", fail_execution
        )
    planned = registry.dispatch(
        _m26_assemble_action(ready, "request.m26.failure.assemble")
    ).projection
    assert isinstance(planned, ProductionWorkbenchProjection)
    if cancel:
        registry.dispatch(
            _m26_cancel_or_retry_action(planned, "request.m26.failure.cancel", "cancel_assembly")
        )
    callbacks.pop()()
    failed = _m26_read(registry, ready)
    assert failed.assembly.state == "failed"
    assert failed.assembly.failure_code == (
        "cancelled" if cancel else "controlled_execution_failure"
    )
    assert failed.assembly.receipt_fingerprint is None
    assert entry.accepted_authorities is original_authority
    assert entry.preview_sources is original_previews
    assert failed.outputs == ready.outputs


@pytest.mark.parametrize("failure", ["projection", "preview"])
def test_m26_store_backed_publication_failure_never_exposes_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    entry = registry._entries[ready.workspace_handle]
    original_authority = entry.accepted_authorities
    original_previews = entry.preview_sources
    original_projection = registry._projection

    def reject_completed_projection(
        handle: str, candidate: production_workspace_module._ProductionEntry
    ) -> ProductionWorkbenchProjection:
        if candidate.accepted_authorities.assembly_execution is not None:
            raise ProductionWorkbenchError("accepted_authority_mismatch", 422)
        return original_projection(handle, candidate)

    def reject_preview_authority(**_kwargs: object) -> None:
        raise ValueError("controlled_eligible_preview_failure")

    if failure == "projection":
        monkeypatch.setattr(registry, "_projection", reject_completed_projection)
    else:
        monkeypatch.setattr(
            production_workspace_module,
            "build_media_preview_source_authority",
            reject_preview_authority,
        )
    registry.dispatch(_m26_assemble_action(ready, "request.m26.publication_failure.assemble"))
    callbacks.pop()()

    failed = _m26_read(registry, ready)
    assert failed.assembly.state == "failed"
    assert failed.assembly.receipt_fingerprint is None
    assert entry.accepted_authorities is original_authority
    assert entry.preview_sources is original_previews
    assert failed.outputs == ready.outputs


def test_m26_store_backed_cancel_after_preview_preparation_remains_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comfyui_h3_context.adapters.media_preview_authority import (
        MediaPreviewSourceAuthority,
        build_media_preview_source_authority,
    )

    registry, ready, callbacks, receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    entry = registry._entries[ready.workspace_handle]
    original_authority = entry.accepted_authorities
    original_previews = entry.preview_sources
    original_store = entry.artifact_store
    claim = registry.claim_authoring_output_batch(
        workspace_handle=ready.workspace_handle,
        workspace_id=ready.workspace_id,
        expected_workspace_revision=ready.workspace_revision,
        expected_workspace_fingerprint=ready.workspace_fingerprint,
        pairs=((receipts[0].segment_id, ready.outputs[0].output_handle),),
    )[0]
    prepared_handles: list[str] = []

    def prepare_then_cancel(**kwargs: object) -> MediaPreviewSourceAuthority:
        source = build_media_preview_source_authority(**kwargs)  # type: ignore[arg-type]
        prepared_handles.append(source.opaque_output_handle)
        running = _m26_read(registry, ready)
        assert running.assembly.state == "running"
        cancelled = registry.dispatch(
            _m26_cancel_or_retry_action(running, "request.m26.prepared.cancel", "cancel_assembly")
        ).projection
        assert isinstance(cancelled, ProductionWorkbenchProjection)
        assert cancelled.assembly.state == "cancelling"
        return source

    monkeypatch.setattr(
        production_workspace_module, "build_media_preview_source_authority", prepare_then_cancel
    )
    planned = registry.dispatch(
        _m26_assemble_action(ready, "request.m26.prepared.assemble")
    ).projection
    assert planned is not None
    callbacks.pop()()
    failed = _m26_read(registry, ready)
    assert len(prepared_handles) == 1
    assert failed.assembly.state == "failed"
    assert failed.assembly.failure_code == "cancelled"
    assert failed.assembly.receipt_fingerprint is None
    assert "retry_assembly" in failed.allowed_actions
    assert entry.accepted_authorities is original_authority
    assert entry.preview_sources is original_previews
    assert entry.artifact_store is original_store
    assert prepared_handles[0] not in entry.preview_sources
    assert failed.outputs == ready.outputs
    assert claim.current()
    with registry.authoring_output_commit_guard((claim,)) as current:
        assert current


def test_m26_production_action_publishes_derived_receipt_subject_as_succeeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context, _proposal, registry, imported, _authority, sequence, receipts = _m26_subject(
        SegmentationPolicyV1.FIXED_15
    )
    callbacks: list[Callable[[], None]] = []
    artifact_store = PrivateSegmentArtifactStore(
        tmp_path / "artifacts",
        clock_ms=lambda: 2_000,
    )

    def runtime_provider(store: PrivateSegmentArtifactStore) -> M26ProductionAssemblyRuntime:
        runtime, _bridge = _m26_runtime(tmp_path, store)
        return runtime

    def successful_execution(**kwargs: object) -> M26ProductionAssemblyExecution:
        return execute_m26_production_assembly(
            **kwargs,  # type: ignore[arg-type]
            low_level_executor=_m26_successful_low_level_executor,
            source_factory=_M26OriginalSource,
        )

    registry._assembly_runtime_provider = runtime_provider
    registry._assembly_submitter = callbacks.append
    registry._clock_ms = _M26Clock()
    monkeypatch.setattr(
        production_workspace_module,
        "execute_m26_production_assembly",
        successful_execution,
    )
    ready = registry.replace_generation_authority(
        imported.workspace_handle,
        expected_workspace_revision=imported.workspace_revision,
        expected_workspace_fingerprint=imported.workspace_fingerprint,
        expected_sequence_state_fingerprint=None,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        artifact_store=artifact_store,
    )
    planned = registry.dispatch(_m26_assemble_action(ready, "request.m26.success")).projection
    assert isinstance(planned, ProductionWorkbenchProjection)
    assert planned.assembly.state == "planned"
    callbacks.pop(0)()

    succeeded = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.m26.read.succeeded",
            "action": "read_projection",
            "payload": {"workspace_handle": ready.workspace_handle},
        }
    ).projection
    assert isinstance(succeeded, ProductionWorkbenchProjection)
    assert succeeded.assembly.state == "succeeded"
    assert succeeded.assembly.completed == succeeded.assembly.total == len(receipts)
    assert succeeded.assembly.receipt_fingerprint is not None
    assert "assemble_sequence" not in succeeded.allowed_actions
    execution = registry._entries[ready.workspace_handle].accepted_authorities.assembly_execution
    assert execution is not None
    assert execution.reconstruction_receipt.artifact_receipt_fingerprints == tuple(
        item.fingerprint for item in execution.outer_plan.derived_input_receipts
    )
    assert execution.assembly_receipt.original_artifact_receipt_fingerprints == tuple(
        item.fingerprint for item in receipts
    )


def test_m26_missing_runtime_is_typed_unavailable_without_worker_or_file_effect(
    tmp_path: Path,
) -> None:
    _context, _proposal, registry, imported, _authority, sequence, receipts = _m26_subject(
        SegmentationPolicyV1.FIXED_15
    )
    artifact_store = PrivateSegmentArtifactStore(
        tmp_path / "artifacts",
        clock_ms=lambda: 2_000,
    )
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    projection = registry.replace_generation_authority(
        imported.workspace_handle,
        expected_workspace_revision=imported.workspace_revision,
        expected_workspace_fingerprint=imported.workspace_fingerprint,
        expected_sequence_state_fingerprint=None,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        artifact_store=artifact_store,
    )
    after = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    assert projection.assembly.state == "unavailable"
    assert projection.assembly.failure_code == "media_runtime_not_authorized"
    assert "assembly_unavailable:media_runtime_not_authorized" in projection.blocker_codes
    assert "assemble_sequence" not in projection.allowed_actions
    assert registry._assembly_executor is None
    assert after == before
