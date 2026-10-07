"""Original managed children retain capacity without duplicating their entire owner."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_m23_15_sequence_coordinator import (
    _action,
    _fp,
    _managed_start_resource_request,
    _observation,
)
from test_production_import import _authorities, _Materializer, _production, _request

import comfyui_h3_context.adapters.comfyui_sequence_coordinator as coordinator_module
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    COORDINATOR_RESPONSE_SCHEMA,
    MAX_COORDINATOR_RESPONSE_BYTES,
    SequenceArtifactAuthorityV1,
    SequenceCoordinatorError,
    SequenceCoordinatorRegistry,
    SequenceCoordinatorResponse,
)
from comfyui_h3_context.adapters.managed_sequence_service import (
    ManagedChildBindingRequestV1,
    ManagedChildCorrelationV1,
    ManagedPreparedGraphObservationV1,
    ManagedSequenceServiceError,
)
from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.generation_sequence import (
    GenerationJobSpec,
    GenerationSequenceState,
    build_generation_sequence_plan,
    build_generation_sequence_projection,
    create_generation_sequence_state,
    record_generation_failure,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
)
from comfyui_h3_context.core.managed_sequence import EligibleSegmentExecutionV1
from comfyui_h3_context.core.production_duration import SegmentationPolicyV1
from comfyui_h3_context.core.production_import import ProductionAutomaticPlanAuthorityV2
from comfyui_h3_context.core.production_storyboard import ManagedExecutionQualificationV1
from comfyui_h3_context.core.recompute_closure import extend_recompute_plan, plan_recompute
from comfyui_h3_context.core.segment_artifacts import (
    SegmentArtifactReceipt,
    begin_segment_artifact_receipt,
    complete_segment_artifact_receipt,
)
from comfyui_h3_context.core.segment_workspace import derive_segment_manifests
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation

MANAGED_CHILD_RESPONSE_SCHEMA = "h3.context.generation_coordinator.managed_child_response.v1"


def _original_child(
    tmp_path: Path, count: int
) -> tuple[
    SequenceCoordinatorRegistry, ManagedChildBindingRequestV1, ProductionAutomaticPlanAuthorityV2
]:
    context, _, proposal = _authorities(
        parts=(4,) * count,
        policy=SegmentationPolicyV1.AUTO_STORYBOARD,
        segment_min_seconds=4,
        segment_max_seconds=4,
        qualification=ManagedExecutionQualificationV1.QUALIFIED,
    )
    sidebar, production, created = _production(context)
    imported = production.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=_Materializer(context),
    ).projection
    authority = production.claim_automatic_plan_authority(
        created.workspace_handle,
        expected_workspace_revision=imported.workspace_revision,
        expected_workspace_fingerprint=imported.workspace_fingerprint,
        expected_plan_fingerprint=imported.plan_fingerprint,
    )
    assert isinstance(authority, ProductionAutomaticPlanAuthorityV2)
    child = _Materializer(context)(context, proposal, proposal.segments[0])
    try:
        published = sidebar.publish(
            child.report, child.wiring, ExecutionCorrelation("context.child", "node.shell")
        )
    finally:
        child.release()
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production,
        output_root_factory=lambda: tmp_path / "output",
        private_root_factory=lambda: tmp_path / "private",
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
    )
    resource = _managed_start_resource_request(count)
    coordinator.reserve_managed_sequence_resources(resource)
    manifest = derive_segment_manifests(authority.workspace)[0]
    observation = ManagedPreparedGraphObservationV1.from_wire(
        {**_observation(), "expected_frames": manifest.duration.frame_count}
    )
    request = ManagedChildBindingRequestV1(
        context_workspace_handle=published.workspace_id,
        execution=EligibleSegmentExecutionV1(
            parent_sequence_id=resource.parent_sequence_id,
            parent_authorization_fingerprint=resource.parent_authorization_fingerprint,
            segment_id=manifest.segment_id,
            slot_revision=2,
            attempt_epoch=1,
            materialization_receipt_fingerprint=authority.materialization_receipts[0].fingerprint,
            predecessor_terminal_fingerprint=None,
            request_id="prepare.original.child",
        ),
        graph_fingerprint=observation.graph_fingerprint,
        compiled_prompt_fingerprint=observation.compiled_prompt_fingerprint,
        correlation=ManagedChildCorrelationV1(
            pending_correlation_id="managed.pending.child", execution_node_id="node.shell"
        ),
        observation=observation,
        production_workspace_handle=created.workspace_handle,
        source_authority=authority,
    )
    return coordinator, request, authority


@pytest.mark.parametrize("count", [2, 4, 15])
def test_original_child_response_fits_reserved_capacity_and_replays(
    tmp_path: Path, count: int
) -> None:
    coordinator, request, authority = _original_child(tmp_path, count)
    claim = coordinator.bind_managed_sequence_child(request)
    entry = coordinator._entry(claim.child_run_handle)
    response = coordinator._response(entry, "current")
    assert len(canonical_bytes(response.to_wire())) <= MAX_COORDINATOR_RESPONSE_BYTES
    assert entry.workspace is authority.workspace
    action = _action("read.child", "read_sequence", {"run_handle": claim.child_run_handle})
    first = coordinator.dispatch(action)
    replay = coordinator.dispatch(action)
    assert first.response is replay.response
    assert first.response is not None
    assert first.response.sequence.state is entry.state
    command = response.sequence.eligible_commands[0]
    submitted_action = _action(
        "submit.child",
        "record_submission",
        {
            "run_handle": claim.child_run_handle,
            "expected_state_fingerprint": entry.state.fingerprint,
            "job_id": command.job_id,
            "transaction_id": command.transaction_id,
            "graph_fingerprint": entry.observation.graph_fingerprint,
            "compiled_prompt_fingerprint": entry.observation.compiled_prompt_fingerprint,
            "queue_prompt_id": "q" * 128,
        },
    )
    submitted = coordinator.dispatch(submitted_action)
    submitted_state = entry.state
    assert coordinator.dispatch(submitted_action).response is submitted.response
    assert entry.state is submitted_state
    running_action = _action(
        "running.child",
        "record_running",
        {
            "run_handle": claim.child_run_handle,
            "expected_state_fingerprint": entry.state.fingerprint,
            "queue_prompt_id": "q" * 128,
            "host_owner_id": "h" * 128,
        },
    )
    running = coordinator.dispatch(running_action)
    running_state = entry.state
    assert coordinator.dispatch(running_action).response is running.response
    assert entry.state is running_state
    assert len(coordinator._ledger) == 3


@pytest.mark.parametrize("count", [2, 4, 15])
def test_every_original_child_phase_preserves_reference_with_maximum_identifiers(
    tmp_path: Path, count: int
) -> None:
    coordinator, request, authority = _original_child(tmp_path, count)
    claim = coordinator.bind_managed_sequence_child(request)
    entry = coordinator._entry(claim.child_run_handle)
    original = coordinator._response(entry, "current")
    assert original.schema == MANAGED_CHILD_RESPONSE_SCHEMA
    reference = original.production_authority
    assert reference is not None
    assert reference.automatic_plan_fingerprint == authority.fingerprint
    manifests = derive_segment_manifests(authority.workspace)
    sizes: list[int] = []
    correlation = ExecutionCorrelation("p" * 128, "n" * 128)
    for manifest in manifests:
        observation = entry.observation
        spec = GenerationJobSpec(
            segment_id=manifest.segment_id,
            job_id=f"job.{manifest.ordinal}." + "x" * 100,
            graph_fingerprint=observation.graph_fingerprint,
            compiled_prompt_fingerprint=observation.compiled_prompt_fingerprint,
            model_fingerprint=observation.model_fingerprint,
            runtime_fingerprint=observation.runtime_fingerprint,
            expected_format="observed_video",
            expected_shape=(manifest.duration.frame_count,),
            timeout_ms=observation.timeout_ms,
            fingerprint_domain=observation.fingerprint_domain,
        )
        recompute = extend_recompute_plan(
            manifests,
            plan_recompute(manifests, manifests),
            forced_dirty_segment_ids=(manifest.segment_id,),
            forced_reason_codes=((manifest.segment_id, ("initial_managed_generation",)),),
        )
        state = create_generation_sequence_state(
            build_generation_sequence_plan(authority.workspace, manifests, recompute, (spec,))
        )
        receipt: SegmentArtifactReceipt | None = None

        def response(
            state: GenerationSequenceState, receipt: SegmentArtifactReceipt | None = None
        ) -> SequenceCoordinatorResponse:
            sequence = build_generation_sequence_projection(state, correlation)
            production = ProductionWorkspaceRegistry.project_managed_child_generation(
                authority.workspace, entry.production_handle, sequence, receipt
            )
            value = SequenceCoordinatorResponse(
                entry.run_handle,
                "current",
                sequence,
                production,
                None
                if receipt is None
                else SequenceArtifactAuthorityV1(receipt.fingerprint, receipt.byte_length),
                None if receipt is None else _fp("terminal"),
                MANAGED_CHILD_RESPONSE_SCHEMA,
                production_authority=reference,
            )
            wire = value.to_wire()
            assert "production" not in wire
            assert wire["production_authority"] == reference.to_wire()
            sizes.append(len(canonical_bytes(wire)))
            return value

        planned = response(state)
        bound = coordinator_module._managed_child_response_size_bound(planned)
        assert bound <= MAX_COORDINATOR_RESPONSE_BYTES
        command = planned.sequence.eligible_commands[0]
        state = record_generation_projection(
            state,
            spec.job_id,
            transaction_id=command.transaction_id,
            graph_fingerprint=spec.graph_fingerprint,
            compiled_prompt_fingerprint=spec.compiled_prompt_fingerprint,
            fingerprint_domain=spec.fingerprint_domain,
        )
        response(state)
        state = record_generation_submission(state, spec.job_id, queue_prompt_id="q" * 128)
        response(state)
        state = record_generation_running(state, spec.job_id, host_owner_id="h" * 128)
        response(state)
        running = state
        state = record_generation_failure(
            state, spec.job_id, result_fingerprint=_fp("failed"), failure_code="f" * 128
        )
        response(state)
        state = running
        transaction = state.runtimes[0].transaction
        assert transaction is not None
        receipt = complete_segment_artifact_receipt(
            begin_segment_artifact_receipt(
                manifest=manifest,
                transaction=transaction,
                artifact_id=f"artifact.{manifest.ordinal}",
                model_fingerprint=spec.model_fingerprint,
                runtime_fingerprint=spec.runtime_fingerprint,
                execution_fingerprint=_fp(f"execution.{manifest.ordinal}"),
                predecessor_artifact_fingerprint=None,
                format_label="mp4",
                shape=(manifest.duration.frame_count, 512, 512, 3),
                created_at_ms=100,
                expires_at_ms=600_000,
            ),
            output_fingerprint=_fp(f"output.{manifest.ordinal}"),
            byte_length=128 * 1024 * 1024,
        )
        state = record_generation_success(
            state, spec.job_id, result_fingerprint=_fp("succeeded"), receipt=receipt
        )
        response(state, receipt)
        assert max(sizes[-6:]) <= bound
    assert len(sizes) == count * 6


def test_response_capacity_refusal_precedes_child_binding_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, request, authority = _original_child(tmp_path, 15)
    resource = coordinator._managed_start_resources[request.execution.parent_sequence_id]
    monkeypatch.setattr(coordinator_module, "MAX_COORDINATOR_RESPONSE_BYTES", 1)
    with pytest.raises(ManagedSequenceServiceError, match="child_response_capacity"):
        coordinator.bind_managed_sequence_child(request)
    assert not coordinator._runs
    assert not coordinator._managed._runs
    assert not coordinator._ledger
    assert resource.bound_child_run_handle is None
    assert resource.original_authority is None
    assert not resource.child_slot_released
    assert not resource.artifact_capacity_released
    assert request.production_workspace_handle is not None
    assert (
        coordinator._production.claim_automatic_plan_authority(
            request.production_workspace_handle,
            expected_workspace_revision=authority.workspace.revision,
            expected_workspace_fingerprint=authority.workspace.fingerprint,
            expected_plan_fingerprint=authority.fingerprint,
        )
        is authority
    )


def test_compact_response_rejects_cross_authority_and_preserves_legacy_wire(tmp_path: Path) -> None:
    coordinator, request, _ = _original_child(tmp_path, 2)
    claim = coordinator.bind_managed_sequence_child(request)
    response = coordinator._response(coordinator._entry(claim.child_run_handle), "current")
    authority = response.production_authority
    assert authority is not None
    for changes in (
        {"workspace_handle": "pw_" + "x" * 32},
        {"workspace_id": "foreign.workspace"},
        {"workspace_revision": authority.workspace_revision + 1},
        {"workspace_fingerprint": _fp("foreign")},
    ):
        with pytest.raises(SequenceCoordinatorError, match="cross_authority_response"):
            replace(response, production_authority=replace(authority, **changes))
    with pytest.raises(SequenceCoordinatorError, match="invalid_response"):
        replace(response, schema=COORDINATOR_RESPONSE_SCHEMA)
    legacy = replace(response, schema=COORDINATOR_RESPONSE_SCHEMA, production_authority=None)
    assert legacy.to_wire() == {
        "schema": COORDINATOR_RESPONSE_SCHEMA,
        "run_handle": response.run_handle,
        "disposition": "current",
        "sequence": response.sequence.to_wire(),
        "production": response.production.to_wire(),
        "artifact_authority": None,
        "terminal_fingerprint": None,
    }
