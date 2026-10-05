"""Single authority for H3 video length: the frame lattice and its conversions.

Both the Context normalization path and the Production segment workspace derive
length from this module. Neither keeps a private copy of the arithmetic, because
two authorities for one contract is exactly the defect `M17-25` exists to remove:
`normalization.py` implemented the lattice correctly while `segment_workspace.py`
accepted any positive frame count, so Production could hold lengths the model
cannot produce.

The seconds-to-frames step is round-to-nearest, matching the official ComfyUI
templates (`video_minimax_h3_t2v.json` and its siblings), which evaluate

    max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17

through a core `ComfyMathExpression` node whose environment binds ``round`` to
Python's built-in. `Decimal` under `ROUND_HALF_EVEN` is the faithful mirror of
that binding. Lattice alignment itself remains upward and is unchanged.

No float reaches a wire value: durations are integer milliseconds and lengths are
integer frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

FPS = 24
MIN_FRAME_COUNT = 5
MAX_FRAME_COUNT = 3600
DEFAULT_FRAME_COUNT = 124
TRAINED_MIN_FRAME_COUNT = 124
TRAINED_MAX_FRAME_COUNT = 362
LATTICE_OFFSET = 5
LATTICE_STEP = 17
MILLISECONDS_PER_SECOND = 1000

#: The largest producible length: the greatest lattice member at or below the
#: host maximum. 3600 itself is not on the lattice, so a request that aligns
#: above this fails rather than being clamped down to something never asked for.
MAX_PRODUCIBLE_FRAME_COUNT = (
    LATTICE_OFFSET + ((MAX_FRAME_COUNT - LATTICE_OFFSET) // LATTICE_STEP) * LATTICE_STEP
)

#: The authored-duration domain, in integer milliseconds, over which a request
#: actually resolves. Callers that bound a control -- a node slider, an HTML
#: input, a schema -- must use these rather than `MIN_FRAME_COUNT / FPS` and
#: `MAX_FRAME_COUNT / FPS`.
#:
#: `MAX_FRAME_COUNT` is the host's stated ceiling, not the largest length that
#: can be delivered, and the gap between them is real: every frame count from
#: 3593 to 3600 sits inside the host's range yet aligns to 3609, above it, so it
#: cannot be produced at all. A control bounded at `MAX_FRAME_COUNT / FPS` --
#: 150.0 s -- therefore admits roughly 313 ms of durations that always fail. The
#: bounds below are derived from what resolves, so no surface can offer a
#: duration the pipeline will reject.
#:
#: Derivation: the rounding step admits `ms` while
#: `round_half_even(ms * 24 / 1000)` stays inside `MIN_FRAME_COUNT ..
#: MAX_PRODUCIBLE_FRAME_COUNT`. No exact tie is reachable on integer
#: milliseconds (see the module docstring), so the open bound is exact.
MIN_ACCEPTED_MILLISECONDS = 188
MAX_ACCEPTED_MILLISECONDS = 149_687


class LengthError(ValueError):
    """A duration or frame count that cannot be represented on the H3 lattice."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_producible(frame_count: int) -> bool:
    """Return whether ``frame_count`` is a length the model can actually produce."""

    return (
        isinstance(frame_count, int)
        and not isinstance(frame_count, bool)
        and MIN_FRAME_COUNT <= frame_count <= MAX_FRAME_COUNT
        and (frame_count - LATTICE_OFFSET) % LATTICE_STEP == 0
    )


def align_frame_count(frame_count: int) -> int:
    """Return the smallest producible length greater than or equal to ``frame_count``."""

    if frame_count <= MIN_FRAME_COUNT:
        return MIN_FRAME_COUNT
    remainder = (frame_count - LATTICE_OFFSET) % LATTICE_STEP
    return frame_count if remainder == 0 else frame_count + (LATTICE_STEP - remainder)


def floor_frame_count(frame_count: int) -> int:
    """Return the greatest native reference length at or below the admitted count."""
    _require_positive_int(frame_count, "frame_count")
    if frame_count < MIN_FRAME_COUNT:
        raise LengthError("reference_too_short")
    if frame_count > MAX_FRAME_COUNT:
        raise LengthError("frame_count_out_of_bounds")
    # CRITICAL: references discard the tail. align_frame_count rounds up and would admit
    # footage the generator never conditions on; do not substitute it for this floor.
    return frame_count - (frame_count - LATTICE_OFFSET) % LATTICE_STEP


def nearest_frames_from_milliseconds(milliseconds: int) -> int:
    """Return ``round(ms/1000 * 24)`` before either the clamp or lattice alignment.

    Exposed separately because the accepted range is a real boundary for the
    Context surface: the official template silently clamps a sub-minimum request
    up to five frames, and this repository reports it as out of bounds instead of
    delivering a length nobody asked for.
    """

    _require_positive_int(milliseconds, "duration_milliseconds")
    try:
        scaled = (Decimal(milliseconds) * FPS / MILLISECONDS_PER_SECOND).to_integral_value(
            rounding=ROUND_HALF_EVEN
        )
    except (InvalidOperation, OverflowError) as error:  # pragma: no cover - guard
        raise LengthError("duration_out_of_bounds") from error
    return int(scaled)


def official_template_frames(milliseconds: int) -> int:
    """Return exactly what the official template expression computes for a duration.

    ``max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17`` with
    ``a = ms / 1000``. Kept as its own function so the parity test compares against
    a faithful transcription rather than against the repository's own pipeline,
    and so the one place the two deliberately differ stays visible: the official
    expression clamps a sub-minimum request up to five frames, and this repository
    rejects it instead of delivering a length nobody asked for.
    """

    return align_frame_count(max(MIN_FRAME_COUNT, nearest_frames_from_milliseconds(milliseconds)))


def frames_from_milliseconds(milliseconds: int) -> int:
    """Return the producible length delivered for an integer-millisecond request.

    Fails closed at both ends of the accepted range. Below the minimum the
    official template would clamp; this repository does not, because delivering a
    length shorter than anything the caller could have meant is a silent
    substitution, and the whole point of this contract is that movement is
    reported.
    """

    nearest = nearest_frames_from_milliseconds(milliseconds)
    if nearest < MIN_FRAME_COUNT:
        raise LengthError("duration_below_minimum")
    if nearest > MAX_FRAME_COUNT:
        raise LengthError("duration_above_maximum")
    aligned = align_frame_count(nearest)
    if aligned > MAX_FRAME_COUNT:
        raise LengthError("duration_above_maximum")
    return aligned


def milliseconds_from_frames(frame_count: int) -> int:
    """Return the integer-millisecond duration of ``frame_count`` frames."""

    _require_positive_int(frame_count, "frame_count")
    return int(
        (Decimal(frame_count) * MILLISECONDS_PER_SECOND / FPS).to_integral_value(
            rounding=ROUND_HALF_EVEN
        )
    )


def _require_positive_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LengthError(f"{field}_out_of_bounds")


@dataclass(frozen=True, slots=True)
class ResolvedLength:
    """What was asked for, what will be delivered, and whether they differ.

    ``snapped`` is true whenever the delivered duration is not the requested one,
    in either direction. Alignment only ever moves upward, but the rounding step
    can move downward, so a delivered duration shorter than the request is
    reachable and must be reported: at 4.46 s the raw and aligned lengths are both
    107 frames, which is 4.458 s, and the pre-M17-25 diagnostic was silent
    precisely because it compared the aligned value against the raw one instead of
    against the request.
    """

    requested_milliseconds: int
    frame_count: int
    delivered_milliseconds: int
    snapped: bool
    direction: str

    @property
    def within_trained_range(self) -> bool:
        return TRAINED_MIN_FRAME_COUNT <= self.frame_count <= TRAINED_MAX_FRAME_COUNT


def resolve_milliseconds(milliseconds: int) -> ResolvedLength:
    """Resolve an authored duration into the producible length it delivers."""

    frame_count = frames_from_milliseconds(milliseconds)
    # Movement is judged at wire resolution, not on the exact rational. A length
    # of 124 frames is 5166.67 ms, so comparing rationals would report the
    # perfectly ordinary request of 5167 ms as snapped by a third of a
    # millisecond -- a difference no wire value can carry and no user can act on.
    # Sub-millisecond movement is not representable and is therefore not movement.
    delivered = milliseconds_from_frames(frame_count)
    if delivered == milliseconds:
        direction = "exact"
    elif delivered > milliseconds:
        direction = "longer"
    else:
        direction = "shorter"
    return ResolvedLength(
        requested_milliseconds=milliseconds,
        frame_count=frame_count,
        delivered_milliseconds=delivered,
        snapped=direction != "exact",
        direction=direction,
    )


def resolve_frames(frame_count: int) -> ResolvedLength:
    """Resolve a legacy authored frame count as if it had been authored as a duration.

    Migration uses this so a stored non-producible value lands on exactly the
    length alignment would have given it, rather than on a third answer. That
    holds over ``MIN_FRAME_COUNT .. MAX_PRODUCIBLE_FRAME_COUNT``; a stored count
    above the largest producible length has no aligned answer inside the host
    range and fails closed with ``frame_count_out_of_bounds``.
    """

    _require_positive_int(frame_count, "frame_count")
    # Fail closed rather than clamp: a stored value that cannot be produced was
    # never a working length, and silently moving it to the nearest end would
    # replace a user's recorded intent with a length they never authored.
    #
    # The bound is MAX_PRODUCIBLE_FRAME_COUNT, not MAX_FRAME_COUNT. Distinct
    # review of M17-25 found the difference the hard way: 3593..3600 pass a
    # `<= MAX_FRAME_COUNT` guard and then fail downstream on the aligned value,
    # so the guard claimed a domain the function did not have. Rejecting them
    # here names the real reason -- the stored count is not producible -- instead
    # of reporting it as a duration that came out too long.
    if not MIN_FRAME_COUNT <= frame_count <= MAX_PRODUCIBLE_FRAME_COUNT:
        raise LengthError("frame_count_out_of_bounds")
    return resolve_milliseconds(milliseconds_from_frames(frame_count))


__all__ = [
    "DEFAULT_FRAME_COUNT",
    "MAX_ACCEPTED_MILLISECONDS",
    "MIN_ACCEPTED_MILLISECONDS",
    "FPS",
    "LATTICE_OFFSET",
    "LATTICE_STEP",
    "LengthError",
    "MAX_FRAME_COUNT",
    "MAX_PRODUCIBLE_FRAME_COUNT",
    "MILLISECONDS_PER_SECOND",
    "MIN_FRAME_COUNT",
    "ResolvedLength",
    "TRAINED_MAX_FRAME_COUNT",
    "TRAINED_MIN_FRAME_COUNT",
    "align_frame_count",
    "floor_frame_count",
    "frames_from_milliseconds",
    "is_producible",
    "milliseconds_from_frames",
    "nearest_frames_from_milliseconds",
    "official_template_frames",
    "resolve_frames",
    "resolve_milliseconds",
]
