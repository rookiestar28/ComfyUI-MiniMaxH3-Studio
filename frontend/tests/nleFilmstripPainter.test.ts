import { describe, expect, it, vi } from "vitest";

import {
  paintFilmstrip,
  planFilmstripCells,
  type FilmstripPaintInput,
} from "../src/runtime/nleFilmstripPainter";

const asset = {
  assetId: "video-1",
  kind: "video" as const,
  sourceWidth: 160,
  sourceHeight: 90,
  sourceTimeBase: { num: 1, den: 10 },
  sourceFrameCount: 8,
  landmarks: Array.from({ length: 8 }, (_, frameIndex) => ({
    frameIndex,
    pts: frameIndex,
    dts: frameIndex,
    durationTicks: 1,
  })),
};

// B-M2563-05: the painter maps output frames into the source's time base, so every input names
// the timeline's frame rate. This fixture is a 10 fps source (one 1/10 s tick per frame) on a
// 10 fps timeline, where the mapping is the identity the M25-49 cases always assumed.
const input = (
  overrides: Partial<FilmstripPaintInput> = {},
): FilmstripPaintInput => ({
  asset,
  outputFrameRate: { num: 10, den: 1 },
  clip: { sourceStartFrame: 2, durationFrames: 4 },
  bounds: { x: 10, y: 20, width: 180, height: 45 },
  visible: { x: 10, width: 180 },
  sprite: {
    bitmap: {} as ImageBitmap,
    width: 320,
    height: 48,
    tileCount: 8,
  },
  ...overrides,
});

describe("M25-49 filmstrip painter", () => {
  it("uses fixed source aspect cells and trim/slip source time", () => {
    const cells = planFilmstripCells(input());
    expect(cells.map((cell) => cell.sourceTile)).toEqual([2, 4, 5]);
    expect(cells.map((cell) => cell.destination.width)).toEqual([80, 80, 20]);
    expect(cells[2]?.source.width).toBe(10);
  });

  it("clips both viewport edges without stretching a partial cell", () => {
    const cells = planFilmstripCells(input({ visible: { x: 35, width: 90 } }));
    expect(cells[0]?.destination).toEqual({
      x: 35,
      y: 20,
      width: 55,
      height: 45,
    });
    expect(cells.at(-1)?.destination).toEqual({
      x: 90,
      y: 20,
      width: 35,
      height: 45,
    });
    expect(cells[0]?.source.width).toBeCloseTo(27.5);
    expect(cells.at(-1)?.source.width).toBeCloseTo(17.5);
  });

  it("maps sparse timing landmarks without requiring one landmark per source frame", () => {
    // 512 ticks of 1/12288 s per frame: a 24 fps source on a 24 fps timeline.
    const sparseAsset = {
      ...asset,
      sourceTimeBase: { num: 1, den: 12288 },
      sourceFrameCount: 72,
      landmarks: [
        { frameIndex: 0, pts: 0, dts: 0, durationTicks: 512 },
        { frameIndex: 12, pts: 6144, dts: 6144, durationTicks: 512 },
        { frameIndex: 47, pts: 24064, dts: 24064, durationTicks: 512 },
      ],
    };
    const cells = planFilmstripCells(
      input({
        asset: sparseAsset,
        outputFrameRate: { num: 24, den: 1 },
        clip: { sourceStartFrame: 12, durationFrames: 36 },
      }),
    );

    expect(cells.map((cell) => cell.sourceTile)).toEqual([3, 6, 7]);
  });

  it("paints planned cells only and treats missing filmstrip as an empty layer", () => {
    const drawImage = vi.fn();
    const context = { drawImage } as unknown as CanvasRenderingContext2D;
    paintFilmstrip(context, input());
    expect(drawImage).toHaveBeenCalledTimes(3);
    paintFilmstrip(context, null);
    expect(drawImage).toHaveBeenCalledTimes(3);
  });

  it("refuses mismatched sprite geometry and a source start outside the source", () => {
    expect(() =>
      planFilmstripCells(input({ sprite: { ...input().sprite, width: 319 } })),
    ).toThrow();
    expect(() =>
      planFilmstripCells(
        input({ clip: { sourceStartFrame: 8, durationFrames: 2 } }),
      ),
    ).toThrow("clip.source-range");
    expect(() =>
      planFilmstripCells(input({ outputFrameRate: { num: 0, den: 1 } })),
    ).toThrow("clip.output-rate");
  });
});

// B-M2563-05: `source_start_frame` names a source landmark while `durationFrames` is output time
// (timeline_authoring.py:463). An output frame shows the source at the start landmark's pts plus
// the elapsed output time in source ticks (composition_contract._source_target_tick). The painter
// added output frames to source frames and compared the sum with the source frame count, so every
// draw of a different-rate or VFR clip threw, and a same-length one picked the wrong tiles.
describe("B-M2563-05 filmstrip output time in the source's time base", () => {
  // The reference fixture's `vid-timing`: a 12 fps source (1,024 ticks of 1/12288 s per frame),
  // 84 frames, on a 24 fps timeline; a 120-frame clip is 5 s of its 7.
  const twelve = {
    assetId: "vid-timing",
    kind: "video" as const,
    sourceWidth: 160,
    sourceHeight: 90,
    sourceTimeBase: { num: 1, den: 12288 },
    sourceFrameCount: 84,
    landmarks: Array.from({ length: 84 }, (_, frameIndex) => ({
      frameIndex,
      pts: frameIndex * 1024,
      dts: frameIndex * 1024,
      durationTicks: 1024,
    })),
  };
  const sprite = {
    bitmap: {} as ImageBitmap,
    width: 64 * 14,
    height: 48,
    // A tile per half second of the 7 s source, so no cell centre falls on a rounding tie.
    tileCount: 14,
  };
  // One cell per output second: bounds 5 s wide at 80 px each, cells 80 px wide (16:9 at 45 px).
  const plan = (
    clip: { sourceStartFrame: number; durationFrames: number },
    asset: FilmstripPaintInput["asset"] = twelve,
  ) =>
    planFilmstripCells({
      asset,
      outputFrameRate: { num: 24, den: 1 },
      clip,
      bounds: { x: 0, y: 0, width: 400, height: 45 },
      visible: { x: 0, width: 400 },
      sprite,
    });

  it("draws a clip longer in output frames than its source has frames", () => {
    // Cell centres at 0.5 s .. 4.5 s -> ticks 6144 .. 55296 of 86016 -> tiles round(t * 14 / 86016).
    expect(
      plan({ sourceStartFrame: 0, durationFrames: 120 }).map(
        (cell) => cell.sourceTile,
      ),
    ).toEqual([1, 3, 5, 7, 9]);
  });

  it("starts from the start landmark's pts, not from its index plus output frames", () => {
    // Source frame 24 is 2 s in (24 * 1024 = 24576 ticks); the same five seconds follow.
    expect(
      plan({ sourceStartFrame: 24, durationFrames: 120 }).map(
        (cell) => cell.sourceTile,
      ),
    ).toEqual([5, 7, 9, 11, 13]);
  });

  it("follows unequal intervals of a VFR table", () => {
    // Frames of 512 ticks for the first second, then 2048: frame 24 is at 12288, the end at 94208.
    const vfr = {
      ...twelve,
      sourceFrameCount: 64,
      landmarks: Array.from({ length: 64 }, (_, frameIndex) => {
        const pts =
          frameIndex < 24 ? frameIndex * 512 : 12288 + (frameIndex - 24) * 2048;
        return {
          frameIndex,
          pts,
          dts: pts,
          durationTicks: frameIndex < 24 ? 512 : 2048,
        };
      }),
    };
    // From frame 24: ticks 12288 + 12288 * (0.5 .. 4.5 s) -> tiles round(t * 14 / 94208).
    expect(
      plan({ sourceStartFrame: 24, durationFrames: 120 }, vfr).map(
        (cell) => cell.sourceTile,
      ),
    ).toEqual([3, 5, 6, 8, 10]);
  });

  it("shows the source's last tile for output time past its declared end", () => {
    // From 6 s, only 1 s of source remains; the later cells hold the last tile.
    expect(
      plan({ sourceStartFrame: 72, durationFrames: 120 }).map(
        (cell) => cell.sourceTile,
      ),
    ).toEqual([13, 13, 13, 13, 13]);
  });
});
