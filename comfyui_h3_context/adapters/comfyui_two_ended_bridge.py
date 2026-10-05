"""Torch boundary for two-ended bridge latent composition and interval masking (M20-07).

Composition is exact byte arithmetic first and tensors second.  The M20-04 store hands each
boundary's domains back as headerless C-contiguous little-endian buffers, so the bridge
input -- left tail context, zero-initialized middle, right head context, per domain -- is
assembled by pure-Python block slicing over the declared plan extents (a temporal-axis
slice of a C-contiguous buffer is a fixed-stride byte window; float zeros of every admitted
dtype are zero bytes).  Only the finished payloads become tensors, through the same default
factories the reviewed M20-04 codec uses, and the pair is the host's
``comfy.nested_tensor.NestedTensor``.

The interval mask renders the M20-06 sampler semantics (``0 = preserve, 1 = generate``,
one dense mask per domain at the exact latent grid) in the two-sided form the bridge plan
declares: preserved prefix, generated middle, preserved suffix.  Every binding gate --
plan-to-descriptor fingerprints, byte lengths against descriptor arithmetic, mask-to-plan
fingerprint, extent agreement -- precedes any tensor construction.

Host modules import only inside default factories via :func:`importlib.import_module`;
injectable seams are honored only with ``test_only_seams=True`` (the accepted
``comfyui_continuity`` pattern, same as ``comfyui_joint_av_latent``).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Protocol

from ..core.joint_av_latent import (
    LATENT_DTYPE_ITEM_SIZES,
    JointAVLatentDescriptor,
)
from ..core.masked_av_continuation import MASK_GENERATE, MASK_PRESERVE
from ..core.two_ended_av_bridge import (
    BridgeMaskPlan,
    TwoEndedBridgePlan,
)


class TwoEndedBridgeAdapterError(ValueError):
    """Raised when a bridge latent or mask cannot be composed or attached exactly."""


class DomainTensorFactory(Protocol):
    """Test-only seam: build one dense domain tensor from exact bytes."""

    def __call__(self, payload: bytes, shape: tuple[int, ...], dtype: str) -> object: ...


class MaskTensorFactory(Protocol):
    """Test-only seam: build one dense mask tensor from exact per-step values."""

    def __call__(self, values: tuple[float, ...], shape: tuple[int, ...]) -> object: ...


class PairFactory(Protocol):
    """Test-only seam: assemble the host pair value from the two domain tensors."""

    def __call__(self, video: object, audio: object) -> object: ...


def _default_domain_tensor_factory(payload: bytes, shape: tuple[int, ...], dtype: str) -> object:
    try:
        torch = importlib.import_module("torch")
        torch_dtype = getattr(torch, dtype)
        flat = torch.frombuffer(bytearray(payload), dtype=torch.uint8)
        return flat.view(torch_dtype).reshape(shape).clone()
    except Exception:
        raise TwoEndedBridgeAdapterError("torch_runtime_unavailable") from None


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
        raise TwoEndedBridgeAdapterError("torch_runtime_unavailable") from None


def _default_pair_factory(video: object, audio: object) -> object:
    try:
        nested = importlib.import_module("comfy.nested_tensor")
        return nested.NestedTensor((video, audio))
    except Exception:
        raise TwoEndedBridgeAdapterError("host_runtime_unavailable") from None


def _interval_values(length: int, generate_from: int, generate_to: int) -> tuple[float, ...]:
    return tuple(
        MASK_GENERATE if generate_from <= index < generate_to else MASK_PRESERVE
        for index in range(length)
    )


def _domain_bytes(value: object, expected_length: int, field: str) -> bytes:
    if type(value) is not bytes:
        raise TwoEndedBridgeAdapterError(f"payload_type:{field}")
    if len(value) != expected_length:
        raise TwoEndedBridgeAdapterError(f"payload_length_mismatch:{field}")
    return value


def _compose_axis_blocks(
    left: bytes,
    right: bytes,
    *,
    blocks: int,
    left_steps: int,
    right_steps: int,
    take_left: int,
    middle_steps: int,
    take_right: int,
    unit: int,
) -> bytes:
    """Concatenate tail-of-left, zero middle and head-of-right along one temporal axis.

    Both buffers hold ``blocks`` contiguous blocks of their own step count; the output holds
    the same block count at ``take_left + middle_steps + take_right`` steps each.  Zero bytes
    are the exact encoding of 0.0 in every admitted dtype.
    """

    zero_middle = b"\x00" * (middle_steps * unit)
    left_block = left_steps * unit
    right_block = right_steps * unit
    tail = take_left * unit
    head = take_right * unit
    parts: list[bytes] = []
    for index in range(blocks):
        left_offset = index * left_block
        right_offset = index * right_block
        parts.append(left[left_offset + left_block - tail : left_offset + left_block])
        parts.append(zero_middle)
        parts.append(right[right_offset : right_offset + head])
    return b"".join(parts)


def _checked_bindings(
    plan: TwoEndedBridgePlan,
    left_descriptor: JointAVLatentDescriptor,
    right_descriptor: JointAVLatentDescriptor,
) -> None:
    if type(plan) is not TwoEndedBridgePlan:
        raise TwoEndedBridgeAdapterError("plan_type")
    if (
        type(left_descriptor) is not JointAVLatentDescriptor
        or type(right_descriptor) is not JointAVLatentDescriptor
    ):
        raise TwoEndedBridgeAdapterError("descriptor_type")
    if plan.left_descriptor_fingerprint != left_descriptor.fingerprint:
        raise TwoEndedBridgeAdapterError("plan_descriptor_mismatch:left")
    if plan.right_descriptor_fingerprint != right_descriptor.fingerprint:
        raise TwoEndedBridgeAdapterError("plan_descriptor_mismatch:right")


def bridge_domain_shapes(
    plan: TwoEndedBridgePlan,
    left_descriptor: JointAVLatentDescriptor,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The composed bridge latent's exact per-domain shapes, derived from the plan."""

    if type(plan) is not TwoEndedBridgePlan:
        raise TwoEndedBridgeAdapterError("plan_type")
    if type(left_descriptor) is not JointAVLatentDescriptor:
        raise TwoEndedBridgeAdapterError("descriptor_type")
    batch, channels, _, height, width = left_descriptor.video.shape
    _, audio_channels, planes, _ = left_descriptor.audio.shape
    video_steps = plan.left_video_steps + plan.middle_video_steps + plan.right_video_steps
    audio_steps = plan.left_audio_steps + plan.middle_audio_steps + plan.right_audio_steps
    return (
        (batch, channels, video_steps, height, width),
        (batch, audio_channels, planes, audio_steps),
    )


def compose_bridge_domain_payloads(
    *,
    plan: TwoEndedBridgePlan,
    left_descriptor: JointAVLatentDescriptor,
    right_descriptor: JointAVLatentDescriptor,
    left_video_bytes: bytes,
    left_audio_bytes: bytes,
    right_video_bytes: bytes,
    right_audio_bytes: bytes,
) -> tuple[bytes, bytes]:
    """Assemble the bridge input payloads by exact block slicing; bindings gate all work.

    Pure Python and deterministic: the same declared plan over the same stored bytes yields
    the same payloads, byte for byte, with no tensor library in the loop.
    """

    _checked_bindings(plan, left_descriptor, right_descriptor)
    left_video = _domain_bytes(left_video_bytes, left_descriptor.video.byte_length, "left_video")
    left_audio = _domain_bytes(left_audio_bytes, left_descriptor.audio.byte_length, "left_audio")
    right_video = _domain_bytes(
        right_video_bytes, right_descriptor.video.byte_length, "right_video"
    )
    right_audio = _domain_bytes(
        right_audio_bytes, right_descriptor.audio.byte_length, "right_audio"
    )

    batch, channels, left_video_steps, height, width = left_descriptor.video.shape
    right_video_steps = right_descriptor.video.shape[2]
    video_unit = height * width * LATENT_DTYPE_ITEM_SIZES[left_descriptor.video.dtype]
    if plan.left_video_steps > left_video_steps or plan.right_video_steps > right_video_steps:
        raise TwoEndedBridgeAdapterError("context_exceeds_domain:video")
    composed_video = _compose_axis_blocks(
        left_video,
        right_video,
        blocks=batch * channels,
        left_steps=left_video_steps,
        right_steps=right_video_steps,
        take_left=plan.left_video_steps,
        middle_steps=plan.middle_video_steps,
        take_right=plan.right_video_steps,
        unit=video_unit,
    )

    _, audio_channels, planes, left_audio_steps = left_descriptor.audio.shape
    right_audio_steps = right_descriptor.audio.shape[3]
    audio_unit = LATENT_DTYPE_ITEM_SIZES[left_descriptor.audio.dtype]
    if plan.left_audio_steps > left_audio_steps or plan.right_audio_steps > right_audio_steps:
        raise TwoEndedBridgeAdapterError("context_exceeds_domain:audio")
    composed_audio = _compose_axis_blocks(
        left_audio,
        right_audio,
        blocks=batch * audio_channels * planes,
        left_steps=left_audio_steps,
        right_steps=right_audio_steps,
        take_left=plan.left_audio_steps,
        middle_steps=plan.middle_audio_steps,
        take_right=plan.right_audio_steps,
        unit=audio_unit,
    )
    return composed_video, composed_audio


def compose_bridge_latent(
    *,
    plan: TwoEndedBridgePlan,
    left_descriptor: JointAVLatentDescriptor,
    right_descriptor: JointAVLatentDescriptor,
    left_video_bytes: bytes,
    left_audio_bytes: bytes,
    right_video_bytes: bytes,
    right_audio_bytes: bytes,
    tensor_factory: DomainTensorFactory | None = None,
    pair_factory: PairFactory | None = None,
    test_only_seams: bool = False,
) -> dict[str, object]:
    """Build the host latent dict for the bridge run from the two stored boundaries."""

    if (tensor_factory is not None or pair_factory is not None) and not test_only_seams:
        raise TwoEndedBridgeAdapterError("test_seams_disabled")
    composed_video, composed_audio = compose_bridge_domain_payloads(
        plan=plan,
        left_descriptor=left_descriptor,
        right_descriptor=right_descriptor,
        left_video_bytes=left_video_bytes,
        left_audio_bytes=left_audio_bytes,
        right_video_bytes=right_video_bytes,
        right_audio_bytes=right_audio_bytes,
    )
    video_shape, audio_shape = bridge_domain_shapes(plan, left_descriptor)
    make_tensor = _default_domain_tensor_factory if tensor_factory is None else tensor_factory
    video = make_tensor(composed_video, video_shape, left_descriptor.video.dtype)
    audio = make_tensor(composed_audio, audio_shape, left_descriptor.audio.dtype)
    make_pair = _default_pair_factory if pair_factory is None else pair_factory
    return {"samples": make_pair(video, audio)}


def attach_bridge_interval_mask(
    latent: Mapping[str, object],
    *,
    mask_plan: BridgeMaskPlan,
    plan: TwoEndedBridgePlan,
    left_descriptor: JointAVLatentDescriptor,
    mask_tensor_factory: MaskTensorFactory | None = None,
    pair_factory: PairFactory | None = None,
    test_only_seams: bool = False,
) -> dict[str, object]:
    """Attach the plan's interval mask to a copy of the latent; exact binding gates all work."""

    if type(mask_plan) is not BridgeMaskPlan:
        raise TwoEndedBridgeAdapterError("mask_plan_type")
    if type(plan) is not TwoEndedBridgePlan:
        raise TwoEndedBridgeAdapterError("plan_type")
    if type(left_descriptor) is not JointAVLatentDescriptor:
        raise TwoEndedBridgeAdapterError("descriptor_type")
    if (mask_tensor_factory is not None or pair_factory is not None) and not test_only_seams:
        raise TwoEndedBridgeAdapterError("test_seams_disabled")
    if not isinstance(latent, Mapping):
        raise TwoEndedBridgeAdapterError("latent_mapping_type")
    if latent.get("samples") is None:
        raise TwoEndedBridgeAdapterError("latent_samples_missing")
    if "noise_mask" in latent:
        raise TwoEndedBridgeAdapterError("latent_already_masked")
    if mask_plan.bridge_plan_fingerprint != plan.fingerprint:
        raise TwoEndedBridgeAdapterError("mask_plan_binding_mismatch")
    video_shape, audio_shape = bridge_domain_shapes(plan, left_descriptor)
    if mask_plan.video.length != video_shape[2]:
        raise TwoEndedBridgeAdapterError("mask_extent_mismatch:video")
    if mask_plan.audio.length != audio_shape[3]:
        raise TwoEndedBridgeAdapterError("mask_extent_mismatch:audio")

    make_mask = _default_mask_tensor_factory if mask_tensor_factory is None else mask_tensor_factory
    video_mask = make_mask(
        _interval_values(
            mask_plan.video.length, mask_plan.video.generate_from, mask_plan.video.generate_to
        ),
        (1, 1, video_shape[2], video_shape[3], video_shape[4]),
    )
    audio_mask = make_mask(
        _interval_values(
            mask_plan.audio.length, mask_plan.audio.generate_from, mask_plan.audio.generate_to
        ),
        (1, 1, audio_shape[2], audio_shape[3]),
    )
    make_pair = _default_pair_factory if pair_factory is None else pair_factory
    masked = dict(latent)
    masked["noise_mask"] = make_pair(video_mask, audio_mask)
    return masked


__all__ = [
    "DomainTensorFactory",
    "MaskTensorFactory",
    "PairFactory",
    "TwoEndedBridgeAdapterError",
    "attach_bridge_interval_mask",
    "bridge_domain_shapes",
    "compose_bridge_domain_payloads",
    "compose_bridge_latent",
]
