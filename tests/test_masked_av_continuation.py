"""M20-06 focused tests for dual-domain mask planning and continuation admission.

The authority is constructible only from a matrix whose mask row is SUPPORTED -- the shipped
M19-08 state refuses -- and admission is full binding across the real objects: the resume
decision, the receipt it admitted, the plan's descriptor, and the one descriptor authority
everything must agree on.  Mask plans live on the exact latent grids; a descriptor whose
extents do not match the M20-01 arithmetic cannot be planned against, only refused.
"""

from __future__ import annotations

import unittest

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.core.joint_av_latent import (
    JointAVLatentAuthority,
    JointAVLatentReceipt,
    begin_joint_av_latent_receipt,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
    complete_joint_av_latent_receipt,
)
from comfyui_h3_context.core.latent_checkpoint_resume import (
    LatentCheckpointBoundary,
    LatentResumeDecision,
    LatentResumeRequest,
    admit_latent_checkpoint_resume,
    build_latent_resume_authority,
)
from comfyui_h3_context.core.masked_av_continuation import (
    DomainMaskExtent,
    MaskedAVAuthority,
    MaskedAVContinuationError,
    MaskedContinuationDecision,
    admit_masked_av_continuation,
    build_masked_av_authority,
    plan_nested_av_mask,
)
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    CropPlan,
    QualifiedRow,
    build_temporal_profile,
)

_FP_A = "sha256:" + "1a" * 32
_FP_B = "sha256:" + "2b" * 32
_FP_OTHER = "sha256:" + "9f" * 32

_REFRESH_REASON = "repo_owned_nested_mask_constructor_executes"


def _accepted(mask: CapabilityStatus = CapabilityStatus.SUPPORTED) -> AcceptedQualification:
    """The M19-08 projection as refreshed by this item's own qualification run."""

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
                row="completed_boundary_resume",
                status=CapabilityStatus.SUPPORTED,
                reason_code="structural_resume_executes_no_bitexact_oracle",
                consumer="M20-05",
            ),
            QualifiedRow(
                row="dual_domain_av_mask",
                status=mask,
                reason_code=(
                    _REFRESH_REASON
                    if mask is CapabilityStatus.SUPPORTED
                    else "no_native_nested_mask_constructor"
                ),
                consumer="M20-06",
            ),
        ),
    )


_PROFILE = build_temporal_profile(_accepted())
_MASK_AUTHORITY = build_masked_av_authority(_PROFILE)
_DESCRIPTOR_AUTHORITY = build_joint_av_latent_authority(_PROFILE)
_RESUME_AUTHORITY = build_latent_resume_authority(_PROFILE)

_BOUNDARY = LatentCheckpointBoundary(total_steps=4, completed_steps=2, seed=1, requested_frames=22)

#: The M20-05 live-measured grids for 22 frames: video T=7, audio L=37.
_VIDEO_SHAPE = (1, 24, 7, 16, 24)
_AUDIO_SHAPE = (1, 32, 2, 37)


def _descriptor(video_dtype: str = "float32"):  # type: ignore[no-untyped-def]
    return build_joint_av_latent_descriptor(
        authority=_DESCRIPTOR_AUTHORITY,
        video_shape=_VIDEO_SHAPE,
        video_dtype=video_dtype,
        audio_shape=_AUDIO_SHAPE,
        audio_dtype="float32",
        requested_frames=22,
        completed_frames=22,
    )


def _receipt(artifact_id: str = "masked-cont-001") -> JointAVLatentReceipt:
    partial = begin_joint_av_latent_receipt(
        descriptor=_descriptor(),
        authority=_DESCRIPTOR_AUTHORITY,
        artifact_id=artifact_id,
        transaction_fingerprint=_FP_B,
        execution_fingerprint=_BOUNDARY.fingerprint,
        model_fingerprint=_FP_A,
        runtime_fingerprint=_FP_A,
        settings_fingerprint=_FP_A,
        source_id="segment.masked",
        predecessor_artifact_fingerprint=None,
        created_at_ms=1_000,
        expires_at_ms=61_000,
    )
    return complete_joint_av_latent_receipt(
        partial,
        output_fingerprint=_FP_OTHER,
        byte_length=partial.descriptor.payload_byte_length,
    )


def _resume_decision(
    receipt: JointAVLatentReceipt, *, model_fingerprint: str = _FP_A
) -> LatentResumeDecision:
    request = LatentResumeRequest(
        descriptor_authority_fingerprint=_DESCRIPTOR_AUTHORITY.fingerprint,
        boundary=_BOUNDARY,
        model_fingerprint=model_fingerprint,
        runtime_fingerprint=_FP_A,
        settings_fingerprint=_FP_A,
        source_id="segment.masked",
        predecessor_artifact_fingerprint=None,
        now_ms=2_000,
    )
    return admit_latent_checkpoint_resume(
        authority=_RESUME_AUTHORITY,
        descriptor_authority=_DESCRIPTOR_AUTHORITY,
        request=request,
        receipt=receipt,
    )


def _plan(video_generate_from: int = 4, audio_generate_from: int = 21):  # type: ignore[no-untyped-def]
    return plan_nested_av_mask(
        descriptor=_descriptor(),
        video_generate_from=video_generate_from,
        audio_generate_from=audio_generate_from,
    )


_CONSERVED_CROP = CropPlan(
    requested_target_frames=22,
    leading_context_frames=0,
    trailing_context_frames=0,
    produced_frames=22,
    leading_crop_frames=0,
    trailing_crop_frames=0,
)


class AuthorityTests(unittest.TestCase):
    def test_refreshed_row_freezes_and_unqualified_states_refuse(self) -> None:
        self.assertEqual(_MASK_AUTHORITY.subject_identity, NATIVE_NODE_SHA256)
        self.assertEqual(_MASK_AUTHORITY.reason_code, _REFRESH_REASON)
        with self.assertRaisesRegex(MaskedAVContinuationError, "mask_row_not_supported"):
            build_masked_av_authority(
                build_temporal_profile(_accepted(CapabilityStatus.UNSUPPORTED))
            )
        with self.assertRaisesRegex(MaskedAVContinuationError, "mask_row_not_supported"):
            build_masked_av_authority(build_temporal_profile())
        with self.assertRaisesRegex(
            MaskedAVContinuationError, "authority_requires_temporal_profile"
        ):
            build_masked_av_authority(object())  # type: ignore[arg-type]


class MaskPlanTests(unittest.TestCase):
    def test_plan_lives_on_the_exact_grids_and_is_stable(self) -> None:
        plan = _plan()
        self.assertEqual(plan.video.length, 7)
        self.assertEqual(plan.audio.length, 37)
        self.assertEqual(plan.video.preserved_steps, 4)
        self.assertEqual(plan.video.generated_steps, 3)
        self.assertEqual(plan.fingerprint, _plan().fingerprint)
        self.assertNotEqual(plan.fingerprint, _plan(video_generate_from=5).fingerprint)

    def test_probe_extremes_are_expressible(self) -> None:
        protect_all = _plan(video_generate_from=7, audio_generate_from=37)
        self.assertEqual(protect_all.video.generated_steps, 0)
        self.assertEqual(protect_all.audio.generated_steps, 0)
        open_all = _plan(video_generate_from=0, audio_generate_from=0)
        self.assertEqual(open_all.video.preserved_steps, 0)
        self.assertEqual(open_all.audio.preserved_steps, 0)

    def test_grid_drift_and_bad_extents_refuse(self) -> None:
        wrong_video = build_joint_av_latent_descriptor(
            authority=_DESCRIPTOR_AUTHORITY,
            video_shape=(1, 24, 8, 16, 24),
            video_dtype="float32",
            audio_shape=_AUDIO_SHAPE,
            audio_dtype="float32",
            requested_frames=22,
            completed_frames=22,
        )
        with self.assertRaisesRegex(MaskedAVContinuationError, "grid_mismatch:video"):
            plan_nested_av_mask(
                descriptor=wrong_video, video_generate_from=0, audio_generate_from=0
            )
        wrong_audio = build_joint_av_latent_descriptor(
            authority=_DESCRIPTOR_AUTHORITY,
            video_shape=_VIDEO_SHAPE,
            video_dtype="float32",
            audio_shape=(1, 32, 2, 36),
            audio_dtype="float32",
            requested_frames=22,
            completed_frames=22,
        )
        with self.assertRaisesRegex(MaskedAVContinuationError, "grid_mismatch:audio"):
            plan_nested_av_mask(
                descriptor=wrong_audio, video_generate_from=0, audio_generate_from=0
            )
        with self.assertRaisesRegex(MaskedAVContinuationError, "bounded_integer:generate_from"):
            _plan(video_generate_from=8)
        with self.assertRaisesRegex(MaskedAVContinuationError, "descriptor_type"):
            plan_nested_av_mask(
                descriptor=object(),  # type: ignore[arg-type]
                video_generate_from=0,
                audio_generate_from=0,
            )

    def test_domain_extent_bounds(self) -> None:
        with self.assertRaisesRegex(MaskedAVContinuationError, "bounded_integer:length"):
            DomainMaskExtent(length=0, generate_from=0)


class AdmissionTests(unittest.TestCase):
    def test_full_binding_is_admitted_with_provenance(self) -> None:
        receipt = _receipt()
        decision = admit_masked_av_continuation(
            authority=_MASK_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=_plan(),
            resume_decision=_resume_decision(receipt),
            receipt=receipt,
            crop_plan=_CONSERVED_CROP,
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.crop_verdict_reason, "conserved")
        self.assertEqual(decision.authority_fingerprint, _MASK_AUTHORITY.fingerprint)
        self.assertEqual(
            decision.descriptor_authority_fingerprint, _DESCRIPTOR_AUTHORITY.fingerprint
        )
        wire = decision.to_wire()
        self.assertEqual(wire["mask_plan_fingerprint"], _plan().fingerprint)
        self.assertTrue(decision.fingerprint.startswith("sha256:"))

    def test_every_binding_divergence_is_classified(self) -> None:
        receipt = _receipt()
        admitted_resume = _resume_decision(receipt)

        refused_resume = _resume_decision(receipt, model_fingerprint=_FP_OTHER)
        decision = admit_masked_av_continuation(
            authority=_MASK_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=_plan(),
            resume_decision=refused_resume,
            receipt=receipt,
            crop_plan=_CONSERVED_CROP,
        )
        self.assertIn("resume_not_admitted", decision.reason_codes)

        other_receipt = _receipt(artifact_id="masked-cont-002")
        decision = admit_masked_av_continuation(
            authority=_MASK_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=_plan(),
            resume_decision=admitted_resume,
            receipt=other_receipt,
            crop_plan=_CONSERVED_CROP,
        )
        self.assertIn("checkpoint_binding_mismatch", decision.reason_codes)

        foreign_plan = plan_nested_av_mask(
            descriptor=_descriptor(video_dtype="float16"),
            video_generate_from=4,
            audio_generate_from=21,
        )
        decision = admit_masked_av_continuation(
            authority=_MASK_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=foreign_plan,
            resume_decision=admitted_resume,
            receipt=receipt,
            crop_plan=_CONSERVED_CROP,
        )
        self.assertIn("descriptor_binding_mismatch", decision.reason_codes)

        foreign_subject = MaskedAVAuthority(
            subject_identity=_FP_OTHER,
            reason_code=_MASK_AUTHORITY.reason_code,
            profile_fingerprint=_MASK_AUTHORITY.profile_fingerprint,
        )
        decision = admit_masked_av_continuation(
            authority=foreign_subject,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=_plan(),
            resume_decision=admitted_resume,
            receipt=receipt,
            crop_plan=_CONSERVED_CROP,
        )
        self.assertIn("authority_subject_mismatch", decision.reason_codes)

        foreign_profile = MaskedAVAuthority(
            subject_identity=_MASK_AUTHORITY.subject_identity,
            reason_code=_MASK_AUTHORITY.reason_code,
            profile_fingerprint=_FP_OTHER,
        )
        decision = admit_masked_av_continuation(
            authority=foreign_profile,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=_plan(),
            resume_decision=admitted_resume,
            receipt=receipt,
            crop_plan=_CONSERVED_CROP,
        )
        self.assertIn("authority_profile_mismatch", decision.reason_codes)

        drifted_descriptor_authority = JointAVLatentAuthority(
            subject_identity=_DESCRIPTOR_AUTHORITY.subject_identity,
            reason_code="a_different_reason",
            profile_fingerprint=_DESCRIPTOR_AUTHORITY.profile_fingerprint,
        )
        decision = admit_masked_av_continuation(
            authority=_MASK_AUTHORITY,
            descriptor_authority=drifted_descriptor_authority,
            mask_plan=_plan(),
            resume_decision=admitted_resume,
            receipt=receipt,
            crop_plan=_CONSERVED_CROP,
        )
        self.assertIn("authority_binding_mismatch", decision.reason_codes)

        bad_crop = CropPlan(
            requested_target_frames=10,
            leading_context_frames=0,
            trailing_context_frames=0,
            produced_frames=22,
            leading_crop_frames=0,
            trailing_crop_frames=0,
        )
        decision = admit_masked_av_continuation(
            authority=_MASK_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=_plan(),
            resume_decision=admitted_resume,
            receipt=receipt,
            crop_plan=bad_crop,
        )
        self.assertIn("crop_not_conserved:target_interval_not_producible", decision.reason_codes)

        short_crop = CropPlan(
            requested_target_frames=5,
            leading_context_frames=0,
            trailing_context_frames=0,
            produced_frames=5,
            leading_crop_frames=0,
            trailing_crop_frames=0,
        )
        decision = admit_masked_av_continuation(
            authority=_MASK_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=_plan(),
            resume_decision=admitted_resume,
            receipt=receipt,
            crop_plan=short_crop,
        )
        self.assertIn("extent_accounting_mismatch", decision.reason_codes)

    def test_divergences_accumulate_and_types_raise(self) -> None:
        receipt = _receipt()
        refused_resume = _resume_decision(receipt, model_fingerprint=_FP_OTHER)
        other_receipt = _receipt(artifact_id="masked-cont-003")
        decision = admit_masked_av_continuation(
            authority=_MASK_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            mask_plan=_plan(),
            resume_decision=refused_resume,
            receipt=other_receipt,
            crop_plan=_CONSERVED_CROP,
        )
        self.assertFalse(decision.admitted)
        self.assertIn("resume_not_admitted", decision.reason_codes)
        self.assertIn("checkpoint_binding_mismatch", decision.reason_codes)

        with self.assertRaisesRegex(MaskedAVContinuationError, "authority_type"):
            admit_masked_av_continuation(
                authority=object(),  # type: ignore[arg-type]
                descriptor_authority=_DESCRIPTOR_AUTHORITY,
                mask_plan=_plan(),
                resume_decision=_resume_decision(receipt),
                receipt=receipt,
                crop_plan=_CONSERVED_CROP,
            )
        with self.assertRaisesRegex(MaskedAVContinuationError, "descriptor_authority_type"):
            admit_masked_av_continuation(
                authority=_MASK_AUTHORITY,
                descriptor_authority=object(),  # type: ignore[arg-type]
                mask_plan=_plan(),
                resume_decision=_resume_decision(receipt),
                receipt=receipt,
                crop_plan=_CONSERVED_CROP,
            )
        with self.assertRaisesRegex(MaskedAVContinuationError, "crop_plan_type"):
            admit_masked_av_continuation(
                authority=_MASK_AUTHORITY,
                descriptor_authority=_DESCRIPTOR_AUTHORITY,
                mask_plan=_plan(),
                resume_decision=_resume_decision(receipt),
                receipt=receipt,
                crop_plan=object(),  # type: ignore[arg-type]
            )

    def test_decision_invariant_refuses_inconsistent_construction(self) -> None:
        provenance = {
            "mask_plan_fingerprint": _FP_A,
            "checkpoint_receipt_fingerprint": _FP_A,
            "authority_fingerprint": _FP_A,
            "descriptor_authority_fingerprint": _FP_A,
            "resume_decision_fingerprint": _FP_A,
            "crop_verdict_reason": "conserved",
        }
        with self.assertRaisesRegex(MaskedAVContinuationError, "admitted_with_reasons"):
            MaskedContinuationDecision(
                admitted=True, reason_codes=("resume_not_admitted",), **provenance
            )
        with self.assertRaisesRegex(MaskedAVContinuationError, "refused_without_reasons"):
            MaskedContinuationDecision(admitted=False, reason_codes=(), **provenance)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
