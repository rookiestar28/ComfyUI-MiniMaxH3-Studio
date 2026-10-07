import { describe, expect, it } from "vitest";
import { nativeRangeKeyTarget } from "../src/components/ownedRangeKeys";

// The expected values restate the native horizontal range keyboard model (Chromium
// RangeInputType): arrows move one step, PageUp/PageDown move max(step, span / 10), Home and End
// go to the bounds, and every result is clamped and aligned to the step from the minimum.
describe("B-M2605-SEEK-02 owned range keys", () => {
  const bounds = { value: 40, min: 0, max: 480, step: 1 };

  it("steps arrows by one step in either axis", () => {
    expect(nativeRangeKeyTarget("ArrowRight", bounds)).toBe(41);
    expect(nativeRangeKeyTarget("ArrowUp", bounds)).toBe(41);
    expect(nativeRangeKeyTarget("ArrowLeft", bounds)).toBe(39);
    expect(nativeRangeKeyTarget("ArrowDown", bounds)).toBe(39);
  });

  it("pages by a tenth of the span, never less than one step", () => {
    expect(nativeRangeKeyTarget("PageUp", bounds)).toBe(88);
    expect(nativeRangeKeyTarget("PageDown", bounds)).toBe(0);
    expect(
      nativeRangeKeyTarget("PageUp", { value: 0, min: 0, max: 5, step: 2 }),
    ).toBe(2);
  });

  it("goes to the bounds on Home and End, aligned to the step", () => {
    expect(nativeRangeKeyTarget("Home", bounds)).toBe(0);
    expect(nativeRangeKeyTarget("End", bounds)).toBe(480);
    expect(
      nativeRangeKeyTarget("End", { value: 0, min: 0, max: 7, step: 3 }),
    ).toBe(6);
  });

  it("clamps at the bounds and aligns fractional steps without drift", () => {
    expect(
      nativeRangeKeyTarget("ArrowRight", {
        value: 480,
        min: 0,
        max: 480,
        step: 1,
      }),
    ).toBe(480);
    expect(
      nativeRangeKeyTarget("ArrowLeft", {
        value: 0,
        min: 0,
        max: 480,
        step: 1,
      }),
    ).toBe(0);
    expect(
      nativeRangeKeyTarget("ArrowRight", {
        value: 0.2,
        min: 0,
        max: 2,
        step: 0.1,
      }),
    ).toBe(0.3);
    expect(
      nativeRangeKeyTarget("End", { value: 0, min: 0, max: 7.998, step: 0.1 }),
    ).toBe(7.9);
    expect(
      nativeRangeKeyTarget("ArrowRight", {
        value: 50,
        min: 0,
        max: 480,
        step: 48,
      }),
    ).toBe(96);
  });

  it("leaves every other key to its owner", () => {
    for (const key of ["Enter", " ", "Tab", "Escape", "a", "Delete"])
      expect(nativeRangeKeyTarget(key, bounds)).toBeNull();
  });
});
