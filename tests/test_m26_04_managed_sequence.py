"""M26-04 pure managed serial-sequence authority and transition contract."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.managed_sequence import (
    ARTIFACT_STORE_TOTAL_BYTES,
    MANAGED_SEQUENCE_ABSOLUTE_SECONDS,
    MANAGED_SEQUENCE_IDLE_SECONDS,
    MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS,
    MAX_MANAGED_SEQUENCE_SEGMENTS,
    ArtifactCapacityReservationV1,
    ManagedModeQualificationV1,
    ManagedSequenceAuthorizationV1,
    ManagedSequenceError,
    ManagedSequenceSegmentAuthorizationV1,
    ManagedSequenceStateV1,
    ManagedSequenceV1,
    SequenceSlotStateV1,
    authorize_managed_sequence,
    bind_prepared_child,
    cancel_managed_sequence,
    check_managed_sequence_canvas_authority,
    coordinator_ledger_rows,
    detach_managed_sequence_client,
    expire_managed_sequence_lease,
    fail_bound_sequence_child,
    fail_prepared_sequence_child,
    mark_managed_sequence_invocation_unknown,
    mark_managed_sequence_unknown_ownership,
    prepare_sequence_child,
    read_managed_sequence_projection,
    record_managed_sequence_artifact,
    record_managed_sequence_canvas_rollback,
    record_managed_sequence_canvas_write,
    record_managed_sequence_reuse,
    record_managed_sequence_running,
    record_managed_sequence_submission,
    record_managed_sequence_terminal,
    resume_managed_sequence,
    retry_managed_sequence_segment,
    start_managed_sequence,
)


def _fp(label: str) -> str:
    return f"sha256:{sha256(label.encode('ascii')).hexdigest()}"


def _authorization(count: int = 3) -> ManagedSequenceAuthorizationV1:
    modes = (
        TaskMode.T2VA,
        TaskMode.I2VA,
        TaskMode.FL2VA,
        TaskMode.L2VA,
        TaskMode.REF2VA,
    )
    segments = tuple(
        ManagedSequenceSegmentAuthorizationV1(
            segment_id=f"segment.{index + 1}",
            ordinal=index + 1,
            task_mode=modes[index % len(modes)],
            duration_milliseconds=4_000,
            frame_count=100,
            manifest_fingerprint=_fp(f"manifest.{index + 1}"),
            materialization_receipt_fingerprint=_fp(f"materialization.{index + 1}"),
            local_prompt_fingerprint=_fp(f"prompt.{index + 1}"),
            intent_graph_fingerprint=_fp(f"intent.{index + 1}"),
            profile_fingerprint=_fp(f"profile.{index + 1}"),
            capability_fingerprint=_fp("capability.current"),
            predecessor_segment_id=(None if index == 0 else f"segment.{index}"),
            predecessor_cut_receipt_fingerprint=(
                None if index == 0 else _fp(f"cut.{index}.{index + 1}")
            ),
        )
        for index in range(count)
    )
    return ManagedSequenceAuthorizationV1(
        sequence_id="managed.sequence.1",
        workspace_id="production.workspace.1",
        workspace_revision=7,
        workspace_fingerprint=_fp("workspace.current"),
        production_plan_fingerprint=_fp("production.plan"),
        proposal_fingerprint=_fp("proposal.approved"),
        generation_plan_fingerprint=_fp("generation.plan"),
        compiler_fingerprint=_fp("compiler.current"),
        host_capability_fingerprint=_fp("capability.current"),
        segments=segments,
        max_concurrency=1,
        explicit_intent="generate_approved_sequence",
    )


def _qualification(
    authority: ManagedSequenceAuthorizationV1,
    *,
    include_all_global_modes: bool = True,
) -> ManagedModeQualificationV1:
    modes = tuple(TaskMode) if include_all_global_modes else (TaskMode.T2VA,)
    return ManagedModeQualificationV1(
        host_capability_fingerprint=authority.host_capability_fingerprint,
        compiler_fingerprint=authority.compiler_fingerprint,
        qualified_global_modes=modes,
        qualified_materialization_receipts=tuple(
            segment.materialization_receipt_fingerprint for segment in authority.segments
        ),
        observed_at=100.0,
        expires_at=1_000.0,
    )


def _started(count: int = 3) -> ManagedSequenceV1:
    authority = _authorization(count)
    qualification = _qualification(authority)
    sequence = authorize_managed_sequence(authority, now=10.0)
    return start_managed_sequence(sequence, qualification, now=20.0)


def _bound(sequence: ManagedSequenceV1, segment_id: str = "segment.1") -> ManagedSequenceV1:
    prepared = prepare_sequence_child(
        sequence,
        segment_id=segment_id,
        expected_revision=sequence.revision,
        materialization_receipt_fingerprint=next(
            row.materialization_receipt_fingerprint
            for row in sequence.authorization.segments
            if row.segment_id == segment_id
        ),
        predecessor_terminal_fingerprint=(
            None if segment_id == "segment.1" else _fp(f"terminal.{segment_id}")
        ),
        request_id=f"prepare.{segment_id}",
    )
    return bind_prepared_child(
        prepared.sequence,
        segment_id=segment_id,
        expected_revision=prepared.sequence.revision,
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp(f"child.{segment_id}.prepared"),
        graph_fingerprint=_fp(f"graph.{segment_id}"),
        compiled_prompt_fingerprint=_fp(f"compiled.{segment_id}"),
        previous_owned_projection_fingerprint=_fp(f"owned.{segment_id}"),
        active_workflow_fingerprint=_fp("workflow.current"),
    )


def test_authorization_is_bounded_serial_and_content_free() -> None:
    authority = _authorization(MAX_MANAGED_SEQUENCE_SEGMENTS)

    assert authority.max_concurrency == 1
    assert len(authority.segments) == 15
    assert "prompt." not in str(authority.to_wire())
    with pytest.raises(ManagedSequenceError, match="segment_limit"):
        replace(authority, segments=authority.segments + (authority.segments[-1],))
    with pytest.raises(ManagedSequenceError, match="concurrency"):
        replace(authority, max_concurrency=2)


def test_ledger_and_artifact_capacity_are_reserved_for_the_whole_parent() -> None:
    for count in range(1, MAX_MANAGED_SEQUENCE_SEGMENTS + 1):
        assert coordinator_ledger_rows(count) == 2 + (7 * count) + 8
        sequence = _started(count)
        assert sequence.ledger_reservation is not None
        assert sequence.ledger_reservation.reserved_rows == 2 + (7 * count) + 8
        assert sequence.ledger_reservation.reserved_bytes == coordinator_ledger_rows(count) * 8192
        expected_per_artifact = min(128 * 1024 * 1024, ARTIFACT_STORE_TOTAL_BYTES // count)
        assert sequence.artifact_reservation == ArtifactCapacityReservationV1(
            parent_sequence_id=sequence.authorization.sequence_id,
            parent_authorization_fingerprint=sequence.authorization.fingerprint,
            segment_count=count,
            reserved_entries=count,
            per_artifact_bytes=expected_per_artifact,
            reserved_bytes=expected_per_artifact * count,
            consumed_entries=0,
            consumed_bytes=0,
            released=False,
            expires_at=20.0 + MANAGED_SEQUENCE_ABSOLUTE_SECONDS,
        )


def test_start_requires_current_complete_qualification_and_exact_intent() -> None:
    authority = _authorization()
    authorized = authorize_managed_sequence(authority, now=10.0)

    with pytest.raises(ManagedSequenceError, match="qualification_incomplete"):
        start_managed_sequence(
            authorized,
            _qualification(authority, include_all_global_modes=False),
            now=20.0,
        )
    with pytest.raises(ManagedSequenceError, match="qualification_expired"):
        start_managed_sequence(authorized, _qualification(authority), now=1_001.0)
    with pytest.raises(ManagedSequenceError, match="authorization_drift"):
        start_managed_sequence(
            authorized,
            replace(
                _qualification(authority),
                host_capability_fingerprint=_fp("capability.changed"),
            ),
            now=20.0,
        )

    started = start_managed_sequence(authorized, _qualification(authority), now=20.0)
    assert started.state is ManagedSequenceStateV1.ACTIVE
    assert [slot.state for slot in started.slots] == [
        SequenceSlotStateV1.ELIGIBLE,
        SequenceSlotStateV1.PENDING,
        SequenceSlotStateV1.PENDING,
    ]
    assert started.lease is not None
    assert started.lease.idle_deadline == 20.0 + MANAGED_SEQUENCE_IDLE_SECONDS
    assert started.lease.absolute_deadline == 20.0 + MANAGED_SEQUENCE_ABSOLUTE_SECONDS


def test_prepare_builds_one_job_without_predecessor_artifact_or_graph_inputs() -> None:
    sequence = _started()

    prepared = prepare_sequence_child(
        sequence,
        segment_id="segment.1",
        expected_revision=sequence.revision,
        materialization_receipt_fingerprint=_fp("materialization.1"),
        predecessor_terminal_fingerprint=None,
        request_id="prepare.segment.1",
    )

    assert prepared.execution.job_count == 1
    assert prepared.execution.segment_id == "segment.1"
    wire = prepared.execution.to_wire()
    assert all("artifact" not in key and "graph" not in key for key in wire)
    assert prepared.sequence.slots[0].state is SequenceSlotStateV1.PREPARED
    assert prepared.sequence.slots[1].state is SequenceSlotStateV1.PENDING


def test_prepare_rejects_full_parent_or_ineligible_successor() -> None:
    sequence = _started()

    with pytest.raises(ManagedSequenceError, match="segment_not_eligible"):
        prepare_sequence_child(
            sequence,
            segment_id="segment.2",
            expected_revision=sequence.revision,
            materialization_receipt_fingerprint=_fp("materialization.2"),
            predecessor_terminal_fingerprint=_fp("terminal.1"),
            request_id="prepare.segment.2",
        )
    with pytest.raises(ManagedSequenceError, match="stale_parent"):
        prepare_sequence_child(
            sequence,
            segment_id="segment.1",
            expected_revision=sequence.revision + 1,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            predecessor_terminal_fingerprint=None,
            request_id="prepare.segment.1.stale",
        )


def test_prepared_child_failure_releases_live_slot_into_explicit_retry_state() -> None:
    started = _started()
    prepared = prepare_sequence_child(
        started,
        segment_id="segment.1",
        expected_revision=started.revision,
        materialization_receipt_fingerprint=_fp("materialization.1"),
        predecessor_terminal_fingerprint=None,
        request_id="prepare.segment.1",
    )

    failed = fail_prepared_sequence_child(
        prepared.sequence,
        segment_id="segment.1",
        expected_revision=prepared.sequence.revision,
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        failure_fingerprint=_fp("bind.failed"),
    )

    assert failed.state is ManagedSequenceStateV1.PAUSED_FAILURE
    assert failed.active_segment_id is None
    assert failed.slots[0].state is SequenceSlotStateV1.FAILED
    assert failed.slots[0].terminal_fingerprint == _fp("bind.failed")
    retried = retry_managed_sequence_segment(
        failed,
        segment_id="segment.1",
        expected_revision=failed.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    assert retried.slots[0].state is SequenceSlotStateV1.ELIGIBLE


def test_bound_child_failure_and_unacknowledged_invocation_are_distinct() -> None:
    bound = _bound(_started())
    failed = fail_bound_sequence_child(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        failure_fingerprint=_fp("queue.rejected"),
    )
    assert failed.state is ManagedSequenceStateV1.PAUSED_FAILURE
    assert failed.slots[0].state is SequenceSlotStateV1.FAILED
    assert failed.slots[0].queue_prompt_id is None

    written = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    unknown = mark_managed_sequence_invocation_unknown(
        written,
        segment_id="segment.1",
        expected_revision=written.revision,
        child_run_handle="mc_" + "a" * 40,
        timeout_ms=60_000,
        now=100.0,
    )
    assert unknown.state is ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP
    assert unknown.slots[0].state is SequenceSlotStateV1.UNKNOWN_OWNERSHIP
    assert unknown.slots[0].queue_prompt_id is None
    assert unknown.lease is not None
    assert unknown.lease.active_deadline == (
        100.0 + 60.0 + MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS
    )


def test_exact_reuse_skips_canvas_and_queue_while_unlocking_successor() -> None:
    started = _started(2)

    reused = record_managed_sequence_reuse(
        started,
        segment_id="segment.1",
        expected_revision=started.revision,
        artifact_receipt_fingerprint=_fp("artifact.reused.1"),
        artifact_bytes=1_024,
        terminal_fingerprint=_fp("terminal.reused.1"),
    )

    assert reused.slots[0].state is SequenceSlotStateV1.REUSED
    assert reused.slots[0].queue_prompt_id is None
    assert reused.slots[0].canvas_write_fingerprint is None
    assert reused.slots[1].state is SequenceSlotStateV1.ELIGIBLE
    assert reused.active_segment_id is None
    assert reused.canvas_authority is None
    assert reused.artifact_reservation is not None
    assert reused.artifact_reservation.consumed_entries == 1
    assert reused.artifact_reservation.consumed_bytes == 1_024


def test_bind_captures_canvas_and_child_authority_before_any_submission() -> None:
    bound = _bound(_started())
    slot = bound.slots[0]

    assert slot.state is SequenceSlotStateV1.BOUND
    assert slot.child_run_handle == "mc_" + "a" * 40
    assert slot.queue_prompt_id is None
    assert bound.canvas_authority is not None
    assert bound.canvas_authority.owned_write_count == 0


def test_canvas_cas_pauses_before_drift_and_counts_one_exact_owned_write() -> None:
    bound = _bound(_started())

    drifted = check_managed_sequence_canvas_authority(
        bound,
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.foreign"),
    )
    assert drifted.state is ManagedSequenceStateV1.PAUSED_CANVAS_DRIFT
    assert drifted.canvas_authority is not None
    assert drifted.canvas_authority.owned_write_count == 0

    checked = check_managed_sequence_canvas_authority(
        bound,
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.segment.1"),
    )
    assert checked is bound
    written = record_managed_sequence_canvas_write(
        checked,
        segment_id="segment.1",
        expected_revision=checked.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    assert written.canvas_authority is not None
    assert written.canvas_authority.owned_write_count == 1
    assert written.canvas_authority.owned_projection_fingerprint == _fp("owned.segment.1.written")
    with pytest.raises(ManagedSequenceError, match="stale_parent"):
        record_managed_sequence_canvas_write(
            written,
            segment_id="segment.1",
            expected_revision=checked.revision,
            active_workflow_fingerprint=_fp("workflow.current"),
            previous_owned_projection_fingerprint=_fp("owned.segment.1"),
            written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
        )


def test_repeated_owned_content_is_still_one_recorded_write() -> None:
    # Accepted planning renders the same local prompt for adjacent segments inside one long shot
    # (equal allocations, no segment metadata), so a dirty child can write the owned content its
    # predecessor left. Content equality is not evidence that no write happened.
    bound = _bound(_started())
    written = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1"),
    )
    assert written.slots[0].canvas_write_fingerprint == _fp("owned.segment.1")
    assert written.canvas_authority is not None
    assert written.canvas_authority.owned_write_count == 1
    assert written.canvas_authority.owned_projection_fingerprint == _fp("owned.segment.1")
    with pytest.raises(ManagedSequenceError, match="canvas_write_already_recorded"):
        record_managed_sequence_canvas_write(
            written,
            segment_id="segment.1",
            expected_revision=written.revision,
            active_workflow_fingerprint=_fp("workflow.current"),
            previous_owned_projection_fingerprint=_fp("owned.segment.1"),
            written_owned_projection_fingerprint=_fp("owned.segment.1"),
        )


def test_separately_authorized_canvas_rollback_consumes_one_complete_recovery_path() -> None:
    first = _bound(_started(2))
    first = record_managed_sequence_canvas_write(
        first,
        segment_id="segment.1",
        expected_revision=first.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        first,
        segment_id="segment.1",
        expected_revision=first.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    artifact = record_managed_sequence_artifact(
        submitted,
        segment_id="segment.1",
        expected_revision=submitted.revision,
        queue_prompt_id="prompt.model.1",
        artifact_receipt_fingerprint=_fp("artifact.1"),
        artifact_bytes=1024,
    )
    completed = record_managed_sequence_terminal(
        artifact,
        segment_id="segment.1",
        expected_revision=artifact.revision,
        queue_prompt_id="prompt.model.1",
        kind="success",
        terminal_fingerprint=_fp("terminal.1"),
    )
    prepared = prepare_sequence_child(
        completed,
        segment_id="segment.2",
        expected_revision=completed.revision,
        materialization_receipt_fingerprint=_fp("materialization.2"),
        predecessor_terminal_fingerprint=_fp("terminal.1"),
        request_id="prepare.segment.2",
    )
    drifted = check_managed_sequence_canvas_authority(
        prepared.sequence,
        expected_revision=prepared.sequence.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.foreign"),
    )

    rolled_back = record_managed_sequence_canvas_rollback(
        drifted,
        segment_id="segment.2",
        expected_revision=drifted.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        drifted_owned_projection_fingerprint=_fp("owned.foreign"),
        restored_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )

    assert rolled_back.state is ManagedSequenceStateV1.ACTIVE
    assert rolled_back.active_segment_id is None
    assert rolled_back.retry_consumed is True
    assert rolled_back.slots[1].state is SequenceSlotStateV1.ELIGIBLE
    assert rolled_back.slots[1].attempt_epoch == 2
    assert rolled_back.slots[1].eligible_execution_fingerprint is None
    assert rolled_back.canvas_authority is not None
    assert rolled_back.ledger_reservation is not None
    assert rolled_back.canvas_authority.rollback_write_count == 1
    assert rolled_back.canvas_authority.owned_write_count == 1
    assert rolled_back.canvas_authority.owned_projection_fingerprint == _fp(
        "owned.segment.1.written"
    )
    assert rolled_back.ledger_reservation.recovery_rows_consumed == 1
    with pytest.raises(ManagedSequenceError, match="retry_exhausted"):
        retry_managed_sequence_segment(
            rolled_back,
            segment_id="segment.2",
            expected_revision=rolled_back.revision,
            active_workflow_fingerprint=_fp("workflow.current"),
            owned_projection_fingerprint=_fp("owned.segment.1.written"),
        )


def test_next_child_bind_preserves_the_owned_canvas_cas_chain() -> None:
    first = _bound(_started(2))
    first = record_managed_sequence_canvas_write(
        first,
        segment_id="segment.1",
        expected_revision=first.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        first,
        segment_id="segment.1",
        expected_revision=first.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    artifact = record_managed_sequence_artifact(
        submitted,
        segment_id="segment.1",
        expected_revision=submitted.revision,
        queue_prompt_id="prompt.model.1",
        artifact_receipt_fingerprint=_fp("artifact.1"),
        artifact_bytes=1024,
    )
    completed = record_managed_sequence_terminal(
        artifact,
        segment_id="segment.1",
        expected_revision=artifact.revision,
        queue_prompt_id="prompt.model.1",
        kind="success",
        terminal_fingerprint=_fp("terminal.1"),
    )
    prepared = prepare_sequence_child(
        completed,
        segment_id="segment.2",
        expected_revision=completed.revision,
        materialization_receipt_fingerprint=_fp("materialization.2"),
        predecessor_terminal_fingerprint=_fp("terminal.1"),
        request_id="prepare.segment.2",
    )
    second = bind_prepared_child(
        prepared.sequence,
        segment_id="segment.2",
        expected_revision=prepared.sequence.revision,
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        child_run_handle="mc_" + "b" * 40,
        child_state_fingerprint=_fp("child.segment.2.prepared"),
        graph_fingerprint=_fp("graph.segment.2"),
        compiled_prompt_fingerprint=_fp("compiled.segment.2"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1.written"),
        active_workflow_fingerprint=_fp("workflow.current"),
    )

    assert second.canvas_authority is not None
    assert second.canvas_authority.owned_write_count == 1
    assert second.canvas_authority.owned_projection_fingerprint == _fp("owned.segment.1.written")


def test_submission_is_exact_once_and_running_does_not_slide_the_deadline() -> None:
    bound = _bound(_started())
    with pytest.raises(ManagedSequenceError, match="canvas_write_required"):
        record_managed_sequence_submission(
            bound,
            segment_id="segment.1",
            expected_revision=bound.revision,
            child_run_handle="mc_" + "a" * 40,
            child_state_fingerprint=_fp("child.segment.1.submitted"),
            queue_prompt_id="prompt.model.1",
            timeout_ms=60_000,
            now=100.0,
        )
    bound = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.segment.1.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    assert submitted.slots[0].state is SequenceSlotStateV1.SUBMITTED
    assert submitted.lease is not None
    expected_deadline = 100.0 + 60.0 + MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS
    assert submitted.lease.active_deadline == expected_deadline

    running = record_managed_sequence_running(
        submitted,
        segment_id="segment.1",
        expected_revision=submitted.revision,
        queue_prompt_id="prompt.model.1",
        child_state_fingerprint=_fp("child.segment.1.running"),
        now=400.0,
    )
    assert running.slots[0].state is SequenceSlotStateV1.RUNNING
    assert running.lease is not None
    assert running.lease.active_deadline == expected_deadline

    with pytest.raises(ManagedSequenceError, match="duplicate_submission"):
        record_managed_sequence_submission(
            running,
            segment_id="segment.1",
            expected_revision=running.revision,
            child_run_handle="mc_" + "a" * 40,
            child_state_fingerprint=_fp("child.segment.1.submitted.again"),
            queue_prompt_id="prompt.model.2",
            timeout_ms=60_000,
            now=401.0,
        )


def test_only_matching_prompt_and_verified_artifact_unlock_successor() -> None:
    bound = _bound(_started())
    bound = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    with pytest.raises(ManagedSequenceError, match="foreign_prompt"):
        record_managed_sequence_artifact(
            submitted,
            segment_id="segment.1",
            expected_revision=submitted.revision,
            queue_prompt_id="prompt.foreign",
            artifact_receipt_fingerprint=_fp("artifact.1"),
            artifact_bytes=1024,
        )
    artifact = record_managed_sequence_artifact(
        submitted,
        segment_id="segment.1",
        expected_revision=submitted.revision,
        queue_prompt_id="prompt.model.1",
        artifact_receipt_fingerprint=_fp("artifact.1"),
        artifact_bytes=1024,
    )
    completed = record_managed_sequence_terminal(
        artifact,
        segment_id="segment.1",
        expected_revision=artifact.revision,
        queue_prompt_id="prompt.model.1",
        kind="success",
        terminal_fingerprint=_fp("terminal.1"),
    )

    assert completed.slots[0].state is SequenceSlotStateV1.SUCCEEDED
    assert completed.slots[1].state is SequenceSlotStateV1.ELIGIBLE
    assert completed.active_segment_id is None
    assert completed.artifact_reservation is not None
    assert completed.artifact_reservation.consumed_entries == 1
    assert completed.artifact_reservation.consumed_bytes == 1024


def test_detach_keeps_current_child_and_pauses_every_successor_until_explicit_resume() -> None:
    bound = _bound(_started())
    bound = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    detached = detach_managed_sequence_client(
        submitted,
        expected_revision=submitted.revision,
    )
    artifact = record_managed_sequence_artifact(
        detached,
        segment_id="segment.1",
        expected_revision=detached.revision,
        queue_prompt_id="prompt.model.1",
        artifact_receipt_fingerprint=_fp("artifact.1"),
        artifact_bytes=1024,
    )
    completed = record_managed_sequence_terminal(
        artifact,
        segment_id="segment.1",
        expected_revision=artifact.revision,
        queue_prompt_id="prompt.model.1",
        kind="success",
        terminal_fingerprint=_fp("terminal.1"),
    )

    assert completed.state is ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT
    assert completed.slots[1].state is SequenceSlotStateV1.PENDING
    resumed = resume_managed_sequence(
        completed,
        expected_revision=completed.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    assert resumed.state is ManagedSequenceStateV1.ACTIVE
    assert resumed.slots[1].state is SequenceSlotStateV1.ELIGIBLE
    assert resumed.ledger_reservation is not None
    assert resumed.ledger_reservation.recovery_rows_consumed == 1


def test_resume_preserves_the_single_successor_already_unlocked_before_detach() -> None:
    bound = _bound(_started())
    bound = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    artifact = record_managed_sequence_artifact(
        submitted,
        segment_id="segment.1",
        expected_revision=submitted.revision,
        queue_prompt_id="prompt.model.1",
        artifact_receipt_fingerprint=_fp("artifact.1"),
        artifact_bytes=1024,
    )
    completed = record_managed_sequence_terminal(
        artifact,
        segment_id="segment.1",
        expected_revision=artifact.revision,
        queue_prompt_id="prompt.model.1",
        kind="success",
        terminal_fingerprint=_fp("terminal.1"),
    )
    detached = detach_managed_sequence_client(completed, expected_revision=completed.revision)

    assert [slot.state for slot in detached.slots] == [
        SequenceSlotStateV1.SUCCEEDED,
        SequenceSlotStateV1.ELIGIBLE,
        SequenceSlotStateV1.PENDING,
    ]
    resumed = resume_managed_sequence(
        detached,
        expected_revision=detached.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )

    assert resumed.state is ManagedSequenceStateV1.ACTIVE
    assert [slot.state for slot in resumed.slots] == [
        SequenceSlotStateV1.SUCCEEDED,
        SequenceSlotStateV1.ELIGIBLE,
        SequenceSlotStateV1.PENDING,
    ]
    assert sum(slot.state is SequenceSlotStateV1.ELIGIBLE for slot in resumed.slots) == 1


def test_unknown_ownership_preserves_prompt_tombstone_and_blocks_resume() -> None:
    bound = _bound(_started())
    bound = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    unknown = mark_managed_sequence_unknown_ownership(
        submitted,
        segment_id="segment.1",
        expected_revision=submitted.revision,
        queue_prompt_id="prompt.model.1",
    )

    assert unknown.state is ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP
    assert unknown.slots[0].state is SequenceSlotStateV1.UNKNOWN_OWNERSHIP
    assert unknown.slots[0].queue_prompt_id == "prompt.model.1"
    with pytest.raises(ManagedSequenceError, match="ownership_unresolved"):
        resume_managed_sequence(
            unknown,
            expected_revision=unknown.revision,
            active_workflow_fingerprint=_fp("workflow.current"),
            owned_projection_fingerprint=_fp("owned.segment.1"),
        )


def test_active_lease_expiry_preserves_exact_prompt_ownership_tombstone() -> None:
    bound = _bound(_started())
    bound = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    assert submitted.lease is not None
    assert submitted.lease.active_deadline is not None

    with pytest.raises(ManagedSequenceError, match="lease_active"):
        expire_managed_sequence_lease(
            submitted,
            expected_revision=submitted.revision,
            now=submitted.lease.active_deadline,
        )
    expired = expire_managed_sequence_lease(
        submitted,
        expected_revision=submitted.revision,
        now=submitted.lease.active_deadline + 0.001,
    )

    assert expired.state is ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP
    assert expired.slots[0].state is SequenceSlotStateV1.UNKNOWN_OWNERSHIP
    assert expired.slots[0].queue_prompt_id == "prompt.model.1"
    assert expired.active_segment_id == "segment.1"
    assert expired.ledger_reservation is not None
    assert expired.ledger_reservation.released is False


def test_pre_submit_idle_expiry_releases_child_authority_and_resume_keeps_one_slot() -> None:
    bound = _bound(_started())
    assert bound.lease is not None

    expired = expire_managed_sequence_lease(
        bound,
        expected_revision=bound.revision,
        now=bound.lease.idle_deadline + 0.001,
    )

    assert expired.state is ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT
    assert expired.active_segment_id is None
    assert expired.lease is not None
    assert expired.lease.idle_expiry_handled is True
    assert expired.lease.absolute_deadline == bound.lease.absolute_deadline
    assert [slot.state for slot in expired.slots] == [
        SequenceSlotStateV1.ELIGIBLE,
        SequenceSlotStateV1.PENDING,
        SequenceSlotStateV1.PENDING,
    ]
    assert expired.slots[0].child_run_handle is None
    assert expired.slots[0].eligible_execution_fingerprint is None

    resumed = resume_managed_sequence(
        expired,
        expected_revision=expired.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.segment.1"),
    )
    assert resumed.state is ManagedSequenceStateV1.ACTIVE
    assert sum(slot.state is SequenceSlotStateV1.ELIGIBLE for slot in resumed.slots) == 1
    with pytest.raises(ManagedSequenceError, match="lease_active"):
        expire_managed_sequence_lease(
            resumed,
            expected_revision=resumed.revision,
            now=bound.lease.idle_deadline + 0.002,
        )


def test_retry_is_one_complete_reserved_reexecution_and_never_partial() -> None:
    bound = _bound(_started())
    bound = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    detached = detach_managed_sequence_client(
        submitted,
        expected_revision=submitted.revision,
    )
    failed = record_managed_sequence_terminal(
        detached,
        segment_id="segment.1",
        expected_revision=detached.revision,
        queue_prompt_id="prompt.model.1",
        kind="error",
        terminal_fingerprint=_fp("terminal.error.1"),
    )
    with pytest.raises(ManagedSequenceError, match="canvas_drift"):
        retry_managed_sequence_segment(
            failed,
            segment_id="segment.1",
            expected_revision=failed.revision,
            active_workflow_fingerprint=_fp("workflow.foreign"),
            owned_projection_fingerprint=_fp("owned.segment.1.written"),
        )
    retried = retry_managed_sequence_segment(
        failed,
        segment_id="segment.1",
        expected_revision=failed.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )

    assert retried.retry_consumed is True
    assert retried.slots[0].attempt_epoch == 2
    assert retried.slots[0].state is SequenceSlotStateV1.ELIGIBLE
    assert retried.client_attached is True
    assert retried.ledger_reservation is not None
    assert retried.ledger_reservation.recovery_rows_consumed == 1
    with pytest.raises(ManagedSequenceError, match="retry_exhausted"):
        retry_managed_sequence_segment(
            retried,
            segment_id="segment.1",
            expected_revision=retried.revision,
            active_workflow_fingerprint=_fp("workflow.current"),
            owned_projection_fingerprint=_fp("owned.segment.1.written"),
        )

    prepared = prepare_sequence_child(
        retried,
        segment_id="segment.1",
        expected_revision=retried.revision,
        materialization_receipt_fingerprint=_fp("materialization.1"),
        predecessor_terminal_fingerprint=None,
        request_id="retry.segment.1",
    )
    assert prepared.sequence.ledger_reservation is not None
    assert prepared.sequence.ledger_reservation.recovery_rows_consumed == 1
    rebound = bind_prepared_child(
        prepared.sequence,
        segment_id="segment.1",
        expected_revision=prepared.sequence.revision,
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        child_run_handle="mc_" + "b" * 40,
        child_state_fingerprint=_fp("child.segment.1.retry.prepared"),
        graph_fingerprint=_fp("graph.segment.1.retry"),
        compiled_prompt_fingerprint=_fp("compiled.segment.1.retry"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1.written"),
        active_workflow_fingerprint=_fp("workflow.current"),
    )
    rebound = record_managed_sequence_canvas_write(
        rebound,
        segment_id="segment.1",
        expected_revision=rebound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1.written"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.retry.written"),
    )
    resubmitted = record_managed_sequence_submission(
        rebound,
        segment_id="segment.1",
        expected_revision=rebound.revision,
        child_run_handle="mc_" + "b" * 40,
        child_state_fingerprint=_fp("child.segment.1.retry.submitted"),
        queue_prompt_id="prompt.model.retry.1",
        timeout_ms=60_000,
        now=200.0,
    )
    rerunning = record_managed_sequence_running(
        resubmitted,
        segment_id="segment.1",
        expected_revision=resubmitted.revision,
        queue_prompt_id="prompt.model.retry.1",
        child_state_fingerprint=_fp("child.segment.1.retry.running"),
        now=201.0,
    )
    reartifact = record_managed_sequence_artifact(
        rerunning,
        segment_id="segment.1",
        expected_revision=rerunning.revision,
        queue_prompt_id="prompt.model.retry.1",
        artifact_receipt_fingerprint=_fp("artifact.retry.1"),
        artifact_bytes=1024,
    )
    recovered = record_managed_sequence_terminal(
        reartifact,
        segment_id="segment.1",
        expected_revision=reartifact.revision,
        queue_prompt_id="prompt.model.retry.1",
        kind="success",
        terminal_fingerprint=_fp("terminal.retry.1"),
    )

    assert recovered.ledger_reservation is not None
    assert recovered.ledger_reservation.recovery_rows_consumed == 7
    assert recovered.slots[1].state is SequenceSlotStateV1.ELIGIBLE


def test_cancel_preserves_committed_artifacts_and_releases_only_unused_capacity() -> None:
    sequence = _started()
    cancelled = cancel_managed_sequence(sequence, expected_revision=sequence.revision)

    assert cancelled.state is ManagedSequenceStateV1.CANCELLED
    assert cancelled.artifact_reservation is not None
    assert cancelled.ledger_reservation is not None
    assert cancelled.artifact_reservation.released is True
    assert cancelled.artifact_reservation.consumed_entries == 0
    assert cancelled.ledger_reservation.released is True


def test_cancelled_host_owned_child_can_finish_without_reopening_successors() -> None:
    bound = _bound(_started())
    bound = record_managed_sequence_canvas_write(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = record_managed_sequence_submission(
        bound,
        segment_id="segment.1",
        expected_revision=bound.revision,
        child_run_handle="mc_" + "a" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.model.1",
        timeout_ms=60_000,
        now=100.0,
    )
    cancelled = cancel_managed_sequence(submitted, expected_revision=submitted.revision)

    assert cancelled.state is ManagedSequenceStateV1.CANCELLED
    assert cancelled.slots[0].state is SequenceSlotStateV1.SUBMITTED
    assert cancelled.ledger_reservation is not None
    assert cancelled.ledger_reservation.released is False
    assert cancelled.ledger_reservation.recovery_rows_consumed == 1
    artifact = record_managed_sequence_artifact(
        cancelled,
        segment_id="segment.1",
        expected_revision=cancelled.revision,
        queue_prompt_id="prompt.model.1",
        artifact_receipt_fingerprint=_fp("artifact.cancelled.1"),
        artifact_bytes=1024,
    )
    terminal = record_managed_sequence_terminal(
        artifact,
        segment_id="segment.1",
        expected_revision=artifact.revision,
        queue_prompt_id="prompt.model.1",
        kind="success",
        terminal_fingerprint=_fp("terminal.cancelled.1"),
    )

    assert terminal.state is ManagedSequenceStateV1.CANCELLED
    assert terminal.slots[1].state is SequenceSlotStateV1.CANCELLED
    assert terminal.ledger_reservation is not None
    assert terminal.artifact_reservation is not None
    assert terminal.ledger_reservation.released is True
    assert terminal.artifact_reservation.released is True


def test_projection_reads_are_bounded_current_and_side_effect_free() -> None:
    sequence = _started(MAX_MANAGED_SEQUENCE_SEGMENTS)
    before = sequence.fingerprint

    first = read_managed_sequence_projection(sequence)
    second = read_managed_sequence_projection(sequence)

    assert first == second
    assert first.etag == sequence.fingerprint
    slots = first.to_wire()["slots"]
    assert type(slots) is list
    assert len(slots) == 15
    assert sequence.fingerprint == before
    assert sequence.ledger_reservation is not None
    assert sequence.ledger_reservation.consumed_rows == 2


def test_authorization_rejects_duplicate_or_misaligned_segments() -> None:
    authority = _authorization(2)
    with pytest.raises(ManagedSequenceError, match="segment_order"):
        replace(authority, segments=(authority.segments[1], authority.segments[0]))
    with pytest.raises(ManagedSequenceError, match="segment_duplicate"):
        replace(authority, segments=(authority.segments[0], authority.segments[0]))
