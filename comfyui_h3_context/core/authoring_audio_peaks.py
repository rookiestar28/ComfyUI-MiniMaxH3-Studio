"""Pure bounded envelope for read-only embedded-audio waveform decoration."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

AUDIO_PEAKS_MAGIC = b"H3AP"
AUDIO_PEAKS_VERSION = 1
AUDIO_PEAKS_PAIR_RATE = 100
AUDIO_PEAKS_SAMPLE_RATE = 8_000
AUDIO_PEAKS_SAMPLES_PER_PAIR = AUDIO_PEAKS_SAMPLE_RATE // AUDIO_PEAKS_PAIR_RATE
AUDIO_PEAKS_MAX_PAIRS = 16_384
AUDIO_PEAKS_MAX_SAMPLE_COUNT = AUDIO_PEAKS_MAX_PAIRS * AUDIO_PEAKS_SAMPLES_PER_PAIR
AUDIO_PEAKS_MAX_PCM_BYTES = AUDIO_PEAKS_MAX_SAMPLE_COUNT * 2
AUDIO_PEAKS_HEADER_BYTES = 20
AUDIO_PEAKS_MAX_ENVELOPE_BYTES = AUDIO_PEAKS_HEADER_BYTES + AUDIO_PEAKS_MAX_PAIRS * 2
_HEADER = struct.Struct("<4sHHIII")


class AudioPeaksError(ValueError):
    """Closed audio-peaks failure with no media or amplitude projection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AudioPeaksEnvelope:
    version: int
    pair_rate: int
    sample_rate: int
    sample_count: int
    pairs: tuple[tuple[int, int], ...]


def encode_audio_peaks_envelope(
    pcm: bytes | bytearray,
    *,
    guard: Callable[[], None] | None = None,
) -> bytearray:
    """Fold mono signed-16 LE PCM into signed-8 min/max buckets."""

    if type(pcm) not in (bytes, bytearray):
        raise AudioPeaksError("invalid_request")
    if len(pcm) > AUDIO_PEAKS_MAX_PCM_BYTES:
        raise AudioPeaksError("resource_limit")
    if not pcm or len(pcm) % 2:
        raise AudioPeaksError("invalid_request")
    sample_count = len(pcm) // 2
    pair_count = (sample_count + AUDIO_PEAKS_SAMPLES_PER_PAIR - 1) // (AUDIO_PEAKS_SAMPLES_PER_PAIR)
    if not 1 <= pair_count <= AUDIO_PEAKS_MAX_PAIRS:
        raise AudioPeaksError("resource_limit")
    body = bytearray(AUDIO_PEAKS_HEADER_BYTES + pair_count * 2)
    _HEADER.pack_into(
        body,
        0,
        AUDIO_PEAKS_MAGIC,
        AUDIO_PEAKS_VERSION,
        AUDIO_PEAKS_PAIR_RATE,
        AUDIO_PEAKS_SAMPLE_RATE,
        sample_count,
        pair_count,
    )
    view = memoryview(pcm)
    try:
        for pair_index in range(pair_count):
            if guard is not None and pair_index % 256 == 0:
                guard()
            start = pair_index * AUDIO_PEAKS_SAMPLES_PER_PAIR
            end = min(sample_count, start + AUDIO_PEAKS_SAMPLES_PER_PAIR)
            low = 32_767
            high = -32_768
            for sample_index in range(start, end):
                sample = struct.unpack_from("<h", view, sample_index * 2)[0]
                low = min(low, sample)
                high = max(high, sample)
            # Signed floor quantization preserves both extrema and is deterministic at zero.
            struct.pack_into(
                "bb", body, AUDIO_PEAKS_HEADER_BYTES + pair_index * 2, low // 256, high // 256
            )
        if guard is not None:
            guard()
        return body
    except AudioPeaksError:
        body.clear()
        raise
    except Exception as exc:
        body.clear()
        raise AudioPeaksError("internal_failure") from exc
    finally:
        view.release()


def decode_audio_peaks_envelope(body: bytes | bytearray) -> AudioPeaksEnvelope:
    """Independently validate the closed envelope produced by the PCM fold."""

    if type(body) not in (bytes, bytearray) or not (
        AUDIO_PEAKS_HEADER_BYTES + 2 <= len(body) <= AUDIO_PEAKS_MAX_ENVELOPE_BYTES
    ):
        raise AudioPeaksError("invalid_request")
    try:
        magic, version, pair_rate, sample_rate, sample_count, pair_count = _HEADER.unpack_from(body)
    except struct.error:
        raise AudioPeaksError("invalid_request") from None
    expected_pairs = (sample_count + AUDIO_PEAKS_SAMPLES_PER_PAIR - 1) // (
        AUDIO_PEAKS_SAMPLES_PER_PAIR
    )
    if (
        magic != AUDIO_PEAKS_MAGIC
        or version != AUDIO_PEAKS_VERSION
        or pair_rate != AUDIO_PEAKS_PAIR_RATE
        or sample_rate != AUDIO_PEAKS_SAMPLE_RATE
        or not 1 <= sample_count <= AUDIO_PEAKS_MAX_SAMPLE_COUNT
        or pair_count != expected_pairs
        or not 1 <= pair_count <= AUDIO_PEAKS_MAX_PAIRS
        or len(body) != AUDIO_PEAKS_HEADER_BYTES + pair_count * 2
    ):
        raise AudioPeaksError("invalid_request")
    pairs: list[tuple[int, int]] = []
    try:
        for pair_index in range(pair_count):
            low, high = struct.unpack_from("bb", body, AUDIO_PEAKS_HEADER_BYTES + pair_index * 2)
            if low > high:
                raise AudioPeaksError("invalid_request")
            pairs.append((low, high))
    except struct.error:
        raise AudioPeaksError("invalid_request") from None
    return AudioPeaksEnvelope(version, pair_rate, sample_rate, sample_count, tuple(pairs))


__all__ = [
    "AUDIO_PEAKS_HEADER_BYTES",
    "AUDIO_PEAKS_MAX_ENVELOPE_BYTES",
    "AUDIO_PEAKS_MAX_PAIRS",
    "AUDIO_PEAKS_MAX_PCM_BYTES",
    "AudioPeaksEnvelope",
    "AudioPeaksError",
    "decode_audio_peaks_envelope",
    "encode_audio_peaks_envelope",
]
