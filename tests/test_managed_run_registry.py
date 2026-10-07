"""M23-31 focused tests for the ManagedRun registry: bounds, locking, pruning and evidence."""

from __future__ import annotations

import threading
import unittest

from comfyui_h3_context.adapters.managed_run_registry import (
    MANAGED_RUN_TTL_SECONDS,
    ManagedRunRegistry,
    ManagedRunRegistryError,
)
from comfyui_h3_context.core.managed_run import (
    MAX_LIVE_MANAGED_RUNS,
    ManagedRunError,
    ManagedRunState,
    ManagedRunTrigger,
    TransitionEvent,
)

T = ManagedRunTrigger
S = ManagedRunState


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _prepared(registry: ManagedRunRegistry, handle: str = "run_1") -> None:
    registry.create(handle)
    registry.advance(handle, T.STAGE_CONTEXT, context_revision=1)
    registry.advance(handle, T.CREATE_PRODUCTION, production_workspace="ws_1", segment_count=1)
    registry.advance(handle, T.PREPARE_SEQUENCE, prepared_sequence="seq_1")


class RegistryBoundTests(unittest.TestCase):
    def test_live_runs_are_capped(self) -> None:
        registry = ManagedRunRegistry()
        for index in range(MAX_LIVE_MANAGED_RUNS):
            registry.create(f"run_{index}")
        with self.assertRaises(ManagedRunRegistryError):
            registry.create("run_overflow")

    def test_a_terminal_run_does_not_occupy_a_live_slot(self) -> None:
        # The cap counts live runs, so finishing one must make room. Counting stored rows instead
        # would let a queue of completed runs block every new one until the TTL expired.
        registry = ManagedRunRegistry()
        for index in range(MAX_LIVE_MANAGED_RUNS):
            registry.create(f"run_{index}")
        registry.advance("run_0", T.STAGE_CONTEXT, context_revision=1)
        registry.advance("run_0", T.CANCEL_BEFORE_HOST)
        self.assertEqual(registry.create("run_overflow").run_handle, "run_overflow")

    def test_a_duplicate_handle_is_refused(self) -> None:
        registry = ManagedRunRegistry()
        registry.create("run_1")
        with self.assertRaises(ManagedRunRegistryError):
            registry.create("run_1")

    def test_an_unknown_handle_and_a_released_one_are_distinguishable(self) -> None:
        registry = ManagedRunRegistry()
        registry.create("run_1")
        registry.release("run_1")
        with self.assertRaises(ManagedRunRegistryError) as released:
            registry.read("run_1")
        with self.assertRaises(ManagedRunRegistryError) as unknown:
            registry.read("run_absent")
        self.assertIn("released", str(released.exception))
        self.assertIn("unavailable", str(unknown.exception))


class RegistryPruneTests(unittest.TestCase):
    def test_a_run_expires_after_its_ttl(self) -> None:
        clock = _Clock()
        registry = ManagedRunRegistry(clock=clock)
        registry.create("run_1")
        clock.now += MANAGED_RUN_TTL_SECONDS + 1
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")

    def test_touching_a_run_defers_its_expiry(self) -> None:
        clock = _Clock()
        registry = ManagedRunRegistry(clock=clock)
        registry.create("run_1")
        clock.now += MANAGED_RUN_TTL_SECONDS - 1
        registry.advance("run_1", T.STAGE_CONTEXT, context_revision=1)
        clock.now += MANAGED_RUN_TTL_SECONDS - 1
        self.assertEqual(registry.read("run_1").state, S.CONTEXT_READY)


class RegistryTransitionTests(unittest.TestCase):
    def test_a_run_advances_through_the_registry(self) -> None:
        registry = ManagedRunRegistry()
        _prepared(registry)
        run = registry.advance("run_1", T.RECORD_SUBMISSION, prompt_id="prompt_1")
        self.assertEqual(run.state, S.SUBMITTED)
        self.assertEqual(registry.read("run_1").state, S.SUBMITTED)

    def test_cancel_decides_the_question_once_and_the_state_is_the_answer(self) -> None:
        registry = ManagedRunRegistry()
        _prepared(registry)
        self.assertEqual(registry.cancel("run_1").state, S.TERMINAL_CANCELLED)

        registry = ManagedRunRegistry()
        _prepared(registry, "run_2")
        registry.advance("run_2", T.RECORD_SUBMISSION, prompt_id="prompt_1")
        self.assertEqual(registry.cancel("run_2").state, S.TERMINAL_UNKNOWN_OWNERSHIP)

    def test_geometry_receipts_attach_and_are_not_re_claimable(self) -> None:
        registry = ManagedRunRegistry()
        registry.create("run_1")
        self.assertEqual(
            registry.attach_geometry("run_1", "receipt_1").geometry_receipts, ("receipt_1",)
        )
        # The re-claim rule is a core invariant of the aggregate, not a registry bound, so it
        # surfaces as the core error rather than the registry one.
        with self.assertRaises(ManagedRunError):
            registry.attach_geometry("run_1", "receipt_1")


class RegistryEvidenceTests(unittest.TestCase):
    def test_every_transition_reaches_the_sink(self) -> None:
        seen: list[TransitionEvent] = []
        registry = ManagedRunRegistry(sink=seen.append)
        _prepared(registry)
        self.assertEqual(
            [event.trigger for event in seen],
            [T.STAGE_CONTEXT.value, T.CREATE_PRODUCTION.value, T.PREPARE_SEQUENCE.value],
        )

    def test_an_unavailable_sink_is_counted_and_never_raised(self) -> None:
        def sink(event: TransitionEvent) -> None:
            raise RuntimeError("sink unavailable")

        registry = ManagedRunRegistry(sink=sink)
        _prepared(registry)
        self.assertEqual(registry.read("run_1").state, S.SEQUENCE_PREPARED)
        self.assertEqual(registry.dropped_events, 3)


class RegistryConcurrencyTests(unittest.TestCase):
    def test_concurrent_transitions_on_one_run_serialize_without_losing_evidence(self) -> None:
        registry = ManagedRunRegistry()
        registry.create("run_1")
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(revision: int) -> None:
            try:
                barrier.wait(timeout=10)
                registry.advance("run_1", T.STAGE_CONTEXT, context_revision=revision)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        run = registry.read("run_1")
        self.assertEqual(run.state, S.CONTEXT_READY)
        self.assertEqual(len(run.events), 8)
        self.assertEqual([event.sequence for event in run.events], list(range(8)))

    def test_transitions_on_different_runs_do_not_block_each_other(self) -> None:
        # The point of the per-run lock. A held run lock must not stop another run advancing; if
        # it did, the two-level design would have bought nothing over the global lock it replaced.
        registry = ManagedRunRegistry()
        registry.create("run_1")
        registry.create("run_2")
        entry = registry._entry("run_1")
        advanced = threading.Event()

        def worker() -> None:
            registry.advance("run_2", T.STAGE_CONTEXT, context_revision=1)
            advanced.set()

        with entry.lock:
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(advanced.wait(timeout=5))
        thread.join(timeout=5)
        self.assertEqual(registry.read("run_2").state, S.CONTEXT_READY)


if __name__ == "__main__":
    unittest.main()
