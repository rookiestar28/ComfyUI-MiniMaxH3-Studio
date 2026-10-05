"""The one amplitude definition of a clip's audio adjustments, and how the final render applies it.

A clip of D output frames covers L = 2000 * D output samples (48 kHz at 24 fps). At the
clip-relative output sample k in [0, L) its embedded audio is multiplied by

    f_in(k)   = min(1, k / N_in)        (1 when N_in = 0),   N_in  = 2000 * fade_in_frames
    f_out(k)  = min(1, (L - k) / N_out) (1 when N_out = 0),  N_out = 2000 * fade_out_frames
    factor(k) = 0 when muted, else 10 ** (gain_mb / 2000) * f_in(k) * f_out(k)

These are the per-sample gains of the pinned FFmpeg's `volume` and triangular `afade`, measured on
that build: a fade-in counts (i - S) / N from its start sample S, a fade-out (S + N - i) / N,
`silence` and `unity` scale those ramps, and `volume=0` writes exact zeros. The final render
applies the factor per audio run of a clip (`clip_audio_run`), and the preview schedules the same
ramps on a Web Audio gain (`frontend/src/runtime/clipAudioEnvelope.ts`); both statements are
tested against one table generated from `clip_audio_factor`.

Which clip is heard at an output frame is decided elsewhere and is unchanged by any of this: a
hand-off to another clip cuts the outgoing clip's audio wherever its envelope stands, and a clip
that is heard again later resumes its own envelope at its own clip-relative position.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from .composition_contract import ClipAudio

SAMPLES_PER_FRAME: Final = 2_000


def clip_audio_gain(audio: ClipAudio) -> float:
    """The gain as an amplitude ratio: the constant part of the factor of a clip not muted."""

    return math.pow(10.0, audio.gain_mb / 2_000)


def clip_audio_factor(audio: ClipAudio, duration_frames: int, k: int) -> float:
    """The factor at clip-relative output sample `k` of a clip `duration_frames` long."""

    length = duration_frames * SAMPLES_PER_FRAME
    if not 0 <= k < length:
        raise ValueError("the sample is outside the clip")
    if audio.muted:
        return 0.0
    fade_in = audio.fade_in_frames * SAMPLES_PER_FRAME
    fade_out = audio.fade_out_frames * SAMPLES_PER_FRAME
    rising = 1.0 if fade_in == 0 else min(1.0, k / fade_in)
    falling = 1.0 if fade_out == 0 else min(1.0, (length - k) / fade_out)
    return clip_audio_gain(audio) * rising * falling


@dataclass(frozen=True, slots=True)
class ClipAudioRun:
    """What one audio run [k0, k1) of a clip receives, in the terms of the FFmpeg filters.

    `volume` is None when the gain is unity and the clip is not muted. A fade-in starts at the
    run's first sample: `fade_in` is (samples, silence). A fade-out is (start, samples, unity), its
    start counted from the run's first sample. Each fade is None where the run does not reach it,
    and both are None for a muted clip, whose `volume` of 0 already silences it.
    """

    volume: float | None
    fade_in: tuple[int, float] | None
    fade_out: tuple[int, int, float] | None


def clip_audio_run(audio: ClipAudio, duration_frames: int, k0: int, k1: int) -> ClipAudioRun:
    """The filters that give `clip_audio_factor` over the run [k0, k1) of the clip."""

    length = duration_frames * SAMPLES_PER_FRAME
    if not 0 <= k0 < k1 <= length:
        raise ValueError("the run is outside the clip")
    if audio.muted:
        return ClipAudioRun(0.0, None, None)
    volume = None if audio.gain_mb == 0 else clip_audio_gain(audio)
    fade_in_samples = audio.fade_in_frames * SAMPLES_PER_FRAME
    fade_out_samples = audio.fade_out_frames * SAMPLES_PER_FRAME
    fade_in: tuple[int, float] | None = None
    if k0 < fade_in_samples:
        # Measured: `afade=t=in:ss=0:ns=N:silence=s` gives s + (1 - s) * i / N, so a run that
        # starts k0 samples into the ramp continues it with N = N_in - k0 and s = k0 / N_in.
        fade_in = (fade_in_samples - k0, k0 / fade_in_samples)
    fade_out: tuple[int, int, float] | None = None
    fade_out_start = length - fade_out_samples
    if k1 > fade_out_start:
        if k0 <= fade_out_start:
            fade_out = (fade_out_start - k0, fade_out_samples, 1.0)
        else:
            # Measured: `afade=t=out:ss=0:ns=N:unity=u` gives u * (N - i) / N, so a run that
            # starts inside the ramp continues it with N = L - k0 and u = (L - k0) / N_out.
            remaining = length - k0
            fade_out = (0, remaining, remaining / fade_out_samples)
    return ClipAudioRun(volume, fade_in, fade_out)
