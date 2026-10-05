"""M20-01: the H3 temporal capability profile, over two declared provenance classes.

Later timeline, latent-continuity and reconstruction items need one place to ask what the model and
its adapter actually do with time.  This module is that place, and the single decision that shapes
it is that the answers do not all come from the same kind of evidence:

* an **adapter fact** is what the native node does with a submitted length.  It is readable from a
  pinned official source with no host, no weights and no execution, so it is bound to that source
  and to nothing else;
* a **model capability claim** asserts what this model can *do* -- the trained range, whether a
  continuation or bridge mode exists at all.  Nothing instantiates one except an accepted `M19-07`
  row with status `supported`.

Every declared member carries its class as a typed field, so a consumer branches on it and a test
asserts it.  A member with no class is a defect, and no adapter fact may be presented as a qualified
capability -- which is the failure this split exists to prevent, because the adapter's arithmetic is
exactly the material that looks authoritative enough to be mistaken for a capability.

**This module is not a second alignment authority.**  `M17-25` made
:mod:`comfyui_h3_context.core.length` the repository's single owner of the frame rate, the
`5 + 17k` video-run lattice, the accepted and trained ranges and the seconds-to-frames convention.
Everything here that touches those is imported from it and composed, never restated; the lattice
step and offset appear as symbols, never as literals, so this file cannot drift from the authority
even by accident.  What this module adds is the half `core.length` does not own: the audio latent
grid, the joint audiovisual boundary, the target-latent join, and the lookahead/crop constraint.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from . import length
from .contracts import EvidenceLevel
from .errors import ContractValidationError
from .fingerprint_domain import DomainFingerprint, IdentityDomain, domain_fingerprint

TEMPORAL_PROFILE_SCHEMA = "h3-context-temporal-profile/1"
CURRENT_TEMPORAL_PROFILE_VERSION = 1

#: The audio latent grid, in steps per second of delivered video.  This is the one temporal rate the
#: repository did not already own: `core.length` is entirely video-side, and `M19-07` derived the
#: audio side from the pinned source without writing it into the product package, precisely so that
#: this item would place it once.
AUDIO_LATENT_FPS = 40

#: The exact rational relation between the two grids, as a ratio rather than a float.  Every
#: audio-side decision below is integer arithmetic over this value, so requalifying either rate
#: moves the derived facts with it instead of leaving a hard-coded table behind.
AUDIO_LATENT_RATIO = Fraction(AUDIO_LATENT_FPS, length.FPS)

#: The frame period at which the audio extent lands on a whole step.  Derived as the ratio's
#: denominator -- with 40 over 24 that reduces to 5/3, so every third frame is exact -- and never
#: enumerated, because an enumerated subset would survive a rate change while being wrong.
EXACT_AUDIO_PERIOD_FRAMES = AUDIO_LATENT_RATIO.denominator

#: The video latent extent of the shortest producible run, and the extra steps each further lattice
#: step costs.  The adapter publishes the extent as a branch on the lattice; the two values that are
#: *not* the lattice are named here and everything else is composed from `core.length`.
VIDEO_LATENT_BASE_STEPS = 2
VIDEO_LATENT_STEPS_PER_LATTICE_STEP = 5

#: The grid a real context extent must land on, in frames: the smallest extent that is a whole
#: number of steps on *both* latent grids at once.  It is the least common multiple of the video
#: lattice step and the exact audio period -- 51 frames -- and it is derived, not chosen.  The
#: consequence that makes it usable is arithmetic: a producible target plus context on this grid is
#: itself producible, because the grid is a whole multiple of the lattice step.
CONTEXT_EXTENT_GRID_FRAMES = math.lcm(length.LATTICE_STEP, EXACT_AUDIO_PERIOD_FRAMES)

#: The minimum real context extent on either side of a target interval: one grid unit.  Anything
#: smaller cannot be a whole number of steps on both grids, so it is not expressible as latent
#: context however the surrounding job is planned.  This is a floor on the *representation*; how
#: much lookahead any particular job requests belongs to `M20-06` and `M20-07`.
MINIMUM_CONTEXT_EXTENT_FRAMES = CONTEXT_EXTENT_GRID_FRAMES


class TemporalProfileError(ContractValidationError):
    """A temporal question was asked in a way the profile refuses to answer."""


def _require_frame_count(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TemporalProfileError(f"{field_name}_must_be_a_non_negative_integer")
    return value


class ProvenanceClass(str, Enum):
    """Which kind of evidence a profile member rests on.

    This is a typed field on every member rather than a naming convention, because the whole point
    is that a consumer must be able to branch on it: an adapter fact is usable immediately and
    claims nothing about the model, while a capability claim is only usable once an accepted row
    instantiates it.
    """

    #: Readable from a pinned official source without host, model or execution.  Ungated.
    ADAPTER_FACT = "adapter_fact"
    #: An assertion about what this model can do.  Instantiated only by an accepted `supported` row.
    MODEL_CAPABILITY = "model_capability"


class CapabilityStatus(str, Enum):
    """The `M19-07` row vocabulary, preserved exactly rather than translated.

    `unqualified` is the member that earns the enum.  It means "we have not established this", which
    a consumer must be able to tell apart from `unsupported` -- an exhaustively evidenced absence
    that sends a design back to replanning -- and from the member never having been declared at all.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNQUALIFIED = "unqualified"
    RETIRED = "retired"


class AudioTargetPolicy(str, Enum):
    """How the audio latent extent for a run is decided."""

    #: The mapping is integral; the extent is the exact value and needs no external authority.
    EXACT = "exact"
    #: The mapping is non-integral; only the adapter's own target length is authoritative.
    AUTHORITATIVE_TARGET = "authoritative_target"


@dataclass(frozen=True, slots=True)
class PinnedSource:
    """The exact official artifact one adapter fact was read from.

    Identity is content and revision, never a location: the label is the artifact's public
    upstream-relative name, and where it happens to sit on a maintainer's disk is a private path
    that never enters a shipped record.

    `digest` and `byte_length` are optional because the two pinned sources are pinned differently
    and pretending otherwise would be an invented fact.  The native node was hashed by `M19-07` and
    carries a content digest; the official templates were pinned by upstream revision, so their
    entry records the revision it actually has rather than a digest nobody computed.
    """

    label: str
    revision: str
    retrieved_on: str
    digest: str | None = None
    byte_length: int | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "label": self.label,
            "revision": self.revision,
            "retrieved_on": self.retrieved_on,
            "digest": self.digest,
            "byte_length": self.byte_length,
        }


#: The native node this repository integrates with, at the exact identity `M19-07` recorded and then
#: reproduced after its distinct review.  The revision is public upstream repository metadata.
NATIVE_NODE_SOURCE = PinnedSource(
    label="comfy_extras/nodes_minimax_h3.py",
    revision="b323a345bbbfb2f3a95b5b73b68eb7919a26515e",  # pragma: allowlist secret
    retrieved_on="2026-08-19",
    digest="sha256:f767df4074b908efb345f5a87c2fd263ba82c12e65bcca932846207cc213e064",
    byte_length=15728,
)

#: The official workflow templates, which own the authored-duration convention `M17-25` adopted.
#: Named separately because one adapter fact -- seconds to frames -- comes from the templates rather
#: than from the node, and a profile recording a single source would be misattributing it.
OFFICIAL_TEMPLATE_SOURCE = PinnedSource(
    label="Comfy-Org/workflow_templates:templates/video_minimax_h3_{t2v,i2v,r2v}.json",
    revision="5097de61ef09fe75466716ac0b200515f5ea078f",  # pragma: allowlist secret
    retrieved_on="2026-08-18",
)


@dataclass(frozen=True, slots=True)
class FrameRange:
    """A closed frame-count interval, with whether its published form is hedged.

    `hedged` is not decoration.  The trained range is published as "approximately 124-362, longer is
    untested", and hardening that into a limit would invent precision the source does not claim.
    """

    minimum: int
    maximum: int
    hedged: bool

    def __post_init__(self) -> None:
        _require_frame_count(self.minimum, "minimum")
        _require_frame_count(self.maximum, "maximum")
        if self.minimum > self.maximum:
            raise TemporalProfileError("frame_range_inverted")

    def contains(self, frame_count: int) -> bool:
        return self.minimum <= frame_count <= self.maximum

    def to_wire(self) -> dict[str, object]:
        return {"minimum": self.minimum, "maximum": self.maximum, "hedged": self.hedged}


@dataclass(frozen=True, slots=True)
class AdapterFact:
    """One fact about the adapter, bound to the source it was read from."""

    name: str
    summary: str
    source: PinnedSource
    evidence_level: EvidenceLevel = EvidenceLevel.OFFICIAL

    @property
    def provenance(self) -> ProvenanceClass:
        return ProvenanceClass.ADAPTER_FACT

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provenance": self.provenance.value,
            "summary": self.summary,
            "evidence_level": self.evidence_level.value,
            "source": self.source.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ModelCapabilityClaim:
    """One assertion about the model, and the accepted row that did or did not instantiate it.

    A claim is always present as a member; what varies is its `status`.  That is the whole reason
    the class exists: dropping an uninstantiated claim would leave a consumer unable to distinguish
    "not established" from "no such capability", and `M19-07` overturned an `unsupported` verdict
    for exactly one of these rows, so "absent" is not an honest state for any of them.

    `evidence_level` is `None` for everything except a qualified claim, and that is deliberate.
    Every member of :class:`~comfyui_h3_context.core.contracts.EvidenceLevel` describes evidence
    that exists, and `experimental` specifically says somebody tried this.  An unqualified
    capability has no evidence at all, so grading it would invent the detail this project's rules
    exist to keep out.  The honest value is the absence of one, and `status` already carries what
    is known.
    """

    name: str
    summary: str
    row: str
    status: CapabilityStatus
    reason_code: str
    evidence_level: EvidenceLevel | None

    @property
    def provenance(self) -> ProvenanceClass:
        return ProvenanceClass.MODEL_CAPABILITY

    @property
    def qualified(self) -> bool:
        return self.status is CapabilityStatus.SUPPORTED

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provenance": self.provenance.value,
            "summary": self.summary,
            "row": self.row,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "evidence_level": None if self.evidence_level is None else self.evidence_level.value,
        }


@dataclass(frozen=True, slots=True)
class TrainedRangeReport:
    """The trained range as a member that is always present and sometimes uninstantiated.

    The invariant enforced here is the one `AC-M20-01-09` turns on: a value exists exactly when the
    status is `supported`.  A consumer cannot read an unqualified trained range as "no trained
    bound", because there is no shape in which the range is missing and the member is absent.
    """

    status: CapabilityStatus
    reason_code: str
    value: FrameRange | None = None

    def __post_init__(self) -> None:
        qualified = self.status is CapabilityStatus.SUPPORTED
        if qualified and self.value is None:
            raise TemporalProfileError("qualified_trained_range_without_value")
        if not qualified and self.value is not None:
            raise TemporalProfileError("unqualified_trained_range_carries_value")

    def to_wire(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "value": None if self.value is None else self.value.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class QualifiedRow:
    """One row of an accepted `M19-07` matrix, in its content-free projection."""

    row: str
    status: CapabilityStatus
    reason_code: str
    consumer: str

    def to_wire(self) -> dict[str, object]:
        return {
            "row": self.row,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "consumer": self.consumer,
        }


@dataclass(frozen=True, slots=True)
class AcceptedQualification:
    """The accepted `M19-07` evidence this profile may join, and nothing else.

    Live probes are not replayed and no other successor row is consulted: the projection carries
    statuses, reason codes and one opaque subject identity, which is everything a binding needs and
    nothing that would disclose a path, a media value or a credential.
    """

    subject_identity: str
    rows: tuple[QualifiedRow, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.subject_identity, str) or not self.subject_identity:
            raise TemporalProfileError("accepted_qualification_without_subject_identity")
        if not isinstance(self.rows, tuple) or not self.rows:
            raise TemporalProfileError("accepted_qualification_without_rows")
        names = [item.row for item in self.rows]
        if len(set(names)) != len(names):
            raise TemporalProfileError("accepted_qualification_has_duplicate_rows")

    def row(self, name: str) -> QualifiedRow | None:
        for item in self.rows:
            if item.row == name:
                return item
        return None


@dataclass(frozen=True, slots=True)
class RunAlignment:
    """One requested frame count, aligned -- or refused with a reason, never silently.

    There is deliberately no variant of this that carries the aligned value alone.  `requested`,
    `frame_count` and `snapped` are one object, so a surface that stores the authoritative length
    has the record that it was moved sitting beside it, which is the user decision this item
    carries: snapping is permitted, silence is not.
    """

    requested_frames: int
    producible: bool
    frame_count: int | None
    snapped: bool
    refusal: str | None

    def __post_init__(self) -> None:
        if self.producible:
            if self.frame_count is None or self.refusal is not None:
                raise TemporalProfileError("producible_alignment_must_carry_only_a_value")
        else:
            if self.frame_count is not None or not self.refusal:
                raise TemporalProfileError("refused_alignment_must_carry_only_a_reason")
            if self.snapped:
                raise TemporalProfileError("refused_alignment_cannot_be_snapped")

    def require(self) -> int:
        """Return the aligned value, raising the recorded reason when there is none."""

        if self.frame_count is None:
            raise TemporalProfileError(self.refusal or "frame_count_not_producible")
        return self.frame_count

    def to_wire(self) -> dict[str, object]:
        return {
            "requested_frames": self.requested_frames,
            "producible": self.producible,
            "frame_count": self.frame_count,
            "snapped": self.snapped,
            "refusal": self.refusal,
        }


@dataclass(frozen=True, slots=True)
class AudioLatentTarget:
    """The admitted audio latent extent for one producible run, and how it was decided."""

    frame_count: int
    audio_latent_length: int
    exact: bool
    policy: AudioTargetPolicy
    nominal: int
    direction: str

    def to_wire(self) -> dict[str, object]:
        return {
            "frame_count": self.frame_count,
            "audio_latent_length": self.audio_latent_length,
            "exact": self.exact,
            "policy": self.policy.value,
            "nominal": self.nominal,
            "direction": self.direction,
        }


@dataclass(frozen=True, slots=True)
class CropPlan:
    """The shape of one continuation, stated as frames, before any of it is executed.

    This item owns the *constraint* only.  How much lookahead a given job asks for, how the crop is
    performed and how it is recorded in a boundary receipt belong to `M20-06` and `M20-07`; they
    consume :func:`check_crop_conservation` rather than holding their own copy of the rule.
    """

    requested_target_frames: int
    leading_context_frames: int
    trailing_context_frames: int
    produced_frames: int
    leading_crop_frames: int
    trailing_crop_frames: int


@dataclass(frozen=True, slots=True)
class CropVerdict:
    """Whether a plan conserves the target interval exactly, and if not, which way it failed."""

    conserved: bool
    reason_code: str
    delivered_target_frames: int

    def to_wire(self) -> dict[str, object]:
        return {
            "conserved": self.conserved,
            "reason_code": self.reason_code,
            "delivered_target_frames": self.delivered_target_frames,
        }


def align_run(requested_frames: int) -> RunAlignment:
    """Align one requested frame count.  Total over the accepted range.

    Every value the adapter accepts gets an answer here -- either a producible length with its
    snapped marker, or a named refusal.  The refusal is not a technicality: `MAX_FRAME_COUNT` is the
    node's declared input ceiling while the largest length that can actually be produced is
    `MAX_PRODUCIBLE_FRAME_COUNT`, so the counts between them are accepted by the input and align
    above it.  Clamping them down would deliver a length nobody asked for.
    """

    _require_frame_count(requested_frames, "requested_frames")
    if requested_frames > length.MAX_FRAME_COUNT:
        return RunAlignment(requested_frames, False, None, False, "above_accepted_range")
    aligned = length.align_frame_count(requested_frames)
    if not length.is_producible(aligned):
        return RunAlignment(requested_frames, False, None, False, "above_producible_ceiling")
    return RunAlignment(requested_frames, True, aligned, aligned != requested_frames, None)


def video_latent_length(frame_count: int) -> int:
    """The video latent extent of a frame count, as the adapter derives it.

    The short-run branch is the adapter's, not a guard added here: the shortest producible run
    occupies two latent steps, and each further lattice step costs five.
    """

    _require_frame_count(frame_count, "frame_count")
    if frame_count <= length.LATTICE_OFFSET:
        return VIDEO_LATENT_BASE_STEPS
    steps = (frame_count - length.LATTICE_OFFSET) // length.LATTICE_STEP
    return steps * VIDEO_LATENT_STEPS_PER_LATTICE_STEP + VIDEO_LATENT_BASE_STEPS


def exact_audio_latent_length(frame_count: int) -> Fraction:
    """The audio latent extent as an exact rational, before any join decision."""

    _require_frame_count(frame_count, "frame_count")
    return frame_count * AUDIO_LATENT_RATIO


def audio_boundary_is_exact(frame_count: int) -> bool:
    """Whether the audio extent lands on a whole latent step.

    Membership is integer arithmetic over the declared rates, never a lookup: the extent is exact
    when the ratio's denominator divides the frame count.  Inside the `5 + 17k` lattice that lands
    on every 51st frame -- 39, 90, 141, 192 -- but the period is a consequence of the rates, so
    requalifying either one moves the subset rather than leaving this predicate wrong.
    """

    return exact_audio_latent_length(frame_count).denominator == 1


def nominal_audio_latent_length(frame_count: int) -> int:
    """The value the declared join rule names for a frame count.

    The rule is round-to-nearest with ties to even, matching the adapter's own rounding.  The tie
    branch is unreachable in practice and that is worth stating rather than relying on: the ratio
    reduces to 5/3, so the fractional part of an extent is only ever 0, 1/3 or 2/3, and no frame
    count lands on a half.  The join is therefore fully determined, and this value is still not
    authoritative on its own -- see :func:`admit_audio_latent_target`.
    """

    exact = exact_audio_latent_length(frame_count)
    floor = exact.numerator // exact.denominator
    remainder = exact - floor
    half = Fraction(1, 2)
    if remainder > half:
        return floor + 1
    if remainder < half:
        return floor
    return floor if floor % 2 == 0 else floor + 1


def admit_audio_latent_target(
    frame_count: int, *, authoritative_target: int | None = None
) -> AudioLatentTarget:
    """Decide the audio latent extent for a producible run, refusing to invent one.

    Two decisions stay separate here, and conflating them is the defect this signature prevents.  A
    run is not rejected for having a non-integral nominal mapping -- most producible runs do, and
    the adapter produces them.  What a non-integral run may not do is have its extent guessed
    downstream: the authoritative target must come from the adapter, and if it disagrees with the
    declared join rule that is drift to report, not a difference to absorb.
    """

    _require_frame_count(frame_count, "frame_count")
    if not length.is_producible(frame_count):
        raise TemporalProfileError("frame_count_not_producible")
    if authoritative_target is not None:
        _require_frame_count(authoritative_target, "authoritative_target")

    nominal = nominal_audio_latent_length(frame_count)

    if audio_boundary_is_exact(frame_count):
        if authoritative_target is not None and authoritative_target != nominal:
            raise TemporalProfileError("conflicting_audio_latent_target")
        return AudioLatentTarget(
            frame_count=frame_count,
            audio_latent_length=nominal,
            exact=True,
            policy=AudioTargetPolicy.EXACT,
            nominal=nominal,
            direction="exact",
        )

    if authoritative_target is None:
        raise TemporalProfileError("authoritative_audio_latent_target_required")
    if authoritative_target != nominal:
        raise TemporalProfileError("conflicting_audio_latent_target")
    exact_extent = exact_audio_latent_length(frame_count)
    return AudioLatentTarget(
        frame_count=frame_count,
        audio_latent_length=authoritative_target,
        exact=False,
        policy=AudioTargetPolicy.AUTHORITATIVE_TARGET,
        nominal=nominal,
        direction="up" if authoritative_target > exact_extent else "down",
    )


def context_extent_is_on_grid(extent_frames: int) -> bool:
    """Whether a real context extent is a whole number of steps on both latent grids."""

    _require_frame_count(extent_frames, "extent_frames")
    return extent_frames % CONTEXT_EXTENT_GRID_FRAMES == 0


def check_crop_conservation(plan: CropPlan) -> CropVerdict:
    """The conservation rule: produced output minus crop equals the requested target, exactly.

    Both failures are named rather than collapsed into one, because they are different mistakes.
    Cropping less than the context that was consumed leaves boundary material in the result, which
    double-counts a seam a neighbouring segment also carries.  Cropping more silently shortens the
    interval the user asked for, which is the failure that looks like success until the timeline no
    longer adds up.
    """

    for field_name in (
        "requested_target_frames",
        "leading_context_frames",
        "trailing_context_frames",
        "produced_frames",
        "leading_crop_frames",
        "trailing_crop_frames",
    ):
        _require_frame_count(getattr(plan, field_name), field_name)

    delivered = plan.produced_frames - plan.leading_crop_frames - plan.trailing_crop_frames

    if not length.is_producible(plan.requested_target_frames):
        return CropVerdict(False, "target_interval_not_producible", delivered)
    for extent in (plan.leading_context_frames, plan.trailing_context_frames):
        if not context_extent_is_on_grid(extent):
            return CropVerdict(False, "context_extent_off_grid", delivered)

    expected_produced = (
        plan.leading_context_frames + plan.requested_target_frames + plan.trailing_context_frames
    )
    if plan.produced_frames != expected_produced:
        return CropVerdict(False, "produced_run_does_not_cover_plan", delivered)
    if not length.is_producible(plan.produced_frames):
        return CropVerdict(False, "produced_run_not_producible", delivered)

    if delivered > plan.requested_target_frames:
        return CropVerdict(False, "under_cropped_boundary_double_counted", delivered)
    if delivered < plan.requested_target_frames:
        return CropVerdict(False, "over_cropped_target_shortened", delivered)
    return CropVerdict(True, "conserved", delivered)


#: Every adapter-level member, each bound to the exact source it was read from.  The tuple is the
#: profile's declared surface: a fact that is not here is not declared, and a fact here without a
#: source would fail construction.
ADAPTER_FACTS: tuple[AdapterFact, ...] = (
    AdapterFact(
        name="video_frame_rate",
        summary="delivered video runs at 24 frames per second",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="audio_latent_rate",
        summary="the audio latent grid runs at 40 steps per second of delivered video",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="video_run_lattice",
        summary="producible frame counts are the arithmetic sequence starting at the lattice "
        "offset and rising by the lattice step",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="accepted_frame_range",
        summary="the node accepts frame counts from 5 to 3600; this is an input bound, not a "
        "statement about what the model was trained on",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="alignment_operation",
        summary="a requested length is snapped up to the next lattice member, and the snap is "
        "reported alongside the aligned value",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="video_latent_length",
        summary="a run occupies two latent steps plus five for each lattice step above the "
        "shortest run",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="audio_latent_join",
        summary="the audio extent is the exact rational extent joined to a whole step by "
        "round-to-nearest; no frame count reaches a tie",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="exact_audio_boundary_subset",
        summary="the audio extent is whole exactly when the ratio denominator divides the frame "
        "count, which inside the lattice recurs every 51 frames",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="context_extent_grid",
        summary="a real context extent is expressible only as a whole multiple of 51 frames, the "
        "least extent whole on both latent grids",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="crop_conservation_rule",
        summary="produced output minus leading and trailing crop equals the requested target "
        "interval exactly",
        source=NATIVE_NODE_SOURCE,
    ),
    AdapterFact(
        name="seconds_to_frames_convention",
        summary="an authored duration becomes a frame count by round-to-nearest before alignment",
        source=OFFICIAL_TEMPLATE_SOURCE,
    ),
)

#: The `M19-07` row each model-level claim is instantiated by, with the summary it asserts.  Rows
#: appear here whether or not they were qualified, because a dropped claim is indistinguishable from
#: a capability that was never considered.
_CAPABILITY_SUBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "trained_frame_range",
        "temporal_profile",
        "the model was trained for approximately 124 to 362 frames; longer runs are untested "
        "rather than unsupported",
    ),
    (
        "joint_av_latent_descriptor",
        "joint_av_latent_descriptor",
        "the joint audiovisual latent has a stable serializable descriptor",
    ),
    (
        "completed_boundary_resume",
        "completed_boundary_resume",
        "a completed boundary can be resumed from exactly",
    ),
    (
        "dual_domain_av_mask",
        "dual_domain_av_mask",
        "continuation can be masked in the video and audio domains together",
    ),
    (
        "two_ended_av_bridge",
        "two_ended_av_bridge",
        "a segment can be generated between two fixed ends against a master audio track",
    ),
)

#: The trained range, held here as the value a `supported` row instantiates.  It is read from the
#: single length authority rather than restated, and it is *not* a module-level profile member: it
#: becomes one only through :func:`build_temporal_profile`.
_TRAINED_RANGE = FrameRange(
    minimum=length.TRAINED_MIN_FRAME_COUNT,
    maximum=length.TRAINED_MAX_FRAME_COUNT,
    hedged=True,
)

#: Limitations that hold whatever the qualification state is.  They are part of the projection
#: because a profile that published only what it can do would be read as a capability list.
_STANDING_LIMITATIONS: tuple[str, ...] = (
    "the profile describes the adapter and the qualified model; it makes no claim of equivalence "
    "with any official hosted service",
    "a run whose audio mapping is non-integral is admitted only with the adapter's own "
    "authoritative target extent; the profile never supplies one",
    "the lookahead and crop members are a constraint, not a plan: no per-job lookahead value and "
    "no crop execution is delivered here",
)


@dataclass(frozen=True, slots=True)
class TemporalCapabilityProfile:
    """One versioned profile: adapter facts, capability claims, and which is which."""

    version: int
    accepted_frame_range: FrameRange
    trained_frame_range: TrainedRangeReport
    adapter_facts: tuple[AdapterFact, ...]
    capability_claims: tuple[ModelCapabilityClaim, ...]
    limitations: tuple[str, ...]
    subject_identity: str | None

    def adapter_fact(self, name: str) -> AdapterFact:
        for item in self.adapter_facts:
            if item.name == name:
                return item
        raise TemporalProfileError("undeclared_adapter_fact")

    def capability_claim(self, name: str) -> ModelCapabilityClaim:
        for item in self.capability_claims:
            if item.name == name:
                return item
        raise TemporalProfileError("undeclared_capability_claim")

    def provenance_of(self, name: str) -> ProvenanceClass:
        """The declared class of one member, refusing a name that is neither.

        A member cannot answer to both: the two name spaces are disjoint, which is checked at
        construction, so this is the branch a consumer is meant to take and it cannot be ambiguous.
        """

        for item in self.adapter_facts:
            if item.name == name:
                return item.provenance
        for claim in self.capability_claims:
            if claim.name == name:
                return claim.provenance
        raise TemporalProfileError("undeclared_profile_member")

    def to_wire(self) -> dict[str, object]:
        """The content-free projection.

        It carries identifiers, statuses, evidence levels and limitations, and no prompt, media
        value, locator or credential.  The subject identity is the opaque digest of a public
        upstream artifact.
        """

        ratio = f"{AUDIO_LATENT_RATIO.numerator}/{AUDIO_LATENT_RATIO.denominator}"
        return {
            "schema": TEMPORAL_PROFILE_SCHEMA,
            "version": self.version,
            "video_fps": length.FPS,
            "audio_latent_fps": AUDIO_LATENT_FPS,
            "audio_latent_ratio": ratio,
            "exact_audio_period_frames": EXACT_AUDIO_PERIOD_FRAMES,
            "lattice_offset": length.LATTICE_OFFSET,
            "lattice_step": length.LATTICE_STEP,
            "context_extent_grid_frames": CONTEXT_EXTENT_GRID_FRAMES,
            "minimum_context_extent_frames": MINIMUM_CONTEXT_EXTENT_FRAMES,
            "accepted_frame_range": self.accepted_frame_range.to_wire(),
            "trained_frame_range": self.trained_frame_range.to_wire(),
            "adapter_facts": [item.to_wire() for item in self.adapter_facts],
            "capability_claims": [item.to_wire() for item in self.capability_claims],
            "limitations": list(self.limitations),
            "subject_identity": self.subject_identity,
        }

    def identity(self) -> DomainFingerprint:
        """The profile's semantic identity: it decides what a run produces."""

        return domain_fingerprint(IdentityDomain.SEMANTIC, self.to_wire())

    def contract_identity(self) -> DomainFingerprint:
        """The structural identity of the contract, which is a different question entirely."""

        return domain_fingerprint(
            IdentityDomain.WIRE_CONTRACT,
            {"schema": TEMPORAL_PROFILE_SCHEMA, "version": self.version},
        )


def build_temporal_profile(
    accepted: AcceptedQualification | None = None,
) -> TemporalCapabilityProfile:
    """Build the profile, joining accepted `M19-07` evidence when there is any.

    With no accepted evidence the adapter facts are complete and every capability claim reports
    `unqualified` -- not absent, not defaulted, and never substituted for by an adapter fact.  With
    accepted evidence each claim takes its row's status verbatim, and only `supported` instantiates
    a value.  A projection whose subject is not the pinned source is refused rather than joined,
    because a capability measured against a different adapter is not evidence about this one.
    """

    if accepted is not None:
        if not isinstance(accepted, AcceptedQualification):
            raise TemporalProfileError("accepted_qualification_has_wrong_type")
        if accepted.subject_identity != NATIVE_NODE_SOURCE.digest:
            raise TemporalProfileError("subject_identity_drift")

    claims: list[ModelCapabilityClaim] = []
    trained = TrainedRangeReport(
        status=CapabilityStatus.UNQUALIFIED,
        reason_code="no_accepted_row",
    )

    for name, row_name, summary in _CAPABILITY_SUBJECTS:
        row = None if accepted is None else accepted.row(row_name)
        status = CapabilityStatus.UNQUALIFIED if row is None else row.status
        reason = "no_accepted_row" if row is None else row.reason_code
        claims.append(
            ModelCapabilityClaim(
                name=name,
                summary=summary,
                row=row_name,
                status=status,
                reason_code=reason,
                evidence_level=(
                    EvidenceLevel.OFFICIAL if status is CapabilityStatus.SUPPORTED else None
                ),
            )
        )
        if name == "trained_frame_range":
            trained = TrainedRangeReport(
                status=status,
                reason_code=reason,
                value=_TRAINED_RANGE if status is CapabilityStatus.SUPPORTED else None,
            )

    collisions = {item.name for item in ADAPTER_FACTS} & {item.name for item in claims}
    if collisions:
        raise TemporalProfileError("member_declared_in_both_provenance_classes")

    limitations = list(_STANDING_LIMITATIONS)
    limitations.extend(
        f"{claim.name} is {claim.status.value} and must not be relied on as available"
        for claim in claims
        if not claim.qualified
    )

    return TemporalCapabilityProfile(
        version=CURRENT_TEMPORAL_PROFILE_VERSION,
        accepted_frame_range=FrameRange(
            minimum=length.MIN_FRAME_COUNT,
            maximum=length.MAX_FRAME_COUNT,
            hedged=False,
        ),
        trained_frame_range=trained,
        adapter_facts=ADAPTER_FACTS,
        capability_claims=tuple(claims),
        limitations=tuple(limitations),
        subject_identity=None if accepted is None else accepted.subject_identity,
    )


__all__ = [
    "ADAPTER_FACTS",
    "AUDIO_LATENT_FPS",
    "AUDIO_LATENT_RATIO",
    "CONTEXT_EXTENT_GRID_FRAMES",
    "CURRENT_TEMPORAL_PROFILE_VERSION",
    "EXACT_AUDIO_PERIOD_FRAMES",
    "MINIMUM_CONTEXT_EXTENT_FRAMES",
    "NATIVE_NODE_SOURCE",
    "OFFICIAL_TEMPLATE_SOURCE",
    "TEMPORAL_PROFILE_SCHEMA",
    "VIDEO_LATENT_BASE_STEPS",
    "VIDEO_LATENT_STEPS_PER_LATTICE_STEP",
    "AcceptedQualification",
    "AdapterFact",
    "AudioLatentTarget",
    "AudioTargetPolicy",
    "CapabilityStatus",
    "CropPlan",
    "CropVerdict",
    "FrameRange",
    "ModelCapabilityClaim",
    "PinnedSource",
    "ProvenanceClass",
    "QualifiedRow",
    "RunAlignment",
    "TemporalCapabilityProfile",
    "TemporalProfileError",
    "TrainedRangeReport",
    "admit_audio_latent_target",
    "align_run",
    "audio_boundary_is_exact",
    "build_temporal_profile",
    "check_crop_conservation",
    "context_extent_is_on_grid",
    "exact_audio_latent_length",
    "nominal_audio_latent_length",
    "video_latent_length",
]
