"""Process-local ManagedRun registry: bounded storage, two-level locking, evidence emission.

The pure aggregate in `core/managed_run.py` decides what a run may do. This module is the only
place that holds runs, so it owns the three things an immutable aggregate cannot: how many runs
exist, when a stale one is pruned, and which lock serializes a transition.

The locking is two levels because one is not enough and a global one is what M23-31 is removing.
A single registry-wide lock made every handler running under `asyncio.to_thread` contend with
every other handler, including handlers for unrelated runs.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..core.canonical import binary64_token, canonical_fingerprint
from ..core.managed_run import (
    MAX_LIVE_MANAGED_RUNS,
    ManagedRun,
    ManagedRunError,
    ManagedRunState,
    ManagedRunTrigger,
    TransitionEvent,
    advance,
    attach_geometry_receipt,
    cancel_trigger,
)

MANAGED_RUN_TTL_SECONDS = 900.0
MAX_MANAGED_RUN_TOMBSTONES = 64
MANAGED_RUN_TOMBSTONE_TTL_SECONDS = 300.0
MANAGED_RUN_SLOT_RESERVATION_SCHEMA = "h3.context.managed_run_slot_reservation.v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ManagedRunRegistryError(ManagedRunError):
    """Raised when the registry cannot hold or find a run while remaining exact."""


@dataclass(frozen=True, slots=True)
class ManagedRunSlotReservationV1:
    reservation_id: str
    parent_sequence_id: str
    parent_authorization_fingerprint: str
    expires_at: float
    released: bool = False
    token_fingerprint: str | None = None
    schema: str = MANAGED_RUN_SLOT_RESERVATION_SCHEMA

    def __post_init__(self) -> None:
        # IMPORTANT: this token reserves shared capacity; keep every authority field canonical.
        if (
            self.schema != MANAGED_RUN_SLOT_RESERVATION_SCHEMA
            or type(self.reservation_id) is not str
            or _IDENTIFIER.fullmatch(self.reservation_id) is None
            or type(self.parent_sequence_id) is not str
            or _IDENTIFIER.fullmatch(self.parent_sequence_id) is None
            or type(self.parent_authorization_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.parent_authorization_fingerprint) is None
            or isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(float(self.expires_at))
            or float(self.expires_at) <= 0
            or type(self.released) is not bool
        ):
            raise ManagedRunRegistryError("invalid managed run slot reservation")
        expected = canonical_fingerprint(self._token_wire())
        if self.token_fingerprint is None:
            object.__setattr__(self, "token_fingerprint", expected)
        elif self.token_fingerprint != expected:
            raise ManagedRunRegistryError("managed run slot reservation fingerprint mismatch")

    def _token_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "reservation_id": self.reservation_id,
            "parent_sequence_id": self.parent_sequence_id,
            "parent_authorization_fingerprint": self.parent_authorization_fingerprint,
            "expires_at": binary64_token(float(self.expires_at)),
        }

    @property
    def fingerprint(self) -> str:
        if self.token_fingerprint is None:  # pragma: no cover - initialized above
            raise ManagedRunRegistryError("managed run slot reservation fingerprint missing")
        return self.token_fingerprint


@dataclass(slots=True)
class _SlotReservationState:
    token: ManagedRunSlotReservationV1
    bound_run_handle: str | None = None
    released: bool = False

    def snapshot(self) -> ManagedRunSlotReservationV1:
        return replace(self.token, released=self.released)


@dataclass
class _Entry:
    run: ManagedRun
    touched_at: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Set once, when the client detaches, and never moved afterwards. While it is `None` the row
    #: expires on the sliding `touched_at` TTL; once it is set, that TTL stops applying to this row.
    detached_deadline: float | None = None
    #: M26 holds one child until its parent-first expiry transition records truthful ownership.
    #: Only that transition's detach or the ordinary exact release clears the protection.
    parent_transition_protected: bool = False


class ManagedRunRegistry:
    """Bounded process-local storage for ManagedRun aggregates."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sink: Callable[[TransitionEvent], None] | None = None,
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._table_lock = threading.Lock()
        self._runs: OrderedDict[str, _Entry] = OrderedDict()
        self._tombstones: OrderedDict[str, float] = OrderedDict()
        self._slot_reservations: dict[str, _SlotReservationState] = {}
        self._run_reservations: dict[str, str] = {}
        self._dropped_events = 0

    # -- registry level -------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        """Drop expired runs and tombstones. Callers hold the table lock."""

        for handle, entry in list(self._runs.items()):
            # CRITICAL: the sole authority on expiry, and the only place a detached row's
            # deadline is consulted. A detached row expires on its own fixed deadline and never on
            # the sliding TTL, and the two must not be merged into one `or`/`and` condition: the
            # sliding branch is renewed by every read, so any expression that still reaches
            # `touched_at` for a detached row hands a returning client the power to keep an
            # abandoned run alive forever inside a sixteen-row bound. `_entry` therefore refreshes
            # `touched_at` unconditionally -- it is this branch, not that one, that makes the lease
            # fixed.
            if entry.parent_transition_protected:
                continue
            if entry.detached_deadline is not None:
                expired = now > entry.detached_deadline
            else:
                expired = now - entry.touched_at > MANAGED_RUN_TTL_SECONDS
            if expired:
                del self._runs[handle]
                self._unbind_reserved_run(handle)
                self._tombstones[handle] = now
        for state in self._slot_reservations.values():
            if (
                not state.released
                and state.bound_run_handle is None
                and float(state.token.expires_at) <= now
            ):
                state.released = True
        for handle, stamped in list(self._tombstones.items()):
            if now - stamped > MANAGED_RUN_TOMBSTONE_TTL_SECONDS:
                del self._tombstones[handle]
        while len(self._tombstones) > MAX_MANAGED_RUN_TOMBSTONES:
            self._tombstones.popitem(last=False)

    def create(self, run_handle: str) -> ManagedRun:
        now = self._clock()
        with self._table_lock:
            self._prune(now)
            if run_handle in self._runs:
                raise ManagedRunRegistryError("run handle is already live")
            live = sum(1 for entry in self._runs.values() if entry.run.is_live)
            held = sum(
                1
                for state in self._slot_reservations.values()
                if not state.released and state.bound_run_handle is None
            )
            if live + held >= MAX_LIVE_MANAGED_RUNS:
                raise ManagedRunRegistryError("live managed runs exceed their bound")
            run = ManagedRun(run_handle=run_handle)
            self._runs[run_handle] = _Entry(run=run, touched_at=now)
            return run

    def reserve_slot(
        self,
        *,
        reservation_id: str,
        parent_sequence_id: str,
        parent_authorization_fingerprint: str,
        expires_at: float,
    ) -> ManagedRunSlotReservationV1:
        token = ManagedRunSlotReservationV1(
            reservation_id=reservation_id,
            parent_sequence_id=parent_sequence_id,
            parent_authorization_fingerprint=parent_authorization_fingerprint,
            expires_at=expires_at,
        )
        now = self._clock()
        with self._table_lock:
            self._prune(now)
            if float(token.expires_at) <= now:
                raise ManagedRunRegistryError("managed run slot reservation expired")
            existing = self._slot_reservations.get(reservation_id)
            if existing is not None:
                if existing.token.fingerprint != token.fingerprint:
                    raise ManagedRunRegistryError("managed run slot reservation conflict")
                return existing.snapshot()
            live = sum(1 for entry in self._runs.values() if entry.run.is_live)
            held = sum(
                1
                for state in self._slot_reservations.values()
                if not state.released and state.bound_run_handle is None
            )
            if live + held >= MAX_LIVE_MANAGED_RUNS:
                raise ManagedRunRegistryError("live managed runs exceed their bound")
            state = _SlotReservationState(token=token)
            self._slot_reservations[reservation_id] = state
            return state.snapshot()

    def create_reserved(
        self,
        run_handle: str,
        reservation: ManagedRunSlotReservationV1,
    ) -> ManagedRun:
        now = self._clock()
        with self._table_lock:
            self._prune(now)
            if type(reservation) is not ManagedRunSlotReservationV1:
                raise ManagedRunRegistryError("managed run slot reservation type")
            state = self._slot_reservations.get(reservation.reservation_id)
            if state is None or state.token.fingerprint != reservation.fingerprint:
                raise ManagedRunRegistryError("managed run slot reservation unavailable")
            if state.released or float(state.token.expires_at) <= now:
                raise ManagedRunRegistryError("managed run slot reservation expired")
            if state.bound_run_handle is not None:
                raise ManagedRunRegistryError("managed run slot is already bound")
            if run_handle in self._runs:
                raise ManagedRunRegistryError("run handle is already live")
            run = ManagedRun(run_handle=run_handle)
            self._runs[run_handle] = _Entry(run=run, touched_at=now)
            state.bound_run_handle = run_handle
            self._run_reservations[run_handle] = reservation.reservation_id
            return run

    def release_slot_reservation(
        self,
        reservation: ManagedRunSlotReservationV1,
    ) -> ManagedRunSlotReservationV1:
        now = self._clock()
        with self._table_lock:
            self._prune(now)
            if type(reservation) is not ManagedRunSlotReservationV1:
                raise ManagedRunRegistryError("managed run slot reservation type")
            state = self._slot_reservations.get(reservation.reservation_id)
            if state is None or state.token.fingerprint != reservation.fingerprint:
                raise ManagedRunRegistryError("managed run slot reservation unavailable")
            if state.bound_run_handle is not None:
                raise ManagedRunRegistryError("reserved managed run is live")
            state.released = True
            return state.snapshot()

    def read(self, run_handle: str) -> ManagedRun:
        """Read one run under its own lock, so a half-applied join is never observed."""

        entry = self._entry(run_handle)
        with entry.lock:
            return entry.run

    def release(self, run_handle: str) -> None:
        now = self._clock()
        with self._table_lock:
            self._prune(now)
            if self._runs.pop(run_handle, None) is None:
                raise ManagedRunRegistryError("run handle is unavailable")
            self._unbind_reserved_run(run_handle)
            self._tombstones[run_handle] = now

    def _unbind_reserved_run(self, run_handle: str) -> None:
        reservation_id = self._run_reservations.pop(run_handle, None)
        if reservation_id is None:
            return
        state = self._slot_reservations.get(reservation_id)
        if state is not None and state.bound_run_handle == run_handle:
            state.bound_run_handle = None

    def detach(self, run_handle: str, *, deadline: float) -> float:
        """Retain a run whose client has left, under a deadline fixed at the first detach.

        The opposite number of `release`: nothing is popped, no transition fires, the aggregate and
        its accepted `prompt_id` stay exactly as they are, and a late terminal from the host still
        lands. Only the expiry rule changes.

        `deadline` is absolute and on this registry's own clock. Returns the deadline actually in
        force, which is the first one ever set for this run.

        CRITICAL: a repeat detach returns the existing deadline and must never overwrite it. A
        client that retries, or a view that unmounts twice, would otherwise renew the lease by
        replaying a request whose entire contract is that it changes nothing.
        """

        now = self._clock()
        with self._table_lock:
            self._prune(now)
            entry = self._runs.get(run_handle)
            if entry is None:
                if run_handle in self._tombstones:
                    raise ManagedRunRegistryError("run handle was released")
                raise ManagedRunRegistryError("run handle is unavailable")
            if entry.detached_deadline is None:
                entry.detached_deadline = deadline
            entry.parent_transition_protected = False
            return entry.detached_deadline

    def protect_for_parent_transition(self, run_handle: str) -> None:
        """Keep one M26 child until its parent-first expiry transaction can classify it."""

        entry = self._entry(run_handle)
        with entry.lock:
            entry.parent_transition_protected = True

    def retained_until(self, run_handle: str) -> float | None:
        """The fixed deadline a detached run is retained to, or `None`.

        `None` means this registry is not keeping the run alive past its own rules: it was never
        detached, or it is already gone. A float is a promise that the row still exists and will
        until that moment on this registry's clock.

        CRITICAL: this exists so the *coordinator* does not grow a second lease. Its row is the only
        way to address a run, and it expires 900 seconds after the last publish -- which, after a
        client detaches, never happens again. Without this question being asked, a detached run
        becomes unaddressable long before its lease ends while still holding one of the sixteen
        live slots, and a returning client is told `run_gone` about a run that exists and has its
        result. Never answer it from a copy of the deadline; read the row that owns it.
        """

        now = self._clock()
        with self._table_lock:
            self._prune(now)
            entry = self._runs.get(run_handle)
            if entry is None:
                return None
            return entry.detached_deadline

    def live_handles(self) -> tuple[str, ...]:
        with self._table_lock:
            return tuple(handle for handle, entry in self._runs.items() if entry.run.is_live)

    @property
    def dropped_events(self) -> int:
        """Evidence the sink refused. Counted, never raised: diagnostics are not authority."""

        return self._dropped_events

    def _entry(self, run_handle: str) -> _Entry:
        now = self._clock()
        # CRITICAL: lock ordering is table-then-run, never the reverse, and no caller holds two
        # run locks. Taking a run lock first and then the table lock -- which a "find and prune
        # while holding the run" refactor produces naturally -- deadlocks against a concurrent
        # prune, and only under real concurrency: no focused test reproduces it, which is why the
        # ordering is stated here rather than left to be inferred.
        with self._table_lock:
            self._prune(now)
            entry = self._runs.get(run_handle)
            if entry is None:
                if run_handle in self._tombstones:
                    raise ManagedRunRegistryError("run handle was released")
                raise ManagedRunRegistryError("run handle is unavailable")
            # `touched_at` is refreshed unconditionally and that is deliberate: `_prune` is the
            # single authority on expiry and simply does not consult it for a detached row. A
            # second guard here would be inert, and an inert guard reads as a defence that is not
            # there.
            entry.touched_at = now
            self._runs.move_to_end(run_handle)
            return entry

    # -- run level ------------------------------------------------------------------------

    def advance(self, run_handle: str, trigger: ManagedRunTrigger, **fields: object) -> ManagedRun:
        """Apply one lifecycle transition to one run, under that run's own lock."""

        entry = self._entry(run_handle)
        with entry.lock:
            updated = advance(entry.run, trigger, **fields)  # type: ignore[arg-type]
            entry.run = updated
        self._emit(updated)
        return updated

    def cancel(self, run_handle: str) -> ManagedRun:
        """Cancel a run, deciding the cancelled/unknown-ownership question exactly once.

        CRITICAL: the caller reports `run.state`, never a separately computed disposition. The
        coordinator's old `_cancel` decided the same question twice with two different predicates
        and could return a disposition that contradicted the state it accompanied.
        """

        entry = self._entry(run_handle)
        with entry.lock:
            updated = advance(entry.run, cancel_trigger(entry.run.state))
            entry.run = updated
        self._emit(updated)
        return updated

    def attach_geometry(self, run_handle: str, receipt: str) -> ManagedRun:
        entry = self._entry(run_handle)
        with entry.lock:
            updated = attach_geometry_receipt(entry.run, receipt)
            entry.run = updated
        return updated

    def _emit(self, run: ManagedRun) -> None:
        if self._sink is None or not run.events:
            return
        try:
            self._sink(run.events[-1])
        except Exception:  # noqa: BLE001
            # CRITICAL: an unavailable evidence sink is counted, never raised. Transition evidence
            # is diagnostics; letting it fail a transition would make the observability seam the
            # reason a run stops advancing, which is exactly backwards.
            self._dropped_events += 1


def is_terminal(run: ManagedRun) -> bool:
    return run.state is not ManagedRunState.CREATED and not run.is_live


__all__ = [
    "MANAGED_RUN_TOMBSTONE_TTL_SECONDS",
    "MANAGED_RUN_TTL_SECONDS",
    "MAX_MANAGED_RUN_TOMBSTONES",
    "ManagedRunRegistry",
    "ManagedRunRegistryError",
    "ManagedRunSlotReservationV1",
    "is_terminal",
]
