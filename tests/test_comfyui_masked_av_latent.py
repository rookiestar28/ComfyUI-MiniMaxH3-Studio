"""M20-06 focused tests for the dual-domain mask attachment adapter.

Fake seams only: the assertions are the codec contract -- exact binding gates every tensor
construction, the suffix values and shapes handed to the factories are exactly the plan's,
the input latent is never mutated, and seams are refused in production form.
"""

from __future__ import annotations

import unittest

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.adapters.comfyui_masked_av_latent import (
    MaskedAVLatentAdapterError,
    attach_nested_av_mask,
)
from comfyui_h3_context.core.joint_av_latent import (
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
)
from comfyui_h3_context.core.masked_av_continuation import (
    DomainMaskExtent,
    NestedAVMaskPlan,
    plan_nested_av_mask,
)
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)

_AUTHORITY = build_joint_av_latent_authority(
    build_temporal_profile(
        AcceptedQualification(
            subject_identity=NATIVE_NODE_SHA256,
            rows=(
                QualifiedRow(
                    row="joint_av_latent_descriptor",
                    status=CapabilityStatus.SUPPORTED,
                    reason_code="descriptor_measured_live",
                    consumer="M20-04",
                ),
            ),
        )
    )
)

_DESCRIPTOR = build_joint_av_latent_descriptor(
    authority=_AUTHORITY,
    video_shape=(1, 24, 7, 16, 24),
    video_dtype="float32",
    audio_shape=(1, 32, 2, 37),
    audio_dtype="float32",
    requested_frames=22,
    completed_frames=22,
)

_PLAN = plan_nested_av_mask(descriptor=_DESCRIPTOR, video_generate_from=4, audio_generate_from=21)


class _FakeMask:
    def __init__(self, values: tuple[float, ...], shape: tuple[int, ...]) -> None:
        self.values = values
        self.shape = shape


class _FakePair:
    def __init__(self, video: object, audio: object) -> None:
        self.video = video
        self.audio = audio


def _mask_factory(values: tuple[float, ...], shape: tuple[int, ...]) -> object:
    return _FakeMask(values, shape)


def _pair_factory(video: object, audio: object) -> object:
    return _FakePair(video, audio)


def _attach(latent: dict[str, object], **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "mask_plan": _PLAN,
        "descriptor": _DESCRIPTOR,
        "mask_tensor_factory": _mask_factory,
        "pair_factory": _pair_factory,
        "test_only_seams": True,
    }
    values.update(overrides)
    return attach_nested_av_mask(latent, **values)  # type: ignore[arg-type]


class AttachTests(unittest.TestCase):
    def test_masks_carry_exact_suffix_values_and_grid_shapes(self) -> None:
        source = {"samples": object()}
        masked = _attach(source)
        pair = masked["noise_mask"]
        assert isinstance(pair, _FakePair)
        video = pair.video
        audio = pair.audio
        assert isinstance(video, _FakeMask) and isinstance(audio, _FakeMask)
        self.assertEqual(video.shape, (1, 1, 7, 16, 24))
        self.assertEqual(video.values, (0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0))
        self.assertEqual(audio.shape, (1, 1, 2, 37))
        self.assertEqual(audio.values, (0.0,) * 21 + (1.0,) * 16)
        self.assertIs(masked["samples"], source["samples"])
        self.assertNotIn("noise_mask", source)

    def test_binding_gates_precede_all_construction(self) -> None:
        calls: list[object] = []

        def counting_factory(values: tuple[float, ...], shape: tuple[int, ...]) -> object:
            calls.append(values)
            return object()

        other = build_joint_av_latent_descriptor(
            authority=_AUTHORITY,
            video_shape=(1, 24, 7, 16, 24),
            video_dtype="float16",
            audio_shape=(1, 32, 2, 37),
            audio_dtype="float16",
            requested_frames=22,
            completed_frames=22,
        )
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "mask_plan_descriptor_mismatch"):
            _attach(
                {"samples": object()},
                descriptor=other,
                mask_tensor_factory=counting_factory,
            )
        self.assertEqual(calls, [])

        drifted = NestedAVMaskPlan(
            video=DomainMaskExtent(length=6, generate_from=0),
            audio=DomainMaskExtent(length=37, generate_from=0),
            descriptor_fingerprint=_DESCRIPTOR.fingerprint,
            descriptor_authority_fingerprint=_DESCRIPTOR.authority_fingerprint,
        )
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "mask_extent_mismatch:video"):
            _attach({"samples": object()}, mask_plan=drifted)
        drifted_audio = NestedAVMaskPlan(
            video=DomainMaskExtent(length=7, generate_from=0),
            audio=DomainMaskExtent(length=36, generate_from=0),
            descriptor_fingerprint=_DESCRIPTOR.fingerprint,
            descriptor_authority_fingerprint=_DESCRIPTOR.authority_fingerprint,
        )
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "mask_extent_mismatch:audio"):
            _attach({"samples": object()}, mask_plan=drifted_audio)

    def test_malformed_latents_and_types_refuse(self) -> None:
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "latent_mapping_type"):
            _attach(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "latent_samples_missing"):
            _attach({})
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "latent_already_masked"):
            _attach({"samples": object(), "noise_mask": object()})
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "mask_plan_type"):
            _attach({"samples": object()}, mask_plan=object())
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "descriptor_type"):
            _attach({"samples": object()}, descriptor=object())

    def test_seams_require_the_explicit_gate(self) -> None:
        with self.assertRaisesRegex(MaskedAVLatentAdapterError, "test_seams_disabled"):
            attach_nested_av_mask(
                {"samples": object()},
                mask_plan=_PLAN,
                descriptor=_DESCRIPTOR,
                pair_factory=_pair_factory,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
