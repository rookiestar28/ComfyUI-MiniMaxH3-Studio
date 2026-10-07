import { expect, test, type Page } from "@playwright/test";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { canonicalWorkspace, snapshot } from "../helpers/nleCanonical";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";

const clipBody = (page: Page, clipId: string) =>
  page.locator(
    `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
  );

test("preview failure keeps authoring navigation and double-click owns one exact seek", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const monitor = page.locator('[data-h3-nle-status="monitor"]');
  await expect(monitor).toContainText("unavailable", { timeout: 30_000 });
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  expect(await playheadFrame(ruler)).toBe(0);

  const body = clipBody(page, "clip-0");
  await body.click({ position: { x: 7, y: 8 } });
  expect(await playheadFrame(ruler)).toBe(0);
  await body.dblclick({ position: { x: 12, y: 8 } });
  await expect.poll(() => playheadFrame(ruler)).toBe(13);

  await body.dblclick({
    position: { x: 18, y: 8 },
    modifiers: ["Shift"],
  });
  expect(await playheadFrame(ruler)).toBe(13);

  const box = await body.boundingBox();
  if (box === null) throw new Error("clip body geometry unavailable");
  await page.mouse.move(box.x + 8, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 20, box.y + box.height / 2, { steps: 3 });
  await page.mouse.up();
  expect(await playheadFrame(ruler)).toBe(13);
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeDisabled();
});

test("empty content has origin zero and shrink clamps the logical position once", async ({
  page,
}) => {
  await page.goto("/nleWorkspace.html?authoringV2=1&frames=120");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator('[data-h3-nle-empty-origin="0"]')).toBeVisible();

  await page
    .getByRole("button", { name: "Add Clip 01 to the timeline" })
    .click();
  await page
    .getByRole("button", { name: "Add Clip 02 to the timeline" })
    .click();
  const ruler = playheadSlider(page);
  await ruler.press("End");
  await expect.poll(() => playheadFrame(ruler)).toBe(239);

  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expect(ruler).toHaveAttribute("aria-valuemax", "119");
  await expect.poll(() => playheadFrame(ruler)).toBe(119);
});

test("a rebind first-paints the kept frame without a bootstrap frame-zero commit", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  const monitor = page.locator('[data-h3-nle-status="monitor"]');
  await expect(monitor).toHaveText("Monitor paused.", { timeout: 30_000 });
  const ruler = playheadSlider(page);
  await seekPlayhead(page, ruler, 30);
  const canvas = page.locator('[data-h3-nle-canvas="composition"]');
  await expect(canvas).toHaveAttribute("data-h3-nle-presented-frame", "30");
  await canvas.evaluate((element) => {
    const seen: string[] = [];
    (window as unknown as { __m2555Paints: string[] }).__m2555Paints = seen;
    new MutationObserver(() => {
      const frame = element.getAttribute("data-h3-nle-presented-frame");
      if (frame !== null) seen.push(frame);
    }).observe(element, {
      attributes: true,
      attributeFilter: ["data-h3-nle-presented-frame"],
    });
  });

  await clipBody(page, "clip-2").focus();
  await page.keyboard.press("Space");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await expect(monitor).toHaveText("Monitor paused.", { timeout: 30_000 });
  await expect(canvas).toHaveAttribute("data-h3-nle-presented-frame", "30");
  const paints = await page.evaluate(
    () => (window as unknown as { __m2555Paints: string[] }).__m2555Paints,
  );
  expect(paints).not.toContain("0");
});
