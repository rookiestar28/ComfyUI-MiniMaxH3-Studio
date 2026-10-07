"""Public parent-service recovery across cancellation, replay and cleanup failures."""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from itertools import count
from threading import RLock

import pytest
import test_m26_04_managed_sequence_service as fixture

from comfyui_h3_context.adapters import comfyui_sequence_coordinator as coordinator_module
from comfyui_h3_context.adapters import managed_sequence_service as service_module
from comfyui_h3_context.adapters.managed_run_registry import ManagedRunSlotReservationV1
from comfyui_h3_context.core import managed_sequence as core


def _physical_reserver() -> tuple[
    coordinator_module.SequenceCoordinatorRegistry,
    Callable[
        [service_module.ManagedSequenceStartResourceRequestV1],
        service_module.ManagedSequenceStartResourceClaimV1,
    ],
]:
    registry = coordinator_module.SequenceCoordinatorRegistry.__new__(
        coordinator_module.SequenceCoordinatorRegistry
    )
    registry._lock = RLock()
    registry._managed_start_resources = OrderedDict()

    def reserve(
        request: service_module.ManagedSequenceStartResourceRequestV1,
    ) -> service_module.ManagedSequenceStartResourceClaimV1:
        state = coordinator_module._ManagedStartResourceState(
            request_fingerprint=request.fingerprint,
            parent_sequence_id=request.parent_sequence_id,
            parent_authorization_fingerprint=request.parent_authorization_fingerprint,
            segment_count=request.segment_count,
            reserved_rows=request.coordinator_reserved_rows,
            reserved_bytes=request.coordinator_reserved_bytes,
            idle_expires_at=request.idle_expires_at,
            expires_at=request.expires_at,
            child_slot=ManagedRunSlotReservationV1(
                "slot.synthetic",
                request.parent_sequence_id,
                request.parent_authorization_fingerprint,
                request.expires_at,
            ),
        )
        registry._managed_start_resources[request.parent_sequence_id] = state
        return service_module.ManagedSequenceStartResourceClaimV1(
            request_fingerprint=request.fingerprint,
            coordinator_reservation_fingerprint=fixture._fp("coordinator.synthetic"),
            child_slot_reservation_fingerprint=fixture._fp("slot.synthetic"),
            artifact_reservation_fingerprint=fixture._fp("artifact.synthetic"),
            consume=lambda request_id, rows: registry._consume_managed_sequence_rows(
                request.parent_sequence_id, request.fingerprint, request_id, rows
            ),
            release=lambda: None,
        )

    return registry, reserve


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("segment_count", [1, 2, core.MAX_MANAGED_SEQUENCE_SEGMENTS])
def test_attachment_churn_preserves_real_coordinator_cancel_capacity(
    alternate: bool,
    segment_count: int,
) -> None:
    registry, reserve = _physical_reserver()
    service, _ = fixture._service(reserve=reserve)
    service._authorization_builder = lambda _source, **values: fixture._authorization(
        str(values["sequence_id"]), count=segment_count
    )
    authorized = service.authorize(fixture._authorize_request())
    assert service._entry is not None
    current = service.start(
        service_module.StartManagedSequenceRequestV1(
            "start.churn",
            authorized.parent_sequence_id,
            authorized.revision,
            authorized.authorization_fingerprint,
        ),
        qualification=fixture._qualification(service._entry.sequence.authorization),
    )
    state = registry._managed_start_resources[authorized.parent_sequence_id]
    assert state.reserved_rows == core.coordinator_ledger_rows(segment_count)
    rejected = []
    for index in range(40):
        try:
            request = _authority(authorized, current, f"detach.churn.{index}")
            current = service.detach(request)
            assert service.detach(request) == replace(current, replayed=True)
            if alternate:
                current = service.resume(
                    service_module.ResumeManagedSequenceRequestV1(
                        authority=_authority(authorized, current, f"resume.churn.{index}"),
                        active_workflow_fingerprint=fixture._fp("workflow.current"),
                        owned_projection_fingerprint=fixture._fp("owned.segment.1"),
                    )
                )
        except service_module.ManagedSequenceServiceError as exc:
            rejected.append(str(exc))
    assert rejected
    assert not any("resource_consumption_capacity" in error for error in rejected)
    before_cancel = state.consumed_rows
    cancelled = service.cancel(_authority(authorized, current, "cancel.after.churn"))
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED
    assert state.consumed_rows == before_cancel + 1
    assert state.consumed_rows <= state.reserved_rows


def _authority(
    authorized: service_module.ManagedSequenceMutationResultV1,
    result: service_module.ManagedSequenceMutationResultV1,
    request_id: str,
) -> service_module.ManagedSequenceMutationAuthorityV1:
    return service_module.ManagedSequenceMutationAuthorityV1(
        request_id,
        authorized.parent_sequence_id,
        result.revision,
        authorized.authorization_fingerprint,
    )


def _environment(
    *,
    release_fails: bool = False,
    rollback_failures: int = 0,
    context_version: int = 1,
    reserve: Callable[
        [service_module.ManagedSequenceStartResourceRequestV1],
        service_module.ManagedSequenceStartResourceClaimV1,
    ]
    | None = None,
) -> tuple[
    service_module.ManagedSequenceService,
    service_module.ManagedSequenceMutationResultV1,
    service_module.ManagedSequenceMutationResultV1,
    service_module.BindManagedSequenceChildRequestV1,
    list[str],
    set[str],
]:
    events: list[str] = []
    children: set[str] = set()
    remaining_failures = rollback_failures

    def release() -> None:
        events.append("context.release")
        if release_fails:
            raise RuntimeError("synthetic cleanup failure")

    def rollback() -> None:
        nonlocal remaining_failures
        events.append("child.rollback")
        if remaining_failures:
            remaining_failures -= 1
            raise RuntimeError("synthetic rollback failure")
        children.remove("child.1")

    def materialize(
        _source: object, segment: str
    ) -> service_module.ManagedSegmentMaterializationClaimV1:
        return service_module.ManagedSegmentMaterializationClaimV1(
            "ws_" + "c" * 40, segment, fixture._fp("materialization.1"), release
        )

    def bind(
        _request: service_module.ManagedChildBindingRequestV1,
    ) -> service_module.ManagedChildBindingClaimV1:
        assert not children, "cannot bind a second child while the first remains owned"
        children.add("child.1")
        events.append("child.bind")
        return service_module.ManagedChildBindingClaimV1(
            "mc_" + "d" * 40,
            fixture._fp("child.bound"),
            "job.segment.1",
            "transaction.segment.1",
            rollback,
        )

    service, _ = fixture._service(materialize=materialize, bind=bind, reserve=reserve)
    tokens = count()
    service._token_factory = lambda: "recovery." + str(next(tokens))
    authorized, _, started = fixture._started(service)
    request = service_module.PrepareManagedSequenceChildRequestV1(
        "prepare.1",
        authorized.parent_sequence_id,
        started.revision,
        authorized.authorization_fingerprint,
        "segment.1",
        None,
    )
    prepared = service.prepare_child(request)
    assert prepared.execution is not None
    binding = service_module.BindManagedSequenceChildRequestV1(
        request_id="bind.1",
        parent_sequence_id=authorized.parent_sequence_id,
        expected_revision=prepared.revision,
        authorization_fingerprint=authorized.authorization_fingerprint,
        segment_id="segment.1",
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        materialization_receipt_fingerprint=fixture._fp("materialization.1"),
        graph_fingerprint=fixture._fp("graph.segment.1"),
        compiled_prompt_fingerprint=fixture._fp("compiled.segment.1"),
        owned_projection_fingerprint=fixture._fp("owned.segment.1"),
        previous_owned_projection_fingerprint=fixture._fp("owned.segment.1"),
        active_workflow_fingerprint=fixture._fp("workflow.current"),
        correlation=fixture._child_correlation(),
        observation=fixture._child_observation("segment.1"),
    )
    if context_version == 2:
        from test_m26_03_prepared_context import _prepared_service

        from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry

        service, read, _source, _context, _proposal = _prepared_service(
            SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
        )
        service._bind_child = bind
        assert service._entry is not None
        held = service._entry.held_materialization
        assert held is not None
        original_release = held.claim.release

        def release_real_context() -> None:
            original_release()
            release()

        held.claim = replace(held.claim, release=release_real_context)
        prepared = service._result(service._entry)
        authorized = prepared
        binding = replace(
            binding,
            parent_sequence_id=read.parent_sequence_id,
            expected_revision=read.expected_revision,
            authorization_fingerprint=read.authorization_fingerprint,
            segment_id=read.segment_id,
            eligible_execution_fingerprint=read.eligible_execution_fingerprint,
            materialization_receipt_fingerprint=read.materialization_receipt_fingerprint,
        )
    return service, authorized, prepared, binding, events, children


def _submit(
    service: service_module.ManagedSequenceService,
    authorized: service_module.ManagedSequenceMutationResultV1,
    bound: service_module.ManagedSequenceMutationResultV1,
) -> service_module.ManagedSequenceMutationResultV1:
    assert bound.child_authority is not None
    return service.record_submission(
        service_module.RecordManagedSequenceSubmissionRequestV1(
            authority=_authority(authorized, bound, "submission.1"),
            segment_id="segment.1",
            child_run_handle=bound.child_authority.run_handle,
            child_state_fingerprint=fixture._fp("child.submitted"),
            queue_prompt_id="prompt.synthetic.1",
            timeout_ms=60_000,
            active_workflow_fingerprint=fixture._fp("workflow.current"),
            previous_owned_projection_fingerprint=fixture._fp("owned.segment.1"),
            written_owned_projection_fingerprint=fixture._fp("owned.segment.1.written"),
        )
    )


@pytest.mark.parametrize("before_submission", [False, True])
def test_real_capacity_keeps_cancel_and_late_child_cleanup_after_detach_churn(
    before_submission: bool,
) -> None:
    registry, reserve = _physical_reserver()
    service, authorized, _, binding, _, _ = _environment(reserve=reserve)
    bound = service.bind_child(binding)
    current = bound if before_submission else _submit(service, authorized, bound)
    current = service.detach(_authority(authorized, current, "detach.owned"))
    state = registry._managed_start_resources[authorized.parent_sequence_id]
    before = state.consumed_rows
    for index in range(40):
        with pytest.raises(
            service_module.ManagedSequenceServiceError, match="client_already_detached"
        ):
            service.detach(_authority(authorized, current, f"detach.owned.{index}"))
    assert state.consumed_rows == before
    assert service._entry is not None and service._entry.start_resources is not None
    # Synthetic reservation pressure still executes the real coordinator consumption method.
    # Leave exactly cancel + optional late submission + terminal/release, not spare capacity.
    cleanup_rows = 3 if before_submission else 2
    service._entry.start_resources.consume_rows(
        "pressure.synthetic", state.reserved_rows - state.consumed_rows - cleanup_rows - 1
    )
    current = service.cancel(_authority(authorized, current, "cancel.owned"))
    if before_submission:
        current = _submit(service, authorized, replace(bound, revision=current.revision))
    current = service.record_terminal(
        service_module.RecordManagedSequenceTerminalRequestV1(
            authority=_authority(authorized, current, "terminal.owned"),
            segment_id="segment.1",
            queue_prompt_id="prompt.synthetic.1",
            kind="error",
            terminal_fingerprint=fixture._fp("terminal.owned"),
        )
    )
    assert current.state is core.ManagedSequenceStateV1.CANCELLED
    assert state.consumed_rows == state.reserved_rows
    assert service._entry is not None and service._can_retire(service._entry)


def test_attachment_cannot_spend_real_owned_child_cleanup_reservation() -> None:
    registry, reserve = _physical_reserver()
    service, authorized, _, binding, _, _ = _environment(reserve=reserve)
    current = service.bind_child(binding)
    assert service._entry is not None and service._entry.start_resources is not None
    state = registry._managed_start_resources[authorized.parent_sequence_id]
    service._entry.start_resources.consume_rows(
        "pressure.synthetic", state.reserved_rows - state.consumed_rows - 4
    )
    for index in range(40):
        with pytest.raises(service_module.ManagedSequenceServiceError, match="recovery_capacity"):
            service.detach(_authority(authorized, current, f"detach.capacity.{index}"))
    assert state.consumed_rows == state.reserved_rows - 4
    assert service._entry.sequence.client_attached
    assert service._entry.sequence.revision == current.revision
    bound = current
    current = service.cancel(_authority(authorized, current, "cancel.capacity"))
    current = _submit(service, authorized, replace(bound, revision=current.revision))
    current = service.record_terminal(
        service_module.RecordManagedSequenceTerminalRequestV1(
            authority=_authority(authorized, current, "terminal.capacity"),
            segment_id="segment.1",
            queue_prompt_id="prompt.synthetic.1",
            kind="error",
            terminal_fingerprint=fixture._fp("terminal.capacity"),
        )
    )
    assert current.state is core.ManagedSequenceStateV1.CANCELLED
    assert state.consumed_rows == state.reserved_rows


def test_cancel_cleanup_retries_new_ids_do_not_spend_the_same_terminal_reservation() -> None:
    registry, reserve = _physical_reserver()
    service, _ = fixture._service(reserve=reserve)
    authorized, _, started = fixture._started(service)
    assert service._entry is not None and service._entry.start_resources is not None
    resources = service._entry.start_resources
    original_release = resources.claim.release

    def fail_release() -> None:
        raise RuntimeError("synthetic release failure")

    resources.claim = replace(resources.claim, release=fail_release)
    for index in range(40):
        with pytest.raises(
            service_module.ManagedSequenceServiceError, match="start_resource_release_failed"
        ):
            service.cancel(_authority(authorized, started, f"cancel.retry.{index}"))
    resources.claim = replace(resources.claim, release=original_release)
    cancelled = service.cancel(_authority(authorized, started, "cancel.retry.final"))
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED
    state = registry._managed_start_resources[authorized.parent_sequence_id]
    assert state.consumed_rows == 3


def test_cancelled_host_owned_parent_remains_addressable_until_late_terminal() -> None:
    service, authorized, _, binding, events, children = _environment()
    bound = service.bind_child(binding)
    submitted = _submit(service, authorized, bound)
    cancelled = service.cancel(_authority(authorized, submitted, "cancel.1"))
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED
    with pytest.raises(
        service_module.ManagedSequenceServiceError, match="active_sequence_capacity"
    ):
        service.authorize(fixture._authorize_request("authorize.next"))
    current = service.read_projection(
        authorized.parent_sequence_id, authorized.read_authority_fingerprint
    ).projection
    assert current is not None
    assert current.slots[0].state is core.SequenceSlotStateV1.SUBMITTED
    assert current.slots[0].queue_prompt_id == "prompt.synthetic.1"
    assert children == {"child.1"}
    assert "child.rollback" not in events
    terminal = service.record_terminal(
        service_module.RecordManagedSequenceTerminalRequestV1(
            authority=_authority(authorized, cancelled, "terminal.late"),
            segment_id="segment.1",
            queue_prompt_id="prompt.synthetic.1",
            kind="error",
            terminal_fingerprint=fixture._fp("terminal.late"),
        )
    )
    assert terminal.state is core.ManagedSequenceStateV1.CANCELLED
    replacement = service.authorize(fixture._authorize_request("authorize.next"))
    assert replacement.parent_sequence_id != authorized.parent_sequence_id


@pytest.mark.parametrize("churn", [False, True])
@pytest.mark.parametrize("cancel_before_terminal", [False, True])
def test_real_retry_attachment_budget_covers_full_retry_and_cancel(
    churn: bool,
    cancel_before_terminal: bool,
) -> None:
    registry, reserve = _physical_reserver()
    service, authorized, prepared, binding, _, _ = _environment(reserve=reserve)
    assert prepared.execution is not None
    failed = service.fail_prepared_child(
        service_module.FailPreparedManagedSequenceChildRequestV1(
            authority=_authority(authorized, prepared, "fail.retry.initial"),
            segment_id="segment.1",
            eligible_execution_fingerprint=prepared.execution.fingerprint,
            failure_fingerprint=fixture._fp("retry.initial.failure"),
        )
    )
    current = service.retry(
        service_module.ManagedSequenceSegmentMutationRequestV1(
            authority=_authority(authorized, failed, "retry.initial"),
            segment_id="segment.1",
            active_workflow_fingerprint=fixture._fp("workflow.current"),
            owned_projection_fingerprint=fixture._fp("owned.segment.1"),
        )
    )
    rounds = 0
    for index in range(40 if churn else 1):
        try:
            detached = service.detach(_authority(authorized, current, f"detach.retry.{index}"))
        except service_module.ManagedSequenceServiceError as exc:
            assert str(exc) in {"recovery_capacity", "ledger_reservation_exhausted"}
            break
        current = service.resume(
            service_module.ResumeManagedSequenceRequestV1(
                authority=_authority(authorized, detached, f"resume.retry.{index}"),
                active_workflow_fingerprint=fixture._fp("workflow.current"),
                owned_projection_fingerprint=fixture._fp("owned.segment.1"),
            )
        )
        rounds += 1
    assert rounds >= 1
    current = service.prepare_child(
        service_module.PrepareManagedSequenceChildRequestV1(
            "prepare.retry.full",
            authorized.parent_sequence_id,
            current.revision,
            authorized.authorization_fingerprint,
            "segment.1",
            None,
        )
    )
    assert current.execution is not None
    current = service.bind_child(
        replace(
            binding,
            request_id="bind.retry.full",
            expected_revision=current.revision,
            eligible_execution_fingerprint=current.execution.fingerprint,
        )
    )
    current = _submit(service, authorized, current)
    current = service.record_running(
        service_module.RecordManagedSequenceRunningRequestV1(
            authority=_authority(authorized, current, "running.retry.full"),
            segment_id="segment.1",
            queue_prompt_id="prompt.synthetic.1",
            child_state_fingerprint=fixture._fp("retry.running"),
        )
    )
    if cancel_before_terminal:
        current = service.cancel(_authority(authorized, current, "cancel.retry.live"))
    current = service.record_artifact(
        service_module.RecordManagedSequenceArtifactRequestV1(
            authority=_authority(authorized, current, "artifact.retry.full"),
            segment_id="segment.1",
            queue_prompt_id="prompt.synthetic.1",
            artifact_receipt_fingerprint=fixture._fp("retry.artifact"),
            artifact_bytes=1024,
        )
    )
    current = service.record_terminal(
        service_module.RecordManagedSequenceTerminalRequestV1(
            authority=_authority(authorized, current, "terminal.retry.full"),
            segment_id="segment.1",
            queue_prompt_id="prompt.synthetic.1",
            kind="success",
            terminal_fingerprint=fixture._fp("retry.terminal"),
        )
    )
    if not cancel_before_terminal:
        current = service.cancel(_authority(authorized, current, "cancel.retry.finished"))
    assert current.state is core.ManagedSequenceStateV1.CANCELLED
    state = registry._managed_start_resources[authorized.parent_sequence_id]
    assert state.consumed_rows <= state.reserved_rows


def test_cancelled_bound_parent_keeps_authority_for_late_submission() -> None:
    service, authorized, _, binding, events, _ = _environment()
    bound = service.bind_child(binding)
    cancelled = service.cancel(_authority(authorized, bound, "cancel.before.ack"))
    current = service.read_projection(
        authorized.parent_sequence_id, authorized.read_authority_fingerprint
    ).projection
    assert current is not None
    assert current.slots[0].state is core.SequenceSlotStateV1.BOUND
    assert "child.rollback" not in events
    submitted = _submit(service, authorized, replace(bound, revision=cancelled.revision))
    assert submitted.state is core.ManagedSequenceStateV1.CANCELLED
    with pytest.raises(
        service_module.ManagedSequenceServiceError, match="active_sequence_capacity"
    ):
        service.authorize(fixture._authorize_request("authorize.after.bound.cancel"))


@pytest.mark.parametrize("start_parent", [False, True])
@pytest.mark.parametrize("segment_count", [2, core.MAX_MANAGED_SEQUENCE_SEGMENTS])
def test_completed_parent_cycles_preserve_cancel_capacity_and_current_replay(
    start_parent: bool, segment_count: int
) -> None:
    service, production = fixture._service()
    service._authorization_builder = lambda _source, **values: fixture._authorization(
        str(values["sequence_id"]), count=segment_count
    )
    tokens = count()
    service._token_factory = lambda: "cycle." + str(next(tokens))
    for index in range(150):
        request = fixture._authorize_request(f"authorize.{index}")
        authorized = service.authorize(request)
        replay = service.authorize(request)
        assert replay == replace(authorized, replayed=True)
        current = authorized
        if start_parent:
            assert service._entry is not None
            current = service.start(
                service_module.StartManagedSequenceRequestV1(
                    request_id=f"start.{index}",
                    parent_sequence_id=authorized.parent_sequence_id,
                    expected_revision=authorized.revision,
                    authorization_fingerprint=authorized.authorization_fingerprint,
                ),
                qualification=fixture._qualification(service._entry.sequence.authorization),
            )
        cancellation = _authority(authorized, current, f"cancel.{index}")
        cancelled = service.cancel(cancellation)
        assert service.cancel(cancellation) == replace(cancelled, replayed=True)
        assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED
        assert len(service._ledger) <= service_module.MAX_MANAGED_SEQUENCE_MUTATION_ROWS
        with pytest.raises(service_module.ManagedSequenceServiceError, match="request_id_conflict"):
            service.authorize(replace(request, compiler_fingerprint=fixture._fp("changed")))
    assert len(production.calls) == 150


def test_failed_replacement_publication_preserves_old_parent_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = fixture._service()
    tokens = count()
    service._token_factory = lambda: "publication." + str(next(tokens))
    authorized = service.authorize(fixture._authorize_request("authorize.original"))
    cancel = _authority(authorized, authorized, "cancel.original")
    cancelled = service.cancel(cancel)
    previous_entry = service._entry
    previous_ledger = service._ledger.copy()
    original = service_module.ManagedSequenceService._publish

    def fail_after_publish(
        self: service_module.ManagedSequenceService,
        request_id: str,
        digest: str,
        result: service_module.ManagedSequenceMutationResultV1,
    ) -> None:
        original(self, request_id, digest, result)
        if request_id == "authorize.replacement":
            raise RuntimeError("synthetic publication rejection")

    monkeypatch.setattr(service_module.ManagedSequenceService, "_publish", fail_after_publish)
    with pytest.raises(RuntimeError, match="synthetic publication rejection"):
        service.authorize(fixture._authorize_request("authorize.replacement"))
    assert service._entry is previous_entry
    assert service._ledger == previous_ledger
    assert service.cancel(cancel) == replace(cancelled, replayed=True)


@pytest.mark.parametrize("release_failure", [False, True])
def test_cancelled_bound_parent_explicit_nonownership_cleanup_does_not_resurrect(
    release_failure: bool,
) -> None:
    service, authorized, _, binding, _, children = _environment()
    bound = service.bind_child(binding)
    assert bound.child_authority is not None
    cancelled = service.cancel(_authority(authorized, bound, "cancel.before.refusal"))
    request = service_module.FailBoundManagedSequenceChildRequestV1(
        authority=_authority(authorized, cancelled, "bound.refused"),
        segment_id="segment.1",
        child_run_handle=bound.child_authority.run_handle,
        failure_fingerprint=fixture._fp("queue.refused"),
    )
    if release_failure:
        assert service._entry is not None
        held = service._entry.start_resources
        assert held is not None
        original = held.claim.release
        calls = 0

        def release() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic resource release failure")
            original()

        held.claim = replace(held.claim, release=release)
        with pytest.raises(
            service_module.ManagedSequenceServiceError, match="start_resource_release_failed"
        ):
            service.fail_bound_child(request)
    cleaned = service.fail_bound_child(request)
    assert cleaned.state is core.ManagedSequenceStateV1.CANCELLED
    assert not children
    service.authorize(fixture._authorize_request("authorize.after.refusal"))


@pytest.mark.parametrize("context_version", [1, 2])
def test_context_release_failure_compensates_provisional_child(context_version: int) -> None:
    service, authorized, prepared, binding, events, children = _environment(
        release_fails=True, context_version=context_version
    )
    with pytest.raises(
        service_module.ManagedSequenceServiceError, match="prepared_context_release_failed"
    ):
        service.bind_child(binding)
    assert events == ["child.bind", "context.release", "child.rollback"]
    assert not children
    cancelled = service.cancel(_authority(authorized, prepared, "cancel.after.release"))
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED


@pytest.mark.parametrize("context_version", [1, 2])
@pytest.mark.parametrize("recover_by_expiry", [False, True])
def test_failed_compensation_retains_child_until_cancel_retries_cleanup(
    context_version: int, recover_by_expiry: bool
) -> None:
    service, authorized, prepared, binding, events, children = _environment(
        release_fails=True, rollback_failures=1, context_version=context_version
    )
    with pytest.raises(
        service_module.ManagedSequenceServiceError, match="child_binding_compensation_failed"
    ):
        service.bind_child(binding)
    assert children == {"child.1"}
    assert service._entry is not None
    assert service._entry.bound_child_binding is not None
    if recover_by_expiry:
        prepared = service.expire_lease(
            authorized.parent_sequence_id,
            authorized.authorization_fingerprint,
            expected_revision=prepared.revision,
            now=100_000.0,
        )
        assert not children
    cancelled = service.cancel(_authority(authorized, prepared, "cancel.after.compensation"))
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED
    assert not children
    assert events == ["child.bind", "context.release", "child.rollback", "child.rollback"]
    assert service._entry.bound_child_binding is None
    assert service._can_retire(service._entry)


@pytest.mark.parametrize("context_version", [1, 2])
@pytest.mark.parametrize("rollback_failures", [0, 1])
def test_publication_failure_compensates_or_retains_retryable_binding(
    monkeypatch: pytest.MonkeyPatch, context_version: int, rollback_failures: int
) -> None:
    service, authorized, prepared, binding, events, children = _environment(
        context_version=context_version, rollback_failures=rollback_failures
    )
    original = service_module.ManagedSequenceService._validate_publication

    def reject_bound(
        self: service_module.ManagedSequenceService,
        result: service_module.ManagedSequenceMutationResultV1,
    ) -> None:
        if result.child_authority is not None:
            raise RuntimeError("synthetic publication failure")
        return original(self, result)

    monkeypatch.setattr(
        service_module.ManagedSequenceService, "_validate_publication", reject_bound
    )
    expected = (
        "child_binding_compensation_failed"
        if rollback_failures
        else "child_binding_publication_failed"
    )
    with pytest.raises(service_module.ManagedSequenceServiceError, match=expected):
        service.bind_child(binding)
    assert bool(children) is bool(rollback_failures)
    cancelled = service.cancel(_authority(authorized, prepared, "cancel.publication"))
    assert cancelled.state is core.ManagedSequenceStateV1.CANCELLED
    assert not children
    assert events.count("child.bind") == 1
    assert events.count("child.rollback") == rollback_failures + 1
    assert service._entry is not None
    assert service._can_retire(service._entry)
