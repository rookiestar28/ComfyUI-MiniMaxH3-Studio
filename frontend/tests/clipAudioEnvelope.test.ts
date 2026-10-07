import { describe, expect, it } from "vitest";

import table from "../../tests/fixtures/m25_77_clip_audio_envelope_v1.json";
import type { ClipAudio } from "../src/contracts/compositionCodec";
import {
  CLIP_AUDIO_SAMPLES_PER_FRAME,
  clipAudioFactor,
  scheduleClipAudioGain,
} from "../src/runtime/clipAudioEnvelope";

// The preview's statement of the clip audio envelope, held to the table generated from the
// Python statement (`scripts/m25_77_clip_audio_envelope_fixture.py`), and its schedule held to
// that statement at every sample frame through the Web Audio automation it writes.

type Wire = Readonly<{
  gain_mb: number;
  muted: boolean;
  fade_in_frames: number;
  fade_out_frames: number;
}>;

function audio(wire: Wire): ClipAudio {
  return Object.freeze({
    gainMb: wire.gain_mb,
    muted: wire.muted,
    fadeInFrames: wire.fade_in_frames,
    fadeOutFrames: wire.fade_out_frames,
  });
}

// `10 ** x` may differ by one unit in the last place between the two languages' `pow`.
function close(actual: number, expected: number) {
  expect(Math.abs(actual - expected)).toBeLessThanOrEqual(
    Math.abs(expected) * 1e-12,
  );
}

type Event =
  | Readonly<{ kind: "set"; value: number; time: number }>
  | Readonly<{ kind: "ramp"; value: number; time: number }>;

function recorder() {
  const events: Event[] = [];
  return {
    events,
    param: {
      setValueAtTime(value: number, time: number) {
        events.push({ kind: "set", value, time });
        return this as unknown as AudioParam;
      },
      linearRampToValueAtTime(value: number, time: number) {
        events.push({ kind: "ramp", value, time });
        return this as unknown as AudioParam;
      },
    },
  };
}

/**
 * The value of an `AudioParam` at time `t` for a list of `setValueAtTime` and
 * `linearRampToValueAtTime` events, as the Web Audio specification computes it: a set value holds
 * until the next event, and a linear ramp runs from the previous event's time and value to its
 * own. Events are in time order and none is before the first set.
 */
function valueAt(events: readonly Event[], t: number): number {
  let previous = events[0]!;
  expect(previous.kind).toBe("set");
  for (const event of events.slice(1)) {
    if (event.time > t) {
      if (event.kind === "set") return previous.value;
      return (
        previous.value +
        ((event.value - previous.value) * (t - previous.time)) /
          (event.time - previous.time)
      );
    }
    previous = event;
  }
  return previous.value;
}

const RATE = 48_000;
const START = 0.5;

describe("clip audio envelope", () => {
  const clips = table.clips.map((row) => ({
    ...row,
    audio: audio(row.audio as Wire),
  }));

  it("reads the table its generator wrote for both statements", () => {
    expect(table.schema).toBe("h3.test.clip_audio_envelope.v1");
    expect(table.samples_per_frame).toBe(CLIP_AUDIO_SAMPLES_PER_FRAME);
    expect(clips.map((clip) => clip.name)).toContain("upper_bound_fades");
    expect(clips.reduce((sum, clip) => sum + clip.factors.length, 0)).toBe(
      table.clips.reduce((sum, clip) => sum + clip.factors.length, 0),
    );
  });

  it.each(clips.map((clip) => [clip.name, clip] as const))(
    "gives the table's factor at every row of %s",
    (_name, clip) => {
      for (const [k, factor] of clip.factors as [number, number][])
        close(clipAudioFactor(clip.audio, clip.duration_frames, k), factor);
    },
  );

  it.each([-1, 2 * 2_000, 0.5, Number.NaN])(
    "has no factor for a sample outside the clip (%s)",
    (k) => {
      const value = audio({
        gain_mb: -600,
        muted: false,
        fade_in_frames: 1,
        fade_out_frames: 0,
      });
      expect(() => clipAudioFactor(value, 2, k)).toThrow(RangeError);
    },
  );

  // Every clip of the table, started at every sample its rows name: the run from there to the
  // clip's end is compared at every sample frame for the short clips and at every row's sample
  // for the long one.
  it.each(clips.map((clip) => [clip.name, clip] as const))(
    "schedules %s so that every sample frame gets the factor",
    (_name, clip) => {
      const length = clip.duration_frames * CLIP_AUDIO_SAMPLES_PER_FRAME;
      const marks = (clip.factors as [number, number][]).map(([k]) => k);
      const dense = length <= 96_000;
      for (const k0 of marks) {
        const { events, param } = recorder();
        scheduleClipAudioGain(
          param,
          clip.audio,
          clip.duration_frames,
          k0,
          START,
        );
        expect(events[0]).toEqual({
          kind: "set",
          value: clipAudioFactor(clip.audio, clip.duration_frames, k0),
          time: START,
        });
        for (let index = 1; index < events.length; index += 1)
          expect(events[index]!.time).toBeGreaterThanOrEqual(
            events[index - 1]!.time,
          );
        const samples = dense
          ? Array.from({ length: length - k0 }, (_, i) => k0 + i)
          : marks.filter((k) => k >= k0);
        // One assertion per start: the largest difference over the run.
        let worst = 0;
        for (const k of samples) {
          const expected = clipAudioFactor(clip.audio, clip.duration_frames, k);
          const actual = valueAt(events, START + (k - k0) / RATE);
          worst = Math.max(worst, Math.abs(actual - expected));
        }
        expect(worst, `${clip.name} from ${k0}`).toBeLessThanOrEqual(1e-9);
      }
    },
  );

  it("writes one constant for a clip whose envelope is constant", () => {
    for (const [wire, value] of [
      [{ gain_mb: 0, muted: false, fade_in_frames: 0, fade_out_frames: 0 }, 1],
      [
        { gain_mb: -600, muted: true, fade_in_frames: 12, fade_out_frames: 12 },
        0,
      ],
    ] as const) {
      const { events, param } = recorder();
      scheduleClipAudioGain(param, audio(wire), 48, 0, START);
      expect(events).toEqual([{ kind: "set", value, time: START }]);
    }
  });

  it("continues a fade-in from where a later start finds it", () => {
    const { events, param } = recorder();
    scheduleClipAudioGain(
      param,
      audio({
        gain_mb: 0,
        muted: false,
        fade_in_frames: 12,
        fade_out_frames: 0,
      }),
      48,
      6_000,
      START,
    );
    expect(events).toEqual([
      { kind: "set", value: 0.25, time: START },
      { kind: "ramp", value: 1, time: START + 18_000 / RATE },
    ]);
  });

  it("resumes a fade-out from inside it without a new hold", () => {
    const { events, param } = recorder();
    scheduleClipAudioGain(
      param,
      audio({
        gain_mb: 0,
        muted: false,
        fade_in_frames: 0,
        fade_out_frames: 12,
      }),
      48,
      80_000,
      START,
    );
    expect(events).toEqual([
      { kind: "set", value: 16_000 / 24_000, time: START },
      { kind: "ramp", value: 0, time: START + 16_000 / RATE },
    ]);
  });
});
