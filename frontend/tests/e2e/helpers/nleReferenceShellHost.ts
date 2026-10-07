// M25-44 reference-shell observations shared by the supplied-host NLE journeys: the four areas in
// the reference proportions, three splitters moved by a real pointer drag, and the narrow tier.
import { expect, type Locator, type Page } from "@playwright/test";

type ShellBox = Readonly<{
  x: number;
  y: number;
  width: number;
  height: number;
}>;

export type ReferenceShellMeasurement = Readonly<{
  tier: string | null;
  areas: number;
  separators: number;
  stageWidth: number;
  stageHeight: number;
  overflowX: number;
  bin: ShellBox;
  monitor: ShellBox;
  inspector: ShellBox;
  timeline: ShellBox;
}>;

type SplitterId = "bin_monitor" | "monitor_inspector" | "top_timeline";

/** The reference shell's areas, splitters and stage, read from the live layout. */
export function referenceShell(
  overlay: Locator,
): Promise<ReferenceShellMeasurement> {
  return overlay.evaluate((dialog) => {
    const box = (id: string) => {
      const rect = dialog
        .querySelector(`[data-h3-nle-area="${id}"]`)!
        .getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    const stage = dialog.querySelector<HTMLElement>(".h3-nle-stage")!;
    return {
      tier: dialog
        .querySelector("[data-h3-nle-shell]")!
        .getAttribute("data-h3-nle-tier"),
      areas: dialog.querySelectorAll("[data-h3-nle-area]").length,
      separators: dialog.querySelectorAll('[role="separator"]').length,
      stageWidth: stage.clientWidth,
      stageHeight: stage.clientHeight,
      overflowX: stage.scrollWidth - stage.clientWidth,
      bin: box("bin"),
      monitor: box("monitor"),
      inspector: box("inspector"),
      timeline: box("timeline"),
    };
  });
}

/** A real mouse drag of one splitter along its axis. */
async function dragSplitter(
  page: Page,
  overlay: Locator,
  id: SplitterId,
  delta: number,
): Promise<void> {
  const box = (await overlay
    .locator(`[data-h3-nle-splitter="${id}"]`)
    .boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  const vertical = id !== "top_timeline";
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(vertical ? x + delta : x, vertical ? y : y + delta, {
    steps: 6,
  });
  await page.mouse.up();
}

/**
 * The standard tier in the reference proportions (within 2 percentage points), then each
 * splitter dragged +60 px and back with the neighbour box following within 1 px. The layout is
 * left where it started, so the journey that called this continues on an unchanged shell.
 */
export async function expectStandardReferenceShell(
  page: Page,
  overlay: Locator,
): Promise<
  Readonly<{
    standard: ReferenceShellMeasurement;
    splitterMoves: Readonly<Record<SplitterId, number>>;
  }>
> {
  const standard = await referenceShell(overlay);
  expect(standard).toMatchObject({
    tier: "standard",
    areas: 4,
    separators: 3,
    overflowX: 0,
  });
  const share = (value: number, total: number) => (value / total) * 100;
  expect(
    Math.abs(share(standard.bin.width, standard.stageWidth) - 21.0),
  ).toBeLessThanOrEqual(2);
  expect(
    Math.abs(share(standard.monitor.width, standard.stageWidth) - 52.5),
  ).toBeLessThanOrEqual(2);
  expect(
    Math.abs(share(standard.inspector.width, standard.stageWidth) - 25.6),
  ).toBeLessThanOrEqual(2);
  expect(
    Math.abs(share(standard.bin.height, standard.stageHeight) - 62.2),
  ).toBeLessThanOrEqual(2);
  const splitterMoves = {} as Record<SplitterId, number>;
  for (const [id, measure] of [
    ["bin_monitor", (value: ReferenceShellMeasurement) => value.bin.width],
    [
      "monitor_inspector",
      (value: ReferenceShellMeasurement) => -value.inspector.width,
    ],
    ["top_timeline", (value: ReferenceShellMeasurement) => value.bin.height],
  ] as const) {
    const before = measure(await referenceShell(overlay));
    await dragSplitter(page, overlay, id, 60);
    const moved = measure(await referenceShell(overlay)) - before;
    splitterMoves[id] = Math.round(moved);
    expect(Math.abs(moved - 60)).toBeLessThanOrEqual(1);
    await dragSplitter(page, overlay, id, -60);
    expect(
      Math.abs(measure(await referenceShell(overlay)) - before),
    ).toBeLessThanOrEqual(1);
  }
  return { standard, splitterMoves };
}

/** The narrow tier: every area at or above its minimum and no horizontal stage overflow. */
export async function expectNarrowReferenceShell(
  overlay: Locator,
): Promise<ReferenceShellMeasurement> {
  const narrow = await referenceShell(overlay);
  expect(narrow).toMatchObject({ tier: "narrow", areas: 4, separators: 3 });
  expect(narrow.overflowX).toBeLessThanOrEqual(0);
  expect(narrow.bin.width).toBeGreaterThanOrEqual(159.5);
  expect(narrow.monitor.width).toBeGreaterThanOrEqual(279.5);
  expect(narrow.inspector.width).toBeGreaterThanOrEqual(239.5);
  expect(narrow.bin.height).toBeGreaterThanOrEqual(239.5);
  expect(narrow.timeline.height).toBeGreaterThanOrEqual(159.5);
  return narrow;
}

/** Rounded area sizes for the retained host observation. */
export function summarizeReferenceShell(value: ReferenceShellMeasurement) {
  return {
    tier: value.tier,
    stage: [value.stageWidth, value.stageHeight],
    bin: Math.round(value.bin.width),
    monitor: Math.round(value.monitor.width),
    inspector: Math.round(value.inspector.width),
    top: Math.round(value.bin.height),
    timeline: Math.round(value.timeline.height),
  };
}
