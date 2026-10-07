from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Any, cast

import pytest

from comfyui_h3_context.core import (
    MAX_SELECTIVE_RERUN_WIRE_BYTES,
    ArtifactReuseStatus,
    FingerprintDomain,
    GenerationJobSpec,
    SelectiveArtifactEvidence,
    SelectiveRerunApproval,
    SelectiveRerunDisposition,
    SelectiveRerunError,
    build_approved_generation_sequence_plan,
    build_selective_rerun_plan,
)
from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.generation_sequence import (
    GenerationSequenceError,
    cancel_generation_sequence,
    create_generation_sequence_state,
    reconcile_generation_sequence,
    record_generation_projection,
    record_generation_submission,
    retry_generation_job,
)
from comfyui_h3_context.core.segment_artifacts import (
    ArtifactLifecycleState,
    SegmentArtifactReceipt,
)
from comfyui_h3_context.core.segment_workspace import (
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentContextManifest,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    create_workspace,
    derive_segment_manifests,
    revise_workspace,
)


def _fp(label: str) -> str:
    return "sha256:" + sha256(label.encode("ascii")).hexdigest()


def _segment(
    segment_id: str,
    *,
    settings: str,
    relation: SegmentRelationKind = SegmentRelationKind.INDEPENDENT,
    predecessor: str | None = None,
) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=TaskMode.T2VA,
        source_id=f"source.{segment_id}",
        reference_ids=(),
        duration=SegmentDuration.from_frame_count(124),
        relation=relation,
        predecessor_segment_id=predecessor,
        accepted_intent_fingerprint=_fp(f"intent.{segment_id}"),
        semantic_receipt_fingerprint=None,
        profile_fingerprint=_fp(f"profile.{segment_id}"),
        reference_registry_fingerprint=_fp(f"registry.{segment_id}"),
        native_binding_fingerprint=_fp(f"native.{segment_id}"),
        producer_settings_fingerprint=_fp(settings),
    )


def _authorities(
    segments: tuple[SegmentDeclaration, ...],
) -> tuple[AcceptedIntentAuthority, ...]:
    return tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in segments
    )


def _workspace(
    segments: tuple[SegmentDeclaration, ...],
    *,
    workspace_id: str = "workspace.selective",
) -> MultiSegmentWorkspace:
    return create_workspace(
        workspace_id,
        segments,
        accepted_intent_authorities=_authorities(segments),
    )


def _revision(
    workspace: MultiSegmentWorkspace,
    segments: tuple[SegmentDeclaration, ...],
) -> MultiSegmentWorkspace:
    return revise_workspace(
        workspace,
        expected_workspace_fingerprint=workspace.fingerprint,
        accepted_intent_authorities=_authorities(segments),
        segments=segments,
    )


def _spec(manifest: SegmentContextManifest) -> GenerationJobSpec:
    return GenerationJobSpec(
        segment_id=manifest.segment_id,
        job_id=f"job.{manifest.segment_id}",
        graph_fingerprint=_fp(f"graph.{manifest.segment_id}"),
        compiled_prompt_fingerprint=_fp(f"compiled.{manifest.segment_id}"),
        model_fingerprint=_fp("model.h3"),
        runtime_fingerprint=_fp("runtime.comfyui.0.32.0"),
        expected_format="video.mp4",
        expected_shape=(120, 512, 512, 3),
        timeout_ms=60_000,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )


def _receipt(
    manifest: SegmentContextManifest,
    spec: GenerationJobSpec,
    *,
    predecessor_output: str | None = None,
) -> SegmentArtifactReceipt:
    return SegmentArtifactReceipt(
        artifact_id=f"artifact.{manifest.segment_id}",
        state=ArtifactLifecycleState.COMPLETE,
        workspace_id=manifest.workspace_id,
        workspace_revision=manifest.workspace_revision,
        workspace_fingerprint=manifest.workspace_fingerprint,
        segment_id=manifest.segment_id,
        manifest_fingerprint=manifest.fingerprint,
        producer_fingerprint=manifest.producer_fingerprint,
        transaction_fingerprint=_fp(f"transaction.{manifest.segment_id}"),
        graph_fingerprint=spec.graph_fingerprint,
        native_binding_fingerprint=manifest.native_binding_fingerprint,
        model_fingerprint=spec.model_fingerprint,
        runtime_fingerprint=spec.runtime_fingerprint,
        settings_fingerprint=manifest.producer_settings_fingerprint,
        source_id=manifest.source_id,
        predecessor_artifact_fingerprint=predecessor_output,
        execution_fingerprint=_fp(f"execution.{manifest.segment_id}"),
        format_label=spec.expected_format,
        shape=spec.expected_shape,
        created_at_ms=100,
        expires_at_ms=10_000,
        output_fingerprint=_fp(f"output.{manifest.segment_id}"),
        byte_length=1024,
    )


def _evidence(
    manifest: SegmentContextManifest,
    spec: GenerationJobSpec,
    *,
    status: ArtifactReuseStatus = ArtifactReuseStatus.REUSABLE,
    predecessor_output: str | None = None,
) -> SelectiveArtifactEvidence:
    receipt = _receipt(manifest, spec, predecessor_output=predecessor_output)
    return SelectiveArtifactEvidence(
        segment_id=manifest.segment_id,
        status=status,
        inspected_at_ms=100,
        receipt=receipt,
    )


def _receipt_field(
    receipt: SegmentArtifactReceipt | None,
    field: str,
    value: object,
) -> SegmentArtifactReceipt:
    assert receipt is not None
    return cast(
        SegmentArtifactReceipt,
        replace(cast(Any, receipt), **{field: value}, receipt_fingerprint=None),
    )


def _fixture(
    *,
    count: int = 2,
    changed_segment: str | None = None,
    chain: bool = True,
) -> tuple[
    MultiSegmentWorkspace,
    tuple[SegmentContextManifest, ...],
    tuple[SegmentContextManifest, ...],
    tuple[GenerationJobSpec, ...],
]:
    segments = tuple(
        _segment(
            f"segment.{index + 1}",
            settings=("old" if changed_segment == f"segment.{index + 1}" else "stable"),
            relation=(
                SegmentRelationKind.PREDECESSOR
                if chain and index > 0
                else SegmentRelationKind.INDEPENDENT
            ),
            predecessor=(f"segment.{index}" if chain and index > 0 else None),
        )
        for index in range(count)
    )
    previous = _workspace(segments)
    current_segments = segments
    if changed_segment is not None:
        current_segments = tuple(
            replace(
                item,
                producer_settings_fingerprint=(
                    _fp("changed")
                    if item.segment_id == changed_segment
                    else item.producer_settings_fingerprint
                ),
            )
            for item in segments
        )
    current = _revision(previous, current_segments)
    previous_manifests = derive_segment_manifests(previous)
    current_manifests = derive_segment_manifests(current)
    specs = tuple(_spec(item) for item in current_manifests)
    return current, previous_manifests, current_manifests, specs


def test_requested_clean_origin_expands_downstream_and_needs_exact_approval() -> None:
    workspace, previous, current, specs = _fixture(count=3)
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        requested_segment_ids=("segment.1",),
        job_specs=specs,
    )

    assert tuple(item.segment_id for item in plan.decisions) == (
        "segment.1",
        "segment.2",
        "segment.3",
    )
    assert plan.queued_segment_ids == ("segment.1", "segment.2", "segment.3")
    assert plan.required_enlargement_segment_ids == ("segment.2", "segment.3")
    assert plan.selection_safe is False
    assert plan.decisions[0].reason_codes == ("requested_rerun",)
    assert plan.decisions[1].disposition is SelectiveRerunDisposition.QUEUE

    with pytest.raises(SelectiveRerunError, match="approval_missing_required_segment"):
        build_approved_generation_sequence_plan(
            plan,
            SelectiveRerunApproval(plan.fingerprint, ("segment.1",)),
            workspace=workspace,
            manifests=current,
            job_specs=specs,
        )

    sequence = build_approved_generation_sequence_plan(
        plan,
        SelectiveRerunApproval(
            plan.fingerprint,
            ("segment.1", "segment.2", "segment.3"),
        ),
        workspace=workspace,
        manifests=current,
        job_specs=specs,
    )
    assert tuple(item.segment_id for item in sequence.jobs) == (
        "segment.1",
        "segment.2",
        "segment.3",
    )


def test_exact_clean_receipt_is_reused_and_only_approved_dirty_segment_is_queued() -> None:
    workspace, previous, current, specs = _fixture(
        count=2,
        changed_segment="segment.1",
        chain=False,
    )
    # The clean output belongs to the accepted predecessor workspace.  A new workspace
    # revision must not force regeneration when the producer identity is unchanged.
    clean_evidence = _evidence(previous[1], specs[1])
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        artifact_evidence=(clean_evidence,),
        job_specs=specs,
    )

    assert plan.queued_segment_ids == ("segment.1",)
    assert plan.reused_segment_ids == ("segment.2",)
    assert plan.blocked_segment_ids == ()
    assert plan.selection_safe is True
    assert previous[1].workspace_revision != current[1].workspace_revision
    assert plan.decisions[1].reason_codes == ("exact_clean_artifact",)
    sequence = build_approved_generation_sequence_plan(
        plan,
        SelectiveRerunApproval(plan.fingerprint, ("segment.1",)),
        workspace=workspace,
        manifests=current,
        job_specs=specs,
        artifact_evidence=(replace(clean_evidence, inspected_at_ms=200),),
        approval_inspected_at_ms=200,
    )
    assert tuple(item.segment_id for item in sequence.jobs) == ("segment.1",)
    assert sequence.clean_segment_ids == ("segment.2",)
    public = sequence.to_wire()
    assert "prompt" not in public
    assert "path" not in public


def test_plan_rejects_reusable_receipt_not_bound_to_its_reuse_decision() -> None:
    workspace, previous, current, specs = _fixture(
        count=2,
        changed_segment="segment.1",
        chain=False,
    )
    clean_evidence = _evidence(previous[1], specs[1])
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        artifact_evidence=(clean_evidence,),
        job_specs=specs,
    )
    replacement = _receipt_field(
        clean_evidence.receipt,
        "transaction_fingerprint",
        _fp("foreign.transaction"),
    )
    with pytest.raises(SelectiveRerunError, match="reusable_receipt_decision_mismatch"):
        replace(plan, reusable_receipts=(replacement,))


@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (ArtifactReuseStatus.MISSING, "artifact_missing"),
        (ArtifactReuseStatus.EXPIRED, "artifact_expired"),
        (ArtifactReuseStatus.STALE, "artifact_stale"),
        (ArtifactReuseStatus.TAMPERED, "artifact_tampered"),
        (ArtifactReuseStatus.WRONG_KIND, "artifact_wrong_kind"),
        (ArtifactReuseStatus.INCOMPATIBLE, "artifact_incompatible"),
    ),
)
def test_unusable_clean_evidence_becomes_a_forced_origin(
    status: ArtifactReuseStatus,
    reason: str,
) -> None:
    workspace, previous, current, specs = _fixture(count=2)
    predecessor = _receipt(previous[0], specs[0]).output_fingerprint
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        requested_segment_ids=(),
        artifact_evidence=(
            _evidence(previous[0], specs[0], status=status),
            _evidence(previous[1], specs[1], predecessor_output=predecessor),
        ),
        job_specs=specs,
    )

    assert plan.queued_segment_ids == ("segment.1", "segment.2")
    assert plan.required_enlargement_segment_ids == ("segment.1", "segment.2")
    assert plan.decisions[0].reason_codes == (reason,)
    assert plan.decisions[1].reason_codes == ("upstream_forced_rerun",)
    with pytest.raises(SelectiveRerunError, match="approval_missing_required_segment"):
        build_approved_generation_sequence_plan(
            plan,
            SelectiveRerunApproval(plan.fingerprint, ("segment.1",)),
            workspace=workspace,
            manifests=current,
            job_specs=specs,
        )


def test_exact_identity_mismatch_is_not_reuse_even_when_store_says_reusable() -> None:
    workspace, previous, current, specs = _fixture(count=1)
    receipt = _receipt(previous[0], specs[0])
    incompatible = replace(receipt, model_fingerprint=_fp("wrong-model"), receipt_fingerprint=None)
    evidence = SelectiveArtifactEvidence(
        segment_id=current[0].segment_id,
        status=ArtifactReuseStatus.REUSABLE,
        inspected_at_ms=100,
        receipt=incompatible,
    )
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        requested_segment_ids=(),
        artifact_evidence=(evidence,),
        job_specs=specs,
    )
    assert plan.reused_segment_ids == ()
    assert plan.queued_segment_ids == ("segment.1",)
    assert plan.decisions[0].reason_codes == ("artifact_incompatible",)


@pytest.mark.parametrize(
    "field",
    (
        "workspace_id",
        "workspace_revision",
        "workspace_fingerprint",
        "segment_id",
        "manifest_fingerprint",
        "producer_fingerprint",
        "native_binding_fingerprint",
        "settings_fingerprint",
        "source_id",
        "graph_fingerprint",
        "model_fingerprint",
        "runtime_fingerprint",
        "format_label",
        "shape",
    ),
)
def test_every_reuse_identity_dimension_is_checked(field: str) -> None:
    workspace, previous, current, specs = _fixture(count=1)
    receipt = _receipt(previous[0], specs[0])
    values: dict[str, object] = {
        "workspace_id": "workspace.foreign",
        "workspace_revision": previous[0].workspace_revision + 1,
        "workspace_fingerprint": _fp("foreign.workspace"),
        "segment_id": "segment.foreign",
        "manifest_fingerprint": _fp("foreign.manifest"),
        "producer_fingerprint": _fp("foreign.producer"),
        "native_binding_fingerprint": _fp("foreign.native"),
        "settings_fingerprint": _fp("foreign.settings"),
        "source_id": "source.foreign",
        "graph_fingerprint": _fp("foreign.graph"),
        "model_fingerprint": _fp("foreign.model"),
        "runtime_fingerprint": _fp("foreign.runtime"),
        "format_label": "video.webm",
        "shape": (121, 512, 512, 3),
    }
    incompatible = _receipt_field(receipt, field, values[field])
    evidence = SelectiveArtifactEvidence(
        segment_id=current[0].segment_id,
        status=ArtifactReuseStatus.REUSABLE,
        inspected_at_ms=100,
        receipt=incompatible,
    )
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        artifact_evidence=(evidence,),
        job_specs=specs,
    )
    assert plan.reused_segment_ids == ()
    assert plan.decisions[0].reason_codes == ("artifact_incompatible",)


def test_actual_wrong_kind_and_tampered_receipt_are_rejected_at_both_boundaries() -> None:
    workspace, previous, current, specs = _fixture(count=1)
    receipt = _receipt(previous[0], specs[0])
    object.__setattr__(receipt, "artifact_kind", "foreign_kind")
    wrong_kind = SelectiveArtifactEvidence(
        segment_id=current[0].segment_id,
        status=ArtifactReuseStatus.REUSABLE,
        inspected_at_ms=100,
        receipt=receipt,
    )
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        artifact_evidence=(wrong_kind,),
        job_specs=specs,
    )
    assert plan.decisions[0].reason_codes == ("artifact_wrong_kind",)

    tampered_receipt = _receipt(previous[0], specs[0])
    object.__setattr__(tampered_receipt, "output_fingerprint", _fp("tampered-output"))
    tampered = SelectiveArtifactEvidence(
        segment_id=current[0].segment_id,
        status=ArtifactReuseStatus.REUSABLE,
        inspected_at_ms=100,
        receipt=tampered_receipt,
    )
    tampered_plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        artifact_evidence=(tampered,),
        job_specs=specs,
    )
    assert tampered_plan.decisions[0].reason_codes == ("artifact_tampered",)

    clean_workspace, clean_previous, clean_current, clean_specs = _fixture(
        count=2, changed_segment="segment.1", chain=False
    )
    initial = _evidence(clean_previous[1], clean_specs[1])
    clean_plan = build_selective_rerun_plan(
        clean_workspace,
        clean_previous,
        clean_current,
        artifact_evidence=(initial,),
        job_specs=clean_specs,
    )
    fresh_tampered_receipt = _receipt(clean_previous[1], clean_specs[1])
    object.__setattr__(fresh_tampered_receipt, "output_fingerprint", _fp("fresh-tampered"))
    fresh_tampered = replace(initial, receipt=fresh_tampered_receipt, inspected_at_ms=200)
    with pytest.raises(SelectiveRerunError, match="reuse_evidence_tampered"):
        build_approved_generation_sequence_plan(
            clean_plan,
            SelectiveRerunApproval(clean_plan.fingerprint, ("segment.1",)),
            workspace=clean_workspace,
            manifests=clean_current,
            job_specs=clean_specs,
            artifact_evidence=(fresh_tampered,),
            approval_inspected_at_ms=200,
        )


def test_chain_predecessor_identity_and_output_binding_are_checked() -> None:
    workspace, previous, current, specs = _fixture(count=2, chain=True)
    predecessor = _evidence(previous[0], specs[0])
    child = _evidence(previous[1], specs[1], predecessor_output=_fp("wrong-predecessor"))
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        artifact_evidence=(predecessor, child),
        job_specs=specs,
    )
    assert plan.reused_segment_ids == ("segment.1",)
    assert plan.queued_segment_ids == ("segment.2",)
    assert plan.decisions[1].reason_codes == ("artifact_incompatible",)


def test_approval_rechecks_fresh_store_status_and_expiry_boundary() -> None:
    workspace, previous, current, specs = _fixture(
        count=2, changed_segment="segment.1", chain=False
    )
    initial = _evidence(previous[1], specs[1])
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        artifact_evidence=(initial,),
        job_specs=specs,
    )
    approval = SelectiveRerunApproval(plan.fingerprint, ("segment.1",))
    with pytest.raises(SelectiveRerunError, match="reuse_approval_inspection_missing"):
        build_approved_generation_sequence_plan(
            plan,
            approval,
            workspace=workspace,
            manifests=current,
            job_specs=specs,
        )
    with pytest.raises(SelectiveRerunError, match="reuse_evidence_expired"):
        build_approved_generation_sequence_plan(
            plan,
            approval,
            workspace=workspace,
            manifests=current,
            job_specs=specs,
            artifact_evidence=(replace(initial, inspected_at_ms=10_000),),
            approval_inspected_at_ms=10_000,
        )
    with pytest.raises(SelectiveRerunError, match="reuse_evidence_not_at_approval"):
        build_approved_generation_sequence_plan(
            plan,
            approval,
            workspace=workspace,
            manifests=current,
            job_specs=specs,
            artifact_evidence=(replace(initial, inspected_at_ms=200),),
            approval_inspected_at_ms=201,
        )
    with pytest.raises(SelectiveRerunError, match="reuse_evidence_not_reusable"):
        build_approved_generation_sequence_plan(
            plan,
            approval,
            workspace=workspace,
            manifests=current,
            job_specs=specs,
            artifact_evidence=(
                replace(initial, status=ArtifactReuseStatus.STALE, inspected_at_ms=200),
            ),
            approval_inspected_at_ms=200,
        )


def test_selective_sequence_cancel_retry_reload_and_wire_privacy_are_bounded() -> None:
    workspace, previous, current, specs = _fixture(count=1)
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        requested_segment_ids=("segment.1",),
        job_specs=specs,
    )
    sequence = build_approved_generation_sequence_plan(
        plan,
        SelectiveRerunApproval(plan.fingerprint, ("segment.1",)),
        workspace=workspace,
        manifests=current,
        job_specs=specs,
    )
    assert len(canonical_bytes(plan.to_wire())) <= MAX_SELECTIVE_RERUN_WIRE_BYTES
    assert "prompt" not in plan.to_wire()
    assert "path" not in plan.to_wire()
    state = create_generation_sequence_state(sequence)
    cancelled = cancel_generation_sequence(state)
    retried = retry_generation_job(
        cancelled,
        "job.segment.1",
        transaction_id="transaction.segment.1.retry",
    )
    assert reconcile_generation_sequence(retried, retried) == retried
    projected = record_generation_projection(
        retried,
        "job.segment.1",
        transaction_id="transaction.segment.1.retry",
        graph_fingerprint=sequence.jobs[0].graph_fingerprint,
        compiled_prompt_fingerprint=sequence.jobs[0].compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    with pytest.raises(GenerationSequenceError, match="job_transition"):
        record_generation_projection(
            projected,
            "job.segment.1",
            transaction_id="transaction.segment.1.retry",
            graph_fingerprint=sequence.jobs[0].graph_fingerprint,
            compiled_prompt_fingerprint=sequence.jobs[0].compiled_prompt_fingerprint,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )


def test_approval_is_bound_to_current_plan_and_m17_08_rejects_duplicate_submission() -> None:
    workspace, previous, current, specs = _fixture(count=1)
    plan = build_selective_rerun_plan(
        workspace,
        previous,
        current,
        requested_segment_ids=("segment.1",),
        job_specs=specs,
    )
    with pytest.raises(SelectiveRerunError, match="approval_plan_mismatch"):
        build_approved_generation_sequence_plan(
            plan,
            SelectiveRerunApproval(_fp("wrong-plan"), ("segment.1",)),
            workspace=workspace,
            manifests=current,
            job_specs=specs,
        )

    sequence = build_approved_generation_sequence_plan(
        plan,
        SelectiveRerunApproval(plan.fingerprint, ("segment.1",)),
        workspace=workspace,
        manifests=current,
        job_specs=specs,
    )
    state = create_generation_sequence_state(sequence)
    job = state.plan.jobs[0]
    state = record_generation_projection(
        state,
        job.job_id,
        transaction_id="transaction.segment.1",
        graph_fingerprint=job.graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(state, job.job_id, queue_prompt_id="queue.segment.1")
    with pytest.raises(GenerationSequenceError, match="job_transition"):
        record_generation_submission(state, job.job_id, queue_prompt_id="queue.duplicate")
