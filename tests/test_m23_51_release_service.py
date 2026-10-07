"""M23-51 focused tests for the one service that owns the release/detach mutation.

The pure table says what should happen. These say that nothing else happens: a refusal leaves the
registry byte-identical, a detach leaves the run and its prompt in place, and a release is the only
path that removes anything.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import replace

from comfyui_h3_context.adapters.managed_run_registry import (
    ManagedRunRegistry,
    ManagedRunRegistryError,
)
from comfyui_h3_context.adapters.managed_run_release_service import (
    ManagedRunReleaseRefused,
    ManagedRunReleaseService,
    ReleaseOutcome,
    ReleaseRequest,
)
from comfyui_h3_context.core.managed_run import ManagedRunState, ManagedRunTrigger
from comfyui_h3_context.core.managed_run_release import (
    DETACHED_GRACE_SECONDS,
    ReleaseIntent,
    ReleaseRefusal,
    terminal_fingerprint,
)

T = ManagedRunTrigger
S = ManagedRunState
TIMEOUT_MS = 120_000


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _fixture(state: S) -> tuple[ManagedRunRegistry, ManagedRunReleaseService, _Clock]:
    clock = _Clock()
    registry = ManagedRunRegistry(clock=clock)
    registry.create("run_1")
    if state is S.CREATED:
        return registry, ManagedRunReleaseService(registry), clock
    registry.advance("run_1", T.STAGE_CONTEXT, context_revision=1)
    registry.advance("run_1", T.CREATE_PRODUCTION, production_workspace="ws_1", segment_count=1)
    if state is S.PRODUCTION_READY:
        return registry, ManagedRunReleaseService(registry), clock
    registry.advance("run_1", T.PREPARE_SEQUENCE, prepared_sequence="seq_1")
    if state is S.SEQUENCE_PREPARED:
        return registry, ManagedRunReleaseService(registry), clock
    registry.advance("run_1", T.RECORD_SUBMISSION, prompt_id="prompt_1")
    if state is S.SUBMITTED:
        return registry, ManagedRunReleaseService(registry), clock
    registry.advance("run_1", T.RECORD_RUNNING)
    if state is S.RUNNING:
        return registry, ManagedRunReleaseService(registry), clock
    registry.advance("run_1", T.RECORD_ARTIFACT, artifact_receipt="receipt_1")
    if state is S.ARTIFACT_RECORDED:
        return registry, ManagedRunReleaseService(registry), clock
    registry.advance("run_1", T.RECORD_SUCCEEDED)
    return registry, ManagedRunReleaseService(registry), clock


def _apply(
    service: ManagedRunReleaseService, intent: ReleaseIntent, **kwargs: object
) -> ReleaseOutcome:
    request = ReleaseRequest(
        run_handle="run_1",
        intent=intent,
        observed_terminal=kwargs.pop("observed_terminal", None),  # type: ignore[arg-type]
    )
    return service.apply(
        request,
        submitted_at=kwargs.pop("submitted_at", 1_000.0),  # type: ignore[arg-type]
        timeout_ms=kwargs.pop("timeout_ms", TIMEOUT_MS),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class ARefusalChangesNothingTests(unittest.TestCase):
    def test_refusing_a_host_owned_cleanup_leaves_the_run_exactly_as_it_was(self) -> None:
        registry, service, _ = _fixture(S.RUNNING)
        before = registry.read("run_1")
        with self.assertRaises(ManagedRunReleaseRefused) as caught:
            _apply(service, ReleaseIntent.CLEANUP_PRE_SUBMIT)
        self.assertEqual(caught.exception.refusal, ReleaseRefusal.HOST_OWNED)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "release_host_owned")
        self.assertEqual(registry.read("run_1"), before)

    def test_refusing_a_detach_before_submission_leaves_the_run_exactly_as_it_was(self) -> None:
        registry, service, _ = _fixture(S.SEQUENCE_PREPARED)
        before = registry.read("run_1")
        with self.assertRaises(ManagedRunReleaseRefused) as caught:
            _apply(service, ReleaseIntent.DETACH_CLIENT)
        self.assertEqual(caught.exception.refusal, ReleaseRefusal.NOT_HOST_OWNED)
        self.assertEqual(registry.read("run_1"), before)

    def test_a_bad_terminal_proof_leaves_the_terminal_in_place(self) -> None:
        registry, service, _ = _fixture(S.TERMINAL_SUCCEEDED)
        before = registry.read("run_1")
        with self.assertRaises(ManagedRunReleaseRefused) as caught:
            _apply(service, ReleaseIntent.CLEANUP_TERMINAL, observed_terminal="not-the-digest")
        self.assertEqual(caught.exception.refusal, ReleaseRefusal.WRONG_INTENT)
        self.assertEqual(registry.read("run_1"), before)

    def test_an_absent_run_is_gone_with_a_410(self) -> None:
        registry, service, _ = _fixture(S.SUBMITTED)
        registry.release("run_1")
        with self.assertRaises(ManagedRunReleaseRefused) as caught:
            _apply(service, ReleaseIntent.DETACH_CLIENT)
        self.assertEqual(caught.exception.refusal, ReleaseRefusal.GONE)
        self.assertEqual(caught.exception.status, 410)
        self.assertEqual(caught.exception.code, "release_gone")


class DetachRetainsEverythingTests(unittest.TestCase):
    def test_a_running_run_detaches_and_keeps_its_prompt(self) -> None:
        registry, service, _ = _fixture(S.RUNNING)
        outcome = _apply(service, ReleaseIntent.DETACH_CLIENT)
        self.assertEqual(outcome.disposition, "detached")
        self.assertFalse(outcome.removes_authority)
        self.assertEqual(registry.read("run_1").prompt_id, "prompt_1")
        self.assertEqual(registry.read("run_1").state, S.RUNNING)

    def test_the_lease_is_the_submission_time_not_the_detach_time(self) -> None:
        # CRITICAL: the deadline must be dated from the accepted prompt, never from when the client
        # happened to leave. A client that leaves an hour late would otherwise buy the run another
        # full window simply by leaving late.
        registry, service, clock = _fixture(S.RUNNING)
        clock.now = 1_500.0
        outcome = _apply(service, ReleaseIntent.DETACH_CLIENT, submitted_at=1_000.0)
        self.assertEqual(outcome.deadline, 1_000.0 + 120.0 + DETACHED_GRACE_SECONDS)

    def test_a_detach_with_no_recorded_submission_is_refused_rather_than_dated_from_now(
        self,
    ) -> None:
        registry, service, _ = _fixture(S.RUNNING)
        with self.assertRaises(ManagedRunReleaseRefused) as caught:
            _apply(service, ReleaseIntent.DETACH_CLIENT, submitted_at=None)
        self.assertEqual(caught.exception.refusal, ReleaseRefusal.NOT_HOST_OWNED)
        self.assertEqual(registry.read("run_1").state, S.RUNNING)

    def test_a_settled_terminal_detaches_without_being_removed(self) -> None:
        registry, service, _ = _fixture(S.TERMINAL_SUCCEEDED)
        outcome = _apply(service, ReleaseIntent.DETACH_CLIENT)
        self.assertEqual(outcome.disposition, "detached_terminal")
        self.assertFalse(outcome.removes_authority)
        self.assertEqual(registry.read("run_1").state, S.TERMINAL_SUCCEEDED)

    def test_the_outcome_carries_a_content_free_projection(self) -> None:
        registry, service, _ = _fixture(S.ARTIFACT_RECORDED)
        outcome = _apply(service, ReleaseIntent.DETACH_CLIENT)
        assert outcome.projection is not None
        wire = outcome.projection.to_wire()
        self.assertEqual(wire["run_handle"], "run_1")
        self.assertEqual(wire["lifecycle_category"], "host_owned")
        self.assertEqual(wire["deadline"], outcome.deadline)
        self.assertNotIn("receipt_1", repr(wire))

    def test_an_m26_parent_deadline_tightens_the_lease(self) -> None:
        registry, service, _ = _fixture(S.RUNNING)
        outcome = _apply(service, ReleaseIntent.DETACH_CLIENT, parent_absolute_deadline=1_050.0)
        self.assertEqual(outcome.deadline, 1_050.0)


class ReleaseIsTheOnlyRemovalTests(unittest.TestCase):
    def test_a_prepared_child_is_released_and_gone(self) -> None:
        registry, service, _ = _fixture(S.SEQUENCE_PREPARED)
        outcome = _apply(service, ReleaseIntent.CLEANUP_PRE_SUBMIT)
        self.assertEqual(outcome.disposition, "released")
        self.assertTrue(outcome.removes_authority)
        self.assertIsNone(outcome.projection)
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")

    def test_a_proven_terminal_is_released_and_gone(self) -> None:
        registry, service, _ = _fixture(S.TERMINAL_SUCCEEDED)
        proof = terminal_fingerprint(registry.read("run_1"))
        outcome = _apply(service, ReleaseIntent.CLEANUP_TERMINAL, observed_terminal=proof)
        self.assertEqual(outcome.disposition, "released")
        self.assertTrue(outcome.removes_authority)
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")

    def test_a_terminal_can_be_cleaned_up_after_it_was_detached(self) -> None:
        # The returning-client path end to end: leave while running, come back to a settled run,
        # prove the result was seen, and only then remove it.
        registry, service, _ = _fixture(S.RUNNING)
        _apply(service, ReleaseIntent.DETACH_CLIENT)
        registry.advance("run_1", T.RECORD_ARTIFACT, artifact_receipt="receipt_1")
        registry.advance("run_1", T.RECORD_SUCCEEDED)
        proof = terminal_fingerprint(registry.read("run_1"))
        outcome = _apply(service, ReleaseIntent.CLEANUP_TERMINAL, observed_terminal=proof)
        self.assertEqual(outcome.disposition, "released")
        with self.assertRaises(ManagedRunRegistryError):
            registry.read("run_1")

    def test_a_proof_bound_to_a_different_observation_of_the_same_run_is_refused(self) -> None:
        # CRITICAL: the proof is bound to the observed *outcome*, not merely to the run handle. A
        # digest that agrees about the handle and the terminal state but disagrees about the
        # artifact is a client that looked away at the wrong moment, and it must not be able to
        # remove the result. Asserted here rather than only in the pure suite because this is the
        # layer that would actually delete something.
        registry, service, _ = _fixture(S.TERMINAL_SUCCEEDED)
        actual = registry.read("run_1")
        self.assertEqual(actual.artifact_receipt, "receipt_1")
        stale = terminal_fingerprint(replace(actual, artifact_receipt="receipt_0"))
        self.assertNotEqual(stale, terminal_fingerprint(actual))
        with self.assertRaises(ManagedRunReleaseRefused) as caught:
            _apply(service, ReleaseIntent.CLEANUP_TERMINAL, observed_terminal=stale)
        self.assertEqual(caught.exception.refusal, ReleaseRefusal.WRONG_INTENT)
        self.assertEqual(registry.read("run_1").state, S.TERMINAL_SUCCEEDED)


class _VanishingRegistry(ManagedRunRegistry):
    """A registry whose run is pruned between the service's read and its write.

    The real window is a concurrent `_prune`: the production registry calls the coordinator's
    liveness probe while holding only its own lock, and that reaches `live_handles`/`read` ->
    `_entry` -> `_prune`. Simulated deterministically by really removing the row inside the write.
    """

    def __init__(self, *, clock: Callable[[], float]) -> None:
        super().__init__(clock=clock)
        self.vanish_on_write = False

    def _vanish(self, run_handle: str) -> None:
        if not self.vanish_on_write:
            return
        self.vanish_on_write = False
        super().release(run_handle)

    def detach(self, run_handle: str, *, deadline: float) -> float:
        self._vanish(run_handle)
        return super().detach(run_handle, deadline=deadline)

    def release(self, run_handle: str) -> None:
        self._vanish(run_handle)
        super().release(run_handle)


class ARunThatVanishesMidApplyIsGoneNotAnInternalErrorTests(unittest.TestCase):
    def _vanishing(self, state: S) -> tuple[_VanishingRegistry, ManagedRunReleaseService]:
        clock = _Clock()
        registry = _VanishingRegistry(clock=clock)
        registry.create("run_1")
        registry.advance("run_1", T.STAGE_CONTEXT, context_revision=1)
        registry.advance("run_1", T.CREATE_PRODUCTION, production_workspace="ws_1", segment_count=1)
        registry.advance("run_1", T.PREPARE_SEQUENCE, prepared_sequence="seq_1")
        if state is not S.SEQUENCE_PREPARED:
            registry.advance("run_1", T.RECORD_SUBMISSION, prompt_id="prompt_1")
        registry.vanish_on_write = True
        return registry, ManagedRunReleaseService(registry)

    def test_a_detach_whose_run_is_pruned_mid_apply_answers_gone(self) -> None:
        # CRITICAL: without the guard the registry error escapes the service and the route turns a
        # perfectly ordinary race into an internal failure. "The run went away" already has an
        # honest answer, and it is the same 410 the read path gives.
        registry, service = self._vanishing(S.SUBMITTED)
        with self.assertRaises(ManagedRunReleaseRefused) as caught:
            _apply(service, ReleaseIntent.DETACH_CLIENT)
        self.assertEqual(caught.exception.refusal, ReleaseRefusal.GONE)
        self.assertEqual(caught.exception.status, 410)

    def test_a_cleanup_whose_run_is_pruned_mid_apply_answers_gone(self) -> None:
        registry, service = self._vanishing(S.SEQUENCE_PREPARED)
        with self.assertRaises(ManagedRunReleaseRefused) as caught:
            _apply(service, ReleaseIntent.CLEANUP_PRE_SUBMIT)
        self.assertEqual(caught.exception.refusal, ReleaseRefusal.GONE)
        self.assertEqual(caught.exception.status, 410)


if __name__ == "__main__":
    unittest.main()
