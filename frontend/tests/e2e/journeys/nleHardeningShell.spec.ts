// M25-21 hardening of the seven frozen SidebarEditorUiInvariantV1 rows on the REAL integrated
// shell. Each row has its own accessibility case and its own recovery case (the long
// UI-CONTRACT-STRESS-V1 workload lives in `nleHardeningStress.spec.ts`). Every case counts the
// unrequested effects of its navigation or lifecycle interaction -- timeline transactions, render
// jobs, queued prompts, provider or production writes -- and requires zero; bounded reads are
// counted separately.
import { test, expect, type Page } from "@playwright/test";

import { EXPORT_BUTTON, openExportPanel } from "../helpers/nleExport";
import { Checks, hardeningEvidence } from "../helpers/nleHardeningEvidence";
import {
  startImportFixture,
  type ImportFixture,
} from "../helpers/nleImportFixture";
import {
  openIntegratedShell,
  shellSnapshot,
  type ShellOracle,
} from "../helpers/nleShell";
import { playheadFrame, playheadSlider } from "../helpers/nleTimeline";
import { openClipMenu } from "../helpers/nleCommandRecipes";

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  hasTouch: true,
});

const NAVIGATION = { name: "H3 Context pages" } as const;
const TABLIST = "[data-h3-director-function-tabs]";
const OVERLAY = '[data-h3-nle-surface="overlay_v1"]';
const ROOT = "[data-h3-nle-root]";
const HEADING = `${OVERLAY} .h3-nle-header h2`;
const LAUNCHER = '[data-h3-nle-entry="open"]';
const LAUNCHER_FOCUS = '[data-h3-focus-key="nle-open-overlay"]';
const CLOSE = '[data-h3-nle-action="close"]';
/** M25-44: the launcher's unavailable region; it carries the surface status alone. */
const UNAVAILABLE = '[data-h3-nle-unavailable="overlay_v1"]';
const PAGE_IDS = ["context", "production", "settings"] as const;

type Effects = Readonly<{
  transactions: number;
  revision: number | null;
  renderJobs: number;
  queued: number;
  writes: readonly string[];
}>;

/**
 * The bounded reads a view may make: the render-capability read, the Settings page's own
 * projection reads and the Media tools status read (M25-33: Settings reads it once, a refused
 * contextual surface reads why). Every other path (the harness records paths, not methods) counts
 * as work.
 * IMPORTANT: admit exactly the status GET path; `/h3-context/v1/media-runtime/setup` is the
 * install/cancel POST and must stay counted.
 */
const READ_PATHS = new Set([
  "/h3-context/v1/authoring/output-capability",
  "/h3-context/v1/provider/settings",
  "/h3-context/v1/build/provenance",
  "/h3-context/v1/media-runtime",
]);

async function effects(page: Page, oracle: ShellOracle): Promise<Effects> {
  const snapshot = await shellSnapshot(page);
  return {
    transactions: oracle.transactions.length,
    revision: snapshot.timelineSnapshot?.timelineRevision ?? null,
    renderJobs: snapshot.renderJobRequests,
    queued: snapshot.queuedPrompts,
    writes: snapshot.fetchPaths.filter(
      (path) =>
        !READ_PATHS.has(path) && path !== "/h3-context/v1/authoring/action",
    ),
  };
}

/**
 * Zero unrequested effects. The accepted revision may appear for the first time -- the history
 * is read on the first open -- but with no transaction it can never change.
 */
function expectNoEffects(before: Effects, after: Effects) {
  expect({
    ...after,
    revision: before.revision === null ? null : after.revision,
  }).toEqual(before);
}

async function productionPage(page: Page) {
  await page
    .getByRole("navigation", NAVIGATION)
    .getByRole("button", { name: "Production" })
    .click();
  await expect(page.locator(TABLIST)).toBeVisible();
}

async function toClipEditor(page: Page) {
  await productionPage(page);
  await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
  await expect(page.locator(LAUNCHER)).toBeVisible();
}

async function tabSize(page: Page, name: string) {
  const tab = page.getByRole("tab", { name, exact: true });
  await tab.scrollIntoViewIfNeeded();
  return (await tab.boundingBox())!;
}

/** Whether focus is on a connected element inside the open dialog. */
function focusInDialog(page: Page): Promise<boolean> {
  return page.evaluate((surface) => {
    const active = document.activeElement;
    const dialog = document.querySelector(surface);
    return (
      active !== null && active.isConnected && dialog?.contains(active) === true
    );
  }, OVERLAY);
}

/**
 * Every host root beside the owned overlay root (and `body` itself), with the attributes a modal
 * must not add to a root it does not own. The Sidebar's own content is not a host root.
 */
function hostRoots(page: Page) {
  return page.evaluate(
    (root) =>
      [document.body, ...document.body.children]
        .filter((element) => !element.matches(root))
        .map((element) => ({
          tag: element.tagName.toLowerCase(),
          id: element.id,
          inert: element.hasAttribute("inert"),
          hidden: element.getAttribute("aria-hidden"),
          style: element.getAttribute("style"),
        })),
    ROOT,
  );
}

/** A genuinely unsupported browser media runtime, and its undo, through the same real DOM API. */
async function degradeMediaRuntime(page: Page) {
  await page.evaluate(() => {
    const scope = window as unknown as { __h3CanPlayType?: unknown };
    scope.__h3CanPlayType ??= HTMLVideoElement.prototype.canPlayType;
    Object.defineProperty(HTMLVideoElement.prototype, "canPlayType", {
      configurable: true,
      value: () => "",
    });
  });
}

async function restoreMediaRuntime(page: Page) {
  await page.evaluate(() => {
    const scope = window as unknown as { __h3CanPlayType?: unknown };
    Object.defineProperty(HTMLVideoElement.prototype, "canPlayType", {
      configurable: true,
      writable: true,
      value: scope.__h3CanPlayType,
    });
  });
}

/**
 * M25-50 removed the Track form these rows once drafted in. The retained draft is now an
 * uncommitted Basic-tab edit of the selected clip: the inspector writes it to its retention slot
 * while it differs from the accepted value, and the selection it belongs to is accepted timeline
 * state, so a reopen or a remount shows the same clip, the same tab and the same unsent number.
 */
function trackOrder(page: Page) {
  return page.getByRole("spinbutton", { name: "Scale (%)", exact: true });
}

/**
 * Select the first clip and open its Basic tab once, before a row's effect counters start. An
 * import-mode shell opens on the fixture's empty Authoring target, so there the subject is made
 * first: one import of the ready output from the Media home and one insert of that asset.
 */
async function draftSubject(page: Page) {
  const clip = page
    .locator('[data-h3-nle-clip] [data-h3-nle-control="selection.set"]')
    .first();
  if ((await clip.count()) === 0) {
    await page.locator('[data-h3-nle-pane="assets"]').click();
    await page
      .locator('[data-h3-nle-control="asset.import_production"]')
      .click();
    const insert = page
      .locator('[data-h3-nle-asset] [data-h3-nle-control="asset.insert"]')
      .first();
    await expect(insert).toBeEnabled();
    await insert.click();
    await expect(clip).toBeVisible();
  }
  await clip.click();
  // The selection is a transaction whose acceptance replaces the monitor's composition (defect
  // record D-6); wait for the accepted selection so a caller that seeks afterwards can wait for
  // the replaced monitor (`monitorReady`) rather than issue a seek into the replacement.
  await expect(clip).toHaveAttribute("aria-pressed", "true");
  await page.locator('[data-h3-nle-property-tab="basic"]').click();
  await expect(trackOrder(page)).toBeVisible();
}

/** The replaced monitor is paused again with its transport back; only rows that seek need it. */
async function monitorReady(page: Page) {
  await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
    "Monitor paused.",
  );
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
}

async function openByLauncher(page: Page) {
  await page.locator(LAUNCHER).click();
  await expect(page.locator(OVERLAY)).toHaveAttribute(
    "data-h3-nle-state",
    "expanded",
  );
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
}

async function ownersReleased(page: Page) {
  await expect
    .poll(async () => (await shellSnapshot(page)).mediaOwnership.live)
    .toBe(0);
  const owners = (await shellSnapshot(page)).mediaOwnership;
  expect(owners.released).toBe(owners.acquired);
  return owners;
}

// Top-level cases: the coverage manifest names these titles without a describe prefix.
let importFixture: ImportFixture | undefined;
test.afterEach(async () => {
  await importFixture?.close();
  importFixture = undefined;
});

{
  test("hardening a11y ui function_switch manual-activation tabs move focus without side effects and activate on Enter or tap", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      expandOverlay: false,
    });
    await productionPage(page);
    const before = await effects(page, oracle);
    const workbench = page.getByRole("tab", {
      name: "Production",
      exact: true,
    });
    const editor = page.getByRole("tab", { name: "Clip editor", exact: true });
    const panel = page.locator("[data-h3-director-panel]");
    const checks = new Checks();
    const facts: Record<string, unknown> = {};
    await checks.step(
      "one_tab_stop_with_exact_selected_and_controlled_panel",
      async () => {
        await expect(page.locator(TABLIST)).toHaveRole("tablist");
        const stops = await page
          .locator(`${TABLIST} [role="tab"]`)
          .evaluateAll(
            (tabs) =>
              tabs.filter((tab) => (tab as HTMLElement).tabIndex === 0).length,
          );
        expect(stops).toBe(1);
        await expect(workbench).toHaveAttribute("aria-selected", "true");
        await expect(workbench).toHaveAttribute(
          "aria-controls",
          "h3-director-panel-production_workbench",
        );
        await expect(panel).toHaveAttribute(
          "id",
          "h3-director-panel-production_workbench",
        );
        await expect(panel).toHaveRole("tabpanel");
      },
    );
    await checks.step("tabs_are_at_least_44px", async () => {
      for (const name of ["Production", "Clip editor"]) {
        const box = await tabSize(page, name);
        facts[`${name}_tab`] = [Math.round(box.width), Math.round(box.height)];
        expect(box.width).toBeGreaterThanOrEqual(44);
        expect(box.height).toBeGreaterThanOrEqual(44);
      }
    });
    await checks.step(
      "arrow_home_end_move_focus_without_activation",
      async () => {
        await workbench.focus();
        await page.keyboard.press("ArrowRight");
        await expect(editor).toBeFocused();
        await expect(workbench).toHaveAttribute("aria-selected", "true");
        await expect(panel).toHaveAttribute(
          "data-h3-director-panel",
          "production_workbench",
        );
        await page.keyboard.press("Home");
        await expect(workbench).toBeFocused();
        await page.keyboard.press("End");
        await expect(editor).toBeFocused();
        await page.keyboard.press("ArrowLeft");
        await expect(workbench).toBeFocused();
        await expect(workbench).toHaveAttribute("aria-selected", "true");
      },
    );
    await checks.step("focus_exploration_has_no_effect", async () => {
      expect(await effects(page, oracle)).toEqual(before);
    });
    await checks.step("enter_activates_and_keeps_focus", async () => {
      await page.keyboard.press("ArrowRight");
      await page.keyboard.press("Enter");
      await expect(editor).toHaveAttribute("aria-selected", "true");
      await expect(panel).toHaveAttribute(
        "data-h3-director-panel",
        "clip_editor",
      );
      await expect(editor).toBeFocused();
    });
    await checks.step("tap_activates_the_other_function", async () => {
      await workbench.tap();
      await expect(workbench).toHaveAttribute("aria-selected", "true");
      await expect(panel).toHaveAttribute(
        "data-h3-director-panel",
        "production_workbench",
      );
    });
    await checks.step(
      "inactive_panel_has_no_focusable_descendant",
      async () => {
        expect(await page.locator("[data-h3-director-panel]").count()).toBe(1);
        expect(
          await page.locator('[data-h3-director-panel="clip_editor"]').count(),
        ).toBe(0);
      },
    );
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.function_switch",
      "accessibility",
      checks,
      {
        ...facts,
        effects_before: before,
        effects_after: after,
      },
    );
  });

  test("hardening recovery ui function_switch drafts on both functions survive switching and a remount without an action", async ({
    page,
  }, testInfo) => {
    importFixture = await startImportFixture(page);
    await page.goto(
      "/nleShell.html?import=1&target=ready&segments=2&viewDestroy=1",
    );
    await productionPage(page);
    // The relation control needs exactly one selected segment. Selection is a backend
    // `set_selection` action (setup, before the effect counters start); the controlled checkbox
    // follows the accepted projection.
    const second = page.getByRole("checkbox", {
      name: "Segment 2",
      exact: true,
    });
    await second.click();
    await expect(second).not.toBeChecked();
    const relation = page.getByRole("combobox", {
      name: "Relationship for segment 1",
    });
    await relation.selectOption("adjacent_pair");
    // The Clip editor's draft is an unsent Track-section edit in the full editor, typed after an
    // explicit Open and left behind by an explicit Close.
    await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
    const launcher = page.locator('[data-h3-nle-entry="open"]');
    const order = trackOrder(page);
    await launcher.click();
    await draftSubject(page);
    await order.fill("50");
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect(page.locator(OVERLAY)).toHaveCount(0);
    // The draft subject above cost one import and two transactions; what this row forbids is
    // anything sent by the switching and the remount themselves, counted from here.
    const sentBefore = {
      authoring: [...importFixture.authoringActions],
      production: [...importFixture.productionActions],
      imports: importFixture.importRequests.length,
      transactions: importFixture.transactions.length,
    };
    const before = await shellSnapshot(page);
    const checks = new Checks();
    await checks.step("switching_keeps_both_drafts", async () => {
      await page.getByRole("tab", { name: "Production", exact: true }).click();
      await expect(relation).toHaveValue("adjacent_pair");
      await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
      await launcher.click();
      await expect(order).toHaveValue("50");
      await page.locator('[data-h3-nle-action="close"]').click();
      await expect(page.locator(OVERLAY)).toHaveCount(0);
    });
    await checks.step(
      "view_destroy_and_render_keep_function_and_drafts",
      async () => {
        await page.evaluate(() => window.nleShellHarness.destroyView());
        await expect(page.locator(TABLIST)).toHaveCount(0);
        await page.evaluate(() => window.nleShellHarness.renderView());
        await expect(
          page.getByRole("tab", { name: "Clip editor", exact: true }),
        ).toHaveAttribute("aria-selected", "true");
        // The full editor is never reopened by a remount; only the explicit Open restores it.
        await expect(page.locator(OVERLAY)).toHaveCount(0);
        await launcher.click();
        await expect(order).toHaveValue("50");
        await page.locator('[data-h3-nle-action="close"]').click();
        await page
          .getByRole("tab", { name: "Production", exact: true })
          .click();
        await expect(second).not.toBeChecked();
        await expect(relation).toHaveValue("adjacent_pair");
      },
    );
    const after = await shellSnapshot(page);
    await checks.step("nothing_replayed_or_sent", () => {
      expect(importFixture!.importRequests).toHaveLength(sentBefore.imports);
      expect(importFixture!.transactions).toHaveLength(sentBefore.transactions);
      const newProduction = importFixture!.productionActions.slice(
        sentBefore.production.length,
      );
      expect(
        newProduction.filter((action) => action !== "read_projection"),
      ).toEqual([]);
      const newAuthoring = importFixture!.authoringActions.slice(
        sentBefore.authoring.length,
      );
      expect(
        newAuthoring.filter((action) => action !== "read_timeline_history"),
      ).toEqual([]);
      expect(after.queuedPrompts).toBe(before.queuedPrompts);
      expect(after.renderJobRequests).toBe(before.renderJobRequests);
    });
    await hardeningEvidence(testInfo).row(
      "ui.function_switch",
      "recovery",
      checks,
      {
        production_draft: "relation for segment 1",
        clip_editor_draft: "inspector scale draft",
        remount: "native view destroy then render",
      },
    );
  });

  test("hardening a11y ui global_shell_identity pages are one navigation landmark of 44px buttons with one current page", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      expandOverlay: false,
    });
    const nav = page.getByRole("navigation", NAVIGATION);
    const pageButton = (id: string) =>
      page.locator(`[data-h3-focus-key="page-${id}"]`);
    const before = await effects(page, oracle);
    const checks = new Checks();
    const facts: Record<string, unknown> = {};
    await checks.step(
      "one_owned_navigation_landmark_of_three_pages",
      async () => {
        await expect(nav).toHaveCount(1);
        await expect(
          page.locator('nav.h3n[aria-label="H3 Context pages"]'),
        ).toHaveCount(1);
        await expect(nav.getByRole("button")).toHaveCount(3);
        for (const id of PAGE_IDS) await expect(pageButton(id)).toHaveCount(1);
      },
    );
    await checks.step("page_buttons_are_44px_tab_stops", async () => {
      for (const id of PAGE_IDS) {
        const box = (await pageButton(id).boundingBox())!;
        facts[`page_${id}`] = [Math.round(box.width), Math.round(box.height)];
        expect(box.width).toBeGreaterThanOrEqual(44);
        expect(box.height).toBeGreaterThanOrEqual(44);
        expect(
          await pageButton(id).evaluate(
            (element) =>
              (element as HTMLElement).tabIndex === 0 &&
              element.closest("[inert],[aria-hidden='true']") === null,
          ),
        ).toBe(true);
      }
    });
    await checks.step("exactly_one_current_page", async () => {
      await expect(nav.locator('[aria-current="page"]')).toHaveCount(1);
    });
    await checks.step(
      "enter_moves_the_current_page_and_keeps_focus",
      async () => {
        await pageButton("settings").focus();
        await page.keyboard.press("Enter");
        await expect(pageButton("settings")).toHaveAttribute(
          "aria-current",
          "page",
        );
        await expect(nav.locator('[aria-current="page"]')).toHaveCount(1);
        await expect(pageButton("settings")).toBeFocused();
        await expect(page.locator(TABLIST)).toHaveCount(0);
      },
    );
    await checks.step(
      "tap_activates_production_which_owns_the_tablist",
      async () => {
        await pageButton("production").tap();
        await expect(pageButton("production")).toHaveAttribute(
          "aria-current",
          "page",
        );
        await expect(nav.locator('[aria-current="page"]')).toHaveCount(1);
        await expect(page.locator(TABLIST)).toHaveRole("tablist");
        await expect(page.locator(`${TABLIST} [role="tab"]`)).toHaveCount(2);
        await pageButton("context").tap();
        await expect(pageButton("context")).toHaveAttribute(
          "aria-current",
          "page",
        );
        await expect(page.locator(TABLIST)).toHaveCount(0);
      },
    );
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.global_shell_identity",
      "accessibility",
      checks,
      { ...facts, effects_before: before, effects_after: after },
    );
  });

  test("hardening recovery ui global_shell_identity the selected page and function survive a view destroy and remount", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      expandOverlay: false,
      extraParams: { viewDestroy: "1" },
    });
    const nav = page.getByRole("navigation", NAVIGATION);
    const current = () =>
      nav.locator('[aria-current="page"]').getAttribute("data-page-id");
    const before = await effects(page, oracle);
    const checks = new Checks();
    const remount = async () => {
      await page.evaluate(() => window.nleShellHarness.destroyView());
      await expect(nav).toHaveCount(0);
      await page.evaluate(() => window.nleShellHarness.renderView());
      await expect(nav).toHaveCount(1);
    };
    await checks.step("settings_page_survives_a_remount", async () => {
      await nav.getByRole("button", { name: "Settings" }).click();
      await remount();
      expect(await current()).toBe("settings");
    });
    await checks.step(
      "production_page_and_clip_editor_survive_a_remount",
      async () => {
        await toClipEditor(page);
        await remount();
        expect(await current()).toBe("production");
        await expect(
          page.getByRole("tab", { name: "Clip editor", exact: true }),
        ).toHaveAttribute("aria-selected", "true");
        await expect(page.locator(LAUNCHER)).toBeVisible();
        await expect(page.locator(ROOT)).toHaveCount(0);
      },
    );
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.global_shell_identity",
      "recovery",
      checks,
      {
        remount: "native view destroy then render",
        pages: ["settings", "production"],
        effects_before: before,
        effects_after: after,
      },
    );
  });

  test("hardening a11y ui overlay_open dialog starts at its heading and contains Tab without touching the host", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: toClipEditor,
      expandOverlay: false,
    });
    const launcher = page.locator(LAUNCHER);
    const hostBefore = await hostRoots(page);
    const before = await effects(page, oracle);
    const checks = new Checks();
    const facts: Record<string, unknown> = {};
    await checks.step("keyboard_open_starts_at_the_heading", async () => {
      await launcher.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(OVERLAY)).toHaveAttribute(
        "data-h3-nle-state",
        "expanded",
      );
      await expect(page.locator(HEADING)).toBeFocused();
    });
    await checks.step("one_owned_named_modal_dialog", async () => {
      await expect(page.locator(ROOT)).toHaveCount(1);
      const dialog = page.getByRole("dialog");
      await expect(dialog).toHaveCount(1);
      await expect(dialog).toHaveAttribute("aria-modal", "true");
      await expect(dialog).toHaveAccessibleName(/\S/);
    });
    await checks.step("tab_wraps_inside_in_both_directions", async () => {
      await page.keyboard.press("Shift+Tab");
      expect(await focusInDialog(page)).toBe(true);
      await page.keyboard.press("Tab");
      expect(await focusInDialog(page)).toBe(true);
      // The chrome bar's first control (M25-44: Export, then Close).
      await expect(page.locator(EXPORT_BUTTON)).toBeFocused();
      let presses = 0;
      for (; presses < 40; presses += 1) {
        await page.keyboard.press("Tab");
        expect(await focusInDialog(page)).toBe(true);
      }
      facts.tab_presses_contained = presses + 2;
    });
    await checks.step("no_host_root_made_inert_or_hidden", async () => {
      expect(await hostRoots(page)).toEqual(hostBefore);
    });
    await checks.step(
      "a_press_on_static_dialog_content_keeps_focus_inside",
      async () => {
        // M25-63: the surface status is visually hidden; the save indicator is the chrome bar's
        // static, non-focusable content now.
        await page.locator(`${OVERLAY} .h3-nle-header .h3-nle-save`).click();
        expect(await focusInDialog(page)).toBe(true);
        await page.keyboard.press("Shift+Tab");
        expect(await focusInDialog(page)).toBe(true);
        await page.keyboard.press("Tab");
        expect(await focusInDialog(page)).toBe(true);
      },
    );
    await checks.step("a_tap_reopen_starts_at_the_heading_again", async () => {
      await page.locator(HEADING).focus();
      await page.keyboard.press("Escape");
      await expect(page.locator(ROOT)).toHaveCount(0);
      await launcher.tap();
      await expect(page.locator(HEADING)).toBeFocused();
      await expect(page.locator(ROOT)).toHaveCount(1);
      expect(await hostRoots(page)).toEqual(hostBefore);
    });
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.overlay_open",
      "accessibility",
      checks,
      {
        ...facts,
        host_roots: hostBefore.length,
        effects_before: before,
        effects_after: after,
      },
    );
  });

  test("hardening recovery ui overlay_open an explicit reopen restores the retained editor state paused and replays nothing", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: toClipEditor,
      open: (shell) => shell.locator(LAUNCHER).click(),
    });
    // The draft subject selects a clip, and an accepted selection replaces the monitor's
    // composition, which reopens at frame 0 (defect record D-6, inherited from the accepted
    // base). The position this row retains is therefore taken after the draft, not before it.
    await draftSubject(page);
    await trackOrder(page).fill("52");
    await monitorReady(page);
    const slider = playheadSlider(page);
    await slider.focus();
    await page.keyboard.press("Home");
    await page.keyboard.press("PageUp");
    await page.keyboard.press("PageUp");
    await expect.poll(() => playheadFrame(slider)).toBe(48);
    const initialWidth = (await shellSnapshot(page)).bounds.width;
    await page.locator('[data-h3-nle-action="resize"]').focus();
    await page.keyboard.press("ArrowLeft");
    await expect
      .poll(async () => (await shellSnapshot(page)).bounds.width)
      .toBe(initialWidth - 16);
    const retained = await shellSnapshot(page);
    await expect(trackOrder(page)).toHaveValue("52");
    await page.locator(CLOSE).click();
    await expect(page.locator(ROOT)).toHaveCount(0);
    const before = await effects(page, oracle);
    const checks = new Checks();
    await checks.step("a_close_does_not_reopen_by_itself", async () => {
      await page.waitForTimeout(250);
      await expect(page.locator(ROOT)).toHaveCount(0);
    });
    await openByLauncher(page);
    const reopened = await shellSnapshot(page);
    await checks.step(
      "a_new_generation_opens_only_on_the_explicit_open",
      () => {
        expect(reopened.generation).toBe(retained.generation + 1);
      },
    );
    await checks.step("transport_restores_its_position_paused", async () => {
      await expect.poll(() => playheadFrame(slider)).toBe(48);
      await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
        "Monitor paused.",
      );
    });
    await checks.step("overlay_bounds_restored", () => {
      expect(reopened.bounds).toEqual(retained.bounds);
    });
    await checks.step("unsent_editor_draft_restored", async () => {
      await expect(trackOrder(page)).toHaveValue("52");
    });
    const after = await effects(page, oracle);
    await checks.step("nothing_replayed", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.overlay_open",
      "recovery",
      checks,
      {
        retained_frame: 48,
        retained_bounds: retained.bounds,
        generation_before: retained.generation,
        generation_after: reopened.generation,
      },
    );
  });

  test("hardening a11y ui duplicate_open the real open origin and a backdrop tap keep one dialog and its focus", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: toClipEditor,
      open: (shell) => shell.locator(LAUNCHER).click(),
    });
    const opened = await shellSnapshot(page);
    const launcher = page.locator(LAUNCHER);
    const before = await effects(page, oracle);
    const checks = new Checks();
    let tapTarget: Record<string, unknown> = {};
    await checks.step("launcher_is_a_44px_named_button", async () => {
      await expect(launcher).toHaveRole("button");
      await expect(launcher).toHaveAccessibleName(/\S/);
      const box = (await launcher.boundingBox())!;
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
    });
    await checks.step(
      "a_duplicate_event_at_the_real_open_origin_returns_to_the_one_dialog",
      async () => {
        // The expanded modal physically covers the launcher. Dispatch at the real open
        // origin; clicking its old coordinates could legitimately Add a thumbnail instead.
        await launcher.dispatchEvent("click");
        await expect(page.locator(ROOT)).toHaveCount(1);
        expect((await shellSnapshot(page)).generation).toBe(opened.generation);
        await expect(page.locator(HEADING)).toBeFocused();
      },
    );
    const hitAt = (x: number, y: number) =>
      page.evaluate(
        ({ x, y, root }) => {
          const hit = document.elementFromPoint(x, y);
          return {
            className: hit?.getAttribute("class") ?? null,
            owned: hit !== null && hit.closest(root) !== null,
          };
        },
        { x, y, root: ROOT },
      );
    await checks.step(
      "a_backdrop_tap_is_absorbed_by_the_modal_and_focus_stays_inside",
      async () => {
        // Modal ownership alone does not mean a hit child is navigation-only.
        const point = { x: 4, y: 4 };
        tapTarget = await hitAt(point.x, point.y);
        expect(tapTarget).toEqual({
          className: "h3-nle-backdrop",
          owned: true,
        });
        await page.touchscreen.tap(point.x, point.y);
        await expect(page.locator(ROOT)).toHaveCount(1);
        expect((await shellSnapshot(page)).generation).toBe(opened.generation);
        expect(await focusInDialog(page)).toBe(true);
      },
    );
    await checks.step("a_tap_on_the_backdrop_moves_no_focus", async () => {
      await page.locator(HEADING).focus();
      expect(await hitAt(4, 4)).toEqual({
        className: "h3-nle-backdrop",
        owned: true,
      });
      await page.touchscreen.tap(4, 4);
      await expect(page.locator(ROOT)).toHaveCount(1);
      await expect(page.locator(HEADING)).toBeFocused();
    });
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.duplicate_open",
      "accessibility",
      checks,
      {
        backdrop_point_hit: tapTarget,
        generation: opened.generation,
        effects_before: before,
        effects_after: after,
      },
    );
  });

  test("hardening recovery ui duplicate_open an open racing a pending capability read settles on one generation", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      render: true,
      capabilityDelayMs: 5_000,
      beforeOpen: toClipEditor,
      open: (shell) => shell.locator(LAUNCHER).click(),
    });
    const opened = await shellSnapshot(page);
    const launcher = page.locator(LAUNCHER);
    const region = page.getByRole("region", { name: "Final video" });
    const before = await effects(page, oracle);
    const checks = new Checks();
    await checks.step("the_capability_read_is_outstanding", async () => {
      // The render card lives in the Export popover; with it open, an absent region means the
      // read has not answered, not that the popover is closed.
      await openExportPanel(page);
      expect(opened.capabilityReads).toBe(1);
      expect(opened.renderStatus).not.toBe("read");
      await expect(region).toHaveCount(0);
    });
    await checks.step(
      "repeat_opens_during_the_read_change_nothing",
      async () => {
        for (let attempt = 0; attempt < 3; attempt += 1) {
          await launcher.dispatchEvent("click");
        }
        expect(
          await page.evaluate(() => document.elementFromPoint(4, 4)?.className),
        ).toBe("h3-nle-backdrop");
        await page.touchscreen.tap(4, 4);
        const racing = await shellSnapshot(page);
        expect(racing.generation).toBe(opened.generation);
        expect(racing.capabilityReads).toBe(1);
        expect(racing.mounted).toBe(opened.mounted);
        expect(racing.renderStatus).not.toBe("read");
        await expect(page.locator(ROOT)).toHaveCount(1);
      },
    );
    await checks.step("the_read_settles_on_the_one_generation", async () => {
      // The backdrop tap above is separate from the duplicate events at the open origin.
      await openExportPanel(page);
      await expect(region).toBeVisible({ timeout: 15_000 });
      const settled = await shellSnapshot(page);
      expect(settled.generation).toBe(opened.generation);
      expect(settled.capabilityReads).toBe(1);
      expect(settled.renderStatus).toBe("read");
      expect(settled.renderSupported).toBe(true);
      expect(settled.mounted).toBe(opened.mounted);
      expect(settled.released).toBe(opened.released);
      await expect(page.locator(ROOT)).toHaveCount(1);
    });
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.duplicate_open",
      "recovery",
      checks,
      {
        capability_delay_ms: 5_000,
        repeat_opens: 3,
        backdrop_taps: 1,
        generation: opened.generation,
      },
    );
  });

  test("hardening a11y ui overlay_close_return_focus every close reason lands on its frozen focus target", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: toClipEditor,
      expandOverlay: false,
    });
    const launcher = page.locator(LAUNCHER);
    const before = await effects(page, oracle);
    const checks = new Checks();
    const landed: Record<string, string> = {};
    const closeBy = async (
      reason: string,
      act: () => Promise<void>,
      target: string,
    ) => {
      const recorded = (await shellSnapshot(page)).closeReasons.length;
      await act();
      await expect(page.locator(ROOT)).toHaveCount(0);
      const snapshot = await shellSnapshot(page);
      expect(snapshot.closeReasons.slice(recorded)).toEqual([reason]);
      expect(snapshot.lastCloseReason).toBe(reason);
      await expect(page.locator(target)).toBeFocused();
      landed[reason] = target;
    };
    await checks.step(
      "explicit_close_by_tap_returns_to_the_launcher",
      async () => {
        await openByLauncher(page);
        await closeBy(
          "explicit_close",
          () => page.locator(CLOSE).tap(),
          LAUNCHER_FOCUS,
        );
      },
    );
    await checks.step("escape_returns_to_the_launcher", async () => {
      await launcher.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(HEADING)).toBeFocused();
      await closeBy(
        "escape",
        () => page.keyboard.press("Escape"),
        LAUNCHER_FOCUS,
      );
    });
    await checks.step(
      "function_switch_lands_on_the_activated_tab",
      async () => {
        await openByLauncher(page);
        await closeBy(
          "function_switch",
          async () => {
            await page
              .getByRole("tab", { name: "Production", exact: true })
              .focus();
            await page.keyboard.press("Enter");
          },
          '[data-h3-director-function="production_workbench"]',
        );
      },
    );
    await checks.step(
      "top_level_navigation_lands_on_the_activated_page",
      async () => {
        await page
          .getByRole("tab", { name: "Clip editor", exact: true })
          .click();
        await openByLauncher(page);
        await closeBy(
          "top_level_navigation",
          async () => {
            await page.locator('[data-h3-focus-key="page-context"]').focus();
            await page.keyboard.press("Enter");
          },
          '[data-h3-focus-key="page-context"]',
        );
      },
    );
    await checks.step(
      "capability_or_mount_failure_returns_to_the_launcher",
      async () => {
        await toClipEditor(page);
        await degradeMediaRuntime(page);
        const recorded = (await shellSnapshot(page)).closeReasons.length;
        await launcher.focus();
        await page.keyboard.press("Enter");
        await expect(page.locator(UNAVAILABLE)).toBeVisible();
        const snapshot = await shellSnapshot(page);
        // A refused open mounts nothing, so no close runs; the session records the reason once.
        expect(snapshot.closeReasons.length).toBe(recorded);
        expect(snapshot.lastCloseReason).toBe("capability_or_mount_failure");
        await expect(page.locator(ROOT)).toHaveCount(0);
        await expect(page.locator(LAUNCHER_FOCUS)).toBeFocused();
        landed.capability_or_mount_failure = LAUNCHER_FOCUS;
        await restoreMediaRuntime(page);
      },
    );
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.overlay_close_return_focus",
      "accessibility",
      checks,
      { landed, effects_before: before, effects_after: after },
    );
  });

  test("hardening recovery ui overlay_close_return_focus every close keeps valid drafts and releases every owner before a reopen", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: toClipEditor,
      expandOverlay: false,
    });
    // The draft's subject (clip-0 selected, Basic tab) is accepted timeline and inspector view
    // state: establish it once before the effect counters start and close as a user would.
    await openByLauncher(page);
    await draftSubject(page);
    await page.locator(CLOSE).click();
    await expect(page.locator(ROOT)).toHaveCount(0);
    const before = await effects(page, oracle);
    const checks = new Checks();
    const released: Record<string, unknown> = {};
    const cycles: ReadonlyArray<
      readonly [string, string, () => Promise<void>, () => Promise<void>]
    > = [
      [
        "explicit_close",
        "51",
        () => page.locator(CLOSE).click(),
        async () => {},
      ],
      [
        "escape",
        "52",
        async () => {
          await page.locator(HEADING).focus();
          await page.keyboard.press("Escape");
        },
        async () => {},
      ],
      [
        "function_switch",
        "53",
        async () => {
          await page
            .getByRole("tab", { name: "Production", exact: true })
            .focus();
          await page.keyboard.press("Enter");
        },
        () =>
          page.getByRole("tab", { name: "Clip editor", exact: true }).click(),
      ],
      [
        "top_level_navigation",
        "51",
        async () => {
          await page.locator('[data-h3-focus-key="page-context"]').focus();
          await page.keyboard.press("Enter");
        },
        () => toClipEditor(page),
      ],
    ];
    let previous: string | null = null;
    for (const [reason, draft, close, back] of cycles) {
      await checks.step(
        `${reason}_keeps_the_draft_and_releases_every_owner`,
        async () => {
          await openByLauncher(page);
          if (previous !== null)
            await expect(trackOrder(page)).toHaveValue(previous);
          expect(
            (await shellSnapshot(page)).mediaOwnership.live,
          ).toBeGreaterThan(0);
          await trackOrder(page).fill(draft);
          await close();
          await expect(page.locator(ROOT)).toHaveCount(0);
          expect((await shellSnapshot(page)).lastCloseReason).toBe(reason);
          released[reason] = await ownersReleased(page);
          await back();
          previous = draft;
        },
      );
    }
    await checks.step(
      "the_last_draft_survives_into_a_final_reopen",
      async () => {
        await openByLauncher(page);
        await expect(trackOrder(page)).toHaveValue(previous!);
      },
    );
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.overlay_close_return_focus",
      "recovery",
      checks,
      { released, effects_before: before, effects_after: after },
    );
  });

  test("hardening a11y ui overlay_unavailable_status the unavailable status is announced and focus stays on the launcher", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: toClipEditor,
      expandOverlay: false,
    });
    const launcher = page.locator(LAUNCHER);
    const before = await effects(page, oracle);
    const checks = new Checks();
    const facts: Record<string, unknown> = {};
    await degradeMediaRuntime(page);
    await checks.step(
      "a_keyboard_open_is_refused_into_the_unavailable_status",
      async () => {
        await launcher.focus();
        await page.keyboard.press("Enter");
        await expect(page.locator(UNAVAILABLE)).toBeVisible();
        await expect(page.locator(UNAVAILABLE)).toHaveRole("region");
        await expect(page.locator(ROOT)).toHaveCount(0);
        const snapshot = await shellSnapshot(page);
        expect(snapshot.surfaceStatus).toBe("compact_unsupported");
        expect(snapshot.capabilityFailureDisposition).toBe(
          "media_runtime_unavailable",
        );
        facts.vocabulary = [
          snapshot.capabilityFailureDisposition,
          snapshot.surfaceStatus,
        ];
      },
    );
    await checks.step(
      "the_refusal_is_announced_and_describes_the_launcher",
      async () => {
        const status = page.locator(
          `${UNAVAILABLE} [data-h3-nle-status="surface"]`,
        );
        await expect(status).toHaveRole("status");
        await expect(status).toHaveAttribute("aria-live", "polite");
        await expect(status).toHaveText(/\S/);
        expect(await launcher.getAttribute("aria-describedby")).toBe(
          await status.getAttribute("id"),
        );
      },
    );
    await checks.step(
      "no_editing_surface_stands_in_for_the_editor",
      async () => {
        // One NLE: the refusal offers no compact editor or source-only fallback surface.
        await expect(page.locator("[data-h3-nle-fallback]")).toHaveCount(0);
        await expect(
          page.getByRole("region", { name: "Reference & timeline authoring" }),
        ).toHaveCount(0);
      },
    );
    await checks.step("focus_stays_on_the_launcher", async () => {
      await expect(page.locator(LAUNCHER_FOCUS)).toBeFocused();
    });
    await checks.step("a_tap_retry_is_refused_the_same_way", async () => {
      const box = (await launcher.boundingBox())!;
      facts.launcher = [Math.round(box.width), Math.round(box.height)];
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
      await launcher.tap();
      await expect(page.locator(ROOT)).toHaveCount(0);
      await expect(page.locator(UNAVAILABLE)).toBeVisible();
      await expect(page.locator(LAUNCHER_FOCUS)).toBeFocused();
    });
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.overlay_unavailable_status",
      "accessibility",
      checks,
      { ...facts, effects_before: before, effects_after: after },
    );
  });

  test("hardening recovery ui overlay_unavailable_status a refused open keeps the retained drafts and a later supported open works", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: toClipEditor,
      open: (shell) => shell.locator(LAUNCHER).click(),
    });
    await draftSubject(page);
    await trackOrder(page).fill("53");
    await page.locator(CLOSE).click();
    await ownersReleased(page);
    const closed = await shellSnapshot(page);
    const before = await effects(page, oracle);
    const checks = new Checks();
    await checks.step(
      "a_refused_open_mounts_and_acquires_nothing",
      async () => {
        await degradeMediaRuntime(page);
        await page.locator(LAUNCHER).click();
        await expect(page.locator(UNAVAILABLE)).toBeVisible();
        await expect(page.locator(ROOT)).toHaveCount(0);
        const refused = await shellSnapshot(page);
        expect(refused.surfaceStatus).toBe("compact_unsupported");
        expect(refused.generation).toBe(closed.generation);
        expect(refused.mediaOwnership.acquired).toBe(
          closed.mediaOwnership.acquired,
        );
      },
    );
    await checks.step(
      "a_later_supported_open_works_with_the_draft",
      async () => {
        await restoreMediaRuntime(page);
        await openByLauncher(page);
        const reopened = await shellSnapshot(page);
        expect(reopened.generation).toBe(closed.generation + 1);
        await expect(trackOrder(page)).toHaveValue("53");
        await expect(page.locator(UNAVAILABLE)).toHaveCount(0);
      },
    );
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.overlay_unavailable_status",
      "recovery",
      checks,
      {
        draft: "inspector scale draft",
        generation_before: closed.generation,
      },
    );
  });

  test("hardening a11y ui view_destroy_cleanup a native close delegates focus to the host without detached targets", async ({
    page,
  }, testInfo) => {
    const oracle = await openIntegratedShell(page, "smoke", {
      beforeOpen: toClipEditor,
      open: (shell) => shell.locator(LAUNCHER).click(),
      extraParams: { viewDestroy: "1" },
    });
    const opened = await shellSnapshot(page);
    const before = await effects(page, oracle);
    const checks = new Checks();
    const facts: Record<string, unknown> = {};
    await checks.step("focus_starts_inside_the_dialog", async () => {
      await page.locator(HEADING).focus();
      expect(await focusInDialog(page)).toBe(true);
    });
    const started = Date.now();
    await page.evaluate(() => window.nleShellHarness.destroyView());
    await checks.step("the_owned_root_and_view_are_gone", async () => {
      await expect(page.locator(ROOT)).toHaveCount(0);
      await expect(page.getByRole("navigation", NAVIGATION)).toHaveCount(0);
      const destroyed = await shellSnapshot(page);
      expect(destroyed.lastCloseReason).toBe("view_destroy");
      expect(destroyed.surfaceStatus).toBe("compact_ready");
      expect(destroyed.generation).toBe(opened.generation);
    });
    await checks.step("focus_is_the_hosts_and_never_detached", async () => {
      const focus = await page.evaluate(() => {
        const active = document.activeElement;
        return {
          connected: active !== null && active.isConnected,
          owned:
            active?.closest("[data-h3-nle-root],[data-h3-focus-key]") !==
              null && active !== null,
          onBody: active === document.body,
        };
      });
      facts.focus = focus;
      expect(focus.connected).toBe(true);
      expect(focus.owned).toBe(false);
      await page.keyboard.press("Tab");
      expect(
        await page.evaluate(() => document.activeElement?.isConnected ?? true),
      ).toBe(true);
    });
    await checks.step("every_owner_released", async () => {
      facts.owners = await ownersReleased(page);
      facts.release_ms = Date.now() - started;
    });
    const after = await effects(page, oracle);
    await checks.step("zero_unrequested_effects", () => {
      expectNoEffects(before, after);
    });
    await hardeningEvidence(testInfo).row(
      "ui.view_destroy_cleanup",
      "accessibility",
      checks,
      { ...facts, effects_before: before, effects_after: after },
    );
  });

  test("hardening recovery ui view_destroy_cleanup a late reply after a native close cannot revive the destroyed view", async ({
    page,
  }, testInfo) => {
    let gate: Promise<void> | undefined;
    const oracle = await openIntegratedShell(page, "smoke", {
      render: true,
      capabilityDelayMs: 8_000,
      beforeOpen: toClipEditor,
      open: (shell) => shell.locator(LAUNCHER).click(),
      extraParams: { viewDestroy: "1" },
      reply: async () => {
        await gate;
        return "fulfill" as const;
      },
    });
    // The held capability read started when the overlay mounted, before this point.
    const openedAt = Date.now();
    const fixtureReplies = { delivered: 0, aborted: 0 };
    page.on("requestfinished", (request) => {
      if (request.url().includes("/__nle_fixture/"))
        fixtureReplies.delivered += 1;
    });
    page.on("requestfailed", (request) => {
      if (request.url().includes("/__nle_fixture/"))
        fixtureReplies.aborted += 1;
    });
    await page
      .locator(
        '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
      )
      .click();
    await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
    let release = () => {};
    gate = new Promise((resolve) => {
      release = resolve;
    });
    const sent = oracle.transactions.length;
    await openClipMenu(page, "clip-0");
    await page.locator('[data-h3-nle-control="clip.enabled"]').click();
    await expect
      .poll(async () => (await shellSnapshot(page)).authoringStatus)
      .toBe("pending");
    const opened = await shellSnapshot(page);
    const replies = { ...fixtureReplies };
    await page.evaluate(() => window.nleShellHarness.destroyView());
    const container = page.locator("#h3-shell-sidebar-mount");
    const checks = new Checks();
    await checks.step(
      "the_view_is_destroyed_with_both_replies_outstanding",
      async () => {
        expect(opened.renderStatus).not.toBe("read");
        await expect(page.locator(ROOT)).toHaveCount(0);
        expect(
          await container.evaluate((element) => element.childElementCount),
        ).toBe(0);
      },
    );
    gate = undefined;
    release();
    await checks.step(
      "the_late_replies_land_without_reviving_the_view",
      async () => {
        await expect
          .poll(() => fixtureReplies.delivered + fixtureReplies.aborted)
          .toBeGreaterThan(replies.delivered + replies.aborted);
        // Outlast the held capability read so its late completion has landed too.
        await page.waitForTimeout(Math.max(0, openedAt + 8_500 - Date.now()));
        await expect(page.locator(ROOT)).toHaveCount(0);
        expect(
          await container.evaluate((element) => element.childElementCount),
        ).toBe(0);
        const late = await shellSnapshot(page);
        expect(late.surfaceStatus).toBe("compact_ready");
        expect(late.generation).toBe(opened.generation);
        expect(late.lastCloseReason).toBe("view_destroy");
        expect(late.mediaOwnership.live).toBe(0);
        expect(late.renderJobRequests).toBe(0);
        expect(oracle.transactions).toHaveLength(sent + 1);
      },
    );
    await checks.step(
      "a_remount_needs_an_explicit_open_and_shows_the_accepted_state",
      async () => {
        await page.evaluate(() => window.nleShellHarness.renderView());
        await expect(page.locator(LAUNCHER)).toBeVisible();
        await expect(page.locator(ROOT)).toHaveCount(0);
        await openByLauncher(page);
        await expect(
          page.locator('[data-h3-nle-clip="clip-0"]'),
        ).toHaveAttribute("data-enabled", "false");
        expect(oracle.transactions).toHaveLength(sent + 1);
      },
    );
    await hardeningEvidence(testInfo).row(
      "ui.view_destroy_cleanup",
      "recovery",
      checks,
      {
        late_replies: {
          delivered: fixtureReplies.delivered - replies.delivered,
          aborted_by_client: fixtureReplies.aborted - replies.aborted,
        },
        capability_delay_ms: 8_000,
        transactions: oracle.transactions.length - sent,
      },
    );
  });
}
