"""M20-04 focused tests for the joint AV latent descriptor, authority and receipt.

The property held apart here is promotion: the artifact family exists only downstream of the
M19-08 `joint_av_latent_descriptor = supported` row, and no descriptor, receipt, decode path or
public projection may manufacture that state.  The second property is exact byte arithmetic:
the payload is two raw dense domains, so every byte-length claim is an integer equation this
suite can check against hand-computed values, with no tensor library anywhere.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.core.joint_av_latent import (
    JOINT_AV_LATENT_PUBLIC_SCHEMA,
    JointAVLatentAuthority,
    JointAVLatentDescriptor,
    JointAVLatentError,
    JointAVLatentReceipt,
    LatentDomain,
    LatentDomainDescriptor,
    begin_joint_av_latent_receipt,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
    complete_joint_av_latent_receipt,
    decode_joint_av_latent_receipt,
    fail_joint_av_latent_receipt,
)
from comfyui_h3_context.core.segment_artifacts import ArtifactLifecycleState
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)

#: Video [1, 24, 3, 16, 24] float32 -> 1*24*3*16*24*4 = 110_592 bytes.
_VIDEO_SHAPE = (1, 24, 3, 16, 24)
_VIDEO_BYTES = 110_592
#: Audio [1, 32, 2, 30] float32 -> 1*32*2*30*4 = 7_680 bytes.
_AUDIO_SHAPE = (1, 32, 2, 30)
_AUDIO_BYTES = 7_680

_FP = "sha256:" + "ab" * 32


def _accepted(status: CapabilityStatus = CapabilityStatus.SUPPORTED) -> AcceptedQualification:
    """The M19-08 refreshed projection: the descriptor row measured live."""

    return AcceptedQualification(
        subject_identity=NATIVE_NODE_SHA256,
        rows=(
            QualifiedRow(
                row="joint_av_latent_descriptor",
                status=status,
                reason_code=(
                    "descriptor_measured_live"
                    if status is CapabilityStatus.SUPPORTED
                    else "requires_weight_backed_execution"
                ),
                consumer="M20-04",
            ),
        ),
    )


def _authority() -> JointAVLatentAuthority:
    return build_joint_av_latent_authority(build_temporal_profile(_accepted()))


def _descriptor(
    authority: JointAVLatentAuthority | None = None, **overrides: object
) -> JointAVLatentDescriptor:
    values: dict[str, object] = {
        "authority": authority if authority is not None else _authority(),
        "video_shape": _VIDEO_SHAPE,
        "video_dtype": "float32",
        "audio_shape": _AUDIO_SHAPE,
        "audio_dtype": "float32",
        "requested_frames": 22,
        "completed_frames": 22,
    }
    values.update(overrides)
    return build_joint_av_latent_descriptor(**values)  # type: ignore[arg-type]


def _receipt(
    descriptor: JointAVLatentDescriptor | None = None,
    authority: JointAVLatentAuthority | None = None,
) -> JointAVLatentReceipt:
    resolved_authority = authority if authority is not None else _authority()
    resolved = descriptor if descriptor is not None else _descriptor(authority=resolved_authority)
    return begin_joint_av_latent_receipt(
        descriptor=resolved,
        authority=resolved_authority,
        artifact_id="latent-chk-001",
        transaction_fingerprint=_FP,
        execution_fingerprint=_FP,
        model_fingerprint=_FP,
        runtime_fingerprint=_FP,
        settings_fingerprint=_FP,
        source_id="segment-a",
        predecessor_artifact_fingerprint=None,
        created_at_ms=1_000,
        expires_at_ms=61_000,
    )


class AuthorityTests(unittest.TestCase):
    def test_supported_row_freezes_a_fingerprinted_authority(self) -> None:
        authority = _authority()
        self.assertEqual(authority.subject_identity, NATIVE_NODE_SHA256)
        self.assertEqual(authority.reason_code, "descriptor_measured_live")
        self.assertTrue(authority.fingerprint.startswith("sha256:"))
        again = build_joint_av_latent_authority(build_temporal_profile(_accepted()))
        self.assertEqual(authority.fingerprint, again.fingerprint)

    def test_unqualified_and_unsupported_rows_refuse_construction(self) -> None:
        with self.assertRaisesRegex(JointAVLatentError, "descriptor_row_not_supported"):
            build_joint_av_latent_authority(build_temporal_profile())
        with self.assertRaisesRegex(JointAVLatentError, "descriptor_row_not_supported"):
            build_joint_av_latent_authority(
                build_temporal_profile(_accepted(CapabilityStatus.UNQUALIFIED))
            )
        with self.assertRaisesRegex(JointAVLatentError, "descriptor_row_not_supported"):
            build_joint_av_latent_authority(
                build_temporal_profile(_accepted(CapabilityStatus.UNSUPPORTED))
            )

    def test_authority_requires_a_real_profile(self) -> None:
        with self.assertRaisesRegex(JointAVLatentError, "authority_requires_temporal_profile"):
            build_joint_av_latent_authority(object())  # type: ignore[arg-type]


class DescriptorTests(unittest.TestCase):
    def test_byte_arithmetic_is_exact_for_both_domains(self) -> None:
        descriptor = _descriptor()
        self.assertEqual(descriptor.video.byte_length, _VIDEO_BYTES)
        self.assertEqual(descriptor.audio.byte_length, _AUDIO_BYTES)
        self.assertEqual(descriptor.payload_byte_length, _VIDEO_BYTES + _AUDIO_BYTES)

    def test_half_precision_dtypes_halve_the_extent(self) -> None:
        for dtype in ("float16", "bfloat16"):
            descriptor = _descriptor(video_dtype=dtype, audio_dtype=dtype)
            self.assertEqual(descriptor.video.byte_length, _VIDEO_BYTES // 2)
            self.assertEqual(descriptor.audio.byte_length, _AUDIO_BYTES // 2)

    def test_measured_layout_pins_are_enforced(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "latent_rank:video": {"video_shape": (1, 24, 3, 16)},
            "video_latent_channels": {"video_shape": (1, 23, 3, 16, 24)},
            "latent_rank:audio": {"audio_shape": (1, 32, 2, 30, 1)},
            "audio_latent_channels": {"audio_shape": (1, 31, 2, 30)},
            "audio_latent_planes": {"audio_shape": (1, 32, 3, 30)},
            "descriptor_batch_mismatch": {"audio_shape": (2, 32, 2, 30)},
            "latent_dtype:video": {"video_dtype": "float64"},
            "latent_dtype:audio": {"audio_dtype": "int8"},
        }
        for code, overrides in cases.items():
            with self.assertRaisesRegex(JointAVLatentError, code, msg=code):
                _descriptor(**overrides)

    def test_completed_boundary_stays_inside_the_requested_extent(self) -> None:
        descriptor = _descriptor(completed_frames=7)
        self.assertEqual(descriptor.completed_frames, 7)
        with self.assertRaisesRegex(JointAVLatentError, "positive_integer:completed_frames"):
            _descriptor(completed_frames=23)
        with self.assertRaisesRegex(JointAVLatentError, "positive_integer:completed_frames"):
            _descriptor(completed_frames=0)

    def test_direct_byte_length_tamper_is_rejected(self) -> None:
        with self.assertRaisesRegex(JointAVLatentError, "latent_byte_arithmetic:video"):
            LatentDomainDescriptor(
                domain=LatentDomain.VIDEO,
                shape=_VIDEO_SHAPE,
                dtype="float32",
                byte_length=_VIDEO_BYTES + 1,
            )

    def test_descriptor_fingerprint_self_verifies_and_rejects_tamper(self) -> None:
        descriptor = _descriptor()
        self.assertTrue(descriptor.fingerprint.startswith("sha256:"))
        with self.assertRaisesRegex(JointAVLatentError, "descriptor_fingerprint_mismatch"):
            replace(descriptor, descriptor_fingerprint="sha256:" + "00" * 32)

    def test_descriptor_requires_the_authority_object(self) -> None:
        with self.assertRaisesRegex(JointAVLatentError, "descriptor_requires_authority"):
            _descriptor(authority="sha256:" + "cd" * 32)  # type: ignore[arg-type]


class ReceiptLifecycleTests(unittest.TestCase):
    def test_begin_binds_descriptor_and_authority_exactly(self) -> None:
        receipt = _receipt()
        self.assertIs(receipt.state, ArtifactLifecycleState.PARTIAL)
        self.assertEqual(receipt.byte_length, 0)
        self.assertIsNone(receipt.output_fingerprint)

    def test_begin_rejects_a_descriptor_from_another_authority(self) -> None:
        foreign = replace(
            _authority(),
            profile_fingerprint="sha256:" + "ef" * 32,
        )
        descriptor = _descriptor(authority=foreign)
        with self.assertRaisesRegex(JointAVLatentError, "descriptor_authority_mismatch"):
            _receipt(descriptor=descriptor)

    def test_complete_requires_the_exact_payload_extent(self) -> None:
        receipt = _receipt()
        total = receipt.descriptor.payload_byte_length
        with self.assertRaisesRegex(JointAVLatentError, "payload_extent_mismatch"):
            complete_joint_av_latent_receipt(receipt, output_fingerprint=_FP, byte_length=total - 1)
        completed = complete_joint_av_latent_receipt(
            receipt, output_fingerprint=_FP, byte_length=total
        )
        self.assertIs(completed.state, ArtifactLifecycleState.COMPLETE)
        self.assertEqual(completed.byte_length, total)

    def test_fail_records_a_code_and_terminal_states_do_not_transition(self) -> None:
        receipt = _receipt()
        failed = fail_joint_av_latent_receipt(receipt, failure_code="producer_cancelled")
        self.assertIs(failed.state, ArtifactLifecycleState.FAILED)
        for terminal in (
            failed,
            complete_joint_av_latent_receipt(
                receipt,
                output_fingerprint=_FP,
                byte_length=receipt.descriptor.payload_byte_length,
            ),
        ):
            with self.assertRaisesRegex(JointAVLatentError, "receipt_not_partial"):
                complete_joint_av_latent_receipt(
                    terminal,
                    output_fingerprint=_FP,
                    byte_length=receipt.descriptor.payload_byte_length,
                )
            with self.assertRaisesRegex(JointAVLatentError, "receipt_not_partial"):
                fail_joint_av_latent_receipt(terminal, failure_code="late")

    def test_lifecycle_shapes_are_closed(self) -> None:
        receipt = _receipt()
        with self.assertRaisesRegex(JointAVLatentError, "partial_receipt_shape"):
            replace(receipt, byte_length=8, receipt_fingerprint=None)
        with self.assertRaisesRegex(JointAVLatentError, "complete_receipt_shape"):
            replace(
                receipt,
                state=ArtifactLifecycleState.COMPLETE,
                receipt_fingerprint=None,
            )
        with self.assertRaisesRegex(JointAVLatentError, "failed_receipt_shape"):
            replace(
                receipt,
                state=ArtifactLifecycleState.FAILED,
                receipt_fingerprint=None,
            )

    def test_ttl_is_bounded(self) -> None:
        receipt = _receipt()
        with self.assertRaisesRegex(JointAVLatentError, "artifact_ttl"):
            replace(
                receipt,
                expires_at_ms=receipt.created_at_ms,
                receipt_fingerprint=None,
            )


class WireTests(unittest.TestCase):
    def test_wire_round_trip_is_exact(self) -> None:
        receipt = complete_joint_av_latent_receipt(
            _receipt(),
            output_fingerprint=_FP,
            byte_length=_VIDEO_BYTES + _AUDIO_BYTES,
        )
        decoded = decode_joint_av_latent_receipt(receipt.to_wire_bytes())
        self.assertEqual(decoded, receipt)
        self.assertEqual(decoded.fingerprint, receipt.fingerprint)

    def test_wire_rejects_duplicate_members_fingerprint_tamper_and_drift(self) -> None:
        receipt = _receipt()
        wire = receipt.to_wire_bytes()
        duplicated = wire[:-1] + b',"artifact_id":"latent-chk-001"}'
        with self.assertRaisesRegex(JointAVLatentError, "duplicate_receipt_member"):
            decode_joint_av_latent_receipt(duplicated)
        tampered = wire.replace(b"latent-chk-001", b"latent-chk-002")
        with self.assertRaisesRegex(JointAVLatentError, "receipt_fingerprint_mismatch"):
            decode_joint_av_latent_receipt(tampered)
        with self.assertRaisesRegex(JointAVLatentError, "receipt_wire_members"):
            decode_joint_av_latent_receipt(b'{"schema":"nope"}')

    def test_public_dict_is_locator_free_with_redacted_tokens(self) -> None:
        receipt = _receipt()
        public = receipt.to_public_dict()
        self.assertEqual(public["schema"], JOINT_AV_LATENT_PUBLIC_SCHEMA)
        self.assertEqual(public["video_shape"], list(_VIDEO_SHAPE))
        self.assertEqual(public["completed_frames"], 22)
        rendered = str(public)
        self.assertNotIn("sha256:", rendered)
        self.assertNotIn("\\", rendered)
        self.assertNotIn("/", rendered)
        self.assertTrue(str(public["receipt_token"]).startswith("redacted:"))
        self.assertTrue(str(public["descriptor_token"]).startswith("redacted:"))
        self.assertIsNone(public["output_token"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
