"""Pure M20-09 explicit seam and crossfade policy with exact accounting.

This module owns the typed seam profile for the M17-11 aggregate join: direct
join (the compatibility baseline) and bounded audiovisual crossfade.  It holds
no filesystem locator, media process, host API or frontend wire.  Every overlap
is validated on a derived exact grid and accounted in whole target frames and
samples before any execution; fractional or unsupported accounting refuses at
construction time instead of rounding.

Master-audio ownership is bound at declaration level (the M20-07 D3 pattern):
the per-seam master source identifiers are caller-asserted bookkeeping recorded
into the seam plan and receipt.  They are not fingerprint-verified against the
audio content — the M17-11 media layer carries no cross-layer audio provenance
channel — and admission therefore checks declared identity consistency, not
acoustic identity.

Grid derivation (guarded below, never re-typed): the target profile runs at 30
frames per second; the native subject runs at ``length.FPS`` (24) frames per
second with audio exact only on ``EXACT_AUDIO_PERIOD_FRAMES`` (3) native-frame
periods.  An overlap of K target frames therefore stays whole in both native
domains only when ``K * 24/30`` is an integer divisible by 3, giving the
15-target-frame seam grid.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from math import gcd
from typing import cast

from . import length
from .av_reconstruction import (
    AVOutputKind,
    AVPublicationState,
    AVRational,
    AVReceiptOutput,
    AVReconstructionApproval,
    AVReconstructionError,
    AVReconstructionPlan,
    qualified_av_target_profile,
    qualified_ffmpeg_capability,
)
from .canonical import canonical_bytes, canonical_fingerprint
from .temporal_profile import EXACT_AUDIO_PERIOD_FRAMES

AV_SEAM_PLAN_SCHEMA = "h3.context.av_seam_plan.v1"
AV_SEAM_RECEIPT_SCHEMA = "h3.context.av_seam_reconstruction_receipt.v1"
AV_SEAM_CAPABILITY_SCHEMA = "h3.context.av_seam_capability.v1"
MAX_AV_SEAM_RECEIPT_BYTES = 65_536

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AVSeamPolicyError(ValueError):
    """Raised when seam truth cannot remain exact and fail-closed."""


class AVSeamOperation(str, Enum):
    DIRECT_JOIN = "direct_join"
    CROSSFADE = "crossfade"


class AVSeamAudioPolicy(str, Enum):
    BLEND = "blend"
    PREDECESSOR = "predecessor"
    SUCCESSOR = "successor"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise AVSeamPolicyError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise AVSeamPolicyError(f"sha256_fingerprint:{field}")
    return value


def _positive(value: object, field: str, maximum: int = 9_999_999_999_999) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise AVSeamPolicyError(f"positive_integer:{field}")
    return value


def _nonnegative(value: object, field: str, maximum: int = 9_999_999_999_999) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise AVSeamPolicyError(f"nonnegative_integer:{field}")
    return value


def _derive_target_frames_per_second() -> int:
    rate = qualified_av_target_profile().frame_rate
    if rate.denominator != 1 or rate.numerator <= 0:
        raise AVSeamPolicyError("seam_target_rate_not_integral")
    return rate.numerator


TARGET_FRAMES_PER_SECOND = _derive_target_frames_per_second()


def _derive_samples_per_target_frame() -> int:
    sample_rate = qualified_av_target_profile().sample_rate
    if sample_rate % TARGET_FRAMES_PER_SECOND != 0:
        raise AVSeamPolicyError("seam_sample_rate_not_integral")
    return sample_rate // TARGET_FRAMES_PER_SECOND


SAMPLES_PER_TARGET_FRAME = _derive_samples_per_target_frame()


def _derive_overlap_grid_target_frames() -> int:
    """Smallest K with K * native_fps/target_fps a whole native-frame count
    divisible by the exact-audio period."""

    ratio = Fraction(length.FPS, TARGET_FRAMES_PER_SECOND)
    base = ratio.denominator
    native_per_base = ratio.numerator
    period_factor = EXACT_AUDIO_PERIOD_FRAMES // gcd(native_per_base, EXACT_AUDIO_PERIOD_FRAMES)
    grid = base * period_factor
    native = grid * ratio
    if native.denominator != 1 or native.numerator % EXACT_AUDIO_PERIOD_FRAMES != 0:
        raise AVSeamPolicyError("seam_overlap_grid_derivation")
    return grid


SEAM_OVERLAP_GRID_TARGET_FRAMES = _derive_overlap_grid_target_frames()
MIN_SEAM_OVERLAP_TARGET_FRAMES = SEAM_OVERLAP_GRID_TARGET_FRAMES
MAX_SEAM_OVERLAP_TARGET_FRAMES = 10 * SEAM_OVERLAP_GRID_TARGET_FRAMES
MAX_SEAM_SEGMENTS = 64
MAX_SEAM_BOUNDARIES = MAX_SEAM_SEGMENTS - 1

_SEAM_FILTER_REQUIREMENTS = (
    "acrossfade",
    "asetpts",
    "asplit",
    "atrim",
    "concat",
    "setpts",
    "settb",
    "split",
    "trim",
    "xfade",
)


@dataclass(frozen=True, slots=True)
class AVSeamCapability:
    """Declared seam capability bound to the exact base media capability."""

    base_capability_fingerprint: str
    operations: tuple[AVSeamOperation, ...]
    audio_policies: tuple[AVSeamAudioPolicy, ...]
    overlap_grid_target_frames: int
    min_overlap_target_frames: int
    max_overlap_target_frames: int
    samples_per_target_frame: int
    video_transition: str
    audio_blend_curve: str
    required_filters: tuple[str, ...]

    def __post_init__(self) -> None:
        _fingerprint(self.base_capability_fingerprint, "seam_capability.base")
        if (
            type(self.operations) is not tuple
            or not self.operations
            or not all(type(item) is AVSeamOperation for item in self.operations)
            or len(set(self.operations)) != len(self.operations)
        ):
            raise AVSeamPolicyError("seam_capability.operations")
        if (
            type(self.audio_policies) is not tuple
            or not self.audio_policies
            or not all(type(item) is AVSeamAudioPolicy for item in self.audio_policies)
            or len(set(self.audio_policies)) != len(self.audio_policies)
        ):
            raise AVSeamPolicyError("seam_capability.audio_policies")
        grid = _positive(self.overlap_grid_target_frames, "seam_capability.grid", 3600)
        minimum = _positive(self.min_overlap_target_frames, "seam_capability.min", 3600)
        maximum = _positive(self.max_overlap_target_frames, "seam_capability.max", 36_000)
        if minimum % grid != 0 or maximum % grid != 0 or minimum > maximum:
            raise AVSeamPolicyError("seam_capability.bounds")
        _positive(self.samples_per_target_frame, "seam_capability.samples_per_frame", 96_000)
        _identifier(self.video_transition, "seam_capability.video_transition")
        _identifier(self.audio_blend_curve, "seam_capability.audio_blend_curve")
        if (
            type(self.required_filters) is not tuple
            or not self.required_filters
            or len(self.required_filters) > 64
        ):
            raise AVSeamPolicyError("seam_capability.required_filters")
        for item in self.required_filters:
            _identifier(item, "seam_capability.required_filter")
        if tuple(sorted(set(self.required_filters))) != self.required_filters:
            raise AVSeamPolicyError("seam_capability.required_filters_canonical")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": AV_SEAM_CAPABILITY_SCHEMA,
            "base_capability_fingerprint": self.base_capability_fingerprint,
            "operations": [item.value for item in self.operations],
            "audio_policies": [item.value for item in self.audio_policies],
            "overlap_grid_target_frames": self.overlap_grid_target_frames,
            "min_overlap_target_frames": self.min_overlap_target_frames,
            "max_overlap_target_frames": self.max_overlap_target_frames,
            "samples_per_target_frame": self.samples_per_target_frame,
            "video_transition": self.video_transition,
            "audio_blend_curve": self.audio_blend_curve,
            "required_filters": list(self.required_filters),
        }


def qualified_seam_capability() -> AVSeamCapability:
    """Return the declared seam capability over the exact Gate-0 tool identity.

    Construction refuses when the base capability does not declare every filter
    the seam compiler emits, so a silent capability regression fails loudly.
    """

    base = qualified_ffmpeg_capability()
    missing = tuple(item for item in _SEAM_FILTER_REQUIREMENTS if item not in base.filters)
    if missing:
        raise AVSeamPolicyError(f"seam_capability_filters_unqualified:{missing[0]}")
    return AVSeamCapability(
        base_capability_fingerprint=base.fingerprint,
        operations=(AVSeamOperation.DIRECT_JOIN, AVSeamOperation.CROSSFADE),
        audio_policies=(
            AVSeamAudioPolicy.BLEND,
            AVSeamAudioPolicy.PREDECESSOR,
            AVSeamAudioPolicy.SUCCESSOR,
        ),
        overlap_grid_target_frames=SEAM_OVERLAP_GRID_TARGET_FRAMES,
        min_overlap_target_frames=MIN_SEAM_OVERLAP_TARGET_FRAMES,
        max_overlap_target_frames=MAX_SEAM_OVERLAP_TARGET_FRAMES,
        samples_per_target_frame=SAMPLES_PER_TARGET_FRAME,
        video_transition="fade",
        audio_blend_curve="tri",
        required_filters=_SEAM_FILTER_REQUIREMENTS,
    )


@dataclass(frozen=True, slots=True)
class AVSeamSpec:
    """Caller intent for exactly one plan boundary."""

    operation: AVSeamOperation
    overlap_frames: int = 0
    audio_policy: AVSeamAudioPolicy | None = None
    predecessor_master_source_id: str | None = None
    successor_master_source_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.operation) is not AVSeamOperation:
            raise AVSeamPolicyError("seam_spec.operation")
        if self.operation is AVSeamOperation.DIRECT_JOIN:
            if (
                self.overlap_frames != 0
                or self.audio_policy is not None
                or self.predecessor_master_source_id is not None
                or self.successor_master_source_id is not None
            ):
                raise AVSeamPolicyError("seam_spec_direct_join_shape")
            return
        _positive(self.overlap_frames, "seam_spec.overlap_frames", 36_000)
        if type(self.audio_policy) is not AVSeamAudioPolicy:
            raise AVSeamPolicyError("seam_spec_audio_policy_missing")
        _identifier(self.predecessor_master_source_id, "seam_spec.predecessor_master")
        _identifier(self.successor_master_source_id, "seam_spec.successor_master")


@dataclass(frozen=True, slots=True)
class AVSeamResult:
    """Derived decision for one boundary; carried by the plan and the receipt."""

    boundary_index: int
    predecessor_segment_id: str
    successor_segment_id: str
    operation: AVSeamOperation
    overlap_frames: int
    overlap_samples: int
    audio_policy: AVSeamAudioPolicy | None
    predecessor_master_source_id: str | None
    successor_master_source_id: str | None

    def __post_init__(self) -> None:
        _nonnegative(self.boundary_index, "seam.boundary_index", MAX_SEAM_BOUNDARIES - 1)
        _identifier(self.predecessor_segment_id, "seam.predecessor_segment_id")
        _identifier(self.successor_segment_id, "seam.successor_segment_id")
        if self.predecessor_segment_id == self.successor_segment_id:
            raise AVSeamPolicyError("seam_self_reference")
        if type(self.operation) is not AVSeamOperation:
            raise AVSeamPolicyError("seam.operation")
        if self.operation is AVSeamOperation.DIRECT_JOIN:
            if (
                self.overlap_frames != 0
                or self.overlap_samples != 0
                or self.audio_policy is not None
                or self.predecessor_master_source_id is not None
                or self.successor_master_source_id is not None
            ):
                raise AVSeamPolicyError("seam_direct_join_shape")
            return
        _positive(self.overlap_frames, "seam.overlap_frames", 36_000)
        if self.overlap_samples != self.overlap_frames * SAMPLES_PER_TARGET_FRAME:
            raise AVSeamPolicyError("seam_overlap_sample_mismatch")
        if type(self.audio_policy) is not AVSeamAudioPolicy:
            raise AVSeamPolicyError("seam_audio_policy_missing")
        _identifier(self.predecessor_master_source_id, "seam.predecessor_master")
        _identifier(self.successor_master_source_id, "seam.successor_master")
        if (
            self.audio_policy is AVSeamAudioPolicy.BLEND
            and self.predecessor_master_source_id != self.successor_master_source_id
        ):
            raise AVSeamPolicyError("master_audio_conflict")

    @property
    def decision_fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "boundary_index": self.boundary_index,
            "predecessor_segment_id": self.predecessor_segment_id,
            "successor_segment_id": self.successor_segment_id,
            "operation": self.operation.value,
            "overlap_frames": self.overlap_frames,
            "overlap_samples": self.overlap_samples,
            "audio_policy": None if self.audio_policy is None else self.audio_policy.value,
            "predecessor_master_source_id": self.predecessor_master_source_id,
            "successor_master_source_id": self.successor_master_source_id,
        }


def _decode_seam_result(value: object, field: str) -> AVSeamResult:
    if type(value) is not dict or set(value) != {
        "boundary_index",
        "predecessor_segment_id",
        "successor_segment_id",
        "operation",
        "overlap_frames",
        "overlap_samples",
        "audio_policy",
        "predecessor_master_source_id",
        "successor_master_source_id",
    }:
        raise AVSeamPolicyError(f"seam_members:{field}")
    try:
        operation = AVSeamOperation(value["operation"])
    except (TypeError, ValueError):
        raise AVSeamPolicyError(f"seam_operation:{field}") from None
    raw_policy = value["audio_policy"]
    if raw_policy is None:
        policy: AVSeamAudioPolicy | None = None
    else:
        try:
            policy = AVSeamAudioPolicy(raw_policy)
        except (TypeError, ValueError):
            raise AVSeamPolicyError(f"seam_audio_policy:{field}") from None
    return AVSeamResult(
        boundary_index=cast(int, value["boundary_index"]),
        predecessor_segment_id=cast(str, value["predecessor_segment_id"]),
        successor_segment_id=cast(str, value["successor_segment_id"]),
        operation=operation,
        overlap_frames=cast(int, value["overlap_frames"]),
        overlap_samples=cast(int, value["overlap_samples"]),
        audio_policy=policy,
        predecessor_master_source_id=cast("str | None", value["predecessor_master_source_id"]),
        successor_master_source_id=cast("str | None", value["successor_master_source_id"]),
    )


@dataclass(frozen=True, slots=True)
class AVSeamPlan:
    """Process-local seam authority bound to one approved base plan."""

    base_plan_fingerprint: str
    base_approval_fingerprint: str
    capability_fingerprint: str
    seam_capability_fingerprint: str
    segment_ids: tuple[str, ...]
    segment_emitted_frames: tuple[int, ...]
    segment_emitted_samples: tuple[int, ...]
    seams: tuple[AVSeamResult, ...]
    total_output_frames: int
    total_output_samples: int
    output_duration: AVRational
    planned_at_ms: int
    plan_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _fingerprint(self.base_plan_fingerprint, "seam_plan.base_plan")
        _fingerprint(self.base_approval_fingerprint, "seam_plan.base_approval")
        _fingerprint(self.capability_fingerprint, "seam_plan.capability")
        _fingerprint(self.seam_capability_fingerprint, "seam_plan.seam_capability")
        seam_capability = qualified_seam_capability()
        # SECURITY: pin the exact qualified capabilities; a self-consistent widened
        # declaration must not admit itself through direct construction or replace().
        if (
            self.capability_fingerprint != qualified_ffmpeg_capability().fingerprint
            or self.seam_capability_fingerprint != seam_capability.fingerprint
        ):
            raise AVSeamPolicyError("seam_capability_changed")
        if (
            type(self.segment_ids) is not tuple
            or not 2 <= len(self.segment_ids) <= MAX_SEAM_SEGMENTS
        ):
            raise AVSeamPolicyError("seam_plan.segments")
        for item in self.segment_ids:
            _identifier(item, "seam_plan.segment_id")
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise AVSeamPolicyError("seam_plan.segment_duplicate")
        for values, field in (
            (self.segment_emitted_frames, "seam_plan.emitted_frames"),
            (self.segment_emitted_samples, "seam_plan.emitted_samples"),
        ):
            if type(values) is not tuple or len(values) != len(self.segment_ids):
                raise AVSeamPolicyError(field)
            for value in values:
                _positive(value, field, 1_000_000_000)
        for frames, samples in zip(
            self.segment_emitted_frames, self.segment_emitted_samples, strict=True
        ):
            if samples != frames * SAMPLES_PER_TARGET_FRAME:
                raise AVSeamPolicyError("seam_segment_rate_mismatch")
        if (
            type(self.seams) is not tuple
            or len(self.seams) != len(self.segment_ids) - 1
            or not all(type(item) is AVSeamResult for item in self.seams)
        ):
            raise AVSeamPolicyError("seam_plan.seams")
        for index, seam in enumerate(self.seams):
            if (
                seam.boundary_index != index
                or seam.predecessor_segment_id != self.segment_ids[index]
                or seam.successor_segment_id != self.segment_ids[index + 1]
            ):
                raise AVSeamPolicyError("seam_boundary_identity_mismatch")
            if seam.operation is AVSeamOperation.CROSSFADE:
                overlap = seam.overlap_frames
                if overlap % seam_capability.overlap_grid_target_frames != 0:
                    raise AVSeamPolicyError("seam_overlap_off_grid")
                if not (
                    seam_capability.min_overlap_target_frames
                    <= overlap
                    <= seam_capability.max_overlap_target_frames
                ):
                    raise AVSeamPolicyError("seam_overlap_out_of_bounds")
                if (
                    overlap >= self.segment_emitted_frames[index]
                    or overlap >= self.segment_emitted_frames[index + 1]
                ):
                    raise AVSeamPolicyError("seam_overlap_exceeds_segment")
        for index in range(1, len(self.segment_ids) - 1):
            consumed = self.seams[index - 1].overlap_frames + self.seams[index].overlap_frames
            if consumed >= self.segment_emitted_frames[index]:
                raise AVSeamPolicyError("seam_overlap_regions_intersect")
        expected_frames = sum(self.segment_emitted_frames) - sum(
            item.overlap_frames for item in self.seams
        )
        expected_samples = sum(self.segment_emitted_samples) - sum(
            item.overlap_samples for item in self.seams
        )
        if (
            self.total_output_frames != expected_frames
            or self.total_output_samples != expected_samples
            or expected_frames < 1
        ):
            raise AVSeamPolicyError("seam_accounting_mismatch")
        if self.total_output_samples != self.total_output_frames * SAMPLES_PER_TARGET_FRAME:
            raise AVSeamPolicyError("seam_accounting_mismatch")
        duration = Fraction(self.total_output_frames, TARGET_FRAMES_PER_SECOND)
        if (
            type(self.output_duration) is not AVRational
            or self.output_duration.fraction != duration
        ):
            raise AVSeamPolicyError("seam_duration_mismatch")
        _positive(self.planned_at_ms, "seam_plan.planned_at_ms")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.plan_fingerprint is None:
            object.__setattr__(self, "plan_fingerprint", expected)
        elif self.plan_fingerprint != expected:
            raise AVSeamPolicyError("seam_plan_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.plan_fingerprint is None:  # pragma: no cover
            raise AVSeamPolicyError("seam_plan_fingerprint_uninitialized")
        return self.plan_fingerprint

    @property
    def crossfade_count(self) -> int:
        return sum(item.operation is AVSeamOperation.CROSSFADE for item in self.seams)

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": AV_SEAM_PLAN_SCHEMA,
            "base_plan_fingerprint": self.base_plan_fingerprint,
            "base_approval_fingerprint": self.base_approval_fingerprint,
            "capability_fingerprint": self.capability_fingerprint,
            "seam_capability_fingerprint": self.seam_capability_fingerprint,
            "segment_ids": list(self.segment_ids),
            "segment_emitted_frames": list(self.segment_emitted_frames),
            "segment_emitted_samples": list(self.segment_emitted_samples),
            "seams": [item.to_wire() for item in self.seams],
            "total_output_frames": self.total_output_frames,
            "total_output_samples": self.total_output_samples,
            "output_duration": self.output_duration.to_wire(),
            "planned_at_ms": self.planned_at_ms,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["plan_fingerprint"] = self.fingerprint
        return value


def _assert_base_identity(
    base_plan: AVReconstructionPlan,
    base_approval: AVReconstructionApproval,
) -> None:
    approval_wire = {
        "plan_fingerprint": base_approval.plan_fingerprint,
        "decision_fingerprints": list(base_approval.decision_fingerprints),
        "approved_at_ms": base_approval.approved_at_ms,
        "expires_at_ms": base_approval.expires_at_ms,
    }
    if canonical_fingerprint(approval_wire) != base_approval.fingerprint:
        raise AVSeamPolicyError("seam_base_identity")
    try:
        # Factory provenance and plan/approval binding are re-verified here; the
        # freshness window is checked again at execution time by the pipeline.
        base_approval.assert_executable(base_plan, now_ms=base_approval.approved_at_ms)
    except AVReconstructionError as exc:
        raise AVSeamPolicyError("seam_base_identity") from exc


def build_av_seam_plan(
    *,
    base_plan: AVReconstructionPlan,
    base_approval: AVReconstructionApproval,
    seam_specs: tuple[AVSeamSpec, ...],
    planned_at_ms: int,
) -> AVSeamPlan:
    """Join one approved base plan with caller seam intent into a seam plan."""

    if type(base_plan) is not AVReconstructionPlan:
        raise AVSeamPolicyError("seam_base_plan_type")
    if type(base_approval) is not AVReconstructionApproval:
        raise AVSeamPolicyError("seam_base_approval_type")
    if type(seam_specs) is not tuple or not all(type(item) is AVSeamSpec for item in seam_specs):
        raise AVSeamPolicyError("seam_specs_type")
    _assert_base_identity(base_plan, base_approval)
    if len(base_plan.segments) < 2:
        raise AVSeamPolicyError("seam_no_boundary")
    if len(seam_specs) != len(base_plan.segments) - 1:
        raise AVSeamPolicyError("seam_spec_count_mismatch")
    _positive(planned_at_ms, "seam_plan.planned_at_ms")

    seam_capability = qualified_seam_capability()
    segment_ids = tuple(item.segment_id for item in base_plan.segments)
    emitted_frames = tuple(item.accounting.emitted_frames for item in base_plan.segments)
    emitted_samples = tuple(item.accounting.emitted_samples for item in base_plan.segments)
    seams: list[AVSeamResult] = []
    for index, spec in enumerate(seam_specs):
        if spec.operation is AVSeamOperation.DIRECT_JOIN:
            seams.append(
                AVSeamResult(
                    boundary_index=index,
                    predecessor_segment_id=segment_ids[index],
                    successor_segment_id=segment_ids[index + 1],
                    operation=AVSeamOperation.DIRECT_JOIN,
                    overlap_frames=0,
                    overlap_samples=0,
                    audio_policy=None,
                    predecessor_master_source_id=None,
                    successor_master_source_id=None,
                )
            )
            continue
        overlap = spec.overlap_frames
        if overlap % seam_capability.overlap_grid_target_frames != 0:
            raise AVSeamPolicyError("seam_overlap_off_grid")
        if not (
            seam_capability.min_overlap_target_frames
            <= overlap
            <= seam_capability.max_overlap_target_frames
        ):
            raise AVSeamPolicyError("seam_overlap_out_of_bounds")
        if overlap >= emitted_frames[index] or overlap >= emitted_frames[index + 1]:
            raise AVSeamPolicyError("seam_overlap_exceeds_segment")
        if (
            spec.audio_policy is AVSeamAudioPolicy.BLEND
            and spec.predecessor_master_source_id != spec.successor_master_source_id
        ):
            raise AVSeamPolicyError("master_audio_conflict")
        seams.append(
            AVSeamResult(
                boundary_index=index,
                predecessor_segment_id=segment_ids[index],
                successor_segment_id=segment_ids[index + 1],
                operation=AVSeamOperation.CROSSFADE,
                overlap_frames=overlap,
                overlap_samples=overlap * SAMPLES_PER_TARGET_FRAME,
                audio_policy=spec.audio_policy,
                predecessor_master_source_id=spec.predecessor_master_source_id,
                successor_master_source_id=spec.successor_master_source_id,
            )
        )
    for index in range(1, len(segment_ids) - 1):
        consumed = seams[index - 1].overlap_frames + seams[index].overlap_frames
        if consumed >= emitted_frames[index]:
            raise AVSeamPolicyError("seam_overlap_regions_intersect")
    total_frames = sum(emitted_frames) - sum(item.overlap_frames for item in seams)
    total_samples = sum(emitted_samples) - sum(item.overlap_samples for item in seams)
    if total_frames < 1:
        raise AVSeamPolicyError("seam_accounting_mismatch")
    duration = Fraction(total_frames, TARGET_FRAMES_PER_SECOND)
    return AVSeamPlan(
        base_plan_fingerprint=base_plan.fingerprint,
        base_approval_fingerprint=base_approval.fingerprint,
        capability_fingerprint=base_plan.capability.fingerprint,
        seam_capability_fingerprint=seam_capability.fingerprint,
        segment_ids=segment_ids,
        segment_emitted_frames=emitted_frames,
        segment_emitted_samples=emitted_samples,
        seams=tuple(seams),
        total_output_frames=total_frames,
        total_output_samples=total_samples,
        output_duration=AVRational(duration.numerator, duration.denominator),
        planned_at_ms=planned_at_ms,
    )


@dataclass(frozen=True, slots=True)
class AVSeamApproval:
    """Explicit approval of one exact seam plan, bounded by the base approval."""

    seam_plan_fingerprint: str
    seam_decision_fingerprints: tuple[str, ...]
    approved_at_ms: int
    expires_at_ms: int
    approval_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _fingerprint(self.seam_plan_fingerprint, "seam_approval.plan")
        if (
            type(self.seam_decision_fingerprints) is not tuple
            or not self.seam_decision_fingerprints
            or len(self.seam_decision_fingerprints) > MAX_SEAM_BOUNDARIES
        ):
            raise AVSeamPolicyError("seam_approval.decisions")
        for item in self.seam_decision_fingerprints:
            _fingerprint(item, "seam_approval.decision")
        _positive(self.approved_at_ms, "seam_approval.approved_at_ms")
        _positive(self.expires_at_ms, "seam_approval.expires_at_ms")
        if self.expires_at_ms <= self.approved_at_ms:
            raise AVSeamPolicyError("seam_approval_expiry")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.approval_fingerprint is None:
            object.__setattr__(self, "approval_fingerprint", expected)
        elif self.approval_fingerprint != expected:
            raise AVSeamPolicyError("seam_approval_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.approval_fingerprint is None:  # pragma: no cover
            raise AVSeamPolicyError("seam_approval_fingerprint_uninitialized")
        return self.approval_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "seam_plan_fingerprint": self.seam_plan_fingerprint,
            "seam_decision_fingerprints": list(self.seam_decision_fingerprints),
            "approved_at_ms": self.approved_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }

    def assert_executable(
        self,
        seam_plan: AVSeamPlan,
        *,
        base_plan: AVReconstructionPlan,
        base_approval: AVReconstructionApproval,
        now_ms: int,
    ) -> None:
        if type(seam_plan) is not AVSeamPlan or seam_plan.fingerprint != (
            self.seam_plan_fingerprint
        ):
            raise AVSeamPolicyError("seam_approval_plan_mismatch")
        if self.seam_decision_fingerprints != tuple(
            item.decision_fingerprint for item in seam_plan.seams
        ):
            raise AVSeamPolicyError("seam_approval_decision_mismatch")
        _assert_base_identity(base_plan, base_approval)
        if (
            seam_plan.base_plan_fingerprint != base_plan.fingerprint
            or seam_plan.base_approval_fingerprint != base_approval.fingerprint
        ):
            raise AVSeamPolicyError("seam_base_binding_mismatch")
        if (
            type(now_ms) is not int
            or now_ms < self.approved_at_ms
            or (now_ms >= self.expires_at_ms)
        ):
            raise AVSeamPolicyError("seam_approval_stale")
        if self.expires_at_ms > base_approval.expires_at_ms:
            raise AVSeamPolicyError("seam_approval_stale")


def approve_av_seam_plan(
    seam_plan: AVSeamPlan,
    *,
    base_approval: AVReconstructionApproval,
    approved_at_ms: int,
    expires_at_ms: int,
) -> AVSeamApproval:
    if type(seam_plan) is not AVSeamPlan:
        raise AVSeamPolicyError("seam_plan_type")
    if type(base_approval) is not AVReconstructionApproval:
        raise AVSeamPolicyError("seam_base_approval_type")
    if seam_plan.base_approval_fingerprint != base_approval.fingerprint:
        raise AVSeamPolicyError("seam_base_binding_mismatch")
    if approved_at_ms < seam_plan.planned_at_ms or expires_at_ms > base_approval.expires_at_ms:
        raise AVSeamPolicyError("seam_approval_stale")
    return AVSeamApproval(
        seam_plan_fingerprint=seam_plan.fingerprint,
        seam_decision_fingerprints=tuple(item.decision_fingerprint for item in seam_plan.seams),
        approved_at_ms=approved_at_ms,
        expires_at_ms=expires_at_ms,
    )


@dataclass(frozen=True, slots=True)
class AVSeamReconstructionReceipt:
    """The sole portable contract of one seamed reconstruction."""

    transaction_id: str
    plan_fingerprint: str
    approval_fingerprint: str
    base_plan_fingerprint: str
    base_approval_fingerprint: str
    capability_fingerprint: str
    seam_capability_fingerprint: str
    execution_fingerprint: str
    seams: tuple[AVSeamResult, ...]
    total_output_frames: int
    total_output_samples: int
    outputs: tuple[AVReceiptOutput, ...]
    publication_state: AVPublicationState
    completed_at_ms: int
    receipt_fingerprint: str | None = None
    schema: str = AV_SEAM_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != AV_SEAM_RECEIPT_SCHEMA:
            raise AVSeamPolicyError("unsupported_seam_receipt_schema")
        _identifier(self.transaction_id, "seam_receipt.transaction_id")
        for value, field in (
            (self.plan_fingerprint, "seam_receipt.plan"),
            (self.approval_fingerprint, "seam_receipt.approval"),
            (self.base_plan_fingerprint, "seam_receipt.base_plan"),
            (self.base_approval_fingerprint, "seam_receipt.base_approval"),
            (self.capability_fingerprint, "seam_receipt.capability"),
            (self.seam_capability_fingerprint, "seam_receipt.seam_capability"),
            (self.execution_fingerprint, "seam_receipt.execution"),
        ):
            _fingerprint(value, field)
        if (
            type(self.seams) is not tuple
            or not self.seams
            or len(self.seams) > MAX_SEAM_BOUNDARIES
            or not all(type(item) is AVSeamResult for item in self.seams)
        ):
            raise AVSeamPolicyError("seam_receipt.seams")
        for index, seam in enumerate(self.seams):
            if seam.boundary_index != index:
                raise AVSeamPolicyError("seam_receipt.seam_order")
        _positive(self.total_output_frames, "seam_receipt.total_frames", 10_000_000)
        _positive(self.total_output_samples, "seam_receipt.total_samples", 1_000_000_000)
        if self.total_output_samples != self.total_output_frames * SAMPLES_PER_TARGET_FRAME:
            raise AVSeamPolicyError("seam_receipt_accounting")
        if (
            type(self.outputs) is not tuple
            or len(self.outputs) != 1
            or type(self.outputs[0]) is not AVReceiptOutput
            or self.outputs[0].kind is not AVOutputKind.RECONSTRUCTION_FULL
        ):
            raise AVSeamPolicyError("seam_receipt.outputs")
        output = self.outputs[0]
        duration = Fraction(self.total_output_frames, TARGET_FRAMES_PER_SECOND)
        if (
            output.video_frame_count != self.total_output_frames
            or output.audio_sample_count != self.total_output_samples
            or output.duration.fraction != duration
        ):
            raise AVSeamPolicyError("seam_receipt_accounting")
        if type(self.publication_state) is not AVPublicationState:
            raise AVSeamPolicyError("seam_receipt.publication_state")
        _positive(self.completed_at_ms, "seam_receipt.completed_at_ms")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise AVSeamPolicyError("seam_receipt_fingerprint_mismatch")
        if len(self.to_wire_bytes()) > MAX_AV_SEAM_RECEIPT_BYTES:
            raise AVSeamPolicyError("seam_receipt_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover
            raise AVSeamPolicyError("seam_receipt_fingerprint_uninitialized")
        return self.receipt_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "plan_fingerprint": self.plan_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "base_plan_fingerprint": self.base_plan_fingerprint,
            "base_approval_fingerprint": self.base_approval_fingerprint,
            "capability_fingerprint": self.capability_fingerprint,
            "seam_capability_fingerprint": self.seam_capability_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "seams": [item.to_wire() for item in self.seams],
            "total_output_frames": self.total_output_frames,
            "total_output_samples": self.total_output_samples,
            "outputs": [item.to_wire() for item in self.outputs],
            "publication_state": self.publication_state.value,
            "completed_at_ms": self.completed_at_ms,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["receipt_fingerprint"] = self.fingerprint
        return value

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "publication_state": self.publication_state.value,
            "completed_at_ms": self.completed_at_ms,
            "total_output_frames": self.total_output_frames,
            "total_output_samples": self.total_output_samples,
            "seams": [item.to_wire() for item in self.seams],
            "outputs": [
                {
                    "handle": item.handle,
                    "kind": item.kind.value,
                    "byte_length": item.byte_length,
                    "video_frame_count": item.video_frame_count,
                    "audio_sample_count": item.audio_sample_count,
                    "duration": item.duration.to_wire(),
                }
                for item in self.outputs
            ],
        }


_SEAM_RECEIPT_FIELDS = {
    "schema",
    "transaction_id",
    "plan_fingerprint",
    "approval_fingerprint",
    "base_plan_fingerprint",
    "base_approval_fingerprint",
    "capability_fingerprint",
    "seam_capability_fingerprint",
    "execution_fingerprint",
    "seams",
    "total_output_frames",
    "total_output_samples",
    "outputs",
    "publication_state",
    "completed_at_ms",
    "receipt_fingerprint",
}
_OUTPUT_FIELDS = {
    "handle",
    "kind",
    "byte_length",
    "content_fingerprint",
    "video_frame_count",
    "audio_sample_count",
    "duration",
}
_RATIONAL_FIELDS = {"numerator", "denominator"}


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AVSeamPolicyError("duplicate_seam_receipt_member")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> object:
    raise AVSeamPolicyError("nonfinite_seam_receipt_value")


def _decode_rational(value: object, field: str) -> AVRational:
    if type(value) is not dict or set(value) != _RATIONAL_FIELDS:
        raise AVSeamPolicyError(f"seam_receipt_members:{field}")
    return AVRational(cast(int, value["numerator"]), cast(int, value["denominator"]))


def decode_av_seam_reconstruction_receipt(
    payload: str | bytes | bytearray,
) -> AVSeamReconstructionReceipt:
    """Strictly decode canonical UTF-8 seam receipt bytes with duplicate rejection."""

    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeError:
            raise AVSeamPolicyError("seam_receipt_utf8") from None
        text = payload
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError:
            raise AVSeamPolicyError("seam_receipt_utf8") from None
    else:
        raise AVSeamPolicyError("seam_receipt_payload_type")
    if len(encoded) > MAX_AV_SEAM_RECEIPT_BYTES:
        raise AVSeamPolicyError("seam_receipt_wire_limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except AVSeamPolicyError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError):
        raise AVSeamPolicyError("seam_receipt_json") from None
    if type(value) is not dict or set(value) != _SEAM_RECEIPT_FIELDS:
        raise AVSeamPolicyError("seam_receipt_members")
    seams_value = value["seams"]
    if type(seams_value) is not list:
        raise AVSeamPolicyError("seam_receipt.seams")
    seams = tuple(
        _decode_seam_result(item, f"seams[{index}]") for index, item in enumerate(seams_value)
    )
    outputs_value = value["outputs"]
    if type(outputs_value) is not list:
        raise AVSeamPolicyError("seam_receipt.outputs")
    outputs: list[AVReceiptOutput] = []
    for item in outputs_value:
        if type(item) is not dict or set(item) != _OUTPUT_FIELDS:
            raise AVSeamPolicyError("seam_receipt_members:output")
        try:
            kind = AVOutputKind(item["kind"])
        except (TypeError, ValueError):
            raise AVSeamPolicyError("seam_receipt_output_kind") from None
        try:
            outputs.append(
                AVReceiptOutput(
                    handle=cast(str, item["handle"]),
                    kind=kind,
                    byte_length=cast(int, item["byte_length"]),
                    content_fingerprint=cast(str, item["content_fingerprint"]),
                    video_frame_count=cast(int, item["video_frame_count"]),
                    audio_sample_count=cast(int, item["audio_sample_count"]),
                    duration=_decode_rational(item["duration"], "output.duration"),
                )
            )
        except AVReconstructionError:
            raise AVSeamPolicyError("seam_receipt_members:output") from None
    try:
        publication_state = AVPublicationState(value["publication_state"])
    except (TypeError, ValueError):
        raise AVSeamPolicyError("seam_receipt.publication_state") from None
    receipt = AVSeamReconstructionReceipt(
        transaction_id=cast(str, value["transaction_id"]),
        plan_fingerprint=cast(str, value["plan_fingerprint"]),
        approval_fingerprint=cast(str, value["approval_fingerprint"]),
        base_plan_fingerprint=cast(str, value["base_plan_fingerprint"]),
        base_approval_fingerprint=cast(str, value["base_approval_fingerprint"]),
        capability_fingerprint=cast(str, value["capability_fingerprint"]),
        seam_capability_fingerprint=cast(str, value["seam_capability_fingerprint"]),
        execution_fingerprint=cast(str, value["execution_fingerprint"]),
        seams=seams,
        total_output_frames=cast(int, value["total_output_frames"]),
        total_output_samples=cast(int, value["total_output_samples"]),
        outputs=tuple(outputs),
        publication_state=publication_state,
        completed_at_ms=cast(int, value["completed_at_ms"]),
        receipt_fingerprint=cast(str, value["receipt_fingerprint"]),
        schema=cast(str, value["schema"]),
    )
    if encoded != receipt.to_wire_bytes():
        raise AVSeamPolicyError("seam_receipt_not_canonical")
    return receipt


__all__ = [
    "AV_SEAM_CAPABILITY_SCHEMA",
    "AV_SEAM_PLAN_SCHEMA",
    "AV_SEAM_RECEIPT_SCHEMA",
    "AVSeamApproval",
    "AVSeamAudioPolicy",
    "AVSeamCapability",
    "AVSeamOperation",
    "AVSeamPlan",
    "AVSeamPolicyError",
    "AVSeamReconstructionReceipt",
    "AVSeamResult",
    "AVSeamSpec",
    "MAX_AV_SEAM_RECEIPT_BYTES",
    "MAX_SEAM_BOUNDARIES",
    "MAX_SEAM_OVERLAP_TARGET_FRAMES",
    "MAX_SEAM_SEGMENTS",
    "MIN_SEAM_OVERLAP_TARGET_FRAMES",
    "SAMPLES_PER_TARGET_FRAME",
    "SEAM_OVERLAP_GRID_TARGET_FRAMES",
    "TARGET_FRAMES_PER_SECOND",
    "approve_av_seam_plan",
    "build_av_seam_plan",
    "decode_av_seam_reconstruction_receipt",
    "qualified_seam_capability",
]
