"""M20-01 focused tests for the H3 temporal capability profile.

The matrix these rows have to hold apart is the one a careless profile collapses. Video-run
validity, exact audiovisual boundary alignment and target audio-latent admission are three
different contract decisions, and so are the two provenance classes: what the adapter does with a
submitted length is readable from a pinned source and true now, while what the model can do is only
true once an accepted `M19-07` row says so.

Nothing here re-implements the arithmetic it checks. The lattice comes from
`comfyui_h3_context.core.length`, which `M17-25` made the single authority, and the parity rows read
`official_temporal_parity`, the one fixture that transcribes upstream independently.
"""

from __future__ import annotations

import json
import re
import unittest
from fractions import Fraction
from pathlib import Path

from jsonschema import Draft202012Validator
from official_temporal_parity import (
    NATIVE_NODE_BYTES,
    NATIVE_NODE_RETRIEVED,
    NATIVE_NODE_REVISION,
    NATIVE_NODE_SHA256,
    OFFICIAL_TEMPLATE_REVISION,
    native_temporal_shape,
    native_video_latent_t,
)
from test_package_boundary import import_blocker_script, run_isolated

from comfyui_h3_context.core.contracts import EvidenceLevel
from comfyui_h3_context.core.fingerprint_domain import (
    FingerprintDomainError,
    IdentityDomain,
    require_semantic_identity,
)
from comfyui_h3_context.core.length import (
    LATTICE_OFFSET,
    LATTICE_STEP,
    MAX_FRAME_COUNT,
    MAX_PRODUCIBLE_FRAME_COUNT,
    MIN_FRAME_COUNT,
    TRAINED_MAX_FRAME_COUNT,
    TRAINED_MIN_FRAME_COUNT,
)
from comfyui_h3_context.core.temporal_profile import (
    ADAPTER_FACTS,
    AUDIO_LATENT_FPS,
    AUDIO_LATENT_RATIO,
    CONTEXT_EXTENT_GRID_FRAMES,
    EXACT_AUDIO_PERIOD_FRAMES,
    MINIMUM_CONTEXT_EXTENT_FRAMES,
    NATIVE_NODE_SOURCE,
    TEMPORAL_PROFILE_SCHEMA,
    AcceptedQualification,
    AudioTargetPolicy,
    CapabilityStatus,
    CropPlan,
    FrameRange,
    ProvenanceClass,
    QualifiedRow,
    TemporalProfileError,
    TrainedRangeReport,
    admit_audio_latent_target,
    align_run,
    audio_boundary_is_exact,
    build_temporal_profile,
    check_crop_conservation,
    context_extent_is_on_grid,
    exact_audio_latent_length,
    nominal_audio_latent_length,
    video_latent_length,
)

ROOT = Path(__file__).resolve().parents[1]

#: The candidate runs the plan names. They are lattice members, chosen so the data separates the
#: three decisions rather than the assertions merely claiming they are separate.
CANDIDATES = (5, 22, 39, 90, 141, 124)

#: Exact and non-exact companions. The exact set is three members spanning two periods, so a rule
#: that happens to be right about 39 alone cannot pass.
EXACT_RUNS = (39, 90, 141)
NON_EXACT_RUNS = (5, 22, 56, 73, 107, 124)


def accepted_matrix(
    temporal: CapabilityStatus = CapabilityStatus.SUPPORTED,
) -> AcceptedQualification:
    """The accepted `M19-07` projection, in the exact shape it was closed with.

    One `supported` row and four `unqualified` ones -- not four absences. `M19-07`'s distinct review
    overturned an `unsupported` verdict for the mask row, so "we did not establish this" is the only
    honest state for the other four and the fixture says so.
    """

    return AcceptedQualification(
        subject_identity=NATIVE_NODE_SHA256,
        rows=(
            QualifiedRow(
                row="temporal_profile",
                status=temporal,
                reason_code="derived_from_exact_source_and_confirmed_live",
                consumer="M20-01",
            ),
            QualifiedRow(
                row="joint_av_latent_descriptor",
                status=CapabilityStatus.UNQUALIFIED,
                reason_code="requires_weight_backed_execution",
                consumer="M20-04",
            ),
            QualifiedRow(
                row="completed_boundary_resume",
                status=CapabilityStatus.UNQUALIFIED,
                reason_code="requires_weight_backed_execution",
                consumer="M20-05",
            ),
            QualifiedRow(
                row="dual_domain_av_mask",
                status=CapabilityStatus.UNQUALIFIED,
                reason_code="requires_weight_backed_execution",
                consumer="M20-06",
            ),
            QualifiedRow(
                row="two_ended_av_bridge",
                status=CapabilityStatus.UNQUALIFIED,
                reason_code="requires_weight_backed_execution",
                consumer="M20-07",
            ),
        ),
    )


class ProvenanceClassTests(unittest.TestCase):
    """AC-M20-01-02. Two classes, declared as typed members, joined by different evidence."""

    def test_every_member_declares_exactly_one_class(self) -> None:
        profile = build_temporal_profile()
        facts = {item.name for item in profile.adapter_facts}
        claims = {item.name for item in profile.capability_claims}
        self.assertTrue(facts)
        self.assertTrue(claims)
        self.assertEqual(facts & claims, set())
        for name in facts:
            self.assertIs(profile.provenance_of(name), ProvenanceClass.ADAPTER_FACT)
        for name in claims:
            self.assertIs(profile.provenance_of(name), ProvenanceClass.MODEL_CAPABILITY)
        with self.assertRaises(TemporalProfileError):
            profile.provenance_of("not_a_declared_member")

    def test_adapter_facts_resolve_with_no_accepted_row_at_all(self) -> None:
        """The whole point of option B: the arithmetic does not wait for a model."""

        profile = build_temporal_profile()
        self.assertEqual(len(profile.adapter_facts), len(ADAPTER_FACTS))
        for fact in profile.adapter_facts:
            self.assertIs(fact.provenance, ProvenanceClass.ADAPTER_FACT)
            self.assertIs(fact.evidence_level, EvidenceLevel.OFFICIAL)
            self.assertTrue(fact.source.label)
            self.assertTrue(fact.source.revision)
            self.assertRegex(fact.source.retrieved_on, r"^\d{4}-\d{2}-\d{2}$")

    def test_capability_claims_are_unqualified_rather_than_absent_or_defaulted(self) -> None:
        profile = build_temporal_profile()
        self.assertTrue(profile.capability_claims)
        for claim in profile.capability_claims:
            self.assertIs(claim.status, CapabilityStatus.UNQUALIFIED)
            self.assertFalse(claim.qualified)
            self.assertEqual(claim.reason_code, "no_accepted_row")
            # Not a lesser level: an unqualified capability has no evidence to grade.
            self.assertIsNone(claim.evidence_level)

    def test_an_accepted_supported_row_instantiates_only_its_own_claim(self) -> None:
        profile = build_temporal_profile(accepted_matrix())
        trained = profile.capability_claim("trained_frame_range")
        self.assertIs(trained.status, CapabilityStatus.SUPPORTED)
        self.assertTrue(trained.qualified)
        self.assertIs(trained.evidence_level, EvidenceLevel.OFFICIAL)
        self.assertEqual(trained.reason_code, "derived_from_exact_source_and_confirmed_live")
        for name in (
            "joint_av_latent_descriptor",
            "completed_boundary_resume",
            "dual_domain_av_mask",
            "two_ended_av_bridge",
        ):
            claim = profile.capability_claim(name)
            self.assertIs(claim.status, CapabilityStatus.UNQUALIFIED)
            self.assertFalse(claim.qualified)

    def test_no_adapter_fact_is_presented_as_a_qualified_capability(self) -> None:
        """The failure this split exists to prevent, asserted on the projection a consumer reads."""

        for accepted in (None, accepted_matrix()):
            profile = build_temporal_profile(accepted)
            facts = profile.to_wire()["adapter_facts"]
            assert isinstance(facts, list)
            for fact in facts:
                assert isinstance(fact, dict)
                self.assertEqual(fact["provenance"], "adapter_fact")
                self.assertNotIn("status", fact)
                self.assertNotIn("row", fact)

    def test_a_non_supported_row_instantiates_nothing(self) -> None:
        for status in (
            CapabilityStatus.UNSUPPORTED,
            CapabilityStatus.UNQUALIFIED,
            CapabilityStatus.RETIRED,
        ):
            with self.subTest(status=status):
                profile = build_temporal_profile(accepted_matrix(status))
                self.assertIs(profile.trained_frame_range.status, status)
                self.assertIsNone(profile.trained_frame_range.value)

    def test_evidence_from_a_different_subject_is_refused_not_joined(self) -> None:
        drifted = AcceptedQualification(
            subject_identity="sha256:" + "0" * 64,
            rows=accepted_matrix().rows,
        )
        with self.assertRaises(TemporalProfileError) as caught:
            build_temporal_profile(drifted)
        self.assertEqual(str(caught.exception), "subject_identity_drift")

    def test_the_pinned_subject_is_the_identity_m19_07_recorded(self) -> None:
        self.assertEqual(NATIVE_NODE_SOURCE.digest, NATIVE_NODE_SHA256)
        self.assertEqual(NATIVE_NODE_SOURCE.revision, NATIVE_NODE_REVISION)
        self.assertEqual(NATIVE_NODE_SOURCE.byte_length, NATIVE_NODE_BYTES)
        self.assertEqual(NATIVE_NODE_SOURCE.retrieved_on, NATIVE_NODE_RETRIEVED)


class AlignmentTests(unittest.TestCase):
    """AC-M20-01-07. Snapping is permitted; silence is not."""

    def test_off_lattice_requests_snap_up_and_say_so_in_every_residue_class(self) -> None:
        for residue in range(LATTICE_STEP):
            requested = LATTICE_OFFSET + LATTICE_STEP * 4 + residue
            with self.subTest(residue=residue):
                result = align_run(requested)
                self.assertTrue(result.producible)
                self.assertEqual(result.requested_frames, requested)
                self.assertEqual(result.snapped, residue != 0)
                self.assertGreaterEqual(result.require(), requested)
                self.assertLess(result.require() - requested, LATTICE_STEP)

    def test_a_request_below_the_floor_aligns_to_the_shortest_run(self) -> None:
        for requested in range(0, MIN_FRAME_COUNT + 1):
            with self.subTest(requested=requested):
                result = align_run(requested)
                self.assertEqual(result.require(), MIN_FRAME_COUNT)
                self.assertEqual(result.snapped, requested != MIN_FRAME_COUNT)

    def test_no_path_yields_an_aligned_value_without_its_marker(self) -> None:
        """A fitness row, not a behaviour row: the object has no value-only shape to return."""

        result = align_run(100)
        self.assertEqual(
            set(result.__slots__),
            {"requested_frames", "producible", "frame_count", "snapped", "refusal"},
        )
        with self.assertRaises(TemporalProfileError):
            type(result)(100, True, 107, True, "unreachable")
        with self.assertRaises(TemporalProfileError):
            type(result)(100, False, 107, False, None)

    def test_the_operation_is_total_over_the_accepted_range(self) -> None:
        """Every accepted value gets an answer, and the only refusal is the real one."""

        refused = []
        for requested in range(MIN_FRAME_COUNT, MAX_FRAME_COUNT + 1):
            result = align_run(requested)
            if result.producible:
                self.assertIsNotNone(result.frame_count)
            else:
                refused.append(requested)
                self.assertEqual(result.refusal, "above_producible_ceiling")
        self.assertEqual(refused, list(range(MAX_PRODUCIBLE_FRAME_COUNT + 1, MAX_FRAME_COUNT + 1)))

    def test_a_request_beyond_the_accepted_range_is_refused_by_name(self) -> None:
        result = align_run(MAX_FRAME_COUNT + 1)
        self.assertFalse(result.producible)
        self.assertEqual(result.refusal, "above_accepted_range")
        with self.assertRaises(TemporalProfileError):
            result.require()

    def test_a_non_integer_request_is_refused_rather_than_coerced(self) -> None:
        for bad in (-1, True, 4.5, "124", None):
            with self.subTest(bad=bad), self.assertRaises(TemporalProfileError):
                align_run(bad)  # type: ignore[arg-type]


class AudioBoundaryTests(unittest.TestCase):
    """AC-M20-01-06. The exact subset is derived from the rates, never enumerated."""

    def test_the_ratio_and_period_are_derived_from_the_declared_rates(self) -> None:
        self.assertEqual(AUDIO_LATENT_RATIO, Fraction(AUDIO_LATENT_FPS, 24))
        self.assertEqual(AUDIO_LATENT_RATIO, Fraction(5, 3))
        self.assertEqual(EXACT_AUDIO_PERIOD_FRAMES, AUDIO_LATENT_RATIO.denominator)

    def test_the_named_candidates_split_exact_from_rounded(self) -> None:
        for frame_count in EXACT_RUNS:
            with self.subTest(frame_count=frame_count):
                self.assertTrue(audio_boundary_is_exact(frame_count))
                self.assertEqual(exact_audio_latent_length(frame_count).denominator, 1)
        for frame_count in NON_EXACT_RUNS:
            with self.subTest(frame_count=frame_count):
                self.assertFalse(audio_boundary_is_exact(frame_count))
                self.assertNotEqual(exact_audio_latent_length(frame_count).denominator, 1)

    def test_the_exact_subset_recurs_every_fifty_one_frames_inside_the_lattice(self) -> None:
        lattice = [
            LATTICE_OFFSET + LATTICE_STEP * k
            for k in range((MAX_PRODUCIBLE_FRAME_COUNT - LATTICE_OFFSET) // LATTICE_STEP + 1)
        ]
        exact = [n for n in lattice if audio_boundary_is_exact(n)]
        self.assertEqual(exact[:4], [39, 90, 141, 192])
        steps = {b - a for a, b in zip(exact, exact[1:], strict=False)}
        self.assertEqual(steps, {CONTEXT_EXTENT_GRID_FRAMES})

    def test_a_hard_coded_list_or_a_parity_rule_would_be_wrong(self) -> None:
        """R3's mutation check: three candidate wrong rules, each refuted by the data.

        Each is evaluated against the real predicate over a window of the lattice and has to
        disagree somewhere. A rule that merely happens to be right about the named candidates is
        the failure mode this row exists to catch.
        """

        window = [LATTICE_OFFSET + LATTICE_STEP * k for k in range(20)]
        truth = {n: audio_boundary_is_exact(n) for n in window}

        enumerated = {39, 90, 141}
        equality = 39
        for n in window:
            k = (n - LATTICE_OFFSET) // LATTICE_STEP
            with self.subTest(frame_count=n):
                self.assertEqual(truth[n], k % 3 == 2)
        self.assertTrue(any(truth[n] != (n in enumerated) for n in window))
        self.assertTrue(any(truth[n] != (n == equality) for n in window))
        self.assertTrue(
            any(truth[n] != (((n - LATTICE_OFFSET) // LATTICE_STEP) % 2 == 0) for n in window)
        )

    def test_the_three_decisions_are_not_conflated(self) -> None:
        """A run can be valid and non-exact, and an extent is a third answer again."""

        for frame_count in CANDIDATES:
            with self.subTest(frame_count=frame_count):
                alignment = align_run(frame_count)
                self.assertTrue(alignment.producible)
                self.assertFalse(alignment.snapped)
                exact = audio_boundary_is_exact(frame_count)
                target = admit_audio_latent_target(
                    frame_count,
                    authoritative_target=(
                        None if exact else nominal_audio_latent_length(frame_count)
                    ),
                )
                self.assertEqual(target.exact, exact)
        self.assertTrue(any(not audio_boundary_is_exact(n) for n in CANDIDATES))
        self.assertTrue(any(audio_boundary_is_exact(n) for n in CANDIDATES))

    def test_no_frame_count_ever_reaches_the_rounding_tie(self) -> None:
        """The join is fully determined, which is stronger than a stated tie-break."""

        fractional = {
            exact_audio_latent_length(n) % 1 for n in range(MIN_FRAME_COUNT, MAX_FRAME_COUNT + 1)
        }
        self.assertEqual(fractional, {Fraction(0), Fraction(1, 3), Fraction(2, 3)})
        self.assertNotIn(Fraction(1, 2), fractional)


class TargetAdmissionTests(unittest.TestCase):
    """AC-M20-01-03. Non-integral is admitted; invented is not."""

    def test_an_exact_run_needs_no_external_authority(self) -> None:
        target = admit_audio_latent_target(39)
        self.assertIs(target.policy, AudioTargetPolicy.EXACT)
        self.assertTrue(target.exact)
        self.assertEqual(target.audio_latent_length, 65)
        self.assertEqual(target.direction, "exact")

    def test_the_research_case_is_admitted_with_the_adapters_own_target(self) -> None:
        """124 frames map to 206 and two thirds steps; the adapter says 207."""

        self.assertEqual(exact_audio_latent_length(124), Fraction(620, 3))
        target = admit_audio_latent_target(124, authoritative_target=207)
        self.assertIs(target.policy, AudioTargetPolicy.AUTHORITATIVE_TARGET)
        self.assertFalse(target.exact)
        self.assertEqual(target.audio_latent_length, 207)
        self.assertEqual(target.nominal, 207)
        self.assertEqual(target.direction, "up")

    def test_a_non_integral_run_is_not_rejected_merely_for_being_non_integral(self) -> None:
        for frame_count in NON_EXACT_RUNS:
            with self.subTest(frame_count=frame_count):
                nominal = nominal_audio_latent_length(frame_count)
                target = admit_audio_latent_target(frame_count, authoritative_target=nominal)
                self.assertEqual(target.audio_latent_length, nominal)

    def test_a_missing_target_on_a_non_integral_run_is_refused_by_name(self) -> None:
        with self.assertRaises(TemporalProfileError) as caught:
            admit_audio_latent_target(124)
        self.assertEqual(str(caught.exception), "authoritative_audio_latent_target_required")

    def test_a_conflicting_target_is_reported_rather_than_absorbed(self) -> None:
        for frame_count, supplied in ((124, 206), (124, 208), (39, 64), (39, 66)):
            with self.subTest(frame_count=frame_count, supplied=supplied):
                with self.assertRaises(TemporalProfileError) as caught:
                    admit_audio_latent_target(frame_count, authoritative_target=supplied)
                self.assertEqual(str(caught.exception), "conflicting_audio_latent_target")

    def test_an_off_lattice_run_fails_before_any_target_question_is_asked(self) -> None:
        for frame_count in (4, 6, 21, 23, 40, 123, 125):
            with self.subTest(frame_count=frame_count):
                with self.assertRaises(TemporalProfileError) as caught:
                    admit_audio_latent_target(frame_count, authoritative_target=1)
                self.assertEqual(str(caught.exception), "frame_count_not_producible")


class AdapterParityTests(unittest.TestCase):
    """AC-M20-01-08. Parity over a contiguous range, against a transcription of upstream."""

    def test_alignment_and_both_latent_lengths_match_the_published_derivation(self) -> None:
        divergences: list[str] = []
        checked = 0
        for requested in range(0, MAX_FRAME_COUNT + 1):
            native_frames, native_video, native_audio = native_temporal_shape(requested)
            result = align_run(requested)
            if not result.producible:
                # The one deliberate divergence, and it is the accepted range's own ceiling:
                # upstream would align these above the input bound it declares.
                if native_frames <= MAX_FRAME_COUNT:
                    divergences.append(f"refused {requested} that upstream produces")
                continue
            checked += 1
            frame_count = result.require()
            if frame_count != native_frames:
                divergences.append(f"align({requested}): {frame_count} vs {native_frames}")
            if video_latent_length(frame_count) != native_video:
                divergences.append(f"video({frame_count})")
            if nominal_audio_latent_length(frame_count) != native_audio:
                divergences.append(f"audio({frame_count})")
        self.assertEqual(divergences, [])
        self.assertEqual(checked, MAX_PRODUCIBLE_FRAME_COUNT + 1)

    def test_the_short_run_branch_is_covered_and_is_not_an_extrapolation(self) -> None:
        for frame_count in range(0, LATTICE_OFFSET + 1):
            with self.subTest(frame_count=frame_count):
                native = native_video_latent_t(frame_count)
                self.assertEqual(video_latent_length(frame_count), native)
                self.assertEqual(video_latent_length(frame_count), 2)
        self.assertEqual(video_latent_length(LATTICE_OFFSET + LATTICE_STEP), 7)

    def test_the_source_identity_travels_with_the_parity_claim(self) -> None:
        self.assertRegex(NATIVE_NODE_SHA256, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(NATIVE_NODE_REVISION, r"^[0-9a-f]{40}$")
        self.assertRegex(NATIVE_NODE_RETRIEVED, r"^\d{4}-\d{2}-\d{2}$")
        self.assertGreater(NATIVE_NODE_BYTES, 0)
        # The duration side of the same shared fixture. M17-25 recorded this revision but
        # asserted nothing about it; now that both parity claims read one fixture, the pin
        # is checked rather than merely written down.
        self.assertRegex(OFFICIAL_TEMPLATE_REVISION, r"^[0-9a-f]{40}$")


class RangeSeparationTests(unittest.TestCase):
    """AC-M20-01-09. Two bounds, two kinds, two provenance classes."""

    def test_the_two_ranges_are_separate_members_with_separate_evidence(self) -> None:
        profile = build_temporal_profile(accepted_matrix())
        accepted = profile.accepted_frame_range
        trained = profile.trained_frame_range
        self.assertIsInstance(accepted, FrameRange)
        self.assertIsInstance(trained, TrainedRangeReport)
        self.assertEqual((accepted.minimum, accepted.maximum), (MIN_FRAME_COUNT, MAX_FRAME_COUNT))
        self.assertIsNotNone(trained.value)
        assert trained.value is not None
        self.assertEqual(
            (trained.value.minimum, trained.value.maximum),
            (TRAINED_MIN_FRAME_COUNT, TRAINED_MAX_FRAME_COUNT),
        )
        self.assertIs(profile.provenance_of("accepted_frame_range"), ProvenanceClass.ADAPTER_FACT)
        self.assertIs(
            profile.provenance_of("trained_frame_range"), ProvenanceClass.MODEL_CAPABILITY
        )

    def test_the_trained_range_keeps_its_published_hedge(self) -> None:
        profile = build_temporal_profile(accepted_matrix())
        assert profile.trained_frame_range.value is not None
        self.assertTrue(profile.trained_frame_range.value.hedged)
        self.assertFalse(profile.accepted_frame_range.hedged)
        self.assertIn("untested", profile.capability_claim("trained_frame_range").summary)

    def test_a_length_inside_accepted_but_outside_trained_is_admitted_and_marked(self) -> None:
        profile = build_temporal_profile(accepted_matrix())
        assert profile.trained_frame_range.value is not None
        untested = LATTICE_OFFSET + LATTICE_STEP * 40
        self.assertGreater(untested, TRAINED_MAX_FRAME_COUNT)
        self.assertTrue(align_run(untested).producible)
        self.assertTrue(profile.accepted_frame_range.contains(untested))
        self.assertFalse(profile.trained_frame_range.value.contains(untested))

    def test_unqualified_is_a_different_answer_from_no_trained_bound(self) -> None:
        unqualified = build_temporal_profile().trained_frame_range
        self.assertIs(unqualified.status, CapabilityStatus.UNQUALIFIED)
        self.assertIsNone(unqualified.value)
        self.assertEqual(unqualified.reason_code, "no_accepted_row")
        wire = build_temporal_profile().to_wire()["trained_frame_range"]
        self.assertEqual(
            wire,
            {"status": "unqualified", "reason_code": "no_accepted_row", "value": None},
        )

    def test_the_report_cannot_be_constructed_in_a_state_that_misleads(self) -> None:
        with self.assertRaises(TemporalProfileError):
            TrainedRangeReport(CapabilityStatus.SUPPORTED, "x")
        with self.assertRaises(TemporalProfileError):
            TrainedRangeReport(CapabilityStatus.UNQUALIFIED, "x", FrameRange(124, 362, True))

    def test_no_consumer_can_read_one_range_as_the_other(self) -> None:
        """The types differ, so reaching the trained bound means passing through its status."""

        profile = build_temporal_profile()
        self.assertFalse(hasattr(profile.trained_frame_range, "minimum"))
        self.assertFalse(hasattr(profile.accepted_frame_range, "status"))


class CropConservationTests(unittest.TestCase):
    """AC-M20-01-10. The constraint, and only the constraint."""

    def test_the_extent_grid_is_derived_and_whole_on_both_latent_grids(self) -> None:
        self.assertEqual(CONTEXT_EXTENT_GRID_FRAMES, LATTICE_STEP * EXACT_AUDIO_PERIOD_FRAMES)
        self.assertEqual(CONTEXT_EXTENT_GRID_FRAMES, 51)
        self.assertEqual(MINIMUM_CONTEXT_EXTENT_FRAMES, CONTEXT_EXTENT_GRID_FRAMES)
        self.assertEqual(CONTEXT_EXTENT_GRID_FRAMES % LATTICE_STEP, 0)
        self.assertEqual(CONTEXT_EXTENT_GRID_FRAMES % EXACT_AUDIO_PERIOD_FRAMES, 0)
        self.assertTrue(context_extent_is_on_grid(0))
        self.assertTrue(context_extent_is_on_grid(102))
        self.assertFalse(context_extent_is_on_grid(50))
        self.assertFalse(context_extent_is_on_grid(17))

    def test_context_on_the_grid_keeps_the_produced_run_producible(self) -> None:
        """Why the grid is this and not something smaller: the arithmetic composes."""

        for target in (39, 124, 141):
            for extent in (0, 51, 102):
                with self.subTest(target=target, extent=extent):
                    plan = CropPlan(target, extent, extent, target + 2 * extent, extent, extent)
                    verdict = check_crop_conservation(plan)
                    self.assertTrue(verdict.conserved, verdict.reason_code)
                    self.assertEqual(verdict.delivered_target_frames, target)

    def test_an_under_cropped_result_double_counts_a_boundary(self) -> None:
        for target in (39, 124):
            with self.subTest(target=target):
                plan = CropPlan(target, 51, 51, target + 102, 51, 34)
                verdict = check_crop_conservation(plan)
                self.assertFalse(verdict.conserved)
                self.assertEqual(verdict.reason_code, "under_cropped_boundary_double_counted")
                self.assertGreater(verdict.delivered_target_frames, target)

    def test_an_over_cropped_result_silently_shortens_the_target(self) -> None:
        for target in (39, 124):
            with self.subTest(target=target):
                plan = CropPlan(target, 51, 51, target + 102, 51, 68)
                verdict = check_crop_conservation(plan)
                self.assertFalse(verdict.conserved)
                self.assertEqual(verdict.reason_code, "over_cropped_target_shortened")
                self.assertLess(verdict.delivered_target_frames, target)

    def test_an_off_grid_context_extent_is_refused(self) -> None:
        plan = CropPlan(124, 50, 51, 225, 50, 51)
        self.assertEqual(check_crop_conservation(plan).reason_code, "context_extent_off_grid")

    def test_a_plan_whose_produced_run_does_not_cover_it_is_refused(self) -> None:
        plan = CropPlan(124, 51, 51, 124, 51, 51)
        self.assertEqual(
            check_crop_conservation(plan).reason_code, "produced_run_does_not_cover_plan"
        )

    def test_a_non_producible_target_is_refused_before_the_conservation_question(self) -> None:
        plan = CropPlan(100, 51, 51, 202, 51, 51)
        self.assertEqual(
            check_crop_conservation(plan).reason_code, "target_interval_not_producible"
        )

    def test_this_item_delivers_no_per_job_lookahead_and_no_crop_execution(self) -> None:
        """AC-M20-01-10's exclusion, asserted rather than promised.

        The surface is checked by shape rather than by a list of names nobody proposed: nothing
        exported may read as performing work, and the crop surface must be exactly the constraint
        -- a plan value object, a verdict and the predicate between them.
        """

        import comfyui_h3_context.core.temporal_profile as module

        exported = set(module.__all__)
        performing = re.compile(r"execute|perform|apply|run_|render|emit|write", re.IGNORECASE)
        self.assertEqual([name for name in exported if performing.search(name)], [])
        self.assertEqual(
            {name for name in exported if "crop" in name.lower()},
            {"CropPlan", "CropVerdict", "check_crop_conservation"},
        )
        self.assertEqual([name for name in exported if "lookahead" in name.lower()], [])


class SingleAuthorityTests(unittest.TestCase):
    """AC-M20-01-04. A second copy of any of this is the defect, not extra coverage."""

    AUDIO_RATE = re.compile(r"^\s*AUDIO_LATENT_FPS\s*=", re.MULTILINE)
    EXTENT_GRID = re.compile(
        r"^\s*(CONTEXT_EXTENT_GRID_FRAMES|MINIMUM_CONTEXT_EXTENT_FRAMES)\s*=", re.MULTILINE
    )
    # A heuristic, and named as one: it catches a consumer that subtracts crop from produced output
    # in this vocabulary, which is how a reimplementation of this rule would actually be spelled. It
    # is not a proof that no consumer has one, which no source scan can be.
    CONSERVATION = re.compile(r"produced\w*\s*-\s*\w*crop|crop\w*.*==.*produced", re.IGNORECASE)

    def _sources(self) -> list[Path]:
        return [
            path
            for path in (ROOT / "comfyui_h3_context").rglob("*.py")
            if "__pycache__" not in path.parts and path.name != "temporal_profile.py"
        ]

    def test_only_the_profile_declares_the_audio_grid(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in self._sources()
            if self.AUDIO_RATE.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_only_the_profile_declares_the_context_extent_grid(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in self._sources()
            if self.EXTENT_GRID.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_no_consumer_reimplements_the_conservation_rule(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in self._sources()
            if self.CONSERVATION.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_the_profile_holds_no_second_copy_of_the_lattice(self) -> None:
        """The other direction: this module must not restate what M17-25 owns."""

        body = "\n".join(
            line
            for line in (ROOT / "comfyui_h3_context/core/temporal_profile.py")
            .read_text(encoding="utf-8")
            .splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertIsNone(re.search(r"%\s*17|17\s*\*|/\s*17\b", body))
        self.assertIsNone(
            re.search(
                r"^\s*(FPS|MIN_FRAME_COUNT|MAX_FRAME_COUNT|LATTICE_STEP)\s*=",
                body,
                re.MULTILINE,
            )
        )

    def test_the_pure_core_imports_no_model_media_or_host_dependency(self) -> None:
        result = run_isolated(
            import_blocker_script(
                """
                from comfyui_h3_context.core.temporal_profile import build_temporal_profile
                profile = build_temporal_profile()
                print(len(profile.adapter_facts), profile.trained_frame_range.status.value)
                """
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unqualified", result.stdout)


class ProjectionTests(unittest.TestCase):
    """AC-M20-01-01 and section 3. One authority, one content-free projection."""

    def _schema(self) -> dict[str, object]:
        path = ROOT / "governance/contracts/temporal_profile_v1.schema.json"
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def test_the_projection_validates_in_both_qualification_states(self) -> None:
        validator = Draft202012Validator(self._schema())
        for accepted in (None, accepted_matrix()):
            with self.subTest(accepted=accepted is not None):
                errors = list(validator.iter_errors(build_temporal_profile(accepted).to_wire()))
                self.assertEqual([error.message for error in errors], [])

    def test_the_projection_declares_every_temporal_limit_in_one_place(self) -> None:
        wire = build_temporal_profile().to_wire()
        self.assertEqual(wire["schema"], TEMPORAL_PROFILE_SCHEMA)
        for key in (
            "video_fps",
            "audio_latent_fps",
            "audio_latent_ratio",
            "exact_audio_period_frames",
            "lattice_offset",
            "lattice_step",
            "context_extent_grid_frames",
            "minimum_context_extent_frames",
            "accepted_frame_range",
            "trained_frame_range",
        ):
            self.assertIn(key, wire)
        self.assertEqual(wire["audio_latent_ratio"], "5/3")

    def test_the_projection_carries_no_path_locator_or_credential(self) -> None:
        rendered = json.dumps(build_temporal_profile(accepted_matrix()).to_wire())
        for pattern in (r"[A-Za-z]:[\\/]", r"file://", r"https?://", r"(?i)secret|token|password"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, rendered))

    def test_unqualified_members_are_visible_as_such_in_the_limitations(self) -> None:
        limitations = build_temporal_profile().limitations
        self.assertTrue(any("trained_frame_range is unqualified" in item for item in limitations))
        qualified = build_temporal_profile(accepted_matrix()).limitations
        self.assertFalse(any("trained_frame_range is" in item for item in qualified))

    def test_the_profile_makes_no_official_equivalence_claim(self) -> None:
        limitations = build_temporal_profile(accepted_matrix()).limitations
        self.assertTrue(any("no claim of equivalence" in item for item in limitations))


class IdentityTests(unittest.TestCase):
    """The M18-02 binding: two identities, two questions, neither substitutable."""

    def test_the_profile_identity_is_semantic_and_deterministic(self) -> None:
        first = build_temporal_profile().identity()
        second = build_temporal_profile().identity()
        self.assertIs(first.domain, IdentityDomain.SEMANTIC)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.label, "semantic/v1")
        self.assertEqual(require_semantic_identity(first), first.digest)

    def test_joining_accepted_evidence_changes_the_semantic_identity(self) -> None:
        self.assertNotEqual(
            build_temporal_profile().identity().digest,
            build_temporal_profile(accepted_matrix()).identity().digest,
        )

    def test_the_contract_identity_is_a_different_domain_and_is_refused_as_semantic(self) -> None:
        contract = build_temporal_profile().contract_identity()
        self.assertIs(contract.domain, IdentityDomain.WIRE_CONTRACT)
        self.assertNotEqual(contract.digest, build_temporal_profile().identity().digest)
        with self.assertRaises(FingerprintDomainError):
            require_semantic_identity(contract)

    def test_the_contract_identity_does_not_move_when_the_evidence_does(self) -> None:
        """Structure is not instance: joining a row changes what the profile says, not its shape."""

        self.assertEqual(
            build_temporal_profile().contract_identity().digest,
            build_temporal_profile(accepted_matrix()).contract_identity().digest,
        )


if __name__ == "__main__":
    unittest.main()
