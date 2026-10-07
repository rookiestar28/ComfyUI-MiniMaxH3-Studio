"""The converse of M23-51's addressability fix: the aggregate goes first, the coordinator row stays.

M23-51 closed the direction that mattered for the item -- a run retained by its lease stayed
addressable through the coordinator. The round-2 reviewer found that the record's claim about the
*other* direction was wrong. A detached lease shorter than the coordinator's own 900-second TTL
expires first, and a real `record_running` / `record_artifact` / `record_terminal` arriving in that
window reached `_mirror`, whose `read` and `advance` were both unguarded, so a
`ManagedRunRegistryError` propagated to the route seam's generic handler and became a plain 500
`internal_failure`.

Nothing is written when that happens -- the exception fires before any mutation -- so it is not a
data-loss path. The cost is at the client: `frontend/src/lifecycle/appModeCorrelation.ts` treats
only 404, 410 and `run_authority_mismatch` as authority-gone, so a 500 reads as a *retryable*
failure for a run that is in fact resolved, and the browser retries something that can never
succeed.

It is reachable with the timeout the shared test observation already uses (60 s, so a lease of
60 + 300 = 360 s against a 900 s row), and it becomes reachable in the product the moment M26-04
passes a `parent_absolute_deadline` or any caller sends a short timeout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_m23_15_sequence_coordinator import (
    _action,
    _prepare,
    _production_workspace,
    _required_response,
    _submit,
)
from test_m23_51_release_route_contract import _MovableClock, _v2

from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    COORDINATOR_TTL_SECONDS,
    ArtifactInspector,
    ObservedVideoArtifact,
    SequenceCoordinatorError,
    SequenceCoordinatorRegistry,
)
from comfyui_h3_context.adapters.managed_run_registry import ManagedRunRegistryError
from comfyui_h3_context.core.managed_run import ManagedRun, ManagedRunError, ManagedRunTrigger
from comfyui_h3_context.core.managed_run_release import DETACHED_GRACE_SECONDS
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection

#: The shared observation's timeout. A lease of `submitted_at + 60 + 300` is shorter than the
#: coordinator's own row, which is what puts the two records out of order in this direction.
SHARED_TIMEOUT_SECONDS = 60.0

#: How far past the lease the tests move, still inside the coordinator's TTL.
PAST_THE_LEASE = SHARED_TIMEOUT_SECONDS + DETACHED_GRACE_SECONDS + 1.0


def _movable_coordinator(
    tmp_path: Path,
    clock: _MovableClock,
    inspector: ArtifactInspector | None = None,
) -> tuple[SequenceCoordinatorRegistry, ProductionWorkbenchProjection]:
    """The shared coordinator, but on a clock a test can advance."""

    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = SequenceCoordinatorRegistry(
        production_registry=registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=(
            inspector
            if inspector is not None
            else (lambda payload, **expected: ObservedVideoArtifact("mp4", (192, 512, 512, 3)))
        ),
        clock=clock,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    return coordinator, production


def _artifact_payload(handle: str, fingerprint: str) -> dict[str, object]:
    return {
        "run_handle": handle,
        "expected_state_fingerprint": fingerprint,
        "queue_prompt_id": "prompt.model.1",
        "output_node_id": "node.save.video",
        "locator": {"filename": "video.mp4", "subfolder": "", "type": "output"},
    }


def _detached_run_past_its_lease(
    tmp_path: Path,
    inspector: ArtifactInspector | None = None,
    *,
    drive_running: bool = False,
) -> tuple[SequenceCoordinatorRegistry, str, str]:
    """A detached run whose aggregate has expired while its coordinator row is still inside its TTL.

    Returns the coordinator, the run handle, and the state fingerprint a late `record_*` carries.

    `drive_running` moves the run to `running` before the detach, because the coordinator's own
    ordering check refuses a terminal from `submitted` with a 409 long before `_mirror` is reached.
    The lease is dated from the submission either way, so driving the extra transition does not
    lengthen it.
    """

    clock = _MovableClock()
    coordinator, production = _movable_coordinator(tmp_path, clock, inspector)
    prepared = _prepare(coordinator, production)
    latest = _submit(coordinator, prepared)

    if drive_running:
        latest = _required_response(
            coordinator.dispatch(
                _action(
                    "m23_51.late.running.setup",
                    "record_running",
                    {
                        "run_handle": prepared.run_handle,
                        "expected_state_fingerprint": latest.sequence.state.fingerprint,
                        "queue_prompt_id": "prompt.model.1",
                        "host_owner_id": "host.owner.1",
                    },
                )
            )
        )

    _required_response(
        coordinator.dispatch(
            _action(
                "m23_51.late.detach",
                "release_sequence",
                _v2(prepared.run_handle, "detach_client", latest.sequence.state.fingerprint),
            )
        )
    )

    assert PAST_THE_LEASE < COORDINATOR_TTL_SECONDS, "the window this test needs would not exist"
    clock.now += PAST_THE_LEASE
    return coordinator, prepared.run_handle, latest.sequence.state.fingerprint


def test_late_host_evidence_for_an_expired_aggregate_is_gone_not_an_internal_failure(
    tmp_path: Path,
) -> None:
    coordinator, handle, fingerprint = _detached_run_past_its_lease(tmp_path)
    assert handle in coordinator._runs, "the coordinator row must outlive the lease for this case"

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_51.late.running",
                "record_running",
                {
                    "run_handle": handle,
                    "expected_state_fingerprint": fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "host_owner_id": "host.owner.1",
                },
            )
        )
    assert (refused.value.status, refused.value.code) == (410, "run_gone")


def test_the_refusal_writes_nothing(tmp_path: Path) -> None:
    # A guard that answered 410 *after* mutating would be worse than the 500 it replaces: the two
    # records would then disagree, which is the divergent-authority defect M23-31 exists to remove.
    coordinator, handle, fingerprint = _detached_run_past_its_lease(tmp_path)
    before = coordinator._runs[handle].state.fingerprint

    with pytest.raises(SequenceCoordinatorError):
        coordinator.dispatch(
            _action(
                "m23_51.late.nowrite",
                "record_running",
                {
                    "run_handle": handle,
                    "expected_state_fingerprint": fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "host_owner_id": "host.owner.1",
                },
            )
        )
    assert coordinator._runs[handle].state.fingerprint == before


def test_a_late_artifact_answers_the_same_way(tmp_path: Path) -> None:
    coordinator, handle, fingerprint = _detached_run_past_its_lease(tmp_path)

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_51.late.artifact", "record_artifact", _artifact_payload(handle, fingerprint)
            )
        )
    assert (refused.value.status, refused.value.code) == (410, "run_gone")


def test_a_late_terminal_answers_the_same_way(tmp_path: Path) -> None:
    # Driven from `running`, because the coordinator refuses a terminal from `submitted` with its
    # own 409 before `_mirror` is ever reached. The point of this case is that the *third* late
    # event class -- the one that decides whether the aggregate ever reaches a terminal at all --
    # is covered too, not only the two that happen to be easy to reach.
    coordinator, handle, fingerprint = _detached_run_past_its_lease(tmp_path, drive_running=True)

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_51.late.terminal",
                "record_terminal",
                {
                    "run_handle": handle,
                    "expected_state_fingerprint": fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "error",
                },
            )
        )
    assert (refused.value.status, refused.value.code) == (410, "run_gone")


def test_the_artifact_inspector_is_never_reached(tmp_path: Path) -> None:
    # The 410 must be decided before any work is done on the host's payload: a run whose authority
    # is gone should not cause the coordinator to open, inspect or copy a file.
    inspected: list[object] = []

    def _inspector(payload: object, **expected: object) -> ObservedVideoArtifact:
        inspected.append(payload)
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator, handle, fingerprint = _detached_run_past_its_lease(tmp_path, _inspector)

    with pytest.raises(SequenceCoordinatorError):
        coordinator.dispatch(
            _action(
                "m23_51.late.inspect",
                "record_artifact",
                _artifact_payload(handle, fingerprint),
            )
        )
    assert inspected == []


def _live_submitted_run(tmp_path: Path) -> tuple[SequenceCoordinatorRegistry, str, str]:
    """An ordinary submitted run, still live in both records."""

    coordinator, production = _movable_coordinator(tmp_path, _MovableClock())
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    return coordinator, prepared.run_handle, submitted.sequence.state.fingerprint


def _running_action(handle: str, fingerprint: str, request: str) -> dict[str, object]:
    return _action(
        request,
        "record_running",
        {
            "run_handle": handle,
            "expected_state_fingerprint": fingerprint,
            "queue_prompt_id": "prompt.model.1",
            "host_owner_id": "host.owner.1",
        },
    )


def _vanish_on_advance(coordinator: SequenceCoordinatorRegistry, handle: str) -> None:
    """Really remove the row inside the first `advance`, reproducing a concurrent prune.

    The window is not hypothetical: the production registry's liveness probe reaches the managed-run
    table while holding only its own lock, so the row can go between `_mirror`'s read and its write.
    Both have to be guarded, and only a double can put a test in that window deterministically.
    """

    real = coordinator._managed.advance
    fired = False

    def _advance(run_handle: str, trigger: ManagedRunTrigger, **fields: object) -> ManagedRun:
        nonlocal fired
        if not fired:
            fired = True
            coordinator._managed.release(run_handle)
        return real(run_handle, trigger, **fields)

    coordinator._managed.advance = _advance  # type: ignore[method-assign]


def test_a_run_pruned_between_the_read_and_the_advance_is_gone_not_an_internal_failure(
    tmp_path: Path,
) -> None:
    coordinator, handle, fingerprint = _live_submitted_run(tmp_path)
    _vanish_on_advance(coordinator, handle)

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(_running_action(handle, fingerprint, "m23_51.late.race"))
    assert (refused.value.status, refused.value.code) == (410, "run_gone")


def test_the_mid_mirror_refusal_writes_nothing_either(tmp_path: Path) -> None:
    # CRITICAL: the guard on the *write* must raise before the coordinator entry is updated. A 410
    # answered after `entry.state = state` would leave the coordinator ahead of an aggregate that
    # is gone, which is worse than the 500 it replaces -- the two records would then disagree with
    # nothing left to reconcile against.
    coordinator, handle, fingerprint = _live_submitted_run(tmp_path)
    before = coordinator._runs[handle].state.fingerprint
    _vanish_on_advance(coordinator, handle)

    with pytest.raises(SequenceCoordinatorError):
        coordinator.dispatch(_running_action(handle, fingerprint, "m23_51.late.race.nowrite"))
    assert coordinator._runs[handle].state.fingerprint == before


def test_a_disagreement_raised_by_the_read_still_raises_loudly(tmp_path: Path) -> None:
    # The read side of the same discrimination as the advance-side case below. A guard written as
    # `except ManagedRunError` catches both subclasses, so both sides need a case or the tidier,
    # wronger version of the guard passes.
    coordinator, handle, fingerprint = _live_submitted_run(tmp_path)

    def _disagree(run_handle: str) -> ManagedRun:
        raise ManagedRunError("the two records disagree about this run")

    coordinator._managed.read = _disagree  # type: ignore[method-assign]
    with pytest.raises(ManagedRunError) as raised:
        coordinator.dispatch(_running_action(handle, fingerprint, "m23_51.late.read.illegal"))
    assert not isinstance(raised.value, ManagedRunRegistryError)
    assert not isinstance(raised.value, SequenceCoordinatorError)


def test_a_disagreement_between_the_two_records_still_raises_loudly(tmp_path: Path) -> None:
    # CRITICAL: `ManagedRunRegistryError` subclasses `ManagedRunError`, so a guard written as
    # `except ManagedRunError` would swallow a genuine disagreement between the two records -- an
    # illegal transition -- and answer a tidy 410 for it. That is exactly what `_mirror`'s existing
    # CRITICAL comment says must stay a loud failure, because the two records agreeing is an
    # asserted invariant rather than a hope. Only "the row is gone" may be translated.
    clock = _MovableClock()
    coordinator, production = _movable_coordinator(tmp_path, clock)
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)

    real_advance = coordinator._managed.advance

    def _disagree(run_handle: str, trigger: ManagedRunTrigger, **fields: object) -> ManagedRun:
        raise ManagedRunError("illegal transition for the aggregate's state")

    coordinator._managed.advance = _disagree  # type: ignore[method-assign]
    try:
        with pytest.raises(ManagedRunError) as raised:
            coordinator.dispatch(
                _action(
                    "m23_51.late.illegal",
                    "record_running",
                    {
                        "run_handle": prepared.run_handle,
                        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                        "queue_prompt_id": "prompt.model.1",
                        "host_owner_id": "host.owner.1",
                    },
                )
            )
    finally:
        coordinator._managed.advance = real_advance  # type: ignore[method-assign]
    assert not isinstance(raised.value, ManagedRunRegistryError)
    assert not isinstance(raised.value, SequenceCoordinatorError)
