import { expect, test } from "../fixtures/h3Page";

const statusRoute = "**/h3-context/v1/perception/status";
const visual = {
  schema: "h3.context.perception_host_status.v1",
  state: "configured",
  visual: true,
  audio: false,
  reason: "none",
  cancellation_observed: false,
};
test.use({ viewport: { width: 704, height: 1100 }, deviceScaleFactor: 1 });

test("current Settings has explicit synchronized refresh and conserved graph/queue state", async ({
  page,
}, testInfo) => {
  let wire = visual;
  let reads = 0;
  await page.route(statusRoute, async (route) => {
    reads += 1;
    expect(route.request().method()).toBe("GET");
    expect(route.request().postData()).toBeNull();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(wire),
    });
  });
  await page.goto("/perceptionHost.html");
  await expect(
    page.getByRole("button", { name: "Settings", exact: true }),
  ).toBeVisible();
  const before = await page.evaluate(() => window.perceptionHarness.counts());
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("status", { name: "Visual analysis" }),
  ).toHaveText("Ready");
  await expect(page.getByRole("status", { name: "Audio analysis" })).toHaveText(
    "Unavailable",
  );
  await expect(
    page.getByText("Host cancellation is not observed."),
  ).toBeVisible();
  expect(reads).toBe(1);
  await page
    .locator("section.h3s-p")
    .screenshot({ path: testInfo.outputPath("settings-current-en.png") });
  wire = { ...visual, visual: false, audio: true };
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(
    page.getByRole("status", { name: "Visual analysis" }),
  ).toHaveText("Unavailable");
  await expect(page.getByRole("status", { name: "Audio analysis" })).toHaveText(
    "Ready",
  );
  expect(reads).toBe(2);
  await page.getByRole("button", { name: "Context", exact: true }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("status", { name: "Audio analysis" })).toHaveText(
    "Ready",
  );
  expect(reads).toBe(2);
  expect(await page.evaluate(() => window.perceptionHarness.counts())).toEqual(
    before,
  );
});

for (const [locale, title, refresh] of [
  ["en", "Local perception", "Refresh status"],
  ["zh-TW", "本機感知", "重新整理狀態"],
  ["zh-CN", "本地感知", "刷新状态"],
] as const) {
  test(`finite refused and unavailable states in ${locale}`, async ({
    page,
  }, testInfo) => {
    let failed = false;
    await page.route(statusRoute, (route) =>
      route.fulfill({
        status: failed ? 500 : 200,
        contentType: "application/json",
        body: JSON.stringify(
          failed
            ? { private: "untrusted_operator_value" }
            : {
                ...visual,
                state: "refused",
                visual: false,
                reason: "path_refused",
              },
        ),
      }),
    );
    await page.goto(`/perceptionHost.html?locale=${locale}`);
    await page.locator('nav.h3n button[data-page-id="settings"]').click();
    const block = page.getByRole("heading", { name: title }).locator("..");
    await expect(block).toBeVisible();
    await expect(block).toHaveAttribute("data-perception-state", "refused");
    expect(await block.locator("input, select, textarea").count()).toBe(0);
    await block.screenshot({
      path: testInfo.outputPath(`perception-refused-${locale}.png`),
    });
    failed = true;
    await page.getByRole("button", { name: refresh }).click();
    await expect(block).toHaveAttribute("data-perception-state", "unavailable");
    expect(await block.innerText()).not.toContain("untrusted_operator_value");
  });
}

test("leaving a pending browser read aborts it and cannot restore an old page generation", async ({
  page,
}) => {
  let finish: (() => void) | undefined;
  let reads = 0;
  await page.route(statusRoute, async (route) => {
    const attempt = ++reads;
    if (attempt === 1)
      await new Promise<void>((done) => {
        finish = done;
      });
    try {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...visual,
          state: "not_configured",
          visual: false,
          reason: "settings_missing",
        }),
      });
    } catch {
      // An aborted first generation has no response consumer; it grants no retry authority.
      if (attempt !== 1) throw new Error("fixture_response_unavailable");
    }
  });
  await page.goto("/perceptionHost.html");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect
    .poll(() => page.evaluate(() => window.perceptionHarness.reads()))
    .toEqual([{ method: "GET", aborted: false }]);
  await page.getByRole("button", { name: "Context", exact: true }).click();
  expect(await page.evaluate(() => window.perceptionHarness.reads())).toEqual([
    { method: "GET", aborted: true },
  ]);
  finish!();
  await expect(
    page.getByRole("heading", { name: "Local perception" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByText("Not configured", { exact: true })).toBeVisible();
  expect(reads).toBe(2);
});
