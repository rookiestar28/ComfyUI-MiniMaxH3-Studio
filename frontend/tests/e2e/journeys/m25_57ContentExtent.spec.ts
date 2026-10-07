import { expect, test, type Page } from "@playwright/test";

import type {} from "../../../e2e/nleWorkspace";

async function harnessSnapshot(page: Page) {
  return page.evaluate(() => window.nleWorkspaceHarness.snapshot());
}

async function expectContentExtent(
  page: Page,
  frames: number,
  timecode: string,
  lastFrame: number | null,
) {
  await expect
    .poll(
      async () => (await harnessSnapshot(page)).authoringV2ContentEndExclusive,
    )
    .toBe(frames);
  const timeline = (await harnessSnapshot(page)).timelineSnapshot;
  if (frames === 0) {
    expect(timeline).toBeNull();
    await expect(page.getByRole("region", { name: "Timeline" })).toContainText(
      "Edit capacity 00:02:30:00",
    );
    await expect(page.getByRole("slider", { name: "Playhead" })).toHaveCount(0);
  } else {
    expect(timeline?.output.durationFrames).toBe(frames);
    await expect(page.getByRole("row")).toHaveAccessibleName(
      new RegExp(timecode),
    );
    await expect(
      page.getByRole("slider", { name: "Playhead" }),
    ).toHaveAttribute("aria-valuemax", String(lastFrame));
  }
}

test("M25-57 V2 empty state, content extent, and history stay independent of V1", async ({
  page,
}) => {
  await page.goto("/nleWorkspace.html?authoringV2=1&frames=120");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expectContentExtent(page, 0, "00:00:00:00", null);

  await page
    .getByRole("button", { name: "Add Clip 01 to the timeline" })
    .click();
  await expectContentExtent(page, 120, "00:00:05:00", 119);
  expect((await harnessSnapshot(page)).timelineSnapshot?.clips).toHaveLength(1);

  await page
    .getByRole("button", { name: "Add Clip 02 to the timeline" })
    .click();
  await expectContentExtent(page, 240, "00:00:10:00", 239);
  expect((await harnessSnapshot(page)).timelineSnapshot?.clips).toHaveLength(2);

  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expectContentExtent(page, 120, "00:00:05:00", 119);
  await page.locator('[data-h3-nle-control="history.redo"]').click();
  await expectContentExtent(page, 240, "00:00:10:00", 239);

  let clips = (await harnessSnapshot(page)).timelineSnapshot!.clips;
  const secondClip = clips.at(-1)!;
  await page
    .locator(
      `[data-h3-nle-clip="${secondClip.clipId}"] [data-h3-nle-control="selection.set"]`,
    )
    .click();
  await expect
    .poll(async () => (await harnessSnapshot(page)).authoringV2Selection)
    .toEqual([secondClip.clipId]);
  await page.locator('[data-h3-nle-control="clip.remove"]').click();
  await expectContentExtent(page, 120, "00:00:05:00", 119);
  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expectContentExtent(page, 240, "00:00:10:00", 239);
  await page.locator('[data-h3-nle-control="history.redo"]').click();
  await expectContentExtent(page, 120, "00:00:05:00", 119);

  clips = (await harnessSnapshot(page)).timelineSnapshot!.clips;
  const remainingClip = clips[0]!;
  await page
    .locator(
      `[data-h3-nle-clip="${remainingClip.clipId}"] [data-h3-nle-control="selection.set"]`,
    )
    .click();
  await expect
    .poll(async () => (await harnessSnapshot(page)).authoringV2Selection)
    .toEqual([remainingClip.clipId]);
  await page.locator('[data-h3-nle-control="clip.remove"]').click();
  await expectContentExtent(page, 0, "00:00:00:00", null);
  await page
    .getByRole("button", { name: "Add Clip 02 to the timeline" })
    .click();
  await expectContentExtent(page, 120, "00:00:05:00", 119);
  expect((await harnessSnapshot(page)).timelineSnapshot?.clips).toHaveLength(1);
  const insertCount = (await harnessSnapshot(page)).intents
    .flatMap(({ commands }) => commands)
    .filter(({ kind }) => kind === "insert_asset_clip").length;
  expect(insertCount).toBe(3);
});

test("M25-57 real 124-frame source placements end at frame 247", async ({
  page,
}) => {
  await page.goto("/nleWorkspace.html?authoringV2=1&frames=124");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expectContentExtent(page, 0, "00:00:00:00", null);
  await page
    .getByRole("button", { name: "Add Clip 01 to the timeline" })
    .click();
  await expectContentExtent(page, 124, "00:00:05:04", 123);
  // Primary Add is an append operation: the logical playhead must not become its insertion point.
  const playhead = page.getByRole("slider", { name: "Playhead" });
  await playhead.focus();
  await page.keyboard.press("Home");
  await expect(playhead).toHaveAttribute("aria-valuenow", "0");
  await page
    .getByRole("button", { name: "Add Clip 02 to the timeline" })
    .click();
  await expectContentExtent(page, 248, "00:00:10:08", 247);
  const clips = (await harnessSnapshot(page)).timelineSnapshot!.clips;
  expect(
    clips.map(({ startFrame, durationFrames }) => [startFrame, durationFrames]),
  ).toEqual([
    [0, 124],
    [124, 124],
  ]);
});

test("content Fit and native lane wheel keep one viewport transform", async ({
  page,
}) => {
  await page.goto("/nleWorkspace.html?authoringV2=1&frames=124");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await page
    .getByRole("button", { name: "Add Clip 01 to the timeline" })
    .click();
  await page
    .getByRole("button", { name: "Add Clip 02 to the timeline" })
    .click();
  await expectContentExtent(page, 248, "00:00:10:08", 247);

  const timeline = page.locator('[data-h3-nle-region="timeline"]');
  const grid = page.getByRole("grid");
  const fit = page.locator('[data-h3-nle-control="transport.zoom_fit"]');
  await fit.click();
  const clips = (await harnessSnapshot(page)).timelineSnapshot!.clips;
  const last = page.locator(`[data-h3-nle-clip="${clips.at(-1)!.clipId}"]`);
  const lane = page.locator(
    '[data-h3-nle-track][data-kind="primary_video"] .h3-nle-track-lane',
  );
  await expect(last).toBeVisible();
  const [laneBox, clipBox] = await Promise.all([
    lane.boundingBox(),
    last.boundingBox(),
  ]);
  expect(laneBox).not.toBeNull();
  expect(clipBox).not.toBeNull();
  expect(
    laneBox!.x + laneBox!.width - (clipBox!.x + clipBox!.width),
  ).toBeCloseTo(24, 0);
  await expect(timeline).toHaveAttribute("data-h3-nle-view-start", "0");

  const gridBox = await grid.boundingBox();
  if (gridBox === null) throw new Error("timeline grid is not rendered");
  await page.mouse.move(
    gridBox.x + gridBox.width * 0.65,
    gridBox.y + gridBox.height * 0.5,
  );
  const before = await timeline.evaluate((element) => ({
    scale: Number((element as HTMLElement).dataset.h3NlePixelsPerFrame),
    dpr: window.devicePixelRatio,
    outerScroll: window.scrollY,
  }));
  await page.keyboard.down("Control");
  await page.mouse.wheel(0, -120);
  await page.keyboard.up("Control");
  await expect
    .poll(async () =>
      Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
    )
    .toBeGreaterThan(before.scale);
  expect(await page.evaluate(() => window.devicePixelRatio)).toBe(before.dpr);
  expect(await page.evaluate(() => window.scrollY)).toBe(before.outerScroll);

  const beforePan = Number(
    await timeline.getAttribute("data-h3-nle-view-start"),
  );
  await page.keyboard.down("Shift");
  await page.mouse.wheel(0, 120);
  await page.keyboard.up("Shift");
  await expect
    .poll(async () =>
      Number(await timeline.getAttribute("data-h3-nle-view-start")),
    )
    .toBeGreaterThan(beforePan);

  // Fit is deterministic after manual view changes and remains authored-content based.
  await fit.click();
  await expect(timeline).toHaveAttribute("data-h3-nle-view-start", "0");
  const restored = await last.boundingBox();
  expect(restored).not.toBeNull();
  expect(
    laneBox!.x + laneBox!.width - (restored!.x + restored!.width),
  ).toBeCloseTo(24, 0);
});

test("primary Add refuses capacity overflow without partial mutation", async ({
  page,
}) => {
  await page.goto("/nleWorkspace.html?authoringV2=1&frames=450");
  await page.getByRole("button", { name: "Open full editor" }).click();
  const add = page.getByRole("button", { name: "Add Clip 01 to the timeline" });
  for (let index = 0; index < 8; index += 1) {
    await page
      .getByRole("button", {
        name: `Add Clip 0${(index % 2) + 1} to the timeline`,
      })
      .click();
    await expect
      .poll(
        async () =>
          (await harnessSnapshot(page)).authoringV2ContentEndExclusive,
      )
      .toBe((index + 1) * 450);
  }
  expect(
    (await harnessSnapshot(page)).timelineSnapshot?.output.durationFrames,
  ).toBe(3600);
  await expect(page.getByRole("slider", { name: "Playhead" })).toHaveAttribute(
    "aria-valuemax",
    "3599",
  );
  const before = await harnessSnapshot(page);
  await add.click();
  await expect(page.locator("[data-h3-nle-add-notice]")).toContainText(
    "remaining timeline capacity is too short",
  );
  const after = await harnessSnapshot(page);
  expect(after.timelineSnapshot?.clips).toEqual(before.timelineSnapshot?.clips);
  expect(after.intents).toHaveLength(before.intents.length);
});
