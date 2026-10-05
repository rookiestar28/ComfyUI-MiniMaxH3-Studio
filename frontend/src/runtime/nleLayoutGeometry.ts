// M25-44: pure geometry for the four-region reference shell.
//
// A layout is three shares of the workspace (R1 and R3 of its width, the top band of its height).
// Resolving it against a measured stage clamps every region to its tier minimum with R2 and the
// timeline as the absorbers. A resize never rewrites the stored shares, so the user's proportions
// come back when the workspace grows again; only an explicit splitter move stores new shares, and
// it stores the pixel sizes the user actually sees. Geometry is mount/session memory only.

import {
  NLE_REFERENCE_UI_CONTRACT_V1 as CONTRACT,
  type NleSplitterId,
} from "../contracts/nleReferenceUiContract";

export type NleLayoutTier = "standard" | "narrow" | "scroll_floor";
export type NleLayout = Readonly<{
  bin: number;
  inspector: number;
  top: number;
}>;
export type NleStageSize = Readonly<{ width: number; height: number }>;
export type NleLayoutBoxes = Readonly<{
  tier: NleLayoutTier;
  /** The effective workspace: the measured stage, never below the scroll floor. */
  width: number;
  height: number;
  bin: number;
  monitor: number;
  inspector: number;
  top: number;
  timeline: number;
  overflowX: boolean;
  overflowY: boolean;
}>;
export type NleSplitterRange = Readonly<{
  size: number;
  min: number;
  max: number;
  total: number;
}>;

const GUTTER = CONTRACT.gutterPx;
const NARROW = CONTRACT.minimums.narrow;

export const NLE_LAYOUT_FLOOR_WIDTH =
  NARROW.bin + NARROW.monitor + NARROW.inspector + 2 * GUTTER;
export const NLE_LAYOUT_FLOOR_HEIGHT =
  CONTRACT.topMinPx + GUTTER + CONTRACT.timelineMinPx;

export const DEFAULT_NLE_LAYOUT: NleLayout = Object.freeze({
  ...CONTRACT.defaultShares,
});

function share(value: unknown, fallback: number): number {
  return typeof value === "number" &&
    Number.isFinite(value) &&
    value > 0 &&
    value < 1
    ? value
    : fallback;
}

/** A retained or requested layout with every invalid share replaced by its default. */
export function normalizeLayout(value: unknown): NleLayout {
  const record =
    typeof value === "object" && value !== null
      ? (value as Record<string, unknown>)
      : {};
  return Object.freeze({
    bin: share(record.bin, DEFAULT_NLE_LAYOUT.bin),
    inspector: share(record.inspector, DEFAULT_NLE_LAYOUT.inspector),
    top: share(record.top, DEFAULT_NLE_LAYOUT.top),
  });
}

export function layoutTier(width: number): NleLayoutTier {
  if (!(width >= CONTRACT.tiers.narrowMinWidth)) return "scroll_floor";
  return width >= CONTRACT.tiers.standardMinWidth ? "standard" : "narrow";
}

function minimums(tier: NleLayoutTier) {
  return tier === "standard"
    ? CONTRACT.minimums.standard
    : CONTRACT.minimums.narrow;
}

function measured(value: number): number {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

export function resolveLayout(
  layout: NleLayout,
  stage: NleStageSize,
): NleLayoutBoxes {
  const safe = normalizeLayout(layout);
  const stageWidth = measured(stage.width);
  const stageHeight = measured(stage.height);
  const tier = layoutTier(stageWidth);
  const min = minimums(tier);
  const width = Math.max(stageWidth, NLE_LAYOUT_FLOOR_WIDTH);
  const height = Math.max(stageHeight, NLE_LAYOUT_FLOOR_HEIGHT);
  const columns = width - 2 * GUTTER;
  const bin = clamp(
    Math.round(safe.bin * width),
    min.bin,
    columns - min.monitor - min.inspector,
  );
  const inspector = clamp(
    Math.round(safe.inspector * width),
    min.inspector,
    columns - min.monitor - bin,
  );
  const rows = height - GUTTER;
  const top = clamp(
    Math.round(safe.top * height),
    CONTRACT.topMinPx,
    rows - CONTRACT.timelineMinPx,
  );
  return Object.freeze({
    tier,
    width,
    height,
    bin,
    monitor: columns - bin - inspector,
    inspector,
    top,
    timeline: rows - top,
    overflowX: stageWidth < width,
    overflowY: stageHeight < height,
  });
}

/** The splitter's primary pane: R1 for S1, R3 for S2, the top band for S3. */
export function splitterRange(
  boxes: NleLayoutBoxes,
  splitter: NleSplitterId,
): NleSplitterRange {
  const min = minimums(boxes.tier);
  const columns = boxes.width - 2 * GUTTER;
  switch (splitter) {
    case "bin_monitor":
      return Object.freeze({
        size: boxes.bin,
        min: min.bin,
        max: columns - min.monitor - boxes.inspector,
        total: boxes.width,
      });
    case "monitor_inspector":
      return Object.freeze({
        size: boxes.inspector,
        min: min.inspector,
        max: columns - min.monitor - boxes.bin,
        total: boxes.width,
      });
    case "top_timeline":
      return Object.freeze({
        size: boxes.top,
        min: CONTRACT.topMinPx,
        max: boxes.height - GUTTER - CONTRACT.timelineMinPx,
        total: boxes.height,
      });
  }
}

function withPrimary(
  boxes: NleLayoutBoxes,
  splitter: NleSplitterId,
  size: number,
): NleLayout {
  const range = splitterRange(boxes, splitter);
  const primary = clamp(Math.round(size), range.min, range.max) / range.total;
  // IMPORTANT: store what the user sees for the other panes, not their requested shares. A
  // requested share that the tier clamped would otherwise re-expand after this move and take the
  // dragged distance from R2 twice, so the neighbour boxes would not move by the pointer distance.
  const seen = {
    bin: boxes.bin / boxes.width,
    inspector: boxes.inspector / boxes.width,
    top: boxes.top / boxes.height,
  };
  return normalizeLayout({
    ...seen,
    ...(splitter === "bin_monitor"
      ? { bin: primary }
      : splitter === "monitor_inspector"
        ? { inspector: primary }
        : { top: primary }),
  });
}

/**
 * Move a splitter by `deltaPx` along its axis: right or down is positive. A pointer drag applies
 * its total displacement to the layout captured at pointer down.
 */
export function moveSplitter(
  layout: NleLayout,
  splitter: NleSplitterId,
  deltaPx: number,
  stage: NleStageSize,
): NleLayout {
  const boxes = resolveLayout(layout, stage);
  const range = splitterRange(boxes, splitter);
  const delta = Number.isFinite(deltaPx) ? deltaPx : 0;
  const sign = splitter === "monitor_inspector" ? -1 : 1;
  return withPrimary(boxes, splitter, range.size + sign * delta);
}

export function stepLayout(
  layout: NleLayout,
  splitter: NleSplitterId,
  direction: -1 | 1,
  coarse: boolean,
  stage: NleStageSize,
): NleLayout {
  const step = coarse ? CONTRACT.keyboardCoarseStepPx : CONTRACT.keyboardStepPx;
  return moveSplitter(layout, splitter, direction * step, stage);
}

/** Home gives the primary pane its smallest allowed size, End its largest. */
export function extremeLayout(
  layout: NleLayout,
  splitter: NleSplitterId,
  extreme: "min" | "max",
  stage: NleStageSize,
): NleLayout {
  const boxes = resolveLayout(layout, stage);
  const range = splitterRange(boxes, splitter);
  return withPrimary(
    boxes,
    splitter,
    extreme === "min" ? range.min : range.max,
  );
}

export function resetSplitter(
  layout: NleLayout,
  splitter: NleSplitterId,
  stage: NleStageSize,
): NleLayout {
  const boxes = resolveLayout(layout, stage);
  const range = splitterRange(boxes, splitter);
  const fallback =
    splitter === "bin_monitor"
      ? DEFAULT_NLE_LAYOUT.bin
      : splitter === "monitor_inspector"
        ? DEFAULT_NLE_LAYOUT.inspector
        : DEFAULT_NLE_LAYOUT.top;
  return withPrimary(boxes, splitter, fallback * range.total);
}

/** Integer percentages of the primary pane for `aria-valuenow/min/max`, plus its pixel size. */
export function layoutAria(
  boxes: NleLayoutBoxes,
  splitter: NleSplitterId,
): Readonly<{
  valueNow: number;
  valueMin: number;
  valueMax: number;
  sizePx: number;
}> {
  const range = splitterRange(boxes, splitter);
  const percent = (value: number) => Math.round((value / range.total) * 100);
  return Object.freeze({
    valueNow: percent(range.size),
    valueMin: percent(range.min),
    valueMax: percent(range.max),
    sizePx: range.size,
  });
}
