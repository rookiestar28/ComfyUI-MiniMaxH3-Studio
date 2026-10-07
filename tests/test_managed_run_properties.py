"""M23-31 property tests for the ManagedRun aggregate and registry.

Two layers, because they cover different things and neither subsumes the other.

The **exhaustive model check** walks the whole reachable state space of the machine and fires every
trigger from every reachable state. The machine has twelve states, so this is complete rather than
sampled: it proves there is no reachable state from which some trigger does something other than
advance to a declared state or raise one of the three typed refusals. A random search cannot make
that claim and does not need to.

The **stateful property test** covers what exhaustion cannot: the registry's bounds, its clock and
its interleavings, driven by Hypothesis over random action sequences. Its budget is pinned and its
profile is derandomized, so a Full Gate result is reproducible rather than a fresh random search
each run. A property that only fails under a wider search is a finding to file, not a reason to
widen the gate's budget.
"""

from __future__ import annotations

import unittest

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from comfyui_h3_context.adapters.managed_run_registry import (
    MANAGED_RUN_TTL_SECONDS,
    ManagedRunRegistry,
    ManagedRunRegistryError,
)
from comfyui_h3_context.core.managed_run import (
    HOST_MAY_HOLD_THE_PROMPT,
    MANAGED_RUN_MACHINE,
    MAX_LIVE_MANAGED_RUNS,
    MAX_TRANSITION_EVENTS,
    GuardRejectedError,
    InvalidTriggerError,
    ManagedRun,
    ManagedRunState,
    ManagedRunTrigger,
    StateMachineError,
    UnknownTriggerError,
    advance,
    cancel_trigger,
)

T = ManagedRunTrigger
S = ManagedRunState

#: Pinned gate budget. Recorded here rather than left to a profile file so the number a Full Gate
#: ran with is visible next to the properties it exercised.
MAX_EXAMPLES = 50
STATEFUL_STEP_COUNT = 24


def _reachable() -> set[str]:
    """Every state reachable from the initial state, by breadth-first closure over transitions."""

    seen = {MANAGED_RUN_MACHINE.initial}
    changed = True
    while changed:
        changed = False
        for transition in MANAGED_RUN_MACHINE.transitions:
            if transition.target not in seen and seen & set(transition.source):
                seen.add(transition.target)
                changed = True
    return seen


class ExhaustiveModelCheckTests(unittest.TestCase):
    """Complete rather than sampled: the machine is small enough to enumerate."""

    def test_every_trigger_from_every_reachable_state_has_a_typed_outcome(self) -> None:
        # The property that matters is that there is no third kind of outcome: a transition either
        # advances to a declared state or refuses with one of the three typed errors. Anything
        # else -- an IndexError, a KeyError, a silent no-op -- is a hole in the table.
        states = sorted(_reachable())
        self.assertEqual(set(states), set(MANAGED_RUN_MACHINE.states))
        for state in states:
            for trigger in T:
                with self.subTest(state=state, trigger=trigger.value):
                    run = ManagedRun(run_handle="run_1", state=S(state), segment_count=1)
                    try:
                        result = advance(run, trigger)
                    except (UnknownTriggerError, InvalidTriggerError, GuardRejectedError):
                        continue
                    self.assertIn(result.state.value, MANAGED_RUN_MACHINE.states)
                    self.assertEqual(len(result.events), len(run.events) + 1)

    def test_no_reachable_state_can_cancel_into_the_wrong_terminal(self) -> None:
        # The routed follow-up, checked exhaustively rather than on two examples: for every state
        # a cancel is accepted from, the terminal it reaches is the one `HOST_MAY_HOLD_THE_PROMPT`
        # predicts, and there is no state where the two disagree.
        for state in sorted(_reachable()):
            run = ManagedRun(run_handle="run_1", state=S(state), segment_count=1)
            try:
                result = advance(run, cancel_trigger(run.state))
            except (InvalidTriggerError, GuardRejectedError):
                continue
            with self.subTest(state=state):
                expected = (
                    S.TERMINAL_UNKNOWN_OWNERSHIP
                    if run.state in HOST_MAY_HOLD_THE_PROMPT
                    else S.TERMINAL_CANCELLED
                )
                self.assertEqual(result.state, expected)

    def test_a_sequence_never_exists_without_the_single_segment_guard_having_passed(self) -> None:
        # The other routed follow-up. Reaching `sequence_prepared` from any reachable state is
        # possible only through the guarded transition, so no path exists that would leave
        # `runtimes[0]` unsafe.
        entries = [
            transition
            for transition in MANAGED_RUN_MACHINE.transitions
            if transition.target == S.SEQUENCE_PREPARED.value
        ]
        self.assertEqual(len(entries), 1)
        self.assertIsNotNone(entries[0].guard)

    def test_every_terminal_state_leads_only_to_expired(self) -> None:
        terminals = {
            S.TERMINAL_SUCCEEDED.value,
            S.TERMINAL_FAILED.value,
            S.TERMINAL_CANCELLED.value,
            S.TERMINAL_UNKNOWN_OWNERSHIP.value,
        }
        for transition in MANAGED_RUN_MACHINE.transitions:
            for state in transition.source:
                if state in terminals:
                    with self.subTest(source=state, trigger=transition.trigger):
                        self.assertEqual(transition.target, S.EXPIRED.value)

    def test_expired_is_absorbing(self) -> None:
        for transition in MANAGED_RUN_MACHINE.transitions:
            with self.subTest(trigger=transition.trigger):
                self.assertNotIn(S.EXPIRED.value, transition.source)


class ManagedRunModel(RuleBasedStateMachine):
    """Random action sequences against the registry, with the three invariants as assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 1_000.0
        self.registry = ManagedRunRegistry(clock=lambda: self.now)
        self.handles: list[str] = []
        self.counter = 0

    def _any(self) -> str | None:
        return self.handles[-1] if self.handles else None

    @rule()
    def create(self) -> None:
        self.counter += 1
        handle = f"run_{self.counter}"
        try:
            self.registry.create(handle)
        except ManagedRunRegistryError:
            return
        self.handles.append(handle)

    @rule(trigger=st.sampled_from(sorted(T, key=lambda item: item.value)))
    def fire(self, trigger: ManagedRunTrigger) -> None:
        handle = self._any()
        if handle is None:
            return
        fields: dict[str, object] = {}
        if trigger is T.STAGE_CONTEXT:
            fields["context_revision"] = self.counter
        elif trigger is T.CREATE_PRODUCTION:
            fields["production_workspace"] = "ws_1"
            fields["segment_count"] = 1
        elif trigger is T.PREPARE_SEQUENCE:
            fields["prepared_sequence"] = "seq_1"
        elif trigger is T.RECORD_SUBMISSION:
            fields["prompt_id"] = "prompt_1"
        elif trigger is T.RECORD_ARTIFACT:
            fields["artifact_receipt"] = "artifact_1"
        try:
            self.registry.advance(handle, trigger, **fields)
        except StateMachineError:
            return

    @rule()
    def cancel(self) -> None:
        handle = self._any()
        if handle is None:
            return
        try:
            self.registry.cancel(handle)
        except StateMachineError:
            return

    @rule()
    def release(self) -> None:
        handle = self._any()
        if handle is None:
            return
        try:
            self.registry.release(handle)
        except ManagedRunRegistryError:
            return
        self.handles.remove(handle)

    @rule(seconds=st.sampled_from([1.0, 60.0, MANAGED_RUN_TTL_SECONDS + 1.0]))
    def advance_clock(self, seconds: float) -> None:
        self.now += seconds

    @invariant()
    def live_runs_stay_within_their_bound(self) -> None:
        assert len(self.registry.live_handles()) <= MAX_LIVE_MANAGED_RUNS

    @invariant()
    def production_is_never_released_under_a_live_sequence(self) -> None:
        # One direction of "an accepted sequence is never pruned while its run is live": a run that
        # reached `expired` through `release_production` must not have held a prepared sequence.
        #
        # CRITICAL: this covers the production-workspace direction only, and stating that plainly is
        # the point. The other direction -- releasing a *sequence* whose host job may still be
        # running -- is not enforced anywhere: the coordinator's `release_sequence` is unconditional
        # and `ManagedRunRegistry.release` is a bare pop that fires no transition, so the `release`
        # rule below never reaches `expired` and this invariant never sees it. That gap is recorded
        # in `core.managed_run.MODEL_ONLY_TRIGGERS` and registered as roadmap work; do not read this
        # invariant as covering it.
        for handle in list(self.handles):
            try:
                run = self.registry.read(handle)
            except ManagedRunRegistryError:
                continue
            if run.state is S.EXPIRED and run.prepared_sequence is not None:
                released = [
                    event for event in run.events if event.trigger == T.RELEASE_PRODUCTION.value
                ]
                assert not released, "production was released while a sequence depended on it"

    @invariant()
    def evidence_stays_bounded_and_ordered(self) -> None:
        for handle in list(self.handles):
            try:
                run = self.registry.read(handle)
            except ManagedRunRegistryError:
                continue
            assert len(run.events) <= MAX_TRANSITION_EVENTS
            sequences = [event.sequence for event in run.events]
            assert sequences == sorted(sequences)
            assert run.state.value in MANAGED_RUN_MACHINE.states


ManagedRunStatefulTest = ManagedRunModel.TestCase
ManagedRunStatefulTest.settings = settings(
    max_examples=MAX_EXAMPLES,
    stateful_step_count=STATEFUL_STEP_COUNT,
    # CRITICAL: derandomized with no example database. A Full Gate must produce the same result
    # for the same tree; a fresh random search each run would make a red gate unreproducible and
    # a green one meaningless as acceptance evidence. Widen the search deliberately in a separate
    # investigation, never by loosening the gate.
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)


if __name__ == "__main__":
    unittest.main()
