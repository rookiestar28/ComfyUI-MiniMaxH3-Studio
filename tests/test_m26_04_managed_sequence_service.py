"""M26-04 parent application service and short-lived child authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256

import pytest

from comfyui_h3_context.adapters.managed_sequence_service import (
    AuthorizeManagedSequenceRequestV1,
    BindManagedSequenceChildRequestV1,
    FailBoundManagedSequenceChildRequestV1,
    FailPreparedManagedSequenceChildRequestV1,
    ManagedChildBindingClaimV1,
    ManagedChildBindingRequestV1,
    ManagedChildCorrelationV1,
    ManagedPreparedGraphObservationV1,
    ManagedSegmentMaterializationClaimV1,
    ManagedSequenceMutationAuthorityV1,
    ManagedSequenceMutationResultV1,
    ManagedSequenceService,
    ManagedSequenceServiceError,
    ManagedSequenceStartResourceClaimV1,
    ManagedSequenceStartResourceRequestV1,
    MarkManagedSequenceInvocationUnknownRequestV1,
    PrepareManagedSequenceChildRequestV1,
    RecordManagedSequenceArtifactRequestV1,
    RecordManagedSequenceCanvasRollbackRequestV1,
    RecordManagedSequenceReuseRequestV1,
    RecordManagedSequenceRunningRequestV1,
    RecordManagedSequenceSubmissionRequestV1,
    RecordManagedSequenceTerminalRequestV1,
    StartManagedSequenceRequestV1,
)
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.managed_sequence import (
    ManagedModeQualificationV1,
    ManagedSequenceAuthorizationV1,
    ManagedSequenceSegmentAuthorizationV1,
    ManagedSequenceStateV1,
    ManagedSequenceV1,
    SequenceSlotStateV1,
)


def _fp(label: str) -> str:
    return f"sha256:{sha256(label.encode('ascii')).hexdigest()}"


def _noop_resource_consume(_request_id: str, _rows: int) -> None:
    return None


def _child_observation(
    segment: str,
    *,
    owned_projection: str | None = None,
) -> ManagedPreparedGraphObservationV1:
    return ManagedPreparedGraphObservationV1(
        route="existing",
        graph_fingerprint=_fp(f"graph.{segment}"),
        compiled_prompt_fingerprint=_fp(f"compiled.{segment}"),
        owned_projection_fingerprint=_fp(
            f"owned.{segment}" if owned_projection is None else owned_projection
        ),
        owned_node_ids=("1", "8", "45"),
        owned_link_ids=("15", "17", "18"),
        model_fingerprint=_fp("model.content-free"),
        runtime_fingerprint=_fp("runtime.content-free"),
        fingerprint_domain="output_producing_graph",
        expected_frames=100,
        source_identity=None,
        timeout_ms=60_000,
        native_anchor_node_id="node.native.h3",
    )


def _child_correlation() -> ManagedChildCorrelationV1:
    return ManagedChildCorrelationV1(
        pending_correlation_id="managed.pending.sequence.child",
        execution_node_id="node.product.shell",
    )


def _authorization(sequence_id: str, count: int = 2) -> ManagedSequenceAuthorizationV1:
    rows = tuple(
        ManagedSequenceSegmentAuthorizationV1(
            segment_id=f"segment.{index + 1}",
            ordinal=index + 1,
            task_mode=TaskMode.T2VA,
            duration_milliseconds=4_000,
            frame_count=100,
            manifest_fingerprint=_fp(f"manifest.{index + 1}"),
            materialization_receipt_fingerprint=_fp(f"materialization.{index + 1}"),
            local_prompt_fingerprint=_fp(f"prompt.{index + 1}"),
            intent_graph_fingerprint=_fp(f"intent.{index + 1}"),
            profile_fingerprint=_fp(f"profile.{index + 1}"),
            capability_fingerprint=_fp("capability.current"),
            predecessor_segment_id=None if index == 0 else f"segment.{index}",
            predecessor_cut_receipt_fingerprint=(
                None if index == 0 else _fp(f"cut.{index}.{index + 1}")
            ),
        )
        for index in range(count)
    )
    return ManagedSequenceAuthorizationV1(
        sequence_id=sequence_id,
        workspace_id="production.workspace.1",
        workspace_revision=7,
        workspace_fingerprint=_fp("workspace.current"),
        production_plan_fingerprint=_fp("production.plan"),
        proposal_fingerprint=_fp("proposal.approved"),
        generation_plan_fingerprint=_fp("generation.plan"),
        compiler_fingerprint=_fp("compiler.current"),
        host_capability_fingerprint=_fp("capability.current"),
        segments=rows,
    )


def _qualification(authority: ManagedSequenceAuthorizationV1) -> ManagedModeQualificationV1:
    return ManagedModeQualificationV1(
        host_capability_fingerprint=authority.host_capability_fingerprint,
        compiler_fingerprint=authority.compiler_fingerprint,
        qualified_global_modes=tuple(TaskMode),
        qualified_materialization_receipts=tuple(
            row.materialization_receipt_fingerprint for row in authority.segments
        ),
        observed_at=90.0,
        expires_at=1_000.0,
    )


class _ProductionRegistry:
    def __init__(self, authority: object = object()) -> None:
        self.authority = authority
        self.calls: list[tuple[object, ...]] = []

    def claim_automatic_plan_authority(
        self,
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_plan_fingerprint: str,
    ) -> object:
        self.calls.append(
            (
                handle,
                expected_workspace_revision,
                expected_workspace_fingerprint,
                expected_plan_fingerprint,
            )
        )
        return self.authority


def _authorize_request(
    request_id: str = "authorize.sequence.1",
) -> AuthorizeManagedSequenceRequestV1:
    return AuthorizeManagedSequenceRequestV1(
        request_id=request_id,
        workspace_handle="pw_" + "a" * 40,
        expected_workspace_revision=7,
        expected_workspace_fingerprint=_fp("workspace.current"),
        expected_plan_fingerprint=_fp("production.plan"),
        generation_plan_fingerprint=_fp("generation.plan"),
        compiler_fingerprint=_fp("compiler.current"),
        host_capability_fingerprint=_fp("capability.current"),
        explicit_intent="generate_approved_sequence",
    )


def _service(
    *,
    materialize: Callable[[object, str], ManagedSegmentMaterializationClaimV1] | None = None,
    bind: Callable[[ManagedChildBindingRequestV1], ManagedChildBindingClaimV1] | None = None,
    clock: Callable[[], float] | None = None,
    epoch_clock_ms: Callable[[], int] | None = None,
    reserve: (
        Callable[[ManagedSequenceStartResourceRequestV1], ManagedSequenceStartResourceClaimV1]
        | None
    ) = None,
) -> tuple[ManagedSequenceService, _ProductionRegistry]:
    production = _ProductionRegistry()
    tokens = iter(("a" * 40, "b" * 40))

    def builder(_source: object, **values: object) -> ManagedSequenceAuthorizationV1:
        return _authorization(str(values["sequence_id"]))

    def default_reserve(
        request: ManagedSequenceStartResourceRequestV1,
    ) -> ManagedSequenceStartResourceClaimV1:
        return ManagedSequenceStartResourceClaimV1(
            request_fingerprint=request.fingerprint,
            coordinator_reservation_fingerprint=_fp("coordinator.reservation"),
            child_slot_reservation_fingerprint=_fp("child.slot.reservation"),
            artifact_reservation_fingerprint=_fp("artifact.reservation"),
            consume=_noop_resource_consume,
            release=lambda: None,
        )

    service = ManagedSequenceService(
        production_registry=production,
        authorization_builder=builder,
        materialize_segment=materialize,
        bind_child=bind,
        reserve_start_resources=default_reserve if reserve is None else reserve,
        token_factory=lambda: next(tokens),
        clock=(lambda: 100.0) if clock is None else clock,
        epoch_clock_ms=(lambda: 1_000_000) if epoch_clock_ms is None else epoch_clock_ms,
    )
    return service, production


def _authorized(
    service: ManagedSequenceService,
) -> tuple[ManagedSequenceMutationResultV1, ManagedSequenceAuthorizationV1]:
    result = service.authorize(_authorize_request())
    authority = _authorization(result.parent_sequence_id)
    return result, authority


def _started(
    service: ManagedSequenceService,
) -> tuple[
    ManagedSequenceMutationResultV1,
    ManagedSequenceAuthorizationV1,
    ManagedSequenceMutationResultV1,
]:
    authorized, authority = _authorized(service)
    started = service.start(
        StartManagedSequenceRequestV1(
            request_id="start.sequence.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=authorized.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        qualification=_qualification(authority),
    )
    return authorized, authority, started


def test_authorize_claims_exact_production_authority_and_is_globally_idempotent() -> None:
    service, production = _service()
    request = _authorize_request()

    first = service.authorize(request)
    replay = service.authorize(request)

    assert replay == replace(first, replayed=True)
    assert production.calls == [
        (
            request.workspace_handle,
            request.expected_workspace_revision,
            request.expected_workspace_fingerprint,
            request.expected_plan_fingerprint,
        )
    ]
    assert first.state is ManagedSequenceStateV1.AUTHORIZED
    assert first.read_authority_fingerprint.startswith("sha256:")
    with pytest.raises(ManagedSequenceServiceError, match="request_id_conflict"):
        service.authorize(replace(request, compiler_fingerprint=_fp("compiler.changed")))
    with pytest.raises(ManagedSequenceServiceError, match="active_sequence_capacity"):
        service.authorize(_authorize_request("authorize.sequence.2"))


def test_start_requires_complete_current_qualification_and_replays_without_mutation() -> None:
    service, _production = _service()
    authorized, authority = _authorized(service)
    request = StartManagedSequenceRequestV1(
        request_id="start.sequence.1",
        parent_sequence_id=authorized.parent_sequence_id,
        expected_revision=authorized.revision,
        authorization_fingerprint=authorized.authorization_fingerprint,
    )
    incomplete = replace(_qualification(authority), qualified_global_modes=(TaskMode.T2VA,))

    with pytest.raises(ManagedSequenceServiceError, match="qualification_incomplete"):
        service.start(request, qualification=incomplete)
    first = service.start(request, qualification=_qualification(authority))
    replay = service.start(request, qualification=_qualification(authority))

    assert first.state is ManagedSequenceStateV1.ACTIVE
    assert first.parent_expires_at_epoch_ms == 87_400_000
    assert replay == replace(first, replayed=True)


def test_start_binds_all_resource_claims_once_and_compensates_invalid_claim() -> None:
    reservations: list[ManagedSequenceStartResourceRequestV1] = []
    releases: list[str] = []

    def reserve(
        request: ManagedSequenceStartResourceRequestV1,
    ) -> ManagedSequenceStartResourceClaimV1:
        reservations.append(request)
        return ManagedSequenceStartResourceClaimV1(
            request_fingerprint=request.fingerprint,
            coordinator_reservation_fingerprint=_fp("coordinator.reservation"),
            child_slot_reservation_fingerprint=_fp("child.slot.reservation"),
            artifact_reservation_fingerprint=_fp("artifact.reservation"),
            consume=_noop_resource_consume,
            release=lambda: releases.append("released"),
        )

    service, _production = _service(reserve=reserve)
    authorized, authority = _authorized(service)
    request = StartManagedSequenceRequestV1(
        request_id="start.sequence.1",
        parent_sequence_id=authorized.parent_sequence_id,
        expected_revision=authorized.revision,
        authorization_fingerprint=authorized.authorization_fingerprint,
    )
    started = service.start(request, qualification=_qualification(authority))
    replay = service.start(request, qualification=_qualification(authority))

    assert replay == replace(started, replayed=True)
    assert len(reservations) == 1
    reservation = reservations[0]
    assert reservation.parent_sequence_id == authorized.parent_sequence_id
    assert reservation.segment_count == 2
    assert reservation.coordinator_reserved_rows == 24
    assert reservation.coordinator_reserved_bytes == 24 * 8_192
    assert reservation.artifact_reserved_entries == 2
    assert reservation.artifact_reserved_bytes == 2 * 128 * 1024 * 1024
    assert reservation.per_artifact_max_bytes == 128 * 1024 * 1024
    assert releases == []

    def invalid_reserve(
        _request: ManagedSequenceStartResourceRequestV1,
    ) -> ManagedSequenceStartResourceClaimV1:
        return ManagedSequenceStartResourceClaimV1(
            request_fingerprint=_fp("wrong.request"),
            coordinator_reservation_fingerprint=_fp("coordinator.reservation"),
            child_slot_reservation_fingerprint=_fp("child.slot.reservation"),
            artifact_reservation_fingerprint=_fp("artifact.reservation"),
            consume=_noop_resource_consume,
            release=lambda: releases.append("invalid.released"),
        )

    invalid, _production = _service(reserve=invalid_reserve)
    invalid_authorized, invalid_authority = _authorized(invalid)
    with pytest.raises(ManagedSequenceServiceError, match="resource_claim_drift"):
        invalid.start(
            StartManagedSequenceRequestV1(
                request_id="start.sequence.1",
                parent_sequence_id=invalid_authorized.parent_sequence_id,
                expected_revision=invalid_authorized.revision,
                authorization_fingerprint=invalid_authorized.authorization_fingerprint,
            ),
            qualification=_qualification(invalid_authority),
        )
    assert releases == ["invalid.released"]
    assert invalid._entry is not None
    assert invalid._entry.sequence.state is ManagedSequenceStateV1.AUTHORIZED
    assert invalid._entry.sequence.revision == invalid_authorized.revision


def test_start_refuses_before_state_change_without_a_real_resource_reserver() -> None:
    production = _ProductionRegistry()
    tokens = iter(("a" * 40, "b" * 40))
    service = ManagedSequenceService(
        production_registry=production,
        authorization_builder=lambda _source, **values: _authorization(str(values["sequence_id"])),
        token_factory=lambda: next(tokens),
        clock=lambda: 100.0,
    )
    authorized, authority = _authorized(service)

    with pytest.raises(ManagedSequenceServiceError, match="resource_reserver_unavailable"):
        service.start(
            StartManagedSequenceRequestV1(
                request_id="start.sequence.1",
                parent_sequence_id=authorized.parent_sequence_id,
                expected_revision=authorized.revision,
                authorization_fingerprint=authorized.authorization_fingerprint,
            ),
            qualification=_qualification(authority),
        )
    assert service._entry is not None
    assert service._entry.sequence.state is ManagedSequenceStateV1.AUTHORIZED
    assert service._entry.sequence.revision == authorized.revision


def test_authorized_parent_can_be_cancelled_after_start_admission_refusal() -> None:
    def refuse_start_resources(
        _request: ManagedSequenceStartResourceRequestV1,
    ) -> ManagedSequenceStartResourceClaimV1:
        raise ManagedSequenceServiceError("artifact_capacity_unavailable", 503)

    service, _production = _service(reserve=refuse_start_resources)
    authorized, authority = _authorized(service)

    with pytest.raises(ManagedSequenceServiceError, match="artifact_capacity_unavailable"):
        service.start(
            StartManagedSequenceRequestV1(
                request_id="start.sequence.refused",
                parent_sequence_id=authorized.parent_sequence_id,
                expected_revision=authorized.revision,
                authorization_fingerprint=authorized.authorization_fingerprint,
            ),
            qualification=_qualification(authority),
        )

    cancelled = service.cancel(
        ManagedSequenceMutationAuthorityV1(
            request_id="cancel.sequence.refused",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=authorized.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        )
    )

    assert cancelled.state is ManagedSequenceStateV1.CANCELLED
    assert service.cancel(
        ManagedSequenceMutationAuthorityV1(
            request_id="cancel.sequence.refused",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=authorized.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        )
    ) == replace(cancelled, replayed=True)


def test_pre_submit_cancel_releases_start_resources_exactly_once() -> None:
    releases: list[str] = []
    consumed: list[tuple[str, int]] = []

    def reserve(
        request: ManagedSequenceStartResourceRequestV1,
    ) -> ManagedSequenceStartResourceClaimV1:
        return ManagedSequenceStartResourceClaimV1(
            request_fingerprint=request.fingerprint,
            coordinator_reservation_fingerprint=_fp("coordinator.reservation"),
            child_slot_reservation_fingerprint=_fp("child.slot.reservation"),
            artifact_reservation_fingerprint=_fp("artifact.reservation"),
            consume=lambda request_id, rows: consumed.append((request_id, rows)),
            release=lambda: releases.append("released"),
        )

    service, _production = _service(reserve=reserve)
    authorized, _authority, started = _started(service)
    cancel = ManagedSequenceMutationAuthorityV1(
        request_id="cancel.sequence.1",
        parent_sequence_id=authorized.parent_sequence_id,
        expected_revision=started.revision,
        authorization_fingerprint=authorized.authorization_fingerprint,
    )

    cancelled = service.cancel(cancel)

    assert service.cancel(cancel) == replace(cancelled, replayed=True)
    assert cancelled.state is ManagedSequenceStateV1.CANCELLED
    assert consumed == [("start.sequence.1", 1), ("cancel.sequence.1", 1)]
    assert releases == ["released"]


def test_prepare_materializes_one_exact_segment_and_replay_does_not_duplicate_claim() -> None:
    releases: list[str] = []
    calls: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        calls.append(segment_id)
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: releases.append(segment_id),
        )

    service, _production = _service(materialize=materialize)
    authorized, _authority, started = _started(service)
    request = PrepareManagedSequenceChildRequestV1(
        request_id="prepare.segment.1",
        parent_sequence_id=authorized.parent_sequence_id,
        expected_revision=started.revision,
        authorization_fingerprint=authorized.authorization_fingerprint,
        segment_id="segment.1",
        predecessor_terminal_fingerprint=None,
    )

    first = service.prepare_child(request)
    replay = service.prepare_child(request)

    assert replay == replace(first, replayed=True)
    assert calls == ["segment.1"]
    assert releases == []
    assert first.context_workspace_handle == "ws_" + "c" * 40
    assert first.execution is not None and first.execution.job_count == 1


def test_pre_submit_expiry_releases_held_context_before_publishing_the_parent_reset() -> None:
    events: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: events.append("context.release"),
        )

    service, _production = _service(materialize=materialize)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )

    expired = service.expire_lease(
        authorized.parent_sequence_id,
        authorized.authorization_fingerprint,
        expected_revision=prepared.revision,
        now=1_000.001,
    )

    assert events == ["context.release"]
    assert expired.state is ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT
    projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert projection is not None
    assert projection.active_segment_id is None
    assert projection.slots[0].state is SequenceSlotStateV1.ELIGIBLE
    assert service._entry is not None
    assert service._entry.held_materialization is None


def test_expired_parent_refuses_a_later_mutation_after_publishing_the_pause() -> None:
    now = [100.0]
    materializations: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        materializations.append(segment_id)
        raise AssertionError("expired mutation reached materialization")

    service, _production = _service(materialize=materialize, clock=lambda: now[0])
    authorized, _authority, started = _started(service)
    now[0] = 1_000.001

    with pytest.raises(ManagedSequenceServiceError, match="lease_expired"):
        service.prepare_child(
            PrepareManagedSequenceChildRequestV1(
                request_id="prepare.after.expiry",
                parent_sequence_id=authorized.parent_sequence_id,
                expected_revision=started.revision,
                authorization_fingerprint=authorized.authorization_fingerprint,
                segment_id="segment.1",
                predecessor_terminal_fingerprint=None,
            )
        )

    projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert projection is not None
    assert projection.state is ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT
    assert projection.revision == started.revision + 1
    assert materializations == []


@pytest.mark.parametrize("transition_fails", (False, True))
def test_bound_expiry_publishes_parent_only_after_the_child_transition(
    transition_fails: bool,
) -> None:
    events: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: events.append("context.release"),
        )

    def bind_child(_request: ManagedChildBindingRequestV1) -> ManagedChildBindingClaimV1:
        return ManagedChildBindingClaimV1(
            child_run_handle="mc_" + "d" * 40,
            child_state_fingerprint=_fp("child.bound"),
            child_job_id="job.segment.1",
            child_transaction_id="transaction.segment.1",
            rollback=lambda: events.append("child.rollback"),
        )

    service, _production = _service(materialize=materialize, bind=bind_child)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None
    bound = service.bind_child(
        BindManagedSequenceChildRequestV1(
            request_id="bind.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=prepared.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            eligible_execution_fingerprint=prepared.execution.fingerprint,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            graph_fingerprint=_fp("graph.segment.1"),
            compiled_prompt_fingerprint=_fp("compiled.segment.1"),
            owned_projection_fingerprint=_fp("owned.segment.1"),
            previous_owned_projection_fingerprint=_fp("owned.segment.1"),
            active_workflow_fingerprint=_fp("workflow.current"),
            correlation=_child_correlation(),
            observation=_child_observation("segment.1"),
        )
    )

    def transition_child(
        before: ManagedSequenceV1,
        candidate: ManagedSequenceV1,
        observed: float,
    ) -> None:
        events.append(
            f"child.transition:{before.slots[0].state.value}:{candidate.slots[0].state.value}:{observed}"
        )
        if transition_fails:
            raise RuntimeError("private transition failure")

    service._lease_transition_owner.bind_child_transition(transition_child)
    if transition_fails:
        with pytest.raises(ManagedSequenceServiceError, match="lease_child_transition_failed"):
            service.expire_lease(
                authorized.parent_sequence_id,
                authorized.authorization_fingerprint,
                expected_revision=bound.revision,
                now=1_000.001,
            )
    else:
        expired = service.expire_lease(
            authorized.parent_sequence_id,
            authorized.authorization_fingerprint,
            expected_revision=bound.revision,
            now=1_000.001,
        )
        assert expired.state is ManagedSequenceStateV1.PAUSED_CLIENT_ABSENT

    projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert projection is not None
    assert events == [
        "context.release",
        "child.transition:bound:eligible:1000.001",
    ]
    if transition_fails:
        assert projection.revision == bound.revision
        assert projection.active_segment_id == "segment.1"
        assert projection.slots[0].state is SequenceSlotStateV1.BOUND
    else:
        assert projection.revision == bound.revision + 1
        assert projection.active_segment_id is None
        assert projection.slots[0].state is SequenceSlotStateV1.ELIGIBLE


def test_prepare_receipt_drift_releases_context_and_leaves_parent_unchanged() -> None:
    releases: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.changed"),
            release=lambda: releases.append(segment_id),
        )

    service, _production = _service(materialize=materialize)
    authorized, _authority, started = _started(service)
    request = PrepareManagedSequenceChildRequestV1(
        request_id="prepare.segment.1",
        parent_sequence_id=authorized.parent_sequence_id,
        expected_revision=started.revision,
        authorization_fingerprint=authorized.authorization_fingerprint,
        segment_id="segment.1",
        predecessor_terminal_fingerprint=None,
    )

    with pytest.raises(ManagedSequenceServiceError, match="materialization_drift"):
        service.prepare_child(request)

    current = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    )
    assert releases == ["segment.1"]
    assert current.projection is not None
    assert current.projection.revision == started.revision
    assert current.projection.slots[0].state is SequenceSlotStateV1.ELIGIBLE


def test_bind_commits_one_child_and_releases_short_lived_context_on_success() -> None:
    events: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: events.append("context.release"),
        )

    def bind_child(request: ManagedChildBindingRequestV1) -> ManagedChildBindingClaimV1:
        events.append(f"bind:{request.context_workspace_handle}")
        return ManagedChildBindingClaimV1(
            child_run_handle="mc_" + "d" * 40,
            child_state_fingerprint=_fp("child.bound"),
            child_job_id="job.segment.1",
            child_transaction_id="transaction.segment.1",
            rollback=lambda: events.append("child.rollback"),
        )

    service, _production = _service(materialize=materialize, bind=bind_child)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None

    bound = service.bind_child(
        BindManagedSequenceChildRequestV1(
            request_id="bind.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=prepared.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            eligible_execution_fingerprint=prepared.execution.fingerprint,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            graph_fingerprint=_fp("graph.segment.1"),
            compiled_prompt_fingerprint=_fp("compiled.segment.1"),
            owned_projection_fingerprint=_fp("owned.segment.1"),
            previous_owned_projection_fingerprint=_fp("owned.segment.1"),
            active_workflow_fingerprint=_fp("workflow.current"),
            correlation=_child_correlation(),
            observation=_child_observation("segment.1"),
        )
    )

    assert bound.state is ManagedSequenceStateV1.ACTIVE
    assert bound.to_wire()["child_authority"] == {
        "schema": "h3.context.managed_sequence_child_authority.v1",
        "run_handle": "mc_" + "d" * 40,
        "state_fingerprint": _fp("child.bound"),
        "job_id": "job.segment.1",
        "transaction_id": "transaction.segment.1",
    }
    assert events == ["bind:ws_" + "c" * 40, "context.release"]
    current = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert current is not None
    assert current.slots[0].state is SequenceSlotStateV1.BOUND


def test_bind_rolls_back_the_child_when_parent_resource_publication_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: events.append("context.release"),
        )

    def bind_child(_request: ManagedChildBindingRequestV1) -> ManagedChildBindingClaimV1:
        events.append("child.bind")
        return ManagedChildBindingClaimV1(
            child_run_handle="mc_" + "d" * 40,
            child_state_fingerprint=_fp("child.bound"),
            child_job_id="job.segment.1",
            child_transaction_id="transaction.segment.1",
            rollback=lambda: events.append("child.rollback"),
        )

    service, _production = _service(materialize=materialize, bind=bind_child)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None

    def fail_consumption(*_args: object, **_kwargs: object) -> None:
        raise ManagedSequenceServiceError("start_resource_consumption_failed", 503)

    monkeypatch.setattr(service, "_consume_candidate_rows", fail_consumption)
    with pytest.raises(
        ManagedSequenceServiceError,
        match="start_resource_consumption_failed",
    ):
        service.bind_child(
            BindManagedSequenceChildRequestV1(
                request_id="bind.segment.1",
                parent_sequence_id=authorized.parent_sequence_id,
                expected_revision=prepared.revision,
                authorization_fingerprint=authorized.authorization_fingerprint,
                segment_id="segment.1",
                eligible_execution_fingerprint=prepared.execution.fingerprint,
                materialization_receipt_fingerprint=_fp("materialization.1"),
                graph_fingerprint=_fp("graph.segment.1"),
                compiled_prompt_fingerprint=_fp("compiled.segment.1"),
                owned_projection_fingerprint=_fp("owned.segment.1"),
                previous_owned_projection_fingerprint=_fp("owned.segment.1"),
                active_workflow_fingerprint=_fp("workflow.current"),
                correlation=_child_correlation(),
                observation=_child_observation("segment.1"),
            )
        )

    assert events == ["child.bind", "context.release", "child.rollback"]
    current = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert current is not None
    assert current.slots[0].state is SequenceSlotStateV1.PREPARED
    assert current.slots[0].child_run_handle is None


def test_read_projection_has_etag_304_and_never_touches_clock_or_mutation_ledger() -> None:
    clock_calls: list[int] = []

    def clock() -> float:
        clock_calls.append(1)
        return 100.0

    service, production = _service(clock=clock)
    authorized, _authority, _started_result = _started(service)
    before_clock = len(clock_calls)
    before_claims = len(production.calls)

    first = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    )
    unchanged = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
        if_none_match=first.etag,
    )

    assert first.status == 200 and first.projection is not None
    assert unchanged.status == 304 and unchanged.projection is None
    assert len(clock_calls) == before_clock
    assert len(production.calls) == before_claims


def test_bound_child_lifecycle_is_one_replayed_parent_action_per_host_transition() -> None:
    bound_segments: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp(
                "materialization.1" if segment_id == "segment.1" else "materialization.2"
            ),
            release=lambda: None,
        )

    def bind_child(request: ManagedChildBindingRequestV1) -> ManagedChildBindingClaimV1:
        bound_segments.append(request.execution.segment_id)
        return ManagedChildBindingClaimV1(
            child_run_handle="mc_" + "d" * 40,
            child_state_fingerprint=_fp("child.bound"),
            child_job_id="job.segment.1",
            child_transaction_id="transaction.segment.1",
            rollback=lambda: None,
        )

    service, _production = _service(materialize=materialize, bind=bind_child)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None
    bound = service.bind_child(
        BindManagedSequenceChildRequestV1(
            request_id="bind.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=prepared.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            eligible_execution_fingerprint=prepared.execution.fingerprint,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            graph_fingerprint=_fp("graph.segment.1"),
            compiled_prompt_fingerprint=_fp("compiled.segment.1"),
            owned_projection_fingerprint=_fp("owned.segment.1.written"),
            previous_owned_projection_fingerprint=_fp("owned.before.sequence"),
            active_workflow_fingerprint=_fp("workflow.current"),
            correlation=_child_correlation(),
            observation=_child_observation("segment.1", owned_projection="owned.segment.1.written"),
        )
    )

    authority = ManagedSequenceMutationAuthorityV1(
        request_id="submit.segment.1",
        parent_sequence_id=authorized.parent_sequence_id,
        expected_revision=bound.revision,
        authorization_fingerprint=authorized.authorization_fingerprint,
    )
    submission_request = RecordManagedSequenceSubmissionRequestV1(
        authority=authority,
        segment_id="segment.1",
        child_run_handle="mc_" + "d" * 40,
        child_state_fingerprint=_fp("child.submitted"),
        queue_prompt_id="prompt.1",
        timeout_ms=60_000,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.before.sequence"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    submitted = service.record_submission(submission_request)
    assert service.record_submission(submission_request) == replace(submitted, replayed=True)

    running = service.record_running(
        RecordManagedSequenceRunningRequestV1(
            authority=replace(
                authority,
                request_id="running.segment.1.prompt.1",
                expected_revision=submitted.revision,
            ),
            segment_id="segment.1",
            queue_prompt_id="prompt.1",
            child_state_fingerprint=_fp("child.running"),
        )
    )
    artifact = service.record_artifact(
        RecordManagedSequenceArtifactRequestV1(
            authority=replace(
                authority,
                request_id="artifact.segment.1",
                expected_revision=running.revision,
            ),
            segment_id="segment.1",
            queue_prompt_id="prompt.1",
            artifact_receipt_fingerprint=_fp("artifact.1"),
            artifact_bytes=1_024,
        )
    )
    terminal = service.record_terminal(
        RecordManagedSequenceTerminalRequestV1(
            authority=replace(
                authority,
                request_id="terminal.segment.1",
                expected_revision=artifact.revision,
            ),
            segment_id="segment.1",
            queue_prompt_id="prompt.1",
            kind="success",
            terminal_fingerprint=_fp("terminal.1"),
        )
    )

    assert terminal.state is ManagedSequenceStateV1.ACTIVE
    projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert projection is not None
    assert projection.slots[0].state is SequenceSlotStateV1.SUCCEEDED
    assert projection.slots[1].state is SequenceSlotStateV1.ELIGIBLE

    prepared_second = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.2",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=terminal.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.2",
            predecessor_terminal_fingerprint=_fp("terminal.1"),
        )
    )
    assert prepared_second.execution is not None
    drifted = service.bind_child(
        BindManagedSequenceChildRequestV1(
            request_id="bind.segment.2.drift",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=prepared_second.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.2",
            eligible_execution_fingerprint=prepared_second.execution.fingerprint,
            materialization_receipt_fingerprint=_fp("materialization.2"),
            graph_fingerprint=_fp("graph.segment.2"),
            compiled_prompt_fingerprint=_fp("compiled.segment.2"),
            owned_projection_fingerprint=_fp("owned.segment.2.candidate"),
            previous_owned_projection_fingerprint=_fp("foreign.canvas"),
            active_workflow_fingerprint=_fp("workflow.current"),
            correlation=_child_correlation(),
            observation=_child_observation(
                "segment.2", owned_projection="owned.segment.2.candidate"
            ),
        )
    )

    assert drifted.state is ManagedSequenceStateV1.PAUSED_CANVAS_DRIFT
    assert bound_segments == ["segment.1"]
    rollback_request = RecordManagedSequenceCanvasRollbackRequestV1(
        authority=ManagedSequenceMutationAuthorityV1(
            request_id="rollback.segment.2",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=drifted.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        segment_id="segment.2",
        active_workflow_fingerprint=_fp("workflow.current"),
        drifted_owned_projection_fingerprint=_fp("foreign.canvas"),
        restored_owned_projection_fingerprint=_fp("owned.segment.1.written"),
    )
    rolled_back = service.record_canvas_rollback(rollback_request)

    assert service.record_canvas_rollback(rollback_request) == replace(
        rolled_back,
        replayed=True,
    )
    assert rolled_back.state is ManagedSequenceStateV1.ACTIVE
    rolled_back_projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert rolled_back_projection is not None
    assert rolled_back_projection.slots[1].state is SequenceSlotStateV1.ELIGIBLE
    assert rolled_back_projection.slots[1].attempt_epoch == 2
    assert bound_segments == ["segment.1"]


def test_explicit_pre_queue_failure_releases_context_before_publishing_pause() -> None:
    events: list[str] = []

    def materialize(
        _authority: object,
        segment_id: str,
    ) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: events.append("context.release"),
        )

    service, _production = _service(materialize=materialize)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None
    request = FailPreparedManagedSequenceChildRequestV1(
        authority=ManagedSequenceMutationAuthorityV1(
            request_id="fail.prepared.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=prepared.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        segment_id="segment.1",
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        failure_fingerprint=_fp("compile.refused"),
    )

    failed = service.fail_prepared_child(request)
    replay = service.fail_prepared_child(request)

    assert events == ["context.release"]
    assert replay == replace(failed, replayed=True)
    assert failed.state is ManagedSequenceStateV1.PAUSED_FAILURE
    projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert projection is not None
    assert projection.active_segment_id is None
    assert projection.slots[0].state is SequenceSlotStateV1.FAILED
    assert projection.slots[0].terminal_fingerprint == _fp("compile.refused")


def test_explicit_queue_rejection_compensates_bound_child_before_parent_pause() -> None:
    events: list[str] = []
    service: ManagedSequenceService

    def materialize(
        _authority: object,
        segment_id: str,
    ) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: events.append("context.release"),
        )

    def rollback() -> None:
        projection = service.read_projection(
            authorized.parent_sequence_id,
            authorized.read_authority_fingerprint,
        ).projection
        assert projection is not None
        events.append(f"child.rollback.{projection.slots[0].state.value}")

    def bind_child(_request: ManagedChildBindingRequestV1) -> ManagedChildBindingClaimV1:
        return ManagedChildBindingClaimV1(
            child_run_handle="mc_" + "d" * 40,
            child_state_fingerprint=_fp("child.bound"),
            child_job_id="job.segment.1",
            child_transaction_id="transaction.segment.1",
            rollback=rollback,
        )

    service, _production = _service(materialize=materialize, bind=bind_child)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None
    bound = service.bind_child(
        BindManagedSequenceChildRequestV1(
            request_id="bind.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=prepared.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            eligible_execution_fingerprint=prepared.execution.fingerprint,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            graph_fingerprint=_fp("graph.segment.1"),
            compiled_prompt_fingerprint=_fp("compiled.segment.1"),
            owned_projection_fingerprint=_fp("owned.segment.1.candidate"),
            previous_owned_projection_fingerprint=_fp("owned.before.sequence"),
            active_workflow_fingerprint=_fp("workflow.current"),
            correlation=_child_correlation(),
            observation=_child_observation(
                "segment.1", owned_projection="owned.segment.1.candidate"
            ),
        )
    )
    request = FailBoundManagedSequenceChildRequestV1(
        authority=ManagedSequenceMutationAuthorityV1(
            request_id="fail.bound.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=bound.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        segment_id="segment.1",
        child_run_handle="mc_" + "d" * 40,
        failure_fingerprint=_fp("queue.rejected"),
    )

    failed = service.fail_bound_child(request)
    replay = service.fail_bound_child(request)

    assert events == ["context.release", "child.rollback.bound"]
    assert replay == replace(failed, replayed=True)
    assert failed.state is ManagedSequenceStateV1.PAUSED_FAILURE


def test_ambiguous_queue_ack_records_owned_write_without_fake_prompt_or_rollback() -> None:
    rollbacks: list[str] = []

    def materialize(
        _authority: object,
        segment_id: str,
    ) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: None,
        )

    def bind_child(_request: ManagedChildBindingRequestV1) -> ManagedChildBindingClaimV1:
        return ManagedChildBindingClaimV1(
            child_run_handle="mc_" + "d" * 40,
            child_state_fingerprint=_fp("child.bound"),
            child_job_id="job.segment.1",
            child_transaction_id="transaction.segment.1",
            rollback=lambda: rollbacks.append("child.rollback"),
        )

    service, _production = _service(materialize=materialize, bind=bind_child)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None
    bound = service.bind_child(
        BindManagedSequenceChildRequestV1(
            request_id="bind.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=prepared.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            eligible_execution_fingerprint=prepared.execution.fingerprint,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            graph_fingerprint=_fp("graph.segment.1"),
            compiled_prompt_fingerprint=_fp("compiled.segment.1"),
            owned_projection_fingerprint=_fp("owned.segment.1.candidate"),
            previous_owned_projection_fingerprint=_fp("owned.before.sequence"),
            active_workflow_fingerprint=_fp("workflow.current"),
            correlation=_child_correlation(),
            observation=_child_observation(
                "segment.1", owned_projection="owned.segment.1.candidate"
            ),
        )
    )
    request = MarkManagedSequenceInvocationUnknownRequestV1(
        authority=ManagedSequenceMutationAuthorityV1(
            request_id="unknown.invocation.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=bound.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        segment_id="segment.1",
        child_run_handle="mc_" + "d" * 40,
        timeout_ms=60_000,
        active_workflow_fingerprint=_fp("workflow.current"),
        previous_owned_projection_fingerprint=_fp("owned.before.sequence"),
        written_owned_projection_fingerprint=_fp("owned.segment.1.candidate"),
    )

    unknown = service.mark_invocation_unknown(request)
    replay = service.mark_invocation_unknown(request)

    assert replay == replace(unknown, replayed=True)
    assert rollbacks == []
    projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert projection is not None
    assert projection.state is ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP
    assert projection.slots[0].state is SequenceSlotStateV1.UNKNOWN_OWNERSHIP
    assert projection.slots[0].queue_prompt_id is None


def test_verified_reuse_never_materializes_binds_writes_or_submits() -> None:
    service, _production = _service()
    authorized, _authority, started = _started(service)
    reused = service.record_reuse(
        RecordManagedSequenceReuseRequestV1(
            authority=ManagedSequenceMutationAuthorityV1(
                request_id="reuse.segment.1",
                parent_sequence_id=authorized.parent_sequence_id,
                expected_revision=started.revision,
                authorization_fingerprint=authorized.authorization_fingerprint,
            ),
            segment_id="segment.1",
            artifact_receipt_fingerprint=_fp("artifact.reused.1"),
            artifact_bytes=1_024,
            terminal_fingerprint=_fp("terminal.reused.1"),
        )
    )

    assert reused.state is ManagedSequenceStateV1.ACTIVE
    projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert projection is not None
    assert projection.slots[0].state is SequenceSlotStateV1.REUSED
    assert projection.slots[0].child_run_handle is None


def test_binding_failure_releases_context_and_parent_no_longer_sticks_prepared() -> None:
    releases: list[str] = []

    def materialize(_authority: object, segment_id: str) -> ManagedSegmentMaterializationClaimV1:
        return ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "c" * 40,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: releases.append("context"),
        )

    def bind_child(_request: ManagedChildBindingRequestV1) -> ManagedChildBindingClaimV1:
        raise RuntimeError("private binder detail")

    service, _production = _service(materialize=materialize, bind=bind_child)
    authorized, _authority, started = _started(service)
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.segment.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None

    request = BindManagedSequenceChildRequestV1(
        request_id="bind.segment.1",
        parent_sequence_id=authorized.parent_sequence_id,
        expected_revision=prepared.revision,
        authorization_fingerprint=authorized.authorization_fingerprint,
        segment_id="segment.1",
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        materialization_receipt_fingerprint=_fp("materialization.1"),
        graph_fingerprint=_fp("graph.segment.1"),
        compiled_prompt_fingerprint=_fp("compiled.segment.1"),
        owned_projection_fingerprint=_fp("owned.segment.1"),
        previous_owned_projection_fingerprint=_fp("owned.segment.1"),
        active_workflow_fingerprint=_fp("workflow.current"),
        correlation=_child_correlation(),
        observation=_child_observation("segment.1"),
    )

    failed = service.bind_child(request)
    replay = service.bind_child(request)

    assert failed.state is ManagedSequenceStateV1.PAUSED_FAILURE
    assert replay == replace(failed, replayed=True)
    assert "private binder detail" not in str(failed.to_wire())

    projection = service.read_projection(
        authorized.parent_sequence_id,
        authorized.read_authority_fingerprint,
    ).projection
    assert releases == ["context"]
    assert projection is not None
    assert projection.state is ManagedSequenceStateV1.PAUSED_FAILURE
    assert projection.active_segment_id is None
    assert projection.slots[0].state is SequenceSlotStateV1.FAILED
