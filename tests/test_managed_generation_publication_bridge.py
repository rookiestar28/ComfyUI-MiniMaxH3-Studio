"""Real managed-child binding must retain the original automatic Production owner."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from test_m23_15_sequence_coordinator import (
    _action,
    _fp,
    _managed_start_resource_request,
    _observation,
    _required_response,
)
from test_m26_03_context_materializer import _proposal, _source
from test_m26_04_managed_sequence_service import _qualification
from test_production_import import _authorities, _Materializer, _production, _request

from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    RELEASE_REQUEST_V2_SCHEMA,
    ObservedVideoArtifact,
    SequenceCoordinatorRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.managed_sequence_service import (
    AuthorizeManagedSequenceRequestV1,
    BindManagedSequenceChildRequestV1,
    ManagedChildBindingRequestV1,
    ManagedChildCorrelationV1,
    ManagedPreparedGraphObservationV1,
    ManagedSequenceMutationAuthorityV1,
    ManagedSequenceServiceError,
    PrepareManagedSequenceChildRequestV1,
    RecordManagedSequenceArtifactRequestV1,
    RecordManagedSequenceSubmissionRequestV1,
    RecordManagedSequenceTerminalRequestV1,
    StartManagedSequenceRequestV1,
    build_managed_sequence_service,
)
from comfyui_h3_context.adapters.production_context_materializer import (
    CanonicalProductionMaterializer,
)
from comfyui_h3_context.adapters.segment_artifact_store import ArtifactStoreError
from comfyui_h3_context.core import ExecutionCorrelation
from comfyui_h3_context.core.m26_assembly import (
    build_production_assembly_capability,
    mint_m26_assembly_authorization,
)
from comfyui_h3_context.core.managed_sequence import EligibleSegmentExecutionV1
from comfyui_h3_context.core.production_storyboard import ManagedExecutionQualificationV1
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.segment_workspace import derive_segment_manifests


def test_real_child_binding_retains_original_automatic_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _admission, proposal = _authorities(
        parts=(15, 15), qualification=ManagedExecutionQualificationV1.QUALIFIED
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
    manifests = derive_segment_manifests(authority.workspace)
    child_context = _Materializer(context)(context, proposal, proposal.segments[0])
    try:
        published = sidebar.publish(
            child_context.report,
            child_context.wiring,
            ExecutionCorrelation("prompt.child.context", "node.product.shell"),
        )
    finally:
        child_context.release()
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production,
        output_root_factory=lambda: tmp_path / "output",
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=lambda _payload, **_expected: ObservedVideoArtifact(
            "mp4", (manifests[0].duration.frame_count, 512, 512, 3)
        ),
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
    )
    resource = _managed_start_resource_request(2)
    coordinator.reserve_managed_sequence_resources(resource)
    execution = EligibleSegmentExecutionV1(
        parent_sequence_id=resource.parent_sequence_id,
        parent_authorization_fingerprint=resource.parent_authorization_fingerprint,
        segment_id=manifests[0].segment_id,
        slot_revision=2,
        attempt_epoch=1,
        materialization_receipt_fingerprint=authority.materialization_receipts[0].fingerprint,
        predecessor_terminal_fingerprint=None,
        request_id="prepare.original.segment.1",
    )
    observation = ManagedPreparedGraphObservationV1.from_wire(
        {**_observation(), "expected_frames": manifests[0].duration.frame_count}
    )
    owner_fields: dict[str, Any] = {
        "production_workspace_handle": created.workspace_handle,
        "source_authority": authority,
    }
    parameters = {item.name for item in fields(ManagedChildBindingRequestV1)}
    # Pre-fix compatibility supplies only proposed internal fields on the exact old type.
    # The real binder still executes; an API AttributeError is not the regression oracle.
    for name, value in owner_fields.items():
        if name not in parameters:

            def observed_owner(_self: object, value: Any = value) -> Any:
                return value

            monkeypatch.setattr(
                ManagedChildBindingRequestV1,
                name,
                property(observed_owner),
                raising=False,
            )
    request = ManagedChildBindingRequestV1(
        context_workspace_handle=published.workspace_id,
        execution=execution,
        graph_fingerprint=observation.graph_fingerprint,
        compiled_prompt_fingerprint=observation.compiled_prompt_fingerprint,
        correlation=ManagedChildCorrelationV1(
            pending_correlation_id="managed.pending.original.1",
            execution_node_id="node.product.shell",
        ),
        observation=observation,
        **{name: value for name, value in owner_fields.items() if name in parameters},
    )
    binding = coordinator.bind_managed_sequence_child(request)
    entry = coordinator._entry(binding.child_run_handle)

    assert entry.production_handle == created.workspace_handle
    assert entry.workspace is authority.workspace
    assert entry.state.plan.workspace_id == authority.workspace.workspace_id
    assert entry.state.plan.manifest_fingerprints == tuple(row.fingerprint for row in manifests)
    assert len(entry.state.plan.jobs) == 1
    assert entry.state.plan.jobs[0].segment_id == manifests[0].segment_id
    assert entry.production.workspace_handle == created.workspace_handle


class _PublicationBridge:
    """Real registries and stores; only host execution and codec inspection are external doubles."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
        source = _source(self.sidebar)
        context, proposal = _proposal(source, parts=(15, 15))
        self.production = ProductionWorkspaceRegistry(seed_claim=self.sidebar.claim_production_seed)
        created = self.production.dispatch(
            {
                "schema": "h3.context.production_workbench.action.v1",
                "request_id": "bridge.create",
                "action": "create_workspace_from_context",
                "payload": {"context_workspace_handle": source.workspace_id},
            }
        ).projection
        assert isinstance(created, ProductionWorkbenchProjection)
        self.context_releases: list[str] = []
        released = self.context_releases

        class TrackedMaterializer(CanonicalProductionMaterializer):
            def _release(self, workspace_id: str, **expected: Any) -> None:
                super()._release(workspace_id, **expected)
                released.append(workspace_id)

        materialize = TrackedMaterializer(source, workspace_registry=self.sidebar)

        imported = self.production.import_automatic_plan(
            _request(created, context, proposal),
            planning_context=context,
            proposal=proposal,
            materialize=materialize,
        ).projection
        self.handle = created.workspace_handle
        self.original = self.production.claim_automatic_plan_authority(
            self.handle,
            expected_workspace_revision=imported.workspace_revision,
            expected_workspace_fingerprint=imported.workspace_fingerprint,
            expected_plan_fingerprint=imported.plan_fingerprint,
        )
        self.manifests = derive_segment_manifests(self.original.workspace)
        self.output = tmp_path / "output"
        self.output.mkdir()
        self.now = [100.0]
        self.coordinator = SequenceCoordinatorRegistry(
            production_registry=self.production,
            output_root_factory=lambda: self.output,
            private_root_factory=lambda: tmp_path / "private",
            artifact_inspector=lambda _body, **_expected: ObservedVideoArtifact(
                "mp4", (362, 512, 512, 3)
            ),
            clock=lambda: self.now[0],
            clock_ms=lambda: round(self.now[0] * 1000),
        )
        monkeypatch.setattr(
            "comfyui_h3_context.adapters.comfyui_media_preview.ensure_media_preview_route_registered",
            lambda: False,
        )
        self.service = build_managed_sequence_service(
            production_registry=self.production,
            coordinator=self.coordinator,
        )
        self.binding_failures: list[str] = []
        real_bind = self.service._bind_child
        assert real_bind is not None

        def observed_bind(request: ManagedChildBindingRequestV1) -> Any:
            try:
                return real_bind(request)
            except Exception as error:
                self.binding_failures.append(getattr(error, "code", type(error).__name__))
                raise

        self.service._bind_child = observed_bind
        self.service._clock = lambda: self.now[0]
        self.service._epoch_clock_ms = lambda: round(self.now[0] * 1000)
        self.authorized = self.service.authorize(
            AuthorizeManagedSequenceRequestV1(
                request_id="bridge.authorize",
                workspace_handle=self.handle,
                expected_workspace_revision=imported.workspace_revision,
                expected_workspace_fingerprint=imported.workspace_fingerprint,
                expected_plan_fingerprint=imported.plan_fingerprint,
                generation_plan_fingerprint=_fp("bridge.generation"),
                compiler_fingerprint=_fp("bridge.compiler"),
                host_capability_fingerprint=self.original.materialization_receipts[
                    0
                ].capability_fingerprint,
                explicit_intent="generate_approved_sequence",
            )
        )
        assert self.service._entry is not None
        self.parent = self.service.start(
            StartManagedSequenceRequestV1(
                request_id="bridge.start",
                parent_sequence_id=self.authorized.parent_sequence_id,
                expected_revision=self.authorized.revision,
                authorization_fingerprint=self.authorized.authorization_fingerprint,
            ),
            qualification=_qualification(self.service._entry.sequence.authorization),
        )
        self.receipts: list[Any] = []
        self.states: list[Any] = []
        self.last_terminal: Any = None

    def mutation(self, request_id: str) -> ManagedSequenceMutationAuthorityV1:
        return ManagedSequenceMutationAuthorityV1(
            request_id=request_id,
            parent_sequence_id=self.parent.parent_sequence_id,
            expected_revision=self.parent.revision,
            authorization_fingerprint=self.parent.authorization_fingerprint,
        )

    def read(self) -> Any:
        result = self.production.dispatch(
            {
                "schema": "h3.context.production_workbench.action.v1",
                "request_id": f"bridge.read.{self.parent.revision}",
                "action": "read_projection",
                "payload": {"workspace_handle": self.handle},
            }
        )
        assert result.projection is not None
        return result.projection

    def complete_child(
        self, index: int, *, publish_terminal: bool = True, artifact_byte_delta: int = 0
    ) -> None:
        segment_id = self.manifests[index].segment_id
        previous = None if index == 0 else self.last_terminal.terminal_fingerprint
        prepared = self.service.prepare_child(
            PrepareManagedSequenceChildRequestV1(
                request_id=f"bridge.prepare.{index}",
                parent_sequence_id=self.parent.parent_sequence_id,
                expected_revision=self.parent.revision,
                authorization_fingerprint=self.parent.authorization_fingerprint,
                segment_id=segment_id,
                predecessor_terminal_fingerprint=previous,
            )
        )
        assert prepared.execution is not None
        observation = ManagedPreparedGraphObservationV1.from_wire(
            {
                **_observation(),
                "expected_frames": 362,
                "graph_fingerprint": _fp(f"bridge.graph.{index}"),
                "compiled_prompt_fingerprint": _fp(f"bridge.prompt.{index}"),
                "owned_projection_fingerprint": _fp(f"bridge.owned.{index}"),
            }
        )
        bound = self.service.bind_child(
            BindManagedSequenceChildRequestV1(
                request_id=f"bridge.bind.{index}",
                parent_sequence_id=prepared.parent_sequence_id,
                expected_revision=prepared.revision,
                authorization_fingerprint=prepared.authorization_fingerprint,
                segment_id=segment_id,
                eligible_execution_fingerprint=prepared.execution.fingerprint,
                materialization_receipt_fingerprint=self.original.materialization_receipts[
                    index
                ].fingerprint,
                graph_fingerprint=observation.graph_fingerprint,
                compiled_prompt_fingerprint=observation.compiled_prompt_fingerprint,
                owned_projection_fingerprint=observation.owned_projection_fingerprint,
                previous_owned_projection_fingerprint=_fp(f"bridge.owned.{index - 1}"),
                active_workflow_fingerprint=_fp("bridge.workflow"),
                correlation=ManagedChildCorrelationV1(
                    pending_correlation_id=f"managed.pending.bridge.{index}",
                    execution_node_id="node.shell",
                ),
                observation=observation,
            )
        )
        assert bound.child_authority is not None, self.binding_failures
        self.parent = bound
        child = _required_response(
            self.coordinator.dispatch(
                _action(
                    f"bridge.child.read.{index}",
                    "read_sequence",
                    {"run_handle": bound.child_authority.run_handle},
                )
            )
        )
        command = child.sequence.eligible_commands[0]
        prompt_id = f"prompt.bridge.{index}"
        submitted = _required_response(
            self.coordinator.dispatch(
                _action(
                    f"bridge.submit.{index}",
                    "record_submission",
                    {
                        "run_handle": child.run_handle,
                        "expected_state_fingerprint": child.sequence.state.fingerprint,
                        "job_id": command.job_id,
                        "transaction_id": command.transaction_id,
                        "graph_fingerprint": command.job.graph_fingerprint,
                        "compiled_prompt_fingerprint": command.job.compiled_prompt_fingerprint,
                        "queue_prompt_id": prompt_id,
                    },
                )
            )
        )
        self.parent = self.service.record_submission(
            RecordManagedSequenceSubmissionRequestV1(
                authority=self.mutation(f"bridge.parent.submit.{index}"),
                segment_id=segment_id,
                child_run_handle=child.run_handle,
                child_state_fingerprint=submitted.sequence.state.fingerprint,
                queue_prompt_id=prompt_id,
                timeout_ms=60000,
                active_workflow_fingerprint=_fp("bridge.workflow"),
                previous_owned_projection_fingerprint=_fp(f"bridge.owned.{index - 1}"),
                written_owned_projection_fingerprint=observation.owned_projection_fingerprint,
            )
        )
        media = self.output / f"child-{index}.mp4"
        payload = f"synthetic-encoded-child-{index}".encode()
        media.write_bytes(payload)
        captured = _required_response(
            self.coordinator.dispatch(
                _action(
                    f"bridge.capture.{index}",
                    "record_artifact",
                    {
                        "run_handle": child.run_handle,
                        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                        "queue_prompt_id": prompt_id,
                        "output_node_id": "node.save.video",
                        "locator": {"filename": media.name, "subfolder": "", "type": "output"},
                    },
                )
            )
        )
        completed = _required_response(
            self.coordinator.dispatch(
                _action(
                    f"bridge.terminal.{index}",
                    "record_terminal",
                    {
                        "run_handle": child.run_handle,
                        "expected_state_fingerprint": captured.sequence.state.fingerprint,
                        "queue_prompt_id": prompt_id,
                        "kind": "success",
                    },
                )
            )
        )
        assert completed.artifact_authority is not None
        assert completed.terminal_fingerprint is not None
        entry = self.coordinator._entry(child.run_handle)
        receipt = entry.artifact_receipt
        assert receipt is not None
        assert entry.workspace is self.original.workspace
        assert receipt.manifest_fingerprint == self.manifests[index].fingerprint
        self.receipts.append(receipt)
        self.states.append(entry.state)
        self.parent = self.service.record_artifact(
            RecordManagedSequenceArtifactRequestV1(
                authority=self.mutation(f"bridge.parent.artifact.{index}"),
                segment_id=segment_id,
                queue_prompt_id=prompt_id,
                artifact_receipt_fingerprint=receipt.fingerprint,
                artifact_bytes=len(payload) + artifact_byte_delta,
            )
        )
        self.coordinator.dispatch(
            _action(
                f"bridge.release.{index}",
                "release_sequence",
                {
                    "schema": RELEASE_REQUEST_V2_SCHEMA,
                    "run_handle": child.run_handle,
                    "intent": "cleanup_terminal",
                    "expected_state_fingerprint": completed.sequence.state.fingerprint,
                    "observed_terminal_fingerprint": completed.terminal_fingerprint,
                },
            )
        )
        assert child.run_handle not in self.coordinator._runs
        assert (
            self.production.claim_workspace_authority(
                self.handle,
                expected_workspace_revision=self.original.workspace.revision,
                expected_workspace_fingerprint=self.original.workspace.fingerprint,
            )
            is self.original.workspace
        )
        self.last_terminal = RecordManagedSequenceTerminalRequestV1(
            authority=self.mutation(f"bridge.parent.terminal.{index}"),
            segment_id=segment_id,
            queue_prompt_id=prompt_id,
            kind="success",
            terminal_fingerprint=completed.terminal_fingerprint,
        )
        if publish_terminal:
            self.parent = self.service.record_terminal(self.last_terminal)


def test_actual_capture_and_serial_release_publish_all_original_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    assert bridge.read().outputs == ()
    bridge.complete_child(1)
    projection = bridge.read()
    assert bridge.parent.state.value == "succeeded"
    assert projection.workspace_handle == bridge.handle
    assert projection.workspace_revision == bridge.original.workspace.revision
    assert projection.run_state == "succeeded"
    assert tuple(row.segment_id for row in projection.outputs) == tuple(
        manifest.segment_id for manifest in bridge.manifests
    )
    assert len(bridge.receipts) == 2
    accepted = bridge.production._entries[bridge.handle].accepted_authorities
    assert all(
        actual is expected
        for actual, expected in zip(accepted.artifact_receipts, bridge.receipts, strict=True)
    )
    assert accepted.generation_sequence is not None
    assert tuple(accepted.generation_sequence.state.plan.jobs) == tuple(
        state.plan.jobs[0] for state in bridge.states
    )
    assert tuple(accepted.generation_sequence.state.runtimes) == tuple(
        state.runtimes[0] for state in bridge.states
    )
    # Actual child completion discharges the historical planning-time hold without
    # replacing the immutable automatic plan or manufacturing a ready sequence.
    assert not bridge.original.startable
    authorization = mint_m26_assembly_authorization(
        authorization_id="assembly.actual.managed.children",
        automatic_plan=bridge.original,
        generation_sequence=accepted.generation_sequence,
        artifact_receipts=accepted.artifact_receipts,
        cut_boundary_receipts=bridge.original.cut_boundary_receipts,
        capability=build_production_assembly_capability(
            store_identity_fingerprint=_fp("bridge.store.capability")
        ),
        issued_at_ms=100_000,
        expires_at_ms=120_000,
    )
    assert authorization.artifact_receipt_fingerprints == tuple(
        item.fingerprint for item in bridge.receipts
    )
    assert not bridge.original.startable
    assert len(bridge.context_releases) == 4
    replay = bridge.service.record_terminal(bridge.last_terminal)
    assert replay.replayed
    assert bridge.read() == projection


@pytest.mark.parametrize("after_publication", (False, True))
def test_parent_publication_failure_retries_exact_completed_children_without_reexecution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_publication: bool,
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    bridge.complete_child(1, publish_terminal=False)
    before_parent = bridge.parent
    actual_publish = bridge.service._publish_completion
    assert actual_publish is not None

    def fail_once(*args: Any) -> None:
        if after_publication:
            actual_publish(*args)
        raise RuntimeError("controlled publication response loss")

    monkeypatch.setattr(bridge.service, "_publish_completion", fail_once)
    with pytest.raises(RuntimeError, match="controlled publication response loss"):
        bridge.service.record_terminal(bridge.last_terminal)
    current_parent = bridge.service.read_projection(
        bridge.authorized.parent_sequence_id,
        bridge.authorized.read_authority_fingerprint,
    ).projection
    assert current_parent is not None
    assert current_parent.revision == before_parent.revision
    assert current_parent.state.value != "succeeded"
    visible = bridge.read()
    assert len(visible.outputs) == (2 if after_publication else 0)
    already_published = bridge.production._entries[bridge.handle].accepted_authorities
    monkeypatch.setattr(bridge.service, "_publish_completion", actual_publish)
    bridge.parent = bridge.service.record_terminal(bridge.last_terminal)
    assert bridge.parent.state.value == "succeeded"
    final = bridge.production._entries[bridge.handle].accepted_authorities
    if after_publication:
        assert final is already_published
    assert all(
        actual is expected
        for actual, expected in zip(final.artifact_receipts, bridge.receipts, strict=True)
    )
    assert bridge.coordinator._runs == {}
    assert len(bridge.context_releases) == 4
    assert bridge.service.record_terminal(bridge.last_terminal).replayed


@pytest.mark.parametrize("byte_delta", (-1, 1))
def test_parent_cannot_publish_caller_misaccounted_artifact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    byte_delta: int,
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    bridge.complete_child(1, publish_terminal=False, artifact_byte_delta=byte_delta)
    with pytest.raises(ManagedSequenceServiceError, match="production_publication_accounting"):
        bridge.service.record_terminal(bridge.last_terminal)
    assert bridge.read().outputs == ()
    assert (
        bridge.service.cancel(bridge.mutation("bridge.cancel.accounting")).state.value
        == "cancelled"
    )
    assert bridge.production._entries[bridge.handle].workspace is bridge.original.workspace


def test_stale_original_workspace_blocks_final_publication_but_allows_parent_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    bridge.complete_child(1, publish_terminal=False)
    changed = bridge.production.dispatch(
        {
            "schema": "h3.context.production_workbench.action.v1",
            "request_id": "bridge.selection",
            "action": "set_selection",
            "payload": {
                "workspace_handle": bridge.handle,
                "expected_workspace_revision": bridge.original.workspace.revision,
                "expected_workspace_fingerprint": bridge.original.workspace.fingerprint,
                "segment_ids": [bridge.manifests[0].segment_id],
            },
        }
    ).projection
    assert isinstance(changed, ProductionWorkbenchProjection)
    with pytest.raises(Exception, match="stale_workspace|production_publication"):
        bridge.service.record_terminal(bridge.last_terminal)
    assert bridge.read().outputs == ()
    cancelled = bridge.service.cancel(bridge.mutation("bridge.cancel.stale"))
    assert cancelled.state.value == "cancelled"
    assert bridge.read().workspace_revision == changed.workspace_revision
    assert bridge.coordinator._runs == {}


@pytest.mark.parametrize("expire_first", (False, True))
def test_partial_parent_cancel_or_expiry_preserves_original_workspace_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expire_first: bool,
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    if expire_first:
        assert bridge.service._entry is not None
        assert bridge.service._entry.sequence.lease is not None
        deadline = bridge.service._entry.sequence.lease.absolute_deadline
        expired = bridge.service.expire_lease(
            bridge.parent.parent_sequence_id,
            bridge.parent.authorization_fingerprint,
            expected_revision=bridge.parent.revision,
            now=deadline + 1.0,
        )
        assert expired.state.value == "paused_client_absent"
        bridge.parent = expired
    bridge.parent = bridge.service.cancel(bridge.mutation("bridge.partial.cancel"))
    assert bridge.parent.state.value == "cancelled"
    assert bridge.read().outputs == ()
    assert bridge.production._entries[bridge.handle].workspace is bridge.original.workspace
    assert bridge.coordinator._runs == {}
    resources = bridge.coordinator._managed_start_resources[bridge.parent.parent_sequence_id]
    assert resources.ledger_released
    assert resources.child_slot_released
    assert resources.artifact_capacity_released


def test_resource_release_failure_after_publication_retries_without_recharging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    bridge.complete_child(1, publish_terminal=False)
    store = bridge.coordinator._store()
    actual_release = store.release_capacity_reservation
    resources = bridge.coordinator._managed_start_resources[bridge.parent.parent_sequence_id]
    before_rows = resources.consumed_rows
    assert bridge.service._entry is not None
    before_accounting = bridge.service._entry.sequence.artifact_reservation

    def fail_release(*_args: Any, **_kwargs: Any) -> None:
        raise ArtifactStoreError("controlled_capacity_release_failure")

    monkeypatch.setattr(store, "release_capacity_reservation", fail_release)
    with pytest.raises(ManagedSequenceServiceError, match="start_resource_release_failed"):
        bridge.service.record_terminal(bridge.last_terminal)
    published = bridge.production._entries[bridge.handle].accepted_authorities
    assert len(published.artifact_receipts) == 2
    assert resources.child_slot_released and resources.ledger_released
    assert not resources.artifact_capacity_released
    assert bridge.service._entry.sequence.artifact_reservation == before_accounting
    charged_rows = resources.consumed_rows
    assert charged_rows > before_rows
    monkeypatch.setattr(store, "release_capacity_reservation", actual_release)
    bridge.parent = bridge.service.record_terminal(bridge.last_terminal)
    assert bridge.parent.state.value == "succeeded"
    assert bridge.production._entries[bridge.handle].accepted_authorities is published
    assert resources.artifact_capacity_released
    assert resources.consumed_rows == charged_rows
    assert len(bridge.context_releases) == 4
    assert bridge.service.record_terminal(bridge.last_terminal).replayed


def test_changed_stored_original_blocks_final_publication_but_allows_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    bridge.complete_child(1, publish_terminal=False)
    stored = bridge.coordinator._store()._artifact_path(bridge.receipts[0].artifact_id)
    assert stored.resolve().is_relative_to(tmp_path.resolve())
    stored.write_bytes(b"changed-original")
    with pytest.raises(
        ManagedSequenceServiceError, match="production_publication_artifact_unavailable"
    ):
        bridge.service.record_terminal(bridge.last_terminal)
    assert bridge.read().outputs == ()
    assert (
        bridge.service.cancel(bridge.mutation("bridge.cancel.changed")).state.value == "cancelled"
    )
    assert bridge.production._entries[bridge.handle].workspace is bridge.original.workspace
    assert bridge.coordinator._runs == {}
