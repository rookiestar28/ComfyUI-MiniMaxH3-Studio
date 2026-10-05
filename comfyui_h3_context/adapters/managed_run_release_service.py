"""The single owner of the release/detach decision and of the mutation it implies.

Plan section 3.4: the HTTP route, the coordinator table and the ManagedRun registry must not each
derive their own predicate. Before M23-51 the coordinator's `release_sequence` branch *was* the
predicate -- it had none, and dropped whatever it was given -- and the obvious repair is to add a
state check to that branch. That repair is the wrong shape: the same question would then be asked
again by whatever next needs it, exactly as the cancel question was asked twice with two different
predicates before `HOST_MAY_HOLD_THE_PROMPT` ended it.

So the decision lives in `core/managed_run_release.py` and the mutation lives here, together, and
callers get an outcome rather than a verdict to act on themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..core.managed_run_release import (
    DetachedRunProjectionV1,
    ReleaseDisposition,
    ReleaseIntent,
    ReleaseRefusal,
    decide_release,
    detached_deadline,
    detached_projection,
    terminal_fingerprint,
)
from .managed_run_registry import ManagedRunRegistry, ManagedRunRegistryError

#: Refusals that mean the run is past its lease. Everything else is a precondition failure.
_GONE_REFUSALS = frozenset({ReleaseRefusal.GONE})


class ManagedRunReleaseRefused(Exception):
    """A release or detach that changed nothing, carrying the reason it changed nothing."""

    def __init__(self, refusal: ReleaseRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal

    @property
    def code(self) -> str:
        return f"release_{self.refusal.value}"

    @property
    def status(self) -> int:
        return 410 if self.refusal in _GONE_REFUSALS else 409


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    """A decoded V1 or V2 request. Decoding and validation happen before the service sees it."""

    run_handle: str
    intent: ReleaseIntent
    observed_terminal: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseOutcome:
    disposition: str
    removes_authority: bool
    terminal_fingerprint: str | None
    projection: DetachedRunProjectionV1 | None = None
    deadline: float | None = None


class ManagedRunReleaseService:
    """Decide once, mutate once, under the caller's existing lock."""

    def __init__(self, registry: ManagedRunRegistry) -> None:
        self._registry = registry

    def apply(
        self,
        request: ReleaseRequest,
        *,
        submitted_at: float | None,
        timeout_ms: int,
        parent_absolute_deadline: float | None = None,
        removal_transaction: Callable[[Callable[[], None]], None] | None = None,
    ) -> ReleaseOutcome:
        """Apply one release/detach request, or raise `ManagedRunReleaseRefused` changing nothing.

        CRITICAL: the run is read, decided and mutated without releasing the caller's lock, and the
        caller must not re-derive the outcome afterwards. A caller that reads the state, decides
        for itself and then calls in has reintroduced the time-of-check/time-of-use window this
        service exists to close: between its read and its call, the host's terminal can land and
        turn a detach into a destructive cleanup.
        """
        try:
            run = self._registry.read(request.run_handle)
        except ManagedRunRegistryError as exc:
            raise ManagedRunReleaseRefused(ReleaseRefusal.GONE) from exc

        decision = decide_release(run, request.intent, request.observed_terminal)
        if decision.refusal is not None:
            raise ManagedRunReleaseRefused(decision.refusal)
        disposition = decision.disposition
        assert disposition is not None  # noqa: S101 - guaranteed by ReleaseDecision.__post_init__

        if decision.removes_authority:
            # CRITICAL: guarded exactly like the read above, and for the same reason. The registry
            # can prune between the read and here, from a thread this caller's lock does not cover
            # -- the production registry's liveness probe reaches `_entry` -> `_prune` while holding
            # only its own lock. A run that went away in that window is `gone`, which is a 410;
            # letting the registry error escape would turn it into an internal failure.
            def release_live_sequence() -> None:
                try:
                    self._registry.release(run.run_handle)
                except ManagedRunRegistryError as exc:
                    raise ManagedRunReleaseRefused(ReleaseRefusal.GONE) from exc

            if removal_transaction is None:
                release_live_sequence()
            else:
                # IMPORTANT: M26 owns a private Production row per serial child. Let that owner
                # bracket this service's exact ManagedRun mutation, while this service remains the
                # sole authority deciding whether M23-51 permits removal at all.
                removal_transaction(release_live_sequence)
            return ReleaseOutcome(
                disposition=ReleaseDisposition.RELEASED.value,
                removes_authority=True,
                terminal_fingerprint=terminal_fingerprint(run),
            )

        if submitted_at is None:
            # CRITICAL: a lease must be dated from the moment the host accepted the prompt, and
            # there is no honest substitute. Falling back to "now" here would make the deadline a
            # function of when the client happened to leave, which is the sliding behaviour the
            # fixed lease exists to remove -- and it would silently succeed, so nothing downstream
            # could tell the difference.
            raise ManagedRunReleaseRefused(ReleaseRefusal.NOT_HOST_OWNED)

        deadline = detached_deadline(
            submitted_at=submitted_at,
            timeout_ms=timeout_ms,
            parent_absolute_deadline=parent_absolute_deadline,
        )
        try:
            effective = self._registry.detach(run.run_handle, deadline=deadline)
        except ManagedRunRegistryError as exc:
            raise ManagedRunReleaseRefused(ReleaseRefusal.GONE) from exc
        return ReleaseOutcome(
            disposition=disposition.value,
            removes_authority=False,
            terminal_fingerprint=terminal_fingerprint(run),
            projection=detached_projection(run, deadline=effective),
            deadline=effective,
        )


__all__ = [
    "ManagedRunReleaseRefused",
    "ManagedRunReleaseService",
    "ReleaseOutcome",
    "ReleaseRequest",
]
