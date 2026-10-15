"""Observational seam metrics over decoded frames and audio (S1-S12, without S7).

These measure what a viewer sees and hears where one generated segment meets the next. They are
pure functions over buffers a decoder already produced, so they run in the ordinary backend suite
and in the continuation study harness alike. Nothing here gates generation: the study applies its
own fixed selection rule to the numbers, and the product's seam report only labels them.

Buffer conventions:

* a luma frame is ``width * height`` bytes of 8-bit gray, row-major;
* an RGB frame is ``width * height * 3`` bytes of packed rgb24, row-major;
* audio is a sequence of signed 16-bit mono samples (``array('h')`` or a list of ints) at the
  caller's stated rate (the study decodes 16 kHz).

S7 (exactness) is the existing exact-output probe and is deliberately not repeated here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: S1's relative floor. A seam ratio divides by the window's typical frame difference; on a
#: near-static clip that typical difference approaches zero and every tiny seam would explode.
#: The floor is relative to normalised (0-1) data. An absolute floor in raw units (for example
#: 1.0) silently disables the check, which is the failure the reference analysis recorded.
JUMP_RATIO_FLOOR: Final = 0.004
#: S1 and S3 windows, in frames.
SEAM_WINDOW_FRAMES: Final = 12
#: S2 window, in frames on each side of the seam.
LUMA_STEP_FRAMES: Final = 6
#: S3's body starts at this frame and stops this many frames before the end; it samples every
#: sixth frame.
SHARPNESS_BODY_START: Final = 24
SHARPNESS_BODY_END_MARGIN: Final = 12
SHARPNESS_BODY_STRIDE: Final = 6
#: S5 band and windows.
LOW_BAND_HZ: Final = (60.0, 250.0)
LOW_BAND_HALF_WINDOW_S: Final = 0.5
MEDIAN_WINDOW_S: Final = 1.0
#: S6 window around the seam.
CLICK_HALF_WINDOW_S: Final = 0.020
CLICK_PERCENTILE: Final = 99.0
#: S8 windows and lag search.
CONTINUATION_WINDOW_SAMPLES: Final = 4_000
CONTINUATION_HOP_SAMPLES: Final = 2_000
CONTINUATION_MAX_LAG_S: Final = 0.020
#: S9 windows.
FLOOR_WINDOW_S: Final = 0.020
FLOOR_SPAN_S: Final = 1.0
FLOOR_PERCENTILE: Final = 10.0
#: S10 span around the seam and its relative threshold.
FREEZE_HALF_SPAN_S: Final = 2.0
FREEZE_RELATIVE_THRESHOLD: Final = 0.2
#: S11 band.
HIGH_BAND_HZ: Final = (3_000.0, 7_500.0)
#: S12 pin window and the frames a variable trim would show.
PIN_FRAMES: Final = 39
PIN_SHOWN_FROM: Final = 23
BUTTERWORTH_Q: Final = 1.0 / math.sqrt(2.0)


class SeamObservationError(ValueError):
    """A buffer or argument cannot support the requested measurement."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SeamObservationError(message)


def _check_frames(frames: Sequence[bytes], size: int, label: str) -> None:
    _require(size > 0, f"{label}: frame size must be positive")
    for index, frame in enumerate(frames):
        _require(len(frame) == size, f"{label}: frame {index} is not {size} bytes")


def median(values: Sequence[float]) -> float:
    _require(len(values) > 0, "median of an empty sequence")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, ``q`` in [0, 100]."""

    _require(len(values) > 0, "percentile of an empty sequence")
    _require(0.0 <= q <= 100.0, "percentile must be within [0, 100]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100.0
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def mean_abs_difference(left: bytes, right: bytes) -> float:
    """``mean|L - R| / 255`` over two equally sized 8-bit buffers."""

    _require(len(left) == len(right) and len(left) > 0, "frames must be equal and non-empty")
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / (255.0 * len(left))


def frame_differences(frames: Sequence[bytes]) -> list[float]:
    """``d(i) = mean|Y_i - Y_{i-1}| / 255`` for ``i >= 1``."""

    _require(len(frames) >= 2, "frame differences need at least two frames")
    return [mean_abs_difference(frames[i], frames[i - 1]) for i in range(1, len(frames))]


def jump_ratio(predecessor_tail: Sequence[bytes], successor_head: Sequence[bytes]) -> float:
    """S1: the seam's frame difference relative to the window's typical difference.

    The window is the last 12 luma frames of the predecessor and the first 12 of the successor.
    The seam pair is excluded from the typical value, which is a median so that one unrelated
    spike elsewhere in the window cannot suppress a real seam jump.
    """

    _require(
        len(predecessor_tail) == SEAM_WINDOW_FRAMES and len(successor_head) == SEAM_WINDOW_FRAMES,
        f"S1 needs {SEAM_WINDOW_FRAMES} frames on each side",
    )
    window = [*predecessor_tail, *successor_head]
    _check_frames(window, len(window[0]), "S1")
    differences = frame_differences(window)
    seam_index = SEAM_WINDOW_FRAMES - 1
    seam = differences[seam_index]
    others = differences[:seam_index] + differences[seam_index + 1 :]
    return seam / max(median(others), JUMP_RATIO_FLOOR)


def _mean_luma(frames: Sequence[bytes]) -> float:
    total = sum(sum(frame) for frame in frames)
    count = sum(len(frame) for frame in frames)
    return total / count


def luma_step(predecessor_tail: Sequence[bytes], successor_head: Sequence[bytes]) -> float:
    """S2: ``|mean Y(last 6 of N) - mean Y(first 6 of N+1)| / 255``."""

    _require(
        len(predecessor_tail) >= LUMA_STEP_FRAMES and len(successor_head) >= LUMA_STEP_FRAMES,
        f"S2 needs {LUMA_STEP_FRAMES} frames on each side",
    )
    before = predecessor_tail[-LUMA_STEP_FRAMES:]
    after = successor_head[:LUMA_STEP_FRAMES]
    _check_frames([*before, *after], len(before[0]), "S2")
    return abs(_mean_luma(before) - _mean_luma(after)) / 255.0


def laplacian_variance(frame: bytes, width: int, height: int) -> float:
    """Variance of the 4-neighbour Laplacian over the frame's interior."""

    _require(width >= 3 and height >= 3, "the Laplacian needs at least a 3x3 frame")
    _require(len(frame) == width * height, "luma frame size does not match its geometry")
    values: list[int] = []
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            index = row + x
            values.append(
                frame[index - 1]
                + frame[index + 1]
                + frame[index - width]
                + frame[index + width]
                - 4 * frame[index]
            )
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def head_sharpness(frames: Sequence[bytes], width: int, height: int) -> float:
    """S3: mean Laplacian variance of the first 12 frames over that of the segment body.

    The body is frames 24 to -12, every sixth frame. A value well below 1 means the head is
    softer than the rest of the segment, the signature of a re-drawn guide settling in.
    """

    body = list(
        frames[
            SHARPNESS_BODY_START : len(frames) - SHARPNESS_BODY_END_MARGIN : SHARPNESS_BODY_STRIDE
        ]
    )
    _require(len(frames) >= SEAM_WINDOW_FRAMES and len(body) > 0, "S3 needs a head and a body")
    head = frames[:SEAM_WINDOW_FRAMES]
    head_value = sum(laplacian_variance(f, width, height) for f in head) / len(head)
    body_value = sum(laplacian_variance(f, width, height) for f in body) / len(body)
    _require(body_value > 0.0, "S3 body has no texture")
    return head_value / body_value


@dataclass(frozen=True)
class AppearanceStats:
    """Per-channel RGB mean and standard deviation, in 0-255 units."""

    mean: tuple[float, float, float]
    std: tuple[float, float, float]

    def distance(self, other: AppearanceStats) -> float:
        """Euclidean distance over the six values, normalised by 255."""

        pairs = [*zip(self.mean, other.mean, strict=True), *zip(self.std, other.std, strict=True)]
        return math.sqrt(sum((a - b) ** 2 for a, b in pairs)) / 255.0


def appearance_stats(frames: Sequence[bytes]) -> AppearanceStats:
    """S4 input: RGB statistics of a segment body (packed rgb24 frames)."""

    _require(len(frames) > 0, "S4 needs at least one frame")
    _check_frames(frames, len(frames[0]), "S4")
    _require(len(frames[0]) % 3 == 0, "S4 frames must be packed rgb24")
    sums = [0, 0, 0]
    squares = [0, 0, 0]
    count = 0
    for frame in frames:
        for channel in range(3):
            plane = frame[channel::3]
            sums[channel] += sum(plane)
            squares[channel] += sum(value * value for value in plane)
        count += len(frame) // 3
    means = tuple(total / count for total in sums)
    stds = tuple(math.sqrt(max(squares[c] / count - means[c] ** 2, 0.0)) for c in range(3))
    return AppearanceStats(mean=(means[0], means[1], means[2]), std=(stds[0], stds[1], stds[2]))


def appearance_drift(segments: Sequence[AppearanceStats]) -> list[float]:
    """S4: the distance of each segment to segment 1 (the first value is 0)."""

    _require(len(segments) > 0, "S4 needs at least one segment")
    return [segment.distance(segments[0]) for segment in segments]


# --- audio -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Biquad:
    """One RBJ cookbook biquad, normalised so ``a0 == 1``."""

    b0: float
    b1: float
    b2: float
    a1: float
    a2: float

    @classmethod
    def lowpass(cls, cutoff: float, rate: int, q: float = BUTTERWORTH_Q) -> Biquad:
        return cls._design(cutoff, rate, q, high=False)

    @classmethod
    def highpass(cls, cutoff: float, rate: int, q: float = BUTTERWORTH_Q) -> Biquad:
        return cls._design(cutoff, rate, q, high=True)

    @classmethod
    def _design(cls, cutoff: float, rate: int, q: float, *, high: bool) -> Biquad:
        _require(0.0 < cutoff < rate / 2.0, "cutoff must lie below the Nyquist frequency")
        omega = 2.0 * math.pi * cutoff / rate
        alpha = math.sin(omega) / (2.0 * q)
        cos = math.cos(omega)
        a0 = 1.0 + alpha
        if high:
            b0 = (1.0 + cos) / 2.0
            b1 = -(1.0 + cos)
        else:
            b0 = (1.0 - cos) / 2.0
            b1 = 1.0 - cos
        return cls(b0 / a0, b1 / a0, b0 / a0, -2.0 * cos / a0, (1.0 - alpha) / a0)

    def apply(self, samples: Sequence[float]) -> list[float]:
        out: list[float] = []
        x1 = x2 = y1 = y2 = 0.0
        for x in samples:
            y = self.b0 * x + self.b1 * x1 + self.b2 * x2 - self.a1 * y1 - self.a2 * y2
            out.append(y)
            x2, x1 = x1, x
            y2, y1 = y1, y
        return out


def band_pass(samples: Sequence[float], rate: int, band: tuple[float, float]) -> list[float]:
    """A high-pass then a low-pass biquad: the band between ``band[0]`` and ``band[1]``."""

    return Biquad.lowpass(band[1], rate).apply(Biquad.highpass(band[0], rate).apply(samples))


def rms(samples: Sequence[float]) -> float:
    _require(len(samples) > 0, "RMS of an empty window")
    return math.sqrt(sum(float(x) * x for x in samples) / len(samples))


def _check_audio(samples: Sequence[int], rate: int, label: str) -> None:
    _require(rate > 0, f"{label}: sample rate must be positive")
    _require(len(samples) > 0, f"{label}: no samples")


def _check_seam(samples: Sequence[int], seam_sample: int, half: int, label: str) -> None:
    _require(
        half <= seam_sample <= len(samples) - half,
        f"{label}: the seam window extends past the samples",
    )


def band_energy_ratio(samples: Sequence[int], rate: int, seam_sample: int) -> float:
    """S5: 60-250 Hz RMS within +/-0.5 s of the seam over the film's median 1 s window."""

    _check_audio(samples, rate, "S5")
    half = round(LOW_BAND_HALF_WINDOW_S * rate)
    _check_seam(samples, seam_sample, half, "S5")
    filtered = band_pass(samples, rate, LOW_BAND_HZ)
    seam_rms = rms(filtered[seam_sample - half : seam_sample + half])
    window = round(MEDIAN_WINDOW_S * rate)
    _require(len(filtered) >= window, "S5 needs at least one whole 1 s window")
    typical = median(
        [rms(filtered[i : i + window]) for i in range(0, len(filtered) - window + 1, window)]
    )
    _require(typical > 0.0, "S5: the film's low band is silent")
    return seam_rms / typical


def click_ratio(samples: Sequence[int], rate: int, seam_sample: int) -> float:
    """S6: the largest sample step within +/-20 ms of the seam over the film's P99 step."""

    _check_audio(samples, rate, "S6")
    half = max(1, round(CLICK_HALF_WINDOW_S * rate))
    _check_seam(samples, seam_sample, half, "S6")
    steps = [abs(samples[i] - samples[i - 1]) for i in range(1, len(samples))]
    seam_steps = steps[seam_sample - half : seam_sample + half - 1]
    typical = percentile(steps, CLICK_PERCENTILE)
    _require(typical > 0.0, "S6: the film has no sample steps")
    return max(seam_steps) / typical


@dataclass(frozen=True)
class AudioContinuation:
    """S8 result: median best normalised correlation and its median lag."""

    median_max_correlation: float
    median_lag_ms: float
    windows: int


def _normalised_correlation(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = 0.0
    left_energy = 0.0
    right_energy = 0.0
    for a, b in zip(left, right, strict=True):
        da = a - left_mean
        db = b - right_mean
        numerator += da * db
        left_energy += da * da
        right_energy += db * db
    if left_energy == 0.0 or right_energy == 0.0:
        return 0.0
    return numerator / math.sqrt(left_energy * right_energy)


def audio_continuation(
    successor_head: Sequence[int], source_tail: Sequence[int], rate: int
) -> AudioContinuation:
    """S8: how closely the successor's untrimmed head audio repeats the pinned source tail.

    Windows of 4,000 samples step by 2,000. For each window, the successor's window is compared
    with the source shifted by every lag within +/-20 ms; the best normalised correlation and its
    lag are kept. A positive lag means the successor's content appears later than the source's.
    """

    _check_audio(successor_head, rate, "S8")
    length = min(len(successor_head), len(source_tail))
    max_lag = round(CONTINUATION_MAX_LAG_S * rate)
    size = CONTINUATION_WINDOW_SAMPLES
    _require(length >= size + 2 * max_lag, "S8 needs a window plus the lag margin")
    best_values: list[float] = []
    best_lags: list[float] = []
    for start in range(max_lag, length - size - max_lag + 1, CONTINUATION_HOP_SAMPLES):
        succ = [float(x) for x in successor_head[start : start + size]]
        best = -2.0
        best_lag = 0
        for lag in range(-max_lag, max_lag + 1):
            source = source_tail[start - lag : start - lag + size]
            value = _normalised_correlation(succ, [float(x) for x in source])
            if value > best:
                best, best_lag = value, lag
        best_values.append(best)
        best_lags.append(best_lag * 1000.0 / rate)
    return AudioContinuation(
        median_max_correlation=median(best_values),
        median_lag_ms=median(best_lags),
        windows=len(best_values),
    )


def _window_rms_values(samples: Sequence[int], window: int) -> list[float]:
    return [rms(samples[i : i + window]) for i in range(0, len(samples) - window + 1, window)]


def floor_level_step(samples: Sequence[int], rate: int, seam_sample: int) -> float:
    """S9: the change in the background floor across the seam, in dB.

    The floor on each side is the 10th percentile of 20 ms RMS windows over 1 s. A percentile,
    not a mean, so speech or effects on one side do not read as a bed level change.
    """

    _check_audio(samples, rate, "S9")
    span = round(FLOOR_SPAN_S * rate)
    _check_seam(samples, seam_sample, span, "S9")
    window = max(1, round(FLOOR_WINDOW_S * rate))
    before = percentile(
        _window_rms_values(samples[seam_sample - span : seam_sample], window), FLOOR_PERCENTILE
    )
    after = percentile(
        _window_rms_values(samples[seam_sample : seam_sample + span], window), FLOOR_PERCENTILE
    )
    _require(before > 0.0 and after > 0.0, "S9: a side of the seam is digital silence")
    return abs(20.0 * math.log10(before / after))


def freeze_run_ms(body_differences: Sequence[float], seam_index: int, fps: float) -> float:
    """S10: the longest near-still run within +/-2 s of the seam, in milliseconds.

    ``body_differences`` are consecutive frame differences over the joined bodies; index ``i``
    is the difference between frames ``i`` and ``i + 1``. The threshold is relative: 0.2 times
    the median difference over the whole span supplied.
    """

    _require(fps > 0.0, "S10: fps must be positive")
    _require(0 <= seam_index < len(body_differences), "S10: the seam is outside the differences")
    threshold = FREEZE_RELATIVE_THRESHOLD * median(body_differences)
    half = round(FREEZE_HALF_SPAN_S * fps)
    start = max(0, seam_index - half)
    stop = min(len(body_differences), seam_index + half)
    longest = current = 0
    for value in body_differences[start:stop]:
        if value < threshold:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest * 1000.0 / fps


def high_band_fraction(samples: Sequence[int], rate: int) -> float:
    """S11 input: the energy fraction of a segment body in 3-7.5 kHz."""

    _check_audio(samples, rate, "S11")
    _require(HIGH_BAND_HZ[1] < rate / 2.0, "S11: the sample rate cannot carry the band")
    total = sum(float(x) * x for x in samples)
    _require(total > 0.0, "S11: the segment is silent")
    band = band_pass(samples, rate, HIGH_BAND_HZ)
    return sum(x * x for x in band) / total


def chain_audio_dulling(fractions: Sequence[float]) -> list[float]:
    """S11: each segment's high-band fraction over segment 1's."""

    _require(len(fractions) > 0 and fractions[0] > 0.0, "S11 needs a non-dull first segment")
    return [value / fractions[0] for value in fractions]


@dataclass(frozen=True)
class PinFidelity:
    """S12: per-pinned-frame difference and its mean over the frames a variable trim shows."""

    curve: tuple[float, ...]
    shown_mean: float


def pin_fidelity(successor_untrimmed: Sequence[bytes], pin_source: Sequence[bytes]) -> PinFidelity:
    """S12: how faithfully the successor's head reproduces the pinned source frames.

    ``pin_source`` is the 39-frame window the pin came from, in order. The successor frames are
    its untrimmed decode; pinned frame ``i`` is the successor's frame ``i`` counted from the head,
    never from the shown end.
    """

    _require(len(pin_source) == PIN_FRAMES, f"S12 needs the {PIN_FRAMES}-frame pin source")
    _require(len(successor_untrimmed) >= PIN_FRAMES, "S12 needs the untrimmed successor head")
    head = successor_untrimmed[:PIN_FRAMES]
    _check_frames([*head, *pin_source], len(pin_source[0]), "S12")
    curve = tuple(mean_abs_difference(head[i], pin_source[i]) for i in range(PIN_FRAMES))
    shown = curve[PIN_SHOWN_FROM:]
    return PinFidelity(curve=curve, shown_mean=sum(shown) / len(shown))


@dataclass(frozen=True)
class FilmSeamMetrics:
    """The six metrics the product's seam report carries for one seam of an assembled film."""

    s1: float
    s2: float
    s5: float
    s6: float
    s9: float
    s10: float


def film_seam_metrics(
    luma_frames: Sequence[bytes],
    samples: Sequence[int],
    rate: int,
    fps: int,
    seam_frames: Sequence[int],
) -> list[FilmSeamMetrics | str]:
    """S1, S2, S5, S6, S9 and S10 at every seam of one film, film-wide statistics computed once.

    ``seam_frames`` are the first frames of each successor. Each value equals the single-seam
    definition above; only the film-wide reference (S5's median 1 s low-band window, S6's P99
    step) is shared between seams instead of being recomputed per seam. A seam whose metric is
    undefined (window past the film, a silent film, a silent side) yields the string
    ``"metric_undefined"`` instead of a number.
    """

    _require(rate > 0 and fps > 0, "film metrics need positive rates")
    _require(len(luma_frames) > 1 and len(samples) > 0, "film metrics need frames and samples")
    _check_frames(luma_frames, len(luma_frames[0]), "film")
    differences = frame_differences(luma_frames)
    filtered = band_pass(samples, rate, LOW_BAND_HZ)
    window = round(MEDIAN_WINDOW_S * rate)
    low_typical = (
        median(
            [rms(filtered[i : i + window]) for i in range(0, len(filtered) - window + 1, window)]
        )
        if len(filtered) >= window
        else 0.0
    )
    steps = [abs(samples[i] - samples[i - 1]) for i in range(1, len(samples))]
    click_typical = percentile(steps, CLICK_PERCENTILE) if steps else 0.0
    low_half = round(LOW_BAND_HALF_WINDOW_S * rate)
    click_half = max(1, round(CLICK_HALF_WINDOW_S * rate))
    results: list[FilmSeamMetrics | str] = []
    for seam in seam_frames:
        try:
            _require(
                SEAM_WINDOW_FRAMES <= seam <= len(luma_frames) - SEAM_WINDOW_FRAMES,
                "the seam window extends past the frames",
            )
            tail = luma_frames[seam - SEAM_WINDOW_FRAMES : seam]
            head = luma_frames[seam : seam + SEAM_WINDOW_FRAMES]
            _require((seam * rate) % fps == 0, "the seam is not on an audio sample")
            seam_sample = seam * rate // fps
            _check_seam(samples, seam_sample, low_half, "S5")
            _require(low_typical > 0.0 and click_typical > 0.0, "the film is silent")
            s5 = rms(filtered[seam_sample - low_half : seam_sample + low_half]) / low_typical
            _check_seam(samples, seam_sample, click_half, "S6")
            s6 = max(steps[seam_sample - click_half : seam_sample + click_half - 1]) / click_typical
            results.append(
                FilmSeamMetrics(
                    s1=jump_ratio(tail, head),
                    s2=luma_step(tail, head),
                    s5=s5,
                    s6=s6,
                    s9=floor_level_step(samples, rate, seam_sample),
                    s10=freeze_run_ms(differences, seam - 1, float(fps)),
                )
            )
        except SeamObservationError:
            results.append("metric_undefined")
    return results
