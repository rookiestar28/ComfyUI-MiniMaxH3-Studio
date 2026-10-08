// M25-33 AC33-02/03/04/06/09: the Media tools card on the REAL integrated shell
// (frontend/e2e/nleShell.tsx, `mediaSetup=1`). The media runtime status, setup and job routes, the
// output capability read and the clip preview route are answered by one stateful double per test
// (`helpers/mediaRuntimeDouble.ts`) whose wires are the backend-generated v3 samples; import
// requests reach the real import service (`scripts/m25_16_import_fixture.py`). Every row drives a
// real transition -- a click or a key, a poll, a settled job -- and reads the outcome from the
// product's own session and request counters, never from a seeded state.
import { test, expect, type Locator, type Page } from "@playwright/test";

import {
  createRuntimeDouble,
  type RuntimeDouble,
  type RuntimeDoubleOptions,
} from "../helpers/mediaRuntimeDouble";
import {
  startImportFixture,
  expectImportBootstrap,
  type ImportFixture,
} from "../helpers/nleImportFixture";
import { openExportPanel } from "../helpers/nleExport";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";

const settingsCard = '[data-h3-media-tools-placement="settings"]';
const sidebarCard = '[data-h3-media-tools-placement="contextual-sidebar"]';
const overlayCard = '[data-h3-media-tools-placement="contextual-overlay"]';
const primary = '[data-h3-media-tools-action="primary"]';
const cancel = '[data-h3-media-tools-action="cancel"]';
const importButton = '[data-h3-nle-control="asset.import_production"]';
const editor = '[data-h3-nle-surface="overlay_v1"]';

// The sidebar mount is `min(100%, 704px)`; a 704 px viewport puts every row at the design floor.
test.use({ viewport: { width: 704, height: 900 }, deviceScaleFactor: 1 });

const mediaRuntime = async (page: Page) =>
  (await shellSnapshot(page)).mediaRuntime;

/**
 * M25-48 re-homed the Production import into the full editor's Media pane (B-M2561-01): the
 * Production page's Clip editor function opens the editor, and the import control and its Media
 * tools card live in that pane.
 */
async function openImportPane(page: Page): Promise<void> {
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor" }).click();
  await page.locator('[data-h3-nle-entry="open"]').click();
  await expect(page.locator(editor)).toBeVisible();
  await page.locator('[data-h3-nle-pane="assets"]').click();
}

/** The editor is modal: the Sidebar under it is reachable only once it is closed. */
async function closeEditor(page: Page): Promise<void> {
  await page.locator(editor).locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(editor)).toHaveCount(0);
}

async function openSettings(page: Page): Promise<Locator> {
  await page.locator('nav.h3n button[data-page-id="settings"]').click();
  const card = page.locator(settingsCard);
  await expect(card).toBeVisible();
  return card;
}

async function openShell(
  page: Page,
  double: RuntimeDouble,
  params: Record<string, string> = {},
): Promise<void> {
  await double.attach(page);
  await page.goto(
    `/nleShell.html?${new URLSearchParams({ mediaSetup: "1", ...params })}`,
  );
}

async function focusKey(page: Page): Promise<string | null> {
  return page.evaluate(
    () =>
      document.activeElement?.getAttribute("data-h3-focus-key") ??
      document.activeElement?.tagName ??
      null,
  );
}

async function noHorizontalOverflow(page: Page, card: Locator): Promise<void> {
  const widths = await card.evaluate((element) => ({
    card: [element.scrollWidth, element.clientWidth],
    page: [
      document.documentElement.scrollWidth,
      document.documentElement.clientWidth,
    ],
  }));
  expect(widths.card[0]).toBeLessThanOrEqual(widths.card[1]!);
  expect(widths.page[0]).toBeLessThanOrEqual(widths.page[1]!);
}

test.describe("Settings", () => {
  const LOCALES = [
    {
      locale: "en",
      install: "Install",
      release: "Release page",
      advanced: "Advanced",
      ready: "Media tools are ready.",
    },
    {
      locale: "zh-TW",
      install: "安裝",
      release: "發行頁面",
      advanced: "進階",
      ready: "媒體工具已就緒。",
    },
    {
      locale: "zh-CN",
      install: "安装",
      release: "发布页面",
      advanced: "高级",
      ready: "媒体工具已就绪。",
    },
  ] as const;

  for (const row of LOCALES) {
    test(`${row.locale}: one Install by keyboard, polite progress with focus, then ready at 704 px`, async ({
      page,
    }) => {
      const double = createRuntimeDouble({ polls: 3 });
      await openShell(page, double, { locale: row.locale });
      const card = await openSettings(page);
      await expect(card).toHaveAttribute(
        "data-h3-media-tools",
        "setup_required",
      );
      await expect(card.locator(primary)).toHaveCount(1);
      await expect(card.locator(primary)).toHaveText(row.install);
      const link = card.getByRole("link", { name: row.release });
      await expect(link).toHaveAttribute("rel", "noopener noreferrer");
      await expect(link).toHaveAttribute("target", "_blank");
      // No text input on the normal path: the folder field is behind a closed disclosure.
      const advanced = card.locator("details.h3s-mt-advanced");
      await expect(advanced).not.toHaveAttribute("open", "");
      await expect(advanced.locator("summary")).toHaveText(row.advanced);
      await expect(card.locator("input:visible")).toHaveCount(0);
      await noHorizontalOverflow(page, card);
      expect(double.counts().statusReads).toBe(1);

      await card.locator(primary).focus();
      await page.keyboard.press("Enter");
      await expect(card).toHaveAttribute("data-h3-media-tools", "installing");
      const line = card.locator('p.h3s-mt-state[role="status"]');
      await expect(line).toHaveAttribute("aria-live", "polite");
      await expect.poll(() => focusKey(page)).toBe("settings-media-tools");
      await expect(card.locator("progress")).toBeVisible();
      await expect(card.locator(cancel)).toBeEnabled();
      await noHorizontalOverflow(page, card);

      await expect(card).toHaveAttribute("data-h3-media-tools", "ready");
      await expect(line).toContainText(row.ready);
      await expect(card.locator("progress")).toHaveCount(0);
      const counts = double.counts();
      expect(counts.installPosts).toBe(1);
      expect(counts.jobReads).toBe(3);
      await noHorizontalOverflow(page, card);
      // The poll stopped at the terminal state.
      await page.waitForTimeout(2_500);
      expect(double.counts().jobReads).toBe(3);
    });
  }

  test("a failed install names its reason and returns focus to Install again", async ({
    page,
  }) => {
    const double = createRuntimeDouble({ polls: 2, outcome: "failed" });
    await openShell(page, double);
    const card = await openSettings(page);
    await card.locator(primary).click();
    await expect(card).toHaveAttribute("data-h3-media-tools", "failed");
    await expect(card).toHaveAttribute(
      "data-h3-media-tools-reason",
      "digest_mismatch",
    );
    await expect(card.locator(primary)).toHaveText("Install again");
    await expect.poll(() => focusKey(page)).toBe("media-tools-primary");
    await expect(card.locator("[data-h3-media-tools-source]")).toBeVisible();
    expect(double.counts().installPosts).toBe(1);
  });

  test("cancel returns to setup required with focus on Install and no second job", async ({
    page,
  }) => {
    const double = createRuntimeDouble({ polls: Infinity });
    await openShell(page, double);
    const card = await openSettings(page);
    await card.locator(primary).click();
    await expect(card).toHaveAttribute("data-h3-media-tools", "installing");
    await card.locator(cancel).click();
    await expect(card).toHaveAttribute("data-h3-media-tools", "setup_required");
    await expect(card.locator(primary)).toHaveText("Install");
    await expect.poll(() => focusKey(page)).toBe("media-tools-primary");
    await expect
      .poll(async () => (await mediaRuntime(page)).jobState)
      .toBe("cancelled");
    await page.waitForTimeout(2_500);
    const counts = double.counts();
    expect(counts.cancelPosts).toBe(1);
    expect(counts.installPosts).toBe(1);
    expect(double.installed()).toBe(false);
    await expect(card).toHaveAttribute("data-h3-media-tools", "setup_required");
  });

  test("a lost install response adopts the running job without sending it again", async ({
    page,
  }) => {
    const double = createRuntimeDouble({ polls: 2, loseInstallReply: true });
    await openShell(page, double);
    const card = await openSettings(page);
    await card.locator(primary).click();
    await expect(card).toHaveAttribute("data-h3-media-tools", "ready");
    const counts = double.counts();
    expect(counts.installPosts).toBe(1);
    // The first read on open, the re-read that found the running job, the read after it settled.
    expect(counts.statusReads).toBe(3);
    expect((await mediaRuntime(page)).notice).toBeNull();
  });

  test("two pages share the one running job", async ({ page, context }) => {
    const double = createRuntimeDouble({ polls: Infinity });
    await openShell(page, double);
    const first = await openSettings(page);
    await first.locator(primary).click();
    await expect(first).toHaveAttribute("data-h3-media-tools", "installing");

    const second = await context.newPage();
    await openShell(second, double);
    const secondCard = await openSettings(second);
    await expect(secondCard).toHaveAttribute(
      "data-h3-media-tools",
      "installing",
    );
    await expect(secondCard.locator(primary)).toHaveCount(0);
    expect((await mediaRuntime(second)).jobId).toBe(
      (await mediaRuntime(page)).jobId,
    );
    double.finish();
    await expect(first).toHaveAttribute("data-h3-media-tools", "ready");
    await expect(secondCard).toHaveAttribute("data-h3-media-tools", "ready");
    expect(double.counts().installPosts).toBe(1);
    await second.close();
  });

  test("a reclaimable earlier copy is removed only by its own action", async ({
    page,
  }) => {
    const double = createRuntimeDouble({ initial: "recovery" });
    await openShell(page, double);
    const card = await openSettings(page);
    await expect(card).toHaveAttribute("data-h3-media-tools", "ready");
    const recovery = card.locator("[data-h3-media-tools-recovery]");
    await expect(recovery).toBeVisible();
    await page.waitForTimeout(1_500);
    expect(double.counts().reclaimPosts).toBe(0);
    await recovery.getByRole("button", { name: "Remove earlier copy" }).click();
    await expect(recovery).toHaveCount(0);
    await expect(card).toHaveAttribute("data-h3-media-tools", "ready");
    expect(double.counts().reclaimPosts).toBe(1);
    expect(double.counts().installPosts).toBe(0);
  });

  test("a host whose renderer is unavailable is told so and offered no install for it", async ({
    page,
  }) => {
    const double = createRuntimeDouble({
      initial: "ready",
      renderUnqualified: true,
    });
    await openShell(page, double);
    const card = await openSettings(page);
    await expect(card).toHaveAttribute("data-h3-media-tools", "ready");
    await expect(
      card.locator('[data-h3-media-tools-render="unavailable"]'),
    ).toBeVisible();
    await expect(card.locator(primary)).toHaveCount(0);
  });
});

test.describe("import install-and-continue", () => {
  let fixture: ImportFixture | undefined;
  test.afterEach(async () => {
    await fixture?.close();
    fixture = undefined;
  });

  async function refusedImport(
    page: Page,
    options: RuntimeDoubleOptions,
    params: Record<string, string> = {},
  ): Promise<{ double: RuntimeDouble; card: Locator }> {
    const double = createRuntimeDouble(options);
    fixture = await startImportFixture(page, {
      // The first import meets a host without media tools: the real route's bodiless 503.
      refuse: (kind, index) => (kind === "import" && index === 0 ? 503 : null),
    });
    await openShell(page, double, { import: "1", target: "ready", ...params });
    await expectImportBootstrap(fixture);
    await openImportPane(page);
    await expect(page.locator(importButton)).toBeEnabled();
    await page.locator(importButton).click();
    await expect
      .poll(async () => (await shellSnapshot(page)).importRefusal)
      .toBe("service_unavailable");
    const card = page.locator(sidebarCard);
    await expect(card).toHaveAttribute("data-h3-media-tools", "setup_required");
    await expect(card.locator(primary)).toHaveText("Install and continue");
    return { double, card };
  }

  test("a double click installs once and continues the original import exactly once", async ({
    page,
  }) => {
    const { double, card } = await refusedImport(page, { polls: 2 });
    await card.locator(primary).dblclick();
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toBe("succeeded");
    const after = await shellSnapshot(page);
    expect(after.mediaRuntime).toMatchObject({
      notice: "continued",
      pendingKind: null,
      resumeKind: "production_import",
    });
    expect(double.counts().installPosts).toBe(1);
    expect(fixture!.importRequests).toHaveLength(2);
    expect(after.queuedPrompts).toBe(0);
    expect(after.renderJobRequests).toBe(0);
    await page.waitForTimeout(2_500);
    expect(fixture!.importRequests).toHaveLength(2);
  });

  test("leaving the page drops the waiting import with a visible notice", async ({
    page,
  }) => {
    const { double } = await refusedImport(page, { polls: Infinity });
    await page.locator(sidebarCard).locator(primary).click();
    await expect
      .poll(async () => (await mediaRuntime(page)).pendingKind)
      .toBe("production_import");
    await closeEditor(page);
    const settings = await openSettings(page);
    await expect(
      settings.locator('[data-h3-media-tools-notice="continuation_cleared"]'),
    ).toBeVisible();
    double.finish();
    await expect(settings).toHaveAttribute("data-h3-media-tools", "ready");
    await page.waitForTimeout(1_500);
    expect(fixture!.importRequests).toHaveLength(1);
    expect((await mediaRuntime(page)).resumeKind).toBeNull();
  });

  // B-M2561-01: since M25-48 the import waits inside the modal editor, and the selection lives on
  // the Production function under it. Changing the selection therefore means closing the editor
  // first, and closing it drops the waiting import (the leaving rule above). The `select_again`
  // guard for a projection that changed underneath a waiting import is proven by the replaced
  // workspace case below, which is still reachable without leaving.
  test("a selection changed after closing the editor is not imported; the waiting import was dropped", async ({
    page,
  }) => {
    const renderPhaseWarnings: string[] = [];
    page.on("console", (message) => {
      if (
        message.type() === "error" &&
        message.text().includes("Render methods should be a pure function")
      )
        renderPhaseWarnings.push(message.text());
    });
    const { double, card } = await refusedImport(
      page,
      { polls: Infinity },
      { segments: "2" },
    );
    await card.locator(primary).click();
    await expect(card).toHaveAttribute("data-h3-media-tools", "installing");
    await closeEditor(page);
    await page.getByRole("tab", { name: "Production" }).click();
    const second = page.getByRole("checkbox", {
      name: "Segment 2",
      exact: true,
    });
    await second.click();
    await expect(second).not.toBeChecked();
    double.finish();
    await expect
      .poll(async () => (await mediaRuntime(page)).notice)
      .toBe("continuation_cleared");
    await page.waitForTimeout(1_500);
    expect(fixture!.importRequests).toHaveLength(1);
    expect((await mediaRuntime(page)).pendingKind).toBeNull();
    // The user is told where the import was: in the reopened Media pane and in Settings.
    await openImportPane(page);
    await expect(
      page
        .locator(editor)
        .locator('[data-h3-media-tools-notice="continuation_cleared"]'),
    ).toBeVisible();
    await closeEditor(page);
    const settings = await openSettings(page);
    await expect(
      settings.locator('[data-h3-media-tools-notice="continuation_cleared"]'),
    ).toBeVisible();
    expect(fixture!.importRequests).toHaveLength(1);
    expect(renderPhaseWarnings).toEqual([]);
  });

  test("a replaced Production workspace is never imported into", async ({
    page,
  }) => {
    const { double, card } = await refusedImport(page, { polls: Infinity });
    await card.locator(primary).click();
    await expect(card).toHaveAttribute("data-h3-media-tools", "installing");
    await page.evaluate(() =>
      window.nleShellHarness.replaceProductionWorkspace(),
    );
    double.finish();
    await expect
      .poll(async () => (await mediaRuntime(page)).notice)
      .toBe("select_again");
    await page.waitForTimeout(1_500);
    expect(fixture!.importRequests).toHaveLength(1);
    await closeEditor(page);
    const settings = await openSettings(page);
    await expect(
      settings.locator('[data-h3-media-tools-notice="select_again"]'),
    ).toBeVisible();
  });
});

test.describe("preview and render install-and-continue", () => {
  test("clip preview reopens once after install", async ({ page }) => {
    // M25-44 (one NLE): the selected-clip source preview lives in the full editor's monitor, which
    // offers it when this browser cannot composite (no 2D canvas); its Media tools card is the
    // overlay placement inside the monitor region.
    await page.addInitScript(() => {
      const original = HTMLCanvasElement.prototype.getContext;
      Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
        configurable: true,
        value: function noCanvas2d(
          this: HTMLCanvasElement,
          type: string,
          ...rest: unknown[]
        ) {
          if (type === "2d") return null;
          return (
            original as (
              this: HTMLCanvasElement,
              type: string,
              ...rest: unknown[]
            ) => unknown
          ).apply(this, [type, ...rest]);
        },
      });
    });
    const double = createRuntimeDouble({ polls: 2 });
    await double.attach(page);
    await openIntegratedShell(page, "smoke", {
      extraParams: { mediaSetup: "1" },
      beforeOpen: async (shell) => {
        await shell
          .getByRole("navigation", { name: "H3 Context pages" })
          .getByRole("button", { name: "Production" })
          .click();
        await shell.getByRole("tab", { name: "Clip editor" }).click();
      },
      expandOverlay: false,
    });
    await page.locator('[data-h3-nle-entry="open"]').click();
    const monitor = page.locator(
      '[data-h3-nle-surface="overlay_v1"] [data-h3-nle-area="monitor"]',
    );
    await expect(
      monitor.locator('[data-h3-nle-fallback="selected_source_only"]'),
    ).toBeVisible();
    await page
      .locator(
        '[data-h3-nle-clip="clip-1"] [data-h3-nle-control="selection.set"]',
      )
      .click();
    await monitor
      .getByRole("button", { name: "Selected source only", exact: true })
      .click();
    const card = monitor.locator(overlayCard);
    await expect(card).toHaveAttribute("data-h3-media-tools", "setup_required");
    expect(double.counts().previewRequests).toBe(1);
    await card.locator(primary).click();
    await expect(monitor.locator("section.h3a-preview video")).toBeVisible();
    await expect(monitor.locator(overlayCard)).toHaveCount(0);
    // The Clip editor tab no longer hosts a clip preview, so no sidebar card appears for it.
    await expect(page.locator(sidebarCard)).toHaveCount(0);
    const after = await shellSnapshot(page);
    expect(after.mediaRuntime).toMatchObject({
      resumeKind: "clip_preview",
      pendingKind: null,
    });
    expect(double.counts().previewRequests).toBe(2);
    expect(double.counts().installPosts).toBe(1);
    await page.waitForTimeout(2_000);
    expect(double.counts().previewRequests).toBe(2);
  });

  test("final output re-reads its capability after install and starts no render", async ({
    page,
  }) => {
    const double = createRuntimeDouble({ polls: 2 });
    await double.attach(page);
    await openIntegratedShell(page, "smoke", {
      extraParams: { mediaSetup: "1" },
    });
    await openExportPanel(page);
    const card = page.locator(overlayCard);
    await expect(card).toHaveAttribute("data-h3-media-tools", "setup_required");
    const reads = double.counts().capabilityReads;
    await card.locator(primary).click();
    await expect(
      page.locator('[data-h3-nle-render="available"]'),
    ).toBeVisible();
    expect(double.counts().capabilityReads).toBe(reads + 1);
    const after = await shellSnapshot(page);
    expect(after.renderJobRequests).toBe(0);
    expect(after.queuedPrompts).toBe(0);
    expect(after.mediaRuntime.resumeKind).toBe("final_render");
  });

  test("the first open before in-process activation re-reads final output once the status is read", async ({
    page,
  }) => {
    const double = createRuntimeDouble({
      initial: "ready",
      activateOnStatusRead: true,
    });
    await double.attach(page);
    await openIntegratedShell(page, "smoke", {
      extraParams: { mediaSetup: "1" },
    });
    await openExportPanel(page);
    await expect(
      page.locator('[data-h3-nle-render="available"]'),
    ).toBeVisible();
    const counts = double.counts();
    expect(counts.statusReads).toBe(1);
    expect(counts.capabilityReads).toBe(2);
    expect(counts.installPosts).toBe(0);
    await expect(page.locator(overlayCard)).toHaveCount(0);
    await page.waitForTimeout(2_500);
    expect(double.counts()).toMatchObject({
      statusReads: 1,
      capabilityReads: 2,
    });
    expect((await shellSnapshot(page)).renderJobRequests).toBe(0);
  });

  test("render_qualification_unavailable offers no install", async ({
    page,
  }) => {
    const double = createRuntimeDouble({
      initial: "ready",
      renderUnqualified: true,
    });
    await double.attach(page);
    await openIntegratedShell(page, "smoke", {
      extraParams: { mediaSetup: "1" },
    });
    await openExportPanel(page);
    await expect(
      page.locator('[data-h3-nle-render="backend_render_unavailable"]'),
    ).toBeVisible();
    await expect
      .poll(async () => (await mediaRuntime(page)).status)
      .toBe("read");
    await expect(page.locator(overlayCard)).toHaveCount(0);
    await expect(page.locator(primary)).toHaveCount(0);
    expect(double.counts().installPosts).toBe(0);
  });
});
