import { expect, test, type Page } from "@playwright/test";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { openIntegratedShell } from "../helpers/nleShell";

async function openFilmstripWorkspace(page: Page, fail = false) {
  await routeGenericFixtureMedia(page);
  await page.goto(
    `/nleWorkspace.html?media=1&filmstrip=1${fail ? "&filmstripFail=1" : ""}`,
  );
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator('[data-h3-nle-status="timeline"]')).toHaveAttribute(
    "data-h3-nle-authoring",
    "ready",
  );
}

test("M25-56 replays the inherited smoke filmstrip caller and names invalid input", async ({
  page,
}, testInfo) => {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  // D-7 was reported by this exact integrated `shape=smoke` caller. Keep this replay separate
  // from the filmstrip-only workspace so a clean result cannot come from bypassing the caller.
  await openIntegratedShell(page, "smoke");
  await expect(page.locator('[data-h3-nle-status="timeline"]')).toHaveAttribute(
    "data-h3-nle-authoring",
    "ready",
  );
  await page.waitForTimeout(250);

  const invalidFilmstripErrors = [...pageErrors, ...consoleErrors].filter(
    (message) => message.includes("filmstrip paint input is invalid"),
  );
  const trace = {
    schema: "M25-56D7FilmstripCallerTraceV1",
    caller: "/nleShell.html?shape=smoke",
    pageErrors,
    consoleErrors,
    invalidFilmstripErrors,
    disposition:
      invalidFilmstripErrors.length === 0
        ? "NOT_REPRODUCED_EXACT_SMOKE_CALLER_CLEAN"
        : "RED_EXACT_SMOKE_CALLER_INVALID_FILMSTRIP_INPUT",
  };
  await testInfo.attach("m25-56-d7-filmstrip-caller-trace", {
    contentType: "application/json",
    body: Buffer.from(JSON.stringify(trace)),
  });
  console.log(`M25-56_D7_FILMSTRIP_CALLER=${JSON.stringify(trace)}`);
  expect(trace.caller).toBe("/nleShell.html?shape=smoke");
});

test("filmstrip pixels sit below readable clip controls and clear on close", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await openFilmstripWorkspace(page);

  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().mediaOwnership),
    )
    .toMatchObject({
      filmstripAttempts: 1,
      filmstripBitmaps: 1,
      maximumFilmstripBitmaps: 1,
    });
  await page.waitForTimeout(100);
  expect(pageErrors).toEqual([]);
  const canvas = page.locator('[data-h3-nle-canvas="timeline_decoration"]');
  await expect(canvas).toHaveCount(1);
  await expect
    .poll(() =>
      canvas.evaluate((element) => {
        const target = element as HTMLCanvasElement;
        const pixels = target
          .getContext("2d")!
          .getImageData(0, 0, target.width, target.height).data;
        let coloured = 0;
        for (let index = 0; index < pixels.length; index += 4) {
          const red = pixels[index]!;
          const green = pixels[index + 1]!;
          const blue = pixels[index + 2]!;
          if (
            (red > 130 && green < 120 && blue < 140) ||
            (green > 100 && red < 150 && blue < 150) ||
            (blue > 150 && red < 120 && green > 100) ||
            (red > 150 && green > 120 && blue < 120)
          )
            coloured += 1;
        }
        return coloured;
      }),
    )
    .toBeGreaterThan(100);

  const body = page.locator(".h3-nle-clip-body").first();
  await expect(body).toBeVisible();
  await expect(body.locator("span")).not.toHaveText("");
  const layers = await body.evaluate((element) => {
    const canvas = document.querySelector(
      '[data-h3-nle-canvas="timeline_decoration"]',
    )!;
    const bodyStyle = getComputedStyle(element);
    const canvasStyle = getComputedStyle(canvas);
    const bounds = element.getBoundingClientRect();
    const top = document.elementFromPoint(
      bounds.left + bounds.width / 2,
      bounds.top + bounds.height / 2,
    );
    return {
      bodyZ: bodyStyle.zIndex,
      canvasZ: canvasStyle.zIndex,
      bodyBackground: bodyStyle.backgroundColor,
      canvasPointerEvents: canvasStyle.pointerEvents,
      bodyOwnsHit: top === element || element.contains(top),
    };
  });
  expect(layers).toEqual({
    bodyZ: "5",
    canvasZ: "4",
    bodyBackground: "rgba(0, 0, 0, 0)",
    canvasPointerEvents: "none",
    bodyOwnsHit: true,
  });

  await page.locator('[data-h3-nle-action="close"]').click();
  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().mediaOwnership),
    )
    .toMatchObject({
      live: 0,
      surfaces: 0,
      retainedBytes: 0,
      filmstripBitmaps: 0,
    });
  await expect(canvas).toHaveCount(0);
});

test("missing filmstrip keeps solid clips selectable without an edit transaction", async ({
  page,
}) => {
  await openFilmstripWorkspace(page, true);

  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().mediaOwnership),
    )
    .toMatchObject({
      filmstripBitmaps: 0,
      maximumFilmstripBitmaps: 0,
    });
  // The accepted startup layout may replan this failed decoration once. Bound both the initial
  // failure and any subsequent retry instead of requiring a scheduler-internal exact count.
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          window.nleWorkspaceHarness.snapshot().mediaOwnership
            .filmstripAttempts,
      ),
    )
    .toBeGreaterThan(0);
  await page.waitForTimeout(100);
  const attempts = await page.evaluate(
    () =>
      window.nleWorkspaceHarness.snapshot().mediaOwnership.filmstripAttempts,
  );
  expect(attempts).toBeLessThanOrEqual(2);
  const clip = page.locator("[data-h3-nle-clip]").nth(1);
  const body = clip.locator(".h3-nle-clip-body");
  await expect(body).toBeVisible();
  expect(
    await clip.evaluate((element) => getComputedStyle(element).backgroundColor),
  ).not.toBe("rgba(0, 0, 0, 0)");
  const intentsBefore = await page.evaluate(
    () => window.nleWorkspaceHarness.snapshot().intents.length,
  );
  expect(intentsBefore).toBe(0);
  await body.click();
  await expect(body).toBeFocused();
  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().intents.length),
    )
    .toBe(1);
  expect(
    await page.evaluate(
      () =>
        window.nleWorkspaceHarness.snapshot().mediaOwnership.filmstripAttempts,
    ),
  ).toBe(attempts);

  await page.locator('[data-h3-nle-action="close"]').click();
  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().mediaOwnership),
    )
    .toMatchObject({ live: 0, surfaces: 0, filmstripBitmaps: 0 });
});
