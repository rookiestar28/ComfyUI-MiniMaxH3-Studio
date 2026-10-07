from __future__ import annotations

from dataclasses import replace

import pytest

from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.pipeline_transaction import (
    PipelineTransaction,
    PipelineTransactionError,
    PipelineTransactionState,
    cancel_pipeline_transaction,
    mark_pipeline_unknown_ownership,
    prepare_pipeline_transaction,
    reconcile_pipeline_transaction,
    record_pipeline_result,
    record_pipeline_running,
    record_pipeline_submission,
    retry_pipeline_transaction,
)
from comfyui_h3_context.core.recompute_closure import RecomputePlan, plan_recompute
from comfyui_h3_context.core.segment_workspace import (
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    create_workspace,
    derive_segment_manifests,
    revise_workspace,
)


def _fingerprint(character: str) -> str:
    return "sha256:" + character * 64


def _segment(
    segment_id: str,
    *,
    relation: SegmentRelationKind = SegmentRelationKind.INDEPENDENT,
    predecessor: str | None = None,
    settings_digest_char: str = "d",
) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=TaskMode.T2VA,
        source_id=f"source.{segment_id}",
        reference_ids=(),
        duration=SegmentDuration.from_frame_count(124),
        relation=relation,
        predecessor_segment_id=predecessor,
        accepted_intent_fingerprint=_fingerprint("a"),
        semantic_receipt_fingerprint=_fingerprint("b"),
        profile_fingerprint=_fingerprint("1"),
        reference_registry_fingerprint=_fingerprint("2"),
        native_binding_fingerprint=_fingerprint("c"),
        producer_settings_fingerprint=_fingerprint(settings_digest_char),
    )


def _authorities(
    segments: tuple[SegmentDeclaration, ...],
) -> tuple[AcceptedIntentAuthority, ...]:
    return tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in segments
    )


def _workspace_pair() -> tuple[MultiSegmentWorkspace, MultiSegmentWorkspace]:
    previous_segments = (
        _segment("s1"),
        _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
    )
    previous = create_workspace(
        "workspace.alpha",
        previous_segments,
        accepted_intent_authorities=_authorities(previous_segments),
    )
    current_segments = (
        _segment("s1", settings_digest_char="e"),
        previous_segments[1],
    )
    current = revise_workspace(
        previous,
        expected_workspace_fingerprint=previous.fingerprint,
        accepted_intent_authorities=_authorities(current_segments),
        segments=current_segments,
    )
    return previous, current


def _prepared() -> tuple[MultiSegmentWorkspace, RecomputePlan, PipelineTransaction]:
    previous, current = _workspace_pair()
    plan = plan_recompute(
        derive_segment_manifests(previous),
        derive_segment_manifests(current),
        requested_segment_ids=("s1", "s2"),
    )
    transaction = prepare_pipeline_transaction(
        workspace=current,
        manifests=derive_segment_manifests(current),
        recompute_plan=plan,
        transaction_id="tx.1",
        graph_fingerprint=_fingerprint("3"),
        compiled_prompt_fingerprint=_fingerprint("4"),
    )
    return current, plan, transaction


def test_clean_closure_is_explicit_noop_without_pipeline_materialization() -> None:
    _, current = _workspace_pair()
    manifests = derive_segment_manifests(current)
    clean = plan_recompute(manifests, manifests)

    transaction = prepare_pipeline_transaction(
        workspace=current,
        manifests=manifests,
        recompute_plan=clean,
        transaction_id="tx.clean",
    )

    assert transaction.state is PipelineTransactionState.NOOP_CLEAN
    assert transaction.dirty_segment_ids == ()
    assert transaction.graph_fingerprint is None
    assert transaction.compiled_prompt_fingerprint is None
    with pytest.raises(PipelineTransactionError, match="clean_receipts_forbidden"):
        prepare_pipeline_transaction(
            workspace=current,
            manifests=manifests,
            recompute_plan=clean,
            transaction_id="tx.bad-clean",
            graph_fingerprint=_fingerprint("3"),
            compiled_prompt_fingerprint=_fingerprint("4"),
        )


def test_dirty_closure_prepares_exact_existing_pipeline_identity() -> None:
    current, plan, transaction = _prepared()

    assert transaction.state is PipelineTransactionState.PREPARED
    assert transaction.workspace_fingerprint == current.fingerprint
    assert transaction.dirty_segment_ids == plan.mandatory_segment_ids == ("s1", "s2")
    assert transaction.graph_fingerprint == _fingerprint("3")
    assert transaction.compiled_prompt_fingerprint == _fingerprint("4")
    assert transaction.queue_prompt_id is None


def test_one_prepared_transaction_authorizes_at_most_one_submission() -> None:
    _, _, prepared = _prepared()
    submitted = record_pipeline_submission(
        prepared,
        expected_transaction_fingerprint=prepared.fingerprint,
        queue_prompt_id="prompt.1",
    )

    assert submitted.state is PipelineTransactionState.SUBMITTED
    assert submitted.queue_prompt_id == "prompt.1"
    with pytest.raises(PipelineTransactionError, match="duplicate_submission"):
        record_pipeline_submission(
            submitted,
            expected_transaction_fingerprint=submitted.fingerprint,
            queue_prompt_id="prompt.2",
        )


def test_projection_submission_running_and_result_are_distinct_states() -> None:
    _, _, prepared = _prepared()
    submitted = record_pipeline_submission(
        prepared,
        expected_transaction_fingerprint=prepared.fingerprint,
        queue_prompt_id="prompt.1",
    )
    running = record_pipeline_running(
        submitted,
        expected_transaction_fingerprint=submitted.fingerprint,
        host_owner_id="host.execution.1",
    )
    succeeded = record_pipeline_result(
        running,
        expected_transaction_fingerprint=running.fingerprint,
        result_fingerprint=_fingerprint("5"),
        succeeded=True,
    )

    assert [prepared.state, submitted.state, running.state, succeeded.state] == [
        PipelineTransactionState.PREPARED,
        PipelineTransactionState.SUBMITTED,
        PipelineTransactionState.RUNNING,
        PipelineTransactionState.SUCCEEDED,
    ]
    assert succeeded.result_fingerprint == _fingerprint("5")


def test_stale_or_late_transition_cannot_overwrite_newer_truth() -> None:
    _, _, prepared = _prepared()
    submitted = record_pipeline_submission(
        prepared,
        expected_transaction_fingerprint=prepared.fingerprint,
        queue_prompt_id="prompt.1",
    )
    with pytest.raises(PipelineTransactionError, match="stale_transaction"):
        record_pipeline_result(
            submitted,
            expected_transaction_fingerprint=prepared.fingerprint,
            result_fingerprint=_fingerprint("5"),
            succeeded=True,
        )

    running = record_pipeline_running(
        submitted,
        expected_transaction_fingerprint=submitted.fingerprint,
        host_owner_id="host.execution.1",
    )
    succeeded = record_pipeline_result(
        running,
        expected_transaction_fingerprint=running.fingerprint,
        result_fingerprint=_fingerprint("5"),
        succeeded=True,
    )
    with pytest.raises(PipelineTransactionError, match="terminal_transaction"):
        record_pipeline_running(
            succeeded,
            expected_transaction_fingerprint=succeeded.fingerprint,
            host_owner_id="host.execution.late",
        )


def test_cancellation_preserves_the_host_ownership_boundary() -> None:
    _, _, prepared = _prepared()
    cancelled = cancel_pipeline_transaction(
        prepared,
        expected_transaction_fingerprint=prepared.fingerprint,
    )
    assert cancelled.state is PipelineTransactionState.CANCELLED
    assert cancelled.queue_prompt_id is None

    _, _, second = _prepared()
    submitted = record_pipeline_submission(
        second,
        expected_transaction_fingerprint=second.fingerprint,
        queue_prompt_id="prompt.1",
    )
    requested = cancel_pipeline_transaction(
        submitted,
        expected_transaction_fingerprint=submitted.fingerprint,
    )
    assert requested.state is PipelineTransactionState.SUBMITTED
    assert requested.cancellation_requested is True
    assert requested.queue_prompt_id == "prompt.1"

    unknown = mark_pipeline_unknown_ownership(
        requested,
        expected_transaction_fingerprint=requested.fingerprint,
    )
    assert unknown.state is PipelineTransactionState.UNKNOWN_OWNERSHIP
    assert unknown.queue_prompt_id == "prompt.1"
    resolved = record_pipeline_result(
        unknown,
        expected_transaction_fingerprint=unknown.fingerprint,
        result_fingerprint=_fingerprint("5"),
        succeeded=True,
    )
    assert resolved.state is PipelineTransactionState.SUCCEEDED


def test_retry_and_remount_are_monotonic_and_nonduplicating() -> None:
    _, _, prepared = _prepared()
    submitted = record_pipeline_submission(
        prepared,
        expected_transaction_fingerprint=prepared.fingerprint,
        queue_prompt_id="prompt.1",
    )
    assert reconcile_pipeline_transaction(prepared, submitted) == submitted
    with pytest.raises(PipelineTransactionError, match="stale_remount"):
        reconcile_pipeline_transaction(submitted, prepared)

    cancelled = cancel_pipeline_transaction(
        prepared,
        expected_transaction_fingerprint=prepared.fingerprint,
    )
    retry = retry_pipeline_transaction(
        cancelled,
        expected_transaction_fingerprint=cancelled.fingerprint,
        transaction_id="tx.2",
    )

    assert retry.state is PipelineTransactionState.PREPARED
    assert retry.attempt == 2
    assert retry.queue_prompt_id is None
    assert reconcile_pipeline_transaction(cancelled, retry) == retry
    with pytest.raises(PipelineTransactionError, match="stale_remount"):
        reconcile_pipeline_transaction(retry, cancelled)

    _, _, tampered = _prepared()
    object.__setattr__(tampered, "graph_fingerprint", _fingerprint("9"))
    with pytest.raises(PipelineTransactionError, match="tampered_transaction"):
        cancel_pipeline_transaction(
            tampered,
            expected_transaction_fingerprint=tampered.fingerprint,
        )


def test_blocked_closure_never_submits_and_public_identity_is_content_free() -> None:
    previous, current = _workspace_pair()
    current_manifests = derive_segment_manifests(current)
    full = plan_recompute(derive_segment_manifests(previous)[:1], current_manifests)
    blocked = prepare_pipeline_transaction(
        workspace=current,
        manifests=current_manifests,
        recompute_plan=full,
        transaction_id="tx.blocked",
    )

    assert blocked.state is PipelineTransactionState.BLOCKED
    with pytest.raises(PipelineTransactionError, match="not_submittable"):
        record_pipeline_submission(
            blocked,
            expected_transaction_fingerprint=blocked.fingerprint,
            queue_prompt_id="prompt.forbidden",
        )
    encoded = str(blocked.to_public_dict()).lower()
    for forbidden in ("prompt text", "media_path", "credential", "provider", "http://"):
        assert forbidden not in encoded

    tampered_manifest = replace(
        current_manifests[0],
        manifest_fingerprint=None,
        workspace_fingerprint=_fingerprint("9"),
    )
    with pytest.raises(PipelineTransactionError, match="manifest_authority_mismatch"):
        prepare_pipeline_transaction(
            workspace=current,
            manifests=(tampered_manifest, current_manifests[1]),
            recompute_plan=full,
            transaction_id="tx.tampered",
        )
