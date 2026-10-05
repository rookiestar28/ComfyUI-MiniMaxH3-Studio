// M25-64 (R10): the inspector's exact decimal adapter. Drafts, command builders and codecs keep
// the canonical integers -- basis points, per-mille, millidegrees, millibels and output frames --
// and only the numeric field's text is shown and read in %, degrees, dB and seconds.
//
// IMPORTANT: string arithmetic only, in both directions. A float step (`value / 100`,
// `Number(raw) * 100`) turns 29 bp into 0.29000000000000004 % and 1.005° into 1004 mdeg; an
// untouched value would then no longer round-trip, and re-committing it would issue an edit the
// user never made. Every value `formatUnitValue` prints parses back to the same integer.

export type InspectorUnit = "bp" | "permille" | "mdeg" | "mb" | "frames";

/**
 * Decimal places of the display unit: 1 % = 100 bp, 1 % = 10 per-mille, 1° = 1000 mdeg,
 * 1 dB = 100 mb; output frames are shown as seconds to the hundredth.
 */
export const UNIT_DECIMALS: Readonly<Record<InspectorUnit, number>> =
  Object.freeze({ bp: 2, permille: 1, mdeg: 3, mb: 2, frames: 2 });

type UnitSymbol = "%" | "°" | "dB" | "s";

const SYMBOL: Readonly<Record<InspectorUnit, UnitSymbol>> = Object.freeze({
  bp: "%",
  permille: "%",
  mdeg: "°",
  mb: "dB",
  frames: "s",
});

/**
 * The output profile admits 24 frames per second and no other rate (`compositionCodec.ts`,
 * `output.frame_rate`), so a fade's frames are seconds at this rate.
 */
const FRAMES_PER_SECOND = 24;

export function unitSymbol(unit: InspectorUnit): UnitSymbol {
  return SYMBOL[unit];
}

/** The number input's `step`: one canonical unit, in display units. */
export function unitInputStep(unit: InspectorUnit): string {
  // One frame is not a whole number of hundredths of a second. The field's own parser judges
  // the text; the browser's step check must not mark a valid frame as a step mismatch.
  if (unit === "frames") return "any";
  return `0.${"1".padStart(UNIT_DECIMALS[unit], "0")}`;
}

/** `numerator / denominator` rounded half away from zero, in integers. */
function roundedQuotient(numerator: number, denominator: number): number {
  const magnitude = Math.floor(
    (2 * Math.abs(numerator) + denominator) / (2 * denominator),
  );
  return numerator < 0 ? -magnitude : magnitude;
}

/** Hundredths of a second for a frame count: the one place the frames unit rounds. */
function frameHundredths(frames: number): number {
  return roundedQuotient(frames * 100, FRAMES_PER_SECOND);
}

/** A canonical integer in display units, trailing fractional zeros dropped. */
export function formatUnitValue(value: number, unit: InspectorUnit): string {
  if (!Number.isSafeInteger(value))
    throw new Error("inspector unit value must be a safe integer");
  // IMPORTANT: a frame count is shown as its nearest hundredth of a second, the only rounding
  // in this file. It cannot break the round trip: the hundredth is within 0.005 s, which is
  // 0.12 of a frame, so reading the text back snaps to the same frame.
  const shown = unit === "frames" ? frameHundredths(value) : value;
  const decimals = UNIT_DECIMALS[unit];
  const digits = String(Math.abs(shown)).padStart(decimals + 1, "0");
  const whole = digits.slice(0, digits.length - decimals);
  const fraction = digits.slice(digits.length - decimals).replace(/0+$/u, "");
  const body = fraction === "" ? whole : `${whole}.${fraction}`;
  return shown < 0 ? `-${body}` : body;
}

/**
 * The field's display text: the exact value with at least `minDecimals` fractional digits (never
 * more than the unit holds). A value that needs more digits shows them all.
 *
 * IMPORTANT: this pads `formatUnitValue`'s text with zeros and does nothing else. Do not replace
 * it with `toFixed` or any rounding format: 12.345° shown as "12.3" would be re-entered as
 * 12,300 mdeg by the next commit, an edit the user never made.
 */
export function formatUnitDisplay(
  value: number,
  unit: InspectorUnit,
  minDecimals: number,
): string {
  const exact = formatUnitValue(value, unit);
  const wanted = Math.min(minDecimals, UNIT_DECIMALS[unit]);
  const point = exact.indexOf(".");
  const fraction = point < 0 ? "" : exact.slice(point + 1);
  if (fraction.length >= wanted) return exact;
  const whole = point < 0 ? exact : exact.slice(0, point);
  return `${whole}.${fraction.padEnd(wanted, "0")}`;
}

export type ParsedUnitValue =
  | Readonly<{ ok: true; value: number }>
  | Readonly<{ ok: false; reason: "format" | "precision" | "range" }>;

// An optional sign, ASCII digits and at most one point. No exponent, no grouping, no locale
// digits: `Number()` would accept "1e3", "0x10" and "Infinity", and none of them is a value a
// person typed into a percentage field.
const DECIMAL = /^([+-]?)([0-9]*)(?:\.([0-9]*))?$/u;

/** Display-unit text to the canonical integer, inside `[min, max]` (canonical units). */
export function parseUnitValue(
  raw: string,
  unit: InspectorUnit,
  min: number,
  max: number,
): ParsedUnitValue {
  const match = DECIMAL.exec(raw.trim());
  if (match === null) return { ok: false, reason: "format" };
  const sign = match[1] ?? "";
  const whole = match[2] ?? "";
  const fraction = match[3] ?? "";
  if (whole === "" && fraction === "") return { ok: false, reason: "format" };
  const decimals = UNIT_DECIMALS[unit];
  // Zeros past the precision change nothing ("12.340" is 12.34 %); any other digit there is a
  // value the canonical unit cannot hold.
  if (/[1-9]/u.test(fraction.slice(decimals)))
    return { ok: false, reason: "precision" };
  const magnitude = Number(
    `${whole === "" ? "0" : whole}${fraction.slice(0, decimals).padEnd(decimals, "0")}`,
  );
  if (!Number.isSafeInteger(magnitude)) return { ok: false, reason: "range" };
  const shown = sign === "-" && magnitude !== 0 ? -magnitude : magnitude;
  if (unit !== "frames") {
    if (shown < min || shown > max) return { ok: false, reason: "range" };
    return { ok: true, value: shown };
  }
  // Seconds are judged against the range as the field states it ("0 to 10 s"), then snap to
  // the nearest frame: "10.01" is outside the field even though its nearest frame is not.
  if (shown < frameHundredths(min) || shown > frameHundredths(max))
    return { ok: false, reason: "range" };
  const value = roundedQuotient(shown * FRAMES_PER_SECOND, 100);
  if (value < min || value > max) return { ok: false, reason: "range" };
  return { ok: true, value };
}
