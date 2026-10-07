"""M23-31: the coordinator's job state and the ManagedRun aggregate never disagree.

The aggregate is a second record of the same run, and a second record is only safe when something
checks that it agrees with the first. Without this module the mirror inside `_publish` would be
exactly the divergent-authority defect M23-31 exists to remove -- the `_cancel` bug one layer up.

The correspondence below is the whole claim. It is written as data rather than as branches so that
a new job state has to be classified here before it can be published at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from test_m23_15_sequence_coordinator import (
    _action,
    _coordinator,
    _prepare,
    _production_workspace,
    _required_response,
    _submit,
)

from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    ObservedVideoArtifact,
    SequenceCoordinatorError,
    SequenceCoordinatorRegistry,
    SequenceCoordinatorResponse,
    lifecycle_triggers,
)
from comfyui_h3_context.core.generation_sequence import GenerationJobState
from comfyui_h3_context.core.managed_run import ManagedRunState, ManagedRunTrigger
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection

#: This module's file name, matched against pytest node ids (`tests/<name>.py::<test>`), so the
#: guard below can tell a whole-module run from a filtered one.
MODULE_PATH = Path(__file__).name
#: How many tests in this module actually executed, so the end-of-module guard can tell a whole run
#: from a filtered one.
_COLLECTED: dict[str, int] = {"count": 0}

J = GenerationJobState
S = ManagedRunState
T = ManagedRunTrigger

#: Job state -> the aggregate states that are consistent with it. Several job states admit two,
#: because recording an artifact is a lifecycle step the job state does not express.
_CONSISTENT: dict[GenerationJobState, set[ManagedRunState]] = {
    J.PLANNED: {S.SEQUENCE_PREPARED},
    J.PROJECTED: {S.SEQUENCE_PREPARED},
    J.SUBMITTED: {S.SUBMITTED},
    J.RUNNING: {S.RUNNING, S.ARTIFACT_RECORDED},
    J.OUTPUT_VERIFICATION_FAILED: {S.RUNNING, S.ARTIFACT_RECORDED},
    J.SUCCEEDED: {S.TERMINAL_SUCCEEDED},
    J.FAILED: {S.TERMINAL_FAILED},
    J.TIMED_OUT: {S.TERMINAL_FAILED},
    J.CANCELLED: {S.TERMINAL_CANCELLED, S.TERMINAL_FAILED, S.TERMINAL_SUCCEEDED},
    J.UNKNOWN_OWNERSHIP: {S.TERMINAL_UNKNOWN_OWNERSHIP},
}


#: Job states this module's tests have actually reached, recorded as they run. Read by the
#: end-of-module guard below.
_OBSERVED: set[GenerationJobState] = set()


def _assert_agrees(
    coordinator: SequenceCoordinatorRegistry, run_handle: str, job: GenerationJobState
) -> None:
    aggregate = coordinator._managed.read(run_handle).state
    assert job in _CONSISTENT, f"job state {job} has no recorded correspondence"
    assert aggregate in _CONSISTENT[job], f"aggregate {aggregate} disagrees with job state {job}"
    _OBSERVED.add(job)


def _run(
    tmp_path: Path,
) -> tuple[SequenceCoordinatorRegistry, SequenceCoordinatorResponse, ProductionWorkbenchProjection]:
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    return coordinator, prepared, production


def test_every_job_state_this_repository_declares_has_a_recorded_correspondence() -> None:
    # A new `GenerationJobState` must be classified here before the mirror can publish it. Leaving
    # one out would let the aggregate drift into a state nothing checks.
    assert set(_CONSISTENT) == set(GenerationJobState)


def test_the_aggregate_agrees_at_every_step_of_a_submitted_run(tmp_path: Path) -> None:
    coordinator, prepared, _ = _run(tmp_path)
    _assert_agrees(coordinator, prepared.run_handle, prepared.sequence.state.runtimes[0].state)
    submitted = _submit(coordinator, prepared)
    _assert_agrees(coordinator, prepared.run_handle, submitted.sequence.state.runtimes[0].state)


@pytest.mark.parametrize("kind", ["error", "interrupted"])
def test_the_aggregate_agrees_through_a_terminal_and_a_later_cancel(
    tmp_path: Path, kind: str
) -> None:
    # The coordinator collapses SUBMITTED -> RUNNING -> terminal into one publish, and then still
    # re-marks the sequence on a later cancel. Both are places the two records could drift apart.
    coordinator, prepared, _ = _run(tmp_path)
    submitted = _submit(coordinator, prepared)
    terminal = _required_response(
        coordinator.dispatch(
            _action(
                f"m23_31.projection.terminal.{kind}",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": kind,
                },
            )
        )
    )
    _assert_agrees(coordinator, prepared.run_handle, terminal.sequence.state.runtimes[0].state)

    cancelled = _required_response(
        coordinator.dispatch(
            _action(
                f"m23_31.projection.cancel.{kind}",
                "cancel_sequence",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": terminal.sequence.state.fingerprint,
                },
            )
        )
    )
    _assert_agrees(coordinator, prepared.run_handle, cancelled.sequence.state.runtimes[0].state)


def test_the_aggregate_records_the_two_joins_prepare_made(tmp_path: Path) -> None:
    # The M23-19 class. The production workspace and the prepared sequence are recorded together,
    # at the moment the join is made, from the values `_prepare` already holds -- not reconstructed
    # later from a second source.
    coordinator, prepared, production = _run(tmp_path)
    run = coordinator._managed.read(prepared.run_handle)
    assert run.state is S.SEQUENCE_PREPARED
    assert run.production_workspace == production.workspace_handle
    assert run.prepared_sequence == prepared.run_handle
    assert run.context_revision == production.workspace_revision
    assert run.segment_count == 1


def test_a_collapsed_submitted_to_terminal_publish_reconstructs_the_running_step() -> None:
    assert lifecycle_triggers(J.SUBMITTED, J.FAILED, S.SUBMITTED, artifact_recorded=False) == (
        T.RECORD_RUNNING,
        T.RECORD_FAILED,
    )


def test_an_artifact_is_recorded_after_running_never_before(tmp_path: Path) -> None:
    # Ordering matters: `record_artifact` leaves `running`, so emitting it first would refuse.
    assert lifecycle_triggers(J.SUBMITTED, J.RUNNING, S.SUBMITTED, artifact_recorded=True) == (
        T.RECORD_RUNNING,
        T.RECORD_ARTIFACT,
    )


def test_a_run_that_has_already_ended_takes_no_further_transition() -> None:
    # The coordinator re-marks a terminal run's sequence on a later cancel. That is a re-marking,
    # not a second ending, and replaying it would move the aggregate to a state the run is not in.
    for ended in (
        S.TERMINAL_SUCCEEDED,
        S.TERMINAL_FAILED,
        S.TERMINAL_CANCELLED,
        S.TERMINAL_UNKNOWN_OWNERSHIP,
        S.EXPIRED,
    ):
        assert lifecycle_triggers(J.FAILED, J.CANCELLED, ended, artifact_recorded=False) == ()


def test_an_unchanged_job_state_with_no_artifact_is_no_transition() -> None:
    assert lifecycle_triggers(J.RUNNING, J.RUNNING, S.RUNNING, artifact_recorded=False) == ()


def test_output_verification_failure_is_not_a_lifecycle_transition() -> None:
    # The run is still running. Inventing a transition for it would put the aggregate in a state
    # the coordinator is not in, which is the drift this whole module exists to catch.
    assert (
        lifecycle_triggers(
            J.RUNNING, J.OUTPUT_VERIFICATION_FAILED, S.RUNNING, artifact_recorded=False
        )
        == ()
    )


#: The `_CONSISTENT` rows this module actually drives through the real dispatch surface, as opposed
#: to the rows it only declares. CRITICAL: keep this honest, and "honest" means a test in this
#: module dispatches a coordinator action and reaches that job state -- not that the mapping looks
#: right. The map's *keys* are asserted complete by
#: `test_every_job_state_this_repository_declares_has_a_recorded_correspondence`, but a key with an
#: unexercised value is a classification nobody has ever checked, and one of them was wrong:
#: `output_verification_failed` was declared consistent with `{running, artifact_recorded}` while
#: the aggregate actually sat at `submitted`, because the mirror emitted no trigger for it.
#:
#: `running` was then listed here on the same faith -- no test in this repository had ever
#: dispatched `record_running` -- and so was `succeeded`, whose job path the M23-15 suite covers
#: eight times over while checking nothing about the aggregate. The acceptance review caught both.
#: Adding a row without a test in this module that reaches it is how all three return.
_EXERCISED_ROWS = frozenset(
    {
        J.PLANNED,
        J.SUBMITTED,
        J.RUNNING,
        J.OUTPUT_VERIFICATION_FAILED,
        J.SUCCEEDED,
        J.FAILED,
        J.CANCELLED,
    }
)

#: Declared but not driven here, each for a stated reason rather than by omission.
_UNEXERCISED_ROWS = {
    J.PROJECTED: "a transient planning state the managed dispatch surface never publishes alone",
    J.TIMED_OUT: "produced by host timeout accounting, not by any coordinator action under test",
    J.UNKNOWN_OWNERSHIP: "covered end to end by tests/test_m23_31_cancel_predicate.py instead",
}


def test_the_exercised_and_unexercised_rows_together_are_the_whole_map() -> None:
    assert _EXERCISED_ROWS.isdisjoint(_UNEXERCISED_ROWS)
    assert _EXERCISED_ROWS | set(_UNEXERCISED_ROWS) == set(_CONSISTENT)


def _run_was_filtered(request: pytest.FixtureRequest) -> bool:
    """Whether this session ran a subset rather than the whole module."""

    config = request.config
    if config.getoption("keyword", default="") or config.getoption("markexpr", default=""):
        return True
    if any("::" in str(argument) for argument in config.args):
        return True
    return bool(request.session.testsfailed)


@pytest.fixture(scope="module", autouse=True)
def _every_exercised_row_was_really_reached(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the module if a row claims to be exercised and no test actually reached it.

    CRITICAL: this exists because the same defect appeared three times inside this one table --
    `output_verification_failed` was declared with the wrong value, and `running` and `succeeded`
    were declared exercised while nothing drove them. Two of the three were caught by a reviewer
    reading the file, which is not a mechanism. `_assert_agrees` records what it sees and this
    compares it against the claim.

    It is skipped when the module was filtered, because a partial run legitimately reaches fewer
    rows and a guard that cries wolf then would be disabled rather than fixed. CRITICAL: filtering
    cannot be detected by comparing `request.session.items` against the number of tests that ran --
    `-k` removes deselected items from that list too, so the two always match and the guard silently
    becomes vacuous. The first version of this fixture did exactly that and passed its own mutation.
    """

    yield
    if _run_was_filtered(request) or _COLLECTED["count"] == 0:
        return
    missing = {job.value for job in _EXERCISED_ROWS - _OBSERVED}
    assert not missing, (
        f"rows claimed exercised that no test reached: {sorted(missing)}; "
        "either drive them or move them to _UNEXERCISED_ROWS with a stated reason"
    )


@pytest.fixture(autouse=True)
def _count_executed_tests() -> Iterator[None]:
    _COLLECTED["count"] += 1
    yield


def _failing_coordinator(
    tmp_path: Path,
) -> tuple[SequenceCoordinatorRegistry, SequenceCoordinatorResponse, Path]:
    """A coordinator whose artifact inspector always refuses, so a run can reach the OVF row."""

    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"synthetic-video-payload")

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        raise SequenceCoordinatorError("artifact_shape_mismatch", 422)

    coordinator = SequenceCoordinatorRegistry(
        production_registry=registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=inspect,
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    return coordinator, _prepare(coordinator, production), artifact


def test_the_aggregate_agrees_when_artifact_verification_fails(tmp_path: Path) -> None:
    # The regression. `_artifact` advances a SUBMITTED runtime to RUNNING before doing its own
    # work, so by the time verification fails the host has demonstrably begun executing. The mirror
    # used to emit nothing for this arrival, leaving the aggregate at `submitted` while the
    # coordinator reported `output_verification_failed` -- a pairing this module's own map declares
    # inconsistent, and which no test drove.
    coordinator, prepared, artifact = _failing_coordinator(tmp_path)
    submitted = _submit(coordinator, prepared)

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_31.projection.artifact.fail",
                "record_artifact",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "output_node_id": "node.save.video",
                    "locator": {"filename": artifact.name, "subfolder": "", "type": "output"},
                },
            )
        )
    assert refused.value.code == "artifact_shape_mismatch"

    current = _required_response(
        coordinator.dispatch(
            _action(
                "m23_31.projection.artifact.read",
                "read_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    job = current.sequence.progress[0].state
    assert job is J.OUTPUT_VERIFICATION_FAILED
    assert coordinator._managed.read(prepared.run_handle).state is S.RUNNING
    _assert_agrees(coordinator, prepared.run_handle, job)


def test_the_aggregate_agrees_after_a_dispatched_record_running(tmp_path: Path) -> None:
    # The `running` row, driven rather than declared. The re-review found that no test anywhere in
    # this repository dispatched the coordinator's `record_running` action -- not this module, not
    # the 51 M23-15 tests -- so the row sat in `_EXERCISED_ROWS` on the strength of a mapping
    # nobody had run. That is the same shape as the `output_verification_failed` defect this round
    # fixed, in the very table built to stop it. This drives the action for real.
    coordinator, prepared, _ = _run(tmp_path)
    submitted = _submit(coordinator, prepared)
    assert coordinator._managed.read(prepared.run_handle).state is S.SUBMITTED

    running = _required_response(
        coordinator.dispatch(
            _action(
                "m23_31.projection.running",
                "record_running",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "host_owner_id": "host.owner.1",
                },
            )
        )
    )
    assert running.disposition == "running"
    job = running.sequence.state.runtimes[0].state
    assert job is J.RUNNING
    assert coordinator._managed.read(prepared.run_handle).state is S.RUNNING
    _assert_agrees(coordinator, prepared.run_handle, job)


def test_the_aggregate_agrees_at_a_successful_terminal(tmp_path: Path) -> None:
    # The `succeeded` row, driven rather than declared. The re-review found it listed as exercised
    # on the same faith `running` had been: `record_terminal(kind="success")` is dispatched eight
    # times across the M23-15 suite, so the job path is well covered, but those tests predate the
    # aggregate and check nothing about it -- so the correspondence itself had never been driven.
    #
    # The success path is two dispatches, not one: the terminal is recorded first and the artifact
    # completes it, which is also why the parametrized terminal test above covers only the kinds
    # that end a run on their own.
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    artifact = output_root / "managed.mp4"
    artifact.write_bytes(b"synthetic-video-payload")
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)

    terminal = _required_response(
        coordinator.dispatch(
            _action(
                "m23_31.projection.success.terminal",
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
                "m23_31.projection.success.artifact",
                "record_artifact",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": terminal.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "output_node_id": "node.save.video",
                    "locator": {"filename": artifact.name, "subfolder": "", "type": "output"},
                },
            )
        )
    )
    assert completed.disposition == "succeeded"
    job = completed.sequence.state.runtimes[0].state
    assert job is J.SUCCEEDED
    assert coordinator._managed.read(prepared.run_handle).state is S.TERMINAL_SUCCEEDED
    _assert_agrees(coordinator, prepared.run_handle, job)


def test_the_aggregate_agrees_when_a_prepared_run_is_cancelled(tmp_path: Path) -> None:
    # The `cancelled` row, driven rather than declared -- the fourth false claim in this table, and
    # the first one a mechanism caught rather than a reader. The terminal-then-cancel test above was
    # assumed to reach it; it does not. Cancelling a run whose runtime already ended leaves that
    # runtime's job state where it was (`failed`, `interrupted`), because the cancel marks the
    # sequence rather than the finished job. The only arrival at job `cancelled` is a cancel of a
    # run the host never received, which is also the path the frontend's preparation rollback takes.
    coordinator, prepared, _ = _run(tmp_path)
    assert coordinator._managed.read(prepared.run_handle).state is S.SEQUENCE_PREPARED

    cancelled = _required_response(
        coordinator.dispatch(
            _action(
                "m23_31.projection.cancel.prepared",
                "cancel_sequence",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": prepared.sequence.state.fingerprint,
                },
            )
        )
    )
    assert cancelled.disposition == "cancelled"
    job = cancelled.sequence.state.runtimes[0].state
    assert job is J.CANCELLED
    assert coordinator._managed.read(prepared.run_handle).state is S.TERMINAL_CANCELLED
    _assert_agrees(coordinator, prepared.run_handle, job)


def test_a_verification_failure_arrival_reconstructs_the_running_step() -> None:
    # The unit form of the same rule, so the correction is pinned independently of the fixture.
    assert lifecycle_triggers(
        J.SUBMITTED, J.OUTPUT_VERIFICATION_FAILED, S.SUBMITTED, artifact_recorded=False
    ) == (T.RECORD_RUNNING,)
