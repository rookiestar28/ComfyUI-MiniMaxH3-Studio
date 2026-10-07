"""M23-51: what a detached run retains, and for exactly how long.

A detach that retained nothing would be a cancellation with a friendlier name; a detach that
retained everything would be an unbounded store of private material keyed by an absent client. The
approved contract sits between: a bounded, content-free projection with a deadline that is *fixed*
at the moment of detach.

"Fixed" is the whole security property. A deadline that slid on reads would be renewed by the very
polling a returning client does, so a run nobody ever comes back for would live forever inside a
16-row bound and starve real ones. These tests pin the arithmetic and the boundedness; the registry
suite pins that nothing refreshes it.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from comfyui_h3_context.core.managed_run import ManagedRun, ManagedRunState
from comfyui_h3_context.core.managed_run_release import (
    DETACHED_GRACE_SECONDS,
    MAX_DETACHED_VISIBILITY_SECONDS,
    DetachedRunProjectionV1,
    detached_deadline,
    detached_projection,
    terminal_fingerprint,
)

HANDLE = "run-m23-51"
HOUR = 3600.0


class TheDeadlineIsFixedArithmeticTests(unittest.TestCase):
    def test_a_legacy_child_gets_its_timeout_plus_the_grace_window(self) -> None:
        self.assertEqual(
            detached_deadline(submitted_at=1_000.0, timeout_ms=120_000),
            1_000.0 + 120.0 + DETACHED_GRACE_SECONDS,
        )

    def test_an_absurd_timeout_is_capped_at_the_visibility_ceiling(self) -> None:
        # A host-supplied or workflow-supplied timeout is untrusted input. Without the ceiling a
        # single run could hold one of the sixteen live slots for a week.
        self.assertEqual(
            detached_deadline(submitted_at=1_000.0, timeout_ms=30 * 24 * 3_600_000),
            1_000.0 + MAX_DETACHED_VISIBILITY_SECONDS,
        )
        self.assertEqual(MAX_DETACHED_VISIBILITY_SECONDS, 24 * HOUR)

    def test_an_m26_parent_deadline_can_only_tighten_it_never_extend_it(self) -> None:
        # CRITICAL: the parent lease is authoritative for an M26 child, and it is applied as a
        # `min`, never as a replacement. A parent that outlives the child's own window must not
        # extend the child -- an M26 sequence holding a stale grandchild alive past its own timeout
        # is how a bounded store becomes an unbounded one.
        tight = detached_deadline(
            submitted_at=1_000.0, timeout_ms=600_000, parent_absolute_deadline=1_200.0
        )
        self.assertEqual(tight, 1_200.0)
        loose = detached_deadline(
            submitted_at=1_000.0, timeout_ms=600_000, parent_absolute_deadline=99_999_999.0
        )
        self.assertEqual(loose, 1_000.0 + 600.0 + DETACHED_GRACE_SECONDS)

    def test_the_ceiling_still_applies_to_an_m26_child(self) -> None:
        capped = detached_deadline(
            submitted_at=0.0,
            timeout_ms=30 * 24 * 3_600_000,
            parent_absolute_deadline=30 * 24 * HOUR,
        )
        self.assertEqual(capped, MAX_DETACHED_VISIBILITY_SECONDS)

    def test_it_takes_no_clock_and_is_therefore_unslideable(self) -> None:
        # Stated as a structural fact rather than a behavioural one: the function cannot observe
        # "now", so no read, poll, reconnect or duplicate event can move its answer. Reintroducing
        # a `now` parameter here is what a sliding deadline would look like.
        import inspect

        parameters = set(inspect.signature(detached_deadline).parameters)
        self.assertEqual(parameters, {"submitted_at", "timeout_ms", "parent_absolute_deadline"})
        first = detached_deadline(submitted_at=5.0, timeout_ms=1_000)
        for _ in range(3):
            self.assertEqual(detached_deadline(submitted_at=5.0, timeout_ms=1_000), first)

    def test_a_negative_or_non_integral_timeout_is_refused(self) -> None:
        for timeout in (-1, -600_000):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    detached_deadline(submitted_at=0.0, timeout_ms=timeout)


class TheProjectionIsBoundedAndContentFreeTests(unittest.TestCase):
    def _run(self, **fields: object) -> ManagedRun:
        base: dict[str, object] = {
            "run_handle": HANDLE,
            "state": ManagedRunState.RUNNING,
            "prompt_id": "0c7f8b1e-1111-2222-3333-444455556666",
            "prepared_sequence": "child-1",
        }
        base.update(fields)
        return ManagedRun(**base)  # type: ignore[arg-type]

    def test_the_wire_shape_is_exactly_the_approved_members(self) -> None:
        # CRITICAL: asserted as an equality, not a subset. A projection that grows a member is how
        # host output locators, exception text and workflow fragments reach a store that outlives
        # the client -- the failure this assertion exists to catch is an *addition*, so a
        # `assertIn`-style check would not catch it.
        wire = detached_projection(self._run(), deadline=1_500.0).to_wire()
        self.assertEqual(
            set(wire),
            {
                "schema",
                "run_handle",
                "lifecycle_category",
                "observation_category",
                "prompt_id",
                "prepared_sequence",
                "deadline",
                "terminal_fingerprint",
            },
        )
        self.assertEqual(wire["schema"], "h3.context.managed_run_detached_projection.v1")

    def test_it_reports_the_lifecycle_category_not_a_second_opinion(self) -> None:
        for state, category in (
            (ManagedRunState.SUBMITTED, "host_owned"),
            (ManagedRunState.RUNNING, "host_owned"),
            (ManagedRunState.ARTIFACT_RECORDED, "host_owned"),
            (ManagedRunState.TERMINAL_SUCCEEDED, "terminal"),
            (ManagedRunState.TERMINAL_FAILED, "terminal"),
            (ManagedRunState.TERMINAL_CANCELLED, "terminal"),
            (ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP, "unknown_ownership"),
        ):
            with self.subTest(state=state.value):
                wire = detached_projection(self._run(state=state), deadline=1.0).to_wire()
                self.assertEqual(wire["lifecycle_category"], category)
                self.assertEqual(wire["observation_category"], state.value)

    def test_a_settled_run_carries_its_terminal_fingerprint_and_a_live_one_carries_none(
        self,
    ) -> None:
        settled = self._run(state=ManagedRunState.TERMINAL_SUCCEEDED, artifact_receipt="r-1")
        self.assertEqual(
            detached_projection(settled, deadline=1.0).to_wire()["terminal_fingerprint"],
            terminal_fingerprint(settled),
        )
        self.assertIsNone(
            detached_projection(self._run(), deadline=1.0).to_wire()["terminal_fingerprint"]
        )

    def test_no_private_material_reaches_the_wire(self) -> None:
        # The aggregate holds receipts and geometry receipts that address stored media. None of
        # them belong in a row that outlives the client.
        run = self._run(
            state=ManagedRunState.ARTIFACT_RECORDED,
            artifact_receipt="B:/private/output/secret_video.mp4",
            geometry_receipts=("geo-secret-1", "geo-secret-2"),
        )
        rendered = repr(detached_projection(run, deadline=1.0).to_wire())
        for forbidden in ("secret_video", "B:/private", "geo-secret"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_it_is_frozen_so_a_holder_cannot_edit_a_retained_row(self) -> None:
        projection = detached_projection(self._run(), deadline=1.0)
        self.assertIsInstance(projection, DetachedRunProjectionV1)
        with self.assertRaises(FrozenInstanceError):
            projection.deadline = 2.0  # type: ignore[misc]

    def test_a_run_that_cannot_be_detached_has_no_projection(self) -> None:
        for state in (
            ManagedRunState.CREATED,
            ManagedRunState.CONTEXT_READY,
            ManagedRunState.PRODUCTION_READY,
            # `sequence_prepared` belongs here too: a prepared child the host never accepted is
            # refused by `_decide_detach`, so it has nothing to retain either. It is spelled out
            # rather than left to the shared boolean, because that boolean is the thing under test.
            ManagedRunState.SEQUENCE_PREPARED,
            ManagedRunState.EXPIRED,
        ):
            with self.subTest(state=state.value):
                with self.assertRaises(ValueError):
                    detached_projection(ManagedRun(run_handle=HANDLE, state=state), deadline=1.0)


if __name__ == "__main__":
    unittest.main()
