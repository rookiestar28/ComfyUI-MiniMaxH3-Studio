import { expect, test, type Page } from "@playwright/test";
import { nleTargetFloor } from "../helpers/nleTargets";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { canonicalWorkspace, snapshot, surface } from "../helpers/nleCanonical";
import { playheadSlider, seekPlayhead } from "../helpers/nleTimeline";

test.use({ viewport: { width: 720, height: 900 }, deviceScaleFactor: 1 });

async function receipts(page: Page, expected: number) {
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(expected);
}

test("M25-47 toolbar, shortcuts, ripple and roll share one accepted timeline path", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1",
  );

  const overlay = page.locator(surface);
  const toolbar = overlay.getByRole("toolbar", {
    name: "Timeline tools",
  });
  const toolbarBox = await toolbar.boundingBox();
  const overlayBox = await overlay.boundingBox();
  expect(toolbarBox).not.toBeNull();
  expect(overlayBox).not.toBeNull();
  expect(toolbarBox!.width).toBeLessThanOrEqual(overlayBox!.width);
  const targets = toolbar.locator("[data-h3-nle-toolbar-item]");
  expect(await targets.count()).toBe(12);
  const targetBoxes = await targets.evaluateAll((elements) =>
    elements.map((element) => {
      const box = element.getBoundingClientRect();
      return { top: Math.round(box.top), width: box.width, height: box.height };
    }),
  );
  expect([...new Set(targetBoxes.map((box) => box.top))]).toHaveLength(1);
  // M25-61: the fine-pointer floor comes from the contract, not a 44 px literal.
  const floor = await nleTargetFloor(page);
  expect(
    targetBoxes.every((box) => box.width >= floor && box.height >= floor),
  ).toBe(true);
  await overlay.locator('[data-h3-nle-control="toolbar.more"]').click();
  const menu = overlay.getByRole("menu", { name: "More timeline tools" });
  await expect(menu).toBeVisible();
  const menuBox = await menu.boundingBox();
  expect(menuBox).not.toBeNull();
  expect(menuBox!.x).toBeGreaterThanOrEqual(overlayBox!.x);
  expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(
    overlayBox!.x + overlayBox!.width + 1,
  );
  await menu.locator('[data-h3-nle-control="selection.next"]').focus();
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await expect(
    overlay.locator('[data-h3-nle-control="toolbar.more"]'),
  ).toBeFocused();

  await overlay
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await receipts(page, 1);
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await seekPlayhead(page, ruler, 12);
  await page.evaluate(() => {
    (window as Window & { __m2547ForeignQ?: number }).__m2547ForeignQ = 0;
    window.addEventListener("keydown", (event) => {
      if (event.key.toLowerCase() === "q")
        (window as Window & { __m2547ForeignQ?: number }).__m2547ForeignQ! += 1;
    });
  });
  await overlay
    .getByRole("grid", { name: "Timeline tracks", exact: true })
    .focus();
  await page.keyboard.press("q");
  await receipts(page, 2);
  expect(
    await page.evaluate(
      () => (window as Window & { __m2547ForeignQ?: number }).__m2547ForeignQ,
    ),
  ).toBe(0);
  expect(
    (transactions.at(-1) as { commands: { kind: string }[] }).commands[0]!.kind,
  ).toBe("trim_clip");
  await overlay.locator('[data-h3-nle-control="history.undo"]').click();
  await receipts(page, 3);

  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await seekPlayhead(page, ruler, 12);
  const split = overlay.locator('[data-h3-nle-control="clip.split"]');
  await expect(split).not.toHaveAttribute("aria-disabled", "true");
  await split.click();
  await receipts(page, 4);
  let timeline = (await snapshot(page)).timelineSnapshot!;
  const right = timeline.clips.find(
    (clip) =>
      clip.trackId === "track-0" &&
      clip.startFrame === 12 &&
      clip.clipId !== "clip-0",
  )!;
  expect(timeline.clips.find((clip) => clip.clipId === "clip-0")).toMatchObject(
    { startFrame: 0, sourceStartFrame: 0, durationFrames: 12 },
  );
  expect(right).toMatchObject({ sourceStartFrame: 12, durationFrames: 36 });

  await overlay.locator('[data-h3-nle-control="history.undo"]').click();
  await receipts(page, 5);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.clipId === "clip-0",
    ),
  ).toMatchObject({ durationFrames: 48 });
  await overlay.locator('[data-h3-nle-control="history.redo"]').click();
  await receipts(page, 6);

  const cut = overlay.locator(
    `[data-h3-nle-control="boundary.roll"][data-h3-nle-roll-left="clip-0"][data-h3-nle-roll-right="${right.clipId}"]`,
  );
  const cutBox = await cut.boundingBox();
  expect(cutBox).not.toBeNull();
  const pixelsPerFrame = Number(
    await overlay
      .locator('[data-h3-nle-region="timeline"]')
      .getAttribute("data-h3-nle-pixels-per-frame"),
  );
  const rollPixels = pixelsPerFrame * 35;
  await page.mouse.move(
    cutBox!.x + cutBox!.width / 2,
    cutBox!.y + cutBox!.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(
    cutBox!.x + cutBox!.width / 2 + rollPixels,
    cutBox!.y + cutBox!.height / 2,
    { steps: 3 },
  );
  await expect(cut).toHaveAttribute("data-admitted", "true");
  await page.mouse.up();
  await receipts(page, 7);
  timeline = (await snapshot(page)).timelineSnapshot!;
  expect(timeline.clips.find((clip) => clip.clipId === "clip-0")).toMatchObject(
    { durationFrames: 47 },
  );
  expect(
    timeline.clips.find((clip) => clip.clipId === right.clipId),
  ).toMatchObject({ startFrame: 47, durationFrames: 1 });
  await overlay.locator('[data-h3-nle-control="history.undo"]').click();
  await receipts(page, 8);

  const beforeEditable = transactions.length;
  // M25-50 moved editable clip properties into Inspector tabs. The old Delta frames field no
  // longer exists; an active Basic numeric draft must still shield Delete from timeline tools.
  const input = overlay.getByRole("spinbutton", {
    name: "Position X (%)",
    exact: true,
  });
  await input.focus();
  await page.keyboard.press("Delete");
  expect(transactions).toHaveLength(beforeEditable);

  await overlay.locator('[data-h3-nle-control="transport.ripple"]').click();
  await overlay.locator('[data-h3-nle-control="range.ripple_delete"]').click();
  await receipts(page, 9);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.some(
      (clip) => clip.clipId === "clip-0",
    ),
  ).toBe(false);
  await overlay.locator('[data-h3-nle-control="history.undo"]').click();
  await receipts(page, 10);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.some(
      (clip) => clip.clipId === "clip-0",
    ),
  ).toBe(true);
});
