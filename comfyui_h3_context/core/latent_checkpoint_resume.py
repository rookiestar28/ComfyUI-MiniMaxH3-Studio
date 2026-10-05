"""Exact latent checkpoint resume admission and the qualified split composition (M20-05).

Downstream of the M19-08 row `completed_boundary_resume = supported`
(`structural_resume_executes_no_bitexact_oracle`).  Two measured facts shape everything here.
The host is nondeterministic end to end -- identical single passes do not reproduce
themselves, and VAE decode alone does not either -- so nothing in this module compares
outputs; admission is identity equality over fingerprints and declarations, never over bytes
the host produced.  And the qualified resume mechanism is the split schedule: ``SplitSigmas``
at the completed step plus a stage-2 ``SamplerCustomAdvanced`` under ``DisableNoise``
consuming the checkpointed trajectory latent, which the row measured to deliver the exact
requested frame count.

A checkpoint's boundary is DECLARED, not inferred.  The writer fingerprints a
:class:`LatentCheckpointBoundary` and stores it as the M20-04 receipt's
``execution_fingerprint`` -- the execution state the artifact embodies.  Admission recomputes
the expected fingerprint from the caller's declaration and refuses on any difference, so a
partially-written or undeclared step state can never be admitted by looking plausible.

Every mismatch is classified, one reason per identity domain, and accumulated; there is no
partial-credit admission and no fallback.  Refusal feeds the accepted recompute engine
through the ``extend_recompute_plan`` origin shape rather than a parallel mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass

from .canonical import canonical_fingerprint
from .fingerprint_domain import IdentityDomain, domain_fingerprint
from .joint_av_latent import JointAVLatentAuthority, JointAVLatentReceipt
from .length import MAX_FRAME_COUNT
from .segment_artifacts import ArtifactLifecycleState
from .temporal_profile import CapabilityStatus, TemporalCapabilityProfile

LATENT_RESUME_CAPABILITY = "completed_boundary_resume"
LATENT_RESUME_AUTHORITY_SCHEMA = "h3.context.latent_resume_authority.v1"
LATENT_CHECKPOINT_BOUNDARY_SCHEMA = "h3.context.latent_checkpoint_boundary.v1"
LATENT_RESUME_DECISION_SCHEMA = "h3.context.latent_resume_decision.v1"
LATENT_RESUME_COMPOSITION_SCHEMA = "h3.context.latent_resume_composition.v1"

MAX_RESUME_TOTAL_STEPS = 10_000
MAX_RESUME_SEED = 2**64 - 1

#: The sampler topology exactly as the M19-08 row qualified it.  These are host node class
#: names; pinning them here keeps the composition builder deterministic and refuses silent
#: topology drift, the same way ``native_h3`` pins the conditioning node names.
QUALIFIED_SCHEDULER_NODE = "BasicScheduler"
QUALIFIED_SPLIT_NODE = "SplitSigmas"
QUALIFIED_SAMPLER_NODE = "SamplerCustomAdvanced"
QUALIFIED_DISABLE_NOISE_NODE = "DisableNoise"
QUALIFIED_RANDOM_NOISE_NODE = "RandomNoise"

_IDENTIFIER_MAX = 128
_FINGERPRINT_PREFIX = "sha256:"


class LatentResumeError(ValueError):
    """Raised when resume admission or composition cannot preserve exact identity."""


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > _IDENTIFIER_MAX:
        raise LatentResumeError(f"bounded_identifier:{field}")
    for char in value:
        if not (char.isascii() and (char.isalnum() or char in "_.:-")):
            raise LatentResumeError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value.startswith(_FINGERPRINT_PREFIX)
        or len(value) != len(_FINGERPRINT_PREFIX) + 64
        or any(char not in "0123456789abcdef" for char in value[len(_FINGERPRINT_PREFIX) :])
    ):
        raise LatentResumeError(f"sha256_fingerprint:{field}")
    return value


def _optional_fingerprint(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field)


def _bounded_integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise LatentResumeError(f"bounded_integer:{field}")
    return value


@dataclass(frozen=True, slots=True)
class LatentResumeAuthority:
    """The frozen consumption of the accepted profile's supported resume row.

    Same shape and same refusal as the M20-04 descriptor authority: the projection exists
    only in the ``SUPPORTED`` state, and capability absence is a construction refusal, not a
    value with a flag.
    """

    subject_identity: str
    reason_code: str
    profile_fingerprint: str
    schema: str = LATENT_RESUME_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != LATENT_RESUME_AUTHORITY_SCHEMA:
            raise LatentResumeError("unsupported_authority_schema")
        _fingerprint(self.subject_identity, "subject_identity")
        _identifier(self.reason_code, "reason_code")
        _fingerprint(self.profile_fingerprint, "profile_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capability": LATENT_RESUME_CAPABILITY,
            "subject_identity": self.subject_identity,
            "reason_code": self.reason_code,
            "profile_fingerprint": self.profile_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return domain_fingerprint(IdentityDomain.SEMANTIC, self.to_wire()).digest


def build_latent_resume_authority(profile: TemporalCapabilityProfile) -> LatentResumeAuthority:
    """Freeze the accepted profile's resume claim, refusing every unqualified state."""

    if not isinstance(profile, TemporalCapabilityProfile):
        raise LatentResumeError("authority_requires_temporal_profile")
    claim = profile.capability_claim(LATENT_RESUME_CAPABILITY)
    if claim.status is not CapabilityStatus.SUPPORTED:
        raise LatentResumeError("resume_row_not_supported")
    if profile.subject_identity is None:
        raise LatentResumeError("resume_row_without_subject_identity")
    return LatentResumeAuthority(
        subject_identity=profile.subject_identity,
        reason_code=claim.reason_code,
        profile_fingerprint=canonical_fingerprint(profile.to_wire()),
    )


@dataclass(frozen=True, slots=True)
class LatentCheckpointBoundary:
    """The declared completed boundary a checkpoint stands at.

    ``completed_steps`` is the number of sampling steps the writer claims are done out of
    ``total_steps``; the checkpoint's latent spans the full temporal extent
    (``requested_frames``) in a partially-denoised state.  The declaration's canonical
    fingerprint is stored as the M20-04 receipt's ``execution_fingerprint``, which is what
    makes the boundary declared rather than inferred: admission recomputes it and any
    difference -- a different split, a different schedule length, a different seed, a
    different extent -- is a refusal, not a negotiation.
    """

    total_steps: int
    completed_steps: int
    seed: int
    requested_frames: int
    schema: str = LATENT_CHECKPOINT_BOUNDARY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != LATENT_CHECKPOINT_BOUNDARY_SCHEMA:
            raise LatentResumeError("unsupported_boundary_schema")
        _bounded_integer(self.total_steps, "total_steps", 1, MAX_RESUME_TOTAL_STEPS)
        _bounded_integer(self.completed_steps, "completed_steps", 1, self.total_steps)
        _bounded_integer(self.seed, "seed", 0, MAX_RESUME_SEED)
        _bounded_integer(self.requested_frames, "requested_frames", 1, MAX_FRAME_COUNT)

    @property
    def remaining_steps(self) -> int:
        return self.total_steps - self.completed_steps

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "seed": self.seed,
            "requested_frames": self.requested_frames,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


@dataclass(frozen=True, slots=True)
class LatentResumeRequest:
    """The target identity a resume must match exactly, one field per domain.

    The stored transaction fingerprint is provenance -- a resume legitimately runs in a new
    transaction -- so it is deliberately absent here.  Everything that decides whether the
    checkpointed work IS the target's work is present: the descriptor authority (profile and
    subject), the declared boundary, model, runtime, settings, source and predecessor.
    """

    descriptor_authority_fingerprint: str
    boundary: LatentCheckpointBoundary
    model_fingerprint: str
    runtime_fingerprint: str
    settings_fingerprint: str
    source_id: str
    predecessor_artifact_fingerprint: str | None
    now_ms: int

    def __post_init__(self) -> None:
        _fingerprint(self.descriptor_authority_fingerprint, "descriptor_authority")
        if type(self.boundary) is not LatentCheckpointBoundary:
            raise LatentResumeError("request_boundary_type")
        _fingerprint(self.model_fingerprint, "model")
        _fingerprint(self.runtime_fingerprint, "runtime")
        _fingerprint(self.settings_fingerprint, "settings")
        _identifier(self.source_id, "source_id")
        _optional_fingerprint(self.predecessor_artifact_fingerprint, "predecessor_artifact")
        _bounded_integer(self.now_ms, "now_ms", 1, 9_999_999_999_999)


@dataclass(frozen=True, slots=True)
class LatentResumeDecision:
    """One admission outcome: admitted exactly, or refused with every classified reason.

    The decision is itself provenance: its wire carries the checkpoint's receipt fingerprint
    and the skipped-work claim, so a generation plan that reuses a checkpoint records what it
    reused and what it skipped without mutating the stored artifact.
    """

    admitted: bool
    reason_codes: tuple[str, ...]
    checkpoint_receipt_fingerprint: str
    checkpoint_transaction_fingerprint: str
    boundary_fingerprint: str
    resume_authority_fingerprint: str
    descriptor_authority_fingerprint: str
    skipped_steps: int
    remaining_steps: int
    schema: str = LATENT_RESUME_DECISION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != LATENT_RESUME_DECISION_SCHEMA:
            raise LatentResumeError("unsupported_decision_schema")
        if type(self.admitted) is not bool:
            raise LatentResumeError("decision_admitted")
        if type(self.reason_codes) is not tuple:
            raise LatentResumeError("decision_reason_codes")
        for reason in self.reason_codes:
            _identifier(reason, "reason_code")
        if self.admitted and self.reason_codes:
            raise LatentResumeError("admitted_with_reasons")
        if not self.admitted and not self.reason_codes:
            raise LatentResumeError("refused_without_reasons")
        _fingerprint(self.checkpoint_receipt_fingerprint, "checkpoint_receipt")
        _fingerprint(self.checkpoint_transaction_fingerprint, "checkpoint_transaction")
        _fingerprint(self.boundary_fingerprint, "boundary")
        _fingerprint(self.resume_authority_fingerprint, "resume_authority")
        _fingerprint(self.descriptor_authority_fingerprint, "descriptor_authority")
        _bounded_integer(self.skipped_steps, "skipped_steps", 0, MAX_RESUME_TOTAL_STEPS)
        _bounded_integer(self.remaining_steps, "remaining_steps", 0, MAX_RESUME_TOTAL_STEPS)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "admitted": self.admitted,
            "reason_codes": list(self.reason_codes),
            "checkpoint_receipt_fingerprint": self.checkpoint_receipt_fingerprint,
            "checkpoint_transaction_fingerprint": self.checkpoint_transaction_fingerprint,
            "boundary_fingerprint": self.boundary_fingerprint,
            "resume_authority_fingerprint": self.resume_authority_fingerprint,
            "descriptor_authority_fingerprint": self.descriptor_authority_fingerprint,
            "skipped_steps": self.skipped_steps,
            "remaining_steps": self.remaining_steps,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def admit_latent_checkpoint_resume(
    *,
    authority: LatentResumeAuthority,
    descriptor_authority: JointAVLatentAuthority,
    request: LatentResumeRequest,
    receipt: JointAVLatentReceipt,
) -> LatentResumeDecision:
    """Decide one checkpoint against one target identity; full equality or classified refusal.

    Type violations raise -- a wrong object is a caller bug, not a mismatch.  Everything else
    accumulates one reason per failed domain so a refusal explains every divergence at once.
    The two authorities must be projections of the same accepted profile over the same
    subject; admission binds itself to the descriptor authority it was handed, so the
    receipt's claimed authority and the request's declared authority are both checked
    against a real object rather than against each other's claims.
    """

    if type(authority) is not LatentResumeAuthority:
        raise LatentResumeError("authority_type")
    if type(descriptor_authority) is not JointAVLatentAuthority:
        raise LatentResumeError("descriptor_authority_type")
    if type(request) is not LatentResumeRequest:
        raise LatentResumeError("request_type")
    if type(receipt) is not JointAVLatentReceipt:
        raise LatentResumeError("receipt_type")

    reasons: list[str] = []
    if authority.subject_identity != descriptor_authority.subject_identity:
        reasons.append("authority_subject_mismatch")
    if authority.profile_fingerprint != descriptor_authority.profile_fingerprint:
        reasons.append("authority_profile_mismatch")
    if receipt.state is not ArtifactLifecycleState.COMPLETE:
        reasons.append("receipt_not_complete")
    if receipt.expires_at_ms <= request.now_ms:
        reasons.append("receipt_expired")
    if (
        receipt.descriptor.authority_fingerprint != descriptor_authority.fingerprint
        or request.descriptor_authority_fingerprint != descriptor_authority.fingerprint
    ):
        reasons.append("descriptor_authority_mismatch")
    if receipt.execution_fingerprint != request.boundary.fingerprint:
        reasons.append("boundary_mismatch")
    if request.boundary.remaining_steps == 0:
        reasons.append("boundary_terminal")
    if receipt.descriptor.completed_frames != receipt.descriptor.requested_frames:
        # A step checkpoint spans the full temporal extent in a partially-denoised state;
        # partial-extent latents belong to the continuation items, not to step resume.
        reasons.append("latent_extent_partial")
    if receipt.descriptor.requested_frames != request.boundary.requested_frames:
        reasons.append("extent_mismatch")
    if receipt.model_fingerprint != request.model_fingerprint:
        reasons.append("model_mismatch")
    if receipt.runtime_fingerprint != request.runtime_fingerprint:
        reasons.append("runtime_mismatch")
    if receipt.settings_fingerprint != request.settings_fingerprint:
        reasons.append("settings_mismatch")
    if receipt.source_id != request.source_id:
        reasons.append("source_mismatch")
    if receipt.predecessor_artifact_fingerprint != request.predecessor_artifact_fingerprint:
        reasons.append("predecessor_mismatch")

    admitted = not reasons
    return LatentResumeDecision(
        admitted=admitted,
        reason_codes=tuple(reasons),
        checkpoint_receipt_fingerprint=receipt.fingerprint,
        checkpoint_transaction_fingerprint=receipt.transaction_fingerprint,
        boundary_fingerprint=request.boundary.fingerprint,
        resume_authority_fingerprint=authority.fingerprint,
        descriptor_authority_fingerprint=descriptor_authority.fingerprint,
        skipped_steps=request.boundary.completed_steps if admitted else 0,
        remaining_steps=request.boundary.remaining_steps if admitted else 0,
    )


def recompute_origin_for_refusal(
    decision: LatentResumeDecision,
    *,
    segment_id: str,
) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    """Map a refusal to the ``extend_recompute_plan`` forced-dirty origin shape.

    An admitted decision contributes nothing; a refusal marks exactly the requesting segment
    dirty with the decision's own reason codes.  The recompute engine keeps graph
    propagation; this helper never invents traversal.
    """

    if type(decision) is not LatentResumeDecision:
        raise LatentResumeError("decision_type")
    _identifier(segment_id, "segment_id")
    if decision.admitted:
        return (), ()
    return (segment_id,), ((segment_id, decision.reason_codes),)


@dataclass(frozen=True, slots=True)
class ResumeCompositionNodeRefs:
    """The injection points a resume fragment wires into: existing graph node ids.

    The fragment never invents model, conditioning or latent sources; the caller names the
    nodes that own them.  Ids are bounded identifiers, not paths.
    """

    model_node: str
    guider_node: str
    sampler_select_node: str
    latent_source_node: str
    model_output_index: int = 0
    guider_output_index: int = 0
    sampler_output_index: int = 0
    latent_output_index: int = 0

    def __post_init__(self) -> None:
        _identifier(self.model_node, "model_node")
        _identifier(self.guider_node, "guider_node")
        _identifier(self.sampler_select_node, "sampler_select_node")
        _identifier(self.latent_source_node, "latent_source_node")
        for name, value in (
            ("model_output_index", self.model_output_index),
            ("guider_output_index", self.guider_output_index),
            ("sampler_output_index", self.sampler_output_index),
            ("latent_output_index", self.latent_output_index),
        ):
            _bounded_integer(value, name, 0, 64)


def build_resume_composition(
    boundary: LatentCheckpointBoundary,
    refs: ResumeCompositionNodeRefs,
    *,
    scheduler_name: str,
    denoise: str = "1.0",
) -> dict[str, dict[str, object]]:
    """Render the qualified stage-2-only fragment as a deterministic prompt-graph dict.

    Exactly the topology the M19-08 row measured: the full schedule is built and split at the
    declared completed step, stage 2 consumes the high tail under ``DisableNoise``, and the
    checkpoint latent enters as ``latent_image``.  A terminal boundary refuses -- there is no
    zero-step composition.

    The returned node ids (``resume_sigmas``, ``resume_split``, ``resume_noise``,
    ``resume_stage2``) are namespaced to avoid colliding with caller graphs; the caller
    merges the fragment into its prompt dict and reads the sampled output from
    ``resume_stage2`` output 0.
    """

    if type(boundary) is not LatentCheckpointBoundary:
        raise LatentResumeError("boundary_type")
    if type(refs) is not ResumeCompositionNodeRefs:
        raise LatentResumeError("composition_refs_type")
    if boundary.remaining_steps == 0:
        raise LatentResumeError("boundary_terminal")
    _identifier(scheduler_name, "scheduler_name")
    if type(denoise) is not str or denoise not in {"1.0", "1"}:
        # The qualified row ran the full schedule at denoise 1.0; any other value is an
        # unqualified variation this builder refuses rather than silently renders.
        raise LatentResumeError("unqualified_denoise")
    return {
        "resume_sigmas": {
            "class_type": QUALIFIED_SCHEDULER_NODE,
            "inputs": {
                "model": [refs.model_node, refs.model_output_index],
                "scheduler": scheduler_name,
                "steps": boundary.total_steps,
                "denoise": float(denoise),
            },
        },
        "resume_split": {
            "class_type": QUALIFIED_SPLIT_NODE,
            "inputs": {
                "sigmas": ["resume_sigmas", 0],
                "step": boundary.completed_steps,
            },
        },
        "resume_noise": {
            "class_type": QUALIFIED_DISABLE_NOISE_NODE,
            "inputs": {},
        },
        "resume_stage2": {
            "class_type": QUALIFIED_SAMPLER_NODE,
            "inputs": {
                "noise": ["resume_noise", 0],
                "guider": [refs.guider_node, refs.guider_output_index],
                "sampler": [refs.sampler_select_node, refs.sampler_output_index],
                "sigmas": ["resume_split", 1],
                "latent_image": [refs.latent_source_node, refs.latent_output_index],
            },
        },
    }


__all__ = [
    "LATENT_CHECKPOINT_BOUNDARY_SCHEMA",
    "LATENT_RESUME_AUTHORITY_SCHEMA",
    "LATENT_RESUME_CAPABILITY",
    "LATENT_RESUME_COMPOSITION_SCHEMA",
    "LATENT_RESUME_DECISION_SCHEMA",
    "LatentCheckpointBoundary",
    "LatentResumeAuthority",
    "LatentResumeDecision",
    "LatentResumeError",
    "LatentResumeRequest",
    "MAX_RESUME_SEED",
    "MAX_RESUME_TOTAL_STEPS",
    "QUALIFIED_DISABLE_NOISE_NODE",
    "QUALIFIED_RANDOM_NOISE_NODE",
    "QUALIFIED_SAMPLER_NODE",
    "QUALIFIED_SCHEDULER_NODE",
    "QUALIFIED_SPLIT_NODE",
    "ResumeCompositionNodeRefs",
    "admit_latent_checkpoint_resume",
    "build_latent_resume_authority",
    "build_resume_composition",
    "recompute_origin_for_refusal",
]
