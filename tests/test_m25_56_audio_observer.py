"""Sample-accurate, packet-boundary tests for the bounded WASAPI reducer."""

from __future__ import annotations

import ctypes
import importlib.util
import math
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest

from scripts.process_audio_observer import PacketMetricsReducer, PacketTimelineTracker


def test_pure_reducers_import_without_windows_callback_abi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(ctypes, "WINFUNCTYPE", raising=False)
    source = Path(__file__).resolve().parents[1] / "scripts/process_audio_observer.py"
    spec = importlib.util.spec_from_file_location("portable_audio_reducers", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reducer = module.PacketMetricsReducer(sample_rate=48_000, channels=2)
    reducer.consume(np.zeros((96, 2), dtype=np.float32))
    result = reducer.finish()
    assert result["samples_analyzed"] == 96
    assert result["raw_audio_retained"] is False
    tracker = module.PacketTimelineTracker()
    tracker.observe(device_position=1_000, qpc_position=10_000_000, frames=96, flags=0)
    assert tracker.finish()["valid"] is True


def _stereo(samples: npt.NDArray[Any]) -> npt.NDArray[np.float32]:
    return np.repeat(samples.astype(np.float32)[:, None], 2, axis=1)


def _feed_irregular_packets(reducer: PacketMetricsReducer, samples: npt.NDArray[Any]) -> None:
    packet_sizes = (317, 997, 113, 1409, 509)
    offset = 0
    packet = 0
    while offset < len(samples):
        end = min(len(samples), offset + packet_sizes[packet % len(packet_sizes)])
        reducer.consume(_stereo(samples[offset:end]))
        offset = end
        packet += 1


def test_silence_runs_keep_exact_sample_length_across_packet_boundaries() -> None:
    sample_rate = 48_000
    count = sample_rate * 5
    indices = np.arange(count, dtype=np.float64)
    tone = (0.25 * np.sin(2 * math.pi * 440 * indices / sample_rate)).astype(np.float32)
    expected = [(48_173, 768), (96_311, 960), (144_257, 1_920)]
    for start, duration in expected:
        tone[start : start + duration] = 0

    reducer = PacketMetricsReducer(sample_rate=sample_rate, channels=2)
    _feed_irregular_packets(reducer, tone)
    result = reducer.finish()

    observed = [
        (row["channel"], row["start_sample"], row["duration_samples"])
        for row in result["silence_runs"]
        if row["duration_samples"] >= 48
    ]
    assert sorted(observed) == sorted(
        (channel, start, duration) for channel in (0, 1) for start, duration in expected
    )
    assert result["raw_audio_retained"] is False


@pytest.mark.parametrize("spacing_ms", [16, 20, 40, 62])
def test_close_impulses_are_not_merged_by_legacy_100ms_onset_window(
    spacing_ms: int,
) -> None:
    starts = [24_137, 24_137 + spacing_ms * 48]
    samples = np.zeros(48_000 * 2, dtype=np.float32)
    for start in starts:
        samples[start : start + 48] = 0.9

    reducer = PacketMetricsReducer(sample_rate=48_000, channels=2)
    _feed_irregular_packets(reducer, samples)
    result = reducer.finish()

    assert sorted(
        (row["channel"], row["start_sample"], row["duration_samples"])
        for row in result["impulse_events"]
    ) == sorted((channel, start, 48) for channel in (0, 1) for start in starts)


def test_continuous_tone_is_not_reported_as_silence_or_impulses() -> None:
    sample_rate = 48_000
    indices = np.arange(sample_rate, dtype=np.float64)
    tone = (0.25 * np.sin(2 * math.pi * 440 * indices / sample_rate)).astype(np.float32)

    reducer = PacketMetricsReducer(sample_rate=sample_rate, channels=2)
    _feed_irregular_packets(reducer, tone)
    result = reducer.finish()

    assert result["silence_runs"] == []
    assert result["impulse_events"] == []
    assert result["samples_analyzed"] == sample_rate
    assert result["envelopes"]
    assert all(len(row["channel_rms"]) == 2 for row in result["envelopes"])
    assert all(len(row["channel_frequency_hz"]) == 2 for row in result["envelopes"])
    assert all(len(row["channel_clipped_samples"]) == 2 for row in result["envelopes"])


def test_one_channel_dropout_is_reported_without_masking_the_other_channel() -> None:
    sample_rate = 48_000
    indices = np.arange(sample_rate, dtype=np.float64)
    tone = (0.25 * np.sin(2 * math.pi * 440 * indices / sample_rate)).astype(np.float32)
    stereo = np.column_stack((tone, np.zeros_like(tone)))

    reducer = PacketMetricsReducer(sample_rate=sample_rate, channels=2)
    packet_sizes = (317, 997, 113, 1409, 509)
    offset = 0
    packet = 0
    while offset < len(stereo):
        end = min(len(stereo), offset + packet_sizes[packet % len(packet_sizes)])
        reducer.consume(stereo[offset:end])
        offset = end
        packet += 1
    result = reducer.finish()

    assert result["silence_runs"] == [
        {"channel": 1, "start_sample": 0, "duration_samples": sample_rate}
    ]
    tone_window = next(row for row in result["envelopes"] if row["start_sample"] == 0)
    assert tone_window["channel_rms"][0] == pytest.approx(0.1768, abs=0.002)
    assert tone_window["channel_rms"][1] == 0
    assert tone_window["channel_frequency_hz"][0] == pytest.approx(440, abs=1)
    assert tone_window["channel_frequency_hz"][1] == 0


@pytest.mark.parametrize("frequency_hz", [220, 440, 880, 1000])
def test_envelope_frequency_estimate_is_sample_accurate(frequency_hz: int) -> None:
    sample_rate = 48_000
    indices = np.arange(sample_rate // 100, dtype=np.float64)
    tone = (0.25 * np.sin(2 * math.pi * frequency_hz * indices / sample_rate)).astype(np.float32)

    reducer = PacketMetricsReducer(sample_rate=sample_rate, channels=2)
    reducer.consume(_stereo(tone))
    result = reducer.finish()

    assert result["envelopes"][0]["channel_frequency_hz"] == pytest.approx(
        [frequency_hz, frequency_hz], abs=1
    )


def test_sample_and_aggregate_limits_fail_closed() -> None:
    reducer = PacketMetricsReducer(
        sample_rate=48_000,
        channels=2,
        max_samples=100,
    )
    with pytest.raises(RuntimeError, match="capture_metadata_bound"):
        reducer.consume(np.zeros((101, 2), dtype=np.float32))


def test_packet_timeline_requires_contiguous_device_positions() -> None:
    tracker = PacketTimelineTracker()
    assert tracker.observe(device_position=1_000, qpc_position=10_000_000, frames=480, flags=0) == (
        None,
        None,
        None,
        0,
    )
    assert tracker.observe(device_position=1_480, qpc_position=10_100_000, frames=480, flags=0) == (
        0,
        100_000,
        0.0,
        0,
    )
    assert tracker.finish() == {
        "clockSource": "device_position",
        "valid": True,
        "positionBreaks": 0,
        "positionGaps": 0,
        "positionOverlaps": 0,
        "discontinuityPackets": 0,
        "timestampErrorPackets": 0,
        "qpcValid": True,
        "qpcBreaks": 0,
        "qpcGaps": 0,
        "qpcOverlaps": 0,
        "qpcGapFramesFilled": 0,
    }


@pytest.mark.parametrize(
    ("flags", "position", "expected"),
    [
        (0, 1_965, 5),  # five device frames disappeared between packets
        (0, 1_955, -5),  # five device frames overlap the prior packet
        (1, 1_960, None),  # WASAPI marks a stream discontinuity
        (4, 1_960, 0),  # the QPC timestamp is explicitly unreliable
    ],
)
def test_packet_timeline_marks_capture_breaks_invalid(
    flags: int, position: int, expected: int | None
) -> None:
    tracker = PacketTimelineTracker()
    tracker.observe(device_position=1_000, qpc_position=10_000_000, frames=960, flags=0)
    observed = tracker.observe(
        device_position=position,
        qpc_position=10_200_000,
        frames=480,
        flags=flags,
    )
    assert observed[0] == expected
    if flags & 5:
        assert observed[1:] == (None, None, 0)
    else:
        assert observed[1:] == (200_000, 0.0, 0)
    result = tracker.finish()
    assert result["valid"] is False
    assert result["positionBreaks"] == int(expected not in (None, 0))


def test_constant_zero_device_position_fills_qpc_gap_as_silence() -> None:
    tracker = PacketTimelineTracker()
    assert tracker.observe(device_position=0, qpc_position=10_000_000, frames=480, flags=0) == (
        None,
        None,
        None,
        0,
    )
    assert tracker.observe(device_position=0, qpc_position=10_100_000, frames=480, flags=0) == (
        None,
        100_000,
        0.0,
        0,
    )
    assert tracker.finish()["clockSource"] == "qpc"
    assert tracker.finish()["valid"] is True

    assert tracker.observe(device_position=0, qpc_position=10_400_000, frames=480, flags=0) == (
        None,
        300_000,
        200_000.0,
        960,
    )
    assert tracker.finish()["valid"] is True
    assert tracker.finish()["qpcValid"] is True
    assert tracker.finish()["qpcBreaks"] == 1
    assert tracker.finish()["qpcGaps"] == 1
    assert tracker.finish()["qpcGapFramesFilled"] == 960


def test_qpc_overlap_is_unrecoverable_and_device_position_remains_authoritative() -> None:
    tracker = PacketTimelineTracker()
    tracker.observe(device_position=1_000, qpc_position=10_000_000, frames=960, flags=0)
    result = tracker.observe(device_position=1_960, qpc_position=10_100_000, frames=480, flags=0)
    assert result == (0, 100_000, -100_000.0, 0)
    assert tracker.finish()["clockSource"] == "device_position"
    assert tracker.finish()["valid"] is True
    assert tracker.finish()["qpcValid"] is False


def test_qpc_fallback_rejects_overlaps_and_gaps_over_the_synthesis_bound() -> None:
    overlap = PacketTimelineTracker()
    overlap.observe(device_position=0, qpc_position=10_000_000, frames=480, flags=0)
    assert overlap.observe(device_position=0, qpc_position=10_050_000, frames=480, flags=0) == (
        None,
        50_000,
        -50_000.0,
        0,
    )
    assert overlap.finish()["clockSource"] == "qpc"
    assert overlap.finish()["valid"] is False
    assert overlap.finish()["qpcOverlaps"] == 1

    oversize_gap = PacketTimelineTracker()
    oversize_gap.observe(device_position=0, qpc_position=10_000_000, frames=480, flags=0)
    assert (
        oversize_gap.observe(device_position=0, qpc_position=461_000_000, frames=480, flags=0)[3]
        == 0
    )
    assert oversize_gap.finish()["valid"] is False
    assert oversize_gap.finish()["qpcGaps"] == 1
    assert oversize_gap.finish()["qpcGapFramesFilled"] == 0


def test_synthetic_qpc_gap_silence_is_included_in_sample_domain_metrics() -> None:
    reducer = PacketMetricsReducer(sample_rate=48_000, channels=2)
    reducer.consume(np.full((480, 2), 0.2, dtype=np.float32))
    reducer.consume_silence(960)
    reducer.consume(np.full((480, 2), 0.2, dtype=np.float32))
    result = reducer.finish()
    assert result["samples_analyzed"] == 1_920
    assert result["silence_runs"] == [
        {"channel": 0, "start_sample": 480, "duration_samples": 960},
        {"channel": 1, "start_sample": 480, "duration_samples": 960},
    ]
