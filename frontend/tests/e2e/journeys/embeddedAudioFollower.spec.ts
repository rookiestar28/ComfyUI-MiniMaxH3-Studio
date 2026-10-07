import { expect, test } from "@playwright/test";
import type {} from "../../../e2e/embeddedAudioFollower";

test("follower controls preserve the selected VIDEO owner and dispose native resources", async ({
  page,
}) => {
  await page.goto("/embeddedAudioFollower.html");
  const status = page.getByRole("status");
  await page.getByRole("button", { name: "Open", exact: true }).click();
  await expect(status).toHaveText("suspended: paused");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect(status).toHaveText("following: none");
  expect(
    await page.evaluate(
      () => window.embeddedAudioFollowerHarness.snapshot().audible,
    ),
  ).toBe(1);
  expect(
    await page.evaluate(
      () => window.embeddedAudioFollowerHarness.snapshot().nativeAudible,
    ),
  ).toBe(0);
  await page.getByRole("slider", { name: "Seek frame" }).fill("12");
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          window.embeddedAudioFollowerHarness.snapshot().transport.outputFrame,
      ),
    )
    .toBe(12);
  await expect(status).toHaveText("following: none");
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(status).toHaveText("suspended: paused");
  expect(
    await page.evaluate(
      () => window.embeddedAudioFollowerHarness.snapshot().audible,
    ),
  ).toBe(0);
  await page.getByRole("button", { name: "Suspend", exact: true }).click();
  await expect(status).toHaveText("suspended: suspended");
  await page.getByRole("button", { name: "Recover", exact: true }).click();
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect(status).toHaveText("following: none");
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(status).toHaveText("silent: closed");
  const final = await page.evaluate(() =>
    window.embeddedAudioFollowerHarness.snapshot(),
  );
  expect(final.active).toBe(0);
  expect(final.audible).toBe(0);
  expect(final.nativeAudible).toBe(0);
  expect(final.audioContexts).toBe(0);
  expect(final.acquired).toBe(final.released);
  expect(final.status.final_render_capability).toBe("not_evaluated");
  await expect(page.locator("audio, video[controls]")).toHaveCount(0);
});
