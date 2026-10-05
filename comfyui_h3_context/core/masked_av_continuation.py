"""Dual-domain masked AV continuation planning and admission (M20-06).

Downstream of the M19-08 `dual_domain_av_mask` row.  The row measured the mechanism itself
affirmatively -- the pinned sampler accepts a NESTED denoise mask and applies one mask per
domain, with semantics ``1 = generate, 0 = preserve`` and an output blend that makes a fully
protected domain an exact passthrough -- while measuring native workflow reachability as
absent (no native node constructs a nested mask value).  This module is the repo-owned half:
a typed per-domain mask plan on the EXACT latent grids, and an admission that composes the
M20-05 resume decision with that plan.  The authority is constructible only from an accepted
matrix whose row is ``SUPPORTED``; the shipped M19-08 state (`unsupported /
no_native_nested_mask_constructor`) refuses, so nothing can admit a masked continuation until
this item's own supported-host requalification is accepted and the refreshed matrix is the
one being consumed.

Masks are planned at the exact per-domain latent grid (video ``T`` steps, audio ``L`` steps,
both derived from the M20-01 arithmetic and cross-checked against the checkpoint descriptor)
so the host's ``reshape_mask`` interpolation is the identity and no soft edge is silently
introduced.  Each domain's plan is the continuation suffix form -- preserve every step below
``generate_from``, generate every step at or above it -- whose extremes express the open and
protect probes the qualification runs.  Frame-level accounting stays in M20-01's
:func:`check_crop_conservation`; this module consumes that constraint and never holds its own
copy of the grid or conservation rule (AC-M20-01-10).

The composition needs no builder of its own: the qualified topology is exactly the M20-05
resume fragment with its ``latent_source_node`` pointed at the node that attached the nested
mask, so callers reuse :func:`~.latent_checkpoint_resume.build_resume_composition`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .canonical import canonical_fingerprint
from .joint_av_latent import (
    JointAVLatentAuthority,
    JointAVLatentDescriptor,
    JointAVLatentReceipt,
)
from .latent_checkpoint_resume import LatentResumeDecision
from .temporal_profile import (
    CapabilityStatus,
    CropPlan,
    TemporalCapabilityProfile,
    check_crop_conservation,
    nominal_audio_latent_length,
    video_latent_length,
)

MASKED_AV_CAPABILITY = "dual_domain_av_mask"
MASKED_AV_AUTHORITY_SCHEMA = "h3.context.masked_av_authority.v1"
NESTED_AV_MASK_PLAN_SCHEMA = "h3.context.nested_av_mask_plan.v1"
MASKED_CONTINUATION_DECISION_SCHEMA = "h3.context.masked_continuation_decision.v1"

#: Mask value semantics, CONFIRMED from the pinned sampler source (samplers.py:634-642):
#: the denoise mask multiplies the generated trajectory, its complement multiplies the
#: preserved latent.  These are the only two values a plan may render.
MASK_PRESERVE = 0.0
MASK_GENERATE = 1.0

_IDENTIFIER_MAX = 128
_FINGERPRINT_PREFIX = "sha256:"


class MaskedAVContinuationError(ValueError):
    """Raised when a mask plan or admission cannot preserve exact identity."""


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > _IDENTIFIER_MAX:
        raise MaskedAVContinuationError(f"bounded_identifier:{field}")
    for char in value:
        if not (char.isascii() and (char.isalnum() or char in "_.:-")):
            raise MaskedAVContinuationError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value.startswith(_FINGERPRINT_PREFIX)
        or len(value) != len(_FINGERPRINT_PREFIX) + 64
        or any(char not in "0123456789abcdef" for char in value[len(_FINGERPRINT_PREFIX) :])
    ):
        raise MaskedAVContinuationError(f"sha256_fingerprint:{field}")
    return value


@dataclass(frozen=True, slots=True)
class MaskedAVAuthority:
    """The frozen consumption of an accepted profile's SUPPORTED dual-domain mask row."""

    subject_identity: str
    reason_code: str
    profile_fingerprint: str
    schema: str = MASKED_AV_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MASKED_AV_AUTHORITY_SCHEMA:
            raise MaskedAVContinuationError("unsupported_authority_schema")
        _fingerprint(self.subject_identity, "subject_identity")
        _identifier(self.reason_code, "reason_code")
        _fingerprint(self.profile_fingerprint, "profile_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capability": MASKED_AV_CAPABILITY,
            "subject_identity": self.subject_identity,
            "reason_code": self.reason_code,
            "profile_fingerprint": self.profile_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def build_masked_av_authority(profile: TemporalCapabilityProfile) -> MaskedAVAuthority:
    """Freeze the accepted profile's mask-row claim, refusing every unqualified state."""

    if not isinstance(profile, TemporalCapabilityProfile):
        raise MaskedAVContinuationError("authority_requires_temporal_profile")
    claim = profile.capability_claim(MASKED_AV_CAPABILITY)
    if claim.status is not CapabilityStatus.SUPPORTED:
        raise MaskedAVContinuationError("mask_row_not_supported")
    if profile.subject_identity is None:
        raise MaskedAVContinuationError("mask_row_without_subject_identity")
    return MaskedAVAuthority(
        subject_identity=profile.subject_identity,
        reason_code=claim.reason_code,
        profile_fingerprint=canonical_fingerprint(profile.to_wire()),
    )


@dataclass(frozen=True, slots=True)
class DomainMaskExtent:
    """One domain's suffix mask on its exact latent grid.

    Steps below ``generate_from`` are preserved, steps at or above it are generated.
    ``generate_from == 0`` is the fully open probe; ``generate_from == length`` is the fully
    protected probe (an exact passthrough by the pinned output blend).  A trivial plan is the
    caller's stated choice, not a refusal: the qualification probes are exactly these
    extremes.
    """

    length: int
    generate_from: int

    def __post_init__(self) -> None:
        if type(self.length) is not int or not 1 <= self.length <= 1_000_000:
            raise MaskedAVContinuationError("bounded_integer:length")
        if type(self.generate_from) is not int or not 0 <= self.generate_from <= self.length:
            raise MaskedAVContinuationError("bounded_integer:generate_from")

    @property
    def preserved_steps(self) -> int:
        return self.generate_from

    @property
    def generated_steps(self) -> int:
        return self.length - self.generate_from

    def to_wire(self) -> dict[str, object]:
        return {"length": self.length, "generate_from": self.generate_from}


@dataclass(frozen=True, slots=True)
class NestedAVMaskPlan:
    """One dual-domain mask plan, bound to the checkpoint descriptor it masks."""

    video: DomainMaskExtent
    audio: DomainMaskExtent
    descriptor_fingerprint: str
    descriptor_authority_fingerprint: str
    schema: str = NESTED_AV_MASK_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != NESTED_AV_MASK_PLAN_SCHEMA:
            raise MaskedAVContinuationError("unsupported_mask_plan_schema")
        if type(self.video) is not DomainMaskExtent or type(self.audio) is not DomainMaskExtent:
            raise MaskedAVContinuationError("mask_plan_domain_type")
        _fingerprint(self.descriptor_fingerprint, "descriptor")
        _fingerprint(self.descriptor_authority_fingerprint, "descriptor_authority")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "video": self.video.to_wire(),
            "audio": self.audio.to_wire(),
            "descriptor_fingerprint": self.descriptor_fingerprint,
            "descriptor_authority_fingerprint": self.descriptor_authority_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def plan_nested_av_mask(
    *,
    descriptor: JointAVLatentDescriptor,
    video_generate_from: int,
    audio_generate_from: int,
) -> NestedAVMaskPlan:
    """Plan one dual-domain mask on the descriptor's exact grids, refusing any drift.

    The descriptor's temporal extents must equal the M20-01 grid arithmetic for its declared
    frame count -- a checkpoint whose latent extents do not match the declared grid cannot be
    masked by declaration, only misdescribed, and this refuses instead.
    """

    if type(descriptor) is not JointAVLatentDescriptor:
        raise MaskedAVContinuationError("descriptor_type")
    video_length = descriptor.video.shape[2]
    audio_length = descriptor.audio.shape[3]
    if video_length != video_latent_length(descriptor.requested_frames):
        raise MaskedAVContinuationError("grid_mismatch:video")
    if audio_length != nominal_audio_latent_length(descriptor.requested_frames):
        raise MaskedAVContinuationError("grid_mismatch:audio")
    return NestedAVMaskPlan(
        video=DomainMaskExtent(length=video_length, generate_from=video_generate_from),
        audio=DomainMaskExtent(length=audio_length, generate_from=audio_generate_from),
        descriptor_fingerprint=descriptor.fingerprint,
        descriptor_authority_fingerprint=descriptor.authority_fingerprint,
    )


@dataclass(frozen=True, slots=True)
class MaskedContinuationDecision:
    """One masked-continuation admission outcome; admitted exactly or classified refusal."""

    admitted: bool
    reason_codes: tuple[str, ...]
    mask_plan_fingerprint: str
    checkpoint_receipt_fingerprint: str
    authority_fingerprint: str
    descriptor_authority_fingerprint: str
    resume_decision_fingerprint: str
    crop_verdict_reason: str
    schema: str = MASKED_CONTINUATION_DECISION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MASKED_CONTINUATION_DECISION_SCHEMA:
            raise MaskedAVContinuationError("unsupported_decision_schema")
        if type(self.admitted) is not bool:
            raise MaskedAVContinuationError("decision_admitted")
        if type(self.reason_codes) is not tuple:
            raise MaskedAVContinuationError("decision_reason_codes")
        for reason in self.reason_codes:
            _identifier(reason, "reason_code")
        if self.admitted and self.reason_codes:
            raise MaskedAVContinuationError("admitted_with_reasons")
        if not self.admitted and not self.reason_codes:
            raise MaskedAVContinuationError("refused_without_reasons")
        _fingerprint(self.mask_plan_fingerprint, "mask_plan")
        _fingerprint(self.checkpoint_receipt_fingerprint, "checkpoint_receipt")
        _fingerprint(self.authority_fingerprint, "authority")
        _fingerprint(self.descriptor_authority_fingerprint, "descriptor_authority")
        _fingerprint(self.resume_decision_fingerprint, "resume_decision")
        _identifier(self.crop_verdict_reason, "crop_verdict_reason")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "admitted": self.admitted,
            "reason_codes": list(self.reason_codes),
            "mask_plan_fingerprint": self.mask_plan_fingerprint,
            "checkpoint_receipt_fingerprint": self.checkpoint_receipt_fingerprint,
            "authority_fingerprint": self.authority_fingerprint,
            "descriptor_authority_fingerprint": self.descriptor_authority_fingerprint,
            "resume_decision_fingerprint": self.resume_decision_fingerprint,
            "crop_verdict_reason": self.crop_verdict_reason,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def admit_masked_av_continuation(
    *,
    authority: MaskedAVAuthority,
    descriptor_authority: JointAVLatentAuthority,
    mask_plan: NestedAVMaskPlan,
    resume_decision: LatentResumeDecision,
    receipt: JointAVLatentReceipt,
    crop_plan: CropPlan,
) -> MaskedContinuationDecision:
    """Admit one masked continuation; full binding across every input or classified refusal.

    Type violations raise.  Everything else accumulates one reason per failed binding: the
    resume decision must itself be admitted, the receipt must be the one that decision
    admitted, the mask plan must be planned against that receipt's descriptor, every party's
    claimed descriptor authority must be the real object handed in (never each other's
    claims), the mask and descriptor authorities must be projections of the same accepted
    profile over the same subject, and the frame-level accounting must conserve the target
    interval exactly (the M20-01 rule, consumed, never copied).
    """

    if type(authority) is not MaskedAVAuthority:
        raise MaskedAVContinuationError("authority_type")
    if type(descriptor_authority) is not JointAVLatentAuthority:
        raise MaskedAVContinuationError("descriptor_authority_type")
    if type(mask_plan) is not NestedAVMaskPlan:
        raise MaskedAVContinuationError("mask_plan_type")
    if type(resume_decision) is not LatentResumeDecision:
        raise MaskedAVContinuationError("resume_decision_type")
    if type(receipt) is not JointAVLatentReceipt:
        raise MaskedAVContinuationError("receipt_type")
    if type(crop_plan) is not CropPlan:
        raise MaskedAVContinuationError("crop_plan_type")

    reasons: list[str] = []
    if authority.subject_identity != descriptor_authority.subject_identity:
        reasons.append("authority_subject_mismatch")
    if authority.profile_fingerprint != descriptor_authority.profile_fingerprint:
        reasons.append("authority_profile_mismatch")
    if not resume_decision.admitted:
        reasons.append("resume_not_admitted")
    if receipt.fingerprint != resume_decision.checkpoint_receipt_fingerprint:
        reasons.append("checkpoint_binding_mismatch")
    if mask_plan.descriptor_fingerprint != receipt.descriptor.fingerprint:
        reasons.append("descriptor_binding_mismatch")
    if (
        mask_plan.descriptor_authority_fingerprint != descriptor_authority.fingerprint
        or resume_decision.descriptor_authority_fingerprint != descriptor_authority.fingerprint
        or receipt.descriptor.authority_fingerprint != descriptor_authority.fingerprint
    ):
        reasons.append("authority_binding_mismatch")
    verdict = check_crop_conservation(crop_plan)
    if not verdict.conserved:
        reasons.append(f"crop_not_conserved:{verdict.reason_code}")
    if crop_plan.produced_frames != receipt.descriptor.requested_frames:
        reasons.append("extent_accounting_mismatch")

    admitted = not reasons
    return MaskedContinuationDecision(
        admitted=admitted,
        reason_codes=tuple(reasons),
        mask_plan_fingerprint=mask_plan.fingerprint,
        checkpoint_receipt_fingerprint=receipt.fingerprint,
        authority_fingerprint=authority.fingerprint,
        descriptor_authority_fingerprint=descriptor_authority.fingerprint,
        resume_decision_fingerprint=resume_decision.fingerprint,
        crop_verdict_reason=verdict.reason_code,
    )


__all__ = [
    "MASKED_AV_AUTHORITY_SCHEMA",
    "MASKED_AV_CAPABILITY",
    "MASKED_CONTINUATION_DECISION_SCHEMA",
    "MASK_GENERATE",
    "MASK_PRESERVE",
    "NESTED_AV_MASK_PLAN_SCHEMA",
    "DomainMaskExtent",
    "MaskedAVAuthority",
    "MaskedAVContinuationError",
    "MaskedContinuationDecision",
    "NestedAVMaskPlan",
    "admit_masked_av_continuation",
    "build_masked_av_authority",
    "plan_nested_av_mask",
]
