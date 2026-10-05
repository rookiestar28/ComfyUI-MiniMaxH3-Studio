"""Drive one corpus row's setup and subject command through the real timeline decoder.

Two stages need to know what a row's command actually does. The backend qualification stage needs
it to record whether the timeline moved; the report join needs it to know which two compositions a
rendering command row's ``before`` and ``after`` phases are supposed to be pictures of. Those two
answers have to be the same answer, and the only way to guarantee that is for both to run this
code: a second driver would be a second opinion about what the corpus says, which is precisely the
kind of disagreement the conformance run exists to detect rather than to contain.

Nothing here judges anything. It substitutes the recipes' payload tokens, applies the setup steps
and then the subject command through ``decode_timeline_transaction`` /
``apply_timeline_transaction``, and hands the resulting states back. Every disposition stays with
the caller.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from .errors import ContractValidationError
from .semantic_conformance_cases import (
    SETUP_CURSOR,
    STALE_FINGERPRINT,
    TIMELINE_FINGERPRINT,
    CaseRecipe,
    apply_edits,
)
from .timeline_history import (
    TIMELINE_TRANSACTION_SCHEMA,
    TimelineHistoryState,
    TimelineReceipt,
    apply_timeline_transaction,
    decode_timeline_transaction,
)

__all__ = [
    "STALE_REBASE_FINGERPRINT",
    "CommandPhases",
    "SetupResult",
    "command_phases",
    "initial_state",
    "resolve_payload",
    "run_setup",
    "transact",
]

#: A well-formed fingerprint that binds nothing, substituted for the ``$stale_fingerprint`` payload
#: token so a rebase is refused for being stale rather than for being malformed.
STALE_REBASE_FINGERPRINT: Final = "sha256:" + "0" * 64


class DriveError(ValueError):
    """A corpus row's own setup or command could not be driven at all."""


def resolve_payload(payload: Any, cursor: str | None, fingerprint: str) -> Any:
    """Substitute the recipes' payload tokens with the values only a live run can know."""

    if isinstance(payload, str):
        if payload == SETUP_CURSOR:
            return cursor
        if payload == TIMELINE_FINGERPRINT:
            return fingerprint
        if payload == STALE_FINGERPRINT:
            return STALE_REBASE_FINGERPRINT
        return payload
    if isinstance(payload, dict):
        return {key: resolve_payload(value, cursor, fingerprint) for key, value in payload.items()}
    if isinstance(payload, list):
        return [resolve_payload(item, cursor, fingerprint) for item in payload]
    return payload


def initial_state(recipe: CaseRecipe, base: Mapping[str, Any]) -> TimelineHistoryState:
    """The history state this row starts from: the base fixture with the row's edits applied."""

    wire: dict[str, Any] = copy.deepcopy(dict(base))
    apply_edits(wire, recipe)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return TimelineHistoryState.initialize(decode_public_snapshot(wire))


def transact(
    state: TimelineHistoryState, kind: str, payload: Any, identifier: str
) -> tuple[TimelineHistoryState, TimelineReceipt]:
    """Apply one command as a real single-command transaction against the current state."""

    current = state.snapshot
    transaction = decode_timeline_transaction(
        {
            "schema": TIMELINE_TRANSACTION_SCHEMA,
            "request_id": identifier,
            "transaction_id": f"tx-{identifier}",
            "workspace_handle": current.workspace_handle,
            "expected_workspace_revision": current.workspace_revision,
            "expected_timeline_revision": current.timeline_revision,
            "expected_timeline_fingerprint": current.timeline_fingerprint,
            "commands": [{"kind": kind, "payload": payload}],
        }
    )
    return apply_timeline_transaction(state, transaction)


@dataclass(frozen=True, slots=True)
class SetupResult:
    """The state a row's setup steps reached, or the reason they did not reach one."""

    state: TimelineHistoryState | None
    cursor: str | None
    error: str | None


def run_setup(recipe: CaseRecipe, base: Mapping[str, Any]) -> SetupResult:
    """Apply the row's setup steps in order, carrying the history cursor between them."""

    try:
        state = initial_state(recipe, base)
    except ContractValidationError:
        return SetupResult(None, None, "the case fixture could not be constructed")
    cursor: str | None = None
    try:
        for index, step in enumerate(recipe.setup):
            payload = resolve_payload(
                dict(step.payload), cursor, state.snapshot.timeline_fingerprint
            )
            state, receipt = transact(state, step.kind, payload, f"setup-{index}")
            cursor = receipt.history_cursor
    except ContractValidationError:
        return SetupResult(None, cursor, "the case setup could not be applied")
    return SetupResult(state, cursor, None)


@dataclass(frozen=True, slots=True)
class CommandPhases:
    """The two compositions an accepted command row's render phases must be pictures of."""

    before: PublicCompositionSnapshot
    after: PublicCompositionSnapshot

    def wires(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return (dict(self.before.to_wire()), dict(self.after.to_wire()))


def command_phases(recipe: CaseRecipe, base: Mapping[str, Any]) -> CommandPhases:
    """The composition before the row's subject command and the composition after it.

    A command's claim is what it changed, so a single artifact cannot express it: the pair is the
    evidence. Deriving both here, from the contract, is what makes a render record's phases
    checkable rather than merely countable -- a record can *say* it rendered the "after", and only
    the public fingerprint of the composition the command actually produces decides whether it did.
    """

    if recipe.command is None:
        raise DriveError(f"{recipe.case_id} declares no subject command")
    setup = run_setup(recipe, base)
    if setup.state is None:
        raise DriveError(setup.error or "the row reached no usable state")
    before = setup.state.snapshot
    payload = resolve_payload(
        dict(recipe.command.payload), setup.cursor, before.timeline_fingerprint
    )
    result, _receipt = transact(setup.state, recipe.command.kind, payload, "subject")
    return CommandPhases(before=before, after=result.snapshot)
