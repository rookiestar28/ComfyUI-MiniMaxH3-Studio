"""M23-51: releasing a run and detaching a client are different questions with different answers.

The defect this item exists to remove is one conditional short of a data-loss bug and one
conditional short of a stranded workspace, and it is the same conditional. `release_sequence`
currently drops whatever run it is given: a prepared, unsubmitted child that must be cleaned up, and
a submitted child the host is still executing, are treated identically. `MODEL_ONLY_TRIGGERS` in
`comfyui_h3_context/core/managed_run.py` already says so in prose.

The approved B1 contract splits the request by *intent* and answers from the run's own state, so
these tests are written against the decision rather than against the route: given a run and an
intent, exactly one outcome is correct, and a terminal arriving between the client's decision and
the server's must never turn a detach into a destructive cleanup.

Everything here is pure. No registry, no coordinator, no lock, no clock.
"""

from __future__ import annotations

import unittest

from comfyui_h3_context.core.managed_run import ManagedRun, ManagedRunState
from comfyui_h3_context.core.managed_run_release import (
    ReleaseDisposition,
    ReleaseIntent,
    ReleaseRefusal,
    decide_release,
    terminal_fingerprint,
)

HANDLE = "run-m23-51"

_LIVE_BEFORE_HOST = (
    ManagedRunState.CREATED,
    ManagedRunState.CONTEXT_READY,
    ManagedRunState.PRODUCTION_READY,
)
_HOST_MAY_HOLD = (
    ManagedRunState.SUBMITTED,
    ManagedRunState.RUNNING,
    ManagedRunState.ARTIFACT_RECORDED,
)
_SETTLED_TERMINALS = (
    ManagedRunState.TERMINAL_SUCCEEDED,
    ManagedRunState.TERMINAL_FAILED,
    ManagedRunState.TERMINAL_CANCELLED,
)


def _run(state: ManagedRunState, **fields: object) -> ManagedRun:
    return ManagedRun(run_handle=HANDLE, state=state, **fields)  # type: ignore[arg-type]


class TheIntentVocabularyIsClosedTests(unittest.TestCase):
    def test_the_three_intents_and_five_dispositions_are_exactly_the_approved_ones(self) -> None:
        # The approved B1 contract names these and no others. A sixth disposition, or an intent
        # that collapses two of these into one, is a product change and not an implementation
        # detail -- the state/intent table below is only meaningful against a closed vocabulary.
        self.assertEqual(
            {intent.value for intent in ReleaseIntent},
            {"cleanup_pre_submit", "detach_client", "cleanup_terminal"},
        )
        self.assertEqual(
            {disposition.value for disposition in ReleaseDisposition},
            {"released", "detached", "detached_terminal", "detached_unknown_ownership", "current"},
        )

    def test_every_refusal_reason_is_named_and_distinct(self) -> None:
        # A refusal that cannot be told apart from another refusal is a 400 with no information in
        # it. Each row of the table refuses for its own reason and the client can act on which.
        values = [refusal.value for refusal in ReleaseRefusal]
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(
            set(values),
            {
                "not_applicable",
                "not_host_owned",
                "not_terminal",
                "host_owned",
                "wrong_intent",
                "unresolved_ownership",
                "gone",
            },
        )


class CleanupBeforeSubmissionTests(unittest.TestCase):
    """The only intent that may remove a run, and only where the host cannot hold the prompt."""

    def test_a_prepared_unsubmitted_child_is_released(self) -> None:
        decision = decide_release(
            _run(ManagedRunState.SEQUENCE_PREPARED, prepared_sequence="child-1"),
            ReleaseIntent.CLEANUP_PRE_SUBMIT,
        )
        self.assertEqual(decision.disposition, ReleaseDisposition.RELEASED)
        self.assertTrue(decision.removes_authority)

    def test_a_run_with_nothing_prepared_is_refused_as_not_applicable(self) -> None:
        for state in _LIVE_BEFORE_HOST:
            with self.subTest(state=state.value):
                decision = decide_release(_run(state), ReleaseIntent.CLEANUP_PRE_SUBMIT)
                self.assertEqual(decision.refusal, ReleaseRefusal.NOT_APPLICABLE)
                self.assertFalse(decision.removes_authority)

    def test_a_submitted_or_running_child_is_refused_as_host_owned(self) -> None:
        # CRITICAL: this is the data-loss case, and the whole item. Removing a run the host is
        # still executing destroys the only authority able to accept its late result. It must
        # refuse, and it must refuse with a reason that says the host owns it -- not with
        # "invalid state", which reads as a client bug rather than as a fact about the run.
        for state in _HOST_MAY_HOLD:
            with self.subTest(state=state.value):
                decision = decide_release(
                    _run(state, prepared_sequence="child-1", prompt_id="prompt-1"),
                    ReleaseIntent.CLEANUP_PRE_SUBMIT,
                )
                self.assertEqual(decision.refusal, ReleaseRefusal.HOST_OWNED)
                self.assertFalse(decision.removes_authority)

    def test_a_prepared_child_that_already_has_a_prompt_is_host_owned_not_released(self) -> None:
        # CRITICAL: defensive, and the reason the decision reads the run rather than its state
        # name. `record_submission` moves prepared -> submitted, so "prepared with a prompt id"
        # should not exist; if it ever does -- a submission recorded against the coordinator but
        # not yet against the aggregate, which is exactly the window this item's atomicity
        # closes -- then treating it as an unsubmitted child deletes a run the host accepted.
        # The prompt id is the fact that matters, not the state label.
        decision = decide_release(
            _run(
                ManagedRunState.SEQUENCE_PREPARED,
                prepared_sequence="child-1",
                prompt_id="prompt-1",
            ),
            ReleaseIntent.CLEANUP_PRE_SUBMIT,
        )
        self.assertEqual(decision.refusal, ReleaseRefusal.HOST_OWNED)
        self.assertFalse(decision.removes_authority)

    def test_a_terminal_run_is_refused_as_wrong_intent(self) -> None:
        for state in (*_SETTLED_TERMINALS, ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP):
            with self.subTest(state=state.value):
                decision = decide_release(_run(state), ReleaseIntent.CLEANUP_PRE_SUBMIT)
                self.assertEqual(decision.refusal, ReleaseRefusal.WRONG_INTENT)
                self.assertFalse(decision.removes_authority)


class DetachNeverCancelsAndNeverRemovesTests(unittest.TestCase):
    """The client leaving is not the run stopping, and no detach may remove authority."""

    def test_detach_is_refused_before_the_host_can_hold_the_prompt(self) -> None:
        for state in (*_LIVE_BEFORE_HOST, ManagedRunState.SEQUENCE_PREPARED):
            with self.subTest(state=state.value):
                decision = decide_release(_run(state), ReleaseIntent.DETACH_CLIENT)
                self.assertEqual(decision.refusal, ReleaseRefusal.NOT_HOST_OWNED)

    def test_a_submitted_running_or_artifact_recorded_run_detaches(self) -> None:
        for state in _HOST_MAY_HOLD:
            with self.subTest(state=state.value):
                decision = decide_release(
                    _run(state, prepared_sequence="child-1", prompt_id="prompt-1"),
                    ReleaseIntent.DETACH_CLIENT,
                )
                self.assertEqual(decision.disposition, ReleaseDisposition.DETACHED)
                self.assertFalse(decision.removes_authority)

    def test_a_terminal_reached_before_the_detach_lands_stays_observable(self) -> None:
        # CRITICAL: the race this item exists to make safe. The client decides to leave while the
        # run is running; the terminal arrives before the request does. Answering `released` here
        # -- which is what the current unconditional handler does -- discards the result the user
        # came back for. The detach still succeeds, and says which kind of detach it was.
        for state in _SETTLED_TERMINALS:
            with self.subTest(state=state.value):
                decision = decide_release(
                    _run(state, prompt_id="prompt-1"), ReleaseIntent.DETACH_CLIENT
                )
                self.assertEqual(decision.disposition, ReleaseDisposition.DETACHED_TERMINAL)
                self.assertFalse(decision.removes_authority)

    def test_unknown_ownership_detaches_into_its_own_disposition(self) -> None:
        # Unknown ownership is not a failure and not a success; collapsing it into either is a
        # fabricated observation. It gets its own disposition so the returning client can be told
        # the truth: we do not know whether the host took this prompt.
        decision = decide_release(
            _run(ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP, prompt_id="prompt-1"),
            ReleaseIntent.DETACH_CLIENT,
        )
        self.assertEqual(decision.disposition, ReleaseDisposition.DETACHED_UNKNOWN_OWNERSHIP)
        self.assertFalse(decision.removes_authority)

    def test_no_detach_outcome_ever_removes_authority(self) -> None:
        # The invariant stated once, over the whole state space, rather than trusted to the rows
        # above staying complete. A future state that starts answering `released` to a detach is
        # the defect this item removed, reintroduced.
        for state in ManagedRunState:
            with self.subTest(state=state.value):
                decision = decide_release(
                    _run(state, prompt_id="prompt-1"), ReleaseIntent.DETACH_CLIENT
                )
                self.assertFalse(decision.removes_authority)
                self.assertNotEqual(decision.disposition, ReleaseDisposition.RELEASED)


class ObservedTerminalCleanupNeedsProofTests(unittest.TestCase):
    """The only way a settled run leaves, and it requires the caller to prove it saw the result."""

    def test_a_matching_proof_releases_a_settled_terminal(self) -> None:
        for state in _SETTLED_TERMINALS:
            with self.subTest(state=state.value):
                run = _run(state, prompt_id="prompt-1", artifact_receipt="receipt-1")
                decision = decide_release(
                    run,
                    ReleaseIntent.CLEANUP_TERMINAL,
                    observed_terminal=terminal_fingerprint(run),
                )
                self.assertEqual(decision.disposition, ReleaseDisposition.RELEASED)
                self.assertTrue(decision.removes_authority)

    def test_missing_stale_and_foreign_proof_all_refuse(self) -> None:
        # CRITICAL: without this, `cleanup_terminal` is a destructive escape hatch -- any client
        # that guesses a run is finished could remove it. The proof is what makes the removal
        # safe, so a wrong proof must be as fatal as no proof, and neither may fall back to
        # releasing anyway.
        run = _run(
            ManagedRunState.TERMINAL_SUCCEEDED, prompt_id="prompt-1", artifact_receipt="receipt-1"
        )
        other = _run(
            ManagedRunState.TERMINAL_FAILED, prompt_id="prompt-2", artifact_receipt="receipt-2"
        )
        for proof in (None, "", "not-a-fingerprint", terminal_fingerprint(other)):
            with self.subTest(proof=proof):
                decision = decide_release(
                    run, ReleaseIntent.CLEANUP_TERMINAL, observed_terminal=proof
                )
                self.assertEqual(decision.refusal, ReleaseRefusal.WRONG_INTENT)
                self.assertFalse(decision.removes_authority)

    def test_a_live_run_refuses_terminal_cleanup_however_good_the_proof(self) -> None:
        for state in (*_LIVE_BEFORE_HOST, ManagedRunState.SEQUENCE_PREPARED, *_HOST_MAY_HOLD):
            with self.subTest(state=state.value):
                run = _run(state, prompt_id="prompt-1")
                decision = decide_release(
                    run,
                    ReleaseIntent.CLEANUP_TERMINAL,
                    observed_terminal=terminal_fingerprint(run),
                )
                self.assertEqual(decision.refusal, ReleaseRefusal.NOT_TERMINAL)
                self.assertFalse(decision.removes_authority)

    def test_unknown_ownership_cannot_be_cleaned_up_by_proving_a_terminal(self) -> None:
        # CRITICAL: the escape hatch that would undo the whole distinction. If unknown ownership
        # accepted terminal cleanup, a client could remove a run whose result nobody knows by
        # asserting a fingerprint over the very uncertainty that makes it unknown. It refuses with
        # its own reason, and never claims the run succeeded or failed.
        run = _run(ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP, prompt_id="prompt-1")
        decision = decide_release(
            run, ReleaseIntent.CLEANUP_TERMINAL, observed_terminal=terminal_fingerprint(run)
        )
        self.assertEqual(decision.refusal, ReleaseRefusal.UNRESOLVED_OWNERSHIP)
        self.assertFalse(decision.removes_authority)


class TheFingerprintIsExactAndCarriesNothingPrivateTests(unittest.TestCase):
    def test_it_distinguishes_runs_that_differ_in_any_observed_field(self) -> None:
        base = _run(
            ManagedRunState.TERMINAL_SUCCEEDED, prompt_id="prompt-1", artifact_receipt="receipt-1"
        )
        variants = (
            _run(
                ManagedRunState.TERMINAL_FAILED,
                prompt_id="prompt-1",
                artifact_receipt="receipt-1",
            ),
            _run(
                ManagedRunState.TERMINAL_SUCCEEDED,
                prompt_id="prompt-2",
                artifact_receipt="receipt-1",
            ),
            _run(
                ManagedRunState.TERMINAL_SUCCEEDED,
                prompt_id="prompt-1",
                artifact_receipt="receipt-2",
            ),
        )
        digests = {terminal_fingerprint(base)} | {terminal_fingerprint(run) for run in variants}
        self.assertEqual(len(digests), 1 + len(variants))

    def test_it_is_deterministic_and_reveals_none_of_its_inputs(self) -> None:
        # It travels to the client and back, so it must be stable across calls and must not carry
        # the prompt id -- an opaque digest is the whole point of using one as proof.
        run = _run(
            ManagedRunState.TERMINAL_SUCCEEDED,
            prompt_id="prompt-secret",
            artifact_receipt="receipt-secret",
        )
        digest = terminal_fingerprint(run)
        self.assertIsNotNone(digest)
        assert digest is not None
        self.assertEqual(digest, terminal_fingerprint(run))
        self.assertNotIn("prompt-secret", digest)
        self.assertNotIn("receipt-secret", digest)

    def test_a_run_that_has_not_settled_has_no_terminal_fingerprint(self) -> None:
        for state in (*_LIVE_BEFORE_HOST, ManagedRunState.SEQUENCE_PREPARED, *_HOST_MAY_HOLD):
            with self.subTest(state=state.value):
                self.assertIsNone(terminal_fingerprint(_run(state, prompt_id="prompt-1")))


class EveryStateAndIntentPairHasExactlyOneAnswerTests(unittest.TestCase):
    def test_the_table_is_total(self) -> None:
        # A missing row is a route that falls through to whatever the last `elif` did, which is how
        # the current unconditional handler behaves. Every pair answers, and answers exactly one of
        # "here is the disposition" or "here is why not".
        for state in ManagedRunState:
            for intent in ReleaseIntent:
                with self.subTest(state=state.value, intent=intent.value):
                    decision = decide_release(_run(state, prompt_id="prompt-1"), intent)
                    self.assertEqual((decision.disposition is None), (decision.refusal is not None))

    def test_only_prepared_cleanup_and_proven_terminal_cleanup_remove_authority(self) -> None:
        # Stated as a census rather than as a rule, because the rule is what a future edit would
        # rewrite. Only two kinds of cell may remove a run: cleanup of a prepared, unsubmitted
        # child, and proven cleanup of a settled terminal.
        removing: set[tuple[str, str]] = set()
        for state in ManagedRunState:
            for intent in ReleaseIntent:
                run = _run(state, prepared_sequence="child-1")
                decision = decide_release(run, intent, observed_terminal=terminal_fingerprint(run))
                if decision.removes_authority:
                    removing.add((state.value, intent.value))
        self.assertEqual(
            removing,
            {
                ("sequence_prepared", "cleanup_pre_submit"),
                ("terminal_succeeded", "cleanup_terminal"),
                ("terminal_failed", "cleanup_terminal"),
                ("terminal_cancelled", "cleanup_terminal"),
            },
        )

    def test_an_expired_run_is_gone_for_every_intent(self) -> None:
        for intent in ReleaseIntent:
            with self.subTest(intent=intent.value):
                decision = decide_release(_run(ManagedRunState.EXPIRED), intent)
                self.assertEqual(decision.refusal, ReleaseRefusal.GONE)
                self.assertFalse(decision.removes_authority)


if __name__ == "__main__":
    unittest.main()
