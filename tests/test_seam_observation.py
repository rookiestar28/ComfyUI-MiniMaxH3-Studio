"""Synthetic fixtures for the seam observation metrics (S1-S6, S8-S12).

Each metric has a fixture it must fire on and one it must stay quiet on. Several fixtures are
shaped so that a tempting simplification of the metric fails them; those are named after the
mutation they guard against, and the release command log records the executed mutation runs.
"""

from __future__ import annotations

import math
import random

import pytest

from comfyui_h3_context.core import seam_observation as so

WIDTH = 16
HEIGHT = 12
SIZE = WIDTH * HEIGHT


def flat(value: int) -> bytes:
    return bytes([value]) * SIZE


def gradient(offset: int, scale: int = 4) -> bytes:
    """A horizontal ramp shifted by ``offset`` pixels: a continuous pan when ``offset`` steps."""

    return bytes(((x + offset) * scale) % 256 for _ in range(HEIGHT) for x in range(WIDTH))


def textured(seed: int) -> bytes:
    rng = random.Random(seed)  # noqa: S311 - deterministic test fixture, not a secret
    return bytes(rng.randrange(256) for _ in range(SIZE))


def pan(start: int, count: int) -> list[bytes]:
    return [gradient(start + i) for i in range(count)]


# --- S1 --------------------------------------------------------------------------------------


def test_s1_fires_on_a_step_and_stays_quiet_on_a_continuous_pan() -> None:
    step = so.jump_ratio(pan(0, 12), [bytes(min(255, v + 60) for v in f) for f in pan(12, 12)])
    assert step >= 4.0
    assert so.jump_ratio(pan(0, 12), pan(12, 12)) < 2.0


def test_s1_one_unrelated_spike_does_not_suppress_the_seam_median_not_max() -> None:
    # A single non-seam spike as large as the seam. A max-based typical value would equal the
    # seam and report a ratio near 1; the median ignores the outlier.
    before = pan(0, 12)
    before[5] = bytes(min(255, v + 60) for v in before[5])
    after = [bytes(min(255, v + 60) for v in f) for f in pan(12, 12)]
    assert so.jump_ratio(before, after) >= 4.0


def test_s1_floor_is_relative_to_normalised_data_not_absolute() -> None:
    # Static frames with a small but real seam step. The typical difference is zero, so the floor
    # decides: 0.004 makes the step fire; an absolute floor of 1.0 would hide it.
    before = [flat(100)] * 12
    after = [flat(105)] * 12  # 5/255 = 0.0196, ratio 4.9 against the 0.004 floor
    assert so.jump_ratio(before, after) >= 4.0


def test_s1_refuses_a_short_window() -> None:
    with pytest.raises(so.SeamObservationError):
        so.jump_ratio(pan(0, 11), pan(11, 12))


# --- S2, S3, S4 ------------------------------------------------------------------------------


def test_s2_reports_the_mean_luma_step() -> None:
    assert so.luma_step([flat(100)] * 6, [flat(160)] * 6) == pytest.approx(60 / 255)
    assert so.luma_step([flat(100)] * 6, [flat(100)] * 6) == 0.0


def test_s3_falls_on_a_blurred_head_and_holds_on_an_even_segment() -> None:
    body = [textured(i) for i in range(60)]
    blurred = [flat(128)] * 12 + body[12:]
    assert so.head_sharpness(blurred, WIDTH, HEIGHT) < 0.1
    assert so.head_sharpness(body, WIDTH, HEIGHT) == pytest.approx(1.0, rel=0.25)


def test_s4_drift_grows_with_a_tone_shift() -> None:
    def rgb(r: int, g: int, b: int) -> bytes:
        return bytes([r, g, b]) * SIZE

    first = so.appearance_stats([rgb(100, 100, 100)] * 4)
    same = so.appearance_stats([rgb(100, 100, 100)] * 4)
    warm = so.appearance_stats([rgb(130, 100, 80)] * 4)
    drift = so.appearance_drift([first, same, warm])
    assert drift[0] == 0.0 and drift[1] == 0.0
    assert drift[2] > 0.1


# --- audio fixtures --------------------------------------------------------------------------

RATE = 16_000


def noise(count: int, seed: int, amplitude: float = 3_000.0) -> list[int]:
    rng = random.Random(seed)  # noqa: S311 - deterministic test fixture, not a secret
    return [round(rng.gauss(0.0, amplitude)) for _ in range(count)]


def tone(count: int, frequency: float, amplitude: float, rate: int = RATE) -> list[int]:
    return [round(amplitude * math.sin(2 * math.pi * frequency * i / rate)) for i in range(count)]


def test_s5_fires_on_a_low_burst_at_the_seam() -> None:
    film = noise(6 * RATE, 1, 1_000.0)
    seam = 3 * RATE
    burst = tone(RATE, 120.0, 8_000.0)
    loud = list(film)
    for i, value in enumerate(burst):
        loud[seam - RATE // 2 + i] += value
    assert so.band_energy_ratio(loud, RATE, seam) > 3.0
    assert so.band_energy_ratio(film, RATE, seam) < 1.5


def test_s6_fires_on_an_impulse_at_the_seam() -> None:
    film = tone(4 * RATE, 440.0, 5_000.0)
    seam = 2 * RATE
    clicked = list(film)
    clicked[seam] += 20_000
    assert so.click_ratio(clicked, RATE, seam) > 3.0
    assert so.click_ratio(film, RATE, seam) < 1.5


# --- S8 --------------------------------------------------------------------------------------

S8_RATE = 8_000


def test_s8_a_copy_correlates_at_lag_zero() -> None:
    source = noise(8_000, 7)
    result = so.audio_continuation(source, source, S8_RATE)
    assert result.median_max_correlation >= 0.99
    assert result.median_lag_ms == 0.0


def test_s8_unrelated_noise_of_equal_spectrum_stays_low() -> None:
    result = so.audio_continuation(noise(8_000, 7), noise(8_000, 8), S8_RATE)
    assert result.median_max_correlation <= 0.2


def test_s8_finds_a_ten_millisecond_shift_lag_search_required() -> None:
    # Without the lag search the shifted copy would correlate near zero and report lag 0.
    shift = S8_RATE // 100
    source = noise(8_000 + shift, 9)
    successor = [0] * shift + source[: len(source) - shift]
    result = so.audio_continuation(successor, source, S8_RATE)
    assert result.median_max_correlation >= 0.99
    assert abs(result.median_lag_ms - 10.0) <= 1.0


# --- S9, S10, S11 ----------------------------------------------------------------------------


def test_s9_reads_a_twelve_db_bed_drop() -> None:
    seam = 2 * RATE
    film = noise(seam, 3, 2_000.0) + noise(2 * RATE, 4, 2_000.0 / 10 ** (12 / 20))
    assert so.floor_level_step(film, RATE, seam) == pytest.approx(12.0, abs=1.0)


def test_s9_a_constant_bed_with_events_is_not_a_floor_change_percentile_not_mean() -> None:
    # Loud events after the seam over an unchanged bed. A mean would read them as a level step.
    seam = 2 * RATE
    bed = noise(4 * RATE, 5, 1_000.0)
    for start in range(seam + 1_600, 4 * RATE, 3_200):
        for i in range(800):
            bed[start + i] += round(20_000 * math.sin(2 * math.pi * 300 * i / RATE))
    assert so.floor_level_step(bed, RATE, seam) < 1.5


def test_s10_measures_a_half_second_hold_relative_threshold() -> None:
    # A slow pan (frame differences about 0.016) with a 0.5 s hold at the seam. An absolute
    # threshold of 0.2 would count every slow-pan frame as still.
    fps = 24.0
    frames = [gradient(i, scale=1) for i in range(48)]
    hold = [frames[-1]] * 12
    frames += hold + [gradient(48 + i, scale=1) for i in range(48)]
    differences = so.frame_differences(frames)
    seam = 48
    assert 450.0 <= so.freeze_run_ms(differences, seam, fps) <= 550.0


def test_s11_a_low_passed_segment_loses_its_high_band() -> None:
    bright = noise(RATE, 11)
    dull = so.Biquad.lowpass(1_500.0, RATE).apply(
        so.Biquad.lowpass(1_500.0, RATE).apply(noise(RATE, 12))
    )
    fractions = [
        so.high_band_fraction(bright, RATE),
        so.high_band_fraction(noise(RATE, 13), RATE),
        so.high_band_fraction([round(x) for x in dull], RATE),
    ]
    dulling = so.chain_audio_dulling(fractions)
    assert dulling[0] == 1.0
    assert dulling[1] == pytest.approx(1.0, rel=0.2)
    assert dulling[2] < 0.5


# --- S12 -------------------------------------------------------------------------------------


def test_s12_identical_frames_are_at_the_floor_and_a_shift_rises() -> None:
    source = pan(0, 39)
    exact = so.pin_fidelity(source, source)
    assert exact.shown_mean == 0.0 and max(exact.curve) == 0.0
    shifted = so.pin_fidelity(pan(1, 39), source)
    assert shifted.shown_mean > 0.01


def test_s12_indexes_from_the_head_not_the_shown_end() -> None:
    # The untrimmed successor repeats the pin at its head and then moves on. Indexing from the
    # shown end would compare the later frames and report a large difference.
    source = pan(0, 39)
    successor = source + [textured(i) for i in range(50)]
    result = so.pin_fidelity(successor, source)
    assert result.shown_mean == 0.0
    assert len(result.curve) == so.PIN_FRAMES


def test_percentile_and_median_are_exact_on_small_inputs() -> None:
    assert so.median([3.0, 1.0, 2.0]) == 2.0
    assert so.median([4.0, 1.0, 2.0, 3.0]) == 2.5
    assert so.percentile([0.0, 10.0], 10.0) == pytest.approx(1.0)


# --- film-wide report ------------------------------------------------------------------------

FILM_FPS = 24


def _film() -> tuple[list[bytes], list[int], list[int]]:
    """Three 2 s segments: a still, a pan and a brighter still; a click at the second seam."""

    frames = [flat(60)] * 48 + pan(0, 48) + [flat(180)] * 48
    samples = noise(6 * RATE, 3, 1_500.0)
    samples[4 * RATE] += 18_000
    return frames, samples, [48, 96]


def test_film_metrics_equal_the_single_seam_definitions() -> None:
    frames, samples, seams = _film()
    values = so.film_seam_metrics(frames, samples, RATE, FILM_FPS, seams)
    differences = so.frame_differences(frames)
    assert len(values) == len(seams)
    for seam, value in zip(seams, values, strict=True):
        assert isinstance(value, so.FilmSeamMetrics)
        tail = frames[seam - so.SEAM_WINDOW_FRAMES : seam]
        head = frames[seam : seam + so.SEAM_WINDOW_FRAMES]
        seam_sample = seam * RATE // FILM_FPS
        assert value.s1 == pytest.approx(so.jump_ratio(tail, head))
        assert value.s2 == pytest.approx(so.luma_step(tail, head))
        assert value.s5 == pytest.approx(so.band_energy_ratio(samples, RATE, seam_sample))
        assert value.s6 == pytest.approx(so.click_ratio(samples, RATE, seam_sample))
        assert value.s9 == pytest.approx(so.floor_level_step(samples, RATE, seam_sample))
        assert value.s10 == pytest.approx(so.freeze_run_ms(differences, seam - 1, FILM_FPS))
    # The clicked seam stands out from the clean one on S6 alone.
    first, second = values
    assert isinstance(first, so.FilmSeamMetrics) and isinstance(second, so.FilmSeamMetrics)
    assert second.s6 > 3.0 > first.s6


def test_film_metrics_mark_undefined_seams_without_failing_the_others() -> None:
    frames, samples, _seams = _film()
    values = so.film_seam_metrics(frames, samples, RATE, FILM_FPS, [6, 48])
    assert values[0] == "metric_undefined"
    assert isinstance(values[1], so.FilmSeamMetrics)
    silent = so.film_seam_metrics(frames, [0] * (6 * RATE), RATE, FILM_FPS, [48])
    assert silent == ["metric_undefined"]
