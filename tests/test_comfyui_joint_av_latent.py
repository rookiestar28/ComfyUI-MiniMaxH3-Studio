"""M20-05 focused tests for the joint AV latent torch-boundary codec.

All host behavior is injected through the ``test_only_seams`` gate, so the assertions here
are about the codec contract: exact byte arithmetic gates every construction, observed
facts feed the authority-gated descriptor unchanged, seams are refused in production form,
and reconstruction never builds a tensor before the buffer lengths are proven.
"""

from __future__ import annotations

import unittest
from collections.abc import Mapping

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.adapters.comfyui_joint_av_latent import (
    DecomposedJointAVLatent,
    JointAVLatentAdapterError,
    decompose_joint_av_latent,
    reconstruct_joint_av_latent,
)
from comfyui_h3_context.core.joint_av_latent import (
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
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

_VIDEO_SHAPE = (1, 24, 1, 1, 1)
_AUDIO_SHAPE = (1, 32, 2, 1)
_VIDEO_BYTES = bytes(range(48))
_AUDIO_BYTES = bytes(range(128, 256))

_DESCRIPTOR = build_joint_av_latent_descriptor(
    authority=_AUTHORITY,
    video_shape=_VIDEO_SHAPE,
    video_dtype="float16",
    audio_shape=_AUDIO_SHAPE,
    audio_dtype="float16",
    requested_frames=22,
    completed_frames=22,
)


class _FakeTensor:
    def __init__(self, shape: tuple[int, ...], dtype: str, data: bytes) -> None:
        self.shape = shape
        self.dtype = dtype
        self.data = data


class _FakePair:
    def __init__(self, *tensors: _FakeTensor) -> None:
        self._tensors = tensors

    def unbind(self) -> tuple[_FakeTensor, ...]:
        return self._tensors


def _fake_reader(tensor: object) -> tuple[tuple[int, ...], str, bytes]:
    assert isinstance(tensor, _FakeTensor)
    return tensor.shape, tensor.dtype, tensor.data


def _fake_unbind(samples: object) -> tuple[object, ...]:
    assert isinstance(samples, _FakePair)
    return samples.unbind()


def _latent(
    video_data: bytes = _VIDEO_BYTES, audio_data: bytes = _AUDIO_BYTES
) -> dict[str, object]:
    return {
        "samples": _FakePair(
            _FakeTensor(_VIDEO_SHAPE, "float16", video_data),
            _FakeTensor(_AUDIO_SHAPE, "float16", audio_data),
        )
    }


def _decompose(latent: Mapping[str, object]) -> DecomposedJointAVLatent:
    return decompose_joint_av_latent(
        latent,
        authority=_AUTHORITY,
        requested_frames=22,
        completed_frames=22,
        pair_unbind=_fake_unbind,
        tensor_reader=_fake_reader,
        test_only_seams=True,
    )


class DecomposeTests(unittest.TestCase):
    def test_live_pair_decomposes_to_descriptor_and_exact_payload(self) -> None:
        decomposed = _decompose(_latent())
        self.assertEqual(decomposed.descriptor.fingerprint, _DESCRIPTOR.fingerprint)
        self.assertEqual(decomposed.payload, _VIDEO_BYTES + _AUDIO_BYTES)
        self.assertEqual(len(decomposed.payload), _DESCRIPTOR.payload_byte_length)

    def test_seams_require_the_explicit_gate(self) -> None:
        with self.assertRaisesRegex(JointAVLatentAdapterError, "test_seams_disabled"):
            decompose_joint_av_latent(
                _latent(),
                authority=_AUTHORITY,
                requested_frames=22,
                completed_frames=22,
                tensor_reader=_fake_reader,
            )

    def test_malformed_inputs_are_typed_refusals(self) -> None:
        with self.assertRaisesRegex(JointAVLatentAdapterError, "authority_type"):
            decompose_joint_av_latent(
                _latent(),
                authority=object(),  # type: ignore[arg-type]
                requested_frames=22,
                completed_frames=22,
                pair_unbind=_fake_unbind,
                tensor_reader=_fake_reader,
                test_only_seams=True,
            )
        one_tensor_pair = _FakePair(_FakeTensor(_VIDEO_SHAPE, "float16", b""))
        cases: tuple[tuple[object, str], ...] = (
            (object(), "latent_mapping_type"),
            ({}, "latent_samples_missing"),
            ({"samples": one_tensor_pair}, "pair_arity"),
            ({"samples": object()}, "pair_not_nested"),
        )
        for latent, code in cases:
            with self.assertRaisesRegex(JointAVLatentAdapterError, code):
                decompose_joint_av_latent(
                    latent,  # type: ignore[arg-type]
                    authority=_AUTHORITY,
                    requested_frames=22,
                    completed_frames=22,
                    tensor_reader=_fake_reader,
                    test_only_seams=True,
                )

    def test_unqualified_layout_is_a_descriptor_rejection(self) -> None:
        bad = {
            "samples": _FakePair(
                _FakeTensor((1, 23, 1, 1, 1), "float16", _VIDEO_BYTES),
                _FakeTensor(_AUDIO_SHAPE, "float16", _AUDIO_BYTES),
            )
        }
        with self.assertRaisesRegex(
            JointAVLatentAdapterError, "descriptor_rejected:video_latent_channels"
        ):
            _decompose(bad)

    def test_buffer_length_divergence_is_refused_per_domain(self) -> None:
        with self.assertRaisesRegex(JointAVLatentAdapterError, "video_byte_length_mismatch"):
            _decompose(_latent(video_data=_VIDEO_BYTES[:-1]))
        with self.assertRaisesRegex(JointAVLatentAdapterError, "audio_byte_length_mismatch"):
            _decompose(_latent(audio_data=_AUDIO_BYTES + b"\x00"))

    def test_misbehaving_reader_is_refused(self) -> None:
        def bad_reader(tensor: object) -> tuple[tuple[int, ...], str, bytes]:
            return (_VIDEO_SHAPE, "float16")  # type: ignore[return-value]

        with self.assertRaisesRegex(JointAVLatentAdapterError, "domain_observation_shape"):
            decompose_joint_av_latent(
                _latent(),
                authority=_AUTHORITY,
                requested_frames=22,
                completed_frames=22,
                pair_unbind=_fake_unbind,
                tensor_reader=bad_reader,
                test_only_seams=True,
            )

    def test_decomposed_invariant_rejects_length_drift(self) -> None:
        with self.assertRaisesRegex(JointAVLatentAdapterError, "decomposed_payload_length"):
            DecomposedJointAVLatent(descriptor=_DESCRIPTOR, payload=b"\x00")


class ReconstructTests(unittest.TestCase):
    def test_roundtrip_rebuilds_the_pair_from_exact_bytes(self) -> None:
        calls: list[tuple[bytes, tuple[int, ...], str]] = []

        def factory(payload: bytes, shape: tuple[int, ...], dtype: str) -> object:
            calls.append((payload, shape, dtype))
            return _FakeTensor(shape, dtype, payload)

        def pair(video: object, audio: object) -> object:
            assert isinstance(video, _FakeTensor) and isinstance(audio, _FakeTensor)
            return _FakePair(video, audio)

        latent = reconstruct_joint_av_latent(
            _DESCRIPTOR,
            _VIDEO_BYTES,
            _AUDIO_BYTES,
            tensor_factory=factory,
            pair_factory=pair,
            test_only_seams=True,
        )
        self.assertEqual(
            calls,
            [
                (_VIDEO_BYTES, _VIDEO_SHAPE, "float16"),
                (_AUDIO_BYTES, _AUDIO_SHAPE, "float16"),
            ],
        )
        rebuilt = latent["samples"]
        assert isinstance(rebuilt, _FakePair)
        video, audio = rebuilt.unbind()
        self.assertEqual(video.data, _VIDEO_BYTES)
        self.assertEqual(audio.data, _AUDIO_BYTES)

    def test_lengths_gate_construction_before_any_factory_call(self) -> None:
        calls: list[object] = []

        def factory(payload: bytes, shape: tuple[int, ...], dtype: str) -> object:
            calls.append(payload)
            return object()

        with self.assertRaisesRegex(JointAVLatentAdapterError, "audio_byte_length_mismatch"):
            reconstruct_joint_av_latent(
                _DESCRIPTOR,
                _VIDEO_BYTES,
                _AUDIO_BYTES[:-1],
                tensor_factory=factory,
                test_only_seams=True,
            )
        self.assertEqual(calls, [])

    def test_seams_and_types_are_gated(self) -> None:
        with self.assertRaisesRegex(JointAVLatentAdapterError, "test_seams_disabled"):
            reconstruct_joint_av_latent(
                _DESCRIPTOR,
                _VIDEO_BYTES,
                _AUDIO_BYTES,
                pair_factory=lambda video, audio: object(),
            )
        with self.assertRaisesRegex(JointAVLatentAdapterError, "descriptor_type"):
            reconstruct_joint_av_latent(
                object(),  # type: ignore[arg-type]
                _VIDEO_BYTES,
                _AUDIO_BYTES,
                test_only_seams=True,
            )
        with self.assertRaisesRegex(JointAVLatentAdapterError, "video_payload_type"):
            reconstruct_joint_av_latent(
                _DESCRIPTOR,
                bytearray(_VIDEO_BYTES),  # type: ignore[arg-type]
                _AUDIO_BYTES,
                test_only_seams=True,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
