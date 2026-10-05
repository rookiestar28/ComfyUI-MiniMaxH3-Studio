"""Deterministic dependency-free state machine behind the ManagedRun aggregate.

This module knows nothing about runs, registries, ComfyUI, threads or time. It answers one
question -- may this trigger fire from this state, and what happens in what order when it does --
so that the aggregate above it can be a table of transitions rather than a chain of `if`
statements spread across four adapters.

The callback phase order is the contract, not an implementation detail. It is the order the
`transitions` library established and the reason a caller can hold a lock in `prepare` and release
it in `finalize`: `finalize` runs in a `finally`, so it runs on the rejection path and the raising
path as well as the successful one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

MANAGED_RUN_STATEGRAPH_SCHEMA = "h3.context.managed_run_stategraph.v1"

#: The closed, ordered callback vocabulary. `on_final` runs only when the target is a final state.
PHASES: tuple[str, ...] = (
    "prepare",
    "guards",
    "before",
    "exit",
    "set",
    "enter",
    "on_final",
    "after",
    "finalize",
)

_ORDINARY_PHASES: tuple[str, ...] = ("prepare", "guards", "before", "exit", "set", "enter")


class StateMachineError(ValueError):
    """Raised when a machine definition or a transition request cannot remain exact."""


class UnknownTriggerError(StateMachineError):
    """The trigger is not in the machine's vocabulary at all -- a caller defect."""


class InvalidTriggerError(StateMachineError):
    """The trigger exists but does not leave this state -- a race, not a caller defect."""


class GuardRejectedError(StateMachineError):
    """A named guard refused the transition, or had no implementation to refuse it with."""


@dataclass(frozen=True)
class Transition:
    """One trigger, the states it may leave, the state it reaches, and the guard it must satisfy."""

    trigger: str
    source: tuple[str, ...]
    target: str
    guard: str | None = None


@dataclass(frozen=True)
class StateMachine:
    """A validated transition table. Construction refuses anything the machine could not honour."""

    states: tuple[str, ...]
    initial: str
    final: tuple[str, ...]
    transitions: tuple[Transition, ...]

    def __post_init__(self) -> None:
        known = set(self.states)
        if len(known) != len(self.states):
            raise StateMachineError("machine states are not unique")
        if self.initial not in known:
            raise StateMachineError("initial state is not declared")
        for state in self.final:
            if state not in known:
                raise StateMachineError(f"final state is not declared: {state}")
        # CRITICAL: reject an ambiguous (trigger, source) pair at construction. Resolution is
        # first-match, so a second transition for the same pair is dead code that the stategraph
        # still advertises -- the machine would publish a transition it can never take.
        seen: set[tuple[str, str]] = set()
        for transition in self.transitions:
            if transition.target not in known:
                raise StateMachineError(f"transition target is not declared: {transition.target}")
            if not transition.source:
                raise StateMachineError(f"transition has no source: {transition.trigger}")
            for state in transition.source:
                if state not in known:
                    raise StateMachineError(f"transition source is not declared: {state}")
                key = (transition.trigger, state)
                if key in seen:
                    raise StateMachineError(
                        f"ambiguous transition: {transition.trigger} from {state}"
                    )
                seen.add(key)

    @property
    def triggers(self) -> frozenset[str]:
        return frozenset(transition.trigger for transition in self.transitions)


@dataclass(frozen=True)
class FireResult:
    """The state reached, the phases that ran, and the transition that produced them."""

    state: str
    phases: tuple[str, ...]
    transition: Transition


def _select(machine: StateMachine, state: str, trigger: str) -> Transition:
    if trigger not in machine.triggers:
        raise UnknownTriggerError(f"trigger is not in the machine vocabulary: {trigger}")
    for transition in machine.transitions:
        if transition.trigger == trigger and state in transition.source:
            return transition
    raise InvalidTriggerError(f"trigger {trigger} does not leave state {state}")


def _evaluate(transition: Transition, guards: Mapping[str, Callable[[], bool]] | None) -> None:
    if transition.guard is None:
        return
    # CRITICAL: a named guard with no implementation is a rejection, never a pass. Treating a
    # missing guard as satisfied loses the invariant while the transition still looks correct,
    # which is the failure this two-line branch exists to prevent.
    implementation = (guards or {}).get(transition.guard)
    if implementation is None or not implementation():
        raise GuardRejectedError(f"guard refused the transition: {transition.guard}")


def fire(
    machine: StateMachine,
    state: str,
    trigger: str,
    *,
    guards: Mapping[str, Callable[[], bool]] | None = None,
    observer: Callable[[str], None] | None = None,
) -> FireResult:
    """Fire one trigger, running the closed phase sequence with `finalize` in a `finally`."""

    transition = _select(machine, state, trigger)
    phases: list[str] = []

    def run(phase: str) -> None:
        phases.append(phase)
        if observer is not None:
            observer(phase)

    try:
        run("prepare")
        run("guards")
        _evaluate(transition, guards)
        for phase in _ORDINARY_PHASES[2:]:
            run(phase)
        if transition.target in machine.final:
            run("on_final")
        run("after")
    finally:
        run("finalize")
    return FireResult(state=transition.target, phases=tuple(phases), transition=transition)


def restore_state(
    machine: StateMachine,
    state: str,
    *,
    observer: Callable[[str], None] | None = None,
) -> str:
    """Adopt a persisted state without running any callback.

    CRITICAL: this deliberately ignores `observer`. Reloading a run must not re-fire `enter`,
    which is where side effects live -- a restore that replayed callbacks would re-emit evidence
    and re-take leases for work that already happened.
    """

    del observer
    if state not in machine.states:
        raise StateMachineError(f"state is not declared: {state}")
    return state


def _rows(transitions: Iterable[Transition]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for transition in transitions:
        for state in transition.source:
            rows.append(
                {
                    "guard": transition.guard,
                    "source": state,
                    "target": transition.target,
                    "trigger": transition.trigger,
                }
            )
    return sorted(rows, key=lambda row: (str(row["trigger"]), str(row["source"])))


def build_stategraph(machine: StateMachine) -> dict[str, object]:
    """Project the machine as deterministic data.

    Derived from the definition rather than written beside it: a hand-maintained copy is a second
    source of truth that drifts silently, and the whole point of publishing the graph is that a
    reader can trust it describes the code that runs.
    """

    return {
        "final": sorted(machine.final),
        "initial": machine.initial,
        "phases": list(PHASES),
        "schema": MANAGED_RUN_STATEGRAPH_SCHEMA,
        "states": sorted(machine.states),
        "transitions": _rows(machine.transitions),
    }


__all__ = [
    "MANAGED_RUN_STATEGRAPH_SCHEMA",
    "PHASES",
    "FireResult",
    "GuardRejectedError",
    "InvalidTriggerError",
    "StateMachine",
    "StateMachineError",
    "Transition",
    "UnknownTriggerError",
    "build_stategraph",
    "fire",
    "restore_state",
]
