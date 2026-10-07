"""M20-00 focused tests for the reference-set authoring and soundtrack-ownership domain.

The rows here hold apart the decisions a careless authoring surface collapses:

* user *intent* (include/exclude) versus producer-attributed *availability*
  (available/unavailable/unknown) — only the qualified producer channel may move availability,
  and an unknown fact blocks the queue instead of being coerced either way;
* sparse ID-based soundtrack ownership versus the legacy positional prefix — the selected
  disposition is ``KEEP_COMPAT`` and the positional mapping is proven equal to the sparse
  relations it has always produced, never silently reinterpreted;
* exact capacity — aggregate, per-kind, socket and timed limits are reported together from one
  capability input, and the remaining-capacity projection never advertises a simultaneous
  maximum the aggregate would reject;
* deterministic canonical projection — images, then each included soundtrack immediately before
  its video, then standalone audio, with backend label text proven equal to the existing
  ``core.registry`` derivation rather than restated.

No fixture carries a prompt, media value, path, URL, filename or credential.
"""

from __future__ import annotations

import ast
import unittest
from contextlib import AbstractContextManager
from pathlib import Path

from comfyui_h3_context.core.contracts import MediaKind
from comfyui_h3_context.core.reference_set_authoring import (
    H3_BASE_AGGREGATE_REFERENCE_FILES,
    LEGACY_PAIRED_AUDIO_DISPOSITION,
    REFERENCE_SET_AUTHORING_SCHEMA,
    AddSource,
    AdmittedSourceInput,
    AvailabilityFact,
    AvailabilityFactSet,
    DerivedSoundtrackState,
    ExcludeSoundtrack,
    IncludeSoundtrack,
    ReferenceCapacityInput,
    ReferenceSetAuthoringError,
    ReferenceSetState,
    RemoveSource,
    ReorderSource,
    SoundtrackAvailability,
    SoundtrackIntent,
    TimedReferenceLimits,
    apply_availability_facts,
    apply_command,
    build_h3_base_capacity,
    canonical_projection,
    capacity_projection,
    create_reference_set,
    derived_soundtrack_state,
    relations_from_legacy_positional,
)
from comfyui_h3_context.core.registry import (
    MAX_PAIRED_VIDEO_AUDIO,
    MAX_REFERENCE_IMAGES,
    MAX_REFERENCE_VIDEOS,
    MAX_STANDALONE_AUDIO,
)

FP = "sha256:" + "0" * 64
PRODUCER = "frontend.host.graphReferenceQualification"

TIMED = TimedReferenceLimits(max_duration_milliseconds=150_000, max_frames=3_600)


def capacity() -> ReferenceCapacityInput:
    return build_h3_base_capacity(authority="core.registry", fingerprint=FP, timed=TIMED)


def img(n: int) -> AdmittedSourceInput:
    return AdmittedSourceInput(
        source_id=f"img-{n}", kind=MediaKind.IMAGE, fingerprint=FP, duration_milliseconds=None
    )


def vid(n: int, duration: int = 5_000) -> AdmittedSourceInput:
    return AdmittedSourceInput(
        source_id=f"vid-{n}", kind=MediaKind.VIDEO, fingerprint=FP, duration_milliseconds=duration
    )


def aud(n: int, duration: int = 5_000) -> AdmittedSourceInput:
    return AdmittedSourceInput(
        source_id=f"aud-{n}", kind=MediaKind.AUDIO, fingerprint=FP, duration_milliseconds=duration
    )


def fresh() -> ReferenceSetState:
    return create_reference_set(capacity())


def added(state: ReferenceSetState, *sources: AdmittedSourceInput) -> ReferenceSetState:
    for source in sources:
        state, _receipt = apply_command(state, AddSource(state.revision, source))
    return state


def facts(
    state: ReferenceSetState,
    *pairs: tuple[str, SoundtrackAvailability],
    revision: int | None = None,
    producer: str = PRODUCER,
) -> ReferenceSetState:
    fact_set = AvailabilityFactSet(
        producer=producer,
        producer_revision=state.availability_revision + 1 if revision is None else revision,
        fingerprint=FP,
        facts=tuple(AvailabilityFact(video_id=v, availability=a) for v, a in pairs),
    )
    state, _receipt = apply_availability_facts(state, fact_set)
    return state


def rejection(testcase: unittest.TestCase, code: str) -> AbstractContextManager[object]:
    return testcase.assertRaisesRegex(ReferenceSetAuthoringError, code)


class CommandAuthorityTests(unittest.TestCase):
    """AC-M20-00-01: one deterministic revisioned command authority, atomic rejections."""

    def test_add_remove_reorder_produce_monotonic_revisions_and_receipts(self) -> None:
        state = fresh()
        first = state.revision
        state, receipt = apply_command(state, AddSource(state.revision, img(1)))
        self.assertEqual(receipt.revision_before, first)
        self.assertEqual(receipt.revision_after, state.revision)
        self.assertEqual(state.revision, first + 1)
        state, _ = apply_command(state, AddSource(state.revision, img(2)))
        state, _ = apply_command(state, ReorderSource(state.revision, "img-2", 0))
        self.assertEqual([e.source_id for e in state.images], ["img-2", "img-1"])
        state, _ = apply_command(state, RemoveSource(state.revision, "img-2"))
        self.assertEqual([e.source_id for e in state.images], ["img-1"])

    def test_stale_revision_and_replay_reject_atomically(self) -> None:
        state = fresh()
        command = AddSource(state.revision, img(1))
        state, _ = apply_command(state, command)
        before = state
        with rejection(self, "stale_revision"):
            apply_command(state, command)  # replay carries the consumed revision
        self.assertEqual(before, state)

    def test_duplicate_and_unknown_ids_reject_without_partial_state(self) -> None:
        state = added(fresh(), img(1))
        with rejection(self, "duplicate_source"):
            apply_command(state, AddSource(state.revision, img(1)))
        with rejection(self, "unknown_source"):
            apply_command(state, RemoveSource(state.revision, "vid-9"))
        with rejection(self, "reorder_bounds"):
            apply_command(state, ReorderSource(state.revision, "img-1", 5))
        self.assertEqual([e.source_id for e in state.images], ["img-1"])

    def test_undo_restores_canonical_projection_via_typed_inverse(self) -> None:
        state = added(fresh(), img(1), vid(1), aud(1))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        baseline = canonical_projection(state).assets
        state, receipt = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        state, _ = apply_command(state, receipt.inverse)
        self.assertEqual(canonical_projection(state).assets, baseline)

    def test_remove_inverse_restores_entry_position_and_relation(self) -> None:
        state = added(fresh(), img(1), img(2), vid(1), aud(1))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        baseline = canonical_projection(state).assets
        state, receipt = apply_command(state, RemoveSource(state.revision, "img-1"))
        state, _ = apply_command(state, receipt.inverse)
        self.assertEqual(canonical_projection(state).assets, baseline)


class SoundtrackOwnershipTests(unittest.TestCase):
    """AC-M20-00-02: sparse, ID-based, one-per-video, no prefix inference."""

    def test_include_requires_admitted_available_audio_and_video_target(self) -> None:
        state = added(fresh(), vid(1), aud(1))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        self.assertIs(derived_soundtrack_state(state, "vid-1"), DerivedSoundtrackState.INCLUDED)

    def test_sparse_pairing_does_not_require_earlier_videos(self) -> None:
        state = added(fresh(), vid(1), vid(2), aud(1))
        state = facts(state, ("vid-2", SoundtrackAvailability.AVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-2", "aud-1"))
        self.assertIs(derived_soundtrack_state(state, "vid-1"), DerivedSoundtrackState.EXCLUDED)
        self.assertIs(derived_soundtrack_state(state, "vid-2"), DerivedSoundtrackState.INCLUDED)

    def test_one_soundtrack_per_video_and_one_video_per_audio(self) -> None:
        state = added(fresh(), vid(1), vid(2), aud(1), aud(2))
        state = facts(
            state,
            ("vid-1", SoundtrackAvailability.AVAILABLE),
            ("vid-2", SoundtrackAvailability.AVAILABLE),
        )
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        with rejection(self, "soundtrack_already_included"):
            apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-2"))
        with rejection(self, "audio_already_bound"):
            apply_command(state, IncludeSoundtrack(state.revision, "vid-2", "aud-1"))

    def test_kind_confusion_and_missing_targets_reject(self) -> None:
        state = added(fresh(), img(1), vid(1), aud(1))
        with rejection(self, "kind_mismatch"):
            apply_command(state, IncludeSoundtrack(state.revision, "img-1", "aud-1"))
        with rejection(self, "kind_mismatch"):
            apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "img-1"))
        with rejection(self, "unknown_video"):
            apply_command(state, IncludeSoundtrack(state.revision, "vid-9", "aud-1"))
        with rejection(self, "unknown_audio"):
            apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-9"))

    def test_unavailable_included_intent_creates_no_audio_label_at_all(self) -> None:
        state = added(fresh(), vid(1), aud(1))
        state = facts(state, ("vid-1", SoundtrackAvailability.UNAVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        self.assertIs(derived_soundtrack_state(state, "vid-1"), DerivedSoundtrackState.UNAVAILABLE)
        projected = canonical_projection(state)
        # WP-B invariant: an excluded or unavailable soundtrack creates no audio label,
        # binding or native child -- the bound audio must not silently become standalone.
        self.assertEqual([a.source_id for a in projected.assets], ["vid-1"])
        self.assertIn(
            ("vid-1", DerivedSoundtrackState.UNAVAILABLE, "aud-1"),
            [
                (row.video_id, row.derived_state, row.soundtrack_source_id)
                for row in projected.soundtracks
            ],
        )
        # An explicit exclude unbinds the audio, which visibly returns to standalone.
        state, _ = apply_command(state, ExcludeSoundtrack(state.revision, "vid-1"))
        projected = canonical_projection(state)
        self.assertEqual([a.source_id for a in projected.assets], ["vid-1", "aud-1"])
        self.assertIsNone(projected.assets[1].paired_with)


class AvailabilityAuthorityTests(unittest.TestCase):
    """AC-M20-00-02A: only the producer channel moves availability; unknown blocks queue."""

    def test_default_availability_is_unknown_and_blocks_included_intent(self) -> None:
        state = added(fresh(), vid(1), aud(1))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        self.assertIs(
            derived_soundtrack_state(state, "vid-1"),
            DerivedSoundtrackState.BLOCKED_UNKNOWN,
        )
        projected = canonical_projection(state)
        self.assertTrue(projected.queue_blockers)
        self.assertIn("vid-1", [b.video_id for b in projected.queue_blockers])

    def test_user_commands_never_move_availability(self) -> None:
        state = added(fresh(), vid(1), aud(1))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        self.assertIs(state.availability_of("vid-1"), SoundtrackAvailability.UNKNOWN)
        state, _ = apply_command(state, ExcludeSoundtrack(state.revision, "vid-1"))
        self.assertIs(state.availability_of("vid-1"), SoundtrackAvailability.UNKNOWN)

    def test_stale_producer_revision_rejects(self) -> None:
        state = added(fresh(), vid(1))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        with rejection(self, "stale_availability"):
            facts(state, ("vid-1", SoundtrackAvailability.UNKNOWN), revision=1)

    def test_second_producer_identity_rejects(self) -> None:
        state = added(fresh(), vid(1))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        with rejection(self, "availability_producer_conflict"):
            facts(
                state,
                ("vid-1", SoundtrackAvailability.AVAILABLE),
                producer="browser.ui.spoof",
            )

    def test_facts_for_unknown_videos_reject(self) -> None:
        state = added(fresh(), img(1))
        with rejection(self, "unknown_video"):
            facts(state, ("vid-9", SoundtrackAvailability.AVAILABLE))


class CapacityTests(unittest.TestCase):
    """AC-M20-00-03: aggregate, per-kind, socket and timed limits enforced together."""

    def test_per_kind_socket_ceilings_are_the_registry_authorities(self) -> None:
        cap = capacity()
        self.assertEqual(cap.image_max, MAX_REFERENCE_IMAGES)
        self.assertEqual(cap.video_max, MAX_REFERENCE_VIDEOS)
        self.assertEqual(cap.paired_audio_max, MAX_PAIRED_VIDEO_AUDIO)
        self.assertEqual(cap.standalone_audio_max, MAX_STANDALONE_AUDIO)
        self.assertEqual(cap.aggregate_max, H3_BASE_AGGREGATE_REFERENCE_FILES)

    def test_aggregate_twelve_rejects_the_thirteenth_file(self) -> None:
        state = added(fresh(), *[img(n) for n in range(1, 10)], vid(1), vid(2), vid(3))
        self.assertEqual(capacity_projection(state).aggregate_remaining, 0)
        with rejection(self, "capacity_aggregate"):
            apply_command(state, AddSource(state.revision, aud(1)))

    def test_per_kind_ceiling_rejects_before_aggregate_room_is_exhausted(self) -> None:
        state = added(fresh(), vid(1), vid(2), vid(3))
        with rejection(self, "capacity_video"):
            apply_command(state, AddSource(state.revision, vid(4)))

    def test_standalone_audio_ceiling_and_pairing_interplay(self) -> None:
        state = added(fresh(), vid(1), aud(1), aud(2), aud(3))
        with rejection(self, "capacity_standalone_audio"):
            apply_command(state, AddSource(state.revision, aud(4)))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        state, _ = apply_command(state, AddSource(state.revision, aud(4)))
        with rejection(self, "capacity_standalone_audio"):
            apply_command(state, ExcludeSoundtrack(state.revision, "vid-1"))

    def test_removing_a_paired_video_cannot_overflow_standalone_audio(self) -> None:
        # Found by the state-machine sweep: freeing a bound audio through video removal
        # must respect the standalone ceiling it re-enters, exactly like exclude does.
        state = added(fresh(), vid(1), aud(1), aud(2), aud(3))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        state, _ = apply_command(state, AddSource(state.revision, aud(4)))
        with rejection(self, "capacity_standalone_audio"):
            apply_command(state, RemoveSource(state.revision, "vid-1"))

    def test_remaining_capacity_never_advertises_an_impossible_maximum(self) -> None:
        state = added(fresh(), *[img(n) for n in range(1, 10)], vid(1), vid(2))
        projection = capacity_projection(state)
        self.assertEqual(projection.aggregate_remaining, 1)
        self.assertEqual(projection.video_remaining, 1)
        self.assertEqual(projection.image_remaining, 0)
        self.assertEqual(projection.standalone_audio_remaining, 1)
        self.assertEqual(projection.paired_audio_remaining, 1)

    def test_timed_reference_limits_apply_to_timed_kinds_only(self) -> None:
        state = fresh()
        with rejection(self, "timed_duration"):
            apply_command(state, AddSource(state.revision, vid(1, duration=150_001)))
        with rejection(self, "missing_duration"):
            apply_command(
                state,
                AddSource(
                    state.revision,
                    AdmittedSourceInput(
                        source_id="vid-x",
                        kind=MediaKind.VIDEO,
                        fingerprint=FP,
                        duration_milliseconds=None,
                    ),
                ),
            )
        with self.assertRaises(ReferenceSetAuthoringError):
            AdmittedSourceInput(
                source_id="img-x",
                kind=MediaKind.IMAGE,
                fingerprint=FP,
                duration_milliseconds=1_000,
            )


class CanonicalProjectionTests(unittest.TestCase):
    """AC-M20-00-04: deterministic order and labels joined by stable identity."""

    def test_included_soundtrack_projects_immediately_before_its_video(self) -> None:
        state = added(fresh(), img(1), vid(1), vid(2), aud(1), aud(2))
        state = facts(state, ("vid-2", SoundtrackAvailability.AVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-2", "aud-2"))
        projected = canonical_projection(state)
        self.assertEqual(
            [a.source_id for a in projected.assets],
            ["img-1", "vid-1", "aud-2", "vid-2", "aud-1"],
        )
        by_id = {a.source_id: a for a in projected.assets}
        self.assertEqual(by_id["aud-2"].paired_with, "vid-2")
        self.assertIsNone(by_id["aud-1"].paired_with)

    def test_labels_agree_with_the_registry_derivation(self) -> None:
        state = added(fresh(), img(1), img(2), vid(1), aud(1), aud(2))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        projected = canonical_projection(state)
        self.assertEqual(
            [a.label for a in projected.assets],
            ["<Picture 1>", "<Picture 2>", "<Audio 1>", "<Video 1>", "<Audio 2>"],
        )

    def test_registry_cross_check_over_the_same_canonical_sequence(self) -> None:
        from comfyui_h3_context.core.contracts import AssetRole
        from comfyui_h3_context.core.registry import (
            ReferenceAsset,
            build_reference_registry,
        )

        state = added(fresh(), img(1), vid(1), aud(1))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        state, _ = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        projected = canonical_projection(state)
        assets = []
        for order, asset in enumerate(projected.assets, start=1):
            assets.append(
                ReferenceAsset(
                    asset_id=asset.source_id,
                    kind=asset.kind,
                    role=AssetRole.REFERENCE,
                    connection_order=order,
                    paired_video_id=asset.paired_with,
                )
            )
        registry = build_reference_registry(tuple(assets))
        for asset in projected.assets:
            self.assertEqual(asset.label, registry.label_for(asset.source_id).label)

    def test_reorder_is_deterministic_and_projection_follows_user_order(self) -> None:
        state = added(fresh(), vid(1), vid(2))
        state, _ = apply_command(state, ReorderSource(state.revision, "vid-2", 0))
        projected = canonical_projection(state)
        self.assertEqual([a.source_id for a in projected.assets], ["vid-2", "vid-1"])


class LegacyDispositionTests(unittest.TestCase):
    """AC-M20-00-06: exactly one disposition, KEEP_COMPAT, no silent reinterpretation."""

    def test_disposition_constant_is_keep_compat(self) -> None:
        self.assertEqual(LEGACY_PAIRED_AUDIO_DISPOSITION, "KEEP_COMPAT")

    def test_positional_prefix_maps_to_identical_sparse_relations(self) -> None:
        relations = relations_from_legacy_positional(
            video_ids=("vid-1", "vid-2", "vid-3"), paired_audio_ids=("aud-1", "aud-2")
        )
        self.assertEqual(
            [(r.video_id, r.intent, r.soundtrack_source_id) for r in relations],
            [
                ("vid-1", SoundtrackIntent.INCLUDED, "aud-1"),
                ("vid-2", SoundtrackIntent.INCLUDED, "aud-2"),
            ],
        )

    def test_prefix_longer_than_videos_rejects_like_the_node_boundary(self) -> None:
        with rejection(self, "legacy_prefix_bounds"):
            relations_from_legacy_positional(
                video_ids=("vid-1",), paired_audio_ids=("aud-1", "aud-2")
            )


class ArchitectureAndPrivacyTests(unittest.TestCase):
    """AC-M20-00-05: no host/media/frontend/provider import; content-free wire."""

    def test_module_imports_stay_inside_stdlib_and_core(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "comfyui_h3_context"
            / "core"
            / "reference_set_authoring.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        forbidden = {"torch", "comfy", "requests", "aiohttp", "PIL", "numpy", "server"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden)
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                self.assertNotIn((node.module or "").split(".")[0], forbidden)

    def test_wire_projections_carry_no_locator_shaped_values(self) -> None:
        state = added(fresh(), img(1), vid(1), aud(1))
        state = facts(state, ("vid-1", SoundtrackAvailability.AVAILABLE))
        state, receipt = apply_command(state, IncludeSoundtrack(state.revision, "vid-1", "aud-1"))
        wires = [
            state.to_wire(),
            receipt.to_wire(),
            canonical_projection(state).to_wire(),
            capacity_projection(state).to_wire(),
        ]

        def scan(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertIsInstance(key, str)
                    scan(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    scan(item)
            elif isinstance(value, str):
                # "/" + "tmp" avoids the S108 literal while still catching a POSIX temp path.
                for token in ("\\", "://", "C:", "/" + "tmp", ".safetensors", ".mp4"):
                    self.assertNotIn(token, value)

        for wire in wires:
            scan(wire)
        self.assertEqual(state.to_wire()["schema"], REFERENCE_SET_AUTHORING_SCHEMA)

    def test_capacity_input_validation_bounds(self) -> None:
        with self.assertRaises(ReferenceSetAuthoringError):
            ReferenceCapacityInput(
                authority="x",
                fingerprint="not-a-fingerprint",
                aggregate_max=12,
                image_max=9,
                video_max=3,
                paired_audio_max=3,
                standalone_audio_max=3,
                timed=TIMED,
            )
        with self.assertRaises(ReferenceSetAuthoringError):
            ReferenceCapacityInput(
                authority="core.registry",
                fingerprint=FP,
                aggregate_max=0,
                image_max=9,
                video_max=3,
                paired_audio_max=3,
                standalone_audio_max=3,
                timed=TIMED,
            )


if __name__ == "__main__":
    unittest.main()


class CommandSequenceInvariantTests(unittest.TestCase):
    """Plan §5.1: a deterministic state-machine sweep over generated command sequences."""

    def test_generated_sequences_preserve_every_structural_invariant(self) -> None:
        import random

        rng = random.Random(0x4D32_0000)  # noqa: S311 -- deterministic sweep, not crypto
        state = fresh()
        counter = 0
        for _step in range(400):
            counter += 1
            choice = rng.randrange(6)
            all_ids = [e.source_id for e in state.images + state.videos + state.audios]
            try:
                if choice == 0:
                    kind = rng.choice([img, vid, aud])
                    state, _ = apply_command(state, AddSource(state.revision, kind(1000 + counter)))
                elif choice == 1 and all_ids:
                    state, _ = apply_command(
                        state, RemoveSource(state.revision, rng.choice(all_ids))
                    )
                elif choice == 2 and all_ids:
                    state, _ = apply_command(
                        state,
                        ReorderSource(state.revision, rng.choice(all_ids), rng.randrange(4)),
                    )
                elif choice == 3 and state.videos and state.audios:
                    state, _ = apply_command(
                        state,
                        IncludeSoundtrack(
                            state.revision,
                            rng.choice(state.videos).source_id,
                            rng.choice(state.audios).source_id,
                        ),
                    )
                elif choice == 4 and state.videos:
                    state, _ = apply_command(
                        state,
                        ExcludeSoundtrack(state.revision, rng.choice(state.videos).source_id),
                    )
                elif choice == 5 and state.videos:
                    state = facts(
                        state,
                        (
                            rng.choice(state.videos).source_id,
                            rng.choice(list(SoundtrackAvailability)),
                        ),
                    )
            except ReferenceSetAuthoringError:
                continue  # a typed rejection must leave the bound state untouched

            capacity_view = capacity_projection(state)
            self.assertLessEqual(capacity_view.aggregate_used, capacity_view.aggregate_max)
            self.assertLessEqual(len(state.images), state.capacity.image_max)
            self.assertLessEqual(len(state.videos), state.capacity.video_max)
            self.assertLessEqual(len(state.bound_audio_ids()), state.capacity.paired_audio_max)
            self.assertLessEqual(
                len(state.audios) - len(state.bound_audio_ids()),
                state.capacity.standalone_audio_max,
            )
            projected = canonical_projection(state)
            seen: set[str] = set()
            previous: str | None = None
            for asset in projected.assets:
                self.assertNotIn(asset.source_id, seen)
                seen.add(asset.source_id)
                if asset.paired_with is not None:
                    self.assertIs(asset.kind, MediaKind.AUDIO)
                bound_next = asset.paired_with
                if previous is not None:
                    self.assertEqual(previous, asset.source_id)
                previous = bound_next
            self.assertIsNone(previous)  # a pair is always closed by its video
