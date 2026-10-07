// M25-64 (R10, A64-4): the inspector's exact decimal adapter. Canonical integers (bp, per-mille,
// millidegrees) are shown and entered as % and degrees, converted by string arithmetic only, so
// every value the adapter prints parses back to the same integer and no float ever rounds one.

import { describe, expect, it } from "vitest";

import {
  UNIT_DECIMALS,
  formatUnitDisplay,
  formatUnitValue,
  parseUnitValue,
  unitInputStep,
  unitSymbol,
} from "../src/runtime/nleInspectorUnits";

describe("M25-64 R10 inspector units", () => {
  it("converts the plan's three examples exactly", () => {
    expect(parseUnitValue("12.34", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: 1_234,
    });
    expect(parseUnitValue("1.234", "mdeg", -180_000, 180_000)).toEqual({
      ok: true,
      value: 1_234,
    });
    expect(parseUnitValue("12.3", "permille", -1_000, 1_000)).toEqual({
      ok: true,
      value: 123,
    });
  });

  it("prints canonical integers at their precision without float artefacts", () => {
    expect(formatUnitValue(1_234, "bp")).toBe("12.34");
    expect(formatUnitValue(10_000, "bp")).toBe("100");
    expect(formatUnitValue(1_230, "bp")).toBe("12.3");
    expect(formatUnitValue(0, "bp")).toBe("0");
    expect(formatUnitValue(-50, "bp")).toBe("-0.5");
    expect(formatUnitValue(-40_000, "bp")).toBe("-400");
    expect(formatUnitValue(1, "bp")).toBe("0.01");
    expect(formatUnitValue(123, "permille")).toBe("12.3");
    expect(formatUnitValue(-1_000, "permille")).toBe("-100");
    expect(formatUnitValue(1_234, "mdeg")).toBe("1.234");
    expect(formatUnitValue(-180_000, "mdeg")).toBe("-180");
    expect(formatUnitValue(-1, "mdeg")).toBe("-0.001");
    // 0.1 + 0.2 style inputs never reach a float: 29 bp is 0.29 %, not 0.29000000000000004.
    expect(formatUnitValue(29, "bp")).toBe("0.29");
    expect(parseUnitValue("0.29", "bp", 0, 10_000)).toEqual({
      ok: true,
      value: 29,
    });
    expect(parseUnitValue("1.005", "mdeg", -180_000, 180_000)).toEqual({
      ok: true,
      value: 1_005,
    });
  });

  it("round-trips every untouched value of every field range", () => {
    const ranges = [
      ["bp", -40_000, 40_000],
      ["bp", 1, 80_000],
      ["bp", 0, 9_999],
      ["permille", -1_000, 1_000],
      ["permille", 0, 2_000],
      ["mdeg", -180_000, 180_000],
    ] as const;
    for (const [unit, min, max] of ranges)
      for (let value = min; value <= max; value += 1) {
        const parsed = parseUnitValue(
          formatUnitValue(value, unit),
          unit,
          min,
          max,
        );
        if (!parsed.ok || parsed.value !== value)
          throw new Error(`${unit} ${value} -> ${JSON.stringify(parsed)}`);
      }
  });

  it("accepts signs, a bare fraction and trailing zeros within the precision", () => {
    expect(parseUnitValue("+5", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: 500,
    });
    expect(parseUnitValue("-0.5", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: -50,
    });
    expect(parseUnitValue(".5", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: 50,
    });
    expect(parseUnitValue("5.", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: 500,
    });
    expect(parseUnitValue("12.30", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: 1_230,
    });
    expect(parseUnitValue(" 7 ", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: 700,
    });
    expect(parseUnitValue("-0", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: 0,
    });
  });

  it("handles both limits and rejects what lies outside them", () => {
    expect(parseUnitValue("-400", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: -40_000,
    });
    expect(parseUnitValue("400", "bp", -40_000, 40_000)).toEqual({
      ok: true,
      value: 40_000,
    });
    expect(parseUnitValue("400.01", "bp", -40_000, 40_000)).toEqual({
      ok: false,
      reason: "range",
    });
    expect(parseUnitValue("-180.001", "mdeg", -180_000, 180_000)).toEqual({
      ok: false,
      reason: "range",
    });
    expect(parseUnitValue("0", "bp", 1, 80_000)).toEqual({
      ok: false,
      reason: "range",
    });
    expect(parseUnitValue("0.01", "bp", 1, 80_000)).toEqual({
      ok: true,
      value: 1,
    });
  });

  it("rejects over-precise, non-finite and malformed input", () => {
    for (const [raw, unit] of [
      ["12.345", "bp"],
      ["12.34", "permille"],
      ["1.2345", "mdeg"],
      ["0.001", "bp"],
    ] as const)
      expect(parseUnitValue(raw, unit, -1_000_000, 1_000_000), raw).toEqual({
        ok: false,
        reason: "precision",
      });
    for (const raw of [
      "",
      " ",
      "-",
      ".",
      "+-1",
      "1e3",
      "Infinity",
      "-Infinity",
      "NaN",
      "0x10",
      "1,5",
      "1.2.3",
      "12 %",
      "１２",
    ])
      expect(parseUnitValue(raw, "bp", -1_000_000, 1_000_000), raw).toEqual({
        ok: false,
        reason: "format",
      });
    // More digits than a safe integer holds is refused, never rounded.
    expect(parseUnitValue("99999999999999999999", "bp", -1e30, 1e30)).toEqual({
      ok: false,
      reason: "range",
    });
  });

  it("names each unit's symbol, precision and input step", () => {
    // A clip's gain is millibels shown as dB and its fades are output frames shown as seconds.
    expect(UNIT_DECIMALS).toEqual({
      bp: 2,
      permille: 1,
      mdeg: 3,
      mb: 2,
      frames: 2,
    });
    expect(unitSymbol("bp")).toBe("%");
    expect(unitSymbol("permille")).toBe("%");
    expect(unitSymbol("mdeg")).toBe("°");
    expect(unitInputStep("bp")).toBe("0.01");
    expect(unitInputStep("permille")).toBe("0.1");
    expect(unitInputStep("mdeg")).toBe("0.001");
    expect(() => formatUnitValue(1.5, "bp")).toThrow();
  });
});

// A clip's audio: gain in millibels shown as dB (1 dB = 100 mb, a decimal shift like the others),
// and fades in output frames shown as seconds. A frame is not a whole number of hundredths of a
// second, so the frames unit is the one that rounds: its text is the nearest hundredth, and text
// read back snaps to the nearest frame.
describe("clip audio units", () => {
  it("shows millibels as dB exactly and reads dB text back to millibels", () => {
    expect(unitSymbol("mb")).toBe("dB");
    expect(unitInputStep("mb")).toBe("0.01");
    expect(formatUnitValue(-600, "mb")).toBe("-6");
    expect(formatUnitDisplay(-600, "mb", 1)).toBe("-6.0");
    expect(formatUnitDisplay(-605, "mb", 1)).toBe("-6.05");
    expect(formatUnitDisplay(1_200, "mb", 1)).toBe("12.0");
    expect(formatUnitDisplay(-6_000, "mb", 1)).toBe("-60.0");
    expect(parseUnitValue("-6", "mb", -6_000, 1_200)).toEqual({
      ok: true,
      value: -600,
    });
    expect(parseUnitValue("12.01", "mb", -6_000, 1_200)).toEqual({
      ok: false,
      reason: "range",
    });
    expect(parseUnitValue("-6.001", "mb", -6_000, 1_200)).toEqual({
      ok: false,
      reason: "precision",
    });
  });

  it("shows output frames as seconds at 24 per second and snaps seconds to a frame", () => {
    expect(unitSymbol("frames")).toBe("s");
    // One frame is not a decimal step; the browser's own step check must not judge the text.
    expect(unitInputStep("frames")).toBe("any");
    expect(formatUnitDisplay(0, "frames", 2)).toBe("0.00");
    expect(formatUnitDisplay(12, "frames", 2)).toBe("0.50");
    expect(formatUnitDisplay(13, "frames", 2)).toBe("0.54");
    expect(formatUnitDisplay(24, "frames", 2)).toBe("1.00");
    expect(formatUnitDisplay(240, "frames", 2)).toBe("10.00");
    expect(formatUnitValue(1, "frames")).toBe("0.04");
    // 3 frames are 12.5 hundredths: half rounds away from zero, on both sides of it.
    expect(formatUnitValue(3, "frames")).toBe("0.13");
    expect(formatUnitValue(-3, "frames")).toBe("-0.13");
    expect(parseUnitValue("0.5", "frames", 0, 240)).toEqual({
      ok: true,
      value: 12,
    });
    expect(parseUnitValue("0.3", "frames", 0, 240)).toEqual({
      ok: true,
      value: 7,
    });
    expect(parseUnitValue("10.01", "frames", 0, 240)).toEqual({
      ok: false,
      reason: "range",
    });
    expect(parseUnitValue("0.125", "frames", 0, 240)).toEqual({
      ok: false,
      reason: "precision",
    });
    expect(parseUnitValue("-0.04", "frames", 0, 240)).toEqual({
      ok: false,
      reason: "range",
    });
    // Below the field's stated range even where the nearest frame would be 0.
    for (const text of ["-0.01", "-0.02"])
      expect(parseUnitValue(text, "frames", 0, 240), text).toEqual({
        ok: false,
        reason: "range",
      });
    expect(Object.is(parseUnitValue("0", "frames", 0, 240).ok, true)).toBe(
      true,
    );
    expect(
      Object.is(
        (parseUnitValue("0", "frames", 0, 240) as { value: number }).value,
        0,
      ),
    ).toBe(true);
    expect(Object.isFrozen(UNIT_DECIMALS)).toBe(true);
  });

  it("round-trips every gain and every fade the member admits", () => {
    for (const [unit, min, max, decimals] of [
      ["mb", -6_000, 1_200, 1],
      ["frames", 0, 240, 2],
    ] as const)
      for (let value = min; value <= max; value += 1) {
        const text = formatUnitDisplay(value, unit, decimals);
        const parsed = parseUnitValue(text, unit, min, max);
        if (!parsed.ok || parsed.value !== value)
          throw new Error(`${unit} ${value} -> ${text}`);
      }
  });
});

// The field's display text: the exact value with at least the decimals the design draws. Padding
// only ever adds zeros, so the text still parses back to the stored integer.
describe("inspector display format", () => {
  it("pads to the minimum decimals and never rounds a value that needs more", () => {
    expect(formatUnitDisplay(0, "bp", 2)).toBe("0.00");
    expect(formatUnitDisplay(1_230, "bp", 2)).toBe("12.30");
    expect(formatUnitDisplay(-50, "bp", 2)).toBe("-0.50");
    expect(formatUnitDisplay(-40_000, "bp", 2)).toBe("-400.00");
    expect(formatUnitDisplay(10_000, "bp", 0)).toBe("100");
    expect(formatUnitDisplay(1_234, "bp", 0)).toBe("12.34");
    expect(formatUnitDisplay(0, "mdeg", 1)).toBe("0.0");
    expect(formatUnitDisplay(1_500, "mdeg", 1)).toBe("1.5");
    expect(formatUnitDisplay(1_234, "mdeg", 1)).toBe("1.234");
    expect(formatUnitDisplay(-180_000, "mdeg", 1)).toBe("-180.0");
    expect(formatUnitDisplay(123, "permille", 0)).toBe("12.3");
    // A minimum beyond the unit's precision is the unit's precision.
    expect(formatUnitDisplay(5, "permille", 4)).toBe("0.5");
    expect(() => formatUnitDisplay(1.5, "bp", 2)).toThrow();
  });

  it("round-trips every value of every field range at its minimum decimals", () => {
    const ranges = [
      ["bp", -40_000, 40_000, 2],
      ["bp", 1, 80_000, 0],
      ["bp", 0, 10_000, 0],
      ["permille", -1_000, 1_000, 0],
      ["mdeg", -180_000, 180_000, 1],
    ] as const;
    for (const [unit, min, max, decimals] of ranges)
      for (let value = min; value <= max; value += 1) {
        const text = formatUnitDisplay(value, unit, decimals);
        const parsed = parseUnitValue(text, unit, min, max);
        if (!parsed.ok || parsed.value !== value)
          throw new Error(`${unit} ${value} -> ${text}`);
        const fraction = text.split(".")[1] ?? "";
        if (fraction.length < decimals)
          throw new Error(`${unit} ${value} -> ${text} is under-padded`);
      }
  });
});
