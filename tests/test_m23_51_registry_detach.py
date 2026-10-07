"""M23-51 focused tests for detaching a run in the registry: retention, and a deadline that holds.

`release` pops a run unconditionally. Detach is its opposite number: the row stays, the aggregate
stays, the prompt id stays, and the only thing that changes is that expiry stops being governed by
the client's attention and starts being governed by a number fixed when the prompt was submitted.

The existing sliding TTL is correct for an *attached* run -- a client that is still watching is
evidence the run still matters -- and `test_touching_a_run_defers_its_expiry` in
`tests/test_managed_run_registry.py` pins it. These tests pin that detaching turns it off for that
row and only for that row.
"""

from __future__ import annotations

import unittest

from comfyui_h3_context.adapters.managed_run_registry import (
    MANAGED_RUN_TTL_SECONDS,
    ManagedRunRegistry,
    ManagedRunRegistryError,
)
from comfyui_h3_context.core.managed_run import (
    MAX_LIVE_MANAGED_RUNS,
    ManagedRunState,
    ManagedRunTrigger,
)

T = ManagedRunTrigger
S = ManagedRunState


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


#: A deadline far enough ahead of `_Clock`'s epoch to be irrelevant to the case under test.
#: CRITICAL: deadlines are absolute and on the *registry's* clock, which defaults to
#: `time.monotonic` -- a number like 9_999.0 is already in the past on a machine that has been up
#: for three hours, and a test that used it would expire its run before its first assertion.
FAR_FUTURE = 11_000.0


def _registry() -> tuple[ManagedRunRegistry, _Clock]:
    clock = _Clock()
    return ManagedRunRegistry(clock=clock), clock


def _submitted(registry: ManagedRunRegistry, handle: str = "run_1") -> None:
    registry.create(handle)
    registry.advance(handle, T.STAGE_CONTEXT, context_revision=1)
    registry.advance(handle, T.CREATE_PRODUCTION, production_workspace="ws_1", segment_count=1)
    registry.advance(handle, T.PREPARE_SEQUENCE, prepared_sequence="seq_1")
    registry.advance(handle, T.RECORD_SUBMISSION, prompt_id="prompt_1")


class DetachRetainsTests(unittest.TestCase):
    def test_a_detached_run_is_still_readable_and_still_holds_its_prompt(self) -> None:
        registry, _clock = _registry()
        _submitted(registry)
        registry.detach("run_1", deadline=FAR_FUTURE)
        run = registry.read("run_1")
        self.assertEqual(run.state, S.SUBMITTED)
        self.assertEqual(run.prompt_id, "prompt_1")

    def test_detaching_fires_no_lifecycle_transition(self) -> None:
        # CRITICAL: a detach is not a cancellation and must leave no trace in the transition
        # history. If it ever fires one, a client leaving would be indistinguishable from the user
        # pressing cancel, which is the M23-51 defect stated in lifecycle terms.
        registry, _clock = _registry()
        _submitted(registry)
        before = registry.read("run_1").events
        registry.detach("run_1", deadline=FAR_FUTURE)
        self.assertEqual(registry.read("run_1").events, before)
        self.assertEqual(registry.read("run_1").state, S.SUBMITTED)

    def test_a_detached_run_still_advances_when_the_host_reports_back(self) -> None:
        # The whole point of retaining it: a late terminal must still land.
        registry, _clock = _registry()
        _submitted(registry)
        registry.detach("run_1", deadline=FAR_FUTURE)
        registry.advance("run_1", T.RECORD_RUNNING)
        registry.advance("run_1", T.RECORD_ARTIFACT, artifact_receipt="receipt_1")
        registry.advance("run_1", T.RECORD_SUCCEEDED)
        self.assertEqual(registry.read("run_1").state, S.TERMINAL_SUCCEEDED)

    def test_a_detached_run_can_still_be_released(self) -> None:
        registry, _clock = _registry()
        _submitted(registry)
        registry.detach("run_1", deadline=FAR_FUTURE)
        registry.release("run_1")
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")

    def test_detaching_an_unknown_or_released_run_is_refused(self) -> None:
        registry, _clock = _registry()
        with self.assertRaises(ManagedRunRegistryError):
            registry.detach("run_absent", deadline=1.0)
        _submitted(registry)
        registry.release("run_1")
        with self.assertRaises(ManagedRunRegistryError):
            registry.detach("run_1", deadline=1.0)


class TheDeadlineDoesNotSlideTests(unittest.TestCase):
    def test_reading_a_detached_run_does_not_defer_its_expiry(self) -> None:
        # CRITICAL: this is the security property of the whole lease. `_entry` refreshes
        # `touched_at` on every read, so before this item a detached run was renewed by the very
        # polling a returning client performs -- an abandoned run would then hold one of the
        # sixteen live slots indefinitely. Never let a detached row's expiry consult `touched_at`.
        clock = _Clock()
        registry = ManagedRunRegistry(clock=clock)
        _submitted(registry)
        registry.detach("run_1", deadline=clock.now + 100.0)
        for step in range(1, 10):
            clock.now = 1_000.0 + step * 10.0
            registry.read("run_1")
        clock.now = 1_000.0 + 100.1
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")

    def test_an_attached_run_keeps_its_sliding_ttl(self) -> None:
        # The counterpart, asserted here so the two behaviours are pinned side by side and a future
        # simplification cannot quietly make both rows behave the same way.
        clock = _Clock()
        registry = ManagedRunRegistry(clock=clock)
        _submitted(registry)
        for step in range(1, 5):
            clock.now = 1_000.0 + step * MANAGED_RUN_TTL_SECONDS * 0.5
            self.assertEqual(registry.read("run_1").state, S.SUBMITTED)

    def test_a_detached_run_expires_at_its_deadline_even_if_never_read(self) -> None:
        clock = _Clock()
        registry = ManagedRunRegistry(clock=clock)
        _submitted(registry)
        registry.detach("run_1", deadline=1_050.0)
        clock.now = 1_049.0
        self.assertEqual(registry.read("run_1").state, S.SUBMITTED)
        clock.now = 1_051.0
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")

    def test_a_detached_run_expires_into_a_tombstone_not_into_nothing(self) -> None:
        # A returning client must be told "gone", never "never existed". The two are different
        # answers and only one of them is honest about a result that once existed.
        clock = _Clock()
        registry = ManagedRunRegistry(clock=clock)
        _submitted(registry)
        registry.detach("run_1", deadline=1_050.0)
        clock.now = 1_051.0
        with self.assertRaises(ManagedRunRegistryError) as caught:
            registry.read("run_1")
        self.assertIn("released", str(caught.exception))

    def test_re_detaching_never_extends_the_lease(self) -> None:
        # CRITICAL: idempotent replay must not be a renewal mechanism. A client that retries its
        # detach -- or a page that unmounts twice -- would otherwise hold the row open forever by
        # repeating a request that is supposed to change nothing.
        clock = _Clock()
        registry = ManagedRunRegistry(clock=clock)
        _submitted(registry)
        self.assertEqual(registry.detach("run_1", deadline=1_050.0), 1_050.0)
        for later in (2_000.0, FAR_FUTURE):
            self.assertEqual(registry.detach("run_1", deadline=later), 1_050.0)
        clock.now = 1_051.0
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")

    def test_a_deadline_already_in_the_past_expires_the_run_at_the_next_prune(self) -> None:
        clock = _Clock()
        registry = ManagedRunRegistry(clock=clock)
        _submitted(registry)
        registry.detach("run_1", deadline=1.0)
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")


class DetachedRunsStayInsideTheBoundTests(unittest.TestCase):
    def test_a_detached_run_still_occupies_a_live_slot(self) -> None:
        # Plan section 3.3: a detached row is not copied to a second, unbounded store. It is the
        # same row under the same cap, so capacity exhaustion refuses a new run rather than
        # evicting a host owner.
        registry, _clock = _registry()
        for index in range(MAX_LIVE_MANAGED_RUNS):
            _submitted(registry, f"run_{index}")
            registry.detach(f"run_{index}", deadline=FAR_FUTURE)
        with self.assertRaises(ManagedRunRegistryError):
            registry.create("run_overflow")

    def test_capacity_exhaustion_never_evicts_a_detached_host_owner(self) -> None:
        registry, _clock = _registry()
        for index in range(MAX_LIVE_MANAGED_RUNS):
            _submitted(registry, f"run_{index}")
        registry.detach("run_0", deadline=FAR_FUTURE)
        with self.assertRaises(ManagedRunRegistryError):
            registry.create("run_overflow")
        self.assertEqual(registry.read("run_0").prompt_id, "prompt_1")

    def test_detached_handles_are_reported_as_live(self) -> None:
        registry, _clock = _registry()
        _submitted(registry)
        registry.detach("run_1", deadline=FAR_FUTURE)
        self.assertIn("run_1", registry.live_handles())


class TheRetentionProbeAnswersForTheCoordinatorTests(unittest.TestCase):
    """The one question the coordinator asks instead of keeping a second lease of its own."""

    def test_an_attached_run_is_not_retained_by_this_registry(self) -> None:
        registry, _clock = _registry()
        _submitted(registry)
        self.assertIsNone(registry.retained_until("run_1"))

    def test_a_detached_run_reports_the_deadline_actually_in_force(self) -> None:
        registry, _clock = _registry()
        _submitted(registry)
        registry.detach("run_1", deadline=1_050.0)
        self.assertEqual(registry.retained_until("run_1"), 1_050.0)
        registry.detach("run_1", deadline=FAR_FUTURE)
        self.assertEqual(registry.retained_until("run_1"), 1_050.0)

    def test_an_expired_or_absent_run_is_not_retained(self) -> None:
        # CRITICAL: `None` must mean "this registry is not keeping it alive", never "I do not know".
        # The coordinator drops its row on `None`, so an ambiguous answer would either strand rows
        # forever or drop them while the run they address still exists.
        registry, clock = _registry()
        _submitted(registry)
        registry.detach("run_1", deadline=1_050.0)
        clock.now = 1_051.0
        self.assertIsNone(registry.retained_until("run_1"))
        self.assertIsNone(registry.retained_until("run_never_existed"))


if __name__ == "__main__":
    unittest.main()
