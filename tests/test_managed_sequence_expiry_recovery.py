"""Parent expiry recovery joined to real coordinator and resource ownership."""

from __future__ import annotations

from dataclasses import replace
from itertools import count
from pathlib import Path
from typing import Any

import pytest
import test_m23_15_sequence_coordinator as coordinator_fixtures
import test_m26_04_managed_sequence_service as service_fixtures

from comfyui_h3_context.adapters import managed_sequence_service as service_module
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import SequenceCoordinatorRegistry
from comfyui_h3_context.core.managed_sequence import (
    ManagedSequenceAuthorizationV1,
    ManagedSequenceStateV1,
    ManagedSequenceV1,
    SequenceSlotStateV1,
)


class _Scenario:
    """Only upstream plan/materialization are fixtures; child cleanup is production code."""

    def __init__(self, tmp_path: Path) -> None:
        self.now = 100.0
        self.production, context = coordinator_fixtures._production_context()
        probe = coordinator_fixtures._managed_child_request(
            self.production,
            context,
            coordinator_fixtures._managed_start_resource_request(1),
            suffix="expiry.fixture",
            slot_revision=2,
        )
        self.segment = probe.execution.segment_id
        tokens = count(1)
        self.coordinator = SequenceCoordinatorRegistry(
            production_registry=self.production,
            output_root_factory=lambda: tmp_path / "output",
            private_root_factory=lambda: tmp_path / "private",
            artifact_inspector=lambda *_args, **_kwargs: pytest.fail("unexpected artifact"),
            clock=lambda: self.now,
            clock_ms=lambda: round(self.now * 1000),
            token_factory=lambda: f"{next(tokens):040x}",
        )
        self.service, _ = service_fixtures._service(
            materialize=lambda _source, segment: (
                service_module.ManagedSegmentMaterializationClaimV1(
                    context,
                    segment,
                    service_fixtures._fp("materialization.1"),
                    lambda: None,
                )
            ),
            bind=self.coordinator.bind_managed_sequence_child,
            reserve=self.coordinator.reserve_managed_sequence_resources,
            clock=lambda: self.now,
            epoch_clock_ms=lambda: round(self.now * 1000),
        )
        parent_tokens = count(100)
        self.service._token_factory = lambda: f"{next(parent_tokens):040x}"

        def build(_source: object, **values: object) -> ManagedSequenceAuthorizationV1:
            authorization = service_fixtures._authorization(str(values["sequence_id"]), 1)
            return replace(
                authorization,
                segments=(
                    replace(
                        authorization.segments[0],
                        segment_id=self.segment,
                        frame_count=192,
                        duration_milliseconds=8000,
                    ),
                ),
            )

        self.service._authorization_builder = build
        self.service._lease_transition_owner.bind_child_transition(
            self.coordinator.transition_managed_sequence_lease
        )
        self.coordinator.bind_managed_sequence_lease_transition(
            self.service._lease_transition_owner.apply_pending
        )
        self.authorized = self.service.authorize(service_fixtures._authorize_request())
        self.start("start.original")
        prepared = self.service.prepare_child(
            service_module.PrepareManagedSequenceChildRequestV1(
                request_id="prepare.original",
                parent_sequence_id=self.authorized.parent_sequence_id,
                expected_revision=self.entry.sequence.revision,
                authorization_fingerprint=self.authorized.authorization_fingerprint,
                segment_id=self.segment,
                predecessor_terminal_fingerprint=None,
            )
        )
        assert prepared.execution is not None
        observation = service_module.ManagedPreparedGraphObservationV1.from_wire(
            coordinator_fixtures._observation()
        )
        self.service.bind_child(
            service_module.BindManagedSequenceChildRequestV1(
                request_id="bind.original",
                parent_sequence_id=self.authorized.parent_sequence_id,
                expected_revision=prepared.revision,
                authorization_fingerprint=self.authorized.authorization_fingerprint,
                segment_id=self.segment,
                eligible_execution_fingerprint=prepared.execution.fingerprint,
                materialization_receipt_fingerprint=service_fixtures._fp("materialization.1"),
                graph_fingerprint=observation.graph_fingerprint,
                compiled_prompt_fingerprint=observation.compiled_prompt_fingerprint,
                owned_projection_fingerprint=observation.owned_projection_fingerprint,
                previous_owned_projection_fingerprint=service_fixtures._fp("graph.before"),
                active_workflow_fingerprint=service_fixtures._fp("workflow.synthetic"),
                correlation=service_fixtures._child_correlation(),
                observation=observation,
            )
        )
        run = self.entry.sequence.slots[0].child_run_handle
        assert run is not None
        self.run = run
        self.child = self.coordinator._runs[self.run]
        self.resource = self.coordinator._managed_start_resources[
            self.authorized.parent_sequence_id
        ]

    @property
    def entry(self) -> service_module._ServiceEntry:
        assert self.service._entry is not None
        return self.service._entry

    def authority(self, request_id: str) -> service_module.ManagedSequenceMutationAuthorityV1:
        return service_module.ManagedSequenceMutationAuthorityV1(
            request_id=request_id,
            parent_sequence_id=self.authorized.parent_sequence_id,
            expected_revision=self.entry.sequence.revision,
            authorization_fingerprint=self.authorized.authorization_fingerprint,
        )

    def start(self, request_id: str) -> service_module.ManagedSequenceMutationResultV1:
        qualification = replace(
            service_fixtures._qualification(self.entry.sequence.authorization),
            observed_at=self.now,
            expires_at=self.now + 900,
        )
        return self.service.start(
            service_module.StartManagedSequenceRequestV1(
                request_id=request_id,
                parent_sequence_id=self.authorized.parent_sequence_id,
                expected_revision=self.entry.sequence.revision,
                authorization_fingerprint=self.authorized.authorization_fingerprint,
            ),
            qualification=qualification,
        )

    def advance(self) -> None:
        assert self.entry.sequence.lease is not None
        self.now = self.entry.sequence.lease.idle_deadline + 0.001

    def expire(self) -> None:
        self.service.expire_lease(
            self.authorized.parent_sequence_id,
            self.authorized.authorization_fingerprint,
            expected_revision=self.entry.sequence.revision,
            now=self.now,
        )

    def assert_removed(self) -> None:
        assert self.run not in self.coordinator._runs
        assert self.run not in self.coordinator._managed._runs
        assert self.child.production.workspace_handle not in self.production._entries
        assert self.resource.bound_child_run_handle is None

    def replace_cancelled_parent(self) -> None:
        retired = self.entry
        assert self.entry.sequence.slots[0].state is SequenceSlotStateV1.ELIGIBLE
        self.service.cancel(self.authority("cancel.original"))
        assert self.entry.sequence.state is ManagedSequenceStateV1.CANCELLED
        old_parent = self.authorized.parent_sequence_id
        self.authorized = self.service.authorize(
            service_fixtures._authorize_request("authorize.replacement")
        )
        assert retired.bound_child_binding is None
        assert not retired.bound_child_cleanup_completed
        assert self.authorized.parent_sequence_id != old_parent
        started = self.start("start.replacement")
        assert started.state is ManagedSequenceStateV1.ACTIVE
        assert self.authorized.parent_sequence_id in self.coordinator._managed_start_resources


def test_published_bound_expiry_cancel_reclaims_real_capacity(tmp_path: Path) -> None:
    scenario = _Scenario(tmp_path)
    scenario.advance()
    scenario.expire()
    scenario.assert_removed()
    scenario.replace_cancelled_parent()


def test_dirty_recovery_guard_preserves_real_child_until_saved(tmp_path: Path) -> None:
    scenario = _Scenario(tmp_path)
    dirty = [True]
    handle = scenario.child.production.workspace_handle
    scenario.production.configure_editor_recovery(
        changed=lambda *_args: None,
        protected=lambda candidate: candidate == handle and dirty[0],
    )
    before, binding = scenario.entry.sequence, scenario.entry.bound_child_binding
    scenario.advance()
    with pytest.raises(service_module.ManagedSequenceServiceError):
        scenario.expire()
    assert scenario.entry.sequence is before
    assert scenario.entry.bound_child_binding is binding
    assert scenario.run in scenario.coordinator._runs
    assert scenario.run in scenario.coordinator._managed._runs
    assert handle in scenario.production._entries
    assert scenario.resource.bound_child_run_handle == scenario.run
    dirty[0] = False
    scenario.expire()
    scenario.assert_removed()


def test_failed_real_cleanup_retains_authority_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _Scenario(tmp_path)
    original = scenario.production._release_managed_child_workspace
    attempts = 0

    def release(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic cleanup refusal")
        return original(*args, **kwargs)

    monkeypatch.setattr(scenario.production, "_release_managed_child_workspace", release)
    before = scenario.entry.sequence
    binding = scenario.entry.bound_child_binding
    scenario.advance()
    with pytest.raises(service_module.ManagedSequenceServiceError):
        scenario.expire()
    assert scenario.entry.sequence is before
    assert scenario.entry.bound_child_binding is binding
    assert scenario.run in scenario.coordinator._runs
    assert scenario.run in scenario.coordinator._managed._runs
    assert scenario.child.production.workspace_handle in scenario.production._entries
    assert scenario.resource.bound_child_run_handle == scenario.run
    assert not scenario.entry.bound_child_cleanup_completed
    with pytest.raises(
        service_module.ManagedSequenceServiceError, match="active_sequence_capacity"
    ):
        scenario.service.authorize(service_fixtures._authorize_request("blocked.replacement"))
    scenario.expire()
    assert attempts == 2
    scenario.assert_removed()
    scenario.replace_cancelled_parent()


@pytest.mark.parametrize("recovery", ("explicit", "pending", "mutation"))
def test_real_refusal_cleanup_then_publication_failure_recovers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    scenario = _Scenario(tmp_path)
    publish = scenario.service._publish
    cleanup = scenario.production._rollback_unpublished_managed_creation
    removals = 0

    def release(*args: Any, **kwargs: Any) -> None:
        nonlocal removals
        result = cleanup(*args, **kwargs)
        removals += 1
        return result

    def refuse_publication(
        request_id: str, digest: str, result: service_module.ManagedSequenceMutationResultV1
    ) -> None:
        if request_id == "refusal.synthetic":
            raise RuntimeError("synthetic publication refusal")
        return publish(request_id, digest, result)

    monkeypatch.setattr(scenario.production, "_rollback_unpublished_managed_creation", release)
    monkeypatch.setattr(scenario.service, "_publish", refuse_publication)
    before = scenario.entry.sequence
    with pytest.raises(RuntimeError, match="synthetic publication refusal"):
        scenario.service.fail_bound_child(
            service_module.FailBoundManagedSequenceChildRequestV1(
                authority=scenario.authority("refusal.synthetic"),
                segment_id=scenario.segment,
                child_run_handle=scenario.run,
                failure_fingerprint=service_fixtures._fp("queue.synthetic.refusal"),
            )
        )
    assert scenario.entry.sequence is before
    assert scenario.entry.bound_child_binding is not None
    assert scenario.entry.bound_child_cleanup_completed
    scenario.assert_removed()
    assert removals == 1
    scenario.advance()
    if recovery == "explicit":
        scenario.expire()
    elif recovery == "pending":
        scenario.service._lease_transition_owner.apply_pending(
            scenario.authorized.parent_sequence_id, scenario.now
        )
    else:
        with pytest.raises(service_module.ManagedSequenceServiceError, match="lease_expired"):
            scenario.service.cancel(scenario.authority("cancel.expired"))
    assert removals == 1
    scenario.assert_removed()
    scenario.replace_cancelled_parent()


def test_lost_expiry_acknowledgement_does_not_repeat_real_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _Scenario(tmp_path)
    transition = scenario.coordinator.transition_managed_sequence_lease
    cleanup = scenario.production._release_managed_child_workspace
    calls = 0
    removals = 0

    def release(*args: Any, **kwargs: Any) -> None:
        nonlocal removals
        assert removals == 0, "destructive cleanup repeated after successful removal"
        result = cleanup(*args, **kwargs)
        removals += 1
        return result

    def lose_ack(before: ManagedSequenceV1, candidate: ManagedSequenceV1, observed: float) -> None:
        nonlocal calls
        calls += 1
        transition(before, candidate, observed)
        if calls == 1:
            raise RuntimeError("synthetic acknowledgement loss")

    monkeypatch.setattr(scenario.production, "_release_managed_child_workspace", release)
    monkeypatch.setattr(scenario.service._lease_transition_owner, "_child_transition", lose_ack)
    scenario.advance()
    before = scenario.entry.sequence
    with pytest.raises(service_module.ManagedSequenceServiceError):
        scenario.expire()
    scenario.assert_removed()
    assert scenario.entry.sequence is before
    assert scenario.entry.bound_child_binding is not None
    scenario.expire()
    assert calls == 2
    assert removals == 1
    scenario.assert_removed()
    scenario.replace_cancelled_parent()


@pytest.mark.parametrize("proof", (None, "mc_" + "f" * 40))
def test_missing_row_without_exact_cleanup_proof_cannot_erase_authority(
    tmp_path: Path, proof: str | None
) -> None:
    scenario = _Scenario(tmp_path)
    # Deliberate inconsistent-row injection, not an example of successful owner cleanup.
    scenario.coordinator._runs.pop(scenario.run)
    scenario.resource.bound_child_run_handle = None
    scenario.resource.pre_submit_cleaned_child_run_handle = proof
    before = scenario.entry.sequence
    binding = scenario.entry.bound_child_binding
    scenario.advance()
    with pytest.raises(service_module.ManagedSequenceServiceError, match="lease_child_unavailable"):
        scenario.expire()
    assert scenario.entry.sequence is before
    assert scenario.entry.bound_child_binding is binding
    assert scenario.run in scenario.coordinator._managed._runs
    assert scenario.child.production.workspace_handle in scenario.production._entries


@pytest.mark.parametrize("missing", ("binding", "matching_handle", "callback"))
def test_bound_expiry_requires_exact_service_cleanup_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    scenario = _Scenario(tmp_path)
    if missing == "binding":
        scenario.entry.bound_child_binding = None
    elif missing == "matching_handle":
        assert scenario.entry.bound_child_binding is not None
        scenario.entry.bound_child_binding = replace(
            scenario.entry.bound_child_binding, child_run_handle="mc_" + "f" * 40
        )
    else:
        monkeypatch.setattr(scenario.service._lease_transition_owner, "_child_transition", None)
    before = scenario.entry.sequence
    binding = scenario.entry.bound_child_binding
    scenario.advance()
    with pytest.raises(
        service_module.ManagedSequenceServiceError, match="lease_child_authority_unavailable"
    ):
        scenario.expire()
    assert scenario.entry.sequence is before
    assert scenario.entry.bound_child_binding is binding
    assert scenario.resource.bound_child_run_handle == scenario.run
    assert scenario.run in scenario.coordinator._runs
    assert scenario.run in scenario.coordinator._managed._runs
    assert scenario.child.production.workspace_handle in scenario.production._entries


@pytest.mark.parametrize("invocation", ("submitted", "unknown"))
def test_invocation_expiry_retains_real_child_and_blocks_replacement(
    tmp_path: Path,
    invocation: str,
) -> None:
    scenario = _Scenario(tmp_path)
    submitted = coordinator_fixtures._submit(
        scenario.coordinator, scenario.coordinator._response(scenario.child, "prepared")
    )
    unknown_request = service_module.MarkManagedSequenceInvocationUnknownRequestV1(
        authority=scenario.authority("invocation.record"),
        segment_id=scenario.segment,
        child_run_handle=scenario.run,
        timeout_ms=60000,
        active_workflow_fingerprint=service_fixtures._fp("workflow.synthetic"),
        previous_owned_projection_fingerprint=service_fixtures._fp("graph.before"),
        written_owned_projection_fingerprint=service_fixtures._fp("graph.owned"),
    )
    if invocation == "unknown":
        scenario.service.mark_invocation_unknown(unknown_request)
    else:
        scenario.service.record_submission(
            service_module.RecordManagedSequenceSubmissionRequestV1(
                authority=unknown_request.authority,
                segment_id=unknown_request.segment_id,
                child_run_handle=unknown_request.child_run_handle,
                timeout_ms=unknown_request.timeout_ms,
                active_workflow_fingerprint=unknown_request.active_workflow_fingerprint,
                previous_owned_projection_fingerprint=(
                    unknown_request.previous_owned_projection_fingerprint
                ),
                written_owned_projection_fingerprint=(
                    unknown_request.written_owned_projection_fingerprint
                ),
                child_state_fingerprint=submitted.sequence.state.fingerprint,
                queue_prompt_id="prompt.model.1",
            )
        )
    assert scenario.entry.sequence.lease is not None
    assert scenario.entry.sequence.lease.active_deadline is not None
    scenario.now = scenario.entry.sequence.lease.active_deadline + 0.001
    scenario.expire()
    assert scenario.entry.sequence.slots[0].state is SequenceSlotStateV1.UNKNOWN_OWNERSHIP
    assert scenario.entry.sequence.slots[0].child_run_handle == scenario.run
    assert scenario.run in scenario.coordinator._runs
    assert scenario.run in scenario.coordinator._managed._runs
    assert scenario.child.production.workspace_handle in scenario.production._entries
    assert scenario.resource.bound_child_run_handle == scenario.run
    assert scenario.resource.pre_submit_cleaned_child_run_handle is None
    # Inspect this transaction's retained row without a new read triggering the separate fixed
    # detached-lease prune. Expiry must not perform pre-submit cleanup on host-owned authority.
    retained = scenario.coordinator._managed._runs[scenario.run].run
    assert retained.prompt_id == "prompt.model.1"
    assert retained.prepared_sequence == scenario.run
    assert retained.production_workspace == scenario.child.production.workspace_handle
    scenario.service.cancel(scenario.authority("cancel.ambiguous"))
    with pytest.raises(
        service_module.ManagedSequenceServiceError, match="active_sequence_capacity"
    ):
        scenario.service.authorize(service_fixtures._authorize_request("blocked.replacement"))


def test_new_real_child_bind_clears_previous_cleanup_proof(tmp_path: Path) -> None:
    production, context = coordinator_fixtures._production_context()
    tokens = count(1)
    coordinator = coordinator_fixtures._coordinator(
        production,
        tmp_path / "output",
        tmp_path / "private",
        token_factory=lambda: f"{next(tokens):040x}",
    )
    reservation = coordinator_fixtures._managed_start_resource_request(2)
    coordinator.reserve_managed_sequence_resources(reservation)
    first_request = coordinator_fixtures._managed_child_request(
        production,
        context,
        reservation,
        suffix="proof.first",
        slot_revision=2,
    )
    first = coordinator.bind_managed_sequence_child(first_request)
    first.rollback()
    resource = coordinator._managed_start_resources[reservation.parent_sequence_id]
    assert resource.pre_submit_cleaned_child_run_handle == first.child_run_handle
    second_request = coordinator_fixtures._managed_child_request(
        production,
        context,
        reservation,
        suffix="proof.second",
        slot_revision=3,
    )
    second = coordinator.bind_managed_sequence_child(second_request)
    assert second.child_run_handle != first.child_run_handle
    assert resource.bound_child_run_handle == second.child_run_handle
    assert resource.pre_submit_cleaned_child_run_handle is None
    assert second.child_run_handle in coordinator._managed._runs
