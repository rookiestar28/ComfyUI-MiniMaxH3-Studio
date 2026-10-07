"""M23-31: the cancel question is decided once, over one authority.

The follow-up routed here from the M23-23 review. `_cancel` decided the same question twice with
two different predicates: the recorded transition tested the runtime's job state, while the
disposition returned to the client tested whether `entry.queue_prompt_id` was set. The two are not
equivalent, so the response could contradict the state it accompanied.

The divergence is reachable, not theoretical. A run that has been submitted holds a prompt id for
the rest of its life; once it terminalizes, its runtime leaves SUBMITTED/RUNNING. Cancelling then
took the `cancel_generation_sequence` branch -- recorded `cancelled` -- while the prompt id was
still set, so the client was told `unknown_ownership`.

The helpers come from the M23-15 suite deliberately: this asserts a behaviour change in the same
system those tests cover, so it must be driven through the same public dispatch surface rather than
through a rebuilt fixture that could drift from it.
"""

from __future__ import annotations

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

from comfyui_h3_context.adapters.comfyui_sequence_coordinator import HOST_MAY_HOLD_JOB_STATES
from comfyui_h3_context.core.managed_run import HOST_MAY_HOLD_THE_PROMPT

#: Imported, never restated. An earlier version of this module kept its own copy of
#: `{SUBMITTED, RUNNING}`, which made three textual definitions of one rule -- the defect the
#: module itself is about, reproduced in the test that checks for it.
_HOST_MAY_HOLD = HOST_MAY_HOLD_JOB_STATES


def _cancel(coordinator: object, run_handle: str, fingerprint: str, request_id: str) -> object:
    return _required_response(
        coordinator.dispatch(  # type: ignore[attr-defined]
            _action(
                request_id,
                "cancel_sequence",
                {"run_handle": run_handle, "expected_state_fingerprint": fingerprint},
            )
        )
    )


def test_a_cancel_after_a_terminal_reports_what_it_recorded(tmp_path: Path) -> None:
    # The reachable divergence, end to end. Before the fix this returned `unknown_ownership`
    # because a prompt id was still recorded, while the transition it had just applied was
    # `cancel_generation_sequence`.
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)

    terminal = _required_response(
        coordinator.dispatch(
            _action(
                "m23_31.terminal.1",
                "record_terminal",
                {
                    "run_handle": prepared.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.1",
                    "kind": "error",
                },
            )
        )
    )
    runtime_state = terminal.sequence.state.runtimes[0].state
    assert runtime_state not in _HOST_MAY_HOLD

    cancelled = _cancel(
        coordinator,
        prepared.run_handle,
        terminal.sequence.state.fingerprint,
        "m23_31.cancel.1",
    )
    assert cancelled.disposition == "cancelled"  # type: ignore[attr-defined]
    assert cancelled.sequence.state.runtimes[0].state not in _HOST_MAY_HOLD  # type: ignore[attr-defined]


def test_a_cancel_while_the_host_may_hold_the_prompt_reports_unknown_ownership(
    tmp_path: Path,
) -> None:
    # The other side of the one predicate. A submitted run has not been observed to stop, so the
    # honest answer is that ownership is unknown -- and the recorded transition says the same.
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    assert submitted.sequence.state.runtimes[0].state in _HOST_MAY_HOLD

    cancelled = _cancel(
        coordinator,
        prepared.run_handle,
        submitted.sequence.state.fingerprint,
        "m23_31.cancel.2",
    )
    assert cancelled.disposition == "unknown_ownership"  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", ["error", "interrupted"])
def test_the_disposition_and_the_recorded_state_never_disagree(tmp_path: Path, kind: str) -> None:
    # Stated as the invariant rather than as two cases: whatever terminal a run reaches, the
    # disposition a cancel reports is derived from the same value the transition used.
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    terminal = _required_response(
        coordinator.dispatch(
            _action(
                f"m23_31.terminal.{kind}",
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
    before = terminal.sequence.state.runtimes[0].state
    cancelled = _cancel(
        coordinator,
        prepared.run_handle,
        terminal.sequence.state.fingerprint,
        f"m23_31.cancel.{kind}",
    )
    expected = "unknown_ownership" if before in _HOST_MAY_HOLD else "cancelled"
    assert cancelled.disposition == expected  # type: ignore[attr-defined]


def test_the_job_state_predicate_implies_the_aggregate_one() -> None:
    # The relationship between the two vocabularies, stated as the thing that is actually true.
    # They are not the same set and cannot be: `_CONSISTENT` is many-to-many, job `running` also
    # covers aggregate `artifact_recorded`, and job `output_verification_failed` covers aggregate
    # states that *are* in `HOST_MAY_HOLD_THE_PROMPT` while itself being a state in which the host
    # provably already ran the prompt. So the implication runs one way only, and pinning it is what
    # would catch a future edit to either set that broke the direction that does hold.
    from test_m23_31_coordinator_projection import _CONSISTENT

    for job, aggregates in _CONSISTENT.items():
        if job not in HOST_MAY_HOLD_JOB_STATES:
            continue
        for aggregate in aggregates:
            assert aggregate in HOST_MAY_HOLD_THE_PROMPT, (
                f"job state {job.value} says the host may hold the prompt, but the consistent "
                f"aggregate state {aggregate.value} says it may not"
            )


def test_the_converse_fails_for_exactly_one_job_state() -> None:
    # Recorded as data rather than left as a surprise. If a future change makes the converse hold
    # everywhere, or breaks it somewhere new, this fails and the asymmetry gets re-examined on
    # purpose instead of being discovered by a disposition that contradicts an aggregate terminal.
    from test_m23_31_coordinator_projection import _CONSISTENT

    asymmetric = {
        job.value
        for job, aggregates in _CONSISTENT.items()
        if job not in HOST_MAY_HOLD_JOB_STATES
        and aggregates
        and all(state in HOST_MAY_HOLD_THE_PROMPT for state in aggregates)
    }
    assert asymmetric == {"output_verification_failed"}
