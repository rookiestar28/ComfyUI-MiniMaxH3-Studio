"""M20-07 focused tests for bridge latent composition and interval mask attachment.

Composition is exercised on real byte values: tiny 1x1-spatial synthetic domains whose every
temporal step carries a distinct float32 pattern, so the tail-zero-head assembly is asserted
byte for byte against an independently constructed expectation.  Tensor and pair factories
are fake seams capturing exactly what would reach torch; binding gates are proven to precede
any factory call.
"""

from __future__ import annotations

import struct
import unittest
from typing import Any

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.adapters.comfyui_two_ended_bridge import (
    TwoEndedBridgeAdapterError,
    attach_bridge_interval_mask,
    bridge_domain_shapes,
    compose_bridge_domain_payloads,
    compose_bridge_latent,
)
from comfyui_h3_context.core.joint_av_latent import (
    JointAVLatentDescriptor,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
)
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)
from comfyui_h3_context.core.two_ended_av_bridge import (
    BridgeMaskPlan,
    IntervalMaskExtent,
    MasterAudioDeclaration,
    plan_bridge_mask,
    plan_two_ended_bridge,
)

_PROFILE = build_temporal_profile(
    AcceptedQualification(
        subject_identity=NATIVE_NODE_SHA256,
        rows=(
            QualifiedRow(
                row="joint_av_latent_descriptor",
                status=CapabilityStatus.SUPPORTED,
                reason_code="descriptor_measured_live",
                consumer="M20-04",
            ),
            QualifiedRow(
                row="two_ended_av_bridge",
                status=CapabilityStatus.SUPPORTED,
                reason_code="pixel_domain_two_ended_composition_executes",
                consumer="M20-07",
            ),
        ),
    )
)
_AUTHORITY = build_joint_av_latent_authority(_PROFILE)

#: 90-frame boundaries at 1x1 spatial extent: video (1,24,27,1,1), audio (1,32,2,150).
_VIDEO_SHAPE = (1, 24, 27, 1, 1)
_AUDIO_SHAPE = (1, 32, 2, 150)
_VIDEO_BLOCKS = 24
_VIDEO_STEPS = 27
_AUDIO_BLOCKS = 64
_AUDIO_STEPS = 150

_MASTER = MasterAudioDeclaration(
    master_source_id="master.audio",
    coverage_start_frame=0,
    coverage_end_frame=240,
)


def _descriptor() -> JointAVLatentDescriptor:
    return build_joint_av_latent_descriptor(
        authority=_AUTHORITY,
        video_shape=_VIDEO_SHAPE,
        video_dtype="float32",
        audio_shape=_AUDIO_SHAPE,
        audio_dtype="float32",
        requested_frames=90,
        completed_frames=90,
    )


_LEFT_DESCRIPTOR = _descriptor()
_RIGHT_DESCRIPTOR = _descriptor()

_PLAN = plan_two_ended_bridge(
    left_descriptor=_LEFT_DESCRIPTOR,
    right_descriptor=_RIGHT_DESCRIPTOR,
    descriptor_authority=_AUTHORITY,
    master=_MASTER,
    target_gap_frames=39,
    left_context_frames=51,
    right_context_frames=51,
    left_end_frame=90,
    right_start_frame=129,
)
_MASK_PLAN = plan_bridge_mask(_PLAN)


def _domain_payload(blocks: int, steps: int, *, tag: float) -> bytes:
    """Every (block, step) cell is a distinct float32, so slices are unambiguous."""

    return b"".join(
        struct.pack("<f", tag + block * 1_000.0 + step)
        for block in range(blocks)
        for step in range(steps)
    )


_LEFT_VIDEO = _domain_payload(_VIDEO_BLOCKS, _VIDEO_STEPS, tag=1_000_000.0)
_LEFT_AUDIO = _domain_payload(_AUDIO_BLOCKS, _AUDIO_STEPS, tag=2_000_000.0)
_RIGHT_VIDEO = _domain_payload(_VIDEO_BLOCKS, _VIDEO_STEPS, tag=3_000_000.0)
_RIGHT_AUDIO = _domain_payload(_AUDIO_BLOCKS, _AUDIO_STEPS, tag=4_000_000.0)


def _expected_axis(
    left: bytes,
    right: bytes,
    *,
    blocks: int,
    steps: int,
    take_left: int,
    middle: int,
    take_right: int,
) -> bytes:
    unit = 4
    parts: list[bytes] = []
    for block in range(blocks):
        offset = block * steps * unit
        parts.append(left[offset + (steps - take_left) * unit : offset + steps * unit])
        parts.append(b"\x00" * (middle * unit))
        parts.append(right[offset : offset + take_right * unit])
    return b"".join(parts)


def _compose(**overrides: Any) -> tuple[bytes, bytes]:
    values: dict[str, Any] = {
        "plan": _PLAN,
        "left_descriptor": _LEFT_DESCRIPTOR,
        "right_descriptor": _RIGHT_DESCRIPTOR,
        "left_video_bytes": _LEFT_VIDEO,
        "left_audio_bytes": _LEFT_AUDIO,
        "right_video_bytes": _RIGHT_VIDEO,
        "right_audio_bytes": _RIGHT_AUDIO,
    }
    values.update(overrides)
    return compose_bridge_domain_payloads(**values)


class CompositionPayloadTests(unittest.TestCase):
    def test_bridge_payloads_are_tail_zero_head_exactly(self) -> None:
        video, audio = _compose()
        self.assertEqual(
            video,
            _expected_axis(
                _LEFT_VIDEO,
                _RIGHT_VIDEO,
                blocks=_VIDEO_BLOCKS,
                steps=_VIDEO_STEPS,
                take_left=15,
                middle=12,
                take_right=15,
            ),
        )
        self.assertEqual(
            audio,
            _expected_axis(
                _LEFT_AUDIO,
                _RIGHT_AUDIO,
                blocks=_AUDIO_BLOCKS,
                steps=_AUDIO_STEPS,
                take_left=85,
                middle=65,
                take_right=85,
            ),
        )

    def test_composed_extents_match_the_declared_bridge_shapes(self) -> None:
        video, audio = _compose()
        video_shape, audio_shape = bridge_domain_shapes(_PLAN, _LEFT_DESCRIPTOR)
        self.assertEqual(video_shape, (1, 24, 42, 1, 1))
        self.assertEqual(audio_shape, (1, 32, 2, 235))
        self.assertEqual(len(video), 24 * 42 * 4)
        self.assertEqual(len(audio), 64 * 235 * 4)

    def test_payload_length_mismatch_refuses(self) -> None:
        with self.assertRaisesRegex(
            TwoEndedBridgeAdapterError, "payload_length_mismatch:left_video"
        ):
            _compose(left_video_bytes=_LEFT_VIDEO[:-4])
        with self.assertRaisesRegex(
            TwoEndedBridgeAdapterError, "payload_length_mismatch:right_video"
        ):
            _compose(right_video_bytes=_RIGHT_VIDEO[:-4])
        with self.assertRaisesRegex(
            TwoEndedBridgeAdapterError, "payload_length_mismatch:left_audio"
        ):
            _compose(left_audio_bytes=_LEFT_AUDIO[:-4])
        with self.assertRaisesRegex(
            TwoEndedBridgeAdapterError, "payload_length_mismatch:right_audio"
        ):
            _compose(right_audio_bytes=_RIGHT_AUDIO[:-4])

    def test_tampered_plan_steps_cannot_exceed_the_source_domains(self) -> None:
        from dataclasses import replace

        with self.assertRaisesRegex(TwoEndedBridgeAdapterError, "context_exceeds_domain:video"):
            _compose(plan=replace(_PLAN, left_video_steps=100))
        with self.assertRaisesRegex(TwoEndedBridgeAdapterError, "context_exceeds_domain:audio"):
            _compose(plan=replace(_PLAN, right_audio_steps=200))

    def test_foreign_descriptor_refuses_binding(self) -> None:
        foreign = build_joint_av_latent_descriptor(
            authority=_AUTHORITY,
            video_shape=_VIDEO_SHAPE,
            video_dtype="float32",
            audio_shape=_AUDIO_SHAPE,
            audio_dtype="float32",
            requested_frames=90,
            completed_frames=51,
        )
        with self.assertRaisesRegex(TwoEndedBridgeAdapterError, "plan_descriptor_mismatch:left"):
            _compose(left_descriptor=foreign)
        with self.assertRaisesRegex(TwoEndedBridgeAdapterError, "plan_descriptor_mismatch:right"):
            _compose(right_descriptor=foreign)


class ComposeLatentTests(unittest.TestCase):
    def test_factories_receive_the_exact_payloads_and_shapes(self) -> None:
        seen: list[tuple[tuple[int, ...], str, int]] = []

        def tensor_factory(payload: bytes, shape: tuple[int, ...], dtype: str) -> object:
            seen.append((shape, dtype, len(payload)))
            return ("tensor", shape)

        def pair_factory(video: object, audio: object) -> object:
            return ("pair", video, audio)

        latent = compose_bridge_latent(
            plan=_PLAN,
            left_descriptor=_LEFT_DESCRIPTOR,
            right_descriptor=_RIGHT_DESCRIPTOR,
            left_video_bytes=_LEFT_VIDEO,
            left_audio_bytes=_LEFT_AUDIO,
            right_video_bytes=_RIGHT_VIDEO,
            right_audio_bytes=_RIGHT_AUDIO,
            tensor_factory=tensor_factory,
            pair_factory=pair_factory,
            test_only_seams=True,
        )
        self.assertEqual(
            seen,
            [
                ((1, 24, 42, 1, 1), "float32", 24 * 42 * 4),
                ((1, 32, 2, 235), "float32", 64 * 235 * 4),
            ],
        )
        self.assertEqual(
            latent["samples"],
            ("pair", ("tensor", (1, 24, 42, 1, 1)), ("tensor", (1, 32, 2, 235))),
        )

    def test_seams_require_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeAdapterError, "test_seams_disabled"):
            compose_bridge_latent(
                plan=_PLAN,
                left_descriptor=_LEFT_DESCRIPTOR,
                right_descriptor=_RIGHT_DESCRIPTOR,
                left_video_bytes=_LEFT_VIDEO,
                left_audio_bytes=_LEFT_AUDIO,
                right_video_bytes=_RIGHT_VIDEO,
                right_audio_bytes=_RIGHT_AUDIO,
                tensor_factory=lambda payload, shape, dtype: None,
            )


class AttachIntervalMaskTests(unittest.TestCase):
    def _attach(self, latent: dict[str, object], **overrides: Any) -> dict[str, object]:
        values: dict[str, Any] = {
            "mask_plan": _MASK_PLAN,
            "plan": _PLAN,
            "left_descriptor": _LEFT_DESCRIPTOR,
            "mask_tensor_factory": overrides.pop(
                "mask_tensor_factory", lambda values_, shape: ("mask", values_, shape)
            ),
            "pair_factory": overrides.pop(
                "pair_factory", lambda video, audio: ("pair", video, audio)
            ),
            "test_only_seams": True,
        }
        values.update(overrides)
        return attach_bridge_interval_mask(latent, **values)

    def test_interval_values_render_preserve_generate_preserve(self) -> None:
        captured: list[tuple[tuple[float, ...], tuple[int, ...]]] = []

        def factory(values: tuple[float, ...], shape: tuple[int, ...]) -> object:
            captured.append((values, shape))
            return ("mask", shape)

        masked = self._attach({"samples": "latent"}, mask_tensor_factory=factory)
        video_values, video_shape = captured[0]
        audio_values, audio_shape = captured[1]
        self.assertEqual(video_shape, (1, 1, 42, 1, 1))
        self.assertEqual(audio_shape, (1, 1, 2, 235))
        self.assertEqual(video_values, (0.0,) * 15 + (1.0,) * 12 + (0.0,) * 15)
        self.assertEqual(audio_values, (0.0,) * 85 + (1.0,) * 65 + (0.0,) * 85)
        self.assertEqual(masked["samples"], "latent")
        self.assertEqual(
            masked["noise_mask"], ("pair", ("mask", video_shape), ("mask", audio_shape))
        )

    def test_input_latent_is_never_mutated(self) -> None:
        latent: dict[str, object] = {"samples": "latent"}
        self._attach(latent)
        self.assertEqual(latent, {"samples": "latent"})

    def test_already_masked_latent_refuses(self) -> None:
        with self.assertRaisesRegex(TwoEndedBridgeAdapterError, "latent_already_masked"):
            self._attach({"samples": "latent", "noise_mask": "existing"})

    def test_mask_from_another_plan_refuses(self) -> None:
        other_plan = plan_two_ended_bridge(
            left_descriptor=_LEFT_DESCRIPTOR,
            right_descriptor=_RIGHT_DESCRIPTOR,
            descriptor_authority=_AUTHORITY,
            master=_MASTER,
            target_gap_frames=39,
            left_context_frames=51,
            right_context_frames=51,
            left_end_frame=90,
            right_start_frame=129,
            left_lookahead_frames=1,
        )
        with self.assertRaisesRegex(TwoEndedBridgeAdapterError, "mask_plan_binding_mismatch"):
            self._attach({"samples": "latent"}, mask_plan=plan_bridge_mask(other_plan))

    def test_drifted_mask_extent_refuses_before_factories(self) -> None:
        drifted = BridgeMaskPlan(
            video=IntervalMaskExtent(length=41, generate_from=15, generate_to=27),
            audio=_MASK_PLAN.audio,
            bridge_plan_fingerprint=_PLAN.fingerprint,
        )

        def exploding_factory(values: tuple[float, ...], shape: tuple[int, ...]) -> object:
            raise AssertionError("factory must not run")

        with self.assertRaisesRegex(TwoEndedBridgeAdapterError, "mask_extent_mismatch:video"):
            self._attach(
                {"samples": "latent"},
                mask_plan=drifted,
                mask_tensor_factory=exploding_factory,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
