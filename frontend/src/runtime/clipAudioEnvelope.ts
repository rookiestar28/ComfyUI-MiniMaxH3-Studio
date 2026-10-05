import { type ClipAudio } from "../contracts/compositionCodec";

/**
 * The preview's statement of a clip's audio envelope. It is the one amplitude definition of
 * `comfyui_h3_context/core/clip_audio.py`, and both are tested against one table generated from
 * the Python statement:
 *
 *   f_in(k)   = min(1, k / N_in)        (1 when N_in = 0)
 *   f_out(k)  = min(1, (L - k) / N_out) (1 when N_out = 0)
 *   factor(k) = 0 when muted, else 10 ** (gain_mb / 2000) * f_in(k) * f_out(k)
 *
 * for a clip-relative output sample k in [0, L), L = 2000 * duration frames.
 */
export const CLIP_AUDIO_SAMPLE_RATE = 48_000;
export const CLIP_AUDIO_SAMPLES_PER_FRAME = 2_000;

/** The gain as an amplitude ratio: the constant part of the factor of a clip not muted. */
export function clipAudioGain(audio: ClipAudio): number {
  return Math.pow(10, audio.gainMb / 2_000);
}

export function clipAudioFactor(
  audio: ClipAudio,
  durationFrames: number,
  k: number,
): number {
  const length = durationFrames * CLIP_AUDIO_SAMPLES_PER_FRAME;
  if (!Number.isSafeInteger(k) || k < 0 || k >= length)
    throw new RangeError("the sample is outside the clip");
  if (audio.muted) return 0;
  const fadeIn = audio.fadeInFrames * CLIP_AUDIO_SAMPLES_PER_FRAME;
  const fadeOut = audio.fadeOutFrames * CLIP_AUDIO_SAMPLES_PER_FRAME;
  const rising = fadeIn === 0 ? 1 : Math.min(1, k / fadeIn);
  const falling = fadeOut === 0 ? 1 : Math.min(1, (length - k) / fadeOut);
  return clipAudioGain(audio) * rising * falling;
}

/** The part of an `AudioParam` the schedule writes; a test records the calls. */
export type ClipAudioGainParam = Pick<
  AudioParam,
  "setValueAtTime" | "linearRampToValueAtTime"
>;

/**
 * Schedule the envelope of a clip on a gain whose source starts at context time `startTime` with
 * the clip-relative output sample `k0`. Web Audio's linear ramp from the value of the previous
 * event gives, per sample frame, the same value as the factor: the ramp from factor(k0) to g
 * over N_in - k0 samples is g * (k0 + i) / N_in, and the ramp to 0 at the clip's end leaves
 * 1 / N_out at its last sample, as FFmpeg's fade-out does.
 */
export function scheduleClipAudioGain(
  param: ClipAudioGainParam,
  audio: ClipAudio,
  durationFrames: number,
  k0: number,
  startTime: number,
): void {
  const length = durationFrames * CLIP_AUDIO_SAMPLES_PER_FRAME;
  const at = (k: number) => startTime + (k - k0) / CLIP_AUDIO_SAMPLE_RATE;
  param.setValueAtTime(clipAudioFactor(audio, durationFrames, k0), startTime);
  if (audio.muted) return;
  const gain = clipAudioGain(audio);
  const fadeIn = audio.fadeInFrames * CLIP_AUDIO_SAMPLES_PER_FRAME;
  const fadeOut = audio.fadeOutFrames * CLIP_AUDIO_SAMPLES_PER_FRAME;
  if (k0 < fadeIn) param.linearRampToValueAtTime(gain, at(fadeIn));
  if (fadeOut !== 0) {
    if (k0 < length - fadeOut) param.setValueAtTime(gain, at(length - fadeOut));
    param.linearRampToValueAtTime(0, at(length));
  }
}
