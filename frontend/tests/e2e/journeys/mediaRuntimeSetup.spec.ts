import { test, expect, type Page } from "@playwright/test";
import { createRuntimeDouble } from "../helpers/mediaRuntimeDouble";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";
import { openExportPanel } from "../helpers/nleExport";
import {
  startImportFixture,
  expectImportBootstrap,
} from "../helpers/nleImportFixture";
const settingsCard = '[data-h3-media-tools-placement="settings"]';
const overlayCard = '[data-h3-media-tools-placement="contextual-overlay"]';
const primary = '[data-h3-media-tools-action="primary"]';
test.use({ viewport: { width: 704, height: 900 }, deviceScaleFactor: 1 });
for (const locale of ["en", "zh-TW", "zh-CN"]) {
  test(`manual setup ${locale}: visible selection, checksum, retry and comparable captures`, async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const double = createRuntimeDouble();
    await double.attach(page);
    await page.goto(`/nleShell.html?mediaSetup=1&locale=${locale}`);
    await page.locator('nav.h3n button[data-page-id="settings"]').click();
    const card = page.locator(settingsCard);
    await expect(card).toHaveAttribute("data-h3-media-tools", "setup_required");
    await expect(card.locator("input")).toBeVisible();
    await expect(card.locator(primary)).toBeDisabled();
    const beforeHelp = double.counts();
    const help = card.locator('[data-h3-media-tools-action="help"]');
    await help.focus();
    await help.press("Enter");
    const panel = card.locator("[data-h3-media-tools-help]");
    await expect(panel).toBeVisible();
    await expect(help).toHaveAttribute("aria-expanded", "true");
    await expect(panel).toContainText("certutil -hashfile <file> SHA256");
    await expect(panel).toContainText("Get-FileHash -Algorithm SHA256");
    const releasePage = await card
      .locator('[data-h3-media-tools-action="download"]')
      .getAttribute("href");
    expect(releasePage).toMatch(
      /^https:\/\/github\.com\/GyanD\/codexffmpeg\/releases\/tag\/[^/]+$/,
    );
    await expect(panel).toContainText(releasePage!.split("/").at(-1)!);
    const checksum = await card
      .locator("[data-h3-media-tools-sha]")
      .innerText();
    await expect(panel).toContainText(checksum);
    if (process.env.MEDIA_SETUP_CAPTURE)
      await card.screenshot({
        path: `${process.env.MEDIA_SETUP_CAPTURE}/help-settings-${locale}.png`,
      });
    await panel.press("Escape");
    await expect(panel).toHaveCount(0);
    await expect(help).toBeFocused();
    await help.press("Space");
    await card.locator('[data-h3-media-tools-action="close-help"]').click();
    await expect(help).toBeFocused();
    // Only the user's new tab navigates. No request is delegated to the local node.
    await context.route(releasePage!, (route) =>
      route.fulfill({
        status: 200,
        contentType: "text/html",
        body: "<title>Release page navigation double</title>",
      }),
    );
    const popupReady = page.waitForEvent("popup");
    await card.locator('[data-h3-media-tools-action="download"]').click();
    const popup = await popupReady;
    await popup.waitForLoadState("domcontentloaded");
    expect(popup.url()).toBe(releasePage);
    expect(await popup.evaluate(() => window.opener === null)).toBe(true);
    await popup.close();
    expect(double.counts()).toEqual(beforeHelp);
    if (process.env.MEDIA_SETUP_CAPTURE)
      await card.screenshot({
        path: `${process.env.MEDIA_SETUP_CAPTURE}/current-settings-${locale}.png`,
      });
    await card.locator('[data-h3-media-tools-action="copy"]').click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
      checksum,
    );
    await card.locator("input").fill("C:/media-tools/bin");
    await card.locator(primary).click();
    await expect(card).toHaveAttribute("data-h3-media-tools", "ready");
    expect(double.counts().localPosts).toBe(1);
    await card.locator("details summary").click();
    await card.locator("details button").click();
    expect(double.counts().restorePosts).toBe(1);
    expect((await shellSnapshot(page)).queuedPrompts).toBe(0);
    // A fresh missing pair gives the same before/after inline placement and viewport.
    const inlineDouble = createRuntimeDouble();
    await inlineDouble.attach(page);
    await openIntegratedShell(page, "smoke", {
      extraParams: { mediaSetup: "1", locale },
      expandOverlay: false,
    });
    await page
      .locator("#root")
      .getByRole("button", { name: "Open full editor" })
      .click();
    await openExportPanel(page);
    const inline = page.locator(overlayCard);
    await expect(inline).toHaveAttribute(
      "data-h3-media-tools",
      "setup_required",
    );
    const inlineHelp = inline.locator('[data-h3-media-tools-action="help"]');
    await inlineHelp.focus();
    await inlineHelp.press("Enter");
    await expect(inline.locator("[data-h3-media-tools-help]")).toBeVisible();
    if (process.env.MEDIA_SETUP_CAPTURE)
      await inline.screenshot({
        path: `${process.env.MEDIA_SETUP_CAPTURE}/help-inline-${locale}.png`,
      });
    expect(await inline.evaluate((e) => e.scrollWidth <= e.clientWidth)).toBe(
      true,
    );
    await inline.locator("[data-h3-media-tools-help]").press("Escape");
    await expect(inlineHelp).toBeFocused();
    await expect(page.locator("[data-h3-nle-root]")).toBeVisible();
    if (process.env.MEDIA_SETUP_CAPTURE)
      await inline.screenshot({
        path: `${process.env.MEDIA_SETUP_CAPTURE}/current-inline-${locale}.png`,
      });
    expect(await inline.evaluate((e) => e.scrollWidth <= e.clientWidth)).toBe(
      true,
    );
    await inline.locator("input").fill("C:/media-tools/bin");
    await inline.locator(primary).click();
    await expect(
      page.locator('[data-h3-nle-render="available"]'),
    ).toBeVisible();
    expect((await shellSnapshot(page)).renderJobRequests).toBe(0);
    expect(inlineDouble.counts().retiredRequests).toBe(0);
  });
}
test("stale config is refused without installation work", async ({ page }) => {
  const double = createRuntimeDouble({ conflict: true });
  await double.attach(page);
  await page.goto("/nleShell.html?mediaSetup=1");
  await page.locator('nav.h3n button[data-page-id="settings"]').click();
  const card = page.locator(settingsCard);
  await card.locator("input").fill("C:/media-tools/bin");
  await card.locator(primary).click();
  await expect(
    card.locator('[data-h3-media-tools-refusal="config_conflict"]'),
  ).toBeVisible();
  expect(double.counts().localPosts).toBe(1);
  expect(double.counts().retiredRequests).toBe(0);
});
test("import selects tools but waits for the user's original retry", async ({
  page,
}) => {
  const double = createRuntimeDouble();
  await double.attach(page);
  const fixture = await startImportFixture(page, {
    refuse: (kind, index) => (kind === "import" && index === 0 ? 503 : null),
  });
  try {
    await page.goto("/nleShell.html?mediaSetup=1&import=1&target=ready");
    await expectImportBootstrap(fixture);
    await page.locator('nav.h3n button[data-page-id="production"]').click();
    await page.getByRole("tab", { name: "Clip editor" }).click();
    await page.locator('[data-h3-nle-entry="open"]').click();
    await page.locator('[data-h3-nle-pane="assets"]').click();
    const button = page.locator(
      '[data-h3-nle-control="asset.import_production"]',
    );
    await button.click();
    const card = page.locator(
      '[data-h3-media-tools-placement="contextual-sidebar"]',
    );
    await expect(card).toHaveAttribute("data-h3-media-tools", "setup_required");
    await card.locator("input").fill("C:/media-tools/bin");
    await card.locator(primary).click();
    await expect(card).toHaveCount(0);
    expect(fixture.importRequests).toHaveLength(1);
    await button.click();
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toBe("succeeded");
    expect(fixture.importRequests).toHaveLength(2);
    expect((await shellSnapshot(page)).queuedPrompts).toBe(0);
    expect(double.counts().retiredRequests).toBe(0);
  } finally {
    await fixture.close();
  }
});

for (const lost of [false, true]) {
  test(
    lost
      ? "lost selection reply rereads without resubmitting"
      : "unsupported local pair remains refused",
    async ({ page }) => {
      const double = createRuntimeDouble({ loseSelectionReply: lost });
      await double.attach(page);
      await page.goto("/nleShell.html?mediaSetup=1");
      await page.locator('nav.h3n button[data-page-id="settings"]').click();
      const card = page.locator(settingsCard);
      await card
        .locator("input")
        .fill(lost ? "C:/media-tools/bin" : "C:/wrong/bin");
      await card.locator(primary).click();
      if (lost)
        await expect(card).toHaveAttribute("data-h3-media-tools", "ready");
      else
        await expect(
          card.locator(
            '[data-h3-media-tools-refusal="selection_unsupported_pair"]',
          ),
        ).toBeVisible();
      expect(double.counts().localPosts).toBe(1);
      expect(double.counts().retiredRequests).toBe(0);
    },
  );
}
