from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import cast

import pytest

from comfyui_h3_context.core import (
    ExecutionCorrelation,
    FingerprintDomain,
    GenerationJobSpec,
    GenerationJobState,
    GenerationSequenceError,
    GenerationSequencePlan,
    GenerationSequenceProjection,
    build_generation_sequence_plan,
    build_generation_sequence_projection,
    cancel_generation_sequence,
    create_generation_sequence_state,
    eligible_generation_jobs,
    mark_generation_unknown_ownership,
    reconcile_generation_sequence,
    record_generation_failure,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
    record_output_verification_failure,
    retry_generation_job,
)
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.pipeline_transaction import PipelineTransactionState
from comfyui_h3_context.core.recompute_closure import RecomputePlan, plan_recompute
from comfyui_h3_context.core.segment_artifacts import (
    ArtifactLifecycleState,
    SegmentArtifactReceipt,
    begin_segment_artifact_receipt,
    complete_segment_artifact_receipt,
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
    return f"sha256:{sha256(label.encode('ascii')).hexdigest()}"


def _segment(
    segment_id: str,
    mode: TaskMode,
    *,
    settings: str,
    relation: SegmentRelationKind = SegmentRelationKind.INDEPENDENT,
    predecessor: str | None = None,
) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=mode,
        source_id=f"source.{segment_id}",
        reference_ids=(() if mode is TaskMode.T2VA else (f"reference.{segment_id}",)),
        duration=SegmentDuration.from_frame_count(124),
        relation=relation,
        predecessor_segment_id=predecessor,
        accepted_intent_fingerprint=_fp(f"intent.{segment_id}"),
        semantic_receipt_fingerprint=None,
        profile_fingerprint=_fp(f"profile.{mode.value}"),
        reference_registry_fingerprint=_fp(f"registry.{segment_id}"),
        native_binding_fingerprint=_fp(f"native.{mode.value}"),
        producer_settings_fingerprint=_fp(settings),
    )


def _authorities(segments: tuple[SegmentDeclaration, ...]) -> tuple[AcceptedIntentAuthority, ...]:
    return tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in segments
    )


def _workspace(
    segments: tuple[SegmentDeclaration, ...],
    *,
    workspace_id: str = "workspace.1",
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


def _spec(segment_id: str, *, timeout_ms: int = 60_000) -> GenerationJobSpec:
    return GenerationJobSpec(
        segment_id=segment_id,
        job_id=f"job.{segment_id}",
        graph_fingerprint=_fp(f"graph.{segment_id}"),
        compiled_prompt_fingerprint=_fp(f"compiled.{segment_id}"),
        model_fingerprint=_fp("model.h3"),
        runtime_fingerprint=_fp("runtime.comfyui.0.32.0"),
        expected_format="video.mp4",
        expected_shape=(124, 512, 512, 3),
        timeout_ms=timeout_ms,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )


def test_plan_preserves_the_pre_authority_positional_constructor_order() -> None:
    _, _, _, plan = _changed_sequence()

    compatible = GenerationSequencePlan(
        plan.sequence_id,
        plan.workspace_id,
        plan.workspace_revision,
        plan.workspace_fingerprint,
        plan.recompute_plan_fingerprint,
        plan.manifest_fingerprints,
        plan.jobs,
        plan.clean_segment_ids,
        2,
    )

    assert compatible.max_concurrency == 2
    assert compatible.source_workspace_authority is None


def _changed_sequence(
    *,
    count: int = 1,
    max_concurrency: int = 1,
) -> tuple[
    MultiSegmentWorkspace,
    tuple[SegmentContextManifest, ...],
    RecomputePlan,
    GenerationSequencePlan,
]:
    modes = (
        TaskMode.T2VA,
        TaskMode.I2VA,
        TaskMode.FL2VA,
        TaskMode.L2VA,
        TaskMode.REF2VA,
    )
    previous_segments = tuple(
        _segment(f"segment.{index + 1}", modes[index], settings=f"settings.old.{index}")
        for index in range(count)
    )
    previous = _workspace(previous_segments)
    current_segments = tuple(
        replace(
            item,
            producer_settings_fingerprint=_fp(f"settings.new.{index}"),
        )
        for index, item in enumerate(previous_segments)
    )
    current = _revision(previous, current_segments)
    previous_manifests = derive_segment_manifests(previous)
    current_manifests = derive_segment_manifests(current)
    recompute = plan_recompute(previous_manifests, current_manifests)
    specs = tuple(_spec(item.segment_id) for item in current_segments)
    plan = build_generation_sequence_plan(
        current,
        current_manifests,
        recompute,
        specs,
        max_concurrency=max_concurrency,
    )
    return current, current_manifests, recompute, plan


def _complete_receipt(manifest: SegmentContextManifest) -> SegmentArtifactReceipt:
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
        graph_fingerprint=_fp(f"graph.{manifest.segment_id}"),
        native_binding_fingerprint=manifest.native_binding_fingerprint,
        model_fingerprint=_fp("model.h3"),
        runtime_fingerprint=_fp("runtime.comfyui.0.32.0"),
        settings_fingerprint=manifest.producer_settings_fingerprint,
        source_id=manifest.source_id,
        predecessor_artifact_fingerprint=None,
        execution_fingerprint=_fp(f"execution.{manifest.segment_id}"),
        format_label="video.mp4",
        shape=(124, 512, 512, 3),
        created_at_ms=100,
        expires_at_ms=10_000,
        output_fingerprint=_fp(f"output.{manifest.segment_id}"),
        byte_length=1024,
    )


def test_plan_is_deterministic_bounded_and_contains_only_dirty_jobs() -> None:
    modes = (
        TaskMode.T2VA,
        TaskMode.I2VA,
        TaskMode.FL2VA,
        TaskMode.L2VA,
        TaskMode.REF2VA,
    )
    previous_segments = tuple(
        _segment(f"segment.{index + 1}", mode, settings=f"settings.{index}")
        for index, mode in enumerate(modes)
    )
    previous = _workspace(previous_segments)
    current_segments = tuple(
        replace(
            item,
            producer_settings_fingerprint=(
                _fp(f"settings.changed.{index}")
                if index in {0, 2, 4}
                else item.producer_settings_fingerprint
            ),
        )
        for index, item in enumerate(previous_segments)
    )
    current = _revision(previous, current_segments)
    old_manifests = derive_segment_manifests(previous)
    manifests = derive_segment_manifests(current)
    recompute = plan_recompute(old_manifests, manifests)
    specs = tuple(
        _spec(item.segment_id)
        for item in current_segments
        if item.segment_id
        in {
            "segment.1",
            "segment.3",
            "segment.5",
        }
    )

    first = build_generation_sequence_plan(
        current,
        manifests,
        recompute,
        specs,
        max_concurrency=2,
    )
    second = build_generation_sequence_plan(
        current,
        manifests,
        recompute,
        tuple(reversed(specs)),
        max_concurrency=2,
    )

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert tuple(job.segment_id for job in first.jobs) == (
        "segment.1",
        "segment.3",
        "segment.5",
    )
    assert tuple(job.task_mode for job in first.jobs) == (
        TaskMode.T2VA,
        TaskMode.FL2VA,
        TaskMode.REF2VA,
    )
    assert first.clean_segment_ids == ("segment.2", "segment.4")
    assert first.max_concurrency == 2
    assert "user_intent" not in first.to_wire()
    assert "prompt" not in first.to_wire()
    assert "path" not in first.to_wire()


def test_plan_retains_exact_backend_source_without_changing_portable_identity() -> None:
    workspace, manifests, recompute, plan = _changed_sequence(count=2)

    assert plan.source_workspace_authority is workspace
    portable = replace(plan, source_workspace_authority=None)
    assert portable == plan
    assert portable.fingerprint == plan.fingerprint
    assert portable.to_wire() == plan.to_wire()
    assert "source_workspace_authority" not in plan.to_public_dict()

    foreign = _workspace(workspace.segments, workspace_id="workspace.foreign")
    with pytest.raises(GenerationSequenceError, match="source_workspace_authority"):
        replace(plan, source_workspace_authority=foreign)

    forged_segments = (
        replace(
            workspace.segments[0],
            producer_settings_fingerprint=_fp("forged.source.settings"),
        ),
        workspace.segments[1],
    )
    forged = create_workspace(
        workspace.workspace_id,
        forged_segments,
        accepted_intent_authorities=_authorities(forged_segments),
    )
    with pytest.raises(GenerationSequenceError, match="source_workspace_authority"):
        replace(
            plan,
            workspace_revision=forged.revision,
            workspace_fingerprint=forged.fingerprint,
            source_workspace_authority=forged,
            sequence_fingerprint=None,
        )


def test_clean_plan_queues_nothing_and_unsafe_selection_fails_closed() -> None:
    segments = (_segment("segment.1", TaskMode.T2VA, settings="same"),)
    previous = _workspace(segments)
    current = _revision(previous, segments)
    old_manifests = derive_segment_manifests(previous)
    manifests = derive_segment_manifests(current)
    clean = plan_recompute(old_manifests, manifests)

    plan = build_generation_sequence_plan(current, manifests, clean, ())
    assert plan.jobs == ()
    assert plan.clean_segment_ids == ("segment.1",)
    assert eligible_generation_jobs(create_generation_sequence_state(plan)) == ()

    changed = _revision(
        current,
        (
            replace(
                segments[0],
                producer_settings_fingerprint=_fp("changed"),
            ),
        ),
    )
    changed_manifests = derive_segment_manifests(changed)
    unsafe = plan_recompute(manifests, changed_manifests, requested_segment_ids=())
    with pytest.raises(GenerationSequenceError, match="unsafe_recompute_selection"):
        build_generation_sequence_plan(changed, changed_manifests, unsafe, (_spec("segment.1"),))


def test_full_recompute_authority_remains_blocked_before_projection() -> None:
    previous_segments = (
        _segment("segment.1", TaskMode.T2VA, settings="old.1"),
        _segment("segment.2", TaskMode.I2VA, settings="old.2"),
    )
    previous = _workspace(previous_segments)
    current_segments = tuple(
        replace(item, producer_settings_fingerprint=_fp(f"new.{index}"))
        for index, item in enumerate(previous_segments)
    )
    current = _revision(previous, current_segments)
    manifests = derive_segment_manifests(current)
    structural_mismatch = plan_recompute(
        derive_segment_manifests(previous)[:1],
        manifests,
    )
    assert structural_mismatch.requires_full_recompute

    with pytest.raises(GenerationSequenceError, match="blocked_recompute_plan"):
        build_generation_sequence_plan(
            current,
            manifests,
            structural_mismatch,
            tuple(_spec(item.segment_id) for item in current_segments),
        )


def test_clean_predecessor_requires_an_exact_complete_receipt() -> None:
    previous_segments = (
        _segment("segment.1", TaskMode.T2VA, settings="stable"),
        _segment(
            "segment.2",
            TaskMode.I2VA,
            settings="old",
            relation=SegmentRelationKind.PREDECESSOR,
            predecessor="segment.1",
        ),
    )
    previous = _workspace(previous_segments)
    current_segments = (
        previous_segments[0],
        replace(previous_segments[1], producer_settings_fingerprint=_fp("new")),
    )
    current = _revision(previous, current_segments)
    old_manifests = derive_segment_manifests(previous)
    manifests = derive_segment_manifests(current)
    recompute = plan_recompute(old_manifests, manifests)

    with pytest.raises(GenerationSequenceError, match="missing_predecessor_receipt"):
        build_generation_sequence_plan(current, manifests, recompute, (_spec("segment.2"),))

    # A clean predecessor is not regenerated for the new workspace revision. Reuse must bind the
    # exact prior output through stable producer identity instead of fabricating a current receipt.
    receipt = _complete_receipt(old_manifests[0])
    plan = build_generation_sequence_plan(
        current,
        manifests,
        recompute,
        (_spec("segment.2"),),
        reusable_receipts=(receipt,),
    )
    assert plan.jobs[0].predecessor_receipt_fingerprint == receipt.fingerprint
    assert plan.jobs[0].predecessor_artifact_fingerprint == receipt.output_fingerprint

    incompatible = replace(
        receipt,
        native_binding_fingerprint=_fp("wrong.native"),
        receipt_fingerprint=None,
    )
    with pytest.raises(GenerationSequenceError, match="incompatible_predecessor_receipt"):
        build_generation_sequence_plan(
            current,
            manifests,
            recompute,
            (_spec("segment.2"),),
            reusable_receipts=(incompatible,),
        )


def test_job_lifecycle_uses_exact_projection_host_and_artifact_correlations() -> None:
    _, manifests, _, plan = _changed_sequence()
    state = create_generation_sequence_state(plan)
    job = eligible_generation_jobs(state)[0]
    assert job.state is GenerationJobState.PLANNED

    state = record_generation_projection(
        state,
        job.job_id,
        transaction_id="transaction.segment.1.attempt.1",
        graph_fingerprint=job.graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    projected = state.runtime_for(job.job_id)
    assert projected.state is GenerationJobState.PROJECTED
    assert projected.transaction is not None
    assert projected.transaction.state is PipelineTransactionState.PREPARED
    assert eligible_generation_jobs(state) == ()

    state = record_generation_submission(state, job.job_id, queue_prompt_id="prompt.1")
    state = record_generation_running(state, job.job_id, host_owner_id="host.execution.1")
    running = state.runtime_for(job.job_id)
    assert running.transaction is not None
    partial = begin_segment_artifact_receipt(
        manifest=manifests[0],
        transaction=running.transaction,
        artifact_id="artifact.segment.1",
        model_fingerprint=job.model_fingerprint,
        runtime_fingerprint=job.runtime_fingerprint,
        execution_fingerprint=_fp("execution.segment.1"),
        predecessor_artifact_fingerprint=None,
        format_label=job.expected_format,
        shape=job.expected_shape,
        created_at_ms=100,
        expires_at_ms=10_000,
    )
    receipt = complete_segment_artifact_receipt(
        partial,
        output_fingerprint=_fp("output.segment.1"),
        byte_length=1024,
    )
    state = record_generation_success(
        state,
        job.job_id,
        result_fingerprint=_fp("host.result.segment.1"),
        receipt=receipt,
    )

    terminal = state.runtime_for(job.job_id)
    assert terminal.state is GenerationJobState.SUCCEEDED
    assert terminal.artifact_receipt_fingerprint == receipt.fingerprint
    assert state.complete
    with pytest.raises(GenerationSequenceError, match="job_transition"):
        record_generation_submission(state, job.job_id, queue_prompt_id="prompt.duplicate")


def test_projection_uses_compiled_execution_identity_and_retains_graph_as_evidence() -> None:
    _, _, _, plan = _changed_sequence()
    state = create_generation_sequence_state(plan)
    job = eligible_generation_jobs(state)[0]
    observed_surroundings = _fp("graph.host-rewritten-surroundings")

    projected = record_generation_projection(
        state,
        job.job_id,
        transaction_id="transaction.segment.1.attempt.1",
        graph_fingerprint=observed_surroundings,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )

    transaction = projected.runtime_for(job.job_id).transaction
    assert transaction is not None
    assert transaction.graph_fingerprint == observed_surroundings
    assert transaction.compiled_prompt_fingerprint == job.compiled_prompt_fingerprint

    with pytest.raises(GenerationSequenceError, match="projection_identity_mismatch"):
        record_generation_projection(
            state,
            job.job_id,
            transaction_id="transaction.segment.1.attempt.1",
            graph_fingerprint=job.graph_fingerprint,
            compiled_prompt_fingerprint=_fp("compiled.host-mutated"),
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )


def test_output_verification_failure_retains_the_attempt_and_recovers_monotonically() -> None:
    _, manifests, _, plan = _changed_sequence()
    job = plan.jobs[0]
    state = create_generation_sequence_state(plan)
    state = record_generation_projection(
        state,
        job.job_id,
        transaction_id="transaction.segment.1.attempt.1",
        graph_fingerprint=job.graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(state, job.job_id, queue_prompt_id="prompt.1")
    running = record_generation_running(state, job.job_id, host_owner_id="host.execution.1")

    verification_failed = record_output_verification_failure(
        running,
        job.job_id,
        failure_code="artifact_store_unavailable",
    )
    failed_runtime = verification_failed.runtime_for(job.job_id)
    assert failed_runtime.state is GenerationJobState.OUTPUT_VERIFICATION_FAILED
    assert failed_runtime.attempt == 1
    assert failed_runtime.transaction is not None
    assert failed_runtime.transaction.state is PipelineTransactionState.RUNNING
    assert eligible_generation_jobs(verification_failed) == ()
    assert reconcile_generation_sequence(running, verification_failed) == verification_failed

    partial = begin_segment_artifact_receipt(
        manifest=manifests[0],
        transaction=failed_runtime.transaction,
        artifact_id="artifact.segment.1",
        model_fingerprint=job.model_fingerprint,
        runtime_fingerprint=job.runtime_fingerprint,
        execution_fingerprint=_fp("execution.segment.1"),
        predecessor_artifact_fingerprint=None,
        format_label=job.expected_format,
        shape=job.expected_shape,
        created_at_ms=100,
        expires_at_ms=10_000,
    )
    receipt = complete_segment_artifact_receipt(
        partial,
        output_fingerprint=_fp("output.segment.1"),
        byte_length=1024,
    )
    recovered = record_generation_success(
        verification_failed,
        job.job_id,
        result_fingerprint=_fp("host.result.segment.1"),
        receipt=receipt,
    )

    recovered_runtime = recovered.runtime_for(job.job_id)
    assert recovered_runtime.state is GenerationJobState.SUCCEEDED
    assert recovered_runtime.attempt == 1
    assert recovered_runtime.failure_code is None
    assert reconcile_generation_sequence(verification_failed, recovered) == recovered


def test_dependency_order_and_concurrency_release_only_exact_successors() -> None:
    previous_segments = (
        _segment("segment.1", TaskMode.T2VA, settings="old.1"),
        _segment(
            "segment.2",
            TaskMode.I2VA,
            settings="old.2",
            relation=SegmentRelationKind.PREDECESSOR,
            predecessor="segment.1",
        ),
        _segment("segment.3", TaskMode.L2VA, settings="old.3"),
    )
    previous = _workspace(previous_segments)
    current_segments = tuple(
        replace(item, producer_settings_fingerprint=_fp(f"new.{index}"))
        for index, item in enumerate(previous_segments)
    )
    current = _revision(previous, current_segments)
    manifests = derive_segment_manifests(current)
    recompute = plan_recompute(derive_segment_manifests(previous), manifests)
    plan = build_generation_sequence_plan(
        current,
        manifests,
        recompute,
        tuple(_spec(item.segment_id) for item in current_segments),
        max_concurrency=2,
    )
    state = create_generation_sequence_state(plan)

    successor = plan.job_for_segment("segment.2")
    with pytest.raises(GenerationSequenceError, match="job_not_eligible"):
        record_generation_projection(
            state,
            successor.job_id,
            transaction_id="transaction.segment.2.attempt.1",
            graph_fingerprint=successor.graph_fingerprint,
            compiled_prompt_fingerprint=successor.compiled_prompt_fingerprint,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )

    assert tuple(job.segment_id for job in eligible_generation_jobs(state)) == (
        "segment.1",
        "segment.3",
    )
    first = plan.job_for_segment("segment.1")
    state = record_generation_projection(
        state,
        first.job_id,
        transaction_id="transaction.segment.1.attempt.1",
        graph_fingerprint=first.graph_fingerprint,
        compiled_prompt_fingerprint=first.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    assert tuple(job.segment_id for job in eligible_generation_jobs(state)) == ("segment.3",)


def test_projection_cannot_bypass_concurrency_and_jobs_are_deeply_immutable() -> None:
    _, _, _, plan = _changed_sequence(count=2, max_concurrency=1)
    first, second = plan.jobs
    assert isinstance(first.duration, SegmentDuration)

    state = create_generation_sequence_state(plan)
    state = record_generation_projection(
        state,
        first.job_id,
        transaction_id="transaction.segment.1.attempt.1",
        graph_fingerprint=first.graph_fingerprint,
        compiled_prompt_fingerprint=first.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    with pytest.raises(GenerationSequenceError, match="job_not_eligible"):
        record_generation_projection(
            state,
            second.job_id,
            transaction_id="transaction.segment.2.attempt.1",
            graph_fingerprint=second.graph_fingerprint,
            compiled_prompt_fingerprint=second.compiled_prompt_fingerprint,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )


def test_cancellation_preserves_submitted_host_ownership_and_stops_future_jobs() -> None:
    _, _, _, plan = _changed_sequence(count=2)
    initial = create_generation_sequence_state(plan)
    cancelled_before = cancel_generation_sequence(initial)
    assert all(item.state is GenerationJobState.CANCELLED for item in cancelled_before.runtimes)
    assert eligible_generation_jobs(cancelled_before) == ()

    first = plan.jobs[0]
    submitted = record_generation_projection(
        initial,
        first.job_id,
        transaction_id="transaction.segment.1.attempt.1",
        graph_fingerprint=first.graph_fingerprint,
        compiled_prompt_fingerprint=first.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    submitted = record_generation_submission(submitted, first.job_id, queue_prompt_id="prompt.1")
    cancelled_after = cancel_generation_sequence(submitted)

    owned = cancelled_after.runtime_for(first.job_id)
    assert owned.state is GenerationJobState.SUBMITTED
    assert owned.transaction is not None and owned.transaction.cancellation_requested
    assert cancelled_after.runtimes[1].state is GenerationJobState.CANCELLED
    assert eligible_generation_jobs(cancelled_after) == ()


def test_failure_retry_timeout_and_unknown_ownership_never_duplicate_work() -> None:
    _, _, _, plan = _changed_sequence()
    job = plan.jobs[0]
    state = create_generation_sequence_state(plan)
    state = record_generation_projection(
        state,
        job.job_id,
        transaction_id="transaction.segment.1.attempt.1",
        graph_fingerprint=job.graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(state, job.job_id, queue_prompt_id="prompt.1")
    unknown = mark_generation_unknown_ownership(state, job.job_id)
    assert unknown.runtime_for(job.job_id).state is GenerationJobState.UNKNOWN_OWNERSHIP
    with pytest.raises(GenerationSequenceError, match="ambiguous_host_ownership"):
        retry_generation_job(
            unknown,
            job.job_id,
            transaction_id="transaction.segment.1.attempt.2",
        )

    failed = record_generation_failure(
        state,
        job.job_id,
        result_fingerprint=_fp("timeout.result"),
        failure_code="host_timeout",
    )
    assert failed.runtime_for(job.job_id).state is GenerationJobState.TIMED_OUT
    retried = retry_generation_job(
        failed,
        job.job_id,
        transaction_id="transaction.segment.1.attempt.2",
    )
    runtime = retried.runtime_for(job.job_id)
    assert runtime.state is GenerationJobState.PLANNED
    assert runtime.attempt == 2
    assert runtime.transaction_id == "transaction.segment.1.attempt.2"


def test_reconcile_accepts_only_forward_exact_same_plan_state() -> None:
    _, _, _, plan = _changed_sequence()
    job = plan.jobs[0]
    current = create_generation_sequence_state(plan)
    submitted = record_generation_projection(
        current,
        job.job_id,
        transaction_id="transaction.segment.1.attempt.1",
        graph_fingerprint=job.graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    submitted = record_generation_submission(submitted, job.job_id, queue_prompt_id="prompt.1")
    running = record_generation_running(submitted, job.job_id, host_owner_id="host.execution.1")

    assert reconcile_generation_sequence(submitted, running) == running
    with pytest.raises(GenerationSequenceError, match="stale_sequence_reconciliation"):
        reconcile_generation_sequence(running, submitted)

    other_plan = replace(plan, sequence_id="sequence.other", sequence_fingerprint=None)
    with pytest.raises(GenerationSequenceError, match="sequence_authority_mismatch"):
        reconcile_generation_sequence(running, create_generation_sequence_state(other_plan))


@pytest.mark.parametrize("max_concurrency", [0, 5, True])
def test_resource_and_identity_bounds_fail_before_any_host_action(max_concurrency: object) -> None:
    segments = (_segment("segment.1", TaskMode.T2VA, settings="old"),)
    previous = _workspace(segments)
    current = _revision(
        previous,
        (replace(segments[0], producer_settings_fingerprint=_fp("new")),),
    )
    manifests = derive_segment_manifests(current)
    recompute = plan_recompute(derive_segment_manifests(previous), manifests)
    with pytest.raises(GenerationSequenceError, match="max_concurrency"):
        build_generation_sequence_plan(
            current,
            manifests,
            recompute,
            (_spec("segment.1"),),
            max_concurrency=max_concurrency,  # type: ignore[arg-type]
        )

    with pytest.raises(GenerationSequenceError, match="job_timeout"):
        _spec("segment.1", timeout_ms=0)


def test_default_sequence_identity_supports_the_maximum_valid_workspace_identifier() -> None:
    segments = (_segment("segment.1", TaskMode.T2VA, settings="same"),)
    workspace = _workspace(segments, workspace_id="w" * 128)
    manifests = derive_segment_manifests(workspace)
    clean = plan_recompute(manifests, manifests)

    plan = build_generation_sequence_plan(workspace, manifests, clean, ())
    assert plan.workspace_id == "w" * 128
    assert len(plan.sequence_id) <= 128


def test_projection_exposes_only_core_eligible_commands_and_exact_progress() -> None:
    _, _, _, plan = _changed_sequence(count=2, max_concurrency=1)
    state = create_generation_sequence_state(plan)
    correlation = ExecutionCorrelation("prompt.sequence", "node.sequence")

    projection = build_generation_sequence_projection(state, correlation)
    assert isinstance(projection, GenerationSequenceProjection)
    wire = projection.to_wire()
    assert wire["schema"] == "h3.context.generation_sequence_projection.v1"
    assert wire["correlation"] == correlation.to_wire()
    assert wire["sequence_fingerprint"] == plan.fingerprint
    assert wire["state_fingerprint"] == state.fingerprint
    eligible_commands = cast(list[dict[str, object]], wire["eligible_commands"])
    progress = cast(list[dict[str, object]], wire["progress"])
    assert [item["job_id"] for item in eligible_commands] == [plan.jobs[0].job_id]
    assert eligible_commands[0]["attempt"] == 1
    assert cast(str, eligible_commands[0]["transaction_id"]).startswith("generation.")
    assert [item["state"] for item in progress] == ["planned", "planned"]

    command = projection.eligible_commands[0]
    state = record_generation_projection(
        state,
        command.job_id,
        transaction_id=command.transaction_id,
        graph_fingerprint=command.job.graph_fingerprint,
        compiled_prompt_fingerprint=command.job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(
        state,
        command.job_id,
        queue_prompt_id="prompt.segment.1",
    )
    submitted = build_generation_sequence_projection(state, correlation).to_wire()
    assert submitted["eligible_commands"] == []
    submitted_progress = cast(list[dict[str, object]], submitted["progress"])
    assert submitted_progress[0]["state"] == "submitted"
    assert submitted_progress[0]["queue_prompt_id"] == "prompt.segment.1"


def test_projection_retry_command_uses_the_core_owned_attempt_transaction() -> None:
    _, _, _, plan = _changed_sequence()
    state = create_generation_sequence_state(plan)
    first = build_generation_sequence_projection(
        state,
        ExecutionCorrelation("prompt.sequence", "node.sequence"),
    ).eligible_commands[0]
    state = record_generation_projection(
        state,
        first.job_id,
        transaction_id=first.transaction_id,
        graph_fingerprint=first.job.graph_fingerprint,
        compiled_prompt_fingerprint=first.job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(
        state,
        first.job_id,
        queue_prompt_id="prompt.segment.1.attempt.1",
    )
    state = record_generation_failure(
        state,
        first.job_id,
        result_fingerprint=_fp("failed"),
        failure_code="host_failure",
    )
    state = retry_generation_job(
        state,
        first.job_id,
        transaction_id="transaction.segment.1.attempt.2",
    )

    retry = build_generation_sequence_projection(
        state,
        ExecutionCorrelation("prompt.sequence", "node.sequence"),
    ).eligible_commands[0]
    assert retry.attempt == 2
    assert retry.transaction_id == "transaction.segment.1.attempt.2"


def test_widened_fingerprint_domain_is_declared_and_enforced() -> None:
    """M17-20 D6: the graph identity's domain travels with it and is checked first.

    App Mode's fingerprints once covered the context subgraph alone; they now
    cover the full output-producing graph. An observation carrying the older
    domain is not a drifted graph to reconcile but a different measurement, and
    reporting it as an identity mismatch would send the caller looking in the
    wrong place. M18-02 separates fingerprint domains further and must not have
    to guess which fingerprints M17-20 widened, so the domain is declared rather
    than inferred.
    """

    _, _, _, plan = _changed_sequence()
    state = create_generation_sequence_state(plan)
    job = eligible_generation_jobs(state)[0]

    assert job.fingerprint_domain is FingerprintDomain.OUTPUT_PRODUCING_GRAPH
    assert job.to_wire()["fingerprint_domain"] == "output_producing_graph"

    with pytest.raises(GenerationSequenceError, match="fingerprint_domain_mismatch"):
        record_generation_projection(
            state,
            job.job_id,
            transaction_id="transaction.segment.1.attempt.1",
            graph_fingerprint=job.graph_fingerprint,
            compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
            fingerprint_domain=FingerprintDomain.CONTEXT_SUBGRAPH,
        )

    # The domain is compared before the values, so a stale-domain observation
    # never presents itself as a drifted graph.
    with pytest.raises(GenerationSequenceError, match="fingerprint_domain_mismatch"):
        record_generation_projection(
            state,
            job.job_id,
            transaction_id="transaction.segment.1.attempt.1",
            graph_fingerprint=_fp("some.other.graph"),
            compiled_prompt_fingerprint=_fp("some.other.prompt"),
            fingerprint_domain=FingerprintDomain.CONTEXT_SUBGRAPH,
        )

    with pytest.raises(GenerationSequenceError, match="fingerprint_domain"):
        record_generation_projection(
            state,
            job.job_id,
            transaction_id="transaction.segment.1.attempt.1",
            graph_fingerprint=job.graph_fingerprint,
            compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
            fingerprint_domain="output_producing_graph",  # type: ignore[arg-type]
        )

    with pytest.raises(GenerationSequenceError, match="job_spec.fingerprint_domain"):
        GenerationJobSpec(
            segment_id="segment.1",
            job_id="job.segment.1",
            graph_fingerprint=_fp("graph.segment.1"),
            compiled_prompt_fingerprint=_fp("compiled.segment.1"),
            model_fingerprint=_fp("model.h3"),
            runtime_fingerprint=_fp("runtime.comfyui.0.32.0"),
            expected_format="video.mp4",
            expected_shape=(124, 512, 512, 3),
            timeout_ms=60_000,
            fingerprint_domain="output_producing_graph",  # type: ignore[arg-type]
        )
