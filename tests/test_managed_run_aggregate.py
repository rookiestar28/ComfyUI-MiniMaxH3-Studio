"""M23-31 focused tests for the ManagedRun aggregate.

One test per lifecycle transition of the 33-row action table, plus the two invariants the four
registries used to hold between them, plus the two follow-ups routed here from the M23-23 review.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

import jsonschema

from comfyui_h3_context.core.managed_run import (
    HOST_MAY_HOLD_THE_PROMPT,
    MANAGED_RUN_EVENT_SCHEMA,
    MANAGED_RUN_MACHINE,
    MAX_GEOMETRY_RECEIPTS_PER_RUN,
    MAX_TRANSITION_EVENTS,
    NO_LIVE_SEQUENCE_GUARD,
    SINGLE_SEGMENT_GUARD,
    GuardRejectedError,
    InvalidTriggerError,
    ManagedRun,
    ManagedRunError,
    ManagedRunState,
    ManagedRunTrigger,
    advance,
    attach_geometry_receipt,
    cancel_trigger,
    managed_run_stategraph,
    restore,
)

T = ManagedRunTrigger
S = ManagedRunState


def _transitions() -> list[dict[str, object]]:
    return cast(list[dict[str, object]], managed_run_stategraph()["transitions"])


def _prepared(segment_count: int = 1) -> ManagedRun:
    run = advance(ManagedRun(run_handle="run_1"), T.STAGE_CONTEXT, context_revision=1)
    run = advance(
        run, T.CREATE_PRODUCTION, production_workspace="ws_1", segment_count=segment_count
    )
    return advance(run, T.PREPARE_SEQUENCE, prepared_sequence="seq_1")


def _submitted() -> ManagedRun:
    return advance(_prepared(), T.RECORD_SUBMISSION, prompt_id="prompt_1")


class LifecycleTests(unittest.TestCase):
    def test_the_happy_path_walks_every_lifecycle_state(self) -> None:
        run = advance(ManagedRun(run_handle="run_1"), T.STAGE_CONTEXT, context_revision=1)
        self.assertEqual(run.state, S.CONTEXT_READY)
        run = advance(run, T.CREATE_PRODUCTION, production_workspace="ws_1", segment_count=1)
        self.assertEqual(run.state, S.PRODUCTION_READY)
        run = advance(run, T.PREPARE_SEQUENCE, prepared_sequence="seq_1")
        self.assertEqual(run.state, S.SEQUENCE_PREPARED)
        run = advance(run, T.RECORD_SUBMISSION, prompt_id="prompt_1")
        self.assertEqual(run.state, S.SUBMITTED)
        run = advance(run, T.RECORD_RUNNING)
        self.assertEqual(run.state, S.RUNNING)
        run = advance(run, T.RECORD_ARTIFACT, artifact_receipt="artifact_1")
        self.assertEqual(run.state, S.ARTIFACT_RECORDED)
        run = advance(run, T.RECORD_SUCCEEDED)
        self.assertEqual(run.state, S.TERMINAL_SUCCEEDED)
        run = advance(run, T.RELEASE_SEQUENCE)
        self.assertEqual(run.state, S.EXPIRED)
        self.assertFalse(run.is_live)

    def test_staging_context_again_advances_the_revision_without_leaving_the_state(self) -> None:
        # `stage_prompt`, `import_prompt` and `validate` all commit a workspace entry, so the
        # transition is idempotent in state and not in revision.
        run = advance(ManagedRun(run_handle="run_1"), T.STAGE_CONTEXT, context_revision=1)
        run = advance(run, T.STAGE_CONTEXT, context_revision=2)
        self.assertEqual(run.state, S.CONTEXT_READY)
        self.assertEqual(run.context_revision, 2)

    def test_a_trigger_that_does_not_leave_this_state_is_an_invalid_trigger(self) -> None:
        with self.assertRaises(InvalidTriggerError):
            advance(ManagedRun(run_handle="run_1"), T.RECORD_SUBMISSION)

    def test_a_run_may_end_from_running_without_an_artifact(self) -> None:
        # A generation can end without producing one, and the coordinator terminalizes a RUNNING
        # runtime whether or not an artifact was recorded. Requiring `artifact_recorded` first
        # would refuse a real outcome.
        running = advance(_submitted(), T.RECORD_RUNNING)
        self.assertEqual(advance(running, T.RECORD_FAILED).state, S.TERMINAL_FAILED)
        self.assertEqual(advance(running, T.RECORD_SUCCEEDED).state, S.TERMINAL_SUCCEEDED)

    def test_a_terminal_is_refused_before_the_host_starts_running_the_prompt(self) -> None:
        # The coordinator's own auto-advance from SUBMITTED is mirrored as an explicit
        # `record_running`, so the table keeps one transition per real event rather than
        # admitting a terminal straight from `submitted`.
        with self.assertRaises(InvalidTriggerError):
            advance(_submitted(), T.RECORD_FAILED)


class InvariantTests(unittest.TestCase):
    def test_a_sequence_is_refused_unless_the_workspace_holds_exactly_one_segment(self) -> None:
        run = advance(ManagedRun(run_handle="run_1"), T.STAGE_CONTEXT, context_revision=1)
        run = advance(run, T.CREATE_PRODUCTION, production_workspace="ws_1", segment_count=2)
        with self.assertRaises(GuardRejectedError) as caught:
            advance(run, T.PREPARE_SEQUENCE, prepared_sequence="seq_1")
        self.assertIn(SINGLE_SEGMENT_GUARD, str(caught.exception))

    def test_the_single_segment_guard_is_the_one_the_stategraph_publishes(self) -> None:
        # The premise that makes `runtimes[0]` safe is named once and published once. A guard the
        # code evaluates but the graph does not name is a guard a reader cannot find.
        rows = {(row["trigger"], row["source"]): row for row in _transitions()}
        row = rows[(T.PREPARE_SEQUENCE.value, S.PRODUCTION_READY.value)]
        self.assertEqual(row["guard"], SINGLE_SEGMENT_GUARD)

    def test_production_cannot_be_released_while_a_sequence_depends_on_it(self) -> None:
        with self.assertRaises(GuardRejectedError) as caught:
            advance(_prepared(), T.RELEASE_PRODUCTION)
        self.assertIn(NO_LIVE_SEQUENCE_GUARD, str(caught.exception))

    def test_production_may_be_released_before_a_sequence_exists(self) -> None:
        run = advance(ManagedRun(run_handle="run_1"), T.STAGE_CONTEXT, context_revision=1)
        run = advance(run, T.CREATE_PRODUCTION, production_workspace="ws_1", segment_count=1)
        self.assertEqual(advance(run, T.RELEASE_PRODUCTION).state, S.EXPIRED)


class CancelPredicateTests(unittest.TestCase):
    def test_the_cancel_question_is_decided_once_from_the_run_state(self) -> None:
        # The defect this replaces: the coordinator tested the runtime's job state to decide the
        # recorded transition and tested `queue_prompt_id` to decide the reported disposition, so
        # the two could disagree. Here the disposition IS the transition target.
        before = _prepared()
        self.assertEqual(cancel_trigger(before.state), T.CANCEL_BEFORE_HOST)
        self.assertEqual(advance(before, cancel_trigger(before.state)).state, S.TERMINAL_CANCELLED)
        after = _submitted()
        self.assertEqual(cancel_trigger(after.state), T.CANCEL_AFTER_HOST)
        self.assertEqual(
            advance(after, cancel_trigger(after.state)).state, S.TERMINAL_UNKNOWN_OWNERSHIP
        )

    def test_every_state_the_host_may_hold_the_prompt_in_reaches_unknown_ownership(self) -> None:
        for state in sorted(HOST_MAY_HOLD_THE_PROMPT, key=lambda item: item.value):
            with self.subTest(state=state):
                self.assertEqual(cancel_trigger(state), T.CANCEL_AFTER_HOST)

    def test_the_host_authority_is_exactly_the_states_after_submission(self) -> None:
        # A run reaches `submitted` exactly when the host received the prompt, which is why the
        # aggregate state is a sufficient authority and a second field is not needed.
        self.assertEqual(
            {state.value for state in HOST_MAY_HOLD_THE_PROMPT},
            {"submitted", "running", "artifact_recorded"},
        )

    def test_the_two_cancel_triggers_never_share_a_source_state(self) -> None:
        # If they overlapped, the choice would be ambiguous again and the machine would resolve it
        # by first match rather than by the predicate.
        rows = MANAGED_RUN_MACHINE.transitions
        before = next(row for row in rows if row.trigger == T.CANCEL_BEFORE_HOST.value)
        after = next(row for row in rows if row.trigger == T.CANCEL_AFTER_HOST.value)
        self.assertEqual(set(before.source) & set(after.source), set())


class TransitionEvidenceTests(unittest.TestCase):
    def test_every_transition_records_one_bounded_event(self) -> None:
        run = _submitted()
        self.assertEqual(len(run.events), 4)
        self.assertEqual([event.sequence for event in run.events], [0, 1, 2, 3])
        self.assertEqual(run.events[-1].trigger, T.RECORD_SUBMISSION.value)
        self.assertEqual(run.events[-1].source, S.SEQUENCE_PREPARED.value)
        self.assertEqual(run.events[-1].target, S.SUBMITTED.value)

    def test_an_event_carries_only_closed_vocabulary_and_identifiers(self) -> None:
        # The redaction contract is structural: there is no free-text member to redact. A `detail`
        # or `message` field would be the channel prompt text arrives through.
        wire = _submitted().events[-1].to_wire()
        self.assertEqual(
            sorted(wire),
            ["guard", "run_handle", "schema", "sequence", "source", "target", "trigger"],
        )
        self.assertEqual(wire["schema"], MANAGED_RUN_EVENT_SCHEMA)
        states = set(MANAGED_RUN_MACHINE.states)
        self.assertIn(wire["source"], states)
        self.assertIn(wire["target"], states)
        self.assertIn(wire["trigger"], MANAGED_RUN_MACHINE.triggers)

    def test_evidence_is_bounded_and_drops_the_oldest_rather_than_refusing(self) -> None:
        # Diagnostics must never be the reason a run stops advancing.
        run = ManagedRun(run_handle="run_1")
        for revision in range(MAX_TRANSITION_EVENTS + 8):
            run = advance(run, T.STAGE_CONTEXT, context_revision=revision)
        self.assertEqual(len(run.events), MAX_TRANSITION_EVENTS)
        self.assertEqual(run.state, S.CONTEXT_READY)


class GeometryAttachmentTests(unittest.TestCase):
    def test_a_receipt_attaches_once_and_is_not_re_claimable(self) -> None:
        run = attach_geometry_receipt(ManagedRun(run_handle="run_1"), "receipt_1")
        self.assertEqual(run.geometry_receipts, ("receipt_1",))
        with self.assertRaises(ManagedRunError):
            attach_geometry_receipt(run, "receipt_1")

    def test_receipts_are_bounded(self) -> None:
        run = ManagedRun(run_handle="run_1")
        for index in range(MAX_GEOMETRY_RECEIPTS_PER_RUN):
            run = attach_geometry_receipt(run, f"receipt_{index}")
        with self.assertRaises(ManagedRunError):
            attach_geometry_receipt(run, "receipt_overflow")


class RestoreTests(unittest.TestCase):
    def test_a_restored_run_emits_no_evidence(self) -> None:
        run = restore("run_1", S.RUNNING.value)
        self.assertEqual(run.state, S.RUNNING)
        self.assertEqual(run.events, ())

    def test_a_restored_state_must_be_one_the_machine_declares(self) -> None:
        with self.assertRaises(ValueError):
            restore("run_1", "somewhere_else")

    def test_a_malformed_run_handle_is_refused(self) -> None:
        with self.assertRaises(ManagedRunError):
            ManagedRun(run_handle="not a handle")


class StategraphTests(unittest.TestCase):
    def test_the_graph_declares_every_state_and_trigger_the_enums_name(self) -> None:
        graph = managed_run_stategraph()
        self.assertEqual(graph["states"], sorted(state.value for state in S))
        self.assertEqual(
            sorted({str(row["trigger"]) for row in _transitions()}),
            sorted(trigger.value for trigger in T),
        )

    def test_no_transition_is_a_catch_all(self) -> None:
        # The plan forbids an "any state" transition: a route action without a clean transition
        # gets an explicit guarded one instead. `release_sequence` is the widest and it still names
        # only the four terminal states.
        for row in MANAGED_RUN_MACHINE.transitions:
            with self.subTest(trigger=row.trigger):
                self.assertLess(len(row.source), len(MANAGED_RUN_MACHINE.states))

    def test_every_state_is_reachable_from_the_initial_state(self) -> None:
        reachable = {MANAGED_RUN_MACHINE.initial}
        changed = True
        while changed:
            changed = False
            for row in MANAGED_RUN_MACHINE.transitions:
                if row.target not in reachable and reachable & set(row.source):
                    reachable.add(row.target)
                    changed = True
        self.assertEqual(reachable, set(MANAGED_RUN_MACHINE.states))


class PublishedStategraphArtifactTests(unittest.TestCase):
    """The shipped contract must be the machine, not a copy of it that drifted.

    CRITICAL: this class must stay above the `__main__` guard. Defined below it, pytest still
    collects it but `python tests/test_managed_run_aggregate.py` silently runs a suite without the
    only drift check the shipped contract has, so the two ways of running this module would
    disagree about which tests exist.
    """

    ARTIFACT = (
        Path(__file__).resolve().parents[1]
        / "governance"
        / "contracts"
        / "managed_run_stategraph_v1.json"
    )

    def test_the_shipped_artifact_equals_the_machine_projection(self) -> None:
        shipped = json.loads(self.ARTIFACT.read_text(encoding="utf-8"))
        self.assertEqual(
            shipped,
            managed_run_stategraph(),
            "the shipped stategraph no longer equals the machine; "
            "run `python scripts/managed_run_stategraph.py --write`",
        )

    def test_the_shipped_artifact_validates_against_its_schema(self) -> None:
        schema = json.loads(
            self.ARTIFACT.with_name("managed_run_stategraph_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.validate(json.loads(self.ARTIFACT.read_text(encoding="utf-8")), schema)


if __name__ == "__main__":
    unittest.main()
