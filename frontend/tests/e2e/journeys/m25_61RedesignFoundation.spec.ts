/**
 * M25-61 redesign foundation, in the real editor:
 * - A61-1: no periodic line is painted through the tracks at any zoom; the snap guide survives,
 *   and is the probe's own negative: the column test that finds no grid does find the guide.
 * - A61-2: the ruler carries short labels at least 72 px apart and minor ticks down to single
 *   frames, at the fine-pointer ruler height.
 * - A61-3: frame 0 sits one 16 px lead-in right of the track header, for clips, the ruler and the
 *   playhead alike.
 * - A61-4: both pointer modes keep their own control floor.
 */

import { expect, test, type Page } from "@playwright/test";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { canonicalWorkspace, snapshot, surface } from "../helpers/nleCanonical";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";
import {
  nleFineGripException,
  nleLaneOrigin,
  nleTargetFloor,
} from "../helpers/nleTargets";

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

const timeline = (page: Page) =>
  page.locator(surface).locator('[data-h3-nle-region="timeline"]');

async function scale(page: Page): Promise<number> {
  return Number(
    await timeline(page).getAttribute("data-h3-nle-pixels-per-frame"),
  );
}

async function zoomTo(page: Page, position: number) {
  await page
    .locator('[data-h3-nle-control="transport.zoom_continuous"]')
    .fill(String(position));
}

/**
 * Columns of the decoration canvas that are painted over at least 90 % of its height. The old
 * periodic grid was exactly such a column at every ruler mark; clip outlines and waveforms are
 * row-bounded and cannot qualify.
 */
async function fullHeightColumns(page: Page) {
  return page
    .locator('[data-h3-nle-canvas="timeline_decoration"]')
    .evaluate((element) => {
      const canvas = element as HTMLCanvasElement;
      const context = canvas.getContext("2d");
      if (context === null)
        throw new Error("decoration canvas has no 2d context");
      const { width, height } = canvas;
      const pixels = context.getImageData(0, 0, width, height).data;
      const columns: number[] = [];
      for (let x = 0; x < width; x += 1) {
        let painted = 0;
        for (let y = 0; y < height; y += 1)
          if (pixels[(y * width + x) * 4 + 3]! > 0) painted += 1;
        if (painted >= height * 0.9) columns.push(x);
      }
      return { width, height, columns };
    });
}

test("A61-1: no periodic line crosses the tracks at fit, maximum and minimum zoom", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const tracks = page.locator(surface).locator(".h3-nle-tracks");
  await expect(tracks).toBeVisible();
  for (const [label, position] of [
    ["fit", null],
    ["maximum", 1_000],
    ["minimum", 0],
  ] as const) {
    if (position === null)
      await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
    else await zoomTo(page, position);
    await expect
      .poll(async () => (await fullHeightColumns(page)).height, {
        message: `${label} canvas painted`,
      })
      .toBeGreaterThan(40);
    const probe = await fullHeightColumns(page);
    expect(probe.columns, `${label} full-height columns`).toEqual([]);
  }
});

test("A61-1 negative: the same probe sees the snap guide, the one full-height line that remains", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1",
  );
  await page.locator('[data-h3-nle-control="transport.snap"]').click();
  const clip = page
    .locator(surface)
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    );
  const canvas = page.locator('[data-h3-nle-canvas="timeline_decoration"]');
  const box = (await clip.boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  expect((await fullHeightColumns(page)).columns).toEqual([]);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 130, y, { steps: 4 });
  await expect(canvas).toHaveAttribute("data-h3-nle-snap-line", "present");
  await expect
    .poll(async () => (await fullHeightColumns(page)).columns.length)
    .toBeGreaterThan(0);
  const guide = (await fullHeightColumns(page)).columns;
  // One guide, one or two device pixels wide depending on its sub-pixel position.
  expect(guide.at(-1)! - guide[0]!).toBeLessThanOrEqual(2);
  await page.keyboard.press("Escape");
  await page.mouse.up();
  expect(transactions).toHaveLength(0);
});

test("A61-2: the ruler has short labels 72 px apart and single-frame minor ticks at maximum zoom", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const ruler = page.locator(surface).locator(".h3-nle-ruler");
  expect((await ruler.boundingBox())!.height).toBeCloseTo(26, 0);
  for (const position of [null, 1_000] as const) {
    if (position === null)
      await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
    else await zoomTo(page, position);
    const ppf = await scale(page);
    const marks = await ruler
      .locator(".h3-nle-ruler-mark")
      .evaluateAll((elements) =>
        elements.map((element) => ({
          left: element.getBoundingClientRect().left,
          text: element.textContent ?? "",
        })),
      );
    expect(marks.length).toBeGreaterThan(1);
    for (const mark of marks)
      expect(mark.text).toMatch(/^(\d{2}:\d{2}|\d+:\d{2}:\d{2}|\d{2}f)$/);
    for (let index = 1; index < marks.length; index += 1)
      expect(
        marks[index]!.left - marks[index - 1]!.left,
      ).toBeGreaterThanOrEqual(72 - 0.5);
    const minor = await ruler
      .locator("[data-h3-nle-ruler-minor] path")
      .getAttribute("d");
    const xs = [...(minor ?? "").matchAll(/M([0-9.]+) /g)].map((match) =>
      Number(match[1]),
    );
    expect(xs.length, `minor ticks at ${ppf} px/frame`).toBeGreaterThan(0);
    for (let index = 1; index < xs.length; index += 1)
      expect(xs[index]! - xs[index - 1]!).toBeGreaterThanOrEqual(6 - 0.2);
    if (position === 1_000) {
      expect(ppf).toBeCloseTo(16, 6);
      // Between two majors every frame has its own tick: steps are one frame, or two across a major.
      const steps = new Set(
        xs.slice(1).map((x, index) => Math.round((x - xs[index]!) / ppf)),
      );
      expect([...steps].sort()).toEqual([1, 2]);
    }
  }
});

test("A61-3: frame 0 sits one lead-in right of the header for clips, ruler and playhead", async ({
  page,
}) => {
  // Seeking needs the media transport, as in every other ruler-seek journey.
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  const origin = await nleLaneOrigin(page);
  const header = await timeline(page).evaluate((element) =>
    Number.parseFloat(
      getComputedStyle(element).getPropertyValue("--h3-nle-track-header-width"),
    ),
  );
  expect(origin - header).toBe(16);
  const clips = (await snapshot(page)).timelineSnapshot!.clips;
  const first = clips.reduce((earliest, clip) =>
    clip.startFrame < earliest.startFrame ? clip : earliest,
  );
  expect(first.startFrame).toBe(0);
  const lane = page
    .locator(surface)
    .locator(`[data-h3-nle-clip="${first.clipId}"]`)
    .locator("xpath=ancestor::*[contains(@class,'h3-nle-track-lane')][1]");
  const laneBox = (await lane.boundingBox())!;
  const clipBox = (await page
    .locator(surface)
    .locator(`[data-h3-nle-clip="${first.clipId}"]`)
    .boundingBox())!;
  expect(clipBox.x - laneBox.x).toBeCloseTo(16, 0);
  const rulerLane = (await page
    .locator(surface)
    .locator(".h3-nle-ruler-lane")
    .boundingBox())!;
  const zeroMark = (await page
    .locator(surface)
    .locator('.h3-nle-ruler-mark[data-h3-nle-ruler-frame="0"]')
    .boundingBox())!;
  expect(zeroMark.x - rulerLane.x).toBeCloseTo(16, 0);
  expect(zeroMark.x).toBeCloseTo(clipBox.x, 0);
  // No edit is made here: an accepted edit, even a selection, re-acquires the transport, which
  // briefly disables the ruler and returns the playhead to frame 0 (registered against M25-64).
  // Without one, the frame reached below can only come from the press being measured.
  const slider = playheadSlider(page);
  await seekPlayhead(page, slider, 30);
  // The frame holds, so the frame 0 that follows is the lead-in press's own doing.
  await page.waitForTimeout(500);
  expect(await playheadFrame(slider)).toBe(30);
  // A press inside the lead-in band, left of frame 0, clamps to frame 0 rather than missing.
  const sliderBox = (await slider.boundingBox())!;
  await page.mouse.click(rulerLane.x + 6, sliderBox.y + sliderBox.height / 2);
  await expect.poll(() => playheadFrame(slider)).toBe(0);
  const playhead = page.locator(surface).locator(".h3-nle-playhead");
  await expect(playhead).toBeVisible();
  // The ruler reports the requested frame at once; the playhead follows when the frame is
  // presented, on a later animation frame, so its position is polled rather than read once.
  await expect
    .poll(async () => (await playhead.boundingBox())!.x)
    .toBeCloseTo(clipBox.x, 0);
});

test("A61-4: a fine pointer measures controls against the 24 px minimum and a 26 px ruler", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  expect(await page.evaluate(() => matchMedia("(pointer: fine)").matches)).toBe(
    true,
  );
  expect(await nleTargetFloor(page)).toBe(24);
  expect(await nleTargetFloor(page, "icon")).toBe(30);
  const grip = await nleFineGripException(page);
  const small = await page.locator(surface).evaluate(
    (dialog, grip) =>
      [...dialog.querySelectorAll<HTMLElement>("button")]
        .filter((element) => element.getClientRects().length > 0)
        // M25-62 (B-M2562-05): only the pinned 8 px fine grip is excused its width.
        .filter(
          (element) =>
            !(
              grip !== null &&
              element.matches(grip.selector) &&
              Math.round(element.getBoundingClientRect().width) ===
                grip.widthPx &&
              element.getBoundingClientRect().height >= 24
            ),
        )
        .map((element) => element.getBoundingClientRect())
        .filter((box) => box.width < 24 || box.height < 24).length,
    grip,
  );
  expect(small).toBe(0);
});

test.describe("coarse pointer", () => {
  test.use({ hasTouch: true });

  test("A61-4: a coarse pointer keeps the 44 px touch floor, including the ruler", async ({
    page,
  }) => {
    await canonicalWorkspace(page);
    expect(
      await page.evaluate(() => matchMedia("(pointer: coarse)").matches),
    ).toBe(true);
    expect(await nleTargetFloor(page)).toBe(44);
    const ruler = page.locator(surface).locator(".h3-nle-ruler");
    expect((await ruler.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    const small = await page.locator(surface).evaluate(
      (dialog) =>
        [...dialog.querySelectorAll<HTMLElement>("button")]
          .filter(
            (element) =>
              element.getClientRects().length > 0 &&
              !element.classList.contains("h3-nle-clip-body"),
          )
          .map((element) => element.getBoundingClientRect())
          .filter((box) => box.width < 44 || box.height < 44).length,
    );
    expect(small).toBe(0);
  });
});
