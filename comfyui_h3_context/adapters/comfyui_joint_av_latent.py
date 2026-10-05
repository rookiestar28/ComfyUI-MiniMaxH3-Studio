"""Torch boundary for M20-04 joint AV latent checkpoints.

The host's joint AV latent is ``{"samples": comfy.nested_tensor.NestedTensor((video, audio))}``
-- a plain two-tensor wrapper from the pinned native source, not ``torch.nested`` -- so the
codec here is exact per-domain dense decomposition and reconstruction.  Decompose reads the
two dense sub-tensors into the headerless video-bytes-then-audio-bytes payload the M20-04
store persists, building the authority-gated descriptor from observed shape/dtype facts.
Reconstruct validates buffer lengths against the descriptor's exact byte arithmetic BEFORE
any tensor is constructed, then reassembles the host pair.

Host modules (``torch``, ``comfy.nested_tensor``) are imported only inside default factories
via :func:`importlib.import_module`; the module itself imports nothing host-side, and every
injectable seam is honored only with ``test_only_seams=True`` (the accepted
``comfyui_continuity`` pattern).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ..core.joint_av_latent import (
    JointAVLatentAuthority,
    JointAVLatentDescriptor,
    JointAVLatentError,
    LatentDomainDescriptor,
    build_joint_av_latent_descriptor,
)


class JointAVLatentAdapterError(ValueError):
    """Raised when the live pair cannot be admitted or reconstructed exactly."""


class PairUnbind(Protocol):
    """Test-only seam: split one live pair object into its ordered sub-tensors."""

    def __call__(self, samples: object) -> tuple[object, ...]: ...


class TensorByteReader(Protocol):
    """Test-only seam: read one dense sub-tensor as (shape, dtype name, exact bytes)."""

    def __call__(self, tensor: object) -> tuple[tuple[int, ...], str, bytes]: ...


class DomainTensorFactory(Protocol):
    """Test-only seam: build one dense domain tensor from exact bytes."""

    def __call__(self, payload: bytes, shape: tuple[int, ...], dtype: str) -> object: ...


class PairFactory(Protocol):
    """Test-only seam: assemble the host pair object from the two domain tensors."""

    def __call__(self, video: object, audio: object) -> object: ...


@dataclass(frozen=True, slots=True)
class DecomposedJointAVLatent:
    """One decomposed checkpoint: the descriptor plus the exact store payload."""

    descriptor: JointAVLatentDescriptor
    payload: bytes

    def __post_init__(self) -> None:
        if type(self.descriptor) is not JointAVLatentDescriptor:
            raise JointAVLatentAdapterError("decomposed_descriptor_type")
        if type(self.payload) is not bytes:
            raise JointAVLatentAdapterError("decomposed_payload_type")
        if len(self.payload) != self.descriptor.payload_byte_length:
            raise JointAVLatentAdapterError("decomposed_payload_length")


def _default_pair_unbind(samples: object) -> tuple[object, ...]:
    unbind = getattr(samples, "unbind", None)
    if not callable(unbind):
        raise JointAVLatentAdapterError("pair_not_nested")
    try:
        return tuple(unbind())
    except JointAVLatentAdapterError:
        raise
    except Exception:
        raise JointAVLatentAdapterError("pair_unbind_failed") from None


def _default_tensor_reader(tensor: object) -> tuple[tuple[int, ...], str, bytes]:
    try:
        torch = importlib.import_module("torch")
    except Exception:
        raise JointAVLatentAdapterError("torch_runtime_unavailable") from None
    if not isinstance(tensor, torch.Tensor):
        raise JointAVLatentAdapterError("domain_tensor_type")
    try:
        dense = tensor.detach().to(device="cpu").contiguous()
        shape = tuple(int(dimension) for dimension in dense.shape)
        dtype = str(dense.dtype).removeprefix("torch.")
        data = dense.reshape(-1).view(torch.uint8).numpy().tobytes()
    except Exception:
        raise JointAVLatentAdapterError("domain_tensor_read_failed") from None
    return shape, dtype, data


def _default_domain_tensor_factory(payload: bytes, shape: tuple[int, ...], dtype: str) -> object:
    try:
        torch = importlib.import_module("torch")
        torch_dtype = getattr(torch, dtype)
        flat = torch.frombuffer(bytearray(payload), dtype=torch.uint8)
        return flat.view(torch_dtype).reshape(shape).clone()
    except Exception:
        raise JointAVLatentAdapterError("torch_runtime_unavailable") from None


def _default_pair_factory(video: object, audio: object) -> object:
    try:
        nested = importlib.import_module("comfy.nested_tensor")
        return nested.NestedTensor((video, audio))
    except Exception:
        raise JointAVLatentAdapterError("host_runtime_unavailable") from None


def _read_domain(
    tensor: object,
    reader: TensorByteReader,
) -> tuple[tuple[int, ...], str, bytes]:
    observed = reader(tensor)
    if type(observed) is not tuple or len(observed) != 3:
        raise JointAVLatentAdapterError("domain_observation_shape")
    shape, dtype, data = observed
    if (
        type(shape) is not tuple
        or not shape
        or any(type(dimension) is not int for dimension in shape)
    ):
        raise JointAVLatentAdapterError("domain_observation_dimensions")
    if type(dtype) is not str or type(data) is not bytes:
        raise JointAVLatentAdapterError("domain_observation_types")
    return shape, dtype, data


def decompose_joint_av_latent(
    latent: Mapping[str, object],
    *,
    authority: JointAVLatentAuthority,
    requested_frames: int,
    completed_frames: int,
    pair_unbind: PairUnbind | None = None,
    tensor_reader: TensorByteReader | None = None,
    test_only_seams: bool = False,
) -> DecomposedJointAVLatent:
    """Read one live pair into its descriptor and exact payload without mutating it."""

    if type(authority) is not JointAVLatentAuthority:
        raise JointAVLatentAdapterError("authority_type")
    if (pair_unbind is not None or tensor_reader is not None) and not test_only_seams:
        raise JointAVLatentAdapterError("test_seams_disabled")
    if not isinstance(latent, Mapping):
        raise JointAVLatentAdapterError("latent_mapping_type")
    samples = latent.get("samples")
    if samples is None:
        raise JointAVLatentAdapterError("latent_samples_missing")
    unbind = _default_pair_unbind if pair_unbind is None else pair_unbind
    parts = unbind(samples)
    if type(parts) is not tuple or len(parts) != 2:
        raise JointAVLatentAdapterError("pair_arity")
    reader = _default_tensor_reader if tensor_reader is None else tensor_reader
    video_shape, video_dtype, video_bytes = _read_domain(parts[0], reader)
    audio_shape, audio_dtype, audio_bytes = _read_domain(parts[1], reader)
    try:
        descriptor = build_joint_av_latent_descriptor(
            authority=authority,
            video_shape=video_shape,
            video_dtype=video_dtype,
            audio_shape=audio_shape,
            audio_dtype=audio_dtype,
            requested_frames=requested_frames,
            completed_frames=completed_frames,
        )
    except JointAVLatentError as error:
        raise JointAVLatentAdapterError(f"descriptor_rejected:{error}") from None
    if len(video_bytes) != descriptor.video.byte_length:
        raise JointAVLatentAdapterError("video_byte_length_mismatch")
    if len(audio_bytes) != descriptor.audio.byte_length:
        raise JointAVLatentAdapterError("audio_byte_length_mismatch")
    return DecomposedJointAVLatent(descriptor=descriptor, payload=video_bytes + audio_bytes)


def _validated_domain_bytes(domain: LatentDomainDescriptor, payload: bytes, label: str) -> bytes:
    if type(payload) is not bytes:
        raise JointAVLatentAdapterError(f"{label}_payload_type")
    if len(payload) != domain.byte_length:
        raise JointAVLatentAdapterError(f"{label}_byte_length_mismatch")
    return payload


def reconstruct_joint_av_latent(
    descriptor: JointAVLatentDescriptor,
    video_bytes: bytes,
    audio_bytes: bytes,
    *,
    tensor_factory: DomainTensorFactory | None = None,
    pair_factory: PairFactory | None = None,
    test_only_seams: bool = False,
) -> dict[str, object]:
    """Rebuild the host latent dict from stored domain bytes; lengths gate construction."""

    if type(descriptor) is not JointAVLatentDescriptor:
        raise JointAVLatentAdapterError("descriptor_type")
    if (tensor_factory is not None or pair_factory is not None) and not test_only_seams:
        raise JointAVLatentAdapterError("test_seams_disabled")
    video_payload = _validated_domain_bytes(descriptor.video, video_bytes, "video")
    audio_payload = _validated_domain_bytes(descriptor.audio, audio_bytes, "audio")
    make_tensor = _default_domain_tensor_factory if tensor_factory is None else tensor_factory
    video = make_tensor(video_payload, descriptor.video.shape, descriptor.video.dtype)
    audio = make_tensor(audio_payload, descriptor.audio.shape, descriptor.audio.dtype)
    make_pair = _default_pair_factory if pair_factory is None else pair_factory
    return {"samples": make_pair(video, audio)}


__all__ = [
    "DecomposedJointAVLatent",
    "DomainTensorFactory",
    "JointAVLatentAdapterError",
    "PairFactory",
    "PairUnbind",
    "TensorByteReader",
    "decompose_joint_av_latent",
    "reconstruct_joint_av_latent",
]
