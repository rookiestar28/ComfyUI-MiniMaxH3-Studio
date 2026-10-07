import { expect, test } from "@playwright/test";

import type { VisualCompositionHarnessSnapshot } from "../../../e2e/visualCompositionPreview";

declare global {
  interface Window {
    visualCompositionPreviewHarness: Readonly<{
      snapshot(): VisualCompositionHarnessSnapshot;
    }>;
  }
}

const metrics = (page: import("@playwright/test").Page) =>
  page.evaluate(() => window.visualCompositionPreviewHarness.snapshot());

test("real preview controls preserve one canvas and clean the synthetic session boundary", async ({
  page,
}) => {
  await page.goto("/visualCompositionPreview.html");
  await page.waitForFunction(
    () => window.visualCompositionPreviewHarness?.snapshot().ready === true,
  );

  const status = page.getByRole("status").filter({
    hasText: /Composition preview (paused|playing|unavailable)/,
  });
  const output = page.locator("output");
  await expect(status).toHaveText("Composition preview paused.");
  await expect(output).toHaveText("Frame 0 at 0.000 seconds");
  await expect(page.locator("canvas")).toHaveCount(1);
  await expect(
    page.getByText(/does not qualify media decoding/i),
  ).toBeVisible();
  expect((await metrics(page)).resources).toEqual({
    imageOwners: 1,
    fontOwners: 1,
    videoOwners: 2,
    staticLeases: 2,
    pendingOperations: 0,
    blobBytes: 96,
  });

  await page.getByRole("button", { name: "Next frame" }).click();
  await expect(output).toHaveText("Frame 1 at 0.042 seconds");
  await page.getByRole("button", { name: "Previous frame" }).click();
  await expect(output).toHaveText("Frame 0 at 0.000 seconds");

  const seek = page.getByRole("slider", { name: "Seek frame" });
  await seek.fill("17");
  await expect(output).toHaveText("Frame 17 at 0.708 seconds");
  expect((await metrics(page)).operations.seek).toBe(1);

  await page.getByRole("button", { name: "Play" }).click();
  await expect(status).toHaveText("Composition preview playing.");
  await expect(page.getByRole("button", { name: "Pause" })).toBeEnabled();
  await page.getByRole("button", { name: "Pause" }).click();
  await expect(status).toHaveText("Composition preview paused.");

  const rendersBeforeBurst = (await metrics(page)).reactRenderCount;
  await page.getByRole("button", { name: "Publish frame burst" }).click();
  await expect(output).toHaveText("Frame 31 at 1.292 seconds");
  expect((await metrics(page)).reactRenderCount).toBe(rendersBeforeBurst);
  const pixel = await page
    .locator("canvas")
    .evaluate((canvas) =>
      Array.from(
        (canvas as HTMLCanvasElement)
          .getContext("2d", { willReadFrequently: true })!
          .getImageData(0, 0, 1, 1).data,
      ),
    );
  expect(pixel).toEqual([
    (31 * 17) % 256,
    (31 * 29) % 256,
    (31 * 43) % 256,
    255,
  ]);

  await page.getByRole("button", { name: "Simulate source loss" }).click();
  await expect(status).toHaveText(
    "Composition preview unavailable: source unavailable.",
  );
  await expect(output).toHaveText("Frame unavailable");
  await page.getByRole("button", { name: "Recover" }).click();
  await expect(status).toHaveText("Composition preview paused.");
  await expect(output).toHaveText("Frame 31 at 1.292 seconds");

  const fingerprintBefore = (await metrics(page)).publicFingerprint;
  await page.getByRole("button", { name: "Replace public binding" }).click();
  await expect
    .poll(async () => (await metrics(page)).operations.replace)
    .toBe(1);
  await expect(status).toHaveText("Composition preview paused.");
  const afterReplace = await metrics(page);
  expect(afterReplace.operations.factory).toBe(1);
  expect(afterReplace.bindingMatched).toBe(true);
  expect(afterReplace.publicFingerprint).not.toBe(fingerprintBefore);
  await expect(page.locator("canvas")).toHaveCount(1);

  await page.getByRole("button", { name: "Unmount preview" }).click();
  await expect(page.getByRole("status")).toHaveText(
    "Composition preview unmounted.",
  );
  await expect(page.locator("canvas")).toHaveCount(0);
  await expect.poll(async () => (await metrics(page)).activeSessions).toBe(0);
  const afterUnmount = await metrics(page);
  expect(afterUnmount.operations).toMatchObject({
    factory: 1,
    open: 1,
    seek: 1,
    previous: 1,
    next: 1,
    play: 1,
    pause: 1,
    recover: 1,
    replace: 1,
    close: 1,
  });
  expect(afterUnmount.statusListenerCount).toBe(0);
  expect(afterUnmount.frameListenerCount).toBe(0);
  expect(afterUnmount.resources).toEqual({
    imageOwners: 0,
    fontOwners: 0,
    videoOwners: 0,
    staticLeases: 0,
    pendingOperations: 0,
    blobBytes: 0,
  });
});
