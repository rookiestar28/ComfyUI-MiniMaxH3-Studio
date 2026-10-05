"""Two-ended audiovisual bridge planning, admission and accounting (M20-07).

Downstream of the supported `two_ended_av_bridge` row (`pixel_domain_two_ended_composition_
executes`, refreshed matrix consumed via the temporal capability profile).  The row measured
the pixel-domain composition affirmatively -- fl2va with both keyframes executes end to end
and delivers the exact requested frame count, keyframe condition latents are re-injected
every sampling step and never denoised, and no audio anchor exists on the native surface --
while both stock latent combinators reject the joint NestedTensor.  This module is the
repo-owned half: a typed bridge plan between two exact M20-04 checkpoint boundaries, one
explicit master-audio authority, a two-sided interval mask in the exact M20-06 sampler
semantics, and an admission that binds every party by fingerprint.

Frame-level accounting stays in M20-01: contexts land on ``CONTEXT_EXTENT_GRID_FRAMES`` and
conservation is checked exclusively through :func:`check_crop_conservation` (AC-M20-01-10;
this module never holds its own copy of the grid or the rule).  Two alignment facts are
derived here from the declared rates, never enumerated: the generated middle must be a whole
number of steps on both latent grids, so the gap is producible AND divisible by the exact
audio period; and the left segment's context boundary must itself be audio-exact inside that
segment, or the tail audio slice would not correspond to the declared context.  The right
head slice is always exact because the context grid is a multiple of the audio period.

Master audio is declaration-level authority: one bounded source identity plus a coverage
interval on the master timeline.  Competing boundary declarations or missing coverage are
classified refusals -- nothing is synthesized, mixed, padded or auto-selected, and no audio
media value enters this module.  The per-boundary declarations are caller-asserted
bookkeeping, not fingerprint-bound identity: the M20-04 receipt carries no master-audio
member, so admission proves that the caller's declarations agree with each other and with
the one master declaration, and nothing stronger (distinct review 2026-08-21, accepted
design under D3's declaration-level scope).

The composition builder renders the fresh full-schedule fragment (``RandomNoise`` +
``BasicScheduler`` + ``SamplerCustomAdvanced``) that consumes the composed masked bridge
latent; the caller owns model, conditioning (the pinned fl2va two-keyframe form) and the
latent source node, exactly as the M20-05 resume builder does.
"""

from __future__ import annotations

from dataclasses import dataclass

from .canonical import canonical_fingerprint
from .fingerprint_domain import IdentityDomain, domain_fingerprint
from .joint_av_latent import (
    JointAVLatentAuthority,
    JointAVLatentDescriptor,
    JointAVLatentReceipt,
)
from .latent_checkpoint_resume import (
    MAX_RESUME_SEED,
    MAX_RESUME_TOTAL_STEPS,
    QUALIFIED_RANDOM_NOISE_NODE,
    QUALIFIED_SAMPLER_NODE,
    QUALIFIED_SCHEDULER_NODE,
    ResumeCompositionNodeRefs,
)
from .length import LATTICE_STEP, MAX_FRAME_COUNT, is_producible
from .masked_av_continuation import MaskedAVAuthority
from .segment_artifacts import ArtifactLifecycleState
from .temporal_profile import (
    CONTEXT_EXTENT_GRID_FRAMES,
    EXACT_AUDIO_PERIOD_FRAMES,
    MINIMUM_CONTEXT_EXTENT_FRAMES,
    VIDEO_LATENT_STEPS_PER_LATTICE_STEP,
    CapabilityStatus,
    CropPlan,
    TemporalCapabilityProfile,
    check_crop_conservation,
    exact_audio_latent_length,
    nominal_audio_latent_length,
    video_latent_length,
)

TWO_ENDED_BRIDGE_CAPABILITY = "two_ended_av_bridge"
TWO_ENDED_BRIDGE_AUTHORITY_SCHEMA = "h3.context.two_ended_bridge_authority.v1"
MASTER_AUDIO_DECLARATION_SCHEMA = "h3.context.master_audio_declaration.v1"
TWO_ENDED_BRIDGE_PLAN_SCHEMA = "h3.context.two_ended_bridge_plan.v1"
BRIDGE_MASK_PLAN_SCHEMA = "h3.context.bridge_mask_plan.v1"
TWO_ENDED_BRIDGE_DECISION_SCHEMA = "h3.context.two_ended_bridge_decision.v1"
TWO_ENDED_BRIDGE_RECEIPT_SCHEMA = "h3.context.two_ended_bridge_receipt.v1"

#: One context grid unit expressed on each exact latent grid.  Both are derived from the
#: M20-01 symbols -- the grid is a whole number of lattice steps and a whole number of audio
#: periods by construction -- so requalifying a rate moves these with it.
VIDEO_LATENT_STEPS_PER_CONTEXT_UNIT = (
    CONTEXT_EXTENT_GRID_FRAMES // LATTICE_STEP
) * VIDEO_LATENT_STEPS_PER_LATTICE_STEP
_AUDIO_STEPS_PER_UNIT_EXACT = exact_audio_latent_length(CONTEXT_EXTENT_GRID_FRAMES)
if _AUDIO_STEPS_PER_UNIT_EXACT.denominator != 1:  # pragma: no cover - grid derivation guard
    raise AssertionError("context_extent_grid_not_audio_integral")
AUDIO_LATENT_STEPS_PER_CONTEXT_UNIT = int(_AUDIO_STEPS_PER_UNIT_EXACT)

MAX_MASTER_TIMELINE_FRAMES = 10_000_000
MAX_BRIDGE_LOOKAHEAD_FRAMES = MAX_FRAME_COUNT

_IDENTIFIER_MAX = 128
_FINGERPRINT_PREFIX = "sha256:"


class TwoEndedBridgeError(ValueError):
    """Raised when a bridge plan, admission or receipt cannot preserve exact identity."""


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > _IDENTIFIER_MAX:
        raise TwoEndedBridgeError(f"bounded_identifier:{field}")
    for char in value:
        if not (char.isascii() and (char.isalnum() or char in "_.:-")):
            raise TwoEndedBridgeError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value.startswith(_FINGERPRINT_PREFIX)
        or len(value) != len(_FINGERPRINT_PREFIX) + 64
        or any(char not in "0123456789abcdef" for char in value[len(_FINGERPRINT_PREFIX) :])
    ):
        raise TwoEndedBridgeError(f"sha256_fingerprint:{field}")
    return value


def _bounded_integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or isinstance(value, bool) or not minimum <= value <= maximum:
        raise TwoEndedBridgeError(f"bounded_integer:{field}")
    return value


@dataclass(frozen=True, slots=True)
class TwoEndedBridgeAuthority:
    """The frozen consumption of the accepted profile's supported bridge row.

    Same shape and same refusal as the sibling M20-04/05/06 authorities: the projection
    exists only in the ``SUPPORTED`` state, and capability absence is a construction
    refusal, not a value with a flag.
    """

    subject_identity: str
    reason_code: str
    profile_fingerprint: str
    schema: str = TWO_ENDED_BRIDGE_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != TWO_ENDED_BRIDGE_AUTHORITY_SCHEMA:
            raise TwoEndedBridgeError("unsupported_authority_schema")
        _fingerprint(self.subject_identity, "subject_identity")
        _identifier(self.reason_code, "reason_code")
        _fingerprint(self.profile_fingerprint, "profile_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capability": TWO_ENDED_BRIDGE_CAPABILITY,
            "subject_identity": self.subject_identity,
            "reason_code": self.reason_code,
            "profile_fingerprint": self.profile_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return domain_fingerprint(IdentityDomain.SEMANTIC, self.to_wire()).digest


def build_two_ended_bridge_authority(
    profile: TemporalCapabilityProfile,
) -> TwoEndedBridgeAuthority:
    """Freeze the accepted profile's bridge claim, refusing every unqualified state."""

    if not isinstance(profile, TemporalCapabilityProfile):
        raise TwoEndedBridgeError("authority_requires_temporal_profile")
    claim = profile.capability_claim(TWO_ENDED_BRIDGE_CAPABILITY)
    if claim.status is not CapabilityStatus.SUPPORTED:
        raise TwoEndedBridgeError("bridge_row_not_supported")
    if profile.subject_identity is None:
        raise TwoEndedBridgeError("bridge_row_without_subject_identity")
    return TwoEndedBridgeAuthority(
        subject_identity=profile.subject_identity,
        reason_code=claim.reason_code,
        profile_fingerprint=canonical_fingerprint(profile.to_wire()),
    )


@dataclass(frozen=True, slots=True)
class MasterAudioDeclaration:
    """One explicit master-audio authority: identity plus coverage, nothing else.

    Coverage is a half-open frame interval on the master timeline.  The declaration carries
    no media value, no path and no locator; whether real audio exists behind the identity is
    the owning workspace's concern, and inventing any of it here is exactly what this item
    forbids.
    """

    master_source_id: str
    coverage_start_frame: int
    coverage_end_frame: int
    schema: str = MASTER_AUDIO_DECLARATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MASTER_AUDIO_DECLARATION_SCHEMA:
            raise TwoEndedBridgeError("unsupported_master_audio_schema")
        _identifier(self.master_source_id, "master_source_id")
        _bounded_integer(
            self.coverage_start_frame, "coverage_start_frame", 0, MAX_MASTER_TIMELINE_FRAMES
        )
        _bounded_integer(
            self.coverage_end_frame, "coverage_end_frame", 0, MAX_MASTER_TIMELINE_FRAMES
        )
        if self.coverage_end_frame <= self.coverage_start_frame:
            raise TwoEndedBridgeError("master_audio_coverage_empty")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "master_source_id": self.master_source_id,
            "coverage_start_frame": self.coverage_start_frame,
            "coverage_end_frame": self.coverage_end_frame,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


@dataclass(frozen=True, slots=True)
class IntervalMaskExtent:
    """One domain's two-sided interval mask on its exact latent grid.

    Steps in ``[0, generate_from)`` and ``[generate_to, length)`` are preserved; steps in
    ``[generate_from, generate_to)`` are generated.  The M20-06 suffix form is the special
    case ``generate_to == length``; the bridge needs both sides, so the interval is the
    declared shape rather than a pair of suffixes.
    """

    length: int
    generate_from: int
    generate_to: int

    def __post_init__(self) -> None:
        if type(self.length) is not int or not 1 <= self.length <= 1_000_000:
            raise TwoEndedBridgeError("bounded_integer:length")
        if type(self.generate_from) is not int or not 0 <= self.generate_from <= self.length:
            raise TwoEndedBridgeError("bounded_integer:generate_from")
        if (
            type(self.generate_to) is not int
            or not self.generate_from <= self.generate_to <= self.length
        ):
            raise TwoEndedBridgeError("bounded_integer:generate_to")

    @property
    def leading_preserved_steps(self) -> int:
        return self.generate_from

    @property
    def generated_steps(self) -> int:
        return self.generate_to - self.generate_from

    @property
    def trailing_preserved_steps(self) -> int:
        return self.length - self.generate_to

    def to_wire(self) -> dict[str, object]:
        return {
            "length": self.length,
            "generate_from": self.generate_from,
            "generate_to": self.generate_to,
        }


@dataclass(frozen=True, slots=True)
class TwoEndedBridgePlan:
    """The exact shape of one bridge run, stated before any of it is executed.

    Every extent is integral by the grid arithmetic this plan enforces at construction; the
    latent-step members are derived, cross-checked against the M20-01 functions, and carried
    explicitly so the adapter slices by declaration instead of re-deriving.
    """

    target_gap_frames: int
    left_context_frames: int
    right_context_frames: int
    produced_frames: int
    left_end_frame: int
    right_start_frame: int
    bridge_start_frame: int
    left_lookahead_frames: int
    right_lookahead_frames: int
    left_video_steps: int
    middle_video_steps: int
    right_video_steps: int
    left_audio_steps: int
    middle_audio_steps: int
    right_audio_steps: int
    left_descriptor_fingerprint: str
    right_descriptor_fingerprint: str
    descriptor_authority_fingerprint: str
    master_fingerprint: str
    schema: str = TWO_ENDED_BRIDGE_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != TWO_ENDED_BRIDGE_PLAN_SCHEMA:
            raise TwoEndedBridgeError("unsupported_plan_schema")
        for name in (
            "target_gap_frames",
            "left_context_frames",
            "right_context_frames",
            "produced_frames",
        ):
            _bounded_integer(getattr(self, name), name, 1, MAX_FRAME_COUNT)
        for name in ("left_end_frame", "right_start_frame", "bridge_start_frame"):
            _bounded_integer(getattr(self, name), name, 0, MAX_MASTER_TIMELINE_FRAMES)
        for name in ("left_lookahead_frames", "right_lookahead_frames"):
            _bounded_integer(getattr(self, name), name, 0, MAX_BRIDGE_LOOKAHEAD_FRAMES)
        for name in (
            "left_video_steps",
            "middle_video_steps",
            "right_video_steps",
            "left_audio_steps",
            "middle_audio_steps",
            "right_audio_steps",
        ):
            _bounded_integer(getattr(self, name), name, 1, 1_000_000)
        _fingerprint(self.left_descriptor_fingerprint, "left_descriptor")
        _fingerprint(self.right_descriptor_fingerprint, "right_descriptor")
        _fingerprint(self.descriptor_authority_fingerprint, "descriptor_authority")
        _fingerprint(self.master_fingerprint, "master")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "target_gap_frames": self.target_gap_frames,
            "left_context_frames": self.left_context_frames,
            "right_context_frames": self.right_context_frames,
            "produced_frames": self.produced_frames,
            "left_end_frame": self.left_end_frame,
            "right_start_frame": self.right_start_frame,
            "bridge_start_frame": self.bridge_start_frame,
            "left_lookahead_frames": self.left_lookahead_frames,
            "right_lookahead_frames": self.right_lookahead_frames,
            "left_video_steps": self.left_video_steps,
            "middle_video_steps": self.middle_video_steps,
            "right_video_steps": self.right_video_steps,
            "left_audio_steps": self.left_audio_steps,
            "middle_audio_steps": self.middle_audio_steps,
            "right_audio_steps": self.right_audio_steps,
            "left_descriptor_fingerprint": self.left_descriptor_fingerprint,
            "right_descriptor_fingerprint": self.right_descriptor_fingerprint,
            "descriptor_authority_fingerprint": self.descriptor_authority_fingerprint,
            "master_fingerprint": self.master_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def _check_boundary_descriptor(descriptor: JointAVLatentDescriptor, side: str) -> None:
    """A boundary must be a complete, grid-true segment; anything else is misdescription."""

    if type(descriptor) is not JointAVLatentDescriptor:
        raise TwoEndedBridgeError(f"descriptor_type:{side}")
    if descriptor.completed_frames != descriptor.requested_frames:
        raise TwoEndedBridgeError(f"boundary_extent_partial:{side}")
    if descriptor.video.shape[2] != video_latent_length(descriptor.requested_frames):
        raise TwoEndedBridgeError(f"grid_mismatch:{side}_video")
    if descriptor.audio.shape[3] != nominal_audio_latent_length(descriptor.requested_frames):
        raise TwoEndedBridgeError(f"grid_mismatch:{side}_audio")


def plan_two_ended_bridge(
    *,
    left_descriptor: JointAVLatentDescriptor,
    right_descriptor: JointAVLatentDescriptor,
    descriptor_authority: JointAVLatentAuthority,
    master: MasterAudioDeclaration,
    target_gap_frames: int,
    left_context_frames: int,
    right_context_frames: int,
    left_end_frame: int,
    right_start_frame: int,
    left_lookahead_frames: int = 0,
    right_lookahead_frames: int = 0,
) -> TwoEndedBridgePlan:
    """Plan one bridge on the exact grids, refusing every misalignment by name.

    Everything refused here is a structural impossibility -- an extent the grids cannot
    represent, a context the segment does not contain, a shape the composition cannot join.
    Identity questions (receipt state, expiry, profile alignment, master conflicts) belong
    to :func:`admit_two_ended_bridge`, which classifies instead of raising.
    """

    if type(descriptor_authority) is not JointAVLatentAuthority:
        raise TwoEndedBridgeError("descriptor_authority_type")
    if type(master) is not MasterAudioDeclaration:
        raise TwoEndedBridgeError("master_audio_type")
    _check_boundary_descriptor(left_descriptor, "left")
    _check_boundary_descriptor(right_descriptor, "right")
    for side, descriptor in (("left", left_descriptor), ("right", right_descriptor)):
        if descriptor.authority_fingerprint != descriptor_authority.fingerprint:
            raise TwoEndedBridgeError(f"descriptor_authority_mismatch:{side}")
    if (
        left_descriptor.video.shape[0] != right_descriptor.video.shape[0]
        or left_descriptor.video.shape[3] != right_descriptor.video.shape[3]
        or left_descriptor.video.shape[4] != right_descriptor.video.shape[4]
        or left_descriptor.video.dtype != right_descriptor.video.dtype
    ):
        raise TwoEndedBridgeError("boundary_shape_mismatch:video")
    if (
        left_descriptor.audio.shape[0] != right_descriptor.audio.shape[0]
        or left_descriptor.audio.shape[2] != right_descriptor.audio.shape[2]
        or left_descriptor.audio.dtype != right_descriptor.audio.dtype
    ):
        raise TwoEndedBridgeError("boundary_shape_mismatch:audio")

    _bounded_integer(target_gap_frames, "target_gap_frames", 1, MAX_FRAME_COUNT)
    _bounded_integer(left_end_frame, "left_end_frame", 0, MAX_MASTER_TIMELINE_FRAMES)
    _bounded_integer(right_start_frame, "right_start_frame", 0, MAX_MASTER_TIMELINE_FRAMES)
    _bounded_integer(left_lookahead_frames, "left_lookahead_frames", 0, MAX_BRIDGE_LOOKAHEAD_FRAMES)
    _bounded_integer(
        right_lookahead_frames, "right_lookahead_frames", 0, MAX_BRIDGE_LOOKAHEAD_FRAMES
    )

    for name, extent, segment_frames in (
        ("left_context_frames", left_context_frames, left_descriptor.requested_frames),
        ("right_context_frames", right_context_frames, right_descriptor.requested_frames),
    ):
        _bounded_integer(extent, name, 1, MAX_FRAME_COUNT)
        if extent % CONTEXT_EXTENT_GRID_FRAMES != 0:
            raise TwoEndedBridgeError(f"context_extent_off_grid:{name}")
        if extent < MINIMUM_CONTEXT_EXTENT_FRAMES:
            raise TwoEndedBridgeError(f"context_extent_below_minimum:{name}")
        if extent > segment_frames:
            raise TwoEndedBridgeError(f"context_exceeds_segment:{name}")

    if not is_producible(target_gap_frames):
        raise TwoEndedBridgeError("gap_not_producible")
    if target_gap_frames % EXACT_AUDIO_PERIOD_FRAMES != 0:
        raise TwoEndedBridgeError("gap_not_audio_exact")
    if (left_descriptor.requested_frames - left_context_frames) % EXACT_AUDIO_PERIOD_FRAMES != 0:
        raise TwoEndedBridgeError("left_context_boundary_not_audio_exact")
    if right_start_frame - left_end_frame != target_gap_frames:
        raise TwoEndedBridgeError("gap_placement_mismatch")
    if left_end_frame < left_context_frames:
        raise TwoEndedBridgeError("bridge_start_negative")

    produced_frames = left_context_frames + target_gap_frames + right_context_frames
    if not is_producible(produced_frames):
        raise TwoEndedBridgeError("produced_not_producible")

    left_units = left_context_frames // CONTEXT_EXTENT_GRID_FRAMES
    right_units = right_context_frames // CONTEXT_EXTENT_GRID_FRAMES
    left_video_steps = left_units * VIDEO_LATENT_STEPS_PER_CONTEXT_UNIT
    right_video_steps = right_units * VIDEO_LATENT_STEPS_PER_CONTEXT_UNIT
    produced_video_steps = video_latent_length(produced_frames)
    middle_video_steps = produced_video_steps - left_video_steps - right_video_steps
    if middle_video_steps != video_latent_length(target_gap_frames):
        raise TwoEndedBridgeError("video_step_accounting_mismatch")

    produced_audio_exact = exact_audio_latent_length(produced_frames)
    if produced_audio_exact.denominator != 1:
        raise TwoEndedBridgeError("produced_not_audio_exact")
    left_audio_steps = left_units * AUDIO_LATENT_STEPS_PER_CONTEXT_UNIT
    right_audio_steps = right_units * AUDIO_LATENT_STEPS_PER_CONTEXT_UNIT
    middle_audio_steps = int(produced_audio_exact) - left_audio_steps - right_audio_steps
    middle_audio_exact = exact_audio_latent_length(target_gap_frames)
    if middle_audio_exact.denominator != 1 or middle_audio_steps != int(middle_audio_exact):
        raise TwoEndedBridgeError("audio_step_accounting_mismatch")

    return TwoEndedBridgePlan(
        target_gap_frames=target_gap_frames,
        left_context_frames=left_context_frames,
        right_context_frames=right_context_frames,
        produced_frames=produced_frames,
        left_end_frame=left_end_frame,
        right_start_frame=right_start_frame,
        bridge_start_frame=left_end_frame - left_context_frames,
        left_lookahead_frames=left_lookahead_frames,
        right_lookahead_frames=right_lookahead_frames,
        left_video_steps=left_video_steps,
        middle_video_steps=middle_video_steps,
        right_video_steps=right_video_steps,
        left_audio_steps=left_audio_steps,
        middle_audio_steps=middle_audio_steps,
        right_audio_steps=right_audio_steps,
        left_descriptor_fingerprint=left_descriptor.fingerprint,
        right_descriptor_fingerprint=right_descriptor.fingerprint,
        descriptor_authority_fingerprint=descriptor_authority.fingerprint,
        master_fingerprint=master.fingerprint,
    )


@dataclass(frozen=True, slots=True)
class BridgeMaskPlan:
    """One dual-domain interval mask, bound to the bridge plan that shaped it."""

    video: IntervalMaskExtent
    audio: IntervalMaskExtent
    bridge_plan_fingerprint: str
    schema: str = BRIDGE_MASK_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != BRIDGE_MASK_PLAN_SCHEMA:
            raise TwoEndedBridgeError("unsupported_mask_plan_schema")
        if type(self.video) is not IntervalMaskExtent or type(self.audio) is not (
            IntervalMaskExtent
        ):
            raise TwoEndedBridgeError("mask_plan_domain_type")
        _fingerprint(self.bridge_plan_fingerprint, "bridge_plan")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "video": self.video.to_wire(),
            "audio": self.audio.to_wire(),
            "bridge_plan_fingerprint": self.bridge_plan_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def plan_bridge_mask(plan: TwoEndedBridgePlan) -> BridgeMaskPlan:
    """Derive the interval mask from the plan; there is nothing else to decide."""

    if type(plan) is not TwoEndedBridgePlan:
        raise TwoEndedBridgeError("plan_type")
    video_length = plan.left_video_steps + plan.middle_video_steps + plan.right_video_steps
    audio_length = plan.left_audio_steps + plan.middle_audio_steps + plan.right_audio_steps
    return BridgeMaskPlan(
        video=IntervalMaskExtent(
            length=video_length,
            generate_from=plan.left_video_steps,
            generate_to=plan.left_video_steps + plan.middle_video_steps,
        ),
        audio=IntervalMaskExtent(
            length=audio_length,
            generate_from=plan.left_audio_steps,
            generate_to=plan.left_audio_steps + plan.middle_audio_steps,
        ),
        bridge_plan_fingerprint=plan.fingerprint,
    )


@dataclass(frozen=True, slots=True)
class TwoEndedBridgeDecision:
    """One bridge admission outcome; admitted exactly or classified refusal."""

    admitted: bool
    reason_codes: tuple[str, ...]
    plan_fingerprint: str
    mask_plan_fingerprint: str
    left_receipt_fingerprint: str
    right_receipt_fingerprint: str
    master_fingerprint: str
    authority_fingerprint: str
    descriptor_authority_fingerprint: str
    mask_authority_fingerprint: str
    crop_verdict_reason: str
    schema: str = TWO_ENDED_BRIDGE_DECISION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != TWO_ENDED_BRIDGE_DECISION_SCHEMA:
            raise TwoEndedBridgeError("unsupported_decision_schema")
        if type(self.admitted) is not bool:
            raise TwoEndedBridgeError("decision_admitted")
        if type(self.reason_codes) is not tuple:
            raise TwoEndedBridgeError("decision_reason_codes")
        for reason in self.reason_codes:
            _identifier(reason, "reason_code")
        if self.admitted and self.reason_codes:
            raise TwoEndedBridgeError("admitted_with_reasons")
        if not self.admitted and not self.reason_codes:
            raise TwoEndedBridgeError("refused_without_reasons")
        _fingerprint(self.plan_fingerprint, "plan")
        _fingerprint(self.mask_plan_fingerprint, "mask_plan")
        _fingerprint(self.left_receipt_fingerprint, "left_receipt")
        _fingerprint(self.right_receipt_fingerprint, "right_receipt")
        _fingerprint(self.master_fingerprint, "master")
        _fingerprint(self.authority_fingerprint, "authority")
        _fingerprint(self.descriptor_authority_fingerprint, "descriptor_authority")
        _fingerprint(self.mask_authority_fingerprint, "mask_authority")
        _identifier(self.crop_verdict_reason, "crop_verdict_reason")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "admitted": self.admitted,
            "reason_codes": list(self.reason_codes),
            "plan_fingerprint": self.plan_fingerprint,
            "mask_plan_fingerprint": self.mask_plan_fingerprint,
            "left_receipt_fingerprint": self.left_receipt_fingerprint,
            "right_receipt_fingerprint": self.right_receipt_fingerprint,
            "master_fingerprint": self.master_fingerprint,
            "authority_fingerprint": self.authority_fingerprint,
            "descriptor_authority_fingerprint": self.descriptor_authority_fingerprint,
            "mask_authority_fingerprint": self.mask_authority_fingerprint,
            "crop_verdict_reason": self.crop_verdict_reason,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def admit_two_ended_bridge(
    *,
    authority: TwoEndedBridgeAuthority,
    descriptor_authority: JointAVLatentAuthority,
    mask_authority: MaskedAVAuthority,
    plan: TwoEndedBridgePlan,
    mask_plan: BridgeMaskPlan,
    left_receipt: JointAVLatentReceipt,
    right_receipt: JointAVLatentReceipt,
    master: MasterAudioDeclaration,
    left_master_source_id: str,
    right_master_source_id: str,
    now_ms: int,
) -> TwoEndedBridgeDecision:
    """Admit one bridge; full binding across every party or classified refusal.

    Type violations raise.  Everything else accumulates one reason per failed binding: all
    three authorities must be projections of the same accepted profile over the same
    subject, both receipts must be COMPLETE, unexpired, distinct, planned-against and
    profile-aligned with each other, the declared master authority must be the one both
    boundaries declare and must cover the produced interval plus lookahead, the mask must be
    derived from this exact plan, and the frame-level accounting must conserve the target
    interval exactly (the M20-01 rule, consumed, never copied).
    """

    if type(authority) is not TwoEndedBridgeAuthority:
        raise TwoEndedBridgeError("authority_type")
    if type(descriptor_authority) is not JointAVLatentAuthority:
        raise TwoEndedBridgeError("descriptor_authority_type")
    if type(mask_authority) is not MaskedAVAuthority:
        raise TwoEndedBridgeError("mask_authority_type")
    if type(plan) is not TwoEndedBridgePlan:
        raise TwoEndedBridgeError("plan_type")
    if type(mask_plan) is not BridgeMaskPlan:
        raise TwoEndedBridgeError("mask_plan_type")
    if type(left_receipt) is not JointAVLatentReceipt:
        raise TwoEndedBridgeError("left_receipt_type")
    if type(right_receipt) is not JointAVLatentReceipt:
        raise TwoEndedBridgeError("right_receipt_type")
    if type(master) is not MasterAudioDeclaration:
        raise TwoEndedBridgeError("master_audio_type")
    _identifier(left_master_source_id, "left_master_source_id")
    _identifier(right_master_source_id, "right_master_source_id")
    _bounded_integer(now_ms, "now_ms", 1, 9_999_999_999_999)

    reasons: list[str] = []
    if (
        authority.subject_identity != descriptor_authority.subject_identity
        or mask_authority.subject_identity != descriptor_authority.subject_identity
    ):
        reasons.append("authority_subject_mismatch")
    if (
        authority.profile_fingerprint != descriptor_authority.profile_fingerprint
        or mask_authority.profile_fingerprint != descriptor_authority.profile_fingerprint
    ):
        reasons.append("authority_profile_mismatch")
    for side, receipt in (("left", left_receipt), ("right", right_receipt)):
        if receipt.state is not ArtifactLifecycleState.COMPLETE:
            reasons.append(f"{side}_receipt_not_complete")
        if receipt.expires_at_ms <= now_ms:
            reasons.append(f"{side}_receipt_expired")
        if receipt.descriptor.authority_fingerprint != descriptor_authority.fingerprint:
            reasons.append(f"authority_binding_mismatch:{side}")
    if left_receipt.fingerprint == right_receipt.fingerprint:
        reasons.append("boundary_not_distinct")
    if plan.left_descriptor_fingerprint != left_receipt.descriptor.fingerprint:
        reasons.append("descriptor_binding_mismatch:left")
    if plan.right_descriptor_fingerprint != right_receipt.descriptor.fingerprint:
        reasons.append("descriptor_binding_mismatch:right")
    if plan.descriptor_authority_fingerprint != descriptor_authority.fingerprint:
        reasons.append("plan_authority_binding_mismatch")
    if left_receipt.model_fingerprint != right_receipt.model_fingerprint:
        reasons.append("boundary_model_mismatch")
    if left_receipt.runtime_fingerprint != right_receipt.runtime_fingerprint:
        reasons.append("boundary_runtime_mismatch")
    if left_receipt.settings_fingerprint != right_receipt.settings_fingerprint:
        reasons.append("boundary_settings_mismatch")
    if left_receipt.source_id != right_receipt.source_id:
        reasons.append("boundary_source_mismatch")
    if plan.master_fingerprint != master.fingerprint:
        reasons.append("master_binding_mismatch")
    if left_master_source_id != master.master_source_id:
        reasons.append("master_audio_conflict:left")
    if right_master_source_id != master.master_source_id:
        reasons.append("master_audio_conflict:right")
    required_start = plan.bridge_start_frame - plan.left_lookahead_frames
    required_end = plan.bridge_start_frame + plan.produced_frames + plan.right_lookahead_frames
    if master.coverage_start_frame > max(required_start, 0) or (
        master.coverage_end_frame < required_end
    ):
        reasons.append("master_audio_coverage_gap")
    if mask_plan.bridge_plan_fingerprint != plan.fingerprint:
        reasons.append("mask_binding_mismatch")
    verdict = check_crop_conservation(
        CropPlan(
            requested_target_frames=plan.target_gap_frames,
            leading_context_frames=plan.left_context_frames,
            trailing_context_frames=plan.right_context_frames,
            produced_frames=plan.produced_frames,
            leading_crop_frames=plan.left_context_frames,
            trailing_crop_frames=plan.right_context_frames,
        )
    )
    if not verdict.conserved:
        reasons.append(f"crop_not_conserved:{verdict.reason_code}")

    admitted = not reasons
    return TwoEndedBridgeDecision(
        admitted=admitted,
        reason_codes=tuple(reasons),
        plan_fingerprint=plan.fingerprint,
        mask_plan_fingerprint=mask_plan.fingerprint,
        left_receipt_fingerprint=left_receipt.fingerprint,
        right_receipt_fingerprint=right_receipt.fingerprint,
        master_fingerprint=master.fingerprint,
        authority_fingerprint=authority.fingerprint,
        descriptor_authority_fingerprint=descriptor_authority.fingerprint,
        mask_authority_fingerprint=mask_authority.fingerprint,
        crop_verdict_reason=verdict.reason_code,
    )


@dataclass(frozen=True, slots=True)
class TwoEndedBridgeReceipt:
    """One content-free bridge provenance record, bound to everything it consumed.

    The receipt exists only for an admitted decision whose output artifact is COMPLETE at
    the planned extent with the left boundary as its declared predecessor (finalization
    decision D5).  It carries identifiers, fingerprints and frame accounting -- no path,
    media value, prompt or credential has a member to hide in.
    """

    decision_fingerprint: str
    plan_fingerprint: str
    left_receipt_fingerprint: str
    right_receipt_fingerprint: str
    master_fingerprint: str
    output_receipt_fingerprint: str
    target_gap_frames: int
    left_context_frames: int
    right_context_frames: int
    left_lookahead_frames: int
    right_lookahead_frames: int
    leading_crop_frames: int
    trailing_crop_frames: int
    schema: str = TWO_ENDED_BRIDGE_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != TWO_ENDED_BRIDGE_RECEIPT_SCHEMA:
            raise TwoEndedBridgeError("unsupported_receipt_schema")
        _fingerprint(self.decision_fingerprint, "decision")
        _fingerprint(self.plan_fingerprint, "plan")
        _fingerprint(self.left_receipt_fingerprint, "left_receipt")
        _fingerprint(self.right_receipt_fingerprint, "right_receipt")
        _fingerprint(self.master_fingerprint, "master")
        _fingerprint(self.output_receipt_fingerprint, "output_receipt")
        for name in (
            "target_gap_frames",
            "left_context_frames",
            "right_context_frames",
            "leading_crop_frames",
            "trailing_crop_frames",
        ):
            _bounded_integer(getattr(self, name), name, 1, MAX_FRAME_COUNT)
        for name in ("left_lookahead_frames", "right_lookahead_frames"):
            _bounded_integer(getattr(self, name), name, 0, MAX_BRIDGE_LOOKAHEAD_FRAMES)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "decision_fingerprint": self.decision_fingerprint,
            "plan_fingerprint": self.plan_fingerprint,
            "left_receipt_fingerprint": self.left_receipt_fingerprint,
            "right_receipt_fingerprint": self.right_receipt_fingerprint,
            "master_fingerprint": self.master_fingerprint,
            "output_receipt_fingerprint": self.output_receipt_fingerprint,
            "target_gap_frames": self.target_gap_frames,
            "left_context_frames": self.left_context_frames,
            "right_context_frames": self.right_context_frames,
            "left_lookahead_frames": self.left_lookahead_frames,
            "right_lookahead_frames": self.right_lookahead_frames,
            "leading_crop_frames": self.leading_crop_frames,
            "trailing_crop_frames": self.trailing_crop_frames,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def build_two_ended_bridge_receipt(
    *,
    decision: TwoEndedBridgeDecision,
    plan: TwoEndedBridgePlan,
    output_receipt: JointAVLatentReceipt,
) -> TwoEndedBridgeReceipt:
    """Bind one admitted decision to its completed output artifact, refusing any drift."""

    if type(decision) is not TwoEndedBridgeDecision:
        raise TwoEndedBridgeError("decision_type")
    if type(plan) is not TwoEndedBridgePlan:
        raise TwoEndedBridgeError("plan_type")
    if type(output_receipt) is not JointAVLatentReceipt:
        raise TwoEndedBridgeError("output_receipt_type")
    if not decision.admitted:
        raise TwoEndedBridgeError("decision_not_admitted")
    if decision.plan_fingerprint != plan.fingerprint:
        raise TwoEndedBridgeError("plan_binding_mismatch")
    if output_receipt.state is not ArtifactLifecycleState.COMPLETE:
        raise TwoEndedBridgeError("output_receipt_not_complete")
    if output_receipt.descriptor.requested_frames != plan.produced_frames:
        raise TwoEndedBridgeError("output_extent_mismatch")
    if output_receipt.descriptor.completed_frames != plan.produced_frames:
        raise TwoEndedBridgeError("output_extent_partial")
    if output_receipt.descriptor.authority_fingerprint != (
        decision.descriptor_authority_fingerprint
    ):
        raise TwoEndedBridgeError("output_authority_mismatch")
    if output_receipt.predecessor_artifact_fingerprint != decision.left_receipt_fingerprint:
        raise TwoEndedBridgeError("output_predecessor_mismatch")
    if output_receipt.fingerprint in (
        decision.left_receipt_fingerprint,
        decision.right_receipt_fingerprint,
    ):
        raise TwoEndedBridgeError("output_not_distinct")
    return TwoEndedBridgeReceipt(
        decision_fingerprint=decision.fingerprint,
        plan_fingerprint=plan.fingerprint,
        left_receipt_fingerprint=decision.left_receipt_fingerprint,
        right_receipt_fingerprint=decision.right_receipt_fingerprint,
        master_fingerprint=decision.master_fingerprint,
        output_receipt_fingerprint=output_receipt.fingerprint,
        target_gap_frames=plan.target_gap_frames,
        left_context_frames=plan.left_context_frames,
        right_context_frames=plan.right_context_frames,
        left_lookahead_frames=plan.left_lookahead_frames,
        right_lookahead_frames=plan.right_lookahead_frames,
        leading_crop_frames=plan.left_context_frames,
        trailing_crop_frames=plan.right_context_frames,
    )


def build_bridge_composition(
    *,
    seed: int,
    total_steps: int,
    refs: ResumeCompositionNodeRefs,
    scheduler_name: str,
    denoise: str = "1.0",
) -> dict[str, dict[str, object]]:
    """Render the fresh full-schedule bridge fragment as a deterministic prompt-graph dict.

    The masked bridge latent enters as ``latent_image`` from the caller's latent source node
    (the composition adapter's output); the caller also owns the model, the pinned fl2va
    two-keyframe conditioning behind the guider, and the sampler selection.  Unlike the
    M20-05 resume fragment there is no split and noise is real: the middle is generated from
    scratch while the interval mask preserves both contexts.

    The returned node ids (``bridge_noise``, ``bridge_sigmas``, ``bridge_sampler``) are
    namespaced to avoid colliding with caller graphs; the sampled output is read from
    ``bridge_sampler`` output 0.
    """

    _bounded_integer(seed, "seed", 0, MAX_RESUME_SEED)
    _bounded_integer(total_steps, "total_steps", 1, MAX_RESUME_TOTAL_STEPS)
    if type(refs) is not ResumeCompositionNodeRefs:
        raise TwoEndedBridgeError("composition_refs_type")
    _identifier(scheduler_name, "scheduler_name")
    if type(denoise) is not str or denoise not in {"1.0", "1"}:
        # The qualified rows all ran the full schedule at denoise 1.0; any other value is an
        # unqualified variation this builder refuses rather than silently renders.
        raise TwoEndedBridgeError("unqualified_denoise")
    return {
        "bridge_noise": {
            "class_type": QUALIFIED_RANDOM_NOISE_NODE,
            "inputs": {"noise_seed": seed},
        },
        "bridge_sigmas": {
            "class_type": QUALIFIED_SCHEDULER_NODE,
            "inputs": {
                "model": [refs.model_node, refs.model_output_index],
                "scheduler": scheduler_name,
                "steps": total_steps,
                "denoise": float(denoise),
            },
        },
        "bridge_sampler": {
            "class_type": QUALIFIED_SAMPLER_NODE,
            "inputs": {
                "noise": ["bridge_noise", 0],
                "guider": [refs.guider_node, refs.guider_output_index],
                "sampler": [refs.sampler_select_node, refs.sampler_output_index],
                "sigmas": ["bridge_sigmas", 0],
                "latent_image": [refs.latent_source_node, refs.latent_output_index],
            },
        },
    }


__all__ = [
    "AUDIO_LATENT_STEPS_PER_CONTEXT_UNIT",
    "BRIDGE_MASK_PLAN_SCHEMA",
    "MASTER_AUDIO_DECLARATION_SCHEMA",
    "MAX_BRIDGE_LOOKAHEAD_FRAMES",
    "MAX_MASTER_TIMELINE_FRAMES",
    "TWO_ENDED_BRIDGE_AUTHORITY_SCHEMA",
    "TWO_ENDED_BRIDGE_CAPABILITY",
    "TWO_ENDED_BRIDGE_DECISION_SCHEMA",
    "TWO_ENDED_BRIDGE_PLAN_SCHEMA",
    "TWO_ENDED_BRIDGE_RECEIPT_SCHEMA",
    "VIDEO_LATENT_STEPS_PER_CONTEXT_UNIT",
    "BridgeMaskPlan",
    "IntervalMaskExtent",
    "MasterAudioDeclaration",
    "TwoEndedBridgeAuthority",
    "TwoEndedBridgeDecision",
    "TwoEndedBridgeError",
    "TwoEndedBridgePlan",
    "TwoEndedBridgeReceipt",
    "admit_two_ended_bridge",
    "build_bridge_composition",
    "build_two_ended_bridge_authority",
    "build_two_ended_bridge_receipt",
    "plan_bridge_mask",
    "plan_two_ended_bridge",
]
