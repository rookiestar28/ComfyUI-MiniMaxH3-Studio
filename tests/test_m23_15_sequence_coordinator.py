from __future__ import annotations

import importlib
import json
import struct
import time
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import comfyui_h3_context.adapters.comfyui_sequence_coordinator as coordinator_adapter
import comfyui_h3_context.adapters.media_preview_authority as preview_authority_module
from comfyui_h3_context.adapters.comfyui_input_geometry import (
    INPUT_GEOMETRY_REQUEST_SCHEMA,
    GeometryPreflightError,
    InputGeometryRegistry,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    COORDINATOR_ACTION_SCHEMA,
    COORDINATOR_ERROR_SCHEMA,
    MAX_COORDINATOR_LEDGER,
    MAX_COORDINATOR_RESPONSE_BYTES,
    MAX_COORDINATOR_RETAINED_BYTES,
    MAX_COORDINATOR_RUNS,
    PREPARED_GRAPH_OBSERVATION_SCHEMA,
    ObservedVideoArtifact,
    PreparedGraphObservation,
    SequenceCoordinatorDispatchResult,
    SequenceCoordinatorError,
    SequenceCoordinatorRegistry,
    SequenceCoordinatorResponse,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.managed_run_registry import ManagedRunRegistryError
from comfyui_h3_context.adapters.managed_sequence_service import (
    ManagedChildBindingRequestV1,
    ManagedChildCorrelationV1,
    ManagedPreparedGraphObservationV1,
    ManagedSequenceServiceError,
    ManagedSequenceStartResourceRequestV1,
)
from comfyui_h3_context.adapters.media_preview_authority import MediaPreviewSourceError
from comfyui_h3_context.adapters.segment_artifact_store import (
    ArtifactStoreError,
    PrivateSegmentArtifactStore,
)
from comfyui_h3_context.core import ExecutionCorrelation, TaskMode, build_native_h3_wiring
from comfyui_h3_context.core.generation_sequence import GenerationSequenceProjection
from comfyui_h3_context.core.managed_sequence import (
    EligibleSegmentExecutionV1,
    ManagedModeQualificationV1,
    ManagedSequenceAuthorizationV1,
    ManagedSequenceSegmentAuthorizationV1,
    SequenceSlotStateV1,
    authorize_managed_sequence,
    bind_prepared_child,
    expire_managed_sequence_lease,
    prepare_sequence_child,
    record_managed_sequence_canvas_write,
    record_managed_sequence_submission,
    start_managed_sequence,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.safe_paths import UnsafePathError
from comfyui_h3_context.core.segment_artifacts import SegmentArtifactReceipt
from comfyui_h3_context.core.segment_workspace import derive_segment_manifests
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)


def _fp(label: str) -> str:
    return f"sha256:{sha256(label.encode('ascii')).hexdigest()}"


def _production_context() -> tuple[ProductionWorkspaceRegistry, str]:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "Synthetic M23-15 managed-run fixture.",
        duration_seconds=8.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    wiring = build_native_h3_wiring(report)
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    context = sidebar.publish(
        report,
        wiring,
        ExecutionCorrelation("prompt.bootstrap", "node.product.shell"),
    )
    production = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=4,
        ttl_seconds=60,
        terminal_ttl_seconds=60,
    )
    return production, context.workspace_id


def _production_workspace() -> tuple[ProductionWorkspaceRegistry, ProductionWorkbenchProjection]:
    production, context_workspace_handle = _production_context()
    created = production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.create.m23-15",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": context_workspace_handle},
        }
    )
    projection = created.projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    return production, projection


def _observation() -> dict[str, object]:
    return {
        "schema": PREPARED_GRAPH_OBSERVATION_SCHEMA,
        "route": "existing",
        "graph_fingerprint": _fp("graph.full"),
        "compiled_prompt_fingerprint": _fp("prompt.full"),
        "owned_projection_fingerprint": _fp("graph.owned"),
        "owned_node_ids": ["1", "8", "45"],
        "owned_link_ids": ["15", "17", "18"],
        "model_fingerprint": _fp("model.content-free"),
        "runtime_fingerprint": _fp("runtime.content-free"),
        "fingerprint_domain": "output_producing_graph",
        "expected_frames": 192,
        "source_identity": None,
        "timeout_ms": 60_000,
        "native_anchor_node_id": "node.native.h3",
    }


def _action(request_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema": COORDINATOR_ACTION_SCHEMA,
        "request_id": request_id,
        "action": action,
        "payload": payload,
    }


def _coordinator(
    production: ProductionWorkspaceRegistry,
    output_root: Path,
    private_root: Path,
    *,
    max_runs: int = MAX_COORDINATOR_RUNS,
    token_factory: Callable[[], str] = lambda: "m" * 40,
) -> SequenceCoordinatorRegistry:
    return SequenceCoordinatorRegistry(
        production_registry=production,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: private_root,
        artifact_inspector=lambda payload, **expected: ObservedVideoArtifact(
            "mp4", (192, 512, 512, 3)
        ),
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=token_factory,
        max_runs=max_runs,
    )


def _required_response(
    result: SequenceCoordinatorDispatchResult,
) -> SequenceCoordinatorResponse:
    assert result.response is not None
    return result.response


def _managed_start_resource_request(count: int = 15) -> ManagedSequenceStartResourceRequestV1:
    per_artifact = min(128 * 1024 * 1024, (1024 * 1024 * 1024) // count)
    rows = 2 + (7 * count) + 8
    return ManagedSequenceStartResourceRequestV1(
        parent_sequence_id="managed." + "p" * 40,
        parent_authorization_fingerprint=_fp("managed.parent.authorization"),
        segment_count=count,
        coordinator_reserved_rows=rows,
        coordinator_reserved_bytes=rows * MAX_COORDINATOR_RESPONSE_BYTES,
        artifact_reserved_entries=count,
        artifact_reserved_bytes=per_artifact * count,
        per_artifact_max_bytes=per_artifact,
        idle_expires_at=150.0,
        expires_at=200.0,
        expires_at_epoch_ms=200_000,
    )


def _managed_child_request(
    production_registry: ProductionWorkspaceRegistry,
    context_handle: str,
    resource_request: ManagedSequenceStartResourceRequestV1,
    *,
    suffix: str,
    slot_revision: int,
) -> ManagedChildBindingRequestV1:
    probe_request_id = f"managed.child.segment.probe.{suffix}"
    probe = production_registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": probe_request_id,
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": context_handle},
        }
    )
    probe_projection = probe.projection
    assert isinstance(probe_projection, ProductionWorkbenchProjection)
    segment_id = probe_projection.segments[0].segment_id
    production_registry._rollback_unpublished_creation(
        request_id=probe_request_id,
        workspace_handle=probe_projection.workspace_handle,
        expected_workspace_revision=probe_projection.workspace_revision,
        expected_workspace_fingerprint=probe_projection.workspace_fingerprint,
    )
    execution = EligibleSegmentExecutionV1(
        parent_sequence_id=resource_request.parent_sequence_id,
        parent_authorization_fingerprint=(resource_request.parent_authorization_fingerprint),
        segment_id=segment_id,
        slot_revision=slot_revision,
        attempt_epoch=1,
        materialization_receipt_fingerprint=_fp(f"materialization.segment.{suffix}"),
        predecessor_terminal_fingerprint=None,
        request_id=f"prepare.managed.child.segment.{suffix}",
    )
    return ManagedChildBindingRequestV1(
        context_workspace_handle=context_handle,
        execution=execution,
        graph_fingerprint=_fp("graph.full"),
        compiled_prompt_fingerprint=_fp("prompt.full"),
        correlation=ManagedChildCorrelationV1(
            pending_correlation_id=f"managed.pending.{suffix}",
            execution_node_id="node.product.shell",
        ),
        observation=ManagedPreparedGraphObservationV1.from_wire(_observation()),
    )


def test_managed_start_reserves_shared_ledger_child_slot_and_artifact_capacity(
    tmp_path: Path,
) -> None:
    production_registry, production = _production_workspace()
    coordinator = _coordinator(
        production_registry,
        tmp_path / "output",
        tmp_path / "private",
    )
    request = _managed_start_resource_request()

    claim = coordinator.reserve_managed_sequence_resources(request)
    assert claim.request_fingerprint == request.fingerprint
    claim.consume("start.sequence.1", 1)
    claim.consume("start.sequence.1", 1)
    with pytest.raises(ManagedSequenceServiceError, match="resource_consumption_conflict"):
        claim.consume("start.sequence.1", 2)

    prepared = _prepare(coordinator, production)
    for index in range(12):
        coordinator.dispatch(
            _action(
                f"coordinator.capacity.read.{index}",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    with pytest.raises(SequenceCoordinatorError, match="coordinator_ledger_capacity"):
        coordinator.dispatch(
            _action(
                "coordinator.capacity.read.refused",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )

    # The prepared legacy run already occupies one of the sixteen live slots. Fourteen more plus
    # the M26 reservation fill the table without evicting either owner.
    for index in range(14):
        coordinator._managed.create("mc_" + f"{index:040d}")
    with pytest.raises(ManagedRunRegistryError, match="live managed runs exceed their bound"):
        coordinator._managed.create("mc_" + "9" * 40)

    store = coordinator._store()
    with pytest.raises(ArtifactStoreError, match="capacity_bytes_unavailable"):
        store.reserve_capacity(
            reservation_id="foreign.capacity.blocked",
            parent_sequence_id="foreign.parent",
            parent_authorization_fingerprint=_fp("foreign.parent.authorization"),
            segment_count=1,
            expires_at_ms=150_000,
        )

    claim.release()
    claim.release()
    coordinator._managed.create("mc_" + "9" * 40)
    coordinator.dispatch(
        _action(
            "coordinator.capacity.read.after-release",
            "read_sequence",
            {"run_handle": prepared.run_handle},
        )
    )
    foreign = store.reserve_capacity(
        reservation_id="foreign.capacity.admitted",
        parent_sequence_id="foreign.parent",
        parent_authorization_fingerprint=_fp("foreign.parent.authorization"),
        segment_count=1,
        expires_at_ms=150_000,
    )
    assert foreign.reserved_entries == 1


def test_managed_and_legacy_paths_share_the_exact_coordinator_run_bound(tmp_path: Path) -> None:
    production_registry, production = _production_workspace()
    managed_first = _coordinator(
        production_registry,
        tmp_path / "managed-first-output",
        tmp_path / "managed-first-private",
        max_runs=1,
    )
    claim = managed_first.reserve_managed_sequence_resources(_managed_start_resource_request(1))
    with pytest.raises(SequenceCoordinatorError, match="coordinator_capacity"):
        _prepare(managed_first, production)
    claim.release()

    second_production_registry, second_production = _production_workspace()
    legacy_first = _coordinator(
        second_production_registry,
        tmp_path / "legacy-first-output",
        tmp_path / "legacy-first-private",
        max_runs=1,
    )
    _prepare(legacy_first, second_production)
    with pytest.raises(ManagedSequenceServiceError, match="coordinator_run_capacity"):
        legacy_first.reserve_managed_sequence_resources(_managed_start_resource_request(1))


def test_managed_child_binder_builds_one_reserved_child_without_legacy_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_registry, context_handle = _production_context()
    coordinator = _coordinator(
        production_registry,
        tmp_path / "output",
        tmp_path / "private",
    )
    resource_request = _managed_start_resource_request(1)
    coordinator.reserve_managed_sequence_resources(resource_request)
    request = _managed_child_request(
        production_registry,
        context_handle,
        resource_request,
        suffix="1",
        slot_revision=2,
    )
    monkeypatch.setattr(
        coordinator,
        "_prepare",
        lambda *_args, **_kwargs: pytest.fail("managed binder called legacy _prepare"),
    )

    binding = coordinator.bind_managed_sequence_child(request)
    entry = coordinator._entry(binding.child_run_handle)

    assert len(entry.workspace.segments) == 1
    assert len(entry.state.plan.jobs) == 1
    assert binding.child_state_fingerprint == entry.state.fingerprint
    assert binding.child_job_id == entry.state.plan.jobs[0].job_id
    prepared_projection = coordinator._response(entry, "prepared").sequence
    assert binding.child_transaction_id == prepared_projection.eligible_commands[0].transaction_id
    assert coordinator._managed.read(binding.child_run_handle).prepared_sequence == (
        binding.child_run_handle
    )

    production_handle = entry.production_handle
    binding.rollback()
    binding.rollback()
    with pytest.raises(SequenceCoordinatorError, match="run_unavailable"):
        coordinator._entry(binding.child_run_handle)
    with pytest.raises(ProductionWorkbenchError, match="workspace_unavailable"):
        production_registry.claim_workspace_authority(
            production_handle,
            expected_workspace_revision=entry.workspace.revision,
            expected_workspace_fingerprint=entry.workspace.fingerprint,
        )


@pytest.mark.parametrize("host_owned", (False, True))
def test_parent_first_expiry_classifies_the_exact_bound_child_transaction(
    tmp_path: Path,
    host_owned: bool,
) -> None:
    production_registry, context_handle = _production_context()
    now = [100.0]
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: tmp_path / "output",
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=lambda payload, **expected: ObservedVideoArtifact(
            "mp4", (192, 512, 512, 3)
        ),
        clock=lambda: now[0],
        clock_ms=lambda: round(now[0] * 1_000),
        token_factory=lambda: "m" * 40,
    )
    base_resource = _managed_start_resource_request(1)
    probe = _managed_child_request(
        production_registry,
        context_handle,
        base_resource,
        suffix="expiry.probe",
        slot_revision=2,
    )
    segment_id = probe.execution.segment_id
    segment = ManagedSequenceSegmentAuthorizationV1(
        segment_id=segment_id,
        ordinal=1,
        task_mode=TaskMode.T2VA,
        duration_milliseconds=8_000,
        frame_count=192,
        manifest_fingerprint=_fp("manifest.expiry"),
        materialization_receipt_fingerprint=_fp("materialization.segment.expiry"),
        local_prompt_fingerprint=_fp("prompt.expiry"),
        intent_graph_fingerprint=_fp("intent.expiry"),
        profile_fingerprint=_fp("profile.expiry"),
        capability_fingerprint=_fp("capability.expiry"),
        predecessor_segment_id=None,
        predecessor_cut_receipt_fingerprint=None,
    )
    authorization = ManagedSequenceAuthorizationV1(
        sequence_id=base_resource.parent_sequence_id,
        workspace_id="production.workspace.expiry",
        workspace_revision=1,
        workspace_fingerprint=_fp("workspace.expiry"),
        production_plan_fingerprint=_fp("production.plan.expiry"),
        proposal_fingerprint=_fp("proposal.expiry"),
        generation_plan_fingerprint=_fp("generation.plan.expiry"),
        compiler_fingerprint=_fp("compiler.expiry"),
        host_capability_fingerprint=_fp("capability.expiry"),
        segments=(segment,),
    )
    resource = replace(
        base_resource,
        parent_authorization_fingerprint=authorization.fingerprint,
    )
    coordinator.reserve_managed_sequence_resources(resource)
    child_request = _managed_child_request(
        production_registry,
        context_handle,
        resource,
        suffix="expiry",
        slot_revision=2,
    )
    binding = coordinator.bind_managed_sequence_child(child_request)
    child_entry = coordinator._entry(binding.child_run_handle)
    production_handle = child_entry.production.workspace_handle
    started = start_managed_sequence(
        authorize_managed_sequence(authorization, now=10.0),
        ManagedModeQualificationV1(
            host_capability_fingerprint=authorization.host_capability_fingerprint,
            compiler_fingerprint=authorization.compiler_fingerprint,
            qualified_global_modes=tuple(TaskMode),
            qualified_materialization_receipts=(segment.materialization_receipt_fingerprint,),
            observed_at=10.0,
            expires_at=2_000.0,
        ),
        now=20.0,
    )
    prepared = prepare_sequence_child(
        started,
        segment_id=segment_id,
        expected_revision=started.revision,
        materialization_receipt_fingerprint=segment.materialization_receipt_fingerprint,
        predecessor_terminal_fingerprint=None,
        request_id="prepare.parent.expiry",
    )
    before = bind_prepared_child(
        prepared.sequence,
        segment_id=segment_id,
        expected_revision=prepared.sequence.revision,
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        child_run_handle=binding.child_run_handle,
        child_state_fingerprint=binding.child_state_fingerprint,
        graph_fingerprint=child_request.graph_fingerprint,
        compiled_prompt_fingerprint=child_request.compiled_prompt_fingerprint,
        previous_owned_projection_fingerprint=(
            child_request.observation.owned_projection_fingerprint
        ),
        active_workflow_fingerprint=_fp("workflow.expiry"),
    )
    assert before.slots[0].state is SequenceSlotStateV1.BOUND
    assert before.lease is not None
    if host_owned:
        coordinator_submitted = _submit(
            coordinator,
            coordinator._response(child_entry, "prepared"),
        )
        before = record_managed_sequence_canvas_write(
            before,
            segment_id=segment_id,
            expected_revision=before.revision,
            active_workflow_fingerprint=_fp("workflow.expiry"),
            previous_owned_projection_fingerprint=(
                child_request.observation.owned_projection_fingerprint
            ),
            written_owned_projection_fingerprint=_fp("owned.expiry.written"),
        )
        before = record_managed_sequence_submission(
            before,
            segment_id=segment_id,
            expected_revision=before.revision,
            child_run_handle=binding.child_run_handle,
            child_state_fingerprint=coordinator_submitted.sequence.state.fingerprint,
            queue_prompt_id="prompt.model.1",
            timeout_ms=60_000,
            now=now[0],
        )
        assert before.lease is not None and before.lease.active_deadline is not None
        now[0] = before.lease.active_deadline + 0.001
    else:
        assert before.lease is not None
        now[0] = before.lease.idle_deadline + 0.001
    injected_failure = [True]
    candidates = []

    def expire_parent(parent_sequence_id: str, observed: float) -> None:
        assert parent_sequence_id == authorization.sequence_id
        if injected_failure[0]:
            raise RuntimeError("injected parent transition failure")
        candidate = expire_managed_sequence_lease(
            before,
            expected_revision=before.revision,
            now=observed,
        )
        candidates.append(candidate)
        coordinator.transition_managed_sequence_lease(before, candidate, observed)

    coordinator.bind_managed_sequence_lease_transition(expire_parent)
    with pytest.raises(SequenceCoordinatorError, match="managed_lease_transition_failed"):
        coordinator.dispatch(
            _action(
                "managed.expiry.failure",
                "read_sequence",
                {"run_handle": binding.child_run_handle},
            )
        )
    state = coordinator._managed_start_resources[resource.parent_sequence_id]
    assert state.expiry_pending is True
    assert coordinator._entry(binding.child_run_handle).run_handle == binding.child_run_handle

    injected_failure[0] = False
    if host_owned:
        current = coordinator.dispatch(
            _action(
                "managed.expiry.success",
                "read_sequence",
                {"run_handle": binding.child_run_handle},
            )
        )
        assert current.response is not None
    else:
        with pytest.raises(SequenceCoordinatorError, match="run_gone"):
            coordinator.dispatch(
                _action(
                    "managed.expiry.success",
                    "read_sequence",
                    {"run_handle": binding.child_run_handle},
                )
            )

    state = coordinator._managed_start_resources[resource.parent_sequence_id]
    assert state.bound_child_run_handle == (binding.child_run_handle if host_owned else None)
    assert state.expiry_pending is False
    assert state.expiry_transition_complete is True
    assert len(candidates) == 1
    if host_owned:
        assert candidates[0].state.value == "paused_unknown_ownership"
        assert candidates[0].slots[0].state is SequenceSlotStateV1.UNKNOWN_OWNERSHIP
        assert candidates[0].slots[0].queue_prompt_id == "prompt.model.1"
        assert coordinator._entry(binding.child_run_handle).run_handle == binding.child_run_handle
        protected = coordinator._managed._runs[binding.child_run_handle]
        assert protected.parent_transition_protected is False
        assert before.lease is not None
        assert protected.detached_deadline == before.lease.active_deadline
    else:
        with pytest.raises(SequenceCoordinatorError, match="run_gone"):
            coordinator._entry(binding.child_run_handle)
        with pytest.raises(ProductionWorkbenchError, match="workspace_gone"):
            production_registry.claim_workspace_authority(
                production_handle,
                expected_workspace_revision=child_entry.production.workspace_revision,
                expected_workspace_fingerprint=child_entry.production.workspace_fingerprint,
            )


def test_managed_child_release_reuses_the_slot_and_reclaims_its_production_workspace(
    tmp_path: Path,
) -> None:
    production_registry, context_handle = _production_context()
    child_tokens = iter(("m" * 40, "n" * 40))
    coordinator = _coordinator(
        production_registry,
        tmp_path / "output",
        tmp_path / "private",
        token_factory=lambda: next(child_tokens),
    )
    resource_request = _managed_start_resource_request(2)
    coordinator.reserve_managed_sequence_resources(resource_request)
    first_request = _managed_child_request(
        production_registry,
        context_handle,
        resource_request,
        suffix="1",
        slot_revision=2,
    )
    first = coordinator.bind_managed_sequence_child(first_request)
    first_entry = coordinator._entry(first.child_run_handle)

    released = _required_response(
        coordinator.dispatch(
            _action(
                "managed.child.release.1",
                "release_sequence",
                {"run_handle": first.child_run_handle},
            )
        )
    )

    assert released.disposition == "released"
    assert (
        coordinator._managed_start_resources[
            resource_request.parent_sequence_id
        ].bound_child_run_handle
        is None
    )
    with pytest.raises(ProductionWorkbenchError, match="workspace_gone"):
        production_registry.claim_workspace_authority(
            first_entry.production.workspace_handle,
            expected_workspace_revision=first_entry.production.workspace_revision,
            expected_workspace_fingerprint=first_entry.production.workspace_fingerprint,
        )

    second_request = _managed_child_request(
        production_registry,
        context_handle,
        resource_request,
        suffix="2",
        slot_revision=3,
    )
    second = coordinator.bind_managed_sequence_child(second_request)
    assert second.child_run_handle != first.child_run_handle
    second.rollback()


def test_managed_child_artifact_consumes_the_exact_parent_capacity_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "comfyui_h3_context.adapters.comfyui_media_preview.ensure_media_preview_route_registered",
        lambda: True,
    )
    production_registry, context_handle = _production_context()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(
        production_registry,
        output_root,
        tmp_path / "private",
    )
    resource_request = _managed_start_resource_request(2)
    coordinator.reserve_managed_sequence_resources(resource_request)
    request = _managed_child_request(
        production_registry,
        context_handle,
        resource_request,
        suffix="artifact",
        slot_revision=2,
    )
    binding = coordinator.bind_managed_sequence_child(request)
    prepared = coordinator._response(
        coordinator._entry(binding.child_run_handle),
        "prepared",
    )
    assert prepared.sequence.correlation.prompt_id == "managed.pending.artifact"
    submitted = _submit(coordinator, prepared)
    assert submitted.sequence.correlation.prompt_id == "prompt.model.1"
    payload = b"managed-sequence-reserved-artifact"
    artifact = output_root / "managed-reserved.mp4"
    artifact.write_bytes(payload)

    coordinator.dispatch(
        _action(
            "managed.child.artifact.1",
            "record_artifact",
            {
                "run_handle": binding.child_run_handle,
                "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                "queue_prompt_id": "prompt.model.1",
                "output_node_id": "node.save.video",
                "locator": {
                    "filename": artifact.name,
                    "subfolder": "",
                    "type": "output",
                },
            },
        )
    )

    resource = coordinator._managed_start_resources[resource_request.parent_sequence_id]
    assert resource.artifact_capacity is not None
    reservation = coordinator._store().read_capacity_reservation(resource.artifact_capacity)
    assert reservation.consumed_entries == 1
    assert reservation.consumed_bytes == len(payload)


def _prepare(
    coordinator: SequenceCoordinatorRegistry,
    production: ProductionWorkbenchProjection,
) -> SequenceCoordinatorResponse:
    return _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.prepare.1",
                "prepare_sequence",
                {
                    "workspace_handle": production.workspace_handle,
                    "expected_workspace_revision": production.workspace_revision,
                    "expected_workspace_fingerprint": production.workspace_fingerprint,
                    "correlation": {
                        "prompt_id": "prompt.bootstrap",
                        "execution_node_id": "node.product.shell",
                    },
                    "observation": _observation(),
                },
            )
        )
    )


def _submit(
    coordinator: SequenceCoordinatorRegistry,
    prepared: SequenceCoordinatorResponse,
) -> SequenceCoordinatorResponse:
    command = prepared.sequence.eligible_commands[0]
    return _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.submit.1",
                "record_submission",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": prepared.sequence.state.fingerprint,
                    "job_id": command.job_id,
                    "transaction_id": command.transaction_id,
                    "graph_fingerprint": command.job.graph_fingerprint,
                    "compiled_prompt_fingerprint": command.job.compiled_prompt_fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                },
            )
        )
    )


class _CancelledPreview:
    def is_cancelled(self) -> bool:
        return True


def _complete_managed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    route_available: bool = True,
    observed: ObservedVideoArtifact | None = None,
) -> tuple[
    ProductionWorkspaceRegistry,
    SequenceCoordinatorRegistry,
    SequenceCoordinatorResponse,
    SequenceCoordinatorResponse,
    SegmentArtifactReceipt,
    bytes,
]:
    monkeypatch.setattr(
        "comfyui_h3_context.adapters.comfyui_media_preview.ensure_media_preview_route_registered",
        lambda: route_available,
    )
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    private_root = tmp_path / "private"
    artifact = output_root / "managed.mp4"
    payload = b"synthetic-video-payload"
    artifact.write_bytes(payload)
    admitted = observed or ObservedVideoArtifact("mp4", (192, 512, 512, 3))
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: private_root,
        artifact_inspector=lambda _payload, **_expected: admitted,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    artifact_recorded = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.helper.artifact",
                "record_artifact",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "output_node_id": "node.save.video",
                    "locator": {
                        "filename": artifact.name,
                        "subfolder": "",
                        "type": "output",
                    },
                },
            )
        )
    )
    completed = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.helper.terminal",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": artifact_recorded.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "success",
                },
            )
        )
    )
    receipt = coordinator._runs[prepared.run_handle].artifact_receipt
    assert receipt is not None
    return production_registry, coordinator, submitted, completed, receipt, payload


@pytest.mark.parametrize(
    ("code", "category"),
    (
        ("artifact_shape_mismatch", "artifact_content_invalid"),
        ("artifact_locator_unsafe", "artifact_locator_rejected"),
        ("transaction_manifest_authority_mismatch", "artifact_authority_mismatch"),
        ("artifact_store_failed", "artifact_store_unavailable"),
        ("host_storage_unavailable", "artifact_store_unavailable"),
        ("run_unavailable", "run_authority_mismatch"),
        ("future_private_failure", "unsupported_failure"),
        ("internal_failure", "internal_failure"),
    ),
)
def test_safe_error_categories_have_one_closed_privacy_safe_wire_shape(
    code: str,
    category: str,
) -> None:
    error = SequenceCoordinatorError(code, 422)

    assert error.to_wire() == {
        "schema": COORDINATOR_ERROR_SCHEMA,
        "category": category,
        "retry_disposition": "none",
        "same_run_authority": False,
    }


@pytest.mark.parametrize("terminal_first", (True, False))
def test_managed_sequence_exists_before_one_submission_and_closes_only_after_artifact_and_terminal(
    tmp_path: Path,
    terminal_first: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "comfyui_h3_context.adapters.comfyui_media_preview.ensure_media_preview_route_registered",
        lambda: True,
    )
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    private_root = tmp_path / "private"
    artifact = output_root / "video" / "managed_00001_.mp4"
    artifact.parent.mkdir()
    artifact.write_bytes(b"synthetic-video-payload")
    coordinator = _coordinator(production_registry, output_root, private_root)

    prepared = _prepare(coordinator, production)
    assert prepared.disposition == "prepared"
    assert prepared.artifact_authority is None
    assert prepared.terminal_fingerprint is None
    assert prepared.sequence.progress[0].state.value == "planned"
    assert len(prepared.sequence.eligible_commands) == 1
    assert prepared.production.run_total == 1
    assert prepared.production.run_completed == 0

    submitted = _submit(coordinator, prepared)
    assert submitted.sequence.progress[0].state.value == "submitted"
    assert submitted.production.run_completed == 0

    terminal_payload: dict[str, object] = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "kind": "success",
    }
    artifact_payload: dict[str, object] = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
        "locator": {
            "filename": artifact.name,
            "subfolder": "video",
            "type": "output",
        },
    }
    first_action, first_payload = (
        ("record_terminal", terminal_payload)
        if terminal_first
        else ("record_artifact", artifact_payload)
    )
    first = _required_response(
        coordinator.dispatch(_action("coordinator.first.1", first_action, first_payload))
    )
    assert first.disposition in {"verification_pending", "artifact_verified"}
    assert first.production.run_completed == 0
    assert first.sequence.progress[0].state.value == "running"
    if first_action == "record_artifact":
        receipt = coordinator._runs[prepared.run_handle].artifact_receipt
        assert receipt is not None
        assert first.artifact_authority is not None
        assert first.artifact_authority.receipt_fingerprint == receipt.fingerprint
        assert first.artifact_authority.byte_length == len(b"synthetic-video-payload")
        assert first.terminal_fingerprint is None
        assert len(first.production.outputs) == 1
        assert first.production.outputs[0].preview is False
        assert "preview_output" not in first.production.allowed_actions
    else:
        assert first.production.outputs == ()

    second_payload = artifact_payload if terminal_first else terminal_payload
    second_payload["expected_state_fingerprint"] = first.sequence.state.fingerprint
    second_action = "record_artifact" if terminal_first else "record_terminal"
    completed = _required_response(
        coordinator.dispatch(_action("coordinator.second.1", second_action, second_payload))
    )
    assert completed.disposition == "succeeded"
    receipt = coordinator._runs[prepared.run_handle].artifact_receipt
    assert receipt is not None
    assert completed.artifact_authority is not None
    assert completed.artifact_authority.to_wire() == {
        "schema": "h3.context.generation_coordinator.artifact_authority.v1",
        "receipt_fingerprint": receipt.fingerprint,
        "byte_length": len(b"synthetic-video-payload"),
    }
    assert completed.terminal_fingerprint is not None
    assert completed.terminal_fingerprint.startswith("sha256:")
    assert completed.sequence.progress[0].state.value == "succeeded"
    assert (completed.production.run_completed, completed.production.run_total) == (1, 1)
    assert completed.production.run_state == "succeeded"
    assert len(completed.production.outputs) == 1
    preview_output = completed.production.outputs[0]
    assert preview_output.segment_id == completed.production.segments[0].segment_id
    assert preview_output.preview is True
    assert "preview_output" in completed.production.allowed_actions
    source = production_registry.admit_media_preview_source(
        workspace_handle=completed.production.workspace_handle,
        expected_workspace_revision=completed.production.workspace_revision,
        expected_workspace_fingerprint=completed.production.workspace_fingerprint,
        output_handle=preview_output.output_handle,
    )
    assert source.render(deadline=time.monotonic() + 1.0) == bytearray(b"synthetic-video-payload")
    assert artifact.exists(), "private admission must never delete the host-owned original"
    public_wire = json.dumps(completed.to_wire(), sort_keys=True)
    assert artifact.name not in public_wire
    assert str(output_root) not in public_wire


@pytest.mark.parametrize(
    "failure",
    ("route_unavailable", "non_mp4", "body", "duration", "frames", "pixels"),
)
def test_generated_preview_ineligibility_suppresses_only_preview_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    observed = ObservedVideoArtifact("mp4", (192, 512, 512, 3))
    route_available = failure != "route_unavailable"
    if failure == "non_mp4":
        observed = ObservedVideoArtifact("webm", (192, 512, 512, 3))
    limit_by_failure = {
        "body": "MAX_MEDIA_PREVIEW_RESPONSE_BYTES",
        "duration": "MAX_MEDIA_PREVIEW_DURATION_MS",
        "frames": "MAX_GENERATED_MEDIA_PREVIEW_FRAMES",
        "pixels": "MAX_GENERATED_MEDIA_PREVIEW_FRAME_PIXELS",
    }
    if failure in limit_by_failure:
        monkeypatch.setattr(preview_authority_module, limit_by_failure[failure], 1)

    production_registry, _coordinator_registry, _submitted, completed, _receipt, _payload = (
        _complete_managed_run(
            tmp_path,
            monkeypatch,
            route_available=route_available,
            observed=observed,
        )
    )

    assert completed.disposition == "succeeded"
    assert completed.production.run_state == "succeeded"
    assert len(completed.production.outputs) == 1
    assert completed.production.outputs[0].preview is False
    assert "preview_output" not in completed.production.allowed_actions
    with pytest.raises(ProductionWorkbenchError) as unavailable:
        production_registry.admit_media_preview_source(
            workspace_handle=completed.production.workspace_handle,
            expected_workspace_revision=completed.production.workspace_revision,
            expected_workspace_fingerprint=completed.production.workspace_fingerprint,
            output_handle=completed.production.outputs[0].output_handle,
        )
    assert (unavailable.value.code, unavailable.value.status) == ("preview_unavailable", 422)


def test_generated_preview_source_rechecks_state_identity_deadline_cancellation_and_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_registry, coordinator, submitted, completed, receipt, payload = (
        _complete_managed_run(tmp_path, monkeypatch)
    )
    output = completed.production.outputs[0]
    source = production_registry.admit_media_preview_source(
        workspace_handle=completed.production.workspace_handle,
        expected_workspace_revision=completed.production.workspace_revision,
        expected_workspace_fingerprint=completed.production.workspace_fingerprint,
        output_handle=output.output_handle,
    )
    store = coordinator._store()
    builder = preview_authority_module.build_generated_segment_media_preview_source_authority
    future_deadline = time.monotonic() + 1.0

    with pytest.raises(ValueError, match="media_preview_source_ineligible"):
        builder(
            generation_sequence=submitted.sequence,
            receipt=receipt,
            store=store,
            opaque_output_handle=output.output_handle,
            deadline=future_deadline,
            clock=time.monotonic,
        )
    foreign = replace(
        receipt,
        model_fingerprint=_fp("foreign.preview.model"),
        receipt_fingerprint=None,
    )
    with pytest.raises(ValueError, match="media_preview_source_ineligible"):
        builder(
            generation_sequence=completed.sequence,
            receipt=foreign,
            store=store,
            opaque_output_handle=output.output_handle,
            deadline=future_deadline,
            clock=time.monotonic,
        )
    with pytest.raises(MediaPreviewSourceError) as deadline_error:
        builder(
            generation_sequence=completed.sequence,
            receipt=receipt,
            store=store,
            opaque_output_handle=output.output_handle,
            deadline=time.monotonic() - 1.0,
            clock=time.monotonic,
        )
    assert deadline_error.value.code == "preview_deadline"
    with pytest.raises(MediaPreviewSourceError) as cancellation_error:
        builder(
            generation_sequence=completed.sequence,
            receipt=receipt,
            store=store,
            opaque_output_handle=output.output_handle,
            deadline=future_deadline,
            cancellation=_CancelledPreview(),
            clock=time.monotonic,
        )
    assert cancellation_error.value.code == "preview_source_unavailable"

    with pytest.raises(MediaPreviewSourceError) as render_cancelled:
        source.render(deadline=future_deadline, cancellation=_CancelledPreview())
    assert render_cancelled.value.code == "preview_source_unavailable"
    with pytest.raises(MediaPreviewSourceError) as render_deadline:
        source.render(deadline=time.monotonic() - 1.0)
    assert render_deadline.value.code == "preview_deadline"

    artifact_path = store._artifact_path(receipt.artifact_id)
    artifact_path.write_bytes(b"tampered")
    with pytest.raises(MediaPreviewSourceError) as tampered:
        source.render(deadline=time.monotonic() + 1.0)
    assert tampered.value.code == "preview_source_unavailable"
    artifact_path.write_bytes(payload)
    assert source.render(deadline=time.monotonic() + 1.0) == bytearray(payload)
    store._clock_ms = lambda: receipt.expires_at_ms
    with pytest.raises(MediaPreviewSourceError) as expired:
        source.render(deadline=time.monotonic() + 1.0)
    assert expired.value.code == "preview_source_unavailable"


def test_generated_preview_survives_selection_cas_and_release_revokes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_registry, _coordinator, _submitted, completed, _receipt, _payload = (
        _complete_managed_run(tmp_path, monkeypatch)
    )
    output = completed.production.outputs[0]
    original_source = production_registry.admit_media_preview_source(
        workspace_handle=completed.production.workspace_handle,
        expected_workspace_revision=completed.production.workspace_revision,
        expected_workspace_fingerprint=completed.production.workspace_fingerprint,
        output_handle=output.output_handle,
    )
    selected = production_registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.preview.selection",
            "action": "set_selection",
            "payload": {
                "workspace_handle": completed.production.workspace_handle,
                "expected_workspace_revision": completed.production.workspace_revision,
                "expected_workspace_fingerprint": completed.production.workspace_fingerprint,
                "segment_ids": [],
            },
        }
    ).projection
    assert isinstance(selected, ProductionWorkbenchProjection)
    assert selected.workspace_revision == completed.production.workspace_revision + 1
    assert selected.outputs == completed.production.outputs
    assert (
        production_registry.admit_media_preview_source(
            workspace_handle=selected.workspace_handle,
            expected_workspace_revision=selected.workspace_revision,
            expected_workspace_fingerprint=selected.workspace_fingerprint,
            output_handle=output.output_handle,
        )
        is original_source
    )

    released = production_registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.preview.release",
            "action": "release_workspace",
            "payload": {
                "workspace_handle": selected.workspace_handle,
                "expected_workspace_revision": selected.workspace_revision,
                "expected_workspace_fingerprint": selected.workspace_fingerprint,
            },
        }
    )
    assert released.status == 204
    with pytest.raises(ProductionWorkbenchError) as revoked:
        production_registry.admit_media_preview_source(
            workspace_handle=selected.workspace_handle,
            expected_workspace_revision=selected.workspace_revision,
            expected_workspace_fingerprint=selected.workspace_fingerprint,
            output_handle=output.output_handle,
        )
    assert revoked.value.status == 410


def test_prepare_is_replay_safe_and_rejects_a_conflicting_request_id(tmp_path: Path) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    action = _action(
        "coordinator.prepare.replay",
        "prepare_sequence",
        {
            "workspace_handle": production.workspace_handle,
            "expected_workspace_revision": production.workspace_revision,
            "expected_workspace_fingerprint": production.workspace_fingerprint,
            "correlation": {
                "prompt_id": "prompt.bootstrap",
                "execution_node_id": "node.product.shell",
            },
            "observation": _observation(),
        },
    )

    first = _required_response(coordinator.dispatch(action))
    replay = _required_response(coordinator.dispatch(action))
    assert replay.to_wire() == first.to_wire()
    payload = action["payload"]
    assert type(payload) is dict
    action["payload"] = {**payload, "observation": {**_observation(), "route": "new"}}
    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(action)
    assert (error.value.code, error.value.status) == ("request_id_conflict", 409)


def test_observation_rejects_non_string_identifiers_without_coercion() -> None:
    observation = {**_observation(), "native_anchor_node_id": 17}

    with pytest.raises(SequenceCoordinatorError) as error:
        PreparedGraphObservation.from_wire(observation)

    assert (error.value.code, error.value.status) == ("invalid_native_anchor_node_id", 400)


def test_observation_excludes_unobserved_container_and_geometry_predictions() -> None:
    observation = PreparedGraphObservation.from_wire(_observation())

    assert observation.expected_frames == 192
    assert "expected_format" not in observation.to_wire()
    assert "expected_shape" not in observation.to_wire()


def test_prepare_claims_the_exact_source_identity_receipt_before_sequence_authority(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    source = input_root / "source.png"
    header = struct.pack(">IIBBBBB", 120, 160, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    source.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )
    geometry = InputGeometryRegistry(
        input_root_factory=lambda: input_root,
        clock=lambda: 100.0,
        token_factory=lambda: "g" * 40,
        fingerprint_secret=b"s" * 32,
    )
    receipt = geometry.observe(
        {
            "schema": INPUT_GEOMETRY_REQUEST_SCHEMA,
            "locator": "source.png",
        }
    )
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=lambda payload, **expected: ObservedVideoArtifact(
            "mp4", (192, 800, 1056, 3)
        ),
        geometry_receipt_claimant=geometry.claim,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    observation = {
        **_observation(),
        "source_identity": receipt.to_wire(),
    }

    prepared = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.prepare.source.geometry",
                "prepare_sequence",
                {
                    "workspace_handle": production.workspace_handle,
                    "expected_workspace_revision": production.workspace_revision,
                    "expected_workspace_fingerprint": production.workspace_fingerprint,
                    "correlation": {
                        "prompt_id": "prompt.bootstrap",
                        "execution_node_id": "node.product.shell",
                    },
                    "observation": observation,
                },
            )
        )
    )

    assert prepared.sequence.eligible_commands[0].job.expected_shape == (192,)
    with pytest.raises(GeometryPreflightError, match="^geometry_receipt_stale$"):
        geometry.claim(receipt.to_wire())


def test_ledger_capacity_rejection_is_atomic_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_adapter, "MAX_COORDINATOR_LEDGER", 2)
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    coordinator.dispatch(
        _action(
            "coordinator.read.capacity",
            "read_sequence",
            {"run_handle": prepared.run_handle},
        )
    )

    with pytest.raises(SequenceCoordinatorError) as error:
        _submit(coordinator, prepared)

    assert (error.value.code, error.value.status) == ("coordinator_ledger_capacity", 429)
    # A rejected request must not publish a state that has no replay receipt.
    assert coordinator._runs[prepared.run_handle].state.runtimes[0].state.value == "planned"


def test_default_ledger_budget_covers_every_active_run_lifecycle() -> None:
    # The closed action vocabulary has eight receipts; capacity must not make an admitted run
    # unusable merely because it exercises running/read/release as well as the success path.
    assert MAX_COORDINATOR_LEDGER >= MAX_COORDINATOR_RUNS * 8
    assert MAX_COORDINATOR_LEDGER * MAX_COORDINATOR_RESPONSE_BYTES <= (
        MAX_COORDINATOR_RETAINED_BYTES
    )


def test_prepare_uses_exact_production_cas_and_creates_no_run_on_conflict(tmp_path: Path) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    payload = {
        "workspace_handle": production.workspace_handle,
        "expected_workspace_revision": production.workspace_revision + 1,
        "expected_workspace_fingerprint": production.workspace_fingerprint,
        "correlation": {
            "prompt_id": "prompt.bootstrap",
            "execution_node_id": "node.product.shell",
        },
        "observation": _observation(),
    }

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(_action("coordinator.prepare.stale", "prepare_sequence", payload))

    assert (error.value.code, error.value.status) == ("stale_workspace", 409)
    assert coordinator._runs == {}


@pytest.mark.parametrize("awaiting_artifact", (False, True))
def test_expired_run_becomes_a_bounded_tombstone(tmp_path: Path, awaiting_artifact: bool) -> None:
    now = [100.0]
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=lambda payload, **expected: ObservedVideoArtifact(
            "mp4", (192, 512, 512, 3)
        ),
        clock=lambda: now[0],
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
        ttl_seconds=1,
    )
    prepared = _prepare(coordinator, production)
    if awaiting_artifact:
        submitted = _submit(coordinator, prepared)
        now[0] = 100.5
        waiting = _required_response(
            coordinator.dispatch(
                _action(
                    "managed.expiry.success",
                    "close_managed_run",
                    {
                        "run_handle": prepared.run_handle,
                        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                        "queue_prompt_id": "prompt.model.1",
                        "kind": "success",
                        "artifact": None,
                    },
                )
            )
        )
        assert waiting.disposition == "verification_pending"
        now[0] = 100.9
        assert (
            _required_response(
                coordinator.dispatch(
                    _action(
                        "managed.expiry.read.pending",
                        "read_managed_run",
                        {"run_handle": prepared.run_handle},
                    )
                )
            ).disposition
            == "verification_pending"
        )
    # A pending-terminal read must not extend the coordinator's existing idle deadline.
    now[0] = 101.6 if awaiting_artifact else 102.0

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(
            _action(
                "coordinator.read.expired",
                "read_managed_run" if awaiting_artifact else "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )

    assert (error.value.code, error.value.status) == ("run_gone", 410)


def test_foreign_prompt_is_rejected_but_first_compatible_video_node_is_admitted(
    tmp_path: Path,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"synthetic-video-payload")
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)

    with pytest.raises(SequenceCoordinatorError) as terminal_error:
        coordinator.dispatch(
            _action(
                "coordinator.terminal.foreign",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.foreign",
                    "kind": "success",
                },
            )
        )
    assert (terminal_error.value.code, terminal_error.value.status) == ("foreign_host_event", 409)

    admitted = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.artifact.compatible-node",
                "record_artifact",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "output_node_id": "node.save.foreign",
                    "locator": {
                        "filename": artifact.name,
                        "subfolder": "",
                        "type": "output",
                    },
                },
            )
        )
    )
    assert admitted.disposition == "artifact_verified"
    assert coordinator._runs[prepared.run_handle].state.runtimes[0].state.value == "running"
    assert artifact.exists()


def test_default_inspector_rejects_stream_shape_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = SimpleNamespace(
        type="video",
        width=16_384,
        height=512,
        average_rate=24,
    )

    class FakeContainer:
        streams = (stream,)
        format = SimpleNamespace(name="mp4")
        decode_calls = 0
        closed = False

        def decode(self, _stream: object) -> Iterator[object]:
            self.decode_calls += 1
            return iter(())

        def close(self) -> None:
            self.closed = True

    container = FakeContainer()
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: SimpleNamespace(open=lambda *_args, **_kwargs: container),
    )

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator_adapter._default_artifact_inspector(
            b"synthetic-container",
            expected_frames=1,
            deadline=time.monotonic() + 1.0,
            should_cancel=lambda: False,
        )

    assert error.value.code == "artifact_shape_mismatch"
    assert container.decode_calls == 0
    assert container.closed is True


@pytest.mark.parametrize(
    ("document_type", "expected"),
    ((b"webm", "webm"), (b"matroska", "mkv")),
)
def test_observed_matroska_family_uses_bounded_ebml_document_type(
    document_type: bytes,
    expected: str,
) -> None:
    payload = b"\x1a\x45\xdf\xa3" + b"\x42\x82" + bytes([0x80 | len(document_type)]) + document_type

    assert coordinator_adapter._observed_format_label("matroska,webm", payload) == expected


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_category", "expected_retry"),
    (
        (
            "inspector",
            "artifact_shape_mismatch",
            "artifact_content_invalid",
            "inspect_native",
        ),
        (
            "store",
            "artifact_store_unavailable",
            "artifact_store_unavailable",
            "retry_output_verification",
        ),
    ),
)
def test_artifact_verification_failures_publish_the_same_explicit_failed_authority(
    tmp_path: Path,
    failure: str,
    expected_code: str,
    expected_category: str,
    expected_retry: str,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"synthetic-video-payload")
    private_root = tmp_path / "private"
    if failure == "store":
        private_root.write_bytes(b"not-a-directory")

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        if failure == "inspector":
            raise SequenceCoordinatorError("artifact_shape_mismatch", 422)
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: private_root,
        artifact_inspector=inspect,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(
            _action(
                f"coordinator.artifact.{failure}",
                "record_artifact",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "output_node_id": "node.save.video",
                    "locator": {
                        "filename": artifact.name,
                        "subfolder": "",
                        "type": "output",
                    },
                },
            )
        )

    assert error.value.code == expected_code
    assert error.value.category == expected_category
    assert error.value.retry_disposition == expected_retry
    assert error.value.same_run_authority is True
    assert coordinator._runs[prepared.run_handle].artifact_receipt is None
    current = _required_response(
        coordinator.dispatch(
            _action(
                f"coordinator.read.after.{failure}",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    assert current.run_handle == prepared.run_handle
    assert current.production.workspace_handle == production.workspace_handle
    assert current.sequence.progress[0].state.value == "output_verification_failed"
    assert (current.production.run_completed, current.production.run_total) == (1, 1)
    assert current.production.run_state == "failed"
    assert artifact.exists()
    public_error = json.dumps(error.value.to_wire(), sort_keys=True)
    assert artifact.name not in public_error
    assert str(output_root) not in public_error
    assert str(private_root) not in public_error


def test_exact_output_verification_retry_closes_without_a_new_generation_attempt(
    tmp_path: Path,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    payload = b"synthetic-video-payload"
    artifact.write_bytes(payload)
    inspection_calls = 0

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        nonlocal inspection_calls
        inspection_calls += 1
        if inspection_calls == 1:
            raise SequenceCoordinatorError("artifact_inspector_unavailable", 422)
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    private_root = tmp_path / "private"
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: private_root,
        artifact_inspector=inspect,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    terminal = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.terminal.before.retry",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "success",
                },
            )
        )
    )
    artifact_payload: dict[str, object] = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": terminal.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
        "locator": {
            "filename": artifact.name,
            "subfolder": "",
            "type": "output",
        },
    }

    with pytest.raises(SequenceCoordinatorError) as first_error:
        coordinator.dispatch(
            _action("coordinator.artifact.retryable", "record_artifact", artifact_payload)
        )
    assert first_error.value.retry_disposition == "retry_output_verification"
    failed = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.read.retryable",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    artifact_payload["expected_state_fingerprint"] = failed.sequence.state.fingerprint
    completed = _required_response(
        coordinator.dispatch(
            _action("coordinator.artifact.retry.exact", "record_artifact", artifact_payload)
        )
    )

    assert completed.disposition == "succeeded"
    assert completed.run_handle == prepared.run_handle
    assert completed.production.workspace_handle == production.workspace_handle
    assert (completed.production.run_completed, completed.production.run_total) == (1, 1)
    assert completed.production.run_state == "succeeded"
    assert completed.sequence.progress[0].attempt == 1
    assert completed.sequence.progress[0].queue_prompt_id == "prompt.model.1"
    assert inspection_calls == 2
    assert artifact.read_bytes() == payload
    assert len(tuple((private_root / "receipts").glob("*.json"))) == 1

    artifact_payload["expected_state_fingerprint"] = completed.sequence.state.fingerprint
    replay = _required_response(
        coordinator.dispatch(
            _action("coordinator.artifact.retry.replay", "record_artifact", artifact_payload)
        )
    )
    assert replay.disposition == "succeeded"
    assert replay.sequence.state.fingerprint == completed.sequence.state.fingerprint
    assert inspection_calls == 2
    assert len(tuple((private_root / "receipts").glob("*.json"))) == 1


def test_production_publish_failure_after_store_commit_retries_the_committed_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    payload = b"synthetic-video-payload"
    artifact.write_bytes(payload)
    private_root = tmp_path / "private"
    inspection_calls = 0

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        nonlocal inspection_calls
        inspection_calls += 1
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: private_root,
        artifact_inspector=inspect,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    terminal = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.terminal.before.publish.retry",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "success",
                },
            )
        )
    )
    artifact_payload: dict[str, object] = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": terminal.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
        "locator": {"filename": artifact.name, "subfolder": "", "type": "output"},
    }
    original_replace = production_registry.replace_generation_authority
    artifact_publish_calls = 0

    def fail_first_artifact_publish(
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_sequence_state_fingerprint: str | None,
        generation_sequence: GenerationSequenceProjection,
        artifact_receipts: tuple[SegmentArtifactReceipt, ...] = (),
        artifact_store: PrivateSegmentArtifactStore | None = None,
        _prepared_preview_sources: Mapping[
            str, preview_authority_module.MediaPreviewSourceAuthority
        ]
        | None = None,
    ) -> ProductionWorkbenchProjection:
        nonlocal artifact_publish_calls
        if artifact_receipts:
            artifact_publish_calls += 1
            if artifact_publish_calls == 1:
                raise ProductionWorkbenchError("workspace_busy", 423)
        return original_replace(
            handle,
            expected_workspace_revision=expected_workspace_revision,
            expected_workspace_fingerprint=expected_workspace_fingerprint,
            expected_sequence_state_fingerprint=expected_sequence_state_fingerprint,
            generation_sequence=generation_sequence,
            artifact_receipts=artifact_receipts,
            artifact_store=artifact_store,
            _prepared_preview_sources=_prepared_preview_sources,
        )

    monkeypatch.setattr(
        production_registry,
        "replace_generation_authority",
        fail_first_artifact_publish,
    )

    with pytest.raises(SequenceCoordinatorError) as first_error:
        coordinator.dispatch(
            _action(
                "coordinator.artifact.production.publish.first",
                "record_artifact",
                artifact_payload,
            )
        )

    assert first_error.value.code == "workspace_busy"
    assert first_error.value.category == "run_authority_mismatch"
    assert first_error.value.retry_disposition == "retry_output_verification"
    assert first_error.value.same_run_authority is True
    retained = coordinator._runs[prepared.run_handle]
    assert retained.state.fingerprint == terminal.sequence.state.fingerprint
    assert retained.artifact_receipt is None
    assert retained.artifact_candidate is not None
    assert retained.artifact_candidate.completed_receipt is not None
    assert inspection_calls == 1
    assert len(tuple((private_root / "receipts").glob("*.json"))) == 1

    completed = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.artifact.production.publish.retry",
                "record_artifact",
                artifact_payload,
            )
        )
    )

    assert completed.disposition == "succeeded"
    assert completed.production.run_state == "succeeded"
    assert artifact_publish_calls == 2
    assert inspection_calls == 1
    assert len(tuple((private_root / "receipts").glob("*.json"))) == 1
    assert artifact.read_bytes() == payload


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        ("stale_workspace", 409),
        ("stale_generation_sequence", 409),
        ("generation_authority_closed", 422),
    ],
)
def test_non_transient_production_publish_failure_never_offers_same_run_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    status: int,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"synthetic-video-payload")
    private_root = tmp_path / "private"
    inspection_calls = 0

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        nonlocal inspection_calls
        inspection_calls += 1
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: private_root,
        artifact_inspector=inspect,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    terminal = _required_response(
        coordinator.dispatch(
            _action(
                f"coordinator.terminal.before.{error_code}",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "success",
                },
            )
        )
    )
    artifact_payload: dict[str, object] = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": terminal.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
        "locator": {"filename": artifact.name, "subfolder": "", "type": "output"},
    }
    original_replace = production_registry.replace_generation_authority
    artifact_publish_calls = 0

    def reject_artifact_publish(
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_sequence_state_fingerprint: str | None,
        generation_sequence: GenerationSequenceProjection,
        artifact_receipts: tuple[SegmentArtifactReceipt, ...] = (),
        artifact_store: PrivateSegmentArtifactStore | None = None,
        _prepared_preview_sources: Mapping[
            str, preview_authority_module.MediaPreviewSourceAuthority
        ]
        | None = None,
    ) -> ProductionWorkbenchProjection:
        nonlocal artifact_publish_calls
        if artifact_receipts:
            artifact_publish_calls += 1
            raise ProductionWorkbenchError(error_code, status)
        return original_replace(
            handle,
            expected_workspace_revision=expected_workspace_revision,
            expected_workspace_fingerprint=expected_workspace_fingerprint,
            expected_sequence_state_fingerprint=expected_sequence_state_fingerprint,
            generation_sequence=generation_sequence,
            artifact_receipts=artifact_receipts,
            artifact_store=artifact_store,
            _prepared_preview_sources=_prepared_preview_sources,
        )

    monkeypatch.setattr(
        production_registry,
        "replace_generation_authority",
        reject_artifact_publish,
    )

    with pytest.raises(SequenceCoordinatorError) as terminal_error:
        coordinator.dispatch(
            _action(
                f"coordinator.artifact.{error_code}.first",
                "record_artifact",
                artifact_payload,
            )
        )

    assert terminal_error.value.code == error_code
    assert terminal_error.value.category == "run_authority_mismatch"
    assert terminal_error.value.retry_disposition == "use_native"
    assert terminal_error.value.same_run_authority is False
    retained = coordinator._runs[prepared.run_handle]
    assert retained.artifact_candidate is not None
    assert retained.artifact_candidate.completed_receipt is not None
    assert inspection_calls == 1
    assert artifact_publish_calls == 1
    assert len(tuple((private_root / "receipts").glob("*.json"))) == 1

    with pytest.raises(SequenceCoordinatorError) as repeated:
        coordinator.dispatch(
            _action(
                f"coordinator.artifact.{error_code}.repeated",
                "record_artifact",
                artifact_payload,
            )
        )

    assert repeated.value.code == "artifact_publication_unavailable"
    assert repeated.value.retry_disposition == "use_native"
    assert repeated.value.same_run_authority is False
    assert inspection_calls == 1
    assert artifact_publish_calls == 1
    assert len(tuple((private_root / "receipts").glob("*.json"))) == 1


def test_completed_receipt_keeps_first_video_authority_when_later_locator_differs(
    tmp_path: Path,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    first_artifact = output_root / "managed.mp4"
    second_artifact = output_root / "different.mp4"
    first_artifact.write_bytes(b"first-output")
    second_artifact.write_bytes(b"second-output")
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    terminal = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.terminal.before.locator.replay",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "success",
                },
            )
        )
    )
    artifact_payload: dict[str, object] = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": terminal.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
        "locator": {
            "filename": first_artifact.name,
            "subfolder": "",
            "type": "output",
        },
    }
    completed = _required_response(
        coordinator.dispatch(
            _action("coordinator.artifact.locator.initial", "record_artifact", artifact_payload)
        )
    )
    artifact_payload["expected_state_fingerprint"] = completed.sequence.state.fingerprint
    artifact_payload["locator"] = {
        "filename": second_artifact.name,
        "subfolder": "",
        "type": "output",
    }

    replay = _required_response(
        coordinator.dispatch(
            _action("coordinator.artifact.locator.changed", "record_artifact", artifact_payload)
        )
    )

    assert replay.disposition == "succeeded"
    assert replay.sequence.state.fingerprint == completed.sequence.state.fingerprint
    assert coordinator._runs[prepared.run_handle].artifact_receipt is not None
    assert first_artifact.read_bytes() == b"first-output"
    assert second_artifact.read_bytes() == b"second-output"


def test_output_verification_retry_refuses_a_changed_artifact_before_inspection(
    tmp_path: Path,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"first-output")
    inspection_calls = 0

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        nonlocal inspection_calls
        inspection_calls += 1
        raise SequenceCoordinatorError("artifact_inspector_unavailable", 422)

    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=inspect,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    artifact_payload: dict[str, object] = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
        "locator": {"filename": artifact.name, "subfolder": "", "type": "output"},
    }
    with pytest.raises(SequenceCoordinatorError):
        coordinator.dispatch(
            _action("coordinator.artifact.changed.initial", "record_artifact", artifact_payload)
        )
    failed = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.read.changed",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    artifact.write_bytes(b"different-output")
    artifact_payload["expected_state_fingerprint"] = failed.sequence.state.fingerprint

    with pytest.raises(SequenceCoordinatorError) as changed:
        coordinator.dispatch(
            _action("coordinator.artifact.changed.retry", "record_artifact", artifact_payload)
        )

    assert changed.value.code == "artifact_output_changed"
    assert changed.value.category == "artifact_locator_rejected"
    assert changed.value.retry_disposition == "inspect_native"
    assert changed.value.same_run_authority is True
    assert inspection_calls == 1
    assert artifact.read_bytes() == b"different-output"
    current = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.read.after.changed",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    assert current.sequence.progress[0].state.value == "output_verification_failed"
    assert current.production.run_state == "failed"


def test_manifest_transaction_authority_mismatch_is_not_collapsed_to_store_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"synthetic-video-payload")
    inspection_calls = 0

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        nonlocal inspection_calls
        inspection_calls += 1
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=inspect,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    run = coordinator._runs[prepared.run_handle]
    manifest = derive_segment_manifests(run.workspace)[0]
    mismatched_manifest = replace(
        manifest,
        workspace_fingerprint=_fp("foreign.workspace"),
        manifest_fingerprint=None,
    )
    monkeypatch.setattr(
        coordinator_adapter,
        "derive_segment_manifests",
        lambda _workspace: (mismatched_manifest,),
    )

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(
            _action(
                "coordinator.artifact.manifest-mismatch",
                "record_artifact",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "output_node_id": "node.save.video",
                    "locator": {
                        "filename": artifact.name,
                        "subfolder": "",
                        "type": "output",
                    },
                },
            )
        )

    assert error.value.code == "transaction_manifest_authority_mismatch"
    assert error.value.category == "artifact_authority_mismatch"
    assert error.value.retry_disposition == "use_native"
    assert error.value.same_run_authority is True
    assert inspection_calls == 1
    current = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.read.after-manifest-mismatch",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    assert current.production.workspace_handle == production.workspace_handle
    assert (current.production.run_completed, current.production.run_total) == (1, 1)
    assert current.production.run_state == "failed"
    assert current.sequence.progress[0].state.value == "output_verification_failed"


def test_managed_prepare_owns_production_creation_and_exact_refusal_rollback(
    tmp_path: Path,
) -> None:
    production, context_workspace_handle = _production_context()
    coordinator = _coordinator(production, tmp_path / "output", tmp_path / "private")
    refused_observation = _observation()
    refused_observation["expected_frames"] = 193
    payload: dict[str, object] = {
        "context_workspace_handle": context_workspace_handle,
        "correlation": {
            "prompt_id": "prompt.bootstrap",
            "execution_node_id": "node.product.shell",
        },
        "observation": refused_observation,
    }

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(_action("managed.prepare.refused", "prepare_managed_run", payload))

    assert error.value.code == "observation_duration_mismatch"
    # IMPORTANT: a failed aggregate prepare must remove both the unpublished Production authority
    # and its replay row; otherwise a corrected retry leaks capacity or replays a gone workspace.
    assert production._entries == {}
    assert production._ledger == {}

    accepted_observation = _observation()
    prepared = _required_response(
        coordinator.dispatch(
            _action(
                "managed.prepare.accepted",
                "prepare_managed_run",
                {
                    "context_workspace_handle": context_workspace_handle,
                    "correlation": payload["correlation"],
                    "observation": accepted_observation,
                },
            )
        )
    )
    assert prepared.disposition == "prepared"
    assert len(production._entries) == 1
    assert len(production._ledger) == 1


def test_managed_prepare_closes_invalid_context_handle_errors(tmp_path: Path) -> None:
    production, _context_workspace_handle = _production_context()
    coordinator = _coordinator(production, tmp_path / "output", tmp_path / "private")

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(
            _action(
                "managed.prepare.invalid-context",
                "prepare_managed_run",
                {
                    "context_workspace_handle": "not-a-context-handle",
                    "correlation": {
                        "prompt_id": "prompt.bootstrap",
                        "execution_node_id": "node.product.shell",
                    },
                    "observation": _observation(),
                },
            )
        )

    assert (error.value.code, error.value.status) == (
        "invalid_context_workspace_handle",
        400,
    )


def test_managed_submit_and_success_close_are_single_aggregate_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "comfyui_h3_context.adapters.comfyui_media_preview.ensure_media_preview_route_registered",
        lambda: True,
    )
    production, context_workspace_handle = _production_context()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"synthetic-video-payload")
    coordinator = _coordinator(production, output_root, tmp_path / "private")
    prepared = _required_response(
        coordinator.dispatch(
            _action(
                "managed.prepare.complete",
                "prepare_managed_run",
                {
                    "context_workspace_handle": context_workspace_handle,
                    "correlation": {
                        "prompt_id": "prompt.bootstrap",
                        "execution_node_id": "node.product.shell",
                    },
                    "observation": _observation(),
                },
            )
        )
    )
    command = prepared.sequence.eligible_commands[0]
    submitted = _required_response(
        coordinator.dispatch(
            _action(
                "managed.submit.complete",
                "submit_managed_run",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": prepared.sequence.state.fingerprint,
                    "job_id": command.job_id,
                    "transaction_id": command.transaction_id,
                    "graph_fingerprint": command.job.graph_fingerprint,
                    "compiled_prompt_fingerprint": command.job.compiled_prompt_fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                },
            )
        )
    )

    closed = _required_response(
        coordinator.dispatch(
            _action(
                "managed.close.complete",
                "close_managed_run",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "success",
                    "artifact": {
                        "output_node_id": "node.save.video",
                        "locator": {
                            "filename": artifact.name,
                            "subfolder": "",
                            "type": "output",
                        },
                    },
                },
            )
        )
    )

    assert closed.disposition == "succeeded"
    assert closed.sequence.progress[0].state.value == "succeeded"
    assert (closed.production.run_completed, closed.production.run_total) == (1, 1)
    current = _required_response(
        coordinator.dispatch(
            _action(
                "managed.read.complete",
                "read_managed_run",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    assert current.disposition == "current"
    assert current.sequence.state.fingerprint == closed.sequence.state.fingerprint


@pytest.mark.parametrize(
    ("kind", "artifact", "expected_code"),
    (
        ("success", {}, "invalid_managed_artifact"),
        (
            "error",
            {
                "output_node_id": "node.save.video",
                "locator": {"filename": "unused.mp4", "subfolder": "", "type": "output"},
            },
            "invalid_managed_artifact",
        ),
    ),
)
def test_managed_close_rejects_malformed_success_artifact_and_non_success_artifact(
    tmp_path: Path,
    kind: str,
    artifact: object,
    expected_code: str,
) -> None:
    production, context_workspace_handle = _production_context()
    coordinator = _coordinator(production, tmp_path / "output", tmp_path / "private")
    prepared = _required_response(
        coordinator.dispatch(
            _action(
                f"managed.prepare.invalid-close.{kind}",
                "prepare_managed_run",
                {
                    "context_workspace_handle": context_workspace_handle,
                    "correlation": {
                        "prompt_id": "prompt.bootstrap",
                        "execution_node_id": "node.product.shell",
                    },
                    "observation": _observation(),
                },
            )
        )
    )
    submitted = _submit(coordinator, prepared)

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(
            _action(
                f"managed.close.invalid.{kind}",
                "close_managed_run",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": kind,
                    "artifact": artifact,
                },
            )
        )

    assert (error.value.code, error.value.status) == (expected_code, 400)


def test_managed_success_without_artifact_is_recorded_and_late_output_closes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "comfyui_h3_context.adapters.comfyui_media_preview.ensure_media_preview_route_registered",
        lambda: True,
    )
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "late.mp4"
    artifact.write_bytes(b"synthetic-video-payload")
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    close_payload: dict[str, object] = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "kind": "success",
        "artifact": None,
    }
    first_request = _action("managed.no-output.close", "close_managed_run", close_payload)
    waiting = _required_response(coordinator.dispatch(first_request))
    assert waiting.disposition == "verification_pending"
    assert waiting.sequence.progress[0].state.value == "running"
    assert waiting.artifact_authority is None
    assert waiting.production.run_completed == 0
    assert coordinator._runs[prepared.run_handle].terminal_kind == "success"
    assert coordinator._managed.read(prepared.run_handle).state.value == "running"
    assert _required_response(coordinator.dispatch(first_request)).to_wire() == waiting.to_wire()

    current = _required_response(
        coordinator.dispatch(
            _action(
                "managed.no-output.read", "read_managed_run", {"run_handle": prepared.run_handle}
            )
        )
    )
    assert current.disposition == "verification_pending"
    assert current.sequence.state.fingerprint == waiting.sequence.state.fingerprint
    duplicate = _required_response(
        coordinator.dispatch(
            _action(
                "managed.no-output.duplicate",
                "close_managed_run",
                {**close_payload, "expected_state_fingerprint": current.sequence.state.fingerprint},
            )
        )
    )
    assert duplicate.to_wire() == current.to_wire()

    for name, override, code in (
        (
            "stale",
            {"expected_state_fingerprint": submitted.sequence.state.fingerprint},
            "stale_sequence_state",
        ),
        ("foreign", {"queue_prompt_id": "prompt.foreign"}, "foreign_host_event"),
        ("conflict", {"kind": "error"}, "conflicting_terminal"),
    ):
        with pytest.raises(SequenceCoordinatorError) as error:
            coordinator.dispatch(
                _action(
                    f"managed.no-output.{name}",
                    "close_managed_run",
                    {
                        **close_payload,
                        "expected_state_fingerprint": current.sequence.state.fingerprint,
                        **override,
                    },
                )
            )
        assert error.value.code == code
        assert (
            coordinator._runs[prepared.run_handle].state.fingerprint
            == current.sequence.state.fingerprint
        )

    completed = _required_response(
        coordinator.dispatch(
            _action(
                "managed.no-output.late",
                "close_managed_run",
                {
                    **close_payload,
                    "expected_state_fingerprint": current.sequence.state.fingerprint,
                    "artifact": {
                        "output_node_id": "node.save.video",
                        "locator": {"filename": artifact.name, "subfolder": "", "type": "output"},
                    },
                },
            )
        )
    )
    assert completed.disposition == "succeeded"
    assert completed.sequence.progress[0].state.value == "succeeded"
    assert completed.artifact_authority is not None
    assert completed.production.run_completed == 1
    assert coordinator._managed.read(prepared.run_handle).state.value == "terminal_succeeded"


def test_artifact_extension_is_not_authority_and_descriptor_race_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"synthetic-video-payload")
    extension_mismatch = output_root / "managed.webm"
    extension_mismatch.write_bytes(b"synthetic-video-payload")
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    base_payload = {
        "run_handle": prepared.run_handle,
        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
    }

    admitted = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.artifact.format",
                "record_artifact",
                {
                    **base_payload,
                    "locator": {
                        "filename": extension_mismatch.name,
                        "subfolder": "",
                        "type": "output",
                    },
                },
            )
        )
    )
    assert admitted.disposition == "artifact_verified"

    race_production_registry, race_production = _production_workspace()
    race_coordinator = _coordinator(
        race_production_registry,
        output_root,
        tmp_path / "race-private",
    )
    race_prepared = _prepare(race_coordinator, race_production)
    race_submitted = _submit(race_coordinator, race_prepared)
    race_payload = {
        "run_handle": race_prepared.run_handle,
        "expected_state_fingerprint": race_submitted.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
    }

    def swapped(*_args: object, **_kwargs: object) -> bytes:
        raise UnsafePathError("descriptor changed")

    monkeypatch.setattr(coordinator_adapter, "_read_regular_bytes", swapped)
    with pytest.raises(SequenceCoordinatorError) as race_error:
        race_coordinator.dispatch(
            _action(
                "coordinator.artifact.race",
                "record_artifact",
                {
                    **race_payload,
                    "locator": {
                        "filename": artifact.name,
                        "subfolder": "",
                        "type": "output",
                    },
                },
            )
        )
    assert race_error.value.code == "artifact_locator_unsafe"
    assert race_error.value.category == "artifact_locator_rejected"
    assert race_coordinator._runs[race_prepared.run_handle].artifact_receipt is None
    assert artifact.exists()


def test_stock_windows_save_video_relative_subfolder_closes_the_managed_run(
    tmp_path: Path,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    artifact_dir = output_root / "stock" / "save_video"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "managed.mp4"
    payload = b"synthetic-stock-save-video"
    artifact.write_bytes(payload)
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    terminal = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.terminal.windows-save-video",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "success",
                },
            )
        )
    )

    completed = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.artifact.windows-save-video",
                "record_artifact",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": terminal.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "output_node_id": "node.save.video",
                    "locator": {
                        "filename": artifact.name,
                        "subfolder": r"stock\save_video",
                        "type": "output",
                    },
                },
            )
        )
    )

    assert completed.disposition == "succeeded"
    assert (completed.production.run_completed, completed.production.run_total) == (1, 1)
    assert completed.production.run_state == "succeeded"
    assert artifact.read_bytes() == payload


@pytest.mark.parametrize(
    ("filename", "subfolder"),
    (
        ("C:escape.mp4", ""),
        ("\\\\server\\share.mp4", ""),
        ("../escape.mp4", ""),
        ("escape.mp4", "../outside"),
        ("stream:secret.mp4", "video"),
        ("escape.mp4", r"\server\share"),
        ("escape.mp4", r"video\..\outside"),
        ("escape.mp4", r"video\\outside"),
        ("escape.mp4", r"video\.\outside"),
        ("escape.mp4", r"C:\outside"),
    ),
)
def test_artifact_locator_refuses_escape_forms_without_creating_success(
    tmp_path: Path,
    filename: str,
    subfolder: str,
) -> None:
    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(production_registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)

    with pytest.raises(SequenceCoordinatorError) as error:
        coordinator.dispatch(
            _action(
                "coordinator.artifact.unsafe",
                "record_artifact",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "output_node_id": "node.save.video",
                    "locator": {
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": "output",
                    },
                },
            )
        )
    assert error.value.code == "artifact_locator_unsafe"
    assert error.value.category == "artifact_locator_rejected"
    assert error.value.retry_disposition == "inspect_native"
    assert error.value.same_run_authority is True
    current = _required_response(
        coordinator.dispatch(
            _action(
                "coordinator.read.after.unsafe",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    assert (current.production.run_completed, current.production.run_total) == (1, 1)
    assert current.production.run_state == "failed"
    assert current.sequence.progress[0].state.value == "output_verification_failed"
