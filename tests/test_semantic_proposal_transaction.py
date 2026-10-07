from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_rendering import make_plan

from comfyui_h3_context.core import (
    AssetRole,
    ContextPlan,
    EnrichmentTargetKind,
    EvidenceLevel,
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceKind,
    FeasibleAVTimelineRequest,
    FrameGridPolicy,
    HierarchicalEvidenceReductionRequest,
    MediaKind,
    ModelBackendFamily,
    ProviderIdentity,
    ReductionBudget,
    ReductionEvidence,
    ReductionLevel,
    ReductionModality,
    ReductionTarget,
    ReferenceAnchor,
    ReferenceAsset,
    SemanticEnrichmentResult,
    SemanticPlanningBudget,
    SemanticPlanningPolicy,
    SemanticPlanningProfile,
    SemanticPlanningRequest,
    SemanticPlanningResult,
    SemanticProposal,
    SemanticTargetKind,
    SupportStatus,
    TaskMode,
    TaskModeRetentionRequest,
    TimePoint,
    apply_semantic_enrichment,
    build_feasible_av_timeline,
    build_hierarchical_evidence_reduction,
    build_reference_registry,
    build_task_mode_retention_report,
    build_unified_evidence_graph,
    parse_semantic_proposal,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.evidence import Provenance
from comfyui_h3_context.core.segment_workspace import (
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    create_workspace,
)
from comfyui_h3_context.core.semantic_proposal_transaction import (
    MAX_SEMANTIC_PROPOSAL_ATTEMPTS,
    SemanticProposalAuthorization,
    SemanticProposalTransaction,
    SemanticProposalTransactionError,
    SemanticProposalTransactionState,
    accept_semantic_proposal_transaction,
    cancel_semantic_proposal_transaction,
    edit_semantic_proposal_transaction,
    fail_semantic_proposal_transaction,
    prepare_semantic_proposal_transaction,
    regenerate_semantic_proposal_transaction,
    reject_semantic_proposal_transaction,
    resolve_semantic_proposal_clarification,
)


def fp(letter: str) -> str:
    return "sha256:" + letter * 64


def _planning_request(*, raw_media: bool = False) -> SemanticPlanningRequest:
    registry = build_reference_registry(
        (ReferenceAsset("scene_1", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),)
    )
    mode_report = build_task_mode_retention_report(
        TaskModeRetentionRequest.from_user_mode(TaskMode.I2VA, registry)
    )
    timeline = build_feasible_av_timeline(
        FeasibleAVTimelineRequest(
            mode_report,
            TimePoint.from_text("4"),
            FrameGridPolicy(24, 1),
            registry,
            anchors=(
                ReferenceAnchor("anchor.first", "first_frame", "scene_1", TimePoint.from_text("0")),
            ),
        )
    )
    assert timeline.plan is not None
    reduction = build_hierarchical_evidence_reduction(
        HierarchicalEvidenceReductionRequest(
            build_unified_evidence_graph(),
            timeline.plan,
            (
                ReductionEvidence(
                    "scene.summary",
                    ReductionLevel.ASSET,
                    ReductionModality.VISUAL,
                    "scene evidence",
                    ("scene_1",),
                    token_estimate=4,
                    fidelity_weight=2,
                ),
            ),
            ReductionTarget("scene_1", focus_source_ids=("scene_1",)),
            ReductionBudget(2, 16, Decimal("0"), Decimal("0"), Decimal("0")),
        )
    )
    assert reduction.plan is not None
    profile = SemanticPlanningProfile(
        "native.profile",
        ModelBackendFamily.COMFYUI_NATIVE,
        "qwen3-vl-8b",
        fp("a"),
        seed=17,
        raw_media_capability=raw_media,
    )
    return SemanticPlanningRequest(
        reduction.plan,
        timeline.plan,
        profile,
        SemanticPlanningPolicy.STRICT,
        budget=SemanticPlanningBudget(4, 128, 1024, 16),
        media_fingerprints=(fp("3"),) if raw_media else (),
    )


def _document(request: SemanticPlanningRequest, claim: str) -> str:
    value = {
        "schema": "h3.constrained_semantic_planning.v1",
        "document_id": "proposal.document",
        "policy": "strict",
        "task_mode": request.timeline_plan.task_mode.value,
        "effective_duration": request.timeline_plan.effective_duration.raw,
        "asset_ids": list(request.timeline_plan.reference_order),
        "reference_order": list(request.timeline_plan.reference_order),
        "timeline_fingerprint": request.timeline_plan.fingerprint,
        "reduction_fingerprint": request.reduction_plan.fingerprint,
        "preserved_exact_text": [],
        "proposals": [
            {
                "schema": "h3.constrained_semantic_planning.v1",
                "proposal_id": "proposal.scene",
                "target_kind": SemanticTargetKind.SCENE.value,
                "target_id": "scene_1",
                "claim": claim,
                "evidence_label": "source",
                "source_ids": ["scene_1"],
                "confidence": "0.80",
                "rationale": "source-grounded reduction evidence",
            }
        ],
        "complete": True,
    }
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _baseline_plan(request: SemanticPlanningRequest) -> ContextPlan:
    registry = build_reference_registry(
        (ReferenceAsset("scene_1", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),)
    )
    plan = make_plan(TaskMode.I2VA, registry)
    normalized = replace(
        plan.request,
        effective_frame_count=90,
        effective_duration_seconds=4.0,
    )
    timeline_segment = replace(plan.intent_graph.segments[0], end=TimePoint.from_text("4"))
    graph = replace(
        plan.intent_graph,
        effective_duration=TimePoint.from_text("4"),
        segments=(timeline_segment,),
    )
    assert request.timeline_plan.reference_order == ("scene_1",)
    return replace(plan, request=normalized, intent_graph=graph)


def _enrichment(
    plan: ContextPlan,
    claim: str,
    *,
    proposal_id: str = "proposal.scene",
) -> SemanticEnrichmentResult:
    source = EvidenceSource(EvidenceSourceKind.PROVIDER_OUTPUT, f"provider.{proposal_id}")
    evidence = EvidenceRecord(
        proposal_id,
        claim,
        EvidenceOrigin.ASSISTED_PROPOSAL,
        support=SupportStatus.SUPPORTED,
        provenance=Provenance(
            source,
            ProviderIdentity.LOCAL,
            EvidenceLevel.EXPERIMENTAL,
            provider_version="reasoner-1.0.0",
            source_revision="weights-rev-1",
        ),
        confidence=Decimal("0.8"),
    )
    proposal = SemanticProposal(
        proposal_id,
        EnrichmentTargetKind.SCENE,
        "scene_1",
        claim,
        evidence,
    )
    return apply_semantic_enrichment(plan, (proposal,))


def _case(
    *,
    claim: str = "with restrained warm morning light",
    raw_media: bool = False,
) -> tuple[
    MultiSegmentWorkspace,
    ContextPlan,
    SemanticPlanningRequest,
    SemanticPlanningResult,
    SemanticEnrichmentResult,
    SemanticProposalAuthorization,
]:
    request = _planning_request(raw_media=raw_media)
    planning = parse_semantic_proposal(request, _document(request, claim))
    assert planning.is_valid
    baseline = _baseline_plan(request)
    enrichment = _enrichment(baseline, claim)
    baseline_graph_fp = canonical_fingerprint(baseline.intent_graph.to_wire())
    segment = SegmentDeclaration(
        "segment.1",
        TaskMode.I2VA,
        "source.segment.1",
        ("scene_1",),
        SegmentDuration.from_frame_count(90),
        SegmentRelationKind.INDEPENDENT,
        None,
        baseline_graph_fp,
        None,
        canonical_fingerprint(request.profile.to_wire()),
        canonical_fingerprint(baseline.request.reference_registry.to_wire()),
        fp("1"),
        fp("2"),
    )
    workspace = create_workspace(
        "workspace.semantic",
        (segment,),
        accepted_intent_authorities=(
            AcceptedIntentAuthority(segment.segment_id, baseline_graph_fp),
        ),
    )
    authorization = SemanticProposalAuthorization(
        capability_fingerprint=fp("c"),
        profile_fingerprint=canonical_fingerprint(request.profile.to_wire()),
        raw_media_allowed=raw_media,
        consent_fingerprint=fp("d") if raw_media else None,
    )
    return workspace, baseline, request, planning, enrichment, authorization


def _prepared(
    *,
    claim: str = "with restrained warm morning light",
    clarifications: tuple[str, ...] = (),
) -> tuple[MultiSegmentWorkspace, SemanticProposalTransaction]:
    workspace, baseline, request, planning, enrichment, authorization = _case(claim=claim)
    transaction = prepare_semantic_proposal_transaction(
        workspace=workspace,
        segment_id="segment.1",
        baseline_plan=baseline,
        planning_request=request,
        planning_result=planning,
        enrichment_result=enrichment,
        authorization=authorization,
        transaction_id="semantic.tx.1",
        clarification_ids=clarifications,
    )
    return workspace, transaction


def test_exact_planning_enrichment_and_graph_diff_are_bound_without_content_leakage() -> None:
    workspace, transaction = _prepared()

    assert transaction.state is SemanticProposalTransactionState.READY_FOR_REVIEW
    assert transaction.workspace_fingerprint == workspace.fingerprint
    assert transaction.graph_diff.before_graph_fingerprint != (
        transaction.graph_diff.after_graph_fingerprint
    )
    assert transaction.graph_diff.changed_collections == ("scenes",)
    public = json.dumps(transaction.to_public_dict(), sort_keys=True).lower()
    for forbidden in ("warm morning light", "bakery", "user_intent", "proposal_text", "http"):
        assert forbidden not in public


def test_only_explicit_current_accept_atomically_revises_one_segment() -> None:
    workspace, transaction = _prepared()

    applied = accept_semantic_proposal_transaction(
        transaction,
        expected_transaction_fingerprint=transaction.fingerprint,
        workspace=workspace,
        expected_workspace_fingerprint=workspace.fingerprint,
    )

    assert applied.transaction.state is SemanticProposalTransactionState.ACCEPTED
    assert applied.workspace.revision == workspace.revision + 1
    assert applied.workspace.parent_workspace_fingerprint == workspace.fingerprint
    assert applied.workspace.segments[0].accepted_intent_fingerprint == (
        transaction.graph_diff.after_graph_fingerprint
    )
    assert applied.workspace.segments[0].semantic_receipt_fingerprint == transaction.fingerprint
    assert canonical_fingerprint(applied.intent_graph.to_wire()) == (
        transaction.graph_diff.after_graph_fingerprint
    )
    with pytest.raises(SemanticProposalTransactionError, match="terminal_transaction"):
        accept_semantic_proposal_transaction(
            applied.transaction,
            expected_transaction_fingerprint=applied.transaction.fingerprint,
            workspace=applied.workspace,
            expected_workspace_fingerprint=applied.workspace.fingerprint,
        )


def test_stale_workspace_or_transaction_cannot_apply() -> None:
    workspace, transaction = _prepared()
    with pytest.raises(SemanticProposalTransactionError, match="stale_transaction"):
        accept_semantic_proposal_transaction(
            transaction,
            expected_transaction_fingerprint=fp("0"),
            workspace=workspace,
            expected_workspace_fingerprint=workspace.fingerprint,
        )
    with pytest.raises(SemanticProposalTransactionError, match="stale_workspace"):
        accept_semantic_proposal_transaction(
            transaction,
            expected_transaction_fingerprint=transaction.fingerprint,
            workspace=workspace,
            expected_workspace_fingerprint=fp("0"),
        )


def test_clarification_blocks_accept_until_exact_resolution() -> None:
    workspace, transaction = _prepared(clarifications=("clarify.scene",))
    assert transaction.state is SemanticProposalTransactionState.CLARIFICATION_REQUIRED
    with pytest.raises(SemanticProposalTransactionError, match="not_review_ready"):
        accept_semantic_proposal_transaction(
            transaction,
            expected_transaction_fingerprint=transaction.fingerprint,
            workspace=workspace,
            expected_workspace_fingerprint=workspace.fingerprint,
        )

    resolved = resolve_semantic_proposal_clarification(
        transaction,
        expected_transaction_fingerprint=transaction.fingerprint,
        resolution_fingerprints=(fp("4"),),
    )
    assert resolved.state is SemanticProposalTransactionState.READY_FOR_REVIEW
    assert resolved.clarification_ids == ("clarify.scene",)
    assert resolved.resolution_fingerprints == (fp("4"),)


def test_edit_is_revalidated_and_regenerate_has_a_new_bounded_identity() -> None:
    workspace, transaction = _prepared()
    _, baseline, request, planning, enrichment, authorization = _case(
        claim="with a controlled cool rim light"
    )
    edited = edit_semantic_proposal_transaction(
        transaction,
        expected_transaction_fingerprint=transaction.fingerprint,
        workspace=workspace,
        baseline_plan=baseline,
        planning_request=request,
        planning_result=planning,
        enrichment_result=enrichment,
        authorization=authorization,
    )
    assert edited.revision == transaction.revision + 1
    assert (
        edited.graph_diff.after_graph_fingerprint != transaction.graph_diff.after_graph_fingerprint
    )

    regenerated = regenerate_semantic_proposal_transaction(
        edited,
        expected_transaction_fingerprint=edited.fingerprint,
        workspace=workspace,
        baseline_plan=baseline,
        planning_request=request,
        planning_result=planning,
        enrichment_result=enrichment,
        authorization=authorization,
        transaction_id="semantic.tx.2",
    )
    assert regenerated.transaction_id == "semantic.tx.2"
    assert regenerated.attempt == 2
    assert regenerated.revision == 1
    assert regenerated.previous_transaction_fingerprint == edited.fingerprint

    capped = replace(
        regenerated,
        attempt=MAX_SEMANTIC_PROPOSAL_ATTEMPTS,
        transaction_fingerprint=None,
    )
    with pytest.raises(SemanticProposalTransactionError, match="attempt_limit"):
        regenerate_semantic_proposal_transaction(
            capped,
            expected_transaction_fingerprint=capped.fingerprint,
            workspace=workspace,
            baseline_plan=baseline,
            planning_request=request,
            planning_result=planning,
            enrichment_result=enrichment,
            authorization=authorization,
            transaction_id="semantic.tx.limit",
        )


def test_reject_cancel_and_failure_are_terminal_without_canonical_output() -> None:
    workspace, rejectable = _prepared()
    rejected = reject_semantic_proposal_transaction(
        rejectable, expected_transaction_fingerprint=rejectable.fingerprint
    )
    assert rejected.state is SemanticProposalTransactionState.REJECTED
    assert rejected.applied_workspace_fingerprint is None

    _, cancellable = _prepared()
    cancelled = cancel_semantic_proposal_transaction(
        cancellable, expected_transaction_fingerprint=cancellable.fingerprint
    )
    assert cancelled.state is SemanticProposalTransactionState.CANCELLED

    _, fallible = _prepared()
    failed = fail_semantic_proposal_transaction(
        fallible,
        expected_transaction_fingerprint=fallible.fingerprint,
        failure_code="candidate_invalid",
    )
    assert failed.state is SemanticProposalTransactionState.FAILED
    assert failed.failure_code == "candidate_invalid"

    _, baseline, request, planning, enrichment, authorization = _case()
    for terminal in (rejected, cancelled, failed):
        with pytest.raises(SemanticProposalTransactionError, match="terminal_transaction"):
            regenerate_semantic_proposal_transaction(
                terminal,
                expected_transaction_fingerprint=terminal.fingerprint,
                workspace=workspace,
                baseline_plan=baseline,
                planning_request=request,
                planning_result=planning,
                enrichment_result=enrichment,
                authorization=authorization,
                transaction_id=f"{terminal.transaction_id}.regenerated",
            )


def test_capability_consent_correspondence_and_preserved_contracts_fail_closed() -> None:
    workspace, baseline, request, planning, enrichment, authorization = _case(raw_media=True)
    denied = replace(authorization, raw_media_allowed=False, consent_fingerprint=None)
    with pytest.raises(SemanticProposalTransactionError, match="raw_media_consent_required"):
        prepare_semantic_proposal_transaction(
            workspace=workspace,
            segment_id="segment.1",
            baseline_plan=baseline,
            planning_request=request,
            planning_result=planning,
            enrichment_result=enrichment,
            authorization=denied,
            transaction_id="semantic.tx.denied",
        )

    mismatched = _enrichment(baseline, "different unbound claim")
    with pytest.raises(SemanticProposalTransactionError, match="proposal_correspondence"):
        prepare_semantic_proposal_transaction(
            workspace=workspace,
            segment_id="segment.1",
            baseline_plan=baseline,
            planning_request=request,
            planning_result=planning,
            enrichment_result=mismatched,
            authorization=authorization,
            transaction_id="semantic.tx.mismatch",
        )

    tampered_audit = replace(
        enrichment,
        audit=replace(enrichment.audit, after_fingerprint=fp("e")),
    )
    with pytest.raises(SemanticProposalTransactionError, match="enrichment_audit_mismatch"):
        prepare_semantic_proposal_transaction(
            workspace=workspace,
            segment_id="segment.1",
            baseline_plan=baseline,
            planning_request=request,
            planning_result=planning,
            enrichment_result=tampered_audit,
            authorization=authorization,
            transaction_id="semantic.tx.audit-mismatch",
        )

    wrong_duration_segment = replace(
        workspace.segments[0], duration=SegmentDuration.from_frame_count(107)
    )
    wrong_duration_workspace = create_workspace(
        "workspace.semantic.duration-mismatch",
        (wrong_duration_segment,),
        accepted_intent_authorities=(
            AcceptedIntentAuthority(
                wrong_duration_segment.segment_id,
                wrong_duration_segment.accepted_intent_fingerprint,
            ),
        ),
    )
    with pytest.raises(SemanticProposalTransactionError, match="baseline_contract_mismatch"):
        prepare_semantic_proposal_transaction(
            workspace=wrong_duration_workspace,
            segment_id="segment.1",
            baseline_plan=baseline,
            planning_request=request,
            planning_result=planning,
            enrichment_result=enrichment,
            authorization=authorization,
            transaction_id="semantic.tx.duration-mismatch",
        )


def test_tampered_transaction_is_rejected_before_any_transition() -> None:
    _, transaction = _prepared()
    object.__setattr__(transaction, "baseline_graph_fingerprint", fp("9"))
    with pytest.raises(SemanticProposalTransactionError, match="tampered_transaction"):
        reject_semantic_proposal_transaction(
            transaction, expected_transaction_fingerprint=transaction.fingerprint
        )
