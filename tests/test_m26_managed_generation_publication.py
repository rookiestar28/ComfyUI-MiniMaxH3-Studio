from __future__ import annotations

from dataclasses import replace

import pytest
from test_generation_sequence import _fp, _segment, _spec, _workspace

from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.generation_sequence import (
    GenerationSequenceError,
    GenerationSequenceState,
    build_generation_sequence_plan,
    cancel_generation_sequence,
    create_generation_sequence_state,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
)
from comfyui_h3_context.core.managed_generation_publication import (
    compose_completed_managed_generation,
)
from comfyui_h3_context.core.recompute_closure import extend_recompute_plan, plan_recompute
from comfyui_h3_context.core.segment_artifacts import (
    SegmentArtifactReceipt,
    begin_segment_artifact_receipt,
    complete_segment_artifact_receipt,
)
from comfyui_h3_context.core.segment_workspace import (
    MultiSegmentWorkspace,
    SegmentRelationKind,
    derive_segment_manifests,
)
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation


def _completed_children() -> tuple[
    MultiSegmentWorkspace,
    tuple[GenerationSequenceState, ...],
    tuple[SegmentArtifactReceipt, ...],
]:
    workspace = _workspace(
        tuple(
            _segment(
                f"segment.{index}",
                TaskMode.T2VA,
                settings=f"settings.{index}",
                relation=SegmentRelationKind.INDEPENDENT if index == 1 else SegmentRelationKind.CUT,
            )
            for index in (1, 2)
        )
    )
    manifests = derive_segment_manifests(workspace)
    states = []
    receipts = []
    for manifest in manifests:
        recompute = extend_recompute_plan(
            manifests,
            plan_recompute(manifests, manifests),
            forced_dirty_segment_ids=(manifest.segment_id,),
        )
        plan = build_generation_sequence_plan(
            workspace, manifests, recompute, (_spec(manifest.segment_id),)
        )
        state = create_generation_sequence_state(plan)
        job = plan.jobs[0]
        state = record_generation_projection(
            state,
            job.job_id,
            transaction_id=f"transaction.{job.segment_id}",
            # Whole-graph surroundings can differ from the compiled execution identity.
            graph_fingerprint=_fp(f"observed.graph.{job.segment_id}"),
            compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
            fingerprint_domain=job.fingerprint_domain,
        )
        state = record_generation_submission(
            state, job.job_id, queue_prompt_id=f"queue.{job.segment_id}"
        )
        state = record_generation_running(state, job.job_id, host_owner_id="host.owner")
        transaction = state.runtimes[0].transaction
        assert transaction is not None
        receipt = complete_segment_artifact_receipt(
            begin_segment_artifact_receipt(
                manifest=manifest,
                transaction=transaction,
                artifact_id=f"artifact.{job.segment_id}",
                model_fingerprint=job.model_fingerprint,
                runtime_fingerprint=job.runtime_fingerprint,
                execution_fingerprint=_fp(f"execution.{job.segment_id}"),
                predecessor_artifact_fingerprint=None,
                format_label=job.expected_format,
                shape=job.expected_shape,
                created_at_ms=100,
                expires_at_ms=10_000,
            ),
            output_fingerprint=_fp(f"output.{job.segment_id}"),
            byte_length=1024,
        )
        states.append(
            record_generation_success(
                state,
                job.job_id,
                result_fingerprint=_fp(f"result.{job.segment_id}"),
                receipt=receipt,
            )
        )
        receipts.append(receipt)
    return workspace, tuple(states), tuple(receipts)


def _correlation() -> ExecutionCorrelation:
    return ExecutionCorrelation("queue.segment.2", "sink.2")


def test_terminal_join_retains_real_children_receipts_and_original_authority() -> None:
    workspace, children, receipts = _completed_children()
    before = tuple(item.to_wire() for item in children)
    correlation = _correlation()
    projection = compose_completed_managed_generation(workspace, children, receipts, correlation)
    repeated = compose_completed_managed_generation(workspace, children, receipts, correlation)

    assert projection.to_wire() == repeated.to_wire()
    assert projection.state.complete
    assert projection.eligible_commands == ()
    assert projection.correlation is correlation
    assert projection.state.plan.source_workspace_authority is workspace
    assert projection.state.plan.clean_segment_ids == ()
    assert projection.state.observation_count == sum(item.observation_count for item in children)
    for ordinal, child in enumerate(children):
        assert projection.state.plan.jobs[ordinal] is child.plan.jobs[0]
        runtime = projection.state.runtimes[ordinal]
        assert runtime is child.runtimes[0]
        transaction = runtime.transaction
        assert transaction is not None
        assert transaction.recompute_plan_fingerprint == child.plan.recompute_plan_fingerprint
        assert (
            transaction.recompute_plan_fingerprint
            != projection.state.plan.recompute_plan_fingerprint
        )
        assert runtime.artifact_receipt_fingerprint == receipts[ordinal].fingerprint
        assert receipts[ordinal].transaction_fingerprint != transaction.fingerprint
    assert tuple(item.to_wire() for item in children) == before


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reordered", "receipts_reordered"])
def test_terminal_join_rejects_missing_duplicate_or_reordered_originals(mutation: str) -> None:
    workspace, children, receipts = _completed_children()
    if mutation == "missing":
        children, receipts = children[:1], receipts[:1]
    elif mutation == "duplicate":
        children, receipts = (children[0], children[0]), (receipts[0], receipts[0])
    elif mutation == "reordered":
        children, receipts = tuple(reversed(children)), tuple(reversed(receipts))
    else:
        receipts = tuple(reversed(receipts))
    with pytest.raises(GenerationSequenceError, match="managed_publication"):
        compose_completed_managed_generation(workspace, children, receipts, _correlation())


def test_equal_reconstructed_workspace_is_not_the_retained_authority() -> None:
    workspace, children, receipts = _completed_children()
    equal_copy = replace(workspace)
    assert equal_copy == workspace and equal_copy is not workspace
    with pytest.raises(GenerationSequenceError, match="child_authority"):
        compose_completed_managed_generation(equal_copy, children, receipts, _correlation())


@pytest.mark.parametrize("cancelled", [False, True])
def test_only_successful_children_can_be_published(cancelled: bool) -> None:
    workspace, children, receipts = _completed_children()
    unfinished = create_generation_sequence_state(children[0].plan)
    if cancelled:
        unfinished = cancel_generation_sequence(unfinished)
        assert unfinished.complete
    with pytest.raises(GenerationSequenceError, match="managed_publication"):
        compose_completed_managed_generation(
            workspace, (unfinished, children[1]), receipts, _correlation()
        )


@pytest.mark.parametrize("field", ["recompute_plan_fingerprint", "compiled_prompt_fingerprint"])
def test_mismatched_child_transaction_cannot_be_relabelled_as_parent_success(field: str) -> None:
    workspace, children, receipts = _completed_children()
    runtime = children[0].runtimes[0]
    assert runtime.transaction is not None
    transaction = (
        replace(
            runtime.transaction,
            recompute_plan_fingerprint=_fp("different"),
            transaction_fingerprint=None,
        )
        if field == "recompute_plan_fingerprint"
        else replace(
            runtime.transaction,
            compiled_prompt_fingerprint=_fp("different"),
            transaction_fingerprint=None,
        )
    )
    changed = replace(
        children[0],
        runtimes=(replace(runtime, transaction=transaction),),
        state_fingerprint=None,
    )
    with pytest.raises(GenerationSequenceError, match="child_completion"):
        compose_completed_managed_generation(
            workspace, (changed, children[1]), receipts, _correlation()
        )


def test_changed_receipt_and_duplicate_queue_are_rejected() -> None:
    workspace, children, receipts = _completed_children()
    changed_receipt = replace(receipts[0], byte_length=2048, receipt_fingerprint=None)
    with pytest.raises(GenerationSequenceError, match="receipt_identity"):
        compose_completed_managed_generation(
            workspace, children, (changed_receipt, receipts[1]), _correlation()
        )
    first = children[0].runtimes[0].transaction
    runtime = children[1].runtimes[0]
    assert first is not None and runtime.transaction is not None
    changed = replace(
        children[1],
        runtimes=(
            replace(
                runtime,
                transaction=replace(
                    runtime.transaction,
                    queue_prompt_id=first.queue_prompt_id,
                    transaction_fingerprint=None,
                ),
            ),
        ),
        state_fingerprint=None,
    )
    with pytest.raises(GenerationSequenceError, match="duplicate_execution"):
        compose_completed_managed_generation(
            workspace, (children[0], changed), receipts, _correlation()
        )


def test_join_does_not_widen_the_observation_budget() -> None:
    workspace, children, receipts = _completed_children()
    crowded = tuple(
        replace(item, observation_count=4096, state_fingerprint=None) for item in children
    )
    with pytest.raises(GenerationSequenceError, match="observation_limit"):
        compose_completed_managed_generation(workspace, crowded, receipts, _correlation())
