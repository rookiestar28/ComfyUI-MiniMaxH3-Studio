"""Managed serial recovery must preserve failure and ownership evidence."""

from dataclasses import replace
from typing import Literal

import pytest
from test_m26_04_managed_sequence import _bound, _fp, _started

from comfyui_h3_context.core import managed_sequence as core


def _retry_ready(count: int) -> core.ManagedSequenceV1:
    sequence = _started(count)
    prepared = core.prepare_sequence_child(
        sequence,
        segment_id="segment.1",
        expected_revision=sequence.revision,
        materialization_receipt_fingerprint=_fp("materialization.1"),
        predecessor_terminal_fingerprint=None,
        request_id="prepare.initial.failure",
    )
    failed = core.fail_prepared_sequence_child(
        prepared.sequence,
        segment_id="segment.1",
        expected_revision=prepared.sequence.revision,
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        failure_fingerprint=_fp("initial.compile.failure"),
    )
    return core.retry_managed_sequence_segment(
        failed,
        segment_id="segment.1",
        expected_revision=failed.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.segment.1"),
    )


def _attachment_roundtrip(sequence: core.ManagedSequenceV1) -> core.ManagedSequenceV1:
    detached = core.detach_managed_sequence_client(sequence, expected_revision=sequence.revision)
    canvas = sequence.canvas_authority
    return core.resume_managed_sequence(
        detached,
        expected_revision=detached.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=(
            _fp("owned.segment.1") if canvas is None else canvas.owned_projection_fingerprint
        ),
    )


def _finish_full_retry(
    sequence: core.ManagedSequenceV1,
    kind: Literal["success", "error"] = "success",
) -> core.ManagedSequenceV1:
    bound = _bound(sequence)
    written = core.record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.retry.written"),
    )
    submitted = core.record_managed_sequence_submission(
        written,
        segment_id="segment.1",
        expected_revision=written.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("retry.submitted"),
        queue_prompt_id="prompt.retry",
        timeout_ms=60_000,
        now=100.0,
    )
    running = core.record_managed_sequence_running(
        submitted,
        segment_id="segment.1",
        expected_revision=submitted.revision,
        queue_prompt_id="prompt.retry",
        child_state_fingerprint=_fp("retry.running"),
        now=101.0,
    )
    artifact = core.record_managed_sequence_artifact(
        running,
        segment_id="segment.1",
        expected_revision=running.revision,
        queue_prompt_id="prompt.retry",
        artifact_receipt_fingerprint=_fp("retry.artifact"),
        artifact_bytes=1024,
    )
    return core.record_managed_sequence_terminal(
        artifact,
        segment_id="segment.1",
        expected_revision=artifact.revision,
        queue_prompt_id="prompt.retry",
        kind=kind,
        terminal_fingerprint=_fp("retry.terminal"),
    )


@pytest.mark.parametrize("count", [2, core.MAX_MANAGED_SEQUENCE_SEGMENTS])
@pytest.mark.parametrize("kind", ["success", "error"])
def test_retry_attachment_roundtrip_preserves_cancel_after_the_complete_retry(
    count: int,
    kind: Literal["success", "error"],
) -> None:
    retried = _retry_ready(count)
    resumed = _attachment_roundtrip(retried)
    completed = _finish_full_retry(resumed, kind)
    cancelled = core.cancel_managed_sequence(completed, expected_revision=completed.revision)
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED
    assert completed.ledger_reservation is not None
    assert completed.ledger_reservation.recovery_rows_consumed <= 7


def test_attachment_after_full_retry_does_not_require_the_reserved_cancel_row() -> None:
    completed = _finish_full_retry(_retry_ready(2))
    resumed = _attachment_roundtrip(completed)
    cancelled = core.cancel_managed_sequence(resumed, expected_revision=resumed.revision)
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED


def test_pure_attachment_churn_keeps_final_cancel_row() -> None:
    sequence = _started(1)
    for _ in range(40):
        try:
            sequence = core.detach_managed_sequence_client(
                sequence,
                expected_revision=sequence.revision,
            )
            sequence = core.resume_managed_sequence(
                sequence,
                expected_revision=sequence.revision,
                active_workflow_fingerprint=_fp("workflow.current"),
                owned_projection_fingerprint=_fp("owned.segment.1"),
            )
        except core.ManagedSequenceError:
            pass
    cancelled = core.cancel_managed_sequence(sequence, expected_revision=sequence.revision)
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("detach_first", [False, True])
def test_failure_and_detach_order_never_turns_resume_into_retry(
    count: int, detach_first: bool
) -> None:
    sequence = _started(count)
    prepared = core.prepare_sequence_child(
        sequence,
        segment_id="segment.1",
        expected_revision=sequence.revision,
        materialization_receipt_fingerprint=_fp("materialization.1"),
        predecessor_terminal_fingerprint=None,
        request_id="prepare.recovery.1",
    )
    sequence = prepared.sequence
    if detach_first:
        sequence = core.detach_managed_sequence_client(
            sequence, expected_revision=sequence.revision
        )
    sequence = core.fail_prepared_sequence_child(
        sequence,
        segment_id="segment.1",
        expected_revision=sequence.revision,
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        failure_fingerprint=_fp("compile.failure"),
    )
    if not detach_first:
        sequence = core.detach_managed_sequence_client(
            sequence, expected_revision=sequence.revision
        )

    assert sequence.state is core.ManagedSequenceStateV1.PAUSED_FAILURE
    assert sequence.client_attached is False
    assert sequence.slots[0].state is core.SequenceSlotStateV1.FAILED
    assert all(row.state is core.SequenceSlotStateV1.PENDING for row in sequence.slots[1:])
    with pytest.raises(core.ManagedSequenceError, match="sequence_not_resumable"):
        core.resume_managed_sequence(
            sequence,
            expected_revision=sequence.revision,
            active_workflow_fingerprint=_fp("workflow.current"),
            owned_projection_fingerprint=_fp("owned.segment.1"),
        )
    retried = core.retry_managed_sequence_segment(
        sequence,
        segment_id="segment.1",
        expected_revision=sequence.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.segment.1"),
    )
    assert retried.state is core.ManagedSequenceStateV1.ACTIVE
    assert retried.client_attached is True
    assert retried.retry_consumed is True
    assert retried.slots[0].attempt_epoch == sequence.slots[0].attempt_epoch + 1


def test_detach_preserves_canvas_drift_hold() -> None:
    sequence = _bound(_started(1))
    drifted = core.check_managed_sequence_canvas_authority(
        sequence,
        expected_revision=sequence.revision,
        active_workflow_fingerprint=_fp("workflow.foreign"),
        owned_projection_fingerprint=_fp("owned.segment.1"),
    )
    assert drifted.state is core.ManagedSequenceStateV1.PAUSED_CANVAS_DRIFT
    detached = core.detach_managed_sequence_client(drifted, expected_revision=drifted.revision)
    assert detached.state is core.ManagedSequenceStateV1.PAUSED_CANVAS_DRIFT
    assert detached.client_attached is False


def test_successful_parent_requires_successful_slots() -> None:
    sequence = _started(1)
    with pytest.raises(core.ManagedSequenceError, match="successful_slot_proof"):
        replace(sequence, state=core.ManagedSequenceStateV1.SUCCEEDED)
