"""M20-05 focused tests for checkpoint resume admission and the qualified split composition.

Two properties are held apart everywhere.  Admission is identity equality over declarations
and fingerprints -- never over produced bytes, because the M19-08 row measured the host as
nondeterministic end to end -- and every divergence is a classified refusal, accumulated per
domain, with no partial credit.  The composition builder renders exactly the topology the row
qualified and refuses every variation (terminal boundary, foreign denoise) instead of
rendering something plausible.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.joint_av_latent import (
    JointAVLatentReceipt,
    begin_joint_av_latent_receipt,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
    complete_joint_av_latent_receipt,
)
from comfyui_h3_context.core.latent_checkpoint_resume import (
    QUALIFIED_DISABLE_NOISE_NODE,
    QUALIFIED_SAMPLER_NODE,
    QUALIFIED_SCHEDULER_NODE,
    QUALIFIED_SPLIT_NODE,
    LatentCheckpointBoundary,
    LatentResumeAuthority,
    LatentResumeError,
    LatentResumeRequest,
    ResumeCompositionNodeRefs,
    admit_latent_checkpoint_resume,
    build_latent_resume_authority,
    build_resume_composition,
    recompute_origin_for_refusal,
)
from comfyui_h3_context.core.recompute_closure import (
    RecomputeDisposition,
    RecomputePlan,
    extend_recompute_plan,
    plan_recompute,
)
from comfyui_h3_context.core.segment_workspace import (
    AcceptedIntentAuthority,
    SegmentContextManifest,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    create_workspace,
    derive_segment_manifests,
)
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)

_FP_MODEL = "sha256:" + "1a" * 32
_FP_RUNTIME = "sha256:" + "2b" * 32
_FP_SETTINGS = "sha256:" + "3c" * 32
_FP_TRANSACTION = "sha256:" + "4d" * 32
_FP_OTHER = "sha256:" + "9f" * 32


def _accepted(
    resume: CapabilityStatus = CapabilityStatus.SUPPORTED,
) -> AcceptedQualification:
    """The M19-08 refreshed projection: descriptor and resume rows measured live."""

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
                status=resume,
                reason_code=(
                    "structural_resume_executes_no_bitexact_oracle"
                    if resume is CapabilityStatus.SUPPORTED
                    else "requires_weight_backed_execution"
                ),
                consumer="M20-05",
            ),
        ),
    )


_PROFILE = build_temporal_profile(_accepted())
_RESUME_AUTHORITY = build_latent_resume_authority(_PROFILE)
_DESCRIPTOR_AUTHORITY = build_joint_av_latent_authority(_PROFILE)

_BOUNDARY = LatentCheckpointBoundary(
    total_steps=4,
    completed_steps=2,
    seed=1,
    requested_frames=22,
)


def _checkpoint_receipt(
    *,
    boundary: LatentCheckpointBoundary = _BOUNDARY,
    completed_frames: int = 22,
    model_fingerprint: str = _FP_MODEL,
    runtime_fingerprint: str = _FP_RUNTIME,
    settings_fingerprint: str = _FP_SETTINGS,
    source_id: str = "segment.resume",
    predecessor: str | None = None,
    complete: bool = True,
) -> JointAVLatentReceipt:
    descriptor = build_joint_av_latent_descriptor(
        authority=_DESCRIPTOR_AUTHORITY,
        video_shape=(1, 24, 3, 16, 24),
        video_dtype="float32",
        audio_shape=(1, 32, 2, 30),
        audio_dtype="float32",
        requested_frames=boundary.requested_frames,
        completed_frames=completed_frames,
    )
    receipt = begin_joint_av_latent_receipt(
        descriptor=descriptor,
        authority=_DESCRIPTOR_AUTHORITY,
        artifact_id="latent-resume-001",
        transaction_fingerprint=_FP_TRANSACTION,
        execution_fingerprint=boundary.fingerprint,
        model_fingerprint=model_fingerprint,
        runtime_fingerprint=runtime_fingerprint,
        settings_fingerprint=settings_fingerprint,
        source_id=source_id,
        predecessor_artifact_fingerprint=predecessor,
        created_at_ms=1_000,
        expires_at_ms=61_000,
    )
    if not complete:
        return receipt
    return complete_joint_av_latent_receipt(
        receipt,
        output_fingerprint=_FP_OTHER,
        byte_length=descriptor.payload_byte_length,
    )


def _request(**overrides: object) -> LatentResumeRequest:
    values: dict[str, object] = {
        "descriptor_authority_fingerprint": _DESCRIPTOR_AUTHORITY.fingerprint,
        "boundary": _BOUNDARY,
        "model_fingerprint": _FP_MODEL,
        "runtime_fingerprint": _FP_RUNTIME,
        "settings_fingerprint": _FP_SETTINGS,
        "source_id": "segment.resume",
        "predecessor_artifact_fingerprint": None,
        "now_ms": 2_000,
    }
    values.update(overrides)
    return LatentResumeRequest(**values)  # type: ignore[arg-type]


class AuthorityTests(unittest.TestCase):
    def test_supported_row_freezes_and_unqualified_states_refuse(self) -> None:
        self.assertEqual(_RESUME_AUTHORITY.subject_identity, NATIVE_NODE_SHA256)
        self.assertEqual(
            _RESUME_AUTHORITY.reason_code,
            "structural_resume_executes_no_bitexact_oracle",
        )
        with self.assertRaisesRegex(LatentResumeError, "resume_row_not_supported"):
            build_latent_resume_authority(build_temporal_profile())
        with self.assertRaisesRegex(LatentResumeError, "resume_row_not_supported"):
            build_latent_resume_authority(
                build_temporal_profile(_accepted(CapabilityStatus.UNSUPPORTED))
            )
        with self.assertRaisesRegex(LatentResumeError, "authority_requires_temporal_profile"):
            build_latent_resume_authority(object())  # type: ignore[arg-type]


class BoundaryTests(unittest.TestCase):
    def test_declaration_is_bounded_and_fingerprint_stable(self) -> None:
        self.assertEqual(_BOUNDARY.remaining_steps, 2)
        again = LatentCheckpointBoundary(
            total_steps=4, completed_steps=2, seed=1, requested_frames=22
        )
        self.assertEqual(_BOUNDARY.fingerprint, again.fingerprint)
        different = replace(again, completed_steps=3)
        self.assertNotEqual(_BOUNDARY.fingerprint, different.fingerprint)
        with self.assertRaisesRegex(LatentResumeError, "bounded_integer:completed_steps"):
            LatentCheckpointBoundary(total_steps=4, completed_steps=5, seed=1, requested_frames=22)
        with self.assertRaisesRegex(LatentResumeError, "bounded_integer:total_steps"):
            LatentCheckpointBoundary(total_steps=0, completed_steps=0, seed=1, requested_frames=22)


class AdmissionTests(unittest.TestCase):
    def test_exact_identity_is_admitted_with_skip_provenance(self) -> None:
        decision = admit_latent_checkpoint_resume(
            authority=_RESUME_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(),
            receipt=_checkpoint_receipt(),
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.skipped_steps, 2)
        self.assertEqual(decision.remaining_steps, 2)
        self.assertEqual(decision.boundary_fingerprint, _BOUNDARY.fingerprint)
        self.assertEqual(
            decision.checkpoint_transaction_fingerprint,
            _FP_TRANSACTION,
        )
        self.assertTrue(decision.fingerprint.startswith("sha256:"))

    def test_every_identity_domain_mismatch_is_classified(self) -> None:
        cases: dict[str, tuple[dict[str, object], dict[str, object]]] = {
            "receipt_not_complete": ({"complete": False}, {}),
            "receipt_expired": ({}, {"now_ms": 61_000}),
            "boundary_mismatch": ({"boundary": replace(_BOUNDARY, seed=2)}, {}),
            "latent_extent_partial": ({"completed_frames": 7}, {}),
            "model_mismatch": ({"model_fingerprint": _FP_OTHER}, {}),
            "runtime_mismatch": ({"runtime_fingerprint": _FP_OTHER}, {}),
            "settings_mismatch": ({"settings_fingerprint": _FP_OTHER}, {}),
            "source_mismatch": ({"source_id": "segment.other"}, {}),
            "predecessor_mismatch": ({"predecessor": _FP_OTHER}, {}),
        }
        for code, (receipt_overrides, request_overrides) in cases.items():
            decision = admit_latent_checkpoint_resume(
                authority=_RESUME_AUTHORITY,
                descriptor_authority=_DESCRIPTOR_AUTHORITY,
                request=_request(**request_overrides),
                receipt=_checkpoint_receipt(**receipt_overrides),  # type: ignore[arg-type]
            )
            self.assertFalse(decision.admitted, msg=code)
            self.assertIn(code, decision.reason_codes, msg=code)
            self.assertEqual(decision.skipped_steps, 0, msg=code)

    def test_boundary_terminal_and_extent_mismatch_are_refused(self) -> None:
        terminal = LatentCheckpointBoundary(
            total_steps=4, completed_steps=4, seed=1, requested_frames=22
        )
        decision = admit_latent_checkpoint_resume(
            authority=_RESUME_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(boundary=terminal),
            receipt=_checkpoint_receipt(boundary=terminal),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("boundary_terminal", decision.reason_codes)

        drifted = replace(_BOUNDARY, requested_frames=39)
        decision = admit_latent_checkpoint_resume(
            authority=_RESUME_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(boundary=drifted),
            receipt=_checkpoint_receipt(),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("extent_mismatch", decision.reason_codes)
        self.assertIn("boundary_mismatch", decision.reason_codes)

    def test_descriptor_authority_drift_is_classified(self) -> None:
        decision = admit_latent_checkpoint_resume(
            authority=_RESUME_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(descriptor_authority_fingerprint=_FP_OTHER),
            receipt=_checkpoint_receipt(),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("descriptor_authority_mismatch", decision.reason_codes)

    def test_authority_pair_binding_is_classified(self) -> None:
        foreign_subject = LatentResumeAuthority(
            subject_identity=_FP_OTHER,
            reason_code=_RESUME_AUTHORITY.reason_code,
            profile_fingerprint=_RESUME_AUTHORITY.profile_fingerprint,
        )
        decision = admit_latent_checkpoint_resume(
            authority=foreign_subject,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(),
            receipt=_checkpoint_receipt(),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("authority_subject_mismatch", decision.reason_codes)

        foreign_profile = LatentResumeAuthority(
            subject_identity=_RESUME_AUTHORITY.subject_identity,
            reason_code=_RESUME_AUTHORITY.reason_code,
            profile_fingerprint=_FP_OTHER,
        )
        decision = admit_latent_checkpoint_resume(
            authority=foreign_profile,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(),
            receipt=_checkpoint_receipt(),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("authority_profile_mismatch", decision.reason_codes)

    def test_decision_records_both_authority_fingerprints(self) -> None:
        decision = admit_latent_checkpoint_resume(
            authority=_RESUME_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(),
            receipt=_checkpoint_receipt(),
        )
        self.assertEqual(decision.resume_authority_fingerprint, _RESUME_AUTHORITY.fingerprint)
        self.assertEqual(
            decision.descriptor_authority_fingerprint, _DESCRIPTOR_AUTHORITY.fingerprint
        )
        wire = decision.to_wire()
        self.assertEqual(wire["resume_authority_fingerprint"], _RESUME_AUTHORITY.fingerprint)
        self.assertEqual(
            wire["descriptor_authority_fingerprint"], _DESCRIPTOR_AUTHORITY.fingerprint
        )

    def test_multiple_divergences_accumulate(self) -> None:
        decision = admit_latent_checkpoint_resume(
            authority=_RESUME_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(now_ms=61_000, model_fingerprint=_FP_OTHER),
            receipt=_checkpoint_receipt(source_id="segment.other"),
        )
        self.assertFalse(decision.admitted)
        for code in ("receipt_expired", "model_mismatch", "source_mismatch"):
            self.assertIn(code, decision.reason_codes)

    def test_wrong_object_types_raise_instead_of_classifying(self) -> None:
        with self.assertRaisesRegex(LatentResumeError, "receipt_type"):
            admit_latent_checkpoint_resume(
                authority=_RESUME_AUTHORITY,
                descriptor_authority=_DESCRIPTOR_AUTHORITY,
                request=_request(),
                receipt=object(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(LatentResumeError, "authority_type"):
            admit_latent_checkpoint_resume(
                authority=object(),  # type: ignore[arg-type]
                descriptor_authority=_DESCRIPTOR_AUTHORITY,
                request=_request(),
                receipt=_checkpoint_receipt(),
            )
        with self.assertRaisesRegex(LatentResumeError, "descriptor_authority_type"):
            admit_latent_checkpoint_resume(
                authority=_RESUME_AUTHORITY,
                descriptor_authority=object(),  # type: ignore[arg-type]
                request=_request(),
                receipt=_checkpoint_receipt(),
            )


class RecomputeJoinTests(unittest.TestCase):
    def _single_segment_plan(
        self,
    ) -> tuple[tuple[SegmentContextManifest, ...], RecomputePlan]:
        fp = "sha256:" + "aa" * 32
        segment = SegmentDeclaration(
            segment_id="segment.resume",
            task_mode=TaskMode.T2VA,
            source_id="source.1",
            reference_ids=(),
            duration=SegmentDuration.from_frame_count(124),
            relation=SegmentRelationKind.INDEPENDENT,
            predecessor_segment_id=None,
            accepted_intent_fingerprint=fp,
            semantic_receipt_fingerprint="sha256:" + "bb" * 32,
            profile_fingerprint="sha256:" + "cc" * 32,
            reference_registry_fingerprint="sha256:" + "dd" * 32,
            native_binding_fingerprint="sha256:" + "ee" * 32,
            producer_settings_fingerprint="sha256:" + "ff" * 32,
        )
        workspace = create_workspace(
            "workspace.resume",
            (segment,),
            accepted_intent_authorities=(AcceptedIntentAuthority("segment.resume", fp),),
        )
        manifests = derive_segment_manifests(workspace)
        return manifests, plan_recompute(manifests, manifests)

    def test_refusal_feeds_the_accepted_recompute_seam(self) -> None:
        manifests, base_plan = self._single_segment_plan()
        clean = base_plan.decisions[0]
        self.assertIs(clean.disposition, RecomputeDisposition.CLEAN)

        refused = admit_latent_checkpoint_resume(
            authority=_RESUME_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(model_fingerprint=_FP_OTHER),
            receipt=_checkpoint_receipt(),
        )
        forced_ids, forced_reasons = recompute_origin_for_refusal(
            refused, segment_id="segment.resume"
        )
        extended = extend_recompute_plan(
            manifests,
            base_plan,
            forced_dirty_segment_ids=forced_ids,
            forced_reason_codes=forced_reasons,
        )
        decision = extended.decisions[0]
        self.assertIs(decision.disposition, RecomputeDisposition.DIRTY_SELF)
        self.assertIn("model_mismatch", decision.reason_codes)

    def test_admitted_decision_contributes_no_origin(self) -> None:
        admitted = admit_latent_checkpoint_resume(
            authority=_RESUME_AUTHORITY,
            descriptor_authority=_DESCRIPTOR_AUTHORITY,
            request=_request(),
            receipt=_checkpoint_receipt(),
        )
        self.assertEqual(
            recompute_origin_for_refusal(admitted, segment_id="segment.resume"),
            ((), ()),
        )


class CompositionTests(unittest.TestCase):
    _REFS = ResumeCompositionNodeRefs(
        model_node="model",
        guider_node="guider",
        sampler_select_node="sampler_select",
        latent_source_node="checkpoint_latent",
    )

    def test_fragment_renders_exactly_the_qualified_topology(self) -> None:
        fragment = build_resume_composition(_BOUNDARY, self._REFS, scheduler_name="simple")
        self.assertEqual(
            set(fragment), {"resume_sigmas", "resume_split", "resume_noise", "resume_stage2"}
        )
        self.assertEqual(fragment["resume_sigmas"]["class_type"], QUALIFIED_SCHEDULER_NODE)
        self.assertEqual(fragment["resume_split"]["class_type"], QUALIFIED_SPLIT_NODE)
        self.assertEqual(fragment["resume_noise"]["class_type"], QUALIFIED_DISABLE_NOISE_NODE)
        self.assertEqual(fragment["resume_stage2"]["class_type"], QUALIFIED_SAMPLER_NODE)
        sigmas_inputs = fragment["resume_sigmas"]["inputs"]
        self.assertEqual(sigmas_inputs["steps"], 4)  # type: ignore[index]
        split_inputs = fragment["resume_split"]["inputs"]
        self.assertEqual(split_inputs["step"], 2)  # type: ignore[index]
        stage2 = fragment["resume_stage2"]["inputs"]
        self.assertEqual(stage2["sigmas"], ["resume_split", 1])  # type: ignore[index]
        self.assertEqual(stage2["noise"], ["resume_noise", 0])  # type: ignore[index]
        self.assertEqual(stage2["latent_image"], ["checkpoint_latent", 0])  # type: ignore[index]

    def test_terminal_boundary_and_foreign_denoise_refuse(self) -> None:
        terminal = LatentCheckpointBoundary(
            total_steps=4, completed_steps=4, seed=1, requested_frames=22
        )
        with self.assertRaisesRegex(LatentResumeError, "boundary_terminal"):
            build_resume_composition(terminal, self._REFS, scheduler_name="simple")
        with self.assertRaisesRegex(LatentResumeError, "unqualified_denoise"):
            build_resume_composition(_BOUNDARY, self._REFS, scheduler_name="simple", denoise="0.5")

    def test_fragment_is_deterministic(self) -> None:
        first = build_resume_composition(_BOUNDARY, self._REFS, scheduler_name="simple")
        second = build_resume_composition(_BOUNDARY, self._REFS, scheduler_name="simple")
        self.assertEqual(first, second)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
