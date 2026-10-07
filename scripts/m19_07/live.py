"""Structural observations taken from the supplied host's own outputs.

The problem with observing a joint audiovisual latent from outside the host is that the HTTP surface
does not hand back tensors. What it does hand back is decoded media, and that turns out to be enough
for the questions this item asks, because the decoded streams carry the temporal contract directly:
the video stream's frame count and the audio stream's sample count are exactly what the latent's two
extents produce.

So the observation is deliberately content-free. Nothing here looks at a pixel or a sample value. It
reads the frame count from how many files the host wrote, and the sample count and rate from the
FLAC stream header — 34 bytes of metadata that state the sample rate and total sample count without
decoding a single audio frame, and without a codec dependency in the harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

#: FLAC layout: a four byte magic, a four byte metadata block header, then STREAMINFO. The rate and
#: total sample count sit in one 64 bit field 10 bytes into STREAMINFO.
_FLAC_MAGIC = b"fLaC"
_STREAMINFO_OFFSET = 8
_RATE_AND_TOTAL_OFFSET = _STREAMINFO_OFFSET + 10
_RATE_AND_TOTAL_BYTES = 8


class MediaHeaderError(ValueError):
    """A decoded stream did not carry the header the observation needs."""


@dataclass(frozen=True)
class AudioStreamFacts:
    sample_rate: int
    channels: int
    bits_per_sample: int
    total_samples: int

    @property
    def seconds(self) -> Fraction:
        return Fraction(self.total_samples, self.sample_rate)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bits_per_sample": self.bits_per_sample,
            "total_samples": self.total_samples,
        }


def flac_streaminfo(data: bytes) -> AudioStreamFacts:
    """Read STREAMINFO without decoding audio.

    Deliberately header-only: the qualification records how long the stream is, never what is in it.
    """
    if data[:4] != _FLAC_MAGIC:
        raise MediaHeaderError("not a FLAC stream")
    field = data[_RATE_AND_TOTAL_OFFSET : _RATE_AND_TOTAL_OFFSET + _RATE_AND_TOTAL_BYTES]
    if len(field) != _RATE_AND_TOTAL_BYTES:
        raise MediaHeaderError("FLAC stream is shorter than its own STREAMINFO block")
    bits = int.from_bytes(field, "big")
    facts = AudioStreamFacts(
        sample_rate=(bits >> 44) & 0xFFFFF,
        channels=((bits >> 41) & 0x7) + 1,
        bits_per_sample=((bits >> 36) & 0x1F) + 1,
        total_samples=bits & 0xFFFFFFFFF,
    )
    if facts.sample_rate == 0:
        raise MediaHeaderError("FLAC stream declares a zero sample rate")
    return facts


@dataclass(frozen=True)
class TemporalCanary:
    """One requested length, and what the host actually produced for it."""

    requested_length: int
    aligned_frame_count: int
    predicted_video_latent_t: int
    predicted_audio_latent_t: int
    decoded_frames: int
    audio: AudioStreamFacts
    video_fps: int
    audio_latent_fps: int

    @property
    def video_seconds(self) -> Fraction:
        return Fraction(self.decoded_frames, self.video_fps)

    @property
    def audio_seconds(self) -> Fraction:
        return self.audio.seconds

    @property
    def stream_delta_seconds(self) -> Fraction:
        """How far the audio stream over- or under-runs the video stream.

        This is the rounded join made physical. It is zero exactly when the frame count puts the
        audio extent on a whole latent step, and otherwise it is the rounding residue divided by the
        audio latent rate — which is why a successor that assumes the two streams are the same
        length is wrong for two frame counts out of every three.
        """
        return self.audio_seconds - self.video_seconds

    @property
    def samples_per_audio_latent(self) -> Fraction:
        return Fraction(self.audio.total_samples, self.predicted_audio_latent_t)

    def frames_match_prediction(self) -> bool:
        return self.decoded_frames == self.aligned_frame_count

    def audio_matches_prediction(self) -> bool:
        expected = Fraction(self.predicted_audio_latent_t, self.audio_latent_fps)
        return self.audio_seconds == expected

    def as_evidence(self) -> dict[str, Any]:
        delta = self.stream_delta_seconds
        return {
            "requested_length": self.requested_length,
            "aligned_frame_count": self.aligned_frame_count,
            "predicted_video_latent_t": self.predicted_video_latent_t,
            "predicted_audio_latent_t": self.predicted_audio_latent_t,
            "decoded_frames": self.decoded_frames,
            "audio": self.audio.as_evidence(),
            "video_seconds": f"{self.video_seconds.numerator}/{self.video_seconds.denominator}",
            "audio_seconds": f"{self.audio_seconds.numerator}/{self.audio_seconds.denominator}",
            "stream_delta_milliseconds": float(delta * 1000),
            "samples_per_audio_latent": (
                f"{self.samples_per_audio_latent.numerator}/"
                f"{self.samples_per_audio_latent.denominator}"
            ),
            "frames_match_prediction": self.frames_match_prediction(),
            "audio_matches_prediction": self.audio_matches_prediction(),
        }


@dataclass(frozen=True)
class MechanismProbe:
    """One attempt to drive a candidate mechanism on the exact subject.

    A probe that fails is evidence, not an accident to be retried away, so the failure is recorded
    with the node and exception that produced it. The message is truncated and never carries a path.
    """

    mechanism: str
    node_type: str
    outcome: str
    exception_type: str | None = None
    detail: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "mechanism": self.mechanism,
            "node_type": self.node_type,
            "outcome": self.outcome,
        }
        if self.exception_type:
            record["exception_type"] = self.exception_type
        if self.detail:
            record["detail"] = self.detail
        return record


def temporal_canaries_agree(canaries: tuple[TemporalCanary, ...]) -> tuple[bool, tuple[str, ...]]:
    """Every canary must reproduce the statically derived extents exactly."""
    problems: list[str] = []
    for canary in canaries:
        if not canary.frames_match_prediction():
            problems.append(
                f"length {canary.requested_length}: host produced {canary.decoded_frames} frames, "
                f"source predicted {canary.aligned_frame_count}"
            )
        if not canary.audio_matches_prediction():
            problems.append(
                f"length {canary.requested_length}: audio ran {canary.audio_seconds} s, source "
                f"predicted {canary.predicted_audio_latent_t}/{canary.audio_latent_fps} s"
            )
    return not problems, tuple(problems)


def exact_boundary_is_separable(canaries: tuple[TemporalCanary, ...]) -> tuple[bool, str]:
    """The live set must contain both an aligned case and a residual one.

    Without both, the observation cannot distinguish "the streams always line up" from "these
    particular lengths happened to line up", and the exact-boundary decision would be invisible
    rather than measured.
    """
    zero = [c for c in canaries if c.stream_delta_seconds == 0]
    nonzero = [c for c in canaries if c.stream_delta_seconds != 0]
    if not zero:
        return False, "no live canary landed on an exact audiovisual boundary"
    if not nonzero:
        return False, "no live canary exercised the rounded join"
    return True, (
        f"{len(zero)} exact and {len(nonzero)} residual canaries; largest residue "
        f"{max(abs(float(c.stream_delta_seconds)) for c in nonzero) * 1000:.4f} ms"
    )
