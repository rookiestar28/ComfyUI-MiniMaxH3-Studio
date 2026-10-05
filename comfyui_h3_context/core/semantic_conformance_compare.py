"""Semantic comparator for the preview/final conformance report.

This module compares three things that were produced independently: the fixture's declared
expectation, what the browser actually presented, and what an independent extractor actually decoded
out of the final artifact. It produces measured values and a closed disposition. It never produces
an observation of its own, never calls the render planner or resolver, and never compares raw bytes.

Two rules shape almost every function here:

- **A missing observation is not a disagreement.** An absent, empty or stub observation yields
  `BLOCKED`, and a case that never executed yields `NOT_RUN`. Collapsing either into `MISMATCH`
  would make a broken harness look like a product defect; collapsing either into `PASS` would make
  it look like conformance. Both are worse than the truth.
- **Tolerances are applied, never chosen.** Every bound comes from the frozen
  `ToleranceProfile`. There is no per-case fitted offset anywhere in this file, because an offset
  fitted to the measurement is a way of writing down the bug rather than detecting it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final

from .semantic_conformance import (
    MISMATCH_CLASSES,
    REPORT_STATUSES,
    SemanticConformanceError,
    ToleranceProfile,
)

#: Output audio is fixed at 48 kHz mono by the accepted profile; drift is expressed in samples so
#: the inherited 2,000/2,048-sample bounds apply directly rather than through a rate conversion.
OUTPUT_SAMPLE_RATE: Final = 48_000

#: A report never carries a filesystem path, URL, process command line or raw exception. Evidence is
#: addressed by relative synthetic identifier instead.
_PRIVATE_PATTERNS: Final = (
    re.compile(r"[A-Za-z]:[\\/]"),
    re.compile(r"^[\\/]{1,2}"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE),
)


def _reject(message: str) -> SemanticConformanceError:
    return SemanticConformanceError(message)


def _reject_private(value: str, name: str) -> str:
    for pattern in _PRIVATE_PATTERNS:
        if pattern.search(value):
            raise _reject(f"{name} would disclose a private path or URL")
    return value


@dataclass(frozen=True, slots=True)
class FrameGrid:
    """The output frame grid: exact dimensions and an exact count on the 24-fps rational grid."""

    width: int
    height: int
    frame_count: int
    frame_rate_num: int
    frame_rate_den: int

    def pts_at(self, index: int) -> Fraction:
        return Fraction(index * self.frame_rate_den, self.frame_rate_num)

    def duration(self) -> Fraction:
        return Fraction(self.frame_count * self.frame_rate_den, self.frame_rate_num)


#: The closed vocabulary for what a decode timestamp table actually does relative to its
#: presentation timestamps. It is observed, never assumed: the accepted output profile encodes with
#: `-bf 0`, so DTS equals PTS, and a build that started emitting B-frames would change that without
#: changing a single nominal rate.
DTS_POLICIES: Final = ("absent", "equal_to_pts", "monotonic_leading", "nonmonotonic")


@dataclass(frozen=True, slots=True)
class FrameTiming:
    """The timestamps a decoder actually reported, in the artifact's own rational time base.

    CRITICAL: this exists because a nominal `r_frame_rate` and a decoded frame count agreeing say
    nothing about where the frames sit. A truncated, re-timed, duplicated or non-monotonic artifact
    can carry a perfectly correct nominal rate, and the old grid derived its duration as
    `count / rate` -- an arithmetic identity, not a measurement. Every value here comes from the
    packet table; nothing is filled in from the container's hints.
    """

    time_base_num: int
    time_base_den: int
    pts_ticks: tuple[int, ...]
    dts_ticks: tuple[int | None, ...]
    duration_ticks: tuple[int | None, ...] = ()

    def __post_init__(self) -> None:
        if self.time_base_den <= 0 or self.time_base_num <= 0:
            raise _reject("a frame timing table needs a positive rational time base")
        if len(self.pts_ticks) != len(self.dts_ticks):
            raise _reject("every packet needs its own decode timestamp slot")

    def count(self) -> int:
        return len(self.pts_ticks)

    def tick(self) -> Fraction:
        return Fraction(self.time_base_num, self.time_base_den)

    def time_at(self, index: int) -> Fraction:
        return self.pts_ticks[index] * self.tick()

    def times(self) -> tuple[Fraction, ...]:
        return tuple(point * self.tick() for point in self.pts_ticks)

    def is_strictly_monotonic(self) -> bool:
        return all(
            later > earlier
            for earlier, later in zip(self.pts_ticks, self.pts_ticks[1:], strict=False)
        )

    def duplicate_timestamps(self) -> tuple[int, ...]:
        seen: list[int] = []
        repeated: list[int] = []
        for point in self.pts_ticks:
            if point in seen:
                if point not in repeated:
                    repeated.append(point)
            else:
                seen.append(point)
        return tuple(repeated)

    def has_negative_timestamp(self) -> bool:
        return any(point < 0 for point in self.pts_ticks)

    def uniform_step(self) -> Fraction | None:
        """The one spacing every consecutive pair shares, or `None` when there is not one.

        A mean or median interval is deliberately not offered. Averaging is how an irregular
        timeline is made to look regular, and irregularity is precisely what this table is
        collected to expose. When the packets also carry their own durations, those have to agree
        with the measured spacing: two independent statements of the same fact disagreeing is an
        observation, not a rounding artefact.
        """

        if len(self.pts_ticks) < 2:
            return None
        steps = {
            later - earlier
            for earlier, later in zip(self.pts_ticks, self.pts_ticks[1:], strict=False)
        }
        if len(steps) != 1:
            return None
        step = steps.pop()
        if step <= 0:
            return None
        declared = {value for value in self.duration_ticks if value is not None}
        if declared and declared != {step}:
            return None
        return step * self.tick()

    def duration(self) -> Fraction | None:
        step = self.uniform_step()
        return None if step is None else step * self.count()

    def dts_policy(self) -> str:
        if all(stamp is None for stamp in self.dts_ticks):
            return "absent"
        stamps = [stamp for stamp in self.dts_ticks if stamp is not None]
        if len(stamps) != len(self.dts_ticks):
            return "nonmonotonic"
        if stamps == list(self.pts_ticks):
            return "equal_to_pts"
        increasing = all(
            later > earlier for earlier, later in zip(stamps, stamps[1:], strict=False)
        )
        leads = all(stamp <= point for stamp, point in zip(stamps, self.pts_ticks, strict=True))
        return "monotonic_leading" if increasing and leads else "nonmonotonic"

    def as_pairs(self) -> tuple[tuple[str, object], ...]:
        return (
            ("time_base", f"{self.time_base_num}/{self.time_base_den}"),
            ("packet_count", self.count()),
            ("first_pts_ticks", self.pts_ticks[0] if self.pts_ticks else None),
            ("last_pts_ticks", self.pts_ticks[-1] if self.pts_ticks else None),
            (
                "uniform_step_seconds",
                None if self.uniform_step() is None else str(self.uniform_step()),
            ),
            ("duration_seconds", None if self.duration() is None else str(self.duration())),
            ("strictly_monotonic", self.is_strictly_monotonic()),
            ("duplicate_timestamps", list(self.duplicate_timestamps())),
            ("negative_timestamp", self.has_negative_timestamp()),
            ("dts_policy", self.dts_policy()),
        )


@dataclass(frozen=True, slots=True)
class SourceLandmark:
    """The source frame an output frame actually resolved to, with its exact source PTS.

    An expectation always carries the PTS its asset's landmark table declares for the frame. An
    observation carries the same table applied to the index it actually read, and `None` when the
    index read is one the table does not name -- the comparison then reports the index mismatch
    that is, rather than a time nobody declared.
    """

    output_frame: int
    source_frame: int
    source_pts: int | None
    source_time_base_num: int
    source_time_base_den: int

    def source_time(self) -> Fraction:
        if self.source_pts is None:
            raise ValueError(
                f"output frame {self.output_frame} resolved to an undeclared source time"
            )
        return Fraction(self.source_pts * self.source_time_base_num, self.source_time_base_den)

    def half_tick(self) -> Fraction:
        return Fraction(self.source_time_base_num, 2 * self.source_time_base_den)


@dataclass(frozen=True, slots=True)
class PresentedFrame:
    """What the browser transport actually reported for one output frame.

    A browser reports a presented *time*, not a source tick index, so this is deliberately not a
    `SourceLandmark`: modelling the browser side with an integer PTS would make the half-tick
    boundary unreachable, because the smallest representable difference would already be a full
    tick. The comparison therefore takes the reported time as given and holds it to the boundary.
    """

    output_frame: int
    source_frame: int
    source_time: Fraction


@dataclass(frozen=True, slots=True)
class GeometryLandmark:
    """A fiducial or bounding box in output-space pixels, against actual backing dimensions."""

    label: str
    left: int
    top: int
    right: int
    bottom: int

    def center_x(self) -> Fraction:
        # The true center, not a doubled coordinate: `(l + r) / 2` keeps a half-pixel center exact.
        return Fraction(self.left + self.right, 2)

    def center_y(self) -> Fraction:
        return Fraction(self.top + self.bottom, 2)

    def coordinates(self) -> tuple[tuple[str, Fraction], ...]:
        return (
            ("left", Fraction(self.left)),
            ("top", Fraction(self.top)),
            ("right", Fraction(self.right)),
            ("bottom", Fraction(self.bottom)),
            ("center_x", self.center_x()),
            ("center_y", self.center_y()),
        )


@dataclass(frozen=True, slots=True)
class PatchSample:
    """An interior patch mean. `edge_distance_px` is recorded so the interior claim is checkable."""

    label: str
    size_px: int
    edge_distance_px: int
    red: int
    green: int
    blue: int

    def channels(self) -> tuple[tuple[str, int], ...]:
        return (("red", self.red), ("green", self.green), ("blue", self.blue))


@dataclass(frozen=True, slots=True)
class AlphaSample:
    """One point on a transition or opacity progression, in thousandths."""

    label: str
    output_frame: int
    alpha_milli: int


@dataclass(frozen=True, slots=True)
class TextObservation:
    """Resolved text facts. Content is compared exactly; per-line bounds carry a pixel tolerance."""

    content: str
    font_identity: str
    line_count: int
    weight: int
    style: str
    align: str
    line_bounds: tuple[GeometryLandmark, ...] = ()
    visible_glyph_ratio_milli: int = 0


@dataclass(frozen=True, slots=True)
class OutputMetadata:
    """Container/codec/color facts read from the artifact, not from a receipt."""

    container: str
    video_codec: str
    pixel_format: str
    width: int
    height: int
    pixel_aspect_num: int
    pixel_aspect_den: int
    color_policy: str
    frame_rate_num: int
    frame_rate_den: int
    audio_stream_count: int
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None

    def as_pairs(self) -> tuple[tuple[str, object], ...]:
        return (
            ("container", self.container),
            ("video_codec", self.video_codec),
            ("pixel_format", self.pixel_format),
            ("width", self.width),
            ("height", self.height),
            ("pixel_aspect_num", self.pixel_aspect_num),
            ("pixel_aspect_den", self.pixel_aspect_den),
            ("color_policy", self.color_policy),
            ("frame_rate_num", self.frame_rate_num),
            ("frame_rate_den", self.frame_rate_den),
            ("audio_stream_count", self.audio_stream_count),
            ("audio_codec", self.audio_codec),
            ("sample_rate", self.sample_rate),
            ("channels", self.channels),
        )


@dataclass(frozen=True, slots=True)
class AudioOnset:
    """A synthetic impulse, addressed by label and by its sample index in the output timeline."""

    label: str
    sample_index: int


@dataclass(frozen=True, slots=True)
class SilenceInterval:
    """A declared silent interval and the absolute PCM16 peak actually measured inside it."""

    label: str
    start_sample: int
    end_sample: int
    abs_peak: int

    def interior_length(self, exclusion: int) -> int:
        return (self.end_sample - self.start_sample) - 2 * exclusion


@dataclass(frozen=True, slots=True)
class AudioLevel:
    """The level of one window of output audio, in millionths of the source tone's own level.

    The window is named by its first output sample and its length, so an observation measured
    anywhere else is a different window rather than a value for this one.
    """

    label: str
    start_sample: int
    sample_count: int
    level_ppm: int


@dataclass(frozen=True, slots=True)
class AvPair:
    """One paired audio onset and actually presented visual landmark, on one monotonic clock."""

    label: str
    audio_time_ms: Fraction
    visual_time_ms: Fraction


@dataclass(frozen=True, slots=True)
class Expectation:
    """What the fixture declares before anything runs. Literal or analytically constructed."""

    case_id: str
    frame_grid: FrameGrid | None = None
    frame_timing: FrameTiming | None = None
    source_mapping: tuple[SourceLandmark, ...] = ()
    geometry: tuple[GeometryLandmark, ...] = ()
    patches: tuple[PatchSample, ...] = ()
    color_patches: tuple[PatchSample, ...] = ()
    alphas: tuple[AlphaSample, ...] = ()
    text: TextObservation | None = None
    output_metadata: OutputMetadata | None = None
    audio_onsets: tuple[AudioOnset, ...] = ()
    silences: tuple[SilenceInterval, ...] = ()
    audio_extent_samples: int | None = None
    audio_levels: tuple[AudioLevel, ...] = ()
    refusal_code: str | None = None
    layer_order: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    """What the accepted runtime actually presented. Absent evidence is named, never synthesized."""

    case_id: str
    canvas_width: int = 0
    canvas_height: int = 0
    source_mapping: tuple[PresentedFrame, ...] = ()
    geometry: tuple[GeometryLandmark, ...] = ()
    patches: tuple[PatchSample, ...] = ()
    color_patches: tuple[PatchSample, ...] = ()
    alphas: tuple[AlphaSample, ...] = ()
    text: TextObservation | None = None
    av_pairs: tuple[AvPair, ...] = ()
    audio_onsets: tuple[AudioOnset, ...] = ()
    silence_probes: tuple[SilenceInterval, ...] = ()
    clock_uncertainty_ms: Fraction = Fraction(0)
    refusal_code: str | None = None
    layer_order: tuple[str, ...] = ()
    executed: bool = True
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FinalObservation:
    """What the independent extractor decoded. It shares no code path with the render graph."""

    case_id: str
    extractor_fingerprint: str
    frame_grid: FrameGrid | None = None
    frame_timing: FrameTiming | None = None
    source_mapping: tuple[SourceLandmark, ...] = ()
    geometry: tuple[GeometryLandmark, ...] = ()
    patches: tuple[PatchSample, ...] = ()
    color_patches: tuple[PatchSample, ...] = ()
    alphas: tuple[AlphaSample, ...] = ()
    text: TextObservation | None = None
    output_metadata: OutputMetadata | None = None
    audio_onsets: tuple[AudioOnset, ...] = ()
    silences: tuple[SilenceInterval, ...] = ()
    audio_extent_samples: int | None = None
    audio_levels: tuple[AudioLevel, ...] = ()
    layer_order: tuple[str, ...] = ()
    executed: bool = True
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Mismatch:
    """One measured disagreement, in a closed class, with the numbers that produced it."""

    mismatch_class: str
    subject: str
    expected: str
    observed: str
    bound: str | None = None

    def __post_init__(self) -> None:
        if self.mismatch_class not in MISMATCH_CLASSES:
            raise _reject(f"{self.mismatch_class} is not a declared mismatch class")
        _reject_private(self.subject, "mismatch subject")
        _reject_private(self.expected, "mismatch expected value")
        _reject_private(self.observed, "mismatch observed value")

    def as_wire(self) -> dict[str, object]:
        return {
            "mismatch_class": self.mismatch_class,
            "subject": self.subject,
            "expected": self.expected,
            "observed": self.observed,
            "bound": self.bound,
        }


@dataclass(frozen=True, slots=True)
class Measurement:
    """A recorded numeric fact, kept whether or not it produced a mismatch."""

    subject: str
    value: str
    bound: str | None = None

    def as_wire(self) -> dict[str, object]:
        return {"subject": self.subject, "value": self.value, "bound": self.bound}


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """A single closed report row."""

    case_id: str
    status: str
    mismatches: tuple[Mismatch, ...] = ()
    measurements: tuple[Measurement, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in REPORT_STATUSES:
            raise _reject(f"{self.status} is not a declared report status")
        if self.status == "PASS" and self.mismatches:
            raise _reject(f"{self.case_id} cannot pass while carrying mismatches")
        if self.status == "MISMATCH" and not self.mismatches:
            raise _reject(f"{self.case_id} claims a mismatch without naming one")
        if self.reason is not None:
            _reject_private(self.reason, "outcome reason")

    def as_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "mismatches": [item.as_wire() for item in self.mismatches],
            "measurements": [item.as_wire() for item in self.measurements],
            "reason": self.reason,
        }


@dataclass(slots=True)
class _Accumulator:
    mismatches: list[Mismatch] = field(default_factory=list)
    measurements: list[Measurement] = field(default_factory=list)

    def note(self, subject: str, value: object, bound: object | None = None) -> None:
        self.measurements.append(
            Measurement(subject, str(value), None if bound is None else str(bound))
        )

    def fail(
        self,
        mismatch_class: str,
        subject: str,
        expected: object,
        observed: object,
        bound: object | None = None,
    ) -> None:
        self.mismatches.append(
            Mismatch(
                mismatch_class,
                subject,
                str(expected),
                str(observed),
                None if bound is None else str(bound),
            )
        )


def _observed_pairs(
    sides: tuple[str, ...], browser: tuple[PatchSample, ...], final: tuple[PatchSample, ...]
) -> tuple[tuple[str, tuple[PatchSample, ...]], ...]:
    """The (side, observation) pairs a comparison over `sides` must visit, in a stable order."""

    pairs: list[tuple[str, tuple[PatchSample, ...]]] = []
    if "browser" in sides:
        pairs.append(("browser", browser))
    pairs.append(("final", final))
    return tuple(pairs)


def _by_label(items: Iterable[GeometryLandmark]) -> dict[str, GeometryLandmark]:
    return {item.label: item for item in items}


def _patches_by_label(items: Iterable[PatchSample]) -> dict[str, PatchSample]:
    return {item.label: item for item in items}


def _onsets_by_label(items: Iterable[AudioOnset]) -> dict[str, AudioOnset]:
    return {item.label: item for item in items}


def _compare_frame_grid(
    accumulator: _Accumulator, expected: FrameGrid, observed: FrameGrid, side: str
) -> None:
    for name, want, got in (
        ("width", expected.width, observed.width),
        ("height", expected.height, observed.height),
        ("frame_count", expected.frame_count, observed.frame_count),
        ("frame_rate_num", expected.frame_rate_num, observed.frame_rate_num),
        ("frame_rate_den", expected.frame_rate_den, observed.frame_rate_den),
    ):
        accumulator.note(f"{side}.frame_grid.{name}", got, want)
        if want != got:
            accumulator.fail("frame_grid", f"{side}.frame_grid.{name}", want, got)
    if expected.duration() != observed.duration():
        accumulator.fail(
            "frame_grid", f"{side}.frame_grid.duration", expected.duration(), observed.duration()
        )


def _compare_frame_timing(
    accumulator: _Accumulator, expected: FrameTiming, observed: FrameTiming, side: str
) -> None:
    """Compare the timestamps that were actually measured, in rational time.

    The whole sequence is compared, not a summary of it. A count, a first stamp and a last stamp
    can all agree while an interior frame sits in the wrong place, and comparing rational times
    rather than raw ticks lets an artifact use a different time base without that alone counting
    as a disagreement.
    """

    for name, want, got in (
        ("packet_count", expected.count(), observed.count()),
        ("dts_policy", expected.dts_policy(), observed.dts_policy()),
        ("strictly_monotonic", expected.is_strictly_monotonic(), observed.is_strictly_monotonic()),
        (
            "negative_timestamp",
            expected.has_negative_timestamp(),
            observed.has_negative_timestamp(),
        ),
        (
            "duplicate_timestamps",
            list(expected.duplicate_timestamps()),
            list(observed.duplicate_timestamps()),
        ),
    ):
        accumulator.note(f"{side}.frame_timing.{name}", got, want)
        if want != got:
            accumulator.fail("frame_grid", f"{side}.frame_timing.{name}", want, got)

    for name, want_time, got_time in (
        ("uniform_step", expected.uniform_step(), observed.uniform_step()),
        ("duration", expected.duration(), observed.duration()),
    ):
        accumulator.note(f"{side}.frame_timing.{name}", got_time, want_time)
        if want_time != got_time:
            accumulator.fail("frame_grid", f"{side}.frame_timing.{name}", want_time, got_time)

    if expected.count() == observed.count():
        paired = zip(expected.times(), observed.times(), strict=True)
        for index, (want_pts, got_pts) in enumerate(paired):
            if want_pts != got_pts:
                # Exact rational equality. The output grid is 24 fps CFR by contract, so there is
                # no tolerance to spend here -- a tolerance would admit the adjacent frame.
                accumulator.fail(
                    "frame_grid", f"{side}.frame_timing.pts[{index}]", want_pts, got_pts
                )
                return


def _missing_output_frames(
    accumulator: _Accumulator, expected: Sequence[int], observed: Sequence[int], side: str
) -> None:
    missing = sorted(set(expected) - set(observed))
    if missing:
        # An unobserved frame is missing evidence. It is never filled in from the timeline.
        accumulator.fail(
            "source_mapping", f"{side}.source_mapping.frames", f"present {missing}", "absent"
        )


def _compare_final_source_mapping(
    accumulator: _Accumulator,
    expected: Sequence[SourceLandmark],
    observed: Sequence[SourceLandmark],
) -> None:
    """The decoded artifact either carries the expected source landmark or it does not."""

    expected_by_frame = {item.output_frame: item for item in expected}
    observed_by_frame = {item.output_frame: item for item in observed}
    _missing_output_frames(accumulator, list(expected_by_frame), list(observed_by_frame), "final")
    for output_frame, want in sorted(expected_by_frame.items()):
        got = observed_by_frame.get(output_frame)
        if got is None:
            continue
        accumulator.note(f"final.source_mapping.{output_frame}.source_frame", got.source_frame)
        if want.source_frame != got.source_frame:
            accumulator.fail(
                "source_mapping",
                f"final.source_mapping.{output_frame}.source_frame",
                want.source_frame,
                got.source_frame,
            )
            continue
        if want.source_pts != got.source_pts:
            accumulator.fail(
                "source_mapping",
                f"final.source_mapping.{output_frame}.source_pts",
                want.source_pts,
                got.source_pts,
            )


def _compare_browser_source_mapping(
    accumulator: _Accumulator,
    expected: Sequence[SourceLandmark],
    observed: Sequence[PresentedFrame],
) -> None:
    """Hold the presented source time to half a source tick, and never wider.

    CRITICAL: the boundary is half a tick because a full tick is exactly the distance to the
    adjacent source frame. Widening this to a whole tick would let the wrong source frame satisfy
    a VFR or different-rate case while every other measurement still agreed -- which is the defect
    class this comparison exists to catch. Do not relax it to absorb transport jitter; jitter that
    large is a real observation and belongs in the report as one.
    """

    expected_by_frame = {item.output_frame: item for item in expected}
    observed_by_frame = {item.output_frame: item for item in observed}
    _missing_output_frames(accumulator, list(expected_by_frame), list(observed_by_frame), "browser")
    for output_frame, want in sorted(expected_by_frame.items()):
        got = observed_by_frame.get(output_frame)
        if got is None:
            continue
        accumulator.note(f"browser.source_mapping.{output_frame}.source_frame", got.source_frame)
        if want.source_frame != got.source_frame:
            accumulator.fail(
                "source_mapping",
                f"browser.source_mapping.{output_frame}.source_frame",
                want.source_frame,
                got.source_frame,
            )
            continue
        drift = abs(want.source_time() - got.source_time)
        bound = want.half_tick()
        accumulator.note(f"browser.source_mapping.{output_frame}.drift", drift, bound)
        if drift > bound:
            accumulator.fail(
                "source_mapping",
                f"browser.source_mapping.{output_frame}.drift",
                f"<= {bound}",
                drift,
                bound,
            )


def _compare_geometry(
    accumulator: _Accumulator,
    expected: Sequence[GeometryLandmark],
    observed: Sequence[GeometryLandmark],
    side: str,
    tolerance: ToleranceProfile,
) -> None:
    observed_by_label = _by_label(observed)
    for want in expected:
        got = observed_by_label.get(want.label)
        if got is None:
            accumulator.fail("geometry", f"{side}.geometry.{want.label}", "present", "absent")
            continue
        for name, want_value in want.coordinates():
            got_value = dict(got.coordinates())[name]
            error = abs(want_value - got_value)
            accumulator.note(
                f"{side}.geometry.{want.label}.{name}", got_value, tolerance.geometry_max_abs_pixels
            )
            if error > tolerance.geometry_max_abs_pixels:
                accumulator.fail(
                    "geometry",
                    f"{side}.geometry.{want.label}.{name}",
                    want_value,
                    got_value,
                    tolerance.geometry_max_abs_pixels,
                )


def _compare_patches(
    accumulator: _Accumulator,
    expected: Sequence[PatchSample],
    observed: Sequence[PatchSample],
    side: str,
    tolerance: ToleranceProfile,
    *,
    mismatch_class: str,
    bound: int,
) -> None:
    observed_by_label = _patches_by_label(observed)
    for want in expected:
        got = observed_by_label.get(want.label)
        if got is None:
            accumulator.fail(mismatch_class, f"{side}.patch.{want.label}", "present", "absent")
            continue
        # An antialiased edge pixel sampled as "interior" makes every patch check meaningless, so
        # the interior claim is enforced rather than trusted.
        if got.size_px != tolerance.patch_size_px:
            accumulator.fail(
                mismatch_class,
                f"{side}.patch.{want.label}.size_px",
                tolerance.patch_size_px,
                got.size_px,
            )
        if got.edge_distance_px < tolerance.patch_min_edge_distance_px:
            accumulator.fail(
                mismatch_class,
                f"{side}.patch.{want.label}.edge_distance_px",
                f">= {tolerance.patch_min_edge_distance_px}",
                got.edge_distance_px,
                tolerance.patch_min_edge_distance_px,
            )
        for channel, want_value in want.channels():
            got_value = dict(got.channels())[channel]
            error = abs(want_value - got_value)
            accumulator.note(f"{side}.patch.{want.label}.{channel}", got_value, bound)
            if error > bound:
                accumulator.fail(
                    mismatch_class,
                    f"{side}.patch.{want.label}.{channel}",
                    want_value,
                    got_value,
                    bound,
                )


def _compare_alphas(
    accumulator: _Accumulator,
    expected: Sequence[AlphaSample],
    observed: Sequence[AlphaSample],
    side: str,
    tolerance: ToleranceProfile,
) -> None:
    observed_by_key = {(item.label, item.output_frame): item for item in observed}
    for want in expected:
        got = observed_by_key.get((want.label, want.output_frame))
        if got is None:
            accumulator.fail(
                "alpha", f"{side}.alpha.{want.label}.{want.output_frame}", "present", "absent"
            )
            continue
        error = abs(want.alpha_milli - got.alpha_milli)
        accumulator.note(
            f"{side}.alpha.{want.label}.{want.output_frame}",
            got.alpha_milli,
            tolerance.alpha_max_abs_error_milli,
        )
        if error > tolerance.alpha_max_abs_error_milli:
            accumulator.fail(
                "alpha",
                f"{side}.alpha.{want.label}.{want.output_frame}",
                want.alpha_milli,
                got.alpha_milli,
                tolerance.alpha_max_abs_error_milli,
            )


def _compare_text(
    accumulator: _Accumulator,
    expected: TextObservation,
    observed: TextObservation | None,
    side: str,
    tolerance: ToleranceProfile,
) -> None:
    if observed is None:
        accumulator.fail("text", f"{side}.text", "present", "absent")
        return
    for name, want, got in (
        ("content", expected.content, observed.content),
        ("font_identity", expected.font_identity, observed.font_identity),
        ("line_count", expected.line_count, observed.line_count),
        ("weight", expected.weight, observed.weight),
        ("style", expected.style, observed.style),
        ("align", expected.align, observed.align),
    ):
        if want != got:
            # Content is compared exactly and never by pixel similarity or OCR score.
            accumulator.fail("text", f"{side}.text.{name}", want, got)
    if expected.visible_glyph_ratio_milli and observed.visible_glyph_ratio_milli <= 0:
        accumulator.fail(
            "text",
            f"{side}.text.visible_glyph_ratio_milli",
            f"> 0 (expected {expected.visible_glyph_ratio_milli})",
            observed.visible_glyph_ratio_milli,
        )
    observed_lines = _by_label(observed.line_bounds)
    for want_line in expected.line_bounds:
        got_line = observed_lines.get(want_line.label)
        if got_line is None:
            accumulator.fail("text", f"{side}.text.line.{want_line.label}", "present", "absent")
            continue
        for name, want_value in want_line.coordinates():
            got_value = dict(got_line.coordinates())[name]
            error = abs(want_value - got_value)
            accumulator.note(
                f"{side}.text.line.{want_line.label}.{name}",
                got_value,
                tolerance.text_bounds_max_abs_pixels,
            )
            if error > tolerance.text_bounds_max_abs_pixels:
                accumulator.fail(
                    "text",
                    f"{side}.text.line.{want_line.label}.{name}",
                    want_value,
                    got_value,
                    tolerance.text_bounds_max_abs_pixels,
                )


def _compare_output_metadata(
    accumulator: _Accumulator, expected: OutputMetadata, observed: OutputMetadata | None
) -> None:
    if observed is None:
        accumulator.fail("output_metadata", "final.output_metadata", "present", "absent")
        return
    observed_pairs = dict(observed.as_pairs())
    for name, want in expected.as_pairs():
        got = observed_pairs[name]
        accumulator.note(f"final.output_metadata.{name}", got, want)
        if want != got:
            # Omitted or unknown metadata is a mismatch, never a match by absence.
            accumulator.fail("output_metadata", f"final.output_metadata.{name}", want, got)


def _compare_av_drift(
    accumulator: _Accumulator, pairs: Sequence[AvPair], tolerance: ToleranceProfile
) -> None:
    for pair in pairs:
        drift_samples = abs(pair.audio_time_ms - pair.visual_time_ms) * OUTPUT_SAMPLE_RATE / 1000
        accumulator.note(
            f"browser.av_drift.{pair.label}",
            drift_samples,
            tolerance.preview_max_av_drift_samples,
        )
        if drift_samples > tolerance.preview_max_av_drift_samples:
            accumulator.fail(
                "av_drift",
                f"browser.av_drift.{pair.label}",
                f"<= {tolerance.preview_max_av_drift_samples} samples",
                drift_samples,
                tolerance.preview_max_av_drift_samples,
            )


def _compare_onsets(
    accumulator: _Accumulator,
    expected: Sequence[AudioOnset],
    observed: Sequence[AudioOnset],
    side: str,
    bound: int,
) -> None:
    observed_by_label = _onsets_by_label(observed)
    for want in expected:
        got = observed_by_label.get(want.label)
        if got is None:
            accumulator.fail("audio_onset", f"{side}.onset.{want.label}", "present", "absent")
            continue
        residual = abs(want.sample_index - got.sample_index)
        accumulator.note(f"{side}.onset.{want.label}.residual", residual, bound)
        if residual > bound:
            accumulator.fail(
                "audio_onset",
                f"{side}.onset.{want.label}.residual",
                f"<= {bound} samples",
                residual,
                bound,
            )


def _compare_silences(
    accumulator: _Accumulator,
    expected: Sequence[SilenceInterval],
    observed: Sequence[SilenceInterval],
    side: str,
    tolerance: ToleranceProfile,
) -> None:
    observed_by_label = {item.label: item for item in observed}
    for want in expected:
        got = observed_by_label.get(want.label)
        if got is None:
            accumulator.fail("audio_silence", f"{side}.silence.{want.label}", "present", "absent")
            continue
        # A gap shorter than twice the codec exclusion has no interior left to inspect, so the
        # check would pass on an empty interval. That is a fixture defect, and it is reported.
        interior = got.interior_length(tolerance.codec_boundary_exclusion_samples)
        accumulator.note(f"{side}.silence.{want.label}.interior_samples", interior)
        if interior <= 0:
            accumulator.fail(
                "audio_silence",
                f"{side}.silence.{want.label}.interior_samples",
                f"> 0 after excluding {tolerance.codec_boundary_exclusion_samples} per boundary",
                interior,
            )
            continue
        accumulator.note(
            f"{side}.silence.{want.label}.abs_peak", got.abs_peak, tolerance.silence_max_abs_peak
        )
        if got.abs_peak > tolerance.silence_max_abs_peak:
            accumulator.fail(
                "audio_silence",
                f"{side}.silence.{want.label}.abs_peak",
                f"<= {tolerance.silence_max_abs_peak}",
                got.abs_peak,
                tolerance.silence_max_abs_peak,
            )


def _compare_audio_extent(
    accumulator: _Accumulator,
    expected: int,
    observed: int | None,
    side: str,
    tolerance: ToleranceProfile,
) -> None:
    if observed is None:
        accumulator.fail("audio_extent", f"{side}.audio_extent_samples", "present", "absent")
        return
    error = abs(expected - observed)
    accumulator.note(f"{side}.audio_extent_samples", observed, tolerance.duration_max_abs_samples)
    if error > tolerance.duration_max_abs_samples:
        accumulator.fail(
            "audio_extent",
            f"{side}.audio_extent_samples",
            expected,
            observed,
            tolerance.duration_max_abs_samples,
        )


def audio_level_bound_ppm(expected_ppm: int, tolerance: ToleranceProfile) -> Fraction:
    """How far a window's level may be from the defined one, in millionths of the tone's level.

    Relative to the defined level, and never below the floor: near silence a relative bound shrinks
    below what a lossy encode holds, while a muted window must still read as silence.
    """

    return max(
        Fraction(tolerance.audio_level_floor_ppm),
        Fraction(expected_ppm * tolerance.audio_level_relative_error_ppm, 1_000_000),
    )


def _compare_audio_levels(
    accumulator: _Accumulator,
    expected: Sequence[AudioLevel],
    observed: Sequence[AudioLevel],
    side: str,
    tolerance: ToleranceProfile,
) -> None:
    observed_by_label = {item.label: item for item in observed}
    for want in expected:
        got = observed_by_label.get(want.label)
        subject = f"{side}.audio_level.{want.label}"
        if got is None:
            accumulator.fail("audio_level", subject, "present", "absent")
            continue
        if (got.start_sample, got.sample_count) != (want.start_sample, want.sample_count):
            # A level measured over another window is that window's, never this one's.
            accumulator.fail(
                "audio_level",
                f"{subject}.window",
                f"{want.start_sample}+{want.sample_count}",
                f"{got.start_sample}+{got.sample_count}",
            )
            continue
        bound = audio_level_bound_ppm(want.level_ppm, tolerance)
        accumulator.note(subject, got.level_ppm, bound)
        if abs(got.level_ppm - want.level_ppm) > bound:
            accumulator.fail("audio_level", subject, want.level_ppm, got.level_ppm, bound)


def _compare_layer_order(
    accumulator: _Accumulator, expected: Sequence[str], observed: Sequence[str], side: str
) -> None:
    if tuple(expected) != tuple(observed):
        # Layer order is exact. A wrong order can leave every patch mean intact.
        accumulator.fail("identity", f"{side}.layer_order", list(expected), list(observed))


#: The observers a case may be compared against. `final` is the independently decoded artifact and
#: is never optional -- a case with no artifact has nothing to be conformant about.
COMPARISON_SIDES: Final = ("browser", "final")


def _blocked_reason(
    browser: BrowserObservation, final: FinalObservation, sides: tuple[str, ...]
) -> tuple[str, str] | None:
    """Return `(status, reason)` when the evidence cannot support a comparison at all."""

    observers: list[BrowserObservation | FinalObservation] = [final]
    if "browser" in sides:
        observers.append(browser)
    if any(not item.executed for item in observers):
        return ("NOT_RUN", "the case did not execute on one or both sides")
    missing = sorted({item for observer in observers for item in observer.missing})
    if missing:
        return ("BLOCKED", f"observation is missing: {', '.join(missing)}")
    if not final.extractor_fingerprint:
        return ("BLOCKED", "the independent extractor did not identify itself")
    return None


def compare_case(
    expectation: Expectation,
    browser: BrowserObservation,
    final: FinalObservation,
    tolerance: ToleranceProfile,
    *,
    supported: bool = True,
    declared_unsupported_reason: str | None = None,
    sides: tuple[str, ...] = COMPARISON_SIDES,
    browser_expectation: Expectation | None = None,
) -> CaseOutcome:
    """Compare one case three ways and return its closed disposition.

    The fixture expectation is compared against the browser and against the independently extracted
    final artifact separately, and the two sides' audio residuals are each held to their own bound.
    A supported case passes only when both comparisons agree.

    `browser_expectation`, when given, is what the browser side is held to in place of
    `expectation`: the same composition as the browser presents it
    (`semantic_conformance_expect.derive_browser_expectation`) -- the colours its decoder
    presents, and only the landmarks the preview can carry. It must describe the same case.
    Every browser-side comparison reads it; every final-side comparison reads `expectation`.

    GUARD: `sides` narrows which observers a case is *entitled to have*, never which comparisons are
    convenient to skip. It exists for rows whose case contract has no browser observation at all --
    an accepted timeline command is judged on the pair of artifacts it renders, and there is no
    canvas anywhere in that claim. Dropping a side a case does have would recreate exactly the
    vacuity this comparator exists to prevent, because an expectation compared against nothing
    passes.
    """

    if expectation.case_id != final.case_id:
        raise _reject("case identifiers disagree across the three observations")
    if "final" not in sides or not set(sides) <= set(COMPARISON_SIDES):
        raise _reject(f"{sides} is not a comparable set of observers")
    compare_browser = "browser" in sides
    if compare_browser and expectation.case_id != browser.case_id:
        raise _reject("case identifiers disagree across the three observations")
    if browser_expectation is None:
        browser_expectation = expectation
    elif browser_expectation.case_id != expectation.case_id:
        raise _reject("the browser expectation describes a different case")

    if not supported:
        if declared_unsupported_reason is None:
            raise _reject(f"{expectation.case_id} is unsupported without a declared reason")
        # A predeclared negative still has to prove its negative; it is never an unexecuted skip.
        if compare_browser and browser.executed is False:
            return CaseOutcome(
                expectation.case_id, "NOT_RUN", reason="declared negative did not execute"
            )
        return CaseOutcome(
            expectation.case_id,
            "DECLARED_UNSUPPORTED",
            reason=_reject_private(declared_unsupported_reason, "unsupported reason"),
        )

    blocked = _blocked_reason(browser, final, sides)
    if blocked is not None:
        status, reason = blocked
        return CaseOutcome(expectation.case_id, status, reason=reason)

    accumulator = _Accumulator()

    if expectation.refusal_code is not None:
        # A refusal case proves the exact code and the absence of effect. It is a passing negative,
        # not an unexecuted "unsupported" row, and it must produce no artifact: a refusal that still
        # renders something is the failure mode a code-only assertion cannot see.
        if compare_browser:
            accumulator.note("browser.refusal_code", browser.refusal_code)
            if browser.refusal_code != expectation.refusal_code:
                accumulator.fail(
                    "refusal_code",
                    "browser.refusal_code",
                    expectation.refusal_code,
                    browser.refusal_code,
                )
        if final.frame_grid is not None or final.output_metadata is not None:
            accumulator.fail(
                "refusal_code",
                "final.artifact",
                "no rendered artifact",
                "an artifact was produced",
            )
        return _finalize(expectation.case_id, accumulator)

    if browser_expectation.layer_order and compare_browser:
        _compare_layer_order(
            accumulator, browser_expectation.layer_order, browser.layer_order, "browser"
        )
    if expectation.layer_order:
        _compare_layer_order(accumulator, expectation.layer_order, final.layer_order, "final")

    if expectation.frame_grid is not None:
        if final.frame_grid is None:
            accumulator.fail("frame_grid", "final.frame_grid", "present", "absent")
        else:
            _compare_frame_grid(accumulator, expectation.frame_grid, final.frame_grid, "final")

    if expectation.frame_timing is not None:
        # A fixture that declares a timeline requires a measured one to compare it against. An
        # absent table is missing evidence, which the collector reports through `missing` so the
        # case blocks; reaching here with none means the observation claimed to be complete and
        # was not.
        if final.frame_timing is None:
            accumulator.fail("frame_grid", "final.frame_timing", "measured", "absent")
        else:
            _compare_frame_timing(
                accumulator, expectation.frame_timing, final.frame_timing, "final"
            )

    if browser_expectation.source_mapping and compare_browser:
        _compare_browser_source_mapping(
            accumulator, browser_expectation.source_mapping, browser.source_mapping
        )
    if expectation.source_mapping:
        _compare_final_source_mapping(accumulator, expectation.source_mapping, final.source_mapping)

    if browser_expectation.geometry and compare_browser:
        if browser.canvas_width <= 0 or browser.canvas_height <= 0:
            # Without actual backing dimensions a pixel tolerance is meaningless: CSS
            # coordinates scale with the viewport and would let any error fit inside two
            # "pixels".
            accumulator.fail(
                "geometry",
                "browser.canvas_dimensions",
                "actual backing dimensions",
                f"{browser.canvas_width}x{browser.canvas_height}",
            )
        _compare_geometry(
            accumulator, browser_expectation.geometry, browser.geometry, "browser", tolerance
        )
    if expectation.geometry:
        _compare_geometry(accumulator, expectation.geometry, final.geometry, "final", tolerance)

    for side, observed in _observed_pairs(sides, browser.patches, final.patches):
        wanted = browser_expectation.patches if side == "browser" else expectation.patches
        if wanted:
            _compare_patches(
                accumulator,
                wanted,
                observed,
                side,
                tolerance,
                mismatch_class="patch",
                bound=tolerance.patch_max_abs_channel,
            )

    for side, observed in _observed_pairs(sides, browser.color_patches, final.color_patches):
        wanted = (
            browser_expectation.color_patches if side == "browser" else expectation.color_patches
        )
        if wanted:
            _compare_patches(
                accumulator,
                wanted,
                observed,
                side,
                tolerance,
                mismatch_class="color_adjust",
                bound=tolerance.color_adjust_max_abs_channel,
            )

    if browser_expectation.alphas and compare_browser:
        _compare_alphas(
            accumulator, browser_expectation.alphas, browser.alphas, "browser", tolerance
        )
    if expectation.alphas:
        _compare_alphas(accumulator, expectation.alphas, final.alphas, "final", tolerance)

    if browser_expectation.text is not None and compare_browser:
        _compare_text(accumulator, browser_expectation.text, browser.text, "browser", tolerance)
    if expectation.text is not None:
        _compare_text(accumulator, expectation.text, final.text, "final", tolerance)

    if expectation.output_metadata is not None:
        _compare_output_metadata(accumulator, expectation.output_metadata, final.output_metadata)

    if compare_browser and browser.av_pairs:
        accumulator.note("browser.clock_uncertainty_ms", browser.clock_uncertainty_ms)
        _compare_av_drift(accumulator, browser.av_pairs, tolerance)

    # Each side meets its own bound independently; a good final onset never excuses a bad
    # preview onset, and neither is averaged into the other.
    if browser_expectation.audio_onsets and compare_browser:
        _compare_onsets(
            accumulator,
            browser_expectation.audio_onsets,
            browser.audio_onsets,
            "browser",
            tolerance.preview_max_av_drift_samples,
        )
    if expectation.audio_onsets:
        _compare_onsets(
            accumulator,
            expectation.audio_onsets,
            final.audio_onsets,
            "final",
            tolerance.final_impulse_tolerance_samples,
        )

    if browser_expectation.silences and compare_browser:
        _compare_silences(
            accumulator,
            browser_expectation.silences,
            browser.silence_probes,
            "browser",
            tolerance,
        )
    if expectation.silences:
        _compare_silences(accumulator, expectation.silences, final.silences, "final", tolerance)

    if expectation.audio_extent_samples is not None:
        _compare_audio_extent(
            accumulator,
            expectation.audio_extent_samples,
            final.audio_extent_samples,
            "final",
            tolerance,
        )

    # The final side only: the preview has no measurement of a level (`BROWSER_UNOBSERVABLE`).
    if expectation.audio_levels:
        _compare_audio_levels(
            accumulator, expectation.audio_levels, final.audio_levels, "final", tolerance
        )

    return _finalize(expectation.case_id, accumulator)


def _finalize(case_id: str, accumulator: _Accumulator) -> CaseOutcome:
    if accumulator.mismatches:
        return CaseOutcome(
            case_id,
            "MISMATCH",
            mismatches=tuple(accumulator.mismatches),
            measurements=tuple(accumulator.measurements),
        )
    return CaseOutcome(case_id, "PASS", measurements=tuple(accumulator.measurements))


@dataclass(frozen=True, slots=True)
class ReportSummary:
    """Per-status totals. They are reported separately and never collapsed into one percentage."""

    totals: Mapping[str, int]
    required_rows: int
    executed_rows: int

    def conformant(self) -> bool:
        return (
            self.totals.get("MISMATCH", 0) == 0
            and self.totals.get("BLOCKED", 0) == 0
            and self.totals.get("NOT_RUN", 0) == 0
            and self.executed_rows == self.required_rows
        )


def summarize(outcomes: Sequence[CaseOutcome], required_rows: int) -> ReportSummary:
    """Summarize outcomes without hiding an unexecuted required row behind a ratio."""

    totals = dict.fromkeys(REPORT_STATUSES, 0)
    for outcome in outcomes:
        totals[outcome.status] += 1
    executed = sum(count for status, count in totals.items() if status != "NOT_RUN")
    return ReportSummary(totals=totals, required_rows=required_rows, executed_rows=executed)
