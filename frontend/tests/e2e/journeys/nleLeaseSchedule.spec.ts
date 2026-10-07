import { expect, test } from "@playwright/test";

import type {} from "../../../e2e/nleLeaseSchedule";

test("twelve bin decorations yield to five playback owners and drain cleanly", async ({
  page,
}) => {
  await page.goto("/nleLeaseSchedule.html");
  await page.waitForFunction(() => window.nleLeaseSchedule !== undefined);

  const result = await page.evaluate(() => window.nleLeaseSchedule.run());

  expect(result.requestedDecorations).toBe(12);
  expect(new Set(result.published).size).toBe(12);
  expect(result.published).toHaveLength(12);
  expect(result.maximumDecorations).toBeLessThanOrEqual(2);
  expect(result.preemptions).toBeGreaterThanOrEqual(1);
  expect(result.playbackResourceLimits).toBe(0);
  expect(result.playbackAttempts).toEqual({
    "video-1": 1,
    "video-2": 1,
    "video-3": 1,
    "image-1": 1,
    "font-1": 1,
  });
  expect(result.finalLeases).toBe(0);
  expect(result.scheduler).toMatchObject({
    activeKey: null,
    pendingKeys: [],
    closed: true,
  });
});
