import type { KeyboardEvent } from "react";

export type RangeKeyBounds = Readonly<{
  value: number;
  min: number;
  max: number;
  step: number;
}>;

/**
 * The value a native horizontal range would take for `key`, or `null` for a key it does not
 * handle. This restates the browser model instead of inventing a new one: arrows move one step,
 * PageUp/PageDown move max(step, span / 10), Home and End go to the bounds, and the result is
 * clamped and aligned to the step counted from the minimum.
 */
export function nativeRangeKeyTarget(
  key: string,
  { value, min, max, step }: RangeKeyBounds,
): number | null {
  const page = Math.max(step, (max - min) / 10);
  const raw =
    key === "ArrowRight" || key === "ArrowUp"
      ? value + step
      : key === "ArrowLeft" || key === "ArrowDown"
        ? value - step
        : key === "PageUp"
          ? value + page
          : key === "PageDown"
            ? value - page
            : key === "Home"
              ? min
              : key === "End"
                ? max
                : null;
  if (raw === null) return null;
  const clamped = Math.min(max, Math.max(min, raw));
  let aligned = Math.round((clamped - min) / step) * step + min;
  if (aligned > max) aligned -= step;
  else if (aligned < min) aligned += step;
  // Twelve significant digits absorb binary drift from fractional steps such as 0.1.
  return Number(aligned.toPrecision(12));
}

/**
 * Performs a range key itself. IMPORTANT (B-M2605-SEEK-02): a page-wide hotkey can cancel the
 * native default of every range input -- ComfyUI-Easy-Use binds the arrows through hotkeys-js on
 * `document`, which does not exempt ranges -- so a range left to its native step silently loses
 * those keys on such a host. The range applies the value and stops the consumed key, so it does
 * not also drive that hotkey behind the surface. Modifier combinations stay with the browser.
 */
export function ownRangeKeyDown(
  event: KeyboardEvent<HTMLInputElement>,
  bounds: RangeKeyBounds,
  apply: (value: number) => void,
): void {
  if (event.altKey || event.ctrlKey || event.metaKey) return;
  const target = nativeRangeKeyTarget(event.key, bounds);
  if (target === null) return;
  event.preventDefault();
  event.stopPropagation();
  // A native range fires no change for a key that does not move it.
  if (target !== bounds.value) apply(target);
}
