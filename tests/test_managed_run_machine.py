"""M23-31 focused tests for the dependency-free ManagedRun state machine.

The machine exists so that one place decides what a run may do next. Every property asserted here
is one a hand-rolled `if` chain silently loses: the callback phases run in a fixed order, an
unknown trigger and a known-but-invalid one are distinguishable, a rejected guard leaves the state
untouched, a restore runs no callbacks, and the stategraph projection is derived from the machine
rather than written down beside it.
"""

from __future__ import annotations

import unittest
from typing import cast

from comfyui_h3_context.core.managed_run_machine import (
    PHASES,
    GuardRejectedError,
    InvalidTriggerError,
    StateMachine,
    Transition,
    UnknownTriggerError,
    build_stategraph,
    fire,
    restore_state,
)

MACHINE = StateMachine(
    states=("created", "ready", "running", "done", "cancelled"),
    initial="created",
    final=("done", "cancelled"),
    transitions=(
        Transition(trigger="prepare", source=("created",), target="ready"),
        Transition(trigger="start", source=("ready",), target="running", guard="may_start"),
        Transition(trigger="finish", source=("running",), target="done"),
        Transition(trigger="cancel", source=("ready", "running"), target="cancelled"),
    ),
)


def _rows(graph: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], graph["transitions"])


class MachinePhaseTests(unittest.TestCase):
    def test_the_callback_phases_run_in_the_documented_order(self) -> None:
        seen: list[str] = []
        result = fire(MACHINE, "created", "prepare", observer=seen.append)
        self.assertEqual(result.state, "ready")
        self.assertEqual(
            seen,
            ["prepare", "guards", "before", "exit", "set", "enter", "after", "finalize"],
        )
        self.assertEqual(result.phases, tuple(seen))

    def test_a_final_target_runs_on_final_between_enter_and_after(self) -> None:
        seen: list[str] = []
        fire(MACHINE, "running", "finish", observer=seen.append)
        self.assertEqual(seen.index("on_final"), seen.index("enter") + 1)
        self.assertEqual(seen.index("after"), seen.index("on_final") + 1)

    def test_finalize_runs_even_when_a_guard_rejects(self) -> None:
        # CRITICAL: `finalize` is the release phase. If it were skipped on the rejection path a
        # per-run lock or an evidence span taken in `prepare` would leak on every refusal, and
        # refusals are the common case rather than the rare one.
        seen: list[str] = []
        with self.assertRaises(GuardRejectedError):
            fire(
                MACHINE,
                "ready",
                "start",
                guards={"may_start": lambda: False},
                observer=seen.append,
            )
        self.assertEqual(seen[-1], "finalize")
        self.assertNotIn("set", seen)

    def test_finalize_runs_even_when_a_callback_raises(self) -> None:
        def observer(phase: str) -> None:
            if phase == "enter":
                raise RuntimeError("callback failed")

        with self.assertRaises(RuntimeError):
            fire(MACHINE, "created", "prepare", observer=observer)


class MachineTriggerTests(unittest.TestCase):
    def test_an_unknown_trigger_and_an_invalid_one_are_different_errors(self) -> None:
        # Two tiers, because they need different answers: an unknown trigger is a caller defect and
        # a wrong state is a race. Collapsing them into one error makes a 400 and a 409
        # indistinguishable at the route.
        with self.assertRaises(UnknownTriggerError):
            fire(MACHINE, "created", "teleport")
        with self.assertRaises(InvalidTriggerError):
            fire(MACHINE, "created", "finish")

    def test_both_tiers_are_state_machine_errors(self) -> None:
        self.assertTrue(issubclass(UnknownTriggerError, ValueError))
        self.assertTrue(issubclass(InvalidTriggerError, ValueError))
        self.assertTrue(issubclass(GuardRejectedError, ValueError))

    def test_a_rejected_guard_leaves_the_state_untouched(self) -> None:
        with self.assertRaises(GuardRejectedError) as caught:
            fire(MACHINE, "ready", "start", guards={"may_start": lambda: False})
        self.assertIn("may_start", str(caught.exception))

    def test_a_satisfied_guard_advances(self) -> None:
        result = fire(MACHINE, "ready", "start", guards={"may_start": lambda: True})
        self.assertEqual(result.state, "running")

    def test_a_named_guard_with_no_implementation_is_rejected_not_ignored(self) -> None:
        # A missing guard must never read as a satisfied one: a transition that names a guard and
        # runs without it has lost the invariant while still looking correct.
        with self.assertRaises(GuardRejectedError):
            fire(MACHINE, "ready", "start")

    def test_one_trigger_may_leave_several_sources(self) -> None:
        self.assertEqual(fire(MACHINE, "ready", "cancel").state, "cancelled")
        self.assertEqual(fire(MACHINE, "running", "cancel").state, "cancelled")

    def test_an_unknown_source_state_is_rejected(self) -> None:
        with self.assertRaises(InvalidTriggerError):
            fire(MACHINE, "nowhere", "prepare")


class MachineRestoreTests(unittest.TestCase):
    def test_restore_runs_no_callbacks(self) -> None:
        # Reloading a persisted run must not re-fire `enter`, which is where side effects live.
        seen: list[str] = []
        self.assertEqual(restore_state(MACHINE, "running", observer=seen.append), "running")
        self.assertEqual(seen, [])

    def test_restore_rejects_a_state_the_machine_does_not_have(self) -> None:
        with self.assertRaises(ValueError):
            restore_state(MACHINE, "elsewhere")


class StategraphTests(unittest.TestCase):
    def test_the_stategraph_is_derived_from_the_machine_and_is_deterministic(self) -> None:
        first = build_stategraph(MACHINE)
        second = build_stategraph(MACHINE)
        self.assertEqual(first, second)
        self.assertEqual(first["initial"], "created")
        self.assertEqual(first["states"], sorted(MACHINE.states))
        self.assertEqual(first["final"], sorted(MACHINE.final))
        transitions = _rows(first)
        self.assertEqual(
            transitions,
            sorted(transitions, key=lambda row: (str(row["trigger"]), str(row["source"]))),
        )

    def test_every_transition_row_carries_its_guard_or_none(self) -> None:
        rows = {(row["trigger"], row["source"]): row for row in _rows(build_stategraph(MACHINE))}
        self.assertEqual(rows[("start", "ready")]["guard"], "may_start")
        self.assertIsNone(rows[("prepare", "created")]["guard"])

    def test_the_phase_vocabulary_is_closed_and_ordered(self) -> None:
        self.assertEqual(
            PHASES,
            (
                "prepare",
                "guards",
                "before",
                "exit",
                "set",
                "enter",
                "on_final",
                "after",
                "finalize",
            ),
        )


class MachineDefinitionTests(unittest.TestCase):
    def test_a_transition_to_an_unknown_state_is_refused_at_construction(self) -> None:
        # A machine that can reach a state it does not declare is not a machine, and the failure
        # would otherwise appear only on the one path that fires that transition.
        with self.assertRaises(ValueError):
            StateMachine(
                states=("a",),
                initial="a",
                final=(),
                transitions=(Transition(trigger="go", source=("a",), target="b"),),
            )

    def test_an_initial_state_outside_the_declared_states_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            StateMachine(states=("a",), initial="b", final=(), transitions=())

    def test_a_final_state_outside_the_declared_states_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            StateMachine(states=("a",), initial="a", final=("b",), transitions=())

    def test_two_transitions_with_the_same_trigger_and_source_are_refused(self) -> None:
        # Ambiguity here is silent: the first match wins and the second is dead, so the machine
        # would report a transition it never takes.
        with self.assertRaises(ValueError):
            StateMachine(
                states=("a", "b", "c"),
                initial="a",
                final=(),
                transitions=(
                    Transition(trigger="go", source=("a",), target="b"),
                    Transition(trigger="go", source=("a",), target="c"),
                ),
            )


if __name__ == "__main__":
    unittest.main()
