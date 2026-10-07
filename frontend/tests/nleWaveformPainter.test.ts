import { describe, expect, it, vi } from "vitest";

import {
  paintNleWaveforms,
  paintWaveform,
  planWaveformColumns,
  type WaveformPaintInput,
} from "../src/runtime/nleWaveformPainter";

const input = (embeddedAudio: "present_bound" | "absent") =>
  ({
    asset: {
      assetId: "asset-video",
      kind: "video",
      embeddedAudio,
      sourceSampleCount: embeddedAudio === "present_bound" ? 48_000 : null,
      sourceTimeBase: { num: 1, den: 1_000 },
      sourceFrameCount: 3,
      landmarks: [
        { frameIndex: 0, pts: 0, dts: 0, durationTicks: 250 },
        { frameIndex: 1, pts: 250, dts: 250, durationTicks: 500 },
        { frameIndex: 2, pts: 750, dts: 750, durationTicks: 250 },
      ],
    },
    // 2 fps: one output frame is 500 ticks of the 1/1000 source time base.
    outputFrameRate: { num: 2, den: 1 },
    clip: { sourceStartFrame: 1, durationFrames: 2 },
    bounds: { x: 10, y: 20, width: 4, height: 12 },
    visible: { x: 11, width: 2 },
    envelope: {
      version: 1,
      pairRate: 100,
      sampleRate: 8_000,
      sampleCount: 8_000,
      pairCount: 100,
      pairs: Int8Array.from(
        Array.from({ length: 100 }, (_, index) => [-index, index]).flat(),
      ),
      byteLength: 220,
    },
  }) satisfies WaveformPaintInput;

describe("NLE waveform painter", () => {
  it("re-buckets the visible clip window through source landmarks", () => {
    const columns = planWaveformColumns(input("present_bound"));

    expect(columns).toHaveLength(2);
    expect(columns.map(({ x }) => x)).toEqual([11.5, 12.5]);
    expect(columns[0]!.min).toBeGreaterThan(columns[1]!.min);
    expect(columns[0]!.max).toBeGreaterThanOrEqual(0);
    expect(columns.every(({ top, bottom }) => top <= bottom)).toBe(true);
  });

  it("keeps quiet nonzero audio visible while preserving silence and slot bounds", () => {
    const base = input("present_bound");
    const pairs = (low: number, high: number) =>
      Int8Array.from(Array.from({ length: 100 }, () => [low, high]).flat());
    const quietColumns = planWaveformColumns({
      ...base,
      envelope: { ...base.envelope, pairs: pairs(-17, 17) },
    });
    const silentColumns = planWaveformColumns({
      ...base,
      envelope: { ...base.envelope, pairs: pairs(0, 0) },
    });
    const fullScaleColumns = planWaveformColumns({
      ...base,
      envelope: { ...base.envelope, pairs: pairs(-128, 127) },
    });

    expect(
      Math.max(...quietColumns.map(({ top, bottom }) => bottom - top)),
    ).toBeGreaterThanOrEqual(4);
    expect(silentColumns.every(({ top, bottom }) => top === bottom)).toBe(true);
    expect(
      fullScaleColumns.every(({ top, bottom }) => top >= 20 && bottom <= 32),
    ).toBe(true);
  });

  it("draws only a pointer-transparent raster band and refuses absent audio", () => {
    const context = {
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      strokeStyle: "",
      lineWidth: 0,
    } as unknown as CanvasRenderingContext2D;

    paintWaveform(context, input("present_bound"));
    expect(context.lineTo).toHaveBeenCalledTimes(2);
    expect(planWaveformColumns(input("absent"))).toEqual([]);
  });

  it("traces actual stroke pixels when the host injects the diagnostic gate", () => {
    const target: Window & {
      __h3NleSchedulerTraceEnabled?: boolean;
      __h3NleWaveformCallTrace?: Array<Readonly<Record<string, unknown>>>;
      __h3NleWaveformStrokeTrace?: Array<Readonly<Record<string, unknown>>>;
    } = {
      location: { search: "" },
      __h3NleSchedulerTraceEnabled: true,
    } as unknown as Window & {
      __h3NleSchedulerTraceEnabled?: boolean;
      __h3NleWaveformCallTrace?: Array<Readonly<Record<string, unknown>>>;
      __h3NleWaveformStrokeTrace?: Array<Readonly<Record<string, unknown>>>;
    };
    vi.stubGlobal("window", target);
    let strokeCount = 0;
    const canvas = {
      width: 500,
      height: 100,
      getBoundingClientRect: () => ({
        left: 0,
        top: 0,
        width: 500,
        height: 100,
      }),
      getAttribute: () => "timeline_decoration",
    } as unknown as HTMLCanvasElement;
    const context = {
      canvas,
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(() => {
        strokeCount += 1;
      }),
      strokeStyle: "",
      lineWidth: 0,
      globalAlpha: 1,
      globalCompositeOperation: "source-over",
      filter: "none",
      getTransform: () => ({ a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 }),
      getLineDash: () => [],
      getImageData: () => ({
        data: Uint8ClampedArray.from(
          strokeCount === 0 ? [0, 0, 0, 0] : [0, 200, 220, 255],
        ),
      }),
    } as unknown as CanvasRenderingContext2D;

    try {
      paintWaveform(context, input("present_bound"));

      expect(
        target.__h3NleWaveformCallTrace?.map(({ outcome }) => outcome),
      ).toEqual(
        expect.arrayContaining([
          "enter",
          "planned",
          "path_built",
          "stroke_complete",
        ]),
      );
      const stroke = target.__h3NleWaveformStrokeTrace?.at(-1);
      expect(stroke).toMatchObject({
        event: "waveform.stroke.result",
        canvasMarker: "timeline_decoration",
        before: { status: "available", nonTransparent: 0, cyan: 0 },
        after: { status: "available", nonTransparent: 1, cyan: 1 },
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("leaves the region after short audio empty instead of stretching the last pair", () => {
    const short = input("present_bound");
    expect(
      planWaveformColumns({
        ...short,
        envelope: {
          ...short.envelope,
          sampleCount: 4_000,
          pairCount: 50,
          pairs: short.envelope.pairs.slice(0, 100),
          byteLength: 120,
        },
      }),
    ).toEqual([]);

    const context = {
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      strokeStyle: "",
      lineWidth: 0,
    } as unknown as CanvasRenderingContext2D;
    paintNleWaveforms(context, [input("present_bound")]);
    expect(context.stroke).toHaveBeenCalledTimes(1);
  });
});

describe("B-M2564-03 waveform output time in the source's time base", () => {
  // The reference fixture's `vid-timing`: 12 fps in a 1/12288 time base (1024 ticks a frame) on a
  // 24 fps timeline, with 5 s of embedded audio. The core places a clip's audio at the start
  // landmark's pts plus the elapsed output time (`semantic_conformance_expect._audio_windows`).
  const landmarks = (durations: readonly number[]) => {
    let pts = 0;
    return durations.map((durationTicks, frameIndex) => {
      const landmark = { frameIndex, pts, dts: pts, durationTicks };
      pts += durationTicks;
      return landmark;
    });
  };
  // Pair i spans -(124 - floor(i / 4)) to floor(i / 4): a column's minimum names the first pair
  // it read and its maximum the last.
  const envelope = {
    version: 1,
    pairRate: 100,
    sampleRate: 8_000,
    sampleCount: 40_000,
    pairCount: 500,
    pairs: Int8Array.from(
      Array.from({ length: 500 }, (_, index) => [
        -(124 - Math.floor(index / 4)),
        Math.floor(index / 4),
      ]).flat(),
    ),
    byteLength: 1_020,
  } as const;
  const timing = (
    durations: readonly number[],
    sourceStartFrame: number,
    durationFrames: number,
  ): WaveformPaintInput => ({
    asset: {
      assetId: "vid-timing",
      kind: "video",
      embeddedAudio: "present_bound",
      sourceSampleCount: 240_000,
      sourceTimeBase: { num: 1, den: 12_288 },
      sourceFrameCount: durations.length,
      landmarks: landmarks(durations),
    },
    outputFrameRate: { num: 24, den: 1 },
    clip: { sourceStartFrame, durationFrames },
    // One pixel an output frame.
    bounds: { x: 0, y: 0, width: durationFrames, height: 14 },
    visible: { x: 0, width: durationFrames },
    envelope,
  });
  const constant = Array.from({ length: 84 }, () => 1_024);

  it("draws a clip longer in output frames than its source has frames", () => {
    // 120 output frames are 5 s; the source has 84 frames (7 s).
    const columns = planWaveformColumns(timing(constant, 0, 120));
    expect(columns).toHaveLength(120);
    // Column p reads pairs floor(p * 100 / 24) up to ceil((p + 1) * 100 / 24).
    expect([0, 60, 119].map((pixel) => columns[pixel]!.max)).toEqual([
      1, 63, 124,
    ]);
  });

  it("starts from the start landmark's pts, not from its index plus output frames", () => {
    // Source frame 24 is 2 s in; 24 output frames later is 3 s, pair 300.
    const columns = planWaveformColumns(timing(constant, 24, 24));
    expect(columns).toHaveLength(24);
    expect([columns[0]!.min, columns[23]!.max]).toEqual([-74, 74]);
  });

  it("follows the start landmark of a VFR table, then output time", () => {
    // 24 frames of 512 ticks, then 40 of 2048: frame 24 is at 1 s, and one output second later
    // is 2 s whatever the later landmarks' intervals are.
    const vfr = [
      ...Array.from({ length: 24 }, () => 512),
      ...Array.from({ length: 40 }, () => 2_048),
    ];
    const columns = planWaveformColumns(timing(vfr, 24, 24));
    expect([columns[0]!.min, columns[23]!.max]).toEqual([-99, 49]);
  });

  it("leaves output time past the audio's end empty", () => {
    // 168 output frames are 7 s, the source's video length; its audio stops at 5 s.
    const columns = planWaveformColumns(timing(constant, 0, 168));
    expect(columns).toHaveLength(120);
    expect(columns.at(-1)!.x).toBe(119.5);
  });

  it("refuses a source start outside the source and a non-positive output rate", () => {
    expect(() => planWaveformColumns(timing(constant, 84, 12))).toThrow(
      "waveform paint input is invalid",
    );
    expect(() =>
      planWaveformColumns({
        ...timing(constant, 0, 12),
        outputFrameRate: { num: 0, den: 1 },
      }),
    ).toThrow("waveform paint input is invalid");
  });
});
