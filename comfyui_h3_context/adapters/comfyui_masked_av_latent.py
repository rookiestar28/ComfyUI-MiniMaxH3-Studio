"""Torch boundary for dual-domain AV mask attachment (M20-06).

Builds the two dense per-domain mask tensors an admitted :class:`NestedAVMaskPlan` describes
-- video ``(1, 1, T, H, W)`` and audio ``(1, 1, 2, L)`` at the exact latent grid, float32,
values only ``MASK_PRESERVE``/``MASK_GENERATE`` -- assembles the host pair value
``comfy.nested_tensor.NestedTensor((video_mask, audio_mask))``, and returns a NEW latent
dict with ``noise_mask`` attached.  The input latent is never mutated; the sampler consumes
``noise_mask`` verbatim and applies one mask per domain (the M19-08-measured mechanism).

The mask tensors carry the full spatial extent of their domain: the plan is temporal-only
(suffix form), so every spatial position within a preserved step is preserved and within a
generated step is generated.  Emitting the exact latent grid means the host's
``reshape_mask`` interpolation is the identity for the temporal axes; the channel dimension
is left at 1 for the host to repeat, which is exact replication, not interpolation.

Host modules import only inside default factories via :func:`importlib.import_module`;
injectable seams are honored only with ``test_only_seams=True`` (the accepted
``comfyui_continuity`` pattern, same as ``comfyui_joint_av_latent``).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Protocol

from ..core.joint_av_latent import JointAVLatentDescriptor
from ..core.masked_av_continuation import (
    MASK_GENERATE,
    MASK_PRESERVE,
    NestedAVMaskPlan,
)


class MaskedAVLatentAdapterError(ValueError):
    """Raised when a mask pair cannot be constructed or attached exactly."""


class MaskTensorFactory(Protocol):
    """Test-only seam: build one dense mask tensor from exact per-step values."""

    def __call__(self, values: tuple[float, ...], shape: tuple[int, ...]) -> object: ...


class PairFactory(Protocol):
    """Test-only seam: assemble the host pair value from the two domain masks."""

    def __call__(self, video: object, audio: object) -> object: ...


def _suffix_values(length: int, generate_from: int) -> tuple[float, ...]:
    return tuple(
        MASK_PRESERVE if index < generate_from else MASK_GENERATE for index in range(length)
    )


def _default_mask_tensor_factory(values: tuple[float, ...], shape: tuple[int, ...]) -> object:
    try:
        torch = importlib.import_module("torch")
        steps = torch.tensor(values, dtype=torch.float32)
        if len(shape) == 5:
            # Video (1, 1, T, H, W): each temporal step's value fills its full spatial extent.
            expanded = steps.reshape(1, 1, -1, 1, 1).expand(shape)
        else:
            # Audio (1, 1, 2, L): the temporal axis is last; both planes share the step value.
            expanded = steps.reshape(1, 1, 1, -1).expand(shape)
        return expanded.contiguous()
    except Exception:
        raise MaskedAVLatentAdapterError("torch_runtime_unavailable") from None


def _default_pair_factory(video: object, audio: object) -> object:
    try:
        nested = importlib.import_module("comfy.nested_tensor")
        return nested.NestedTensor((video, audio))
    except Exception:
        raise MaskedAVLatentAdapterError("host_runtime_unavailable") from None


def _domain_mask_shape(descriptor: JointAVLatentDescriptor, *, video: bool) -> tuple[int, ...]:
    if video:
        _, _, frames, height, width = descriptor.video.shape
        return (1, 1, frames, height, width)
    _, _, planes, steps = descriptor.audio.shape
    return (1, 1, planes, steps)


def attach_nested_av_mask(
    latent: Mapping[str, object],
    *,
    mask_plan: NestedAVMaskPlan,
    descriptor: JointAVLatentDescriptor,
    mask_tensor_factory: MaskTensorFactory | None = None,
    pair_factory: PairFactory | None = None,
    test_only_seams: bool = False,
) -> dict[str, object]:
    """Attach the plan's nested mask to a copy of the latent; exact binding gates all work.

    The plan must be planned against the exact descriptor handed in -- the fingerprints must
    match, and the plan's per-domain lengths must equal the descriptor's temporal extents --
    before any tensor is constructed.
    """

    if type(mask_plan) is not NestedAVMaskPlan:
        raise MaskedAVLatentAdapterError("mask_plan_type")
    if type(descriptor) is not JointAVLatentDescriptor:
        raise MaskedAVLatentAdapterError("descriptor_type")
    if (mask_tensor_factory is not None or pair_factory is not None) and not test_only_seams:
        raise MaskedAVLatentAdapterError("test_seams_disabled")
    if not isinstance(latent, Mapping):
        raise MaskedAVLatentAdapterError("latent_mapping_type")
    if latent.get("samples") is None:
        raise MaskedAVLatentAdapterError("latent_samples_missing")
    if "noise_mask" in latent:
        raise MaskedAVLatentAdapterError("latent_already_masked")
    if mask_plan.descriptor_fingerprint != descriptor.fingerprint:
        raise MaskedAVLatentAdapterError("mask_plan_descriptor_mismatch")
    if mask_plan.video.length != descriptor.video.shape[2]:
        raise MaskedAVLatentAdapterError("mask_extent_mismatch:video")
    if mask_plan.audio.length != descriptor.audio.shape[3]:
        raise MaskedAVLatentAdapterError("mask_extent_mismatch:audio")

    make_mask = _default_mask_tensor_factory if mask_tensor_factory is None else mask_tensor_factory
    video_mask = make_mask(
        _suffix_values(mask_plan.video.length, mask_plan.video.generate_from),
        _domain_mask_shape(descriptor, video=True),
    )
    audio_mask = make_mask(
        _suffix_values(mask_plan.audio.length, mask_plan.audio.generate_from),
        _domain_mask_shape(descriptor, video=False),
    )
    make_pair = _default_pair_factory if pair_factory is None else pair_factory
    masked = dict(latent)
    masked["noise_mask"] = make_pair(video_mask, audio_mask)
    return masked


__all__ = [
    "MaskTensorFactory",
    "MaskedAVLatentAdapterError",
    "PairFactory",
    "attach_nested_av_mask",
]
