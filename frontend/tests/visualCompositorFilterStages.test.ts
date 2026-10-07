/**
 * The native preview path's colour filter, against the renderer's own `eq`.
 *
 * B-M2545-22: the software path (`applyVisualColorAdjust`) was pinned against the oracle in
 * `visualCompositor.test.ts` and matched it. The native path -- the SVG filter the preview
 * actually uses whenever the probe succeeds, which is every real browser -- was exported and
 * never measured against anything. It composed the three `eq` planes and both colour-space maps
 * into a single `feColorMatrix`, which cannot express the planes' individual saturation, and so
 * disagreed with the renderer by up to 238 code values at the declared bounds while agreeing
 * within 1 in the mid range that the one measurement run had sampled.
 *
 * This suite measures the thing the browser evaluates, over the whole declared parameter range,
 * against the same oracle the software path is held to.
 */

import { describe, expect, it } from "vitest";

import {
  applyVisualColorAdjust,
  colorAdjustFilterStages,
} from "../src/runtime/visualCompositor";

/** `ToleranceProfile.color_adjust_max_abs_channel`, the corpus bound these rows are judged on. */
const COLOR_ADJUST_TOLERANCE = 12;
/** `ToleranceProfile.patch_max_abs_channel`, the tighter bound an interior patch is judged on. */
const PATCH_TOLERANCE = 8;
/**
 * The measured worst case of the two-stage filter over the sweep below. A finer sweep of the same
 * space against `semantic_conformance_expect._adjusted` directly -- every 100 permille, 424
 * swatches, 3.9M samples -- measures 6, so this grid's 4 is a sample of that bound, not a
 * tighter claim about the filter.
 */
const MEASURED_WORST = 4;

const BRIGHTNESS = [-1000, -700, -400, -200, -50, 0, 50, 200, 400, 700, 1000];
const CONTRAST = [0, 200, 500, 800, 1000, 1200, 1600, 2000];
const SATURATION = [0, 200, 500, 800, 1000, 1200, 1600, 2000];

const SWATCHES: readonly (readonly [number, number, number])[] = [
  [0, 0, 0],
  [255, 255, 255],
  [128, 128, 128],
  [200, 40, 40],
  [40, 200, 40],
  [40, 40, 200],
  [17, 34, 51],
  [240, 200, 60],
  [255, 255, 128],
  [128, 250, 255],
  [7, 250, 3],
  [250, 3, 250],
  [64, 96, 176],
  [176, 96, 64],
];

/**
 * One stage as a filter primitive evaluates it: the affine map in 0..1, then the clamp and the
 * 8-bit result buffer that the next primitive reads. The clamp is the point of the exercise.
 */
function stage(
  values: readonly number[],
  channels: readonly [number, number, number],
): [number, number, number] {
  const out: number[] = [];
  for (let row = 0; row < 3; row += 1) {
    let value = values[row * 5 + 4]!;
    for (let input = 0; input < 3; input += 1)
      value += values[row * 5 + input]! * channels[input]!;
    out.push(Math.min(255, Math.max(0, Math.round(value * 255))) / 255);
  }
  return out as [number, number, number];
}

function throughFilter(
  rgb: readonly [number, number, number],
  brightness: number,
  contrast: number,
  saturation: number,
): [number, number, number] {
  const stages = colorAdjustFilterStages(brightness, contrast, saturation);
  let channels: [number, number, number] = [
    rgb[0] / 255,
    rgb[1] / 255,
    rgb[2] / 255,
  ];
  for (const values of stages) channels = stage(values, channels);
  return [
    Math.round(channels[0] * 255),
    Math.round(channels[1] * 255),
    Math.round(channels[2] * 255),
  ];
}

/** The oracle: the software path, which `visualCompositor.test.ts` pins to `_adjusted`. */
function throughSoftware(
  rgb: readonly [number, number, number],
  brightness: number,
  contrast: number,
  saturation: number,
): [number, number, number] {
  const adjusted = applyVisualColorAdjust(
    new Uint8ClampedArray([...rgb, 255]),
    {
      kind: "color_adjust_v1",
      brightnessPermille: brightness,
      contrastPermille: contrast,
      saturationPermille: saturation,
    },
  );
  return [adjusted[0]!, adjusted[1]!, adjusted[2]!];
}

function sweep(
  evaluate: (
    rgb: readonly [number, number, number],
    brightness: number,
    contrast: number,
    saturation: number,
  ) => readonly [number, number, number],
): { worst: number; where: string; overPatch: number; samples: number } {
  let worst = 0;
  let where = "";
  let overPatch = 0;
  let samples = 0;
  for (const brightness of BRIGHTNESS)
    for (const contrast of CONTRAST)
      for (const saturation of SATURATION)
        for (const rgb of SWATCHES) {
          const expected = throughSoftware(
            rgb,
            brightness,
            contrast,
            saturation,
          );
          const actual = evaluate(rgb, brightness, contrast, saturation);
          let delta = 0;
          for (let index = 0; index < 3; index += 1)
            delta = Math.max(
              delta,
              Math.abs(expected[index]! - actual[index]!),
            );
          samples += 1;
          if (delta > PATCH_TOLERANCE) overPatch += 1;
          if (delta > worst) {
            worst = delta;
            where = `rgb ${rgb.join(",")} at b=${brightness} c=${contrast} s=${saturation}: expected ${expected.join(",")}, filter ${actual.join(",")}`;
          }
        }
  return { worst, where, overPatch, samples };
}

describe("the native preview filter", () => {
  it("is two chained stages, each a well-formed 4x5 matrix with an untouched alpha row", () => {
    const stages = colorAdjustFilterStages(1000, 2000, 2000);
    expect(stages).toHaveLength(2);
    for (const values of stages) {
      expect(values).toHaveLength(20);
      for (const value of values) expect(Number.isFinite(value)).toBe(true);
      // Alpha passes through untouched: the adjustment is a colour operation, and a filter that
      // leaked a colour term into alpha would change every composite that reads it.
      expect([...values.slice(15)]).toEqual([0, 0, 0, 1, 0]);
      // No colour row reads alpha either, so a premultiplied surface cannot tint the result.
      for (let row = 0; row < 3; row += 1) expect(values[row * 5 + 3]).toBe(0);
    }
  });

  it("is the identity when the adjustment is, to within the plane round trip", () => {
    for (const rgb of SWATCHES) {
      const actual = throughFilter(rgb, 0, 1000, 1000);
      for (let index = 0; index < 3; index += 1)
        expect(Math.abs(actual[index]! - rgb[index]!)).toBeLessThanOrEqual(1);
    }
  });

  it("matches the renderer's eq across the whole declared parameter range", () => {
    const result = sweep(throughFilter);
    expect(result.samples).toBe(
      BRIGHTNESS.length * CONTRAST.length * SATURATION.length * SWATCHES.length,
    );
    // Not one sample outside even the tighter interior-patch bound.
    expect(`${result.overPatch} over ${PATCH_TOLERANCE}: ${result.where}`).toBe(
      `0 over ${PATCH_TOLERANCE}: ${result.where}`,
    );
    expect(result.worst).toBeLessThanOrEqual(COLOR_ADJUST_TOLERANCE);
    // Pinned, so a change that quietly widens the error is a failure rather than a smaller margin.
    expect(`worst ${result.worst} at ${result.where}`).toBe(
      `worst ${MEASURED_WORST} at ${result.where}`,
    );
  });

  it("would have caught B-M2545-22: one composed matrix is far outside the bound", () => {
    // The shipped formulation before the repair, rebuilt here from the two stages so it cannot
    // drift away from what it is a claim about: composing them algebraically is exactly what
    // dropping the intermediate clamp does.
    const composed = (
      rgb: readonly [number, number, number],
      brightness: number,
      contrast: number,
      saturation: number,
    ): [number, number, number] => {
      const [planes, inverse] = colorAdjustFilterStages(
        brightness,
        contrast,
        saturation,
      ) as readonly [readonly number[], readonly number[]];
      const channels: [number, number, number] = [
        rgb[0] / 255,
        rgb[1] / 255,
        rgb[2] / 255,
      ];
      const intermediate: number[] = [];
      for (let row = 0; row < 3; row += 1) {
        let value = planes[row * 5 + 4]!;
        for (let input = 0; input < 3; input += 1)
          value += planes[row * 5 + input]! * channels[input]!;
        intermediate.push(value); // no clamp: this is the defect
      }
      const out: number[] = [];
      for (let row = 0; row < 3; row += 1) {
        let value = inverse[row * 5 + 4]!;
        for (let input = 0; input < 3; input += 1)
          value += inverse[row * 5 + input]! * intermediate[input]!;
        out.push(Math.min(255, Math.max(0, Math.round(value * 255))));
      }
      return out as [number, number, number];
    };

    const result = sweep(composed);
    expect(result.worst).toBeGreaterThan(200);
    // Not a corner case: more than a quarter of the declared space is outside the bound.
    expect(result.overPatch / result.samples).toBeGreaterThan(0.25);
  });
});
