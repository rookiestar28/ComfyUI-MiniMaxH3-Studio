import { expect, test, type Page } from "@playwright/test";
import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { canonicalWorkspace, snapshot, surface } from "../helpers/nleCanonical";
import { playheadSlider, seekPlayhead } from "../helpers/nleTimeline";

test.use({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });

const editKeys = [
  { key: "Delete", kind: "remove_clip" },
  { key: "Shift+Delete", kind: "ripple_delete" },
  { key: "Control+b", kind: "split_clip" },
  { key: "q", kind: "trim_clip" },
  { key: "w", kind: "trim_clip" },
  { key: "Control+z", kind: "undo" },
  { key: "Control+Shift+z", kind: "redo" },
  { key: "Control+a", kind: "select_clips" },
] as const;
type ButtonTarget = "clip" | "toolbar" | "grip" | "roll";

async function buttonWorkspace(page: Page) {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    (wire) => {
      const clips = wire.clips as Record<string, unknown>[];
      clips.find((clip) => clip.clip_id === "clip-4")!.start_frame = 48;
    },
    undefined,
    "&media=1",
  );
  const overlay = page.locator(surface);
  const region = overlay.locator('[data-h3-nle-region="timeline"]');
  const clip = overlay.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await seekPlayhead(page, playheadSlider(page), 12);
  await clip.click();
  await expect(clip).toBeFocused();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await page.evaluate(() => {
    (
      window as Window & { foreignTimelineKeys?: string[] }
    ).foreignTimelineKeys = [];
    window.addEventListener("keydown", (event) => {
      if (
        ["Delete", "b", "q", "w", "z", "a", "=", "-", "+", ",", "."].includes(
          event.key,
        )
      )
        (
          window as Window & { foreignTimelineKeys?: string[] }
        ).foreignTimelineKeys!.push(event.key);
    });
  });
  const target = (name: ButtonTarget) =>
    name === "clip"
      ? clip
      : name === "toolbar"
        ? overlay.locator('[data-h3-nle-control="transport.zoom_in"]')
        : name === "grip"
          ? overlay.locator(
              '[data-h3-nle-trim-clip="clip-0"] [data-h3-nle-trim-edge="end"]:not([hidden])',
            )
          : overlay.locator(
              '[data-h3-nle-control="boundary.roll"][data-h3-nle-roll-left="clip-0"]:not([hidden])',
            );
  const foreignKeys = () =>
    page.evaluate(
      () =>
        (window as Window & { foreignTimelineKeys?: string[] })
          .foreignTimelineKeys,
    );
  return { transactions, overlay, region, clip, target, foreignKeys };
}

test("split shortcut works after a real clip press without leaking to window", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1",
  );
  const overlay = page.locator(surface);
  const clip = overlay.locator(
    '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
  );
  await seekPlayhead(page, playheadSlider(page), 12);
  await page.evaluate(() => {
    (window as Window & { foreignTimelineKeys?: number }).foreignTimelineKeys =
      0;
    window.addEventListener("keydown", (event) => {
      if (event.key.toLowerCase() === "b")
        (
          window as Window & { foreignTimelineKeys?: number }
        ).foreignTimelineKeys! += 1;
    });
  });
  await clip.click();
  await expect(clip).toBeFocused();
  await page.keyboard.press("Control+b");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  expect(
    (transactions.at(-1) as { commands: { kind: string }[] }).commands[0]!.kind,
  ).toBe("split_clip");
  const clips = (await snapshot(page)).timelineSnapshot!.clips;
  expect(clips.find((member) => member.clipId === "clip-0")).toMatchObject({
    durationFrames: 12,
  });
  expect(
    clips.some(
      (member) =>
        member.trackId === "track-0" &&
        member.startFrame === 12 &&
        member.durationFrames === 36,
    ),
  ).toBe(true);
  expect(
    await page.evaluate(
      () =>
        (window as Window & { foreignTimelineKeys?: number })
          .foreignTimelineKeys,
    ),
  ).toBe(0);
});

for (const name of ["clip", "toolbar", "grip", "roll"] as const) {
  for (const { key, kind } of editKeys) {
    test(`${key} acts after a real ${name} button press`, async ({ page }) => {
      const subject = await buttonWorkspace(page);
      if (kind === "undo" || kind === "redo") {
        await subject.overlay
          .locator('[data-h3-nle-control="clip.trim_start_playhead"]')
          .click();
        await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
        if (kind === "redo") {
          await subject.overlay
            .locator('[data-h3-nle-control="history.undo"]')
            .click();
          await expect
            .poll(async () => (await snapshot(page)).receipts)
            .toBe(3);
        }
      }
      const target = subject.target(name);
      await target.click();
      await expect(target).toBeFocused();
      const before = (await snapshot(page)).receipts;
      await page.keyboard.press(key);
      await expect
        .poll(async () => (await snapshot(page)).receipts)
        .toBe(before + 1);
      const command = (
        subject.transactions.at(-1) as {
          commands: { kind: string; payload: Record<string, unknown> }[];
        }
      ).commands[0]!;
      expect(command.kind).toBe(kind);
      const clips = (await snapshot(page)).timelineSnapshot!.clips;
      const first = clips.find((member) => member.clipId === "clip-0");
      if (kind === "remove_clip" || kind === "ripple_delete")
        expect(first).toBeUndefined();
      else if (kind === "split_clip" || key === "w")
        expect(first).toMatchObject({ startFrame: 0, durationFrames: 12 });
      else if (key === "q" || kind === "redo")
        expect(first).toMatchObject({
          startFrame: 12,
          durationFrames: 36,
          sourceStartFrame: 12,
        });
      else if (kind === "undo")
        expect(first).toMatchObject({
          startFrame: 0,
          durationFrames: 48,
          sourceStartFrame: 0,
        });
      else
        expect(new Set(command.payload.clip_ids as string[])).toEqual(
          new Set(clips.map((member) => member.clipId)),
        );
      expect(await subject.foreignKeys()).toEqual([]);
    });
  }

  test(`zoom and fit act after a real ${name} button press`, async ({
    page,
  }) => {
    const subject = await buttonWorkspace(page);
    const target = subject.target(name);
    await target.click();
    await expect(target).toBeFocused();
    const receipts = (await snapshot(page)).receipts;
    const scale = () =>
      subject.region.getAttribute("data-h3-nle-pixels-per-frame").then(Number);
    const before = await scale();
    await page.keyboard.press("Control+=");
    await expect.poll(scale).toBeGreaterThan(before);
    await page.keyboard.press("Control+-");
    await expect.poll(scale).toBe(before);
    await page.keyboard.press("Shift+z");
    await expect.poll(scale).toBeGreaterThan(0);
    expect((await snapshot(page)).receipts).toBe(receipts);
    expect(await subject.foreignKeys()).toEqual([]);
  });
}

for (const name of ["clip", "grip", "roll"] as const) {
  test(`${name} keyboard draft keeps shortcuts inert until Escape`, async ({
    page,
  }) => {
    const subject = await buttonWorkspace(page);
    const target = subject.target(name);
    await target.click();
    await expect(target).toBeFocused();
    await page.keyboard.press(name === "clip" ? "Enter" : "ArrowRight");
    const before = (await snapshot(page)).receipts;
    const scale = await subject.region.getAttribute(
      "data-h3-nle-pixels-per-frame",
    );
    for (const { key } of editKeys) await page.keyboard.press(key);
    await page.keyboard.press("Control+=");
    await page.keyboard.press("Shift+z");
    expect((await snapshot(page)).receipts).toBe(before);
    expect(
      await subject.region.getAttribute("data-h3-nle-pixels-per-frame"),
    ).toBe(scale);
    expect(await subject.foreignKeys()).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(subject.overlay).toBeVisible();
    expect((await snapshot(page)).receipts).toBe(before);
  });
}

test("native activation and non-timeline key ownership survive", async ({
  page,
}) => {
  const subject = await buttonWorkspace(page);
  await page.keyboard.press("Space");
  expect((await snapshot(page)).receipts).toBe(1);
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Escape");
  expect((await snapshot(page)).receipts).toBe(1);
  const zoom = subject.target("toolbar");
  await zoom.click();
  await expect(zoom).toBeFocused();
  const before = Number(
    await subject.region.getAttribute("data-h3-nle-pixels-per-frame"),
  );
  await page.keyboard.press("Space");
  await expect
    .poll(() =>
      subject.region.getAttribute("data-h3-nle-pixels-per-frame").then(Number),
    )
    .toBe(before * 2);
  expect((await snapshot(page)).receipts).toBe(1);
  const inspector = subject.overlay.getByRole("spinbutton", {
    name: "Position X (%)",
    exact: true,
  });
  await inspector.focus();
  for (const { key } of editKeys) await page.keyboard.press(key);
  expect((await snapshot(page)).receipts).toBe(1);
  const bin = subject.overlay.locator('[data-h3-nle-pane="assets"]');
  await bin.click();
  await expect(bin).toBeFocused();
  for (const { key } of editKeys) await page.keyboard.press(key);
  expect((await snapshot(page)).receipts).toBe(1);
});
