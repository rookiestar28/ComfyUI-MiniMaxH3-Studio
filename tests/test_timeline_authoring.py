"""M20-02 focused tests for the timeline authoring domain and constraint engine.

The rows here hold apart the decisions a careless editor collapses:

* command authority -- one revisioned deterministic engine, atomic typed rejections, receipts
  whose typed inverse commands round-trip exactly (undo/redo without hidden snapshots);
* upstream authority -- clips may reference only admitted M20-00 asset IDs through the one
  `reference_view` mapping, and the profile constants come from the accepted M20-01 authority;
* hard constraints versus advice -- overlap, extent, source, link co-timing and exact
  audio-period alignment reject commands, while snap only proposes bounded ordered candidates;
* exactness -- trim, split and merge preserve source ranges and envelopes byte-exactly or
  reject; nothing is clamped, parked or fabricated.

No fixture carries a prompt, media value, path, URL, filename or credential.
"""

from __future__ import annotations

import ast
import random
import unittest
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path

from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.contracts import MediaKind
from comfyui_h3_context.core.reference_set_authoring import (
    AddSource,
    AdmittedSourceInput,
    ReferenceSetState,
    RemoveSource,
    TimedReferenceLimits,
    build_h3_base_capacity,
    create_reference_set,
)
from comfyui_h3_context.core.reference_set_authoring import (
    apply_command as apply_reference_command,
)
from comfyui_h3_context.core.temporal_profile import (
    CONTEXT_EXTENT_GRID_FRAMES,
    EXACT_AUDIO_PERIOD_FRAMES,
    TEMPORAL_PROFILE_SCHEMA,
    build_temporal_profile,
)
from comfyui_h3_context.core.timeline_authoring import (
    TIMELINE_AUTHORING_SCHEMA,
    AddClip,
    EnvelopePoint,
    LinkClips,
    MergeClips,
    MoveClip,
    MoveGroup,
    RemoveClip,
    SelectClips,
    SetEnvelope,
    SnapKind,
    SplitClip,
    TimelineAuthoringError,
    TimelineAuthoringLimits,
    TimelineCommand,
    TimelineProfileInput,
    TimelineReferenceView,
    TimelineState,
    TrimClip,
    TrimEdge,
    UnlinkClips,
    apply_command,
    build_h3_timeline_limits,
    build_h3_timeline_profile,
    create_timeline,
    reference_view,
    snap_candidates,
    timeline_blockers,
)

FP = "sha256:" + "0" * 64


def profile() -> TimelineProfileInput:
    return TimelineProfileInput(
        schema=TEMPORAL_PROFILE_SCHEMA,
        version=1,
        fingerprint=FP,
        video_fps=24,
        frame_grid=51,
        audio_period_frames=3,
        max_extent_frames=3_600,
    )


def limits() -> TimelineAuthoringLimits:
    return TimelineAuthoringLimits(
        max_clips=6, max_lanes=2, max_group=4, max_envelope_points=4, max_selection=4
    )


def reference_state(*, with_image: bool = False) -> ReferenceSetState:
    capacity = build_h3_base_capacity(
        authority="core.registry",
        fingerprint=FP,
        timed=TimedReferenceLimits(max_duration_milliseconds=150_000, max_frames=3_600),
    )
    state = create_reference_set(capacity)
    sources = [
        AdmittedSourceInput(
            source_id="vid-1", kind=MediaKind.VIDEO, fingerprint=FP, duration_milliseconds=5_000
        ),
        AdmittedSourceInput(
            source_id="vid-2", kind=MediaKind.VIDEO, fingerprint=FP, duration_milliseconds=5_000
        ),
        AdmittedSourceInput(
            source_id="aud-1", kind=MediaKind.AUDIO, fingerprint=FP, duration_milliseconds=5_000
        ),
    ]
    if with_image:
        sources.append(
            AdmittedSourceInput(
                source_id="img-1", kind=MediaKind.IMAGE, fingerprint=FP, duration_milliseconds=None
            )
        )
    for source in sources:
        state, _ = apply_reference_command(state, AddSource(state.revision, source))
    return state


def refs(*, with_image: bool = False) -> TimelineReferenceView:
    return reference_view(reference_state(with_image=with_image))


def fresh(view: TimelineReferenceView | None = None) -> TimelineState:
    return create_timeline(profile(), limits(), view if view is not None else refs())


def added(
    state: TimelineState,
    view: TimelineReferenceView,
    clip_id: str,
    asset_id: str,
    *,
    lane: int = 0,
    start: int = 0,
    frames: int = 12,
    source_start: int = 0,
) -> TimelineState:
    state, _ = apply_command(
        state,
        AddClip(
            expected_revision=state.revision,
            clip_id=clip_id,
            asset_id=asset_id,
            lane=lane,
            start_frame=start,
            frames=frames,
            source_start_frame=source_start,
        ),
        view,
    )
    return state


def rejection(testcase: unittest.TestCase, code: str) -> AbstractContextManager[object]:
    return testcase.assertRaisesRegex(TimelineAuthoringError, code)


def fingerprint_of(state: TimelineState) -> str:
    return canonical_fingerprint(state.to_wire())


def content_of(state: TimelineState) -> str:
    return state.content_fingerprint()


class CommandAuthorityTests(unittest.TestCase):
    """AC-M20-02-01: one deterministic revisioned command authority, atomic rejections."""

    def test_commands_produce_monotonic_revisions_and_exact_receipts(self) -> None:
        view = refs()
        state = fresh(view)
        first = state.revision
        state, receipt = apply_command(
            state,
            AddClip(
                expected_revision=state.revision,
                clip_id="c1",
                asset_id="vid-1",
                lane=0,
                start_frame=0,
                frames=12,
                source_start_frame=0,
            ),
            view,
        )
        self.assertEqual(receipt.revision_before, first)
        self.assertEqual(receipt.revision_after, state.revision)
        self.assertEqual(state.revision, first + 1)
        self.assertEqual(receipt.subject_ids, ("c1",))
        self.assertEqual(receipt.state_fingerprint, fingerprint_of(state))

    def test_stale_revision_and_replay_reject_without_partial_state(self) -> None:
        view = refs()
        state = fresh(view)
        command = AddClip(
            expected_revision=state.revision,
            clip_id="c1",
            asset_id="vid-1",
            lane=0,
            start_frame=0,
            frames=12,
            source_start_frame=0,
        )
        state, _ = apply_command(state, command, view)
        before = fingerprint_of(state)
        with rejection(self, "stale_revision"):
            apply_command(state, command, view)
        self.assertEqual(fingerprint_of(state), before)

    def test_expected_fingerprint_guards_the_exact_state(self) -> None:
        view = refs()
        state = fresh(view)
        wrong = "sha256:" + "f" * 64
        with rejection(self, "stale_fingerprint"):
            apply_command(
                state,
                SelectClips(
                    expected_revision=state.revision, clip_ids=(), expected_fingerprint=wrong
                ),
                view,
            )
        state, _ = apply_command(
            state,
            SelectClips(
                expected_revision=state.revision,
                clip_ids=(),
                expected_fingerprint=fingerprint_of(state),
            ),
            view,
        )
        self.assertEqual(state.revision, 2)


class UpstreamAuthorityTests(unittest.TestCase):
    """AC-M20-02-06: admitted IDs and profile constants are consumed, never re-derived."""

    def test_the_h3_profile_view_mirrors_the_accepted_temporal_authority(self) -> None:
        view = build_h3_timeline_profile(build_temporal_profile())
        self.assertEqual(view.schema, TEMPORAL_PROFILE_SCHEMA)
        self.assertEqual(view.frame_grid, CONTEXT_EXTENT_GRID_FRAMES)
        self.assertEqual(view.audio_period_frames, EXACT_AUDIO_PERIOD_FRAMES)
        self.assertGreaterEqual(view.max_extent_frames, view.frame_grid)
        self.assertIsNotNone(build_h3_timeline_limits())

    def test_a_clip_requires_an_admitted_asset_of_its_kind(self) -> None:
        view = refs(with_image=True)
        state = fresh(view)
        with rejection(self, "unknown_asset"):
            added(state, view, "c1", "vid-9")
        with rejection(self, "kind_unsupported"):
            added(state, view, "c1", "img-1")

    def test_source_range_stays_inside_the_admitted_extent(self) -> None:
        view = refs()
        state = fresh(view)
        # 5000 ms at 24 fps admits exactly 120 source frames.
        state = added(state, view, "c1", "vid-1", frames=120)
        with rejection(self, "source_bounds"):
            added(state, view, "c2", "vid-1", lane=1, frames=121)
        with rejection(self, "source_bounds"):
            added(state, view, "c2", "vid-1", lane=1, frames=120, source_start=1)

    def test_an_older_reference_view_is_stale_and_drift_blocks_the_queue(self) -> None:
        reference = reference_state()
        old_view = reference_view(reference)
        reference, _ = apply_reference_command(reference, RemoveSource(reference.revision, "vid-2"))
        new_view = reference_view(reference)
        state = fresh(old_view)
        state = added(state, old_view, "c1", "vid-2")
        state, _ = apply_command(
            state, SelectClips(expected_revision=state.revision, clip_ids=()), new_view
        )
        with rejection(self, "stale_references"):
            apply_command(
                state, SelectClips(expected_revision=state.revision, clip_ids=()), old_view
            )
        codes = [(blocker.clip_id, blocker.code) for blocker in timeline_blockers(state, new_view)]
        self.assertEqual(codes, [("c1", "asset_missing")])
        self.assertEqual(
            [blocker.code for blocker in timeline_blockers(state, old_view)],
            ["stale_references"],
        )


class GeometryTests(unittest.TestCase):
    """AC-M20-02-02: collision, extent and lane invariants hold; no partial state."""

    def test_same_lane_overlap_rejects_and_adjacency_is_exact(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", start=0, frames=12)
        with rejection(self, "overlap"):
            added(state, view, "c2", "vid-2", start=11, frames=12)
        state = added(state, view, "c2", "vid-2", start=12, frames=12)
        self.assertEqual(len(state.clips), 2)
        state = added(state, view, "c3", "vid-1", lane=1, start=0, frames=12)
        self.assertEqual(len(state.clips), 3)

    def test_extent_lane_and_clip_ceilings_reject(self) -> None:
        view = refs()
        state = fresh(view)
        with rejection(self, "extent_bounds"):
            added(state, view, "c1", "vid-1", start=3_589, frames=12)
        with rejection(self, "lane_bounds"):
            added(state, view, "c1", "vid-1", lane=2)
        for index in range(6):
            state = added(state, view, f"c{index}", "vid-1", lane=index % 2, start=index * 24)
        with rejection(self, "capacity_clips"):
            added(state, view, "c9", "vid-1", start=1_000)

    def test_move_and_group_move_are_atomic(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", start=0, frames=12)
        state = added(state, view, "c2", "vid-2", start=24, frames=12)
        state, receipt = apply_command(
            state, MoveClip(expected_revision=state.revision, clip_id="c1", delta_frames=6), view
        )
        clip = state.clip("c1")
        assert clip is not None
        self.assertEqual(clip.start_frame, 6)
        before = fingerprint_of(state)
        with rejection(self, "overlap"):
            apply_command(
                state,
                MoveClip(expected_revision=state.revision, clip_id="c1", delta_frames=12),
                view,
            )
        self.assertEqual(fingerprint_of(state), before)
        state, _ = apply_command(
            state,
            MoveGroup(expected_revision=state.revision, clip_ids=("c1", "c2"), delta_frames=12),
            view,
        )
        moved_one, moved_two = state.clip("c1"), state.clip("c2")
        assert moved_one is not None and moved_two is not None
        self.assertEqual((moved_one.start_frame, moved_two.start_frame), (18, 36))
        with rejection(self, "extent_bounds"):
            apply_command(
                state,
                MoveGroup(
                    expected_revision=state.revision, clip_ids=("c1", "c2"), delta_frames=-100
                ),
                view,
            )


class LinkTests(unittest.TestCase):
    """AC-M20-02-02: linked audiovisual placement is exact, alignment is never clamped."""

    def _linked(self) -> tuple[TimelineState, TimelineReferenceView]:
        view = refs()
        state = fresh(view)
        state = added(state, view, "v1", "vid-1", lane=0, start=0, frames=12)
        state = added(state, view, "a1", "aud-1", lane=0, start=0, frames=12)
        state, _ = apply_command(
            state,
            LinkClips(expected_revision=state.revision, video_clip_id="v1", audio_clip_id="a1"),
            view,
        )
        return state, view

    def test_link_requires_kinds_cotiming_and_audio_period_alignment(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "v1", "vid-1", start=0, frames=12)
        state = added(state, view, "v2", "vid-2", lane=1, start=0, frames=12)
        state = added(state, view, "a1", "aud-1", start=0, frames=13)
        with rejection(self, "link_kind"):
            apply_command(
                state,
                LinkClips(expected_revision=state.revision, video_clip_id="v1", audio_clip_id="v2"),
                view,
            )
        with rejection(self, "link_alignment"):
            apply_command(
                state,
                LinkClips(expected_revision=state.revision, video_clip_id="v1", audio_clip_id="a1"),
                view,
            )
        state, _ = apply_command(
            state,
            TrimClip(
                expected_revision=state.revision,
                clip_id="a1",
                edge=TrimEdge.END,
                delta_frames=-1,
            ),
            view,
        )
        state, _ = apply_command(
            state,
            MoveGroup(expected_revision=state.revision, clip_ids=("v1", "a1"), delta_frames=1),
            view,
        )
        with rejection(self, "audio_alignment"):
            apply_command(
                state,
                LinkClips(expected_revision=state.revision, video_clip_id="v1", audio_clip_id="a1"),
                view,
            )

    def test_a_linked_pair_moves_as_one_and_only_on_period_steps(self) -> None:
        state, view = self._linked()
        with rejection(self, "audio_alignment"):
            apply_command(
                state,
                MoveClip(expected_revision=state.revision, clip_id="v1", delta_frames=1),
                view,
            )
        state, receipt = apply_command(
            state, MoveClip(expected_revision=state.revision, clip_id="a1", delta_frames=6), view
        )
        self.assertEqual(receipt.subject_ids, ("a1", "v1"))
        video, audio = state.clip("v1"), state.clip("a1")
        assert video is not None and audio is not None
        self.assertEqual((video.start_frame, audio.start_frame), (6, 6))

    def test_destructive_edits_on_a_linked_clip_require_unlink(self) -> None:
        state, view = self._linked()
        for command in (
            RemoveClip(expected_revision=state.revision, clip_id="v1"),
            TrimClip(
                expected_revision=state.revision,
                clip_id="v1",
                edge=TrimEdge.END,
                delta_frames=-3,
            ),
            SplitClip(
                expected_revision=state.revision,
                clip_id="v1",
                at_offset_frames=6,
                new_clip_id="v1b",
            ),
        ):
            with rejection(self, "linked_requires_unlink"):
                apply_command(state, command, view)
        with rejection(self, "link_split_by_group"):
            apply_command(
                state,
                MoveGroup(expected_revision=state.revision, clip_ids=("v1",), delta_frames=3),
                view,
            )
        state, _ = apply_command(
            state, UnlinkClips(expected_revision=state.revision, video_clip_id="v1"), view
        )
        state, _ = apply_command(
            state, RemoveClip(expected_revision=state.revision, clip_id="v1"), view
        )
        self.assertIsNone(state.clip("v1"))


class TrimSplitMergeTests(unittest.TestCase):
    """AC-M20-02-02/03: exact source continuity; split and merge are true inverses."""

    def test_start_trim_shifts_source_exactly_and_end_trim_needs_source(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", start=12, frames=24, source_start=0)
        state, _ = apply_command(
            state,
            TrimClip(
                expected_revision=state.revision,
                clip_id="c1",
                edge=TrimEdge.START,
                delta_frames=6,
            ),
            view,
        )
        clip = state.clip("c1")
        assert clip is not None
        self.assertEqual((clip.start_frame, clip.frames, clip.source_start_frame), (18, 18, 6))
        with rejection(self, "source_bounds"):
            apply_command(
                state,
                TrimClip(
                    expected_revision=state.revision,
                    clip_id="c1",
                    edge=TrimEdge.END,
                    delta_frames=97,
                ),
                view,
            )
        with rejection(self, "trim_bounds"):
            apply_command(
                state,
                TrimClip(
                    expected_revision=state.revision,
                    clip_id="c1",
                    edge=TrimEdge.END,
                    delta_frames=-18,
                ),
                view,
            )

    def test_split_then_merge_restores_the_exact_state(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", start=0, frames=24)
        state, _ = apply_command(
            state,
            SetEnvelope(
                expected_revision=state.revision,
                clip_id="c1",
                points=(EnvelopePoint(0, 1_000), EnvelopePoint(9, 500), EnvelopePoint(20, 0)),
            ),
            view,
        )
        before = content_of(state)
        state, receipt = apply_command(
            state,
            SplitClip(
                expected_revision=state.revision,
                clip_id="c1",
                at_offset_frames=9,
                new_clip_id="c1b",
            ),
            view,
        )
        first, second = state.clip("c1"), state.clip("c1b")
        assert first is not None and second is not None
        self.assertEqual((first.frames, second.frames), (9, 15))
        self.assertEqual(second.source_start_frame, 9)
        self.assertEqual([point.offset_frames for point in second.envelope], [0, 11])
        inverse = receipt.inverse
        state, _ = apply_command(state, inverse, view)
        self.assertEqual(content_of(state), before)

    def test_merge_requires_exact_timeline_and_source_contiguity(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", start=0, frames=12, source_start=0)
        state = added(state, view, "c2", "vid-1", start=12, frames=12, source_start=20)
        with rejection(self, "merge_incompatible"):
            apply_command(
                state,
                MergeClips(
                    expected_revision=state.revision, first_clip_id="c1", second_clip_id="c2"
                ),
                view,
            )


class SnapTests(unittest.TestCase):
    """Snap is advisory, bounded and deterministically ordered."""

    def test_candidates_order_by_distance_then_priority_then_frame(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", start=48, frames=12)
        candidates = snap_candidates(state, frame=50, playhead_frame=52)
        self.assertEqual(
            [(item.frame, item.kind) for item in candidates[:3]],
            [(51, SnapKind.GRID), (48, SnapKind.CLIP_BOUNDARY), (52, SnapKind.PLAYHEAD)],
        )
        self.assertEqual([item.distance for item in candidates[:3]], [1, 2, 2])

    def test_a_clip_boundary_outranks_a_grid_line_on_the_same_frame(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", start=51, frames=12)
        candidates = snap_candidates(state, frame=51)
        self.assertEqual(candidates[0].frame, 51)
        self.assertIs(candidates[0].kind, SnapKind.CLIP_BOUNDARY)

    def test_exclusion_and_the_candidate_ceiling_hold(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", start=48, frames=12)
        frames = [item.frame for item in snap_candidates(state, frame=50, exclude_clip_id="c1")]
        self.assertNotIn(48, frames)
        self.assertNotIn(60, frames)
        bounded = snap_candidates(state, frame=50, max_candidates=1)
        self.assertEqual(len(bounded), 1)


class EnvelopeTests(unittest.TestCase):
    """Envelopes are bounded, ordered, exact, and never silently dropped by edits."""

    def test_envelope_validation_rejects_disorder_overflow_and_ceiling(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", frames=12)
        with rejection(self, "envelope_order"):
            apply_command(
                state,
                SetEnvelope(
                    expected_revision=state.revision,
                    clip_id="c1",
                    points=(EnvelopePoint(5, 100), EnvelopePoint(5, 200)),
                ),
                view,
            )
        with rejection(self, "envelope_bounds"):
            apply_command(
                state,
                SetEnvelope(
                    expected_revision=state.revision,
                    clip_id="c1",
                    points=(EnvelopePoint(13, 100),),
                ),
                view,
            )
        with rejection(self, "envelope_points"):
            apply_command(
                state,
                SetEnvelope(
                    expected_revision=state.revision,
                    clip_id="c1",
                    points=tuple(EnvelopePoint(index, 100) for index in range(5)),
                ),
                view,
            )

    def test_a_trim_that_would_drop_a_point_rejects_instead(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", frames=12)
        state, _ = apply_command(
            state,
            SetEnvelope(
                expected_revision=state.revision, clip_id="c1", points=(EnvelopePoint(10, 700),)
            ),
            view,
        )
        with rejection(self, "envelope_bounds"):
            apply_command(
                state,
                TrimClip(
                    expected_revision=state.revision,
                    clip_id="c1",
                    edge=TrimEdge.END,
                    delta_frames=-6,
                ),
                view,
            )


class SerializationTests(unittest.TestCase):
    """Deterministic canonical serialization independent of authoring order."""

    def test_insertion_order_does_not_leak_into_the_wire_state(self) -> None:
        view = refs()
        one = added(added(fresh(view), view, "c1", "vid-1"), view, "c2", "vid-2", lane=1)
        two = added(added(fresh(view), view, "c2", "vid-2", lane=1), view, "c1", "vid-1")
        self.assertEqual(fingerprint_of(one), fingerprint_of(two))
        self.assertEqual(one.to_wire()["schema"], TIMELINE_AUTHORING_SCHEMA)


class CommandSequenceInvariantTests(unittest.TestCase):
    """AC-M20-02-02/03: generated sequences keep every invariant; undo/redo is exact."""

    def test_a_seeded_sweep_preserves_invariants_and_round_trips_undo(self) -> None:
        rng = random.Random(0x4D32_0002)  # noqa: S311 -- deterministic sweep, not crypto
        view = refs()
        state = fresh(view)
        counter = 0
        accepted = 0
        for _ in range(220):
            state, counter, accepted = self._step(state, view, rng, counter, accepted)
        self.assertGreater(accepted, 40)

    def _step(
        self,
        state: TimelineState,
        view: TimelineReferenceView,
        rng: random.Random,
        counter: int,
        accepted: int,
    ) -> tuple[TimelineState, int, int]:
        exact_before = fingerprint_of(state)
        content_before = content_of(state)
        command, counter = self._random_command(state, rng, counter)
        try:
            after_state, receipt = apply_command(state, command, view)
        except TimelineAuthoringError:
            self.assertEqual(fingerprint_of(state), exact_before)
            return state, counter, accepted
        self.assertEqual(after_state.revision, state.revision + 1)
        self._assert_invariants(after_state)
        content_after = content_of(after_state)
        undone, _ = apply_command(after_state, receipt.inverse, view)
        self.assertEqual(content_of(undone), content_before)
        redone, _ = apply_command(undone, replace(command, expected_revision=undone.revision), view)
        self.assertEqual(content_of(redone), content_after)
        return redone, counter, accepted + 1

    def _assert_invariants(self, state: TimelineState) -> None:
        for clip_id in state.selection:
            self.assertIsNotNone(state.clip(clip_id), "selection holds a dangling clip id")
        for clip in state.clips:
            self.assertGreaterEqual(clip.start_frame, 0)
            self.assertLessEqual(clip.end_frame, state.profile.max_extent_frames)
            self.assertLess(clip.lane, state.limits.max_lanes)
        for first in state.clips:
            for second in state.clips:
                if first.clip_id >= second.clip_id:
                    continue
                if first.kind is second.kind and first.lane == second.lane:
                    disjoint = (
                        first.end_frame <= second.start_frame
                        or second.end_frame <= first.start_frame
                    )
                    self.assertTrue(disjoint, "same-lane clips overlap")
        for link in state.links:
            video, audio = state.clip(link.video_clip_id), state.clip(link.audio_clip_id)
            assert video is not None and audio is not None
            self.assertEqual(video.start_frame, audio.start_frame)
            self.assertEqual(video.frames, audio.frames)
            self.assertEqual(video.start_frame % state.profile.audio_period_frames, 0)
            self.assertEqual(video.frames % state.profile.audio_period_frames, 0)

    def _random_command(
        self, state: TimelineState, rng: random.Random, counter: int
    ) -> tuple[TimelineCommand, int]:
        revision = state.revision
        clip_ids = [clip.clip_id for clip in state.clips]
        choice = rng.randrange(9)
        if choice == 0 or not clip_ids:
            counter += 1
            return (
                AddClip(
                    expected_revision=revision,
                    clip_id=f"c{counter}",
                    asset_id=rng.choice(["vid-1", "vid-2", "aud-1"]),
                    lane=rng.randrange(3),
                    start_frame=rng.randrange(0, 90),
                    frames=rng.randrange(1, 30),
                    source_start_frame=rng.randrange(0, 12),
                ),
                counter,
            )
        subject = rng.choice(clip_ids)
        if choice == 1:
            return RemoveClip(expected_revision=revision, clip_id=subject), counter
        if choice == 2:
            return (
                MoveClip(
                    expected_revision=revision,
                    clip_id=subject,
                    delta_frames=rng.randrange(-24, 25),
                    delta_lanes=rng.randrange(-1, 2),
                ),
                counter,
            )
        if choice == 3:
            size = min(len(clip_ids), rng.randrange(1, 4))
            return (
                MoveGroup(
                    expected_revision=revision,
                    clip_ids=tuple(rng.sample(clip_ids, size)),
                    delta_frames=rng.randrange(-24, 25),
                ),
                counter,
            )
        if choice == 4:
            return (
                TrimClip(
                    expected_revision=revision,
                    clip_id=subject,
                    edge=rng.choice([TrimEdge.START, TrimEdge.END]),
                    delta_frames=rng.randrange(-6, 7),
                ),
                counter,
            )
        if choice == 5:
            counter += 1
            return (
                SplitClip(
                    expected_revision=revision,
                    clip_id=subject,
                    at_offset_frames=rng.randrange(1, 12),
                    new_clip_id=f"c{counter}",
                ),
                counter,
            )
        if choice == 6:
            videos = [clip.clip_id for clip in state.clips if clip.kind is MediaKind.VIDEO]
            audios = [clip.clip_id for clip in state.clips if clip.kind is MediaKind.AUDIO]
            if videos and audios and rng.random() < 0.7:
                return (
                    LinkClips(
                        expected_revision=revision,
                        video_clip_id=rng.choice(videos),
                        audio_clip_id=rng.choice(audios),
                    ),
                    counter,
                )
            if videos:
                return (
                    UnlinkClips(expected_revision=revision, video_clip_id=rng.choice(videos)),
                    counter,
                )
            return RemoveClip(expected_revision=revision, clip_id=subject), counter
        if choice == 7:
            clip = state.clip(subject)
            assert clip is not None
            offsets = sorted(rng.sample(range(0, clip.frames + 1), min(2, clip.frames)))
            points = tuple(EnvelopePoint(offset, rng.randrange(0, 1_001)) for offset in offsets)
            return (
                SetEnvelope(expected_revision=revision, clip_id=subject, points=points),
                counter,
            )
        size = min(len(clip_ids), rng.randrange(0, 4))
        return (
            SelectClips(expected_revision=revision, clip_ids=tuple(rng.sample(clip_ids, size))),
            counter,
        )


class ArchitectureAndPrivacyTests(unittest.TestCase):
    """AC-M20-02-04: pure core, no media/host/frontend dependency, content-free wire."""

    def test_module_imports_stay_inside_the_pure_core(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "comfyui_h3_context"
            / "core"
            / "timeline_authoring.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        allowed_absolute = {
            "json",
            "re",
            "collections",
            "dataclasses",
            "enum",
            "__future__",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed_absolute)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    self.assertIn(str(node.module).split(".")[0], allowed_absolute)

    def test_wire_projections_carry_no_content_tokens(self) -> None:
        view = refs()
        state = fresh(view)
        state = added(state, view, "c1", "vid-1", frames=12)
        state, receipt = apply_command(
            state,
            SelectClips(expected_revision=state.revision, clip_ids=("c1",)),
            view,
        )
        dump = (
            repr(state.to_wire())
            + repr(receipt)
            + repr([item.to_wire() for item in snap_candidates(state, frame=10)])
        )
        for token in ("\\", "://", "C:", "/" + "tmp", ".safetensors", ".mp4"):
            self.assertNotIn(token, dump)


if __name__ == "__main__":
    unittest.main()
