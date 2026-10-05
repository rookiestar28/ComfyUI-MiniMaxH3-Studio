"""The ManagedRun aggregate: one authority for a run's lifecycle and its cross-registry joins.

Four backend registries each hold part of one run -- the sidebar context workspace, the production
workspace, the sequence coordinator and the geometry receipt store -- and until this module existed
the *client* kept them consistent by calling six routes in the right order. The M23-19 incident was
a join failure between two of them, which is a defect no single registry could have prevented,
because no single registry could see the join.

This module owns the run lifecycle and nothing else. The registries keep their own editing
semantics: absorbing `reorder_segments` or `edit_assisted_proposal` into the run state would not
prevent an M23-19-class defect and would turn one transition table into several thousand lines. Of
the four registries' 33 actions, twelve are lifecycle transitions and live here; seventeen are
sub-state transitions attached to a context revision, a workspace or a run; four are reads.

Pure core: no ComfyUI, no aiohttp, no threads, no clock, no filesystem.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum

from .managed_run_machine import (
    GuardRejectedError,
    InvalidTriggerError,
    StateMachine,
    StateMachineError,
    Transition,
    UnknownTriggerError,
    build_stategraph,
    fire,
    restore_state,
)

MANAGED_RUN_SCHEMA = "h3.context.managed_run.v1"
MANAGED_RUN_EVENT_SCHEMA = "h3.context.managed_run_transition_event.v1"

#: The same live-run ceiling the coordinator and the production registry already enforce. Kept here
#: because the aggregate is now the thing that knows how many runs are live.
MAX_LIVE_MANAGED_RUNS = 16
#: Bounded evidence. The seam is diagnostics, so it must never be the reason a run stops advancing.
MAX_TRANSITION_EVENTS = 64
MAX_GEOMETRY_RECEIPTS_PER_RUN = 16

_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class ManagedRunError(StateMachineError):
    """Raised when a run cannot advance while remaining exact.

    CRITICAL: it subclasses `StateMachineError` so that one `except` clause catches every refusal
    this subsystem raises -- the machine's two-tier trigger errors, its guard rejections, and the
    aggregate's own. Splitting them into unrelated families is not a cosmetic difference: a route
    handler that catches only one of them turns the other into a 500.
    """


class ManagedRunState(str, Enum):
    CREATED = "created"
    CONTEXT_READY = "context_ready"
    PRODUCTION_READY = "production_ready"
    SEQUENCE_PREPARED = "sequence_prepared"
    SUBMITTED = "submitted"
    RUNNING = "running"
    ARTIFACT_RECORDED = "artifact_recorded"
    TERMINAL_SUCCEEDED = "terminal_succeeded"
    TERMINAL_FAILED = "terminal_failed"
    TERMINAL_CANCELLED = "terminal_cancelled"
    TERMINAL_UNKNOWN_OWNERSHIP = "terminal_unknown_ownership"
    EXPIRED = "expired"


class ManagedRunTrigger(str, Enum):
    STAGE_CONTEXT = "stage_context"
    CREATE_PRODUCTION = "create_production"
    RELEASE_PRODUCTION = "release_production"
    PREPARE_SEQUENCE = "prepare_sequence"
    RECORD_SUBMISSION = "record_submission"
    RECORD_RUNNING = "record_running"
    RECORD_ARTIFACT = "record_artifact"
    RECORD_SUCCEEDED = "record_succeeded"
    RECORD_FAILED = "record_failed"
    CANCEL_BEFORE_HOST = "cancel_before_host"
    CANCEL_AFTER_HOST = "cancel_after_host"
    RELEASE_SEQUENCE = "release_sequence"


#: Guard names. They are data because the stategraph publishes them and a test asserts the
#: published graph names the same guards the code evaluates.
SINGLE_SEGMENT_GUARD = "single_segment_sequence"
NO_LIVE_SEQUENCE_GUARD = "no_live_sequence"

_TERMINALS = (
    ManagedRunState.TERMINAL_SUCCEEDED,
    ManagedRunState.TERMINAL_FAILED,
    ManagedRunState.TERMINAL_CANCELLED,
    ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP,
)
_LIVE_STATES = (
    ManagedRunState.CREATED,
    ManagedRunState.CONTEXT_READY,
    ManagedRunState.PRODUCTION_READY,
    ManagedRunState.SEQUENCE_PREPARED,
    ManagedRunState.SUBMITTED,
    ManagedRunState.RUNNING,
    ManagedRunState.ARTIFACT_RECORDED,
)

#: CRITICAL: the single authority for the cancel question, and the whole of it. The coordinator
#: previously decided it twice with two different predicates -- the recorded state tested the
#: runtime's job state while the disposition returned to the client tested whether a queue prompt
#: id existed -- so a run could be recorded `cancelled` and reported `unknown_ownership`, or the
#: reverse. Both are answers to one question: did the host ever receive this prompt? The run
#: reaches `submitted` exactly when it did. Never reintroduce a second predicate; derive whatever
#: is reported from the transition that actually fired.
HOST_MAY_HOLD_THE_PROMPT = frozenset(
    {
        ManagedRunState.SUBMITTED,
        ManagedRunState.RUNNING,
        ManagedRunState.ARTIFACT_RECORDED,
    }
)

MANAGED_RUN_MACHINE = StateMachine(
    states=tuple(state.value for state in ManagedRunState),
    initial=ManagedRunState.CREATED.value,
    final=tuple(state.value for state in (*_TERMINALS, ManagedRunState.EXPIRED)),
    transitions=(
        Transition(
            trigger=ManagedRunTrigger.STAGE_CONTEXT.value,
            source=(ManagedRunState.CREATED.value, ManagedRunState.CONTEXT_READY.value),
            target=ManagedRunState.CONTEXT_READY.value,
        ),
        Transition(
            trigger=ManagedRunTrigger.CREATE_PRODUCTION.value,
            source=(ManagedRunState.CONTEXT_READY.value,),
            target=ManagedRunState.PRODUCTION_READY.value,
        ),
        Transition(
            # CRITICAL: the sources are every state that owns a production workspace, not only the
            # one where release succeeds. Narrowing them to `production_ready` would make the
            # invariant structural and the guard unable to reject anything -- a bound nothing
            # measures -- and would answer a client that released a workspace under a live run
            # with "invalid state" instead of "a sequence depends on it", which is the fact it
            # needs in order to do something about it.
            trigger=ManagedRunTrigger.RELEASE_PRODUCTION.value,
            source=(
                ManagedRunState.PRODUCTION_READY.value,
                ManagedRunState.SEQUENCE_PREPARED.value,
                ManagedRunState.SUBMITTED.value,
                ManagedRunState.RUNNING.value,
                ManagedRunState.ARTIFACT_RECORDED.value,
            ),
            target=ManagedRunState.EXPIRED.value,
            guard=NO_LIVE_SEQUENCE_GUARD,
        ),
        Transition(
            trigger=ManagedRunTrigger.PREPARE_SEQUENCE.value,
            source=(ManagedRunState.PRODUCTION_READY.value,),
            target=ManagedRunState.SEQUENCE_PREPARED.value,
            guard=SINGLE_SEGMENT_GUARD,
        ),
        Transition(
            trigger=ManagedRunTrigger.RECORD_SUBMISSION.value,
            source=(ManagedRunState.SEQUENCE_PREPARED.value,),
            target=ManagedRunState.SUBMITTED.value,
        ),
        Transition(
            trigger=ManagedRunTrigger.RECORD_RUNNING.value,
            source=(ManagedRunState.SUBMITTED.value,),
            target=ManagedRunState.RUNNING.value,
        ),
        Transition(
            trigger=ManagedRunTrigger.RECORD_ARTIFACT.value,
            source=(ManagedRunState.RUNNING.value,),
            target=ManagedRunState.ARTIFACT_RECORDED.value,
        ),
        Transition(
            # CRITICAL: `running` is a source because the coordinator terminalizes a run whose
            # runtime is RUNNING whether or not an artifact was recorded -- a generation can end
            # without one. The auto-advance the coordinator performs when the runtime is still
            # SUBMITTED is mirrored as an explicit `record_running` by the caller, so the table
            # never needs `submitted` here and the aggregate keeps one transition per real event.
            trigger=ManagedRunTrigger.RECORD_SUCCEEDED.value,
            source=(
                ManagedRunState.RUNNING.value,
                ManagedRunState.ARTIFACT_RECORDED.value,
            ),
            target=ManagedRunState.TERMINAL_SUCCEEDED.value,
        ),
        Transition(
            trigger=ManagedRunTrigger.RECORD_FAILED.value,
            source=(
                ManagedRunState.RUNNING.value,
                ManagedRunState.ARTIFACT_RECORDED.value,
            ),
            target=ManagedRunState.TERMINAL_FAILED.value,
        ),
        Transition(
            trigger=ManagedRunTrigger.CANCEL_BEFORE_HOST.value,
            source=(
                ManagedRunState.CONTEXT_READY.value,
                ManagedRunState.PRODUCTION_READY.value,
                ManagedRunState.SEQUENCE_PREPARED.value,
            ),
            target=ManagedRunState.TERMINAL_CANCELLED.value,
        ),
        Transition(
            trigger=ManagedRunTrigger.CANCEL_AFTER_HOST.value,
            source=tuple(state.value for state in HOST_MAY_HOLD_THE_PROMPT),
            target=ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP.value,
        ),
        Transition(
            trigger=ManagedRunTrigger.RELEASE_SEQUENCE.value,
            source=tuple(state.value for state in _TERMINALS),
            target=ManagedRunState.EXPIRED.value,
        ),
    ),
)


def single_segment_sequence(segment_count: int) -> bool:
    """The named premise that makes indexing the first runtime safe.

    CRITICAL: this is the *only* definition. The coordinator's `_prepare` refused a workspace whose
    segment count was not one, and its `_cancel` then read `entry.state.runtimes[0]` with no length
    check -- the guard at one end was what made the index at the other end safe, and neither site
    said so. Every refusal and every index site now names this function, so they cannot drift apart.
    If multi-segment sequences are ever modelled for real, changing this predicate is what makes the
    index sites fail loudly instead of silently reading the wrong runtime.
    """

    return segment_count == 1


#: Triggers the machine declares but no production path fires, each with the reason it is modelled
#: anyway. CRITICAL: this register exists so that a dead transition is a stated decision rather than
#: a silent one, and `tests/test_m23_31_join_invariants.py` asserts it matches the triggers the
#: adapters actually reference. A shipped stategraph that advertises a lifecycle edge nothing takes
#: is a contract making a promise the system does not keep; naming the exceptions is what keeps the
#: rest of the graph trustworthy.
#:
#: Neither of these is enforcement, and for two different reasons. `release_workspace` refuses
#: through the liveness probe before any transition is attempted, so its guarded transition is
#: never reached. `release_sequence` *is* now enforced -- M23-51 made
#: `comfyui_h3_context.core.managed_run_release.decide_release` the single authority over it -- but
#: enforcement happens there and in the service that applies it, not by firing this trigger. The
#: rows below therefore still describe edges nothing takes.
MODEL_ONLY_TRIGGERS: Mapping[ManagedRunTrigger, str] = {
    ManagedRunTrigger.RELEASE_SEQUENCE: (
        "release and detach are decided by managed_run_release.decide_release and applied by "
        "ManagedRunReleaseService, which removes a run by dropping it from the registry rather "
        "than by advancing it here; the terminal-only sources on this edge state the same rule "
        "the table enforces, and modelling it keeps the published graph honest about the fact "
        "that a run can end without a transition"
    ),
    ManagedRunTrigger.RELEASE_PRODUCTION: (
        "the production registry refuses a live release through the bound liveness probe "
        "before any "
        "transition is attempted, so the guarded transition is never reached; the guard and the "
        "probe are one predicate, and the probe is the one that runs"
    ),
}


def holds_live_sequence(run: ManagedRun) -> bool:
    """Whether a prepared generation sequence still depends on this run's production workspace.

    CRITICAL: this is the *only* definition of the rule `NO_LIVE_SEQUENCE_GUARD` names, and the
    production registry's release path must ask it rather than re-derive it. It was written twice
    once already -- as `prepared_sequence is None` in the guard and as `prepared_sequence is not
    None` beside the release probe -- which is the divergent-authority shape this whole item exists
    to remove, reintroduced one join over. Two texts of one rule agree until somebody edits one.
    """

    return run.prepared_sequence is not None


def cancel_trigger(state: ManagedRunState) -> ManagedRunTrigger:
    """Choose the cancel transition from the run's own state, which is the only authority.

    The disposition a caller reports must be read off the transition this returns, never computed
    a second time from a different field. See `HOST_MAY_HOLD_THE_PROMPT`.
    """

    if state in HOST_MAY_HOLD_THE_PROMPT:
        return ManagedRunTrigger.CANCEL_AFTER_HOST
    return ManagedRunTrigger.CANCEL_BEFORE_HOST


@dataclass(frozen=True)
class TransitionEvent:
    """One privacy-safe transition record.

    CRITICAL: every field is drawn from a closed vocabulary the machine itself defines -- trigger,
    source state, target state, guard name -- or is an identifier already validated against
    `_HANDLE`. There is deliberately no free-text field, because a redaction filter can be bypassed
    by a future caller while a missing channel cannot. Never add a `detail`, `message` or
    `reason_text` member here; the durable journal that M23-35 owns is the place for richer
    evidence, under its own separate review.
    """

    sequence: int
    run_handle: str
    trigger: str
    source: str
    target: str
    guard: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "guard": self.guard,
            "run_handle": self.run_handle,
            "schema": MANAGED_RUN_EVENT_SCHEMA,
            "sequence": self.sequence,
            "source": self.source,
            "target": self.target,
            "trigger": self.trigger,
        }


def _handle(value: object, field: str) -> str:
    if type(value) is not str or _HANDLE.fullmatch(value) is None:
        raise ManagedRunError(f"{field} is not an exact identifier")
    return value


@dataclass(frozen=True)
class ManagedRun:
    """One run's lifecycle state and the joins it has made, as immutable data."""

    run_handle: str
    state: ManagedRunState = ManagedRunState.CREATED
    context_revision: int | None = None
    production_workspace: str | None = None
    prepared_sequence: str | None = None
    prompt_id: str | None = None
    artifact_receipt: str | None = None
    geometry_receipts: tuple[str, ...] = ()
    segment_count: int = 0
    events: tuple[TransitionEvent, ...] = ()

    def __post_init__(self) -> None:
        _handle(self.run_handle, "run_handle")
        if type(self.state) is not ManagedRunState:
            raise ManagedRunError("state is not a ManagedRunState")
        if len(self.geometry_receipts) > MAX_GEOMETRY_RECEIPTS_PER_RUN:
            raise ManagedRunError("geometry receipts exceed the per-run bound")
        if len(self.events) > MAX_TRANSITION_EVENTS:
            raise ManagedRunError("transition evidence exceeds its bound")

    @property
    def is_live(self) -> bool:
        return self.state in _LIVE_STATES

    def to_wire(self) -> dict[str, object]:
        return {
            "artifact_receipt": self.artifact_receipt,
            "context_revision": self.context_revision,
            "geometry_receipts": list(self.geometry_receipts),
            "prepared_sequence": self.prepared_sequence,
            "production_workspace": self.production_workspace,
            "prompt_id": self.prompt_id,
            "run_handle": self.run_handle,
            "schema": MANAGED_RUN_SCHEMA,
            "segment_count": self.segment_count,
            "state": self.state.value,
        }


def _guards(run: ManagedRun) -> dict[str, Callable[[], bool]]:
    return {
        # CRITICAL: `_prepare` in the coordinator refuses a workspace whose segment count is not
        # one, and `_cancel` then indexes `runtimes[0]` with no length check -- the guard at one
        # end is what makes the index at the other end safe, and neither site said so. This is
        # that guard, named once. Every site that indexes the first runtime depends on it; if the
        # premise is ever relaxed into real multi-segment sequences, those index sites must be
        # found and changed together, not discovered by an IndexError in production.
        SINGLE_SEGMENT_GUARD: lambda: single_segment_sequence(run.segment_count),
        # Production never regresses to unavailable once a sequence depends on it. The rule
        # itself lives in `holds_live_sequence`, which the release path also calls.
        NO_LIVE_SEQUENCE_GUARD: lambda: not holds_live_sequence(run),
    }


def advance(
    run: ManagedRun,
    trigger: ManagedRunTrigger,
    *,
    context_revision: int | None = None,
    production_workspace: str | None = None,
    prepared_sequence: str | None = None,
    prompt_id: str | None = None,
    artifact_receipt: str | None = None,
    segment_count: int | None = None,
) -> ManagedRun:
    """Apply one lifecycle transition, recording bounded evidence for it."""

    guards = _guards(run if segment_count is None else replace(run, segment_count=segment_count))
    result = fire(MANAGED_RUN_MACHINE, run.state.value, trigger.value, guards=guards)
    event = TransitionEvent(
        sequence=len(run.events),
        run_handle=run.run_handle,
        trigger=result.transition.trigger,
        source=run.state.value,
        target=result.state,
        guard=result.transition.guard,
    )
    events = (*run.events, event)[-MAX_TRANSITION_EVENTS:]
    return replace(
        run,
        state=ManagedRunState(result.state),
        context_revision=run.context_revision if context_revision is None else context_revision,
        production_workspace=(
            run.production_workspace
            if production_workspace is None
            else _handle(production_workspace, "production_workspace")
        ),
        prepared_sequence=(
            run.prepared_sequence
            if prepared_sequence is None
            else _handle(prepared_sequence, "prepared_sequence")
        ),
        prompt_id=run.prompt_id if prompt_id is None else _handle(prompt_id, "prompt_id"),
        artifact_receipt=(
            run.artifact_receipt
            if artifact_receipt is None
            else _handle(artifact_receipt, "artifact_receipt")
        ),
        segment_count=run.segment_count if segment_count is None else segment_count,
        events=events,
    )


def attach_geometry_receipt(run: ManagedRun, receipt: str) -> ManagedRun:
    """Bind one observed geometry receipt to a run. A claimed receipt is not re-claimable."""

    identifier = _handle(receipt, "geometry_receipt")
    if identifier in run.geometry_receipts:
        raise ManagedRunError("geometry receipt is already claimed")
    if len(run.geometry_receipts) >= MAX_GEOMETRY_RECEIPTS_PER_RUN:
        raise ManagedRunError("geometry receipts exceed the per-run bound")
    return replace(run, geometry_receipts=(*run.geometry_receipts, identifier))


def restore(run_handle: str, state: str) -> ManagedRun:
    """Adopt a persisted run without replaying any transition callback or emitting evidence."""

    return ManagedRun(
        run_handle=_handle(run_handle, "run_handle"),
        state=ManagedRunState(restore_state(MANAGED_RUN_MACHINE, state)),
    )


def managed_run_stategraph() -> dict[str, object]:
    """The published stategraph, derived from `MANAGED_RUN_MACHINE` rather than kept beside it."""

    return build_stategraph(MANAGED_RUN_MACHINE)


__all__ = [
    "HOST_MAY_HOLD_THE_PROMPT",
    "MANAGED_RUN_EVENT_SCHEMA",
    "MANAGED_RUN_MACHINE",
    "MODEL_ONLY_TRIGGERS",
    "MANAGED_RUN_SCHEMA",
    "MAX_GEOMETRY_RECEIPTS_PER_RUN",
    "MAX_LIVE_MANAGED_RUNS",
    "MAX_TRANSITION_EVENTS",
    "NO_LIVE_SEQUENCE_GUARD",
    "SINGLE_SEGMENT_GUARD",
    "GuardRejectedError",
    "InvalidTriggerError",
    "ManagedRun",
    "ManagedRunError",
    "ManagedRunState",
    "ManagedRunTrigger",
    "StateMachineError",
    "TransitionEvent",
    "UnknownTriggerError",
    "advance",
    "attach_geometry_receipt",
    "cancel_trigger",
    "holds_live_sequence",
    "managed_run_stategraph",
    "restore",
    "single_segment_sequence",
]
