// M25-20: the browser landmark collector, exercised on synthetic buffers.
//
// The collector runs inside the page during the conformance journey, where nothing can assert
// against it. These tests are where its rules are actually pinned, and they are deliberately the
// mirror image of the Python extractor's tests in `tests/test_m25_render_conformance.py`: same tile
// bit order, same patch geometry, same edge-distance accounting. If one side is changed alone, one
// of the two suites goes red.

import { describe, expect, it } from "vitest";

import {
  fiducialBounds,
  patchMean,
  readFrameIdTile,
  type Rgba,
} from "../e2e/nleSemanticLandmarks";

function rgba(
  width: number,
  height: number,
  fill: readonly [number, number, number],
): {
  image: Rgba;
  set: (
    x: number,
    y: number,
    colour: readonly [number, number, number],
  ) => void;
} {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let index = 0; index < width * height; index += 1) {
    data[index * 4] = fill[0];
    data[index * 4 + 1] = fill[1];
    data[index * 4 + 2] = fill[2];
    data[index * 4 + 3] = 255;
  }
  const image: Rgba = { data, width, height };
  return {
    image,
    set: (x, y, colour) => {
      const offset = (y * width + x) * 4;
      data[offset] = colour[0];
      data[offset + 1] = colour[1];
      data[offset + 2] = colour[2];
      data[offset + 3] = 255;
    },
  };
}

describe("patchMean", () => {
  it("averages an interior patch exactly and records its measured edge distance", () => {
    const { image, set } = rgba(32, 32, [10, 20, 30]);
    for (let y = 14; y <= 18; y += 1) {
      for (let x = 14; x <= 18; x += 1) set(x, y, [200, 100, 50]);
    }
    const patch = patchMean(image, {
      label: "p",
      centerX: 16,
      centerY: 16,
      sizePx: 5,
    });
    expect([patch.red, patch.green, patch.blue]).toEqual([200, 100, 50]);
    // The same value the Python extractor reports for the same geometry.
    expect(patch.edgeDistancePx).toBe(13);
  });

  it("refuses a patch that does not fit rather than clamping it to the edge", () => {
    const { image } = rgba(8, 8, [0, 0, 0]);
    expect(() =>
      patchMean(image, { label: "p", centerX: 1, centerY: 4, sizePx: 5 }),
    ).toThrow(/does not fit/);
  });

  it("refuses an even patch size, which has no single centre pixel", () => {
    const { image } = rgba(32, 32, [0, 0, 0]);
    expect(() =>
      patchMean(image, { label: "p", centerX: 16, centerY: 16, sizePx: 4 }),
    ).toThrow(/positive odd/);
  });
});

describe("readFrameIdTile", () => {
  const width = 64;
  const height = 16;
  const cellPx = 4;
  const bits = 6;

  function tile(identifier: number) {
    const { image, set } = rgba(width, height, [0, 0, 0]);
    for (let index = 0; index < bits; index += 1) {
      if (identifier & (1 << (bits - 1 - index))) {
        for (let y = 0; y < cellPx; y += 1) {
          for (let x = index * cellPx; x < (index + 1) * cellPx; x += 1) {
            set(x, y, [255, 255, 255]);
          }
        }
      }
    }
    return image;
  }

  it.each([0, 1, 7, 23, 63])("round-trips identifier %i", (identifier) => {
    expect(
      readFrameIdTile(tile(identifier), {
        originX: 0,
        originY: 0,
        cellPx,
        bits,
      }),
    ).toBe(identifier);
  });

  it("reads cell centres so edge blending cannot flip a bit", () => {
    const { image, set } = rgba(width, height, [0, 0, 0]);
    for (let y = 0; y < cellPx; y += 1) {
      for (let x = 0; x < cellPx; x += 1) set(x, y, [255, 255, 255]);
      set(0, y, [120, 120, 120]);
      set(cellPx - 1, y, [120, 120, 120]);
    }
    expect(
      readFrameIdTile(image, { originX: 0, originY: 0, cellPx, bits }),
    ).toBe(1 << (bits - 1));
  });

  it("refuses a tile that falls outside the canvas", () => {
    const { image } = rgba(width, height, [0, 0, 0]);
    expect(() =>
      readFrameIdTile(image, { originX: 60, originY: 0, cellPx, bits }),
    ).toThrow(/outside the canvas/);
  });
});

describe("fiducialBounds", () => {
  it("returns the exact bounding box of a fiducial", () => {
    const { image, set } = rgba(24, 24, [0, 0, 0]);
    for (let y = 5; y <= 9; y += 1) {
      for (let x = 3; x <= 11; x += 1) set(x, y, [250, 8, 8]);
    }
    expect(
      fiducialBounds(image, {
        label: "f",
        red: 255,
        green: 0,
        blue: 0,
        tolerance: 8,
      }),
    ).toEqual({ label: "f", left: 3, top: 5, right: 11, bottom: 9 });
  });

  it("returns null for an absent fiducial instead of a zero box at the origin", () => {
    // A zero-sized box at (0,0) would be compared as though it were an observation, and would
    // agree with any expectation whose landmark also happens to start there.
    const { image } = rgba(24, 24, [0, 0, 0]);
    expect(
      fiducialBounds(image, {
        label: "f",
        red: 255,
        green: 0,
        blue: 0,
        tolerance: 8,
      }),
    ).toBeNull();
  });
});
