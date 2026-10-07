"""M20-07 focused tests for two-ended bridge planning, admission and accounting.

The authority is constructible only from a matrix whose bridge row is SUPPORTED.  Plans are
grid-exact: contexts on the M20-01 51-frame grid, gaps producible and audio-exact, the left
context boundary audio-exact inside its segment, and every latent slice extent derived and
cross-checked against the M20-01 arithmetic.  Admission is full binding across the real
objects -- both boundary receipts, the plan, the derived mask, the master-audio declaration
and the three authorities -- with one classified reason per failed domain.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.core.joint_av_latent import (
    JointAVLatentAuthority,
    JointAVLatentDescriptor,
    JointAVLatentReceipt,
    begin_joint_av_latent_receipt,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
    complete_joint_av_latent_receipt,
)
from comfyui_h3_context.core.latent_checkpoint_resume import ResumeCompositionNodeRefs
from comfyui_h3_context.core.masked_av_continuation import build_masked_av_authority
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)
from comfyui_h3_context.core.two_ended_av_bridge import (
    AUDIO_LATENT_STEPS_PER_CONTEXT_UNIT,
    VIDEO_LATENT_STEPS_PER_CONTEXT_UNIT,
    IntervalMaskExtent,
    MasterAudioDeclaration,
    TwoEndedBridgeAuthority,
    TwoEndedBridgeDecision,
    TwoEndedBridgeError,
    TwoEndedBridgePlan,
    admit_two_ended_bridge,
    build_bridge_composition,
    build_two_ended_bridge_authority,
    build_two_ended_bridge_receipt,
    plan_bridge_mask,
    plan_two_ended_bridge,
)

_FP_A = "sha256:" + "1a" * 32
_FP_B = "sha256:" + "2b" * 32
_FP_OTHER = "sha256:" + "9f" * 32

_BRIDGE_REASON = "pixel_domain_two_ended_composition_executes"


def _accepted(bridge: CapabilityStatus = CapabilityStatus.SUPPORTED) -> AcceptedQualification:
    """The refreshed M19-08 projection this item consumes."""

    return AcceptedQualification(
        subject_identity=NATIVE_NODE_SHA256,
        rows=(
            QualifiedRow(
                row="joint_av_latent_descriptor",
                status=CapabilityStatus.SUPPORTED,
                reason_code="descriptor_measured_live",
                consumer="M20-04",
            ),
            QualifiedRow(
                row="dual_domain_av_mask",
                status=CapabilityStatus.SUPPORTED,
                reason_code="repo_owned_nested_mask_constructor_executes",
                consumer="M20-06",
            ),
            QualifiedRow(
                row="two_ended_av_bridge",
                status=bridge,
                reason_code=(
                    _BRIDGE_REASON if bridge is CapabilityStatus.SUPPORTED else "not_requalified"
                ),
                consumer="M20-07",
            ),
        ),
    )


_PROFILE = build_temporal_profile(_accepted())
_BRIDGE_AUTHORITY = build_two_ended_bridge_authority(_PROFILE)
_DESCRIPTOR_AUTHORITY = build_joint_av_latent_authority(_PROFILE)
_MASK_AUTHORITY = build_masked_av_authority(_PROFILE)

#: 90-frame boundary segments at 384x256: video T=27, audio L=150 (audio-exact length).
_SEGMENT_FRAMES = 90
_VIDEO_SHAPE_90 = (1, 24, 27, 16, 24)
_AUDIO_SHAPE_90 = (1, 32, 2, 150)

#: The probe bridge: contexts 51/51, gap 39, produced 141 (video T=42, audio L=235).
_GAP = 39
_CONTEXT = 51
_PRODUCED = 141
_VIDEO_SHAPE_141 = (1, 24, 42, 16, 24)
_AUDIO_SHAPE_141 = (1, 32, 2, 235)

_MASTER = MasterAudioDeclaration(
    master_source_id="master.audio",
    coverage_start_frame=0,
    coverage_end_frame=240,
)


def _descriptor(
    frames: int = _SEGMENT_FRAMES,
    video_shape: tuple[int, ...] = _VIDEO_SHAPE_90,
    audio_shape: tuple[int, ...] = _AUDIO_SHAPE_90,
    completed: int | None = None,
) -> JointAVLatentDescriptor:
    return build_joint_av_latent_descriptor(
        authority=_DESCRIPTOR_AUTHORITY,
        video_shape=video_shape,
        video_dtype="float32",
        audio_shape=audio_shape,
        audio_dtype="float32",
        requested_frames=frames,
        completed_frames=frames if completed is None else completed,
    )


def _receipt(
    artifact_id: str,
    *,
    frames: int = _SEGMENT_FRAMES,
    video_shape: tuple[int, ...] = _VIDEO_SHAPE_90,
    audio_shape: tuple[int, ...] = _AUDIO_SHAPE_90,
    model_fingerprint: str = _FP_A,
    settings_fingerprint: str = _FP_A,
    source_id: str = "segment.bridge",
    predecessor: str | None = None,
    complete: bool = True,
) -> JointAVLatentReceipt:
    partial = begin_joint_av_latent_receipt(
        descriptor=_descriptor(frames, video_shape, audio_shape),
        authority=_DESCRIPTOR_AUTHORITY,
        artifact_id=artifact_id,
        transaction_fingerprint=_FP_B,
        execution_fingerprint=_FP_A,
        model_fingerprint=model_fingerprint,
        runtime_fingerprint=_FP_A,
        settings_fingerprint=settings_fingerprint,
        source_id=source_id,
        predecessor_artifact_fingerprint=predecessor,
        created_at_ms=1_000,
        expires_at_ms=61_000,
    )
    if not complete:
        return partial
    return complete_joint_av_latent_receipt(
        partial,
        output_fingerprint=_FP_OTHER,
        byte_length=partial.descriptor.payload_byte_length,
    )


_LEFT = _receipt("bridge-left-001")
_RIGHT = _receipt("bridge-right-001")


def _plan(**overrides: object) -> TwoEndedBridgePlan:
    values: dict[str, object] = {
        "left_descriptor": _LEFT.descriptor,
        "right_descriptor": _RIGHT.descriptor,
        "descriptor_authority": _DESCRIPTOR_AUTHORITY,
        "master": _MASTER,
        "target_gap_frames": _GAP,
        "left_context_frames": _CONTEXT,
        "right_context_frames": _CONTEXT,
        "left_end_frame": 90,
        "right_start_frame": 129,
        "left_lookahead_frames": 0,
        "right_lookahead_frames": 0,
    }
    values.update(overrides)
    return plan_two_ended_bridge(**values)  # type: ignore[arg-type]


def _admit(**overrides: object) -> TwoEndedBridgeDecision:
    plan = overrides.pop("plan", None) or _plan()
    values: dict[str, object] = {
        "authority": _BRIDGE_AUTHORITY,
        "descriptor_authority": _DESCRIPTOR_AUTHORITY,
        "mask_authority": _MASK_AUTHORITY,
        "plan": plan,
        "mask_plan": plan_bridge_mask(plan),  # type: ignore[arg-type]
        "left_receipt": _LEFT,
        "right_receipt": _RIGHT,
        "master": _MASTER,
        "left_master_source_id": "master.audio",
        "right_master_source_id": "master.audio",
        "now_ms": 2_000,
    }
    values.update(overrides)
    return admit_two_ended_bridge(**values)  # type: ignore[arg-type]


class DerivedConstantTests(unittest.TestCase):
    def test_context_unit_extents_follow_the_grid_arithmetic(self) -> None:
        self.assertEqual(VIDEO_LATENT_STEPS_PER_CONTEXT_UNIT, 15)
        self.assertEqual(AUDIO_LATENT_STEPS_PER_CONTEXT_UNIT, 85)


class AuthorityTests(unittest.TestCase):
    def test_supported_row_freezes_subject_and_reason(self) -> None:
        self.assertEqual(_BRIDGE_AUTHORITY.subject_identity, NATIVE_NODE_SHA256)
        self.assertEqual(_BRIDGE_AUTHORITY.reason_code, _BRIDGE_REASON)

    def test_every_non_supported_status_refuses(self) -> None:
        for status in (
            CapabilityStatus.UNSUPPORTED,
            CapabilityStatus.UNQUALIFIED,
            CapabilityStatus.RETIRED,
        ):
            profile = build_temporal_profile(_accepted(status))
            with self.assertRaisesRegex(TwoEndedBridgeError, "bridge_row_not_supported"):
                build_two_ended_bridge_authority(profile)

    def test_profile_without_accepted_rows_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "bridge_row_not_supported"):
            build_two_ended_bridge_authority(build_temporal_profile())


class MasterAudioDeclarationTests(unittest.TestCase):
    def test_empty_coverage_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "master_audio_coverage_empty"):
            MasterAudioDeclaration(
                master_source_id="master.audio",
                coverage_start_frame=10,
                coverage_end_frame=10,
            )

    def test_unbounded_identifier_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "bounded_identifier"):
            MasterAudioDeclaration(
                master_source_id="bad id with spaces",
                coverage_start_frame=0,
                coverage_end_frame=10,
            )


class IntervalMaskExtentTests(unittest.TestCase):
    def test_interval_partitions_the_grid(self) -> None:
        extent = IntervalMaskExtent(length=42, generate_from=15, generate_to=27)
        self.assertEqual(extent.leading_preserved_steps, 15)
        self.assertEqual(extent.generated_steps, 12)
        self.assertEqual(extent.trailing_preserved_steps, 15)

    def test_inverted_interval_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "bounded_integer:generate_to"):
            IntervalMaskExtent(length=42, generate_from=20, generate_to=15)


class PlanTests(unittest.TestCase):
    def test_probe_geometry_derives_the_declared_extents(self) -> None:
        plan = _plan()
        self.assertEqual(plan.produced_frames, _PRODUCED)
        self.assertEqual(plan.bridge_start_frame, 39)
        self.assertEqual(
            (plan.left_video_steps, plan.middle_video_steps, plan.right_video_steps),
            (15, 12, 15),
        )
        self.assertEqual(
            (plan.left_audio_steps, plan.middle_audio_steps, plan.right_audio_steps),
            (85, 65, 85),
        )
        self.assertEqual(plan.left_descriptor_fingerprint, _LEFT.descriptor.fingerprint)
        self.assertEqual(plan.master_fingerprint, _MASTER.fingerprint)

    def test_off_grid_context_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "context_extent_off_grid"):
            _plan(left_context_frames=50)

    def test_context_exceeding_its_segment_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "context_exceeds_segment"):
            _plan(left_context_frames=102)

    def test_gap_off_lattice_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "gap_not_producible"):
            _plan(target_gap_frames=40, right_start_frame=130)

    def test_gap_with_inexact_audio_join_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "gap_not_audio_exact"):
            _plan(target_gap_frames=22, right_start_frame=112)

    def test_left_context_boundary_must_be_audio_exact(self) -> None:
        short = _descriptor(56, (1, 24, 17, 16, 24), (1, 32, 2, 93))
        with self.assertRaisesRegex(TwoEndedBridgeError, "left_context_boundary_not_audio_exact"):
            _plan(left_descriptor=short, left_end_frame=56, right_start_frame=95)

    def test_placement_disagreeing_with_the_gap_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "gap_placement_mismatch"):
            _plan(right_start_frame=130)

    def test_bridge_start_before_the_timeline_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "bridge_start_negative"):
            _plan(left_end_frame=39, right_start_frame=78)

    def test_spatially_incompatible_boundaries_refuse(self) -> None:
        wide = _descriptor(90, (1, 24, 27, 16, 25), _AUDIO_SHAPE_90)
        with self.assertRaisesRegex(TwoEndedBridgeError, "boundary_shape_mismatch:video"):
            _plan(right_descriptor=wide)

    def test_partial_boundary_refuses(self) -> None:
        partial = _descriptor(90, completed=51)
        with self.assertRaisesRegex(TwoEndedBridgeError, "boundary_extent_partial:left"):
            _plan(left_descriptor=partial)

    def test_descriptor_off_its_own_grid_refuses(self) -> None:
        drifted = _descriptor(90, (1, 24, 26, 16, 24), _AUDIO_SHAPE_90)
        with self.assertRaisesRegex(TwoEndedBridgeError, "grid_mismatch:left_video"):
            _plan(left_descriptor=drifted)


class MaskPlanTests(unittest.TestCase):
    def test_mask_intervals_follow_the_plan(self) -> None:
        plan = _plan()
        mask = plan_bridge_mask(plan)
        self.assertEqual(
            mask.video.to_wire(), {"length": 42, "generate_from": 15, "generate_to": 27}
        )
        self.assertEqual(
            mask.audio.to_wire(), {"length": 235, "generate_from": 85, "generate_to": 150}
        )
        self.assertEqual(mask.bridge_plan_fingerprint, plan.fingerprint)

    def test_wrong_type_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "plan_type"):
            plan_bridge_mask(object())  # type: ignore[arg-type]


class AdmissionTests(unittest.TestCase):
    def test_full_binding_admits_exactly(self) -> None:
        decision = _admit()
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.crop_verdict_reason, "conserved")
        self.assertEqual(decision.left_receipt_fingerprint, _LEFT.fingerprint)
        self.assertEqual(decision.right_receipt_fingerprint, _RIGHT.fingerprint)

    def test_identical_boundaries_refuse(self) -> None:
        plan = _plan(right_descriptor=_LEFT.descriptor)
        decision = _admit(plan=plan, right_receipt=_LEFT)
        self.assertFalse(decision.admitted)
        self.assertIn("boundary_not_distinct", decision.reason_codes)

    def test_incomplete_boundary_refuses(self) -> None:
        partial = _receipt("bridge-left-partial", complete=False)
        plan = _plan(left_descriptor=partial.descriptor)
        decision = _admit(plan=plan, left_receipt=partial)
        self.assertFalse(decision.admitted)
        self.assertIn("left_receipt_not_complete", decision.reason_codes)

    def test_expired_boundary_refuses(self) -> None:
        decision = _admit(now_ms=61_000)
        self.assertFalse(decision.admitted)
        self.assertIn("left_receipt_expired", decision.reason_codes)
        self.assertIn("right_receipt_expired", decision.reason_codes)

    def test_profile_misaligned_boundaries_refuse(self) -> None:
        drifted = _receipt(
            "bridge-right-002",
            model_fingerprint=_FP_OTHER,
            settings_fingerprint=_FP_OTHER,
            source_id="segment.other",
        )
        plan = _plan(right_descriptor=drifted.descriptor)
        decision = _admit(plan=plan, right_receipt=drifted)
        self.assertFalse(decision.admitted)
        for reason in (
            "boundary_model_mismatch",
            "boundary_settings_mismatch",
            "boundary_source_mismatch",
        ):
            self.assertIn(reason, decision.reason_codes)

    def test_competing_master_declarations_refuse(self) -> None:
        decision = _admit(left_master_source_id="clip.audio")
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("master_audio_conflict:left",))

    def test_master_coverage_gap_refuses(self) -> None:
        narrow = MasterAudioDeclaration(
            master_source_id="master.audio",
            coverage_start_frame=0,
            coverage_end_frame=150,
        )
        plan = _plan(master=narrow)
        decision = _admit(plan=plan, master=narrow)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("master_audio_coverage_gap",))

    def test_lookahead_extends_required_coverage(self) -> None:
        exact = MasterAudioDeclaration(
            master_source_id="master.audio",
            coverage_start_frame=39,
            coverage_end_frame=180,
        )
        plan = _plan(master=exact)
        self.assertTrue(_admit(plan=plan, master=exact).admitted)
        lookahead_plan = _plan(master=exact, right_lookahead_frames=17)
        decision = _admit(plan=lookahead_plan, master=exact)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("master_audio_coverage_gap",))

    def test_foreign_master_declaration_refuses_binding(self) -> None:
        other = MasterAudioDeclaration(
            master_source_id="master.audio",
            coverage_start_frame=0,
            coverage_end_frame=241,
        )
        decision = _admit(master=other)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("master_binding_mismatch",))

    def test_mask_from_another_plan_refuses(self) -> None:
        other_plan = _plan(left_lookahead_frames=1)
        decision = _admit(mask_plan=plan_bridge_mask(other_plan))
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("mask_binding_mismatch",))

    def test_authority_subject_drift_refuses(self) -> None:
        drifted = TwoEndedBridgeAuthority(
            subject_identity=_FP_OTHER,
            reason_code=_BRIDGE_REASON,
            profile_fingerprint=_BRIDGE_AUTHORITY.profile_fingerprint,
        )
        decision = _admit(authority=drifted)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("authority_subject_mismatch",))

    def test_authority_profile_drift_refuses(self) -> None:
        drifted = TwoEndedBridgeAuthority(
            subject_identity=_BRIDGE_AUTHORITY.subject_identity,
            reason_code=_BRIDGE_REASON,
            profile_fingerprint=_FP_OTHER,
        )
        decision = _admit(authority=drifted)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("authority_profile_mismatch",))

    def test_foreign_left_receipt_refuses_descriptor_binding(self) -> None:
        # Same frame plan, different spatial extent: a structurally different checkpoint
        # standing in for the one the plan was built against.
        other = _receipt("bridge-left-alt", video_shape=(1, 24, 27, 16, 25))
        decision = _admit(left_receipt=other)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("descriptor_binding_mismatch:left",))

    def test_foreign_right_receipt_refuses_descriptor_binding(self) -> None:
        other = _receipt("bridge-right-alt", video_shape=(1, 24, 27, 16, 25))
        decision = _admit(right_receipt=other)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_codes, ("descriptor_binding_mismatch:right",))

    def test_drifted_descriptor_authority_accumulates_every_binding_refusal(self) -> None:
        drifted = JointAVLatentAuthority(
            subject_identity=NATIVE_NODE_SHA256,
            reason_code="descriptor_measured_live",
            profile_fingerprint=_FP_OTHER,
        )
        decision = _admit(descriptor_authority=drifted)
        self.assertFalse(decision.admitted)
        for reason in (
            "authority_profile_mismatch",
            "authority_binding_mismatch:left",
            "authority_binding_mismatch:right",
            "plan_authority_binding_mismatch",
        ):
            self.assertIn(reason, decision.reason_codes)
        self.assertNotIn("authority_subject_mismatch", decision.reason_codes)

    def test_unconserved_crop_accounting_refuses(self) -> None:
        # A tampered produced extent no longer covers contexts plus target; the M20-01
        # verdict's reason code must surface through the admission refusal.
        tampered = replace(_plan(), produced_frames=124)
        decision = _admit(plan=tampered)
        self.assertFalse(decision.admitted)
        self.assertIn("crop_not_conserved:produced_run_does_not_cover_plan", decision.reason_codes)

    def test_oversized_left_lookahead_clamps_to_the_timeline_origin(self) -> None:
        # bridge_start is 39; a 51-frame lookahead reaches before frame 0 and the coverage
        # requirement clamps to the origin. Deliberate, recorded behavior (distinct review
        # finding 3): coverage from frame 0 satisfies the clamped requirement.
        plan = _plan(left_lookahead_frames=51)
        decision = _admit(plan=plan)
        self.assertTrue(decision.admitted)

    def test_wrong_types_raise_instead_of_classifying(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "plan_type"):
            _admit(plan=object())


class DecisionInvariantTests(unittest.TestCase):
    def test_decision_invariant_refuses_inconsistent_construction(self) -> None:
        admitted = _admit()
        with self.assertRaisesRegex(TwoEndedBridgeError, "admitted_with_reasons"):
            replace(admitted, reason_codes=("anything",))
        with self.assertRaisesRegex(TwoEndedBridgeError, "refused_without_reasons"):
            replace(admitted, admitted=False)


class ReceiptTests(unittest.TestCase):
    def _output(
        self, *, predecessor: str | None = None, frames: int = _PRODUCED
    ) -> JointAVLatentReceipt:
        video = _VIDEO_SHAPE_141 if frames == _PRODUCED else _VIDEO_SHAPE_90
        audio = _AUDIO_SHAPE_141 if frames == _PRODUCED else _AUDIO_SHAPE_90
        return _receipt(
            "bridge-out-001",
            frames=frames,
            video_shape=video,
            audio_shape=audio,
            predecessor=_LEFT.fingerprint if predecessor is None else predecessor,
        )

    def test_admitted_decision_binds_the_completed_output(self) -> None:
        decision = _admit()
        receipt = build_two_ended_bridge_receipt(
            decision=decision, plan=_plan(), output_receipt=self._output()
        )
        self.assertEqual(receipt.decision_fingerprint, decision.fingerprint)
        self.assertEqual(receipt.leading_crop_frames, _CONTEXT)
        self.assertEqual(receipt.trailing_crop_frames, _CONTEXT)
        self.assertEqual(receipt.target_gap_frames, _GAP)

    def test_refused_decision_cannot_build_a_receipt(self) -> None:
        refused = _admit(left_master_source_id="clip.audio")
        with self.assertRaisesRegex(TwoEndedBridgeError, "decision_not_admitted"):
            build_two_ended_bridge_receipt(
                decision=refused, plan=_plan(), output_receipt=self._output()
            )

    def test_output_at_the_wrong_extent_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "output_extent_mismatch"):
            build_two_ended_bridge_receipt(
                decision=_admit(), plan=_plan(), output_receipt=self._output(frames=90)
            )

    def test_output_without_the_left_predecessor_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "output_predecessor_mismatch"):
            build_two_ended_bridge_receipt(
                decision=_admit(),
                plan=_plan(),
                output_receipt=self._output(predecessor=_FP_OTHER),
            )


class CompositionBuilderTests(unittest.TestCase):
    _REFS = ResumeCompositionNodeRefs(
        model_node="u",
        guider_node="guider",
        sampler_select_node="sampler",
        latent_source_node="bridge_ckpt",
    )

    def test_fragment_pins_the_qualified_fresh_run_topology(self) -> None:
        fragment = build_bridge_composition(
            seed=1, total_steps=4, refs=self._REFS, scheduler_name="simple"
        )
        self.assertEqual(set(fragment), {"bridge_noise", "bridge_sigmas", "bridge_sampler"})
        self.assertEqual(fragment["bridge_noise"]["class_type"], "RandomNoise")
        self.assertEqual(fragment["bridge_sigmas"]["class_type"], "BasicScheduler")
        self.assertEqual(fragment["bridge_sampler"]["class_type"], "SamplerCustomAdvanced")
        inputs = fragment["bridge_sampler"]["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(inputs["latent_image"], ["bridge_ckpt", 0])
        self.assertEqual(inputs["sigmas"], ["bridge_sigmas", 0])

    def test_unqualified_denoise_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeError, "unqualified_denoise"):
            build_bridge_composition(
                seed=1,
                total_steps=4,
                refs=self._REFS,
                scheduler_name="simple",
                denoise="0.5",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
