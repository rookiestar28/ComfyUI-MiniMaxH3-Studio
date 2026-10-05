"""Releasing a run and detaching a client are different questions, decided here and only here.

`release_sequence` was one request that meant one thing: drop this run. That is correct for a
prepared child whose submission failed, and it is data loss for a child the host is still
executing -- `MODEL_ONLY_TRIGGERS` in `managed_run.py` has said so in prose since M23-31 while the
coordinator kept doing it. The approved B1 contract splits the request by *intent* and answers from
the run's own state.

The decision is a total function of `(run, intent, observed_terminal)` and lives in the pure core so
that the HTTP route, the coordinator table and the ManagedRun registry cannot each grow their own
predicate. That is the same failure `HOST_MAY_HOLD_THE_PROMPT` was introduced to end for the cancel
question, and this module is deliberately its neighbour.

Pure core: no ComfyUI, no aiohttp, no threads, no clock, no filesystem.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from enum import Enum

from .managed_run import HOST_MAY_HOLD_THE_PROMPT, ManagedRun, ManagedRunState

RELEASE_DECISION_SCHEMA = "h3.context.managed_run_release_decision.v1"


class ReleaseIntent(str, Enum):
    """What the caller is asking for. The approved B1 vocabulary, closed."""

    #: Undo a preparation the host never accepted. The only intent that may remove a live run.
    CLEANUP_PRE_SUBMIT = "cleanup_pre_submit"
    #: The client is leaving. The run keeps running and keeps its authority.
    DETACH_CLIENT = "detach_client"
    #: The caller has seen the result and is done with it. Requires proof.
    CLEANUP_TERMINAL = "cleanup_terminal"


class ReleaseDisposition(str, Enum):
    """What happened, when something happened."""

    RELEASED = "released"
    DETACHED = "detached"
    DETACHED_TERMINAL = "detached_terminal"
    DETACHED_UNKNOWN_OWNERSHIP = "detached_unknown_ownership"
    #: An idempotent replay: the lease the caller asked for is already the one in force.
    CURRENT = "current"


class ReleaseRefusal(str, Enum):
    """Why nothing happened. Each row of the table refuses for its own reason.

    CRITICAL: these are distinct on purpose and must stay distinct. A single "invalid state"
    refusal reads to a client as its own bug, so it retries or gives up; `host_owned` and
    `not_terminal` are facts about the run that tell the client what to do instead.
    """

    #: Nothing was prepared, so there is nothing to clean up.
    NOT_APPLICABLE = "not_applicable"
    #: The host cannot be holding this prompt, so there is nothing to detach from.
    NOT_HOST_OWNED = "not_host_owned"
    #: The run has not settled, so it cannot be cleaned up as a finished one.
    NOT_TERMINAL = "not_terminal"
    #: The host may still be executing this prompt. Removing it would destroy the only authority
    #: able to accept the result.
    HOST_OWNED = "host_owned"
    #: The intent does not apply to this run, or its terminal proof did not match.
    WRONG_INTENT = "wrong_intent"
    #: We do not know whether the host took this prompt, and no proof can settle that.
    UNRESOLVED_OWNERSHIP = "unresolved_ownership"
    #: The run is past its lease and no longer exists as an authority.
    GONE = "gone"


_SETTLED_TERMINALS = frozenset(
    {
        ManagedRunState.TERMINAL_SUCCEEDED,
        ManagedRunState.TERMINAL_FAILED,
        ManagedRunState.TERMINAL_CANCELLED,
    }
)
_ALL_TERMINALS = _SETTLED_TERMINALS | {ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP}

#: Domain separation, so a digest minted here can never be mistaken for one minted anywhere else.
_FINGERPRINT_DOMAIN = "h3.context.managed_run.terminal.v1"
#: A separator that cannot occur in a handle, a prompt id or a receipt, so no two different runs can
#: produce the same joined string by shifting a boundary.
_FINGERPRINT_SEPARATOR = "\x1f"


@dataclass(frozen=True)
class ReleaseDecision:
    """Exactly one of `disposition` and `refusal`, never both and never neither."""

    intent: ReleaseIntent
    disposition: ReleaseDisposition | None = None
    refusal: ReleaseRefusal | None = None

    def __post_init__(self) -> None:
        if (self.disposition is None) == (self.refusal is None):
            raise ValueError(
                "a release decision carries exactly one of a disposition and a refusal; "
                f"got disposition={self.disposition!r} refusal={self.refusal!r}"
            )

    @property
    def removes_authority(self) -> bool:
        """Whether acting on this decision drops the run.

        CRITICAL: `released` is the only outcome that removes anything, and callers must branch on
        this rather than on the disposition name. Every `detached*` outcome deliberately leaves the
        run, its prompt id and its evidence in place -- a detach that removed the run would be the
        M23-51 defect with a new name on it.
        """
        return self.disposition is ReleaseDisposition.RELEASED


def terminal_fingerprint(run: ManagedRun) -> str | None:
    """An opaque, deterministic digest of a settled run's observable outcome, or `None`.

    It travels to the client with the terminal projection and comes back as the proof that the
    caller has actually seen this result before asking to remove it. It carries no prompt id, no
    receipt and no path, so it is safe in a log or a URL.
    """
    if run.state not in _ALL_TERMINALS:
        return None
    material = _FINGERPRINT_SEPARATOR.join(
        (
            _FINGERPRINT_DOMAIN,
            run.run_handle,
            run.state.value,
            run.prompt_id or "",
            run.artifact_receipt or "",
        )
    )
    # The repository-wide fingerprint shape, so the coordinator's existing `_fingerprint`
    # validator accepts a proof arriving from the wire without a second, parallel format.
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def decide_release(
    run: ManagedRun,
    intent: ReleaseIntent,
    observed_terminal: str | None = None,
) -> ReleaseDecision:
    """The whole of the release/detach decision. Total over every state and every intent."""
    if run.state is ManagedRunState.EXPIRED:
        # A tombstoned run answers the same way to everything: it is gone, and saying so is not an
        # error. Replaying any intent against it must be safe and must not resurrect anything.
        return ReleaseDecision(intent, refusal=ReleaseRefusal.GONE)

    if intent is ReleaseIntent.CLEANUP_PRE_SUBMIT:
        return _decide_cleanup_pre_submit(run)
    if intent is ReleaseIntent.DETACH_CLIENT:
        return _decide_detach(run)
    return _decide_cleanup_terminal(run, observed_terminal)


def _decide_cleanup_pre_submit(run: ManagedRun) -> ReleaseDecision:
    intent = ReleaseIntent.CLEANUP_PRE_SUBMIT
    state = run.state
    if state in _ALL_TERMINALS:
        return ReleaseDecision(intent, refusal=ReleaseRefusal.WRONG_INTENT)
    # CRITICAL: the prompt id is checked *as well as* the state, and both are checked before the
    # prepared branch. `record_submission` moves prepared -> submitted, so "prepared with a prompt
    # id" should not exist -- but a submission recorded against the coordinator and not yet against
    # the aggregate is exactly the window this item's atomic service closes, and during it the
    # state label lags the fact. Trusting the label there deletes a run the host accepted. Never
    # reorder these two checks below the release.
    if state in HOST_MAY_HOLD_THE_PROMPT or run.prompt_id is not None:
        return ReleaseDecision(intent, refusal=ReleaseRefusal.HOST_OWNED)
    if state is ManagedRunState.SEQUENCE_PREPARED and run.prepared_sequence is not None:
        return ReleaseDecision(intent, disposition=ReleaseDisposition.RELEASED)
    return ReleaseDecision(intent, refusal=ReleaseRefusal.NOT_APPLICABLE)


def _decide_detach(run: ManagedRun) -> ReleaseDecision:
    intent = ReleaseIntent.DETACH_CLIENT
    state = run.state
    # CRITICAL: the terminal cases come first, and they succeed. The race this item exists to make
    # safe is a client deciding to leave while the run is live and the terminal landing before the
    # request does. The old unconditional handler answered `released` and discarded the result the
    # user would come back for; here the detach still succeeds and reports which kind it was.
    if state in _SETTLED_TERMINALS:
        return ReleaseDecision(intent, disposition=ReleaseDisposition.DETACHED_TERMINAL)
    if state is ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP:
        # Not a success and not a failure. Collapsing it into either would be a fabricated
        # observation about a host we never heard back from.
        return ReleaseDecision(intent, disposition=ReleaseDisposition.DETACHED_UNKNOWN_OWNERSHIP)
    if state in HOST_MAY_HOLD_THE_PROMPT or run.prompt_id is not None:
        return ReleaseDecision(intent, disposition=ReleaseDisposition.DETACHED)
    return ReleaseDecision(intent, refusal=ReleaseRefusal.NOT_HOST_OWNED)


def _decide_cleanup_terminal(run: ManagedRun, observed_terminal: str | None) -> ReleaseDecision:
    intent = ReleaseIntent.CLEANUP_TERMINAL
    state = run.state
    if state is ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP:
        # CRITICAL: unknown ownership must never be cleanable by proof. The fingerprint is minted
        # over the uncertainty itself, so accepting it here would let a caller remove a run whose
        # result nobody knows by asserting the very fact that is unknown.
        return ReleaseDecision(intent, refusal=ReleaseRefusal.UNRESOLVED_OWNERSHIP)
    if state not in _SETTLED_TERMINALS:
        return ReleaseDecision(intent, refusal=ReleaseRefusal.NOT_TERMINAL)
    expected = terminal_fingerprint(run)
    if expected is None or not observed_terminal:
        return ReleaseDecision(intent, refusal=ReleaseRefusal.WRONG_INTENT)
    # CRITICAL: the proof is the only thing standing between `cleanup_terminal` and a destructive
    # escape hatch any client could guess its way into. A mismatch must refuse -- never fall
    # through to releasing anyway, and never compare with `==`.
    if not hmac.compare_digest(observed_terminal, expected):
        return ReleaseDecision(intent, refusal=ReleaseRefusal.WRONG_INTENT)
    return ReleaseDecision(intent, disposition=ReleaseDisposition.RELEASED)


# -- the retained row: what a detached run keeps, and for exactly how long --------------------

DETACHED_PROJECTION_SCHEMA = "h3.context.managed_run_detached_projection.v1"

#: The window after a run's own timeout in which its result stays observable to a client that
#: comes back. Long enough to survive a reload, short enough that an abandoned run frees its slot.
DETACHED_GRACE_SECONDS = 300.0
#: The hard ceiling on visibility, independent of any timeout the workflow asked for. A timeout is
#: untrusted input; without this one run could hold a live slot for a week.
MAX_DETACHED_VISIBILITY_SECONDS = 24 * 3600.0

_HOST_OWNED_CATEGORY = "host_owned"
_TERMINAL_CATEGORY = "terminal"
_UNKNOWN_OWNERSHIP_CATEGORY = "unknown_ownership"


def detached_deadline(
    *,
    submitted_at: float,
    timeout_ms: int,
    parent_absolute_deadline: float | None = None,
) -> float:
    """When a detached run stops being visible. Fixed at detach, never renewed.

    CRITICAL: this takes no clock, and must not be given one. A deadline computed from "now" is a
    sliding deadline: every read, poll, reconnect and duplicate event a returning client performs
    would renew it, so a run nobody ever comes back for would never expire while occupying one of
    the sixteen live owner slots. The whole lease property is that the answer depends only on facts
    fixed when the prompt was submitted.
    """
    if type(timeout_ms) is not int or timeout_ms < 0:
        raise ValueError(f"timeout_ms must be a non-negative integer; got {timeout_ms!r}")
    grace = submitted_at + timeout_ms / 1000.0 + DETACHED_GRACE_SECONDS
    ceiling = submitted_at + MAX_DETACHED_VISIBILITY_SECONDS
    deadline = min(grace, ceiling)
    if parent_absolute_deadline is not None:
        # CRITICAL: applied as a `min`, never as a replacement. The M26 parent lease is
        # authoritative when it is *stricter*; a parent that outlives its child must not extend the
        # child, or a stale grandchild rides an unrelated sequence past its own timeout.
        deadline = min(deadline, parent_absolute_deadline)
    return deadline


@dataclass(frozen=True, slots=True)
class DetachedRunProjectionV1:
    """The bounded, content-free row a detached run leaves behind.

    CRITICAL: this outlives the client that created it, so every member is an identifier, a
    category or a digest. No prompt content, graph, workflow, media, path, URL, host output
    locator, credential, provider payload or exception text may be added -- the artifact receipt is
    deliberately reduced to `terminal_fingerprint` rather than carried, because a receipt addresses
    stored media.
    """

    run_handle: str
    lifecycle_category: str
    observation_category: str
    deadline: float
    prompt_id: str | None = None
    prepared_sequence: str | None = None
    terminal_fingerprint: str | None = None
    schema: str = DETACHED_PROJECTION_SCHEMA

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "run_handle": self.run_handle,
            "lifecycle_category": self.lifecycle_category,
            "observation_category": self.observation_category,
            "prompt_id": self.prompt_id,
            "prepared_sequence": self.prepared_sequence,
            "deadline": self.deadline,
            "terminal_fingerprint": self.terminal_fingerprint,
        }


def _lifecycle_category(state: ManagedRunState) -> str:
    if state in _SETTLED_TERMINALS:
        return _TERMINAL_CATEGORY
    if state is ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP:
        return _UNKNOWN_OWNERSHIP_CATEGORY
    return _HOST_OWNED_CATEGORY


def detached_projection(run: ManagedRun, *, deadline: float) -> DetachedRunProjectionV1:
    """Project a detachable run onto its retained row, or refuse.

    Detachability is decided the same way `_decide_detach` decides it, and deliberately not by a
    second predicate: a run has a retained row exactly when a detach would have succeeded.
    """
    state = run.state
    if state is ManagedRunState.EXPIRED:
        raise ValueError("an expired run is gone; it has nothing to retain")
    detachable = (
        state in HOST_MAY_HOLD_THE_PROMPT or state in _ALL_TERMINALS or run.prompt_id is not None
    )
    if not detachable:
        raise ValueError(f"a run in {state.value} has nothing to retain")
    return DetachedRunProjectionV1(
        run_handle=run.run_handle,
        lifecycle_category=_lifecycle_category(state),
        observation_category=state.value,
        deadline=deadline,
        prompt_id=run.prompt_id,
        prepared_sequence=run.prepared_sequence,
        terminal_fingerprint=terminal_fingerprint(run),
    )
