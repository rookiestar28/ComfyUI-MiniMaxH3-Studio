"""Observe only an explicitly identified test Chromium process tree via WASAPI.

No microphone, default endpoint, system-wide loopback, or raw audio file is opened.
Samples are reduced immediately to bounded peak/onset metadata and then discarded.
"""

from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, TypedDict

import numpy as np
import numpy.typing as npt

HRESULT = C.c_long
DWORD = C.c_ulong
UINT32 = C.c_uint32
UINT64 = C.c_uint64
PTR = C.c_void_p
MAX_OBSERVER_OUTPUT_BYTES = 1_800_000
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1
QPC_TOLERANCE_100NS = 10_000
MAX_SYNTHETIC_GAP_FRAMES = 45 * 48_000


class PacketTimelineTracker:
    """Validate packet continuity using device positions or calibrated per-packet QPC times."""

    def __init__(self) -> None:
        self._expected_position: int | None = None
        self._previous_device_position: int | None = None
        self._position_clock = "undetermined"
        self._previous_qpc: int | None = None
        self._previous_frames: int | None = None
        self._capture_flags_valid = True
        self._position_valid = True
        self._qpc_valid = True
        self.position_breaks = 0
        self.position_gaps = 0
        self.position_overlaps = 0
        self.discontinuity_packets = 0
        self.timestamp_error_packets = 0
        self.qpc_breaks = 0
        self.qpc_gaps = 0
        self.qpc_overlaps = 0
        self.qpc_gap_frames_filled = 0

    def observe(
        self,
        *,
        device_position: int,
        qpc_position: int,
        frames: int,
        flags: int,
    ) -> tuple[int | None, int | None, float | None, int]:
        # IMPORTANT: GetBuffer positions and flags define capture continuity;
        # if the driver leaves the position fixed at zero, validate its QPC clock instead.
        if (
            type(device_position) is not int
            or not 0 <= device_position <= MAX_SAFE_JSON_INTEGER
            or type(qpc_position) is not int
            or not 0 <= qpc_position <= MAX_SAFE_JSON_INTEGER
            or type(frames) is not int
            or not 0 <= frames <= 48_000
            or device_position + frames > MAX_SAFE_JSON_INTEGER
            or qpc_position > MAX_SAFE_JSON_INTEGER
            or type(flags) is not int
            or flags < 0
        ):
            raise RuntimeError("capture_metadata_bound")

        position_delta = None
        qpc_delta = None
        qpc_error = None
        timeline_gap_frames = 0
        discontinuity = bool(flags & 1)
        timestamp_error = bool(flags & 4)
        if discontinuity:
            self.discontinuity_packets += 1
            self._capture_flags_valid = False
        if timestamp_error:
            self.timestamp_error_packets += 1
            self._capture_flags_valid = False
        if flags & ~7:
            self._capture_flags_valid = False

        if self._position_clock == "undetermined":
            if self._previous_device_position is None:
                self._position_clock = "pending_zero" if device_position == 0 else "device_position"
        elif self._position_clock == "pending_zero":
            self._position_clock = "qpc" if device_position == 0 else "device_position"

        if self._previous_qpc is not None and not timestamp_error and not discontinuity:
            qpc_delta = qpc_position - self._previous_qpc
            expected_frames = self._previous_frames
            if expected_frames is None:
                raise RuntimeError("audio_observer_timeline_state")
            qpc_error_numerator = qpc_delta * 48_000 - expected_frames * 10_000_000
            qpc_error = round(qpc_error_numerator / 48_000, 2)
            tolerance_numerator = QPC_TOLERANCE_100NS * 48_000
            if qpc_delta <= 0 or qpc_error_numerator < -tolerance_numerator:
                self.qpc_breaks += 1
                self._qpc_valid = False
                self.qpc_overlaps += 1
            elif qpc_error_numerator > tolerance_numerator:
                self.qpc_breaks += 1
                self.qpc_gaps += 1
                if self._position_clock == "qpc":
                    elapsed_frames = (qpc_delta * 48_000 + 5_000_000) // 10_000_000
                    missing_frames = elapsed_frames - expected_frames
                    if 0 < missing_frames <= MAX_SYNTHETIC_GAP_FRAMES:
                        timeline_gap_frames = missing_frames
                        self.qpc_gap_frames_filled += missing_frames
                    else:
                        self._qpc_valid = False
                else:
                    self._qpc_valid = False

        if self._position_clock == "device_position":
            if self._expected_position is not None and not discontinuity:
                position_delta = device_position - self._expected_position
                if position_delta:
                    self.position_breaks += 1
                    self._position_valid = False
                    if position_delta > 0:
                        self.position_gaps += 1
                    else:
                        self.position_overlaps += 1

        self._expected_position = device_position + frames
        self._previous_device_position = device_position
        if timestamp_error or discontinuity:
            self._previous_qpc = None
            self._previous_frames = None
        else:
            self._previous_qpc = qpc_position
            self._previous_frames = frames
        return position_delta, qpc_delta, qpc_error, timeline_gap_frames

    def finish(self) -> dict[str, int | bool | str]:
        clock_source = (
            "device_position"
            if self._position_clock == "device_position"
            else "qpc"
            if self._position_clock == "qpc"
            else "unavailable"
        )
        valid = self._capture_flags_valid and (
            self._position_valid
            if clock_source == "device_position"
            else self._qpc_valid
            if clock_source == "qpc"
            else False
        )
        return {
            "clockSource": clock_source,
            "valid": valid,
            "positionBreaks": self.position_breaks,
            "positionGaps": self.position_gaps,
            "positionOverlaps": self.position_overlaps,
            "discontinuityPackets": self.discontinuity_packets,
            "timestampErrorPackets": self.timestamp_error_packets,
            "qpcValid": self._qpc_valid,
            "qpcBreaks": self.qpc_breaks,
            "qpcGaps": self.qpc_gaps,
            "qpcOverlaps": self.qpc_overlaps,
            "qpcGapFramesFilled": self.qpc_gap_frames_filled,
        }


class GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]

    def __init__(self, value: str):
        super().__init__((C.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


class Activation(C.Structure):
    _fields_ = [("kind", UINT32), ("pid", DWORD), ("mode", UINT32)]


class Blob(C.Structure):
    _fields_ = [("size", UINT32), ("data", PTR)]


class Variant(C.Structure):
    _fields_ = [("vt", C.c_ushort), ("reserved", C.c_ushort * 3), ("blob", Blob)]


class WaveFormat(C.Structure):
    _pack_ = 2
    _fields_ = [
        ("format", C.c_ushort),
        ("channels", C.c_ushort),
        ("rate", DWORD),
        ("bytes_per_second", DWORD),
        ("align", C.c_ushort),
        ("bits", C.c_ushort),
        ("extra", C.c_ushort),
    ]


class EnvelopeRow(TypedDict):
    start_sample: int
    sample_count: int
    peak: float
    rms: float
    frequency_hz: float
    clipped_samples: int
    channel_rms: list[float]
    channel_frequency_hz: list[float]
    channel_clipped_samples: list[int]


class MetricsResult(TypedDict):
    sample_rate: int
    channels: int
    samples_analyzed: int
    window_samples: int
    silence_floor: float
    silence_runs: list[dict[str, int]]
    impulse_floor: float
    impulse_events: list[dict[str, int | float]]
    envelopes: list[EnvelopeRow]
    raw_audio_retained: bool


class PacketMetricsReducer:
    """Reduce packet PCM to bounded sample-domain evidence without retaining raw audio."""

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        channels: int = 2,
        max_samples: int = 45 * 48000,
        max_events: int = 5000,
        window_ms: int = 10,
        silence_floor: float = 0.01,
        impulse_floor: float = 0.5,
    ) -> None:
        if (
            type(sample_rate) is not int
            or sample_rate != 48000
            or type(channels) is not int
            or channels != 2
            or type(max_samples) is not int
            or not 1 <= max_samples <= 45 * sample_rate
            or type(max_events) is not int
            or not 1 <= max_events <= 5000
            or type(window_ms) is not int
            or window_ms not in {10, 20}
            or not 0 < silence_floor < impulse_floor < 1
        ):
            raise ValueError("invalid_audio_observer_limits")
        self.sample_rate = sample_rate
        self.channels = channels
        self.max_samples = max_samples
        self.max_events = max_events
        self.window_samples = sample_rate * window_ms // 1000
        self.silence_floor = silence_floor
        self.impulse_floor = impulse_floor
        self.samples_analyzed = 0
        self.silence_runs: list[dict[str, int]] = []
        self.impulse_events: list[dict[str, int | float]] = []
        self.envelopes: list[EnvelopeRow] = []
        self._silence_start: list[int | None] = [None] * channels
        self._impulse_start: list[int | None] = [None] * channels
        self._impulse_peak = [0.0] * channels
        self._window_start = 0
        self._window_remainder: npt.NDArray[np.float32] = np.empty((0, channels), dtype=np.float32)
        self._finished = False

    def _close_silence(self, channel: int, end_sample: int) -> None:
        start = self._silence_start[channel]
        if start is None:
            return
        duration = end_sample - start
        if duration >= self.sample_rate // 1000:
            if len(self.silence_runs) >= self.max_events:
                raise RuntimeError("capture_metadata_bound")
            self.silence_runs.append(
                {"channel": channel, "start_sample": start, "duration_samples": duration}
            )
        self._silence_start[channel] = None

    def _close_impulse(self, channel: int, end_sample: int) -> None:
        start = self._impulse_start[channel]
        if start is None:
            return
        duration = end_sample - start
        # Ignore steady high-energy runs; transient events remain distinct at 16 ms and above.
        if 24 <= duration <= self.sample_rate // 100:
            if len(self.impulse_events) >= self.max_events:
                raise RuntimeError("capture_metadata_bound")
            self.impulse_events.append(
                {
                    "channel": channel,
                    "start_sample": start,
                    "duration_samples": duration,
                    "peak": self._impulse_peak[channel],
                }
            )
        self._impulse_start[channel] = None
        self._impulse_peak[channel] = 0.0

    @staticmethod
    def _true_runs(
        mask: npt.NDArray[Any],
    ) -> tuple[npt.NDArray[np.intp], npt.NDArray[np.intp]]:
        edges = np.flatnonzero(np.diff(np.concatenate(([False], mask, [False])).astype(np.int8)))
        return edges[::2], edges[1::2]

    def _reduce_runs(self, amplitude: npt.NDArray[Any], base_sample: int) -> None:
        for channel in range(self.channels):
            for mask, state_name in (
                (amplitude[:, channel] < self.silence_floor, "silence"),
                (amplitude[:, channel] >= self.impulse_floor, "impulse"),
            ):
                if len(mask) and not mask[0]:
                    if state_name == "silence":
                        self._close_silence(channel, base_sample)
                    else:
                        self._close_impulse(channel, base_sample)
                starts, ends = self._true_runs(mask)
                active_start = (
                    self._silence_start[channel]
                    if state_name == "silence"
                    else self._impulse_start[channel]
                )
                for index, (start, end) in enumerate(
                    zip(starts.tolist(), ends.tolist(), strict=True)
                ):
                    absolute_start = base_sample + start
                    absolute_end = base_sample + end
                    if active_start is None:
                        active_start = absolute_start
                        if state_name == "silence":
                            self._silence_start[channel] = absolute_start
                        else:
                            self._impulse_start[channel] = absolute_start
                    elif start > 0:
                        if state_name == "silence":
                            self._close_silence(channel, absolute_start)
                            self._silence_start[channel] = absolute_start
                            active_start = absolute_start
                        else:
                            self._close_impulse(channel, absolute_start)
                            self._impulse_start[channel] = absolute_start
                            self._impulse_peak[channel] = 0.0
                            active_start = absolute_start
                    if state_name == "impulse":
                        self._impulse_peak[channel] = max(
                            self._impulse_peak[channel],
                            float(np.max(amplitude[start:end, channel])),
                        )
                    if end < len(mask):
                        if state_name == "silence":
                            self._close_silence(channel, absolute_end)
                        else:
                            self._close_impulse(channel, absolute_end)
                        active_start = None
                    elif index != len(starts) - 1:
                        raise RuntimeError("audio_observer_run_order")

    def _frequency(self, samples: npt.NDArray[Any]) -> float:
        peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
        if peak < 0.02:
            return 0.0
        threshold = max(0.002, peak * 0.1)
        armed_negative: int | None = None
        rising_crossings: list[float] = []
        for index, sample in enumerate(samples):
            if sample <= -threshold:
                armed_negative = index
            elif sample >= threshold and armed_negative is not None:
                segment = samples[armed_negative : index + 1]
                crossings = np.flatnonzero((segment[:-1] <= 0) & (segment[1:] > 0))
                if len(crossings):
                    offset = int(crossings[0])
                    left = float(segment[offset])
                    right = float(segment[offset + 1])
                    fraction = -left / (right - left)
                    rising_crossings.append(armed_negative + offset + fraction)
                armed_negative = None
        if len(rising_crossings) < 2:
            return 0.0
        periods = np.diff(np.asarray(rising_crossings, dtype=np.float64))
        return round(float(np.median(self.sample_rate / periods)), 2)

    def _append_envelope(self, window: npt.NDArray[Any], start_sample: int) -> None:
        if len(self.envelopes) >= self.max_samples // self.window_samples + 1:
            raise RuntimeError("capture_metadata_bound")
        amplitude = np.max(np.abs(window), axis=1)
        channel_rms = np.sqrt(np.mean(np.square(window, dtype=np.float64), axis=0))
        channel_frequency = [
            self._frequency(window[:, channel]) for channel in range(self.channels)
        ]
        channel_clipped = np.count_nonzero(np.abs(window) >= 0.99, axis=0)
        self.envelopes.append(
            {
                "start_sample": start_sample,
                "sample_count": len(window),
                "peak": float(np.max(amplitude)) if len(amplitude) else 0.0,
                "rms": float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
                if window.size
                else 0.0,
                "frequency_hz": channel_frequency[0],
                "clipped_samples": int(np.sum(channel_clipped)),
                "channel_rms": [round(float(value), 6) for value in channel_rms],
                "channel_frequency_hz": channel_frequency,
                "channel_clipped_samples": [int(value) for value in channel_clipped],
            }
        )

    def consume(self, samples: npt.NDArray[Any]) -> None:
        if self._finished:
            raise RuntimeError("audio_observer_closed")
        if (
            not isinstance(samples, np.ndarray)
            or samples.ndim != 2
            or samples.shape[1] != self.channels
            or not 1 <= len(samples) <= self.sample_rate
            or self.samples_analyzed + len(samples) > self.max_samples
            or not np.isfinite(samples).all()
        ):
            raise RuntimeError("capture_metadata_bound")
        block = np.asarray(samples, dtype=np.float32)
        amplitude = np.abs(block)
        base_sample = self.samples_analyzed
        self._reduce_runs(amplitude, base_sample)

        combined_start = self._window_start if len(self._window_remainder) else base_sample
        combined = (
            np.concatenate((self._window_remainder, block), axis=0)
            if len(self._window_remainder)
            else block
        )
        complete = len(combined) // self.window_samples
        for window_index in range(complete):
            offset = window_index * self.window_samples
            self._append_envelope(
                combined[offset : offset + self.window_samples], combined_start + offset
            )
        consumed = complete * self.window_samples
        self._window_remainder = combined[consumed:].copy()
        self._window_start = combined_start + consumed
        self.samples_analyzed += len(block)

    def consume_silence(self, sample_count: int) -> None:
        if (
            self._finished
            or type(sample_count) is not int
            or sample_count < 0
            or self.samples_analyzed + sample_count > self.max_samples
        ):
            raise RuntimeError("capture_metadata_bound")
        remaining = sample_count
        while remaining:
            count = min(remaining, 4_800)
            self.consume(np.zeros((count, self.channels), dtype=np.float32))
            remaining -= count

    def finish(self) -> MetricsResult:
        if not self._finished:
            for channel in range(self.channels):
                self._close_silence(channel, self.samples_analyzed)
                self._close_impulse(channel, self.samples_analyzed)
            if len(self._window_remainder):
                self._append_envelope(self._window_remainder, self._window_start)
            self._window_remainder = np.empty((0, self.channels), dtype=np.float32)
            self._finished = True
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "samples_analyzed": self.samples_analyzed,
            "window_samples": self.window_samples,
            "silence_floor": self.silence_floor,
            "silence_runs": list(self.silence_runs),
            "impulse_floor": self.impulse_floor,
            "impulse_events": list(self.impulse_events),
            "envelopes": list(self.envelopes),
            "raw_audio_retained": False,
        }


def call(
    pointer: PTR,
    slot: int,
    restype: Any,
    arguments: list[Any],
    *values: Any,
) -> Any:
    # CRITICAL: keep the Windows ABI lookup lazy; eager binding breaks pure POSIX reducer imports.
    win = C.WINFUNCTYPE
    table = C.cast(pointer, C.POINTER(C.POINTER(PTR))).contents
    return win(restype, PTR, *arguments)(table[slot])(pointer, *values)


def check(result: int, label: str) -> None:
    if result < 0:
        raise RuntimeError(f"{label}_0x{result & 0xFFFFFFFF:08x}")


def executable_identity(pid: int) -> str:
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [DWORD, C.c_int, DWORD]
    kernel.OpenProcess.restype = PTR
    kernel.QueryFullProcessImageNameW.argtypes = [PTR, DWORD, C.c_wchar_p, C.POINTER(DWORD)]
    kernel.CloseHandle.argtypes = [PTR]
    handle = kernel.OpenProcess(0x1000, 0, pid)
    if not handle:
        raise RuntimeError("test_browser_process_unavailable")
    try:
        name = C.create_unicode_buffer(32768)
        size = DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, C.byref(size)):
            raise RuntimeError("test_browser_identity_unavailable")
        path = Path(name.value)
        if path.name.lower() not in {"chrome.exe", "chrome-headless-shell.exe"}:
            raise RuntimeError("target_is_not_test_chromium")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        kernel.CloseHandle(handle)


def observe(pid: int, duration: float) -> dict[str, object]:
    win = C.WINFUNCTYPE
    browser_hash = executable_identity(pid)
    ole = C.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [PTR, DWORD]
    ole.CoInitializeEx.restype = HRESULT
    check(ole.CoInitializeEx(None, 0), "com_initialize")
    activated = threading.Event()
    client = PTR()
    activation_status = {"hr": -1}
    reference_count = 1
    supported = {
        uuid.UUID(value).bytes_le
        for value in (
            "00000000-0000-0000-c000-000000000046",
            "94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90",
            "41d949ab-9862-444a-80f6-c261334da5eb",
        )
    }

    @win(HRESULT, PTR, C.POINTER(GUID), C.POINTER(PTR))  # type: ignore[untyped-decorator]
    def query(this: Any, iid: Any, result: Any) -> int:
        nonlocal reference_count
        if bytes(iid.contents.data) in supported:
            result[0] = this
            reference_count += 1
            return 0
        result[0] = None
        return -2147467262

    @win(DWORD, PTR)  # type: ignore[untyped-decorator]
    def addref(_this: Any) -> int:
        nonlocal reference_count
        reference_count += 1
        return reference_count

    @win(DWORD, PTR)  # type: ignore[untyped-decorator]
    def release(_this: Any) -> int:
        nonlocal reference_count
        reference_count -= 1
        return reference_count

    @win(HRESULT, PTR, PTR)  # type: ignore[untyped-decorator]
    def completed(_this: Any, operation: Any) -> int:
        result = HRESULT()
        status = call(
            operation,
            3,
            HRESULT,
            [C.POINTER(HRESULT), C.POINTER(PTR)],
            C.byref(result),
            C.byref(client),
        )
        activation_status["hr"] = status if status < 0 else result.value
        activated.set()
        return 0

    table = (PTR * 4)(*(C.cast(fn, PTR).value for fn in (query, addref, release, completed)))
    handler = (C.POINTER(PTR) * 1)(C.cast(table, C.POINTER(PTR)))
    # INCLUDE only the provided browser and descendants. Never use an endpoint-loopback fallback.
    activation = Activation(1, pid, 0)
    variant = Variant(
        65, (C.c_ushort * 3)(0, 0, 0), Blob(C.sizeof(activation), C.addressof(activation))
    )
    iid = GUID("1cb9ad4c-dbfa-4c32-b178-c2f568a703b2")
    operation = PTR()
    mmdev = C.WinDLL("Mmdevapi")
    activate = mmdev.ActivateAudioInterfaceAsync
    activate.argtypes = [C.c_wchar_p, C.POINTER(GUID), C.POINTER(Variant), PTR, C.POINTER(PTR)]
    activate.restype = HRESULT
    check(
        activate(
            "VAD\\Process_Loopback",
            C.byref(iid),
            C.byref(variant),
            C.cast(handler, PTR),
            C.byref(operation),
        ),
        "process_loopback_activation",
    )
    if not activated.wait(5):
        raise RuntimeError("process_loopback_activation_timeout")
    check(activation_status["hr"], "process_loopback_result")
    capture = PTR()
    stopped = threading.Event()
    started = False
    rows: list[dict[str, int | float | None]] = []
    onsets: list[dict[str, float]] = []
    last_hot = -100.0
    timeline = PacketTimelineTracker()
    metrics = PacketMetricsReducer()
    try:
        wave = WaveFormat(3, 2, 48000, 384000, 8, 32, 0)
        flags = 0x00020000 | 0x80000000
        check(
            call(
                client,
                3,
                HRESULT,
                [C.c_int, DWORD, C.c_longlong, C.c_longlong, C.POINTER(WaveFormat), PTR],
                0,
                flags,
                200000,
                0,
                C.byref(wave),
                None,
            ),
            "initialize",
        )
        capture_iid = GUID("c8adbd64-e71e-48a0-a4de-185c395cd317")
        check(
            call(
                client,
                14,
                HRESULT,
                [C.POINTER(GUID), C.POINTER(PTR)],
                C.byref(capture_iid),
                C.byref(capture),
            ),
            "capture_service",
        )
        check(call(client, 10, HRESULT, []), "capture_start")
        started = True
        print(
            json.dumps(
                {
                    "ready": True,
                    "selector": "include_test_browser_process_tree",
                    "browserExecutableSha256": browser_hash,
                }
            ),
            flush=True,
        )

        def input_stop() -> None:
            sys.stdin.readline()
            stopped.set()

        threading.Thread(target=input_stop, daemon=True).start()
        deadline = time.monotonic() + duration
        while not stopped.is_set() and time.monotonic() < deadline:
            size = UINT32()
            check(call(capture, 5, HRESULT, [C.POINTER(UINT32)], C.byref(size)), "packet_size")
            if not size.value:
                time.sleep(0.002)
                continue
            data, frames, packet_flags, position, qpc = PTR(), UINT32(), DWORD(), UINT64(), UINT64()
            check(
                call(
                    capture,
                    3,
                    HRESULT,
                    [
                        C.POINTER(PTR),
                        C.POINTER(UINT32),
                        C.POINTER(DWORD),
                        C.POINTER(UINT64),
                        C.POINTER(UINT64),
                    ],
                    C.byref(data),
                    C.byref(frames),
                    C.byref(packet_flags),
                    C.byref(position),
                    C.byref(qpc),
                ),
                "capture_packet",
            )
            try:
                if frames.value > 48000 or len(rows) >= 5000:
                    raise RuntimeError("capture_metadata_bound")
                (
                    position_delta,
                    qpc_delta,
                    qpc_error,
                    timeline_gap_frames,
                ) = timeline.observe(
                    device_position=int(position.value),
                    qpc_position=int(qpc.value),
                    frames=int(frames.value),
                    flags=int(packet_flags.value),
                )
                before = time.perf_counter_ns()
                wall = time.time_ns()
                after = time.perf_counter_ns()
                epoch_offset = wall - (before + after) // 2
                packet_epoch_ms = (qpc.value * 100 + epoch_offset) / 1_000_000
                peak, onset_index = 0.0, None
                if frames.value and packet_flags.value & 2:
                    samples = np.zeros((frames.value, 2), dtype=np.float32)
                elif frames.value:
                    samples = np.ctypeslib.as_array(
                        C.cast(data, C.POINTER(C.c_float)), shape=(frames.value * 2,)
                    ).reshape(-1, 2)
                    amplitude = np.max(np.abs(samples), axis=1)
                    peak = float(np.max(amplitude))
                    hot = np.flatnonzero(amplitude > 0.1)
                    if hot.size:
                        onset_index = int(hot[0])
                    del amplitude
                metrics.consume_silence(timeline_gap_frames)
                if frames.value:
                    metrics.consume(samples)
                rows.append(
                    {
                        "time": packet_epoch_ms,
                        "sampleStart": metrics.samples_analyzed - frames.value,
                        "devicePosition": int(position.value),
                        "positionDeltaFrames": position_delta,
                        "qpcPosition": int(qpc.value),
                        "qpcDelta100ns": qpc_delta,
                        "qpcError100ns": qpc_error,
                        "timelineGapFrames": timeline_gap_frames,
                        "frames": frames.value,
                        "flags": packet_flags.value,
                        "peak": peak,
                        "pcm16Peak": round(peak * 32768),
                        "clockBracketNs": after - before,
                    }
                )
                if onset_index is not None:
                    onset = packet_epoch_ms + onset_index / 48
                    if onset - last_hot > 100:
                        onsets.append({"time": onset})
                    last_hot = packet_epoch_ms + frames.value / 48
            finally:
                check(call(capture, 4, HRESULT, [UINT32], frames.value), "release_packet")
        result: dict[str, object] = {
            "schema": "h3.context.process_audio_observer_capture.v3",
            "selector": "include_test_browser_process_tree",
            "sampleRate": 48000,
            "observationFormat": "pcm_float32_stereo",
            "browserExecutableSha256": browser_hash,
            "packets": rows,
            "onsets": onsets,
            "metrics": metrics.finish(),
            "discontinuityPackets": sum(bool(int(row["flags"] or 0) & 1) for row in rows),
            "timestampErrorPackets": sum(bool(int(row["flags"] or 0) & 4) for row in rows),
            "silentPackets": sum(bool(int(row["flags"] or 0) & 2) for row in rows),
            "packetTimeline": timeline.finish(),
            "rawAudioRetained": False,
            "microphoneOpened": False,
        }
        if (
            len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
            > MAX_OBSERVER_OUTPUT_BYTES
        ):
            raise RuntimeError("capture_metadata_bound")
        return result
    finally:
        if started:
            call(client, 11, HRESULT, [])
        if capture:
            call(capture, 2, DWORD, [])
        if client:
            call(client, 2, DWORD, [])
        if operation:
            call(operation, 2, DWORD, [])
        ole.CoUninitialize()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--duration", type=float, default=30)
    arguments = parser.parse_args()
    if arguments.pid <= 0 or not 1 <= arguments.duration <= 45:
        raise SystemExit("invalid_capture_bound")
    try:
        print(json.dumps({"result": observe(arguments.pid, arguments.duration)}), flush=True)
    except Exception as error:
        print(
            json.dumps(
                {"failure": str(error) if isinstance(error, RuntimeError) else type(error).__name__}
            ),
            flush=True,
        )
        raise SystemExit(1) from error
