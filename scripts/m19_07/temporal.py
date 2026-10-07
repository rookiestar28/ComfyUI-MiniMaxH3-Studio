"""Temporal qualification: the video-run lattice, the audio-latent join, and their separation.

Three decisions have to stay separable, and the whole reason `M19-07` names four specific frame
counts is that a careless set collapses them:

1. **run validity** — is the frame count on the model's producible lattice at all;
2. **exact audiovisual boundary** — does the audio latent extent land on a whole step, or is it a
   rounded join;
3. **authoritative target admission** — does the exact adapter itself supply the target audio latent
   count, rather than a surface inventing one.

The lattice is not re-derived here. `M17-25` made `comfyui_h3_context.core.length` the repository's
single authority for it, and a qualification harness that implements a second copy would recreate
exactly the defect that item removed. This module reads the lattice from the pinned source, reads it
again from the repository authority, and reports any disagreement as a finding for `M20-01`.

What the repository does *not* own is the audio side. There is no audio-latent rate anywhere in
`comfyui_h3_context`, so the join is derived from the source alone, and deliberately not written
back into the product package: that belongs to `M20-01` and only if its row is `supported`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from comfyui_h3_context.core import length as repo_length

from .source_facts import SourceFacts

#: The four candidates the plan names. They are lattice members `5 + 17k` for k of 0, 1, 2 and 7,
#: chosen so that run validity, exact boundary alignment and authoritative target admission are
#: separated by the data rather than merely asserted to be separate.
CANDIDATE_FRAME_COUNTS: tuple[int, ...] = (5, 22, 39, 124)

#: Frame counts that are deliberately *not* on the lattice, used to prove the harness rejects rather
#: than silently snaps. Each one sits between two lattice members.
OFF_GRID_FRAME_COUNTS: tuple[int, ...] = (4, 6, 21, 23, 40, 123, 125)


@dataclass(frozen=True)
class CandidateFacts:
    """One frame count, measured through the source's own arithmetic."""

    frame_count: int
    on_lattice: bool
    video_latent_t: int
    audio_latent_t: int
    audio_exact: bool
    rounding: str

    def as_evidence(self) -> dict[str, Any]:
        return {
            "frame_count": self.frame_count,
            "on_lattice": self.on_lattice,
            "video_latent_t": self.video_latent_t,
            "audio_latent_t": self.audio_latent_t,
            "audio_exact": self.audio_exact,
            "rounding": self.rounding,
        }


@dataclass(frozen=True)
class LatticeAgreement:
    """Whether the pinned source and the repository authority describe the same lattice."""

    agrees: bool
    checked_frame_counts: int
    source_fps: int
    repo_fps: int
    disagreements: tuple[str, ...]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "agrees": self.agrees,
            "checked_frame_counts": self.checked_frame_counts,
            "source_fps": self.source_fps,
            "repo_fps": self.repo_fps,
            "disagreements": list(self.disagreements),
        }


@dataclass(frozen=True)
class AudioJoin:
    """How the source joins a frame count to an audio latent extent."""

    video_fps: int
    audio_latent_fps: int
    ratio: str
    exact_period_frames: int
    is_rounded_join: bool
    minimum_tie_distance: str
    float_matches_exact: bool
    checked_frame_counts: int

    def as_evidence(self) -> dict[str, Any]:
        return {
            "video_fps": self.video_fps,
            "audio_latent_fps": self.audio_latent_fps,
            "ratio": self.ratio,
            "exact_period_frames": self.exact_period_frames,
            "is_rounded_join": self.is_rounded_join,
            "minimum_tie_distance": self.minimum_tie_distance,
            "float_matches_exact": self.float_matches_exact,
            "checked_frame_counts": self.checked_frame_counts,
        }


def lattice_members(facts: SourceFacts, *, lower: int, upper: int) -> tuple[int, ...]:
    """Every frame count in range that the source's own alignment leaves unchanged."""
    align = facts.functions["align_frame_count"]
    return tuple(n for n in range(lower, upper + 1) if align(n) == n)


def compare_lattice(facts: SourceFacts, *, upper: int | None = None) -> LatticeAgreement:
    """Check the repository authority against the pinned source over the whole declared range.

    A disagreement is recorded, never repaired. `comfyui_h3_context.core.length` is accepted
    `M17-25` behaviour; changing it from inside a qualification item would be a product change
    smuggled in as a measurement.
    """
    align_source = facts.functions["align_frame_count"]
    ceiling = repo_length.MAX_FRAME_COUNT if upper is None else upper
    lower = repo_length.MIN_FRAME_COUNT

    disagreements: list[str] = []
    if facts.constants["FPS"] != repo_length.FPS:
        disagreements.append(
            f"fps: source {facts.constants['FPS']} vs repository {repo_length.FPS}"
        )

    checked = 0
    for frame_count in range(lower, ceiling + 1):
        checked += 1
        source_aligned = align_source(frame_count)
        repo_aligned = repo_length.align_frame_count(frame_count)
        if source_aligned != repo_aligned:
            disagreements.append(
                f"align({frame_count}): source {source_aligned} vs repository {repo_aligned}"
            )
        source_producible = source_aligned == frame_count
        if source_producible != repo_length.is_producible(frame_count):
            disagreements.append(
                f"is_producible({frame_count}): source {source_producible} vs repository "
                f"{repo_length.is_producible(frame_count)}"
            )
        if len(disagreements) > 8:
            disagreements.append("... further disagreements suppressed")
            break

    return LatticeAgreement(
        agrees=not disagreements,
        checked_frame_counts=checked,
        source_fps=int(facts.constants["FPS"]),
        repo_fps=int(repo_length.FPS),
        disagreements=tuple(disagreements),
    )


def _exact_audio_latent(facts: SourceFacts, frame_count: int) -> Fraction:
    return Fraction(
        frame_count * int(facts.constants["AUDIO_LATENT_FPS"]), int(facts.constants["FPS"])
    )


def describe_audio_join(facts: SourceFacts, members: Sequence[int]) -> AudioJoin:
    """Classify the audio join and prove the source's float evaluation is safe.

    The source computes the audio latent extent as `round(frame_count / FPS * AUDIO_LATENT_FPS)` in
    binary floating point, while the repository's length authority is deliberately built on
    `Decimal`. That difference only matters if some frame count lands close enough to a tie for
    binary representation error to flip the rounding, so the check is done directly: every lattice
    member is evaluated in exact rational arithmetic and compared against what the source actually
    returns.
    """
    fps = int(facts.constants["FPS"])
    audio_fps = int(facts.constants["AUDIO_LATENT_FPS"])
    ratio = Fraction(audio_fps, fps)

    temporal_shape = facts.functions["temporal_shape"]
    half = Fraction(1, 2)
    minimum_distance = half
    mismatches = 0

    for frame_count in members:
        exact = _exact_audio_latent(facts, frame_count)
        fractional = exact - int(exact)
        distance = abs(fractional - half)
        minimum_distance = min(minimum_distance, distance)
        expected = _round_half_even(exact)
        if temporal_shape(frame_count)[2] != expected:
            mismatches += 1

    # The period is the smallest frame step whose audio extent is a whole number.
    exact_period = ratio.denominator

    return AudioJoin(
        video_fps=fps,
        audio_latent_fps=audio_fps,
        ratio=f"{ratio.numerator}/{ratio.denominator}",
        exact_period_frames=exact_period,
        is_rounded_join=ratio.denominator != 1,
        minimum_tie_distance=f"{minimum_distance.numerator}/{minimum_distance.denominator}",
        float_matches_exact=mismatches == 0,
        checked_frame_counts=len(members),
    )


def _round_half_even(value: Fraction) -> int:
    floor = value.numerator // value.denominator
    remainder = value - floor
    half = Fraction(1, 2)
    if remainder > half:
        return floor + 1
    if remainder < half:
        return floor
    return floor if floor % 2 == 0 else floor + 1


def measure_candidates(
    facts: SourceFacts, frame_counts: Sequence[int] = CANDIDATE_FRAME_COUNTS
) -> tuple[CandidateFacts, ...]:
    align = facts.functions["align_frame_count"]
    temporal_shape = facts.functions["temporal_shape"]

    measured: list[CandidateFacts] = []
    for frame_count in frame_counts:
        aligned, video_t, audio_t = temporal_shape(frame_count)
        exact = _exact_audio_latent(facts, aligned)
        is_exact = exact.denominator == 1
        if is_exact:
            rounding = "exact"
        elif audio_t > exact:
            rounding = "up"
        else:
            rounding = "down"
        measured.append(
            CandidateFacts(
                frame_count=frame_count,
                on_lattice=align(frame_count) == frame_count,
                video_latent_t=video_t,
                audio_latent_t=audio_t,
                audio_exact=is_exact,
                rounding=rounding,
            )
        )
    return tuple(measured)


def candidate_set_separates_decisions(measured: Sequence[CandidateFacts]) -> tuple[bool, str]:
    """A candidate set is only useful if the data actually separates the three decisions.

    Requiring at least one exact and at least one non-integral member is what stops the set from
    silently degenerating — for example if a future subject declared an audio rate that divides the
    video rate, in which case every member would be exact and the rounded-join decision would become
    invisible rather than absent.
    """
    if not all(item.on_lattice for item in measured):
        off = [item.frame_count for item in measured if not item.on_lattice]
        return False, f"candidates not on the producible lattice: {off}"
    if not any(item.audio_exact for item in measured):
        return False, "no candidate lands on an exact audiovisual boundary"
    if not any(not item.audio_exact for item in measured):
        return False, "no candidate exercises the rounded audio join"
    return True, "run validity, exact boundary and rounded join are each exercised"


def off_grid_rejected(facts: SourceFacts) -> tuple[bool, tuple[str, ...]]:
    """Off-lattice inputs must be visibly snapped upward, never accepted as-is."""
    align = facts.functions["align_frame_count"]
    problems: list[str] = []
    for frame_count in OFF_GRID_FRAME_COUNTS:
        aligned = align(frame_count)
        if aligned == frame_count:
            problems.append(f"{frame_count} was expected off-lattice but aligns to itself")
        elif aligned < frame_count:
            problems.append(f"align({frame_count}) = {aligned} snapped downward")
    return not problems, tuple(problems)


def temporal_evidence(facts: SourceFacts) -> Mapping[str, Any]:
    """Everything the `temporal_profile` row needs, measured rather than asserted."""
    members = lattice_members(
        facts, lower=repo_length.MIN_FRAME_COUNT, upper=repo_length.MAX_FRAME_COUNT
    )
    agreement = compare_lattice(facts)
    join = describe_audio_join(facts, members)
    measured = measure_candidates(facts)
    separates, separation_reason = candidate_set_separates_decisions(measured)
    rejects, off_grid_problems = off_grid_rejected(facts)

    return {
        "lattice": {
            "offset": members[0] if members else None,
            "step": (members[1] - members[0]) if len(members) > 1 else None,
            "member_count": len(members),
            "minimum": members[0] if members else None,
            "maximum": members[-1] if members else None,
        },
        "repository_agreement": agreement.as_evidence(),
        "audio_join": join.as_evidence(),
        "candidates": [item.as_evidence() for item in measured],
        "candidate_set_separates_decisions": separates,
        "candidate_separation_reason": separation_reason,
        "off_grid_rejected": rejects,
        "off_grid_problems": list(off_grid_problems),
    }
