// M25-20 F1 corrective, browser half: real per-row observation for the shell-invariant corpus
// rows that `nleSemanticConformance.spec.ts` does not already exercise on the real integrated
// shell (`overlay_open`, four of the five `overlay_close_return_focus` reasons,
// `overlay_unavailable_status`, `view_destroy_cleanup`). `global_shell_identity`,
// `function_switch` (activation) and `duplicate_open` are already exercised there, and
// `overlay_close_return_focus.escape` is already exercised there too; this file adds only what
// was still merely cited. Every case below is driven through the product's own real affordance
// (or, where none exists at all -- see the `overlay_unavailable_status` test -- through the
// closest reachable real gesture, disclosed as a fact rather than hidden) and records its result
// through `recordShellObservation`.

import { expect, test, type Page } from "@playwright/test";

import { recordShellObservation } from "../helpers/nleSemanticShellEvidence";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

const OVERLAY = '[data-h3-nle-surface="overlay_v1"]';
// The product's own launcher, distinct from the harness page's convenience button.
const LAUNCHER = '[data-h3-nle-entry="open"]';
const ASSET_BIN = '[data-h3-nle-region="asset-bin"]';
// M25-44 (one NLE): the launcher's unavailable region carries the surface status alone.
const UNAVAILABLE_STATUS =
  '[data-h3-nle-unavailable="overlay_v1"] [data-h3-nle-status="surface"]';
const NAVIGATION = { name: "H3 Context pages" } as const;

async function toClipEditor(page: Page): Promise<void> {
  await page
    .getByRole("navigation", NAVIGATION)
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor" }).click();
  await expect(page.getByRole("tab", { name: "Clip editor" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
}

/** Boots the real integrated shell on the Clip editor function and opens through the real launcher. */
async function bootAndOpen(page: Page) {
  return openIntegratedShell(page, "smoke", {
    beforeOpen: toClipEditor,
    open: (shell) => shell.locator(LAUNCHER).click(),
  });
}

function activeElementFocusKey(page: Page): Promise<string | null> {
  return page.evaluate(() =>
    document.activeElement instanceof HTMLElement
      ? (document.activeElement.getAttribute("data-h3-focus-key") ?? null)
      : null,
  );
}

test("opens with a fresh generation and the assets pane already visible", async ({
  page,
}, testInfo) => {
  await bootAndOpen(page);
  await expect(page.locator(OVERLAY)).toBeVisible();
  await expect(page.locator(ASSET_BIN)).toBeVisible();
  const snapshot = await shellSnapshot(page);
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.overlay_open",
    executed: true,
    facts: {
      generation: snapshot.generation,
      surfaceStatus: snapshot.surfaceStatus,
      assetsPaneVisible: true,
    },
    missing: [],
  });
  expect(snapshot.generation).toBe(1);
  expect(snapshot.surfaceStatus).toBe("expanded");
});

test("returns focus to the launcher after explicit_close", async ({
  page,
}, testInfo) => {
  await bootAndOpen(page);
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  const snapshot = await shellSnapshot(page);
  const launcherFocused = await page
    .locator(LAUNCHER)
    .evaluate((element) => element === document.activeElement);
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.overlay_close_return_focus.explicit_close",
    executed: true,
    facts: {
      closeReason: snapshot.closeReasons.at(-1) ?? null,
      launcherFocused,
    },
    missing: [],
  });
  expect(snapshot.closeReasons.at(-1)).toBe("explicit_close");
  expect(launcherFocused).toBe(true);
});

test("leaves focus on the newly activated function tab after function_switch", async ({
  page,
}, testInfo) => {
  await bootAndOpen(page);
  const productionTab = page.getByRole("tab", { name: "Production" });
  // The open overlay's own full-viewport modal backdrop (`role="dialog" aria-modal="true"`)
  // genuinely covers the sidebar's tablist for a pointer, exactly as a correctly built modal
  // should; a real user reaches a covered control with the keyboard, not by clicking through an
  // opaque layer. `.focus()` is the real DOM focus API (not a synthetic click), and browsers
  // dispatch a real click for Enter/Space on a focused native control themselves.
  await productionTab.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  const snapshot = await shellSnapshot(page);
  const tabFocused = await productionTab.evaluate(
    (element) => element === document.activeElement,
  );
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.overlay_close_return_focus.function_switch",
    executed: true,
    facts: {
      closeReason: snapshot.closeReasons.at(-1) ?? null,
      productionTabFocused: tabFocused,
      launcherStillPresent: (await page.locator(LAUNCHER).count()) > 0,
      activation: "keyboard_focus_enter",
      pointerReachable: false,
    },
    missing: [],
  });
  expect(snapshot.closeReasons.at(-1)).toBe("function_switch");
  // The launcher only renders under the clip_editor function; leaving it means there is no
  // launcher left to have stolen focus back from the tab the user just activated.
  expect(tabFocused).toBe(true);
});

test("leaves focus on the newly selected top-level page after top_level_navigation", async ({
  page,
}, testInfo) => {
  await bootAndOpen(page);
  const contextButton = page
    .getByRole("navigation", NAVIGATION)
    .getByRole("button", { name: "Context" });
  // Same real-modal-backdrop fact as the function_switch case above: the top-level nav sits
  // behind the open overlay for a pointer, so this reaches it by keyboard.
  await contextButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  await expect(contextButton).toHaveAttribute("aria-current", "page");
  const snapshot = await shellSnapshot(page);
  const focusKey = await activeElementFocusKey(page);
  const focusedOnBody = await page.evaluate(
    () => document.activeElement === document.body,
  );
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.overlay_close_return_focus.top_level_navigation",
    executed: true,
    facts: {
      closeReason: snapshot.closeReasons.at(-1) ?? null,
      focusKey,
      focusedOnBody,
      activation: "keyboard_focus_enter",
      pointerReachable: false,
    },
    missing: [],
  });
  expect(snapshot.closeReasons.at(-1)).toBe("top_level_navigation");
  // Never claim the launcher destination: that half of the policy is reserved for
  // explicit_close/escape/capability_or_mount_failure, and a page navigation must not steal focus
  // back to a control that no longer exists on the newly selected page.
  expect(focusedOnBody).toBe(false);
});

test("reports the unavailable status when the browser media runtime is unsupported", async ({
  page,
}, testInfo) => {
  // A genuine browser-environment condition (not a product hook): `NleLauncher` and
  // `observeBrowserMediaCapabilities` both consult these same real DOM APIs, so overriding them
  // reproduces exactly what an actually unsupported browser would report.
  await page.addInitScript(() => {
    Object.defineProperty(HTMLVideoElement.prototype, "canPlayType", {
      configurable: true,
      value: () => "",
    });
    const originalGetContext = HTMLCanvasElement.prototype.getContext;
    Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
      configurable: true,
      value: function nleE2eGetContext(
        this: HTMLCanvasElement,
        type: string,
        ...rest: unknown[]
      ) {
        if (type === "2d") return null;
        return (
          originalGetContext as (
            this: HTMLCanvasElement,
            type: string,
            ...rest: unknown[]
          ) => unknown
        ).apply(this, [type, ...rest]);
      },
    });
  });
  await page.goto("/nleShell.html?shape=smoke");
  await toClipEditor(page);
  await expect(page.locator(UNAVAILABLE_STATUS)).toBeVisible();
  await expect(page.locator(UNAVAILABLE_STATUS)).toHaveRole("status");
  await expect(page.locator(LAUNCHER)).toHaveCount(0);
  // One NLE: no compact or source-only editing surface stands in for the refused editor.
  await expect(page.locator("[data-h3-nle-fallback]")).toHaveCount(0);
  // No product launcher renders in this disposition (`NleLauncher` never mounts the button when
  // unsupported), so the only real DOM control that still reaches `nleOpenOverlay()` is the
  // harness's own "Open full editor" button -- a real click dispatched through React's real
  // handler, disclosed here rather than substituted silently for the missing product affordance.
  await page.getByRole("button", { name: "Open full editor" }).click();
  const snapshot = await shellSnapshot(page);
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.overlay_unavailable_status",
    executed: true,
    facts: {
      statusVisible: true,
      launcherPresent: false,
      surfaceStatus: snapshot.surfaceStatus,
      capabilityDisposition: snapshot.capabilityDisposition,
      capabilityFailureDisposition: snapshot.capabilityFailureDisposition,
      openTrigger: "harness_convenience_button",
    },
    missing: [],
  });
  expect(snapshot.surfaceStatus).toBe("compact_unsupported");
  expect(snapshot.capabilityFailureDisposition).toBe(
    "media_runtime_unavailable",
  );
});

test("records capability_or_mount_failure and the focus destination it actually reaches", async ({
  page,
}, testInfo) => {
  await page.goto("/nleShell.html?shape=smoke");
  await toClipEditor(page);
  const launcher = page.locator(LAUNCHER);
  await expect(launcher).toBeVisible();
  await launcher.focus();
  // Degrade the environment only now, after the real product launcher already rendered and is
  // focused: the click below is a real interaction with a real, currently-present control, and
  // the capability it observes is re-evaluated fresh at click time (`nleOpenOverlay` calls
  // `observeBrowserMediaCapabilities()` itself; nothing here calls the internal action directly).
  await page.evaluate(() => {
    Object.defineProperty(HTMLVideoElement.prototype, "canPlayType", {
      configurable: true,
      value: () => "",
    });
  });
  await launcher.click();
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  const snapshot = await shellSnapshot(page);
  const launcherStillPresent = (await launcher.count()) > 0;
  const focusKey = await activeElementFocusKey(page);
  const focusedOnBody = await page.evaluate(
    () => document.activeElement === document.body,
  );
  // The frozen destination for this reason is the launcher the gesture came from (B-63: the
  // launcher used to unmount on the refusal that answered its own click, and focus was dropped
  // on `document.body`). A missing observation is recorded as missing, never as passing.
  const missing: string[] = [];
  if (launcherStillPresent !== true)
    missing.push(
      "returns_focus_to_launcher: the launcher is no longer in the document after the refusal",
    );
  else if (focusKey !== "nle-open-overlay")
    missing.push(
      `returns_focus_to_launcher: focus is on ${focusKey ?? (focusedOnBody ? "document.body" : "an unkeyed element")}, not the launcher`,
    );
  await recordShellObservation(testInfo, {
    case_id:
      "ui_invariant.overlay_close_return_focus.capability_or_mount_failure",
    executed: true,
    facts: {
      surfaceStatus: snapshot.surfaceStatus,
      capabilityFailureDisposition: snapshot.capabilityFailureDisposition,
      launcherStillPresent,
      focusKey,
      focusedOnBody,
    },
    missing,
  });
  expect(snapshot.surfaceStatus).toBe("compact_unsupported");
  expect(snapshot.capabilityFailureDisposition).toBe(
    "media_runtime_unavailable",
  );
  expect(missing).toEqual([]);
  // The unavailable status is presented alongside the surviving launcher, and the launcher is
  // still a real control: a later open re-observes the capability.
  await expect(page.locator(UNAVAILABLE_STATUS)).toBeVisible();
  await expect(launcher).toBeEnabled();
});

test("resets geometry, pane and records view_destroy exactly once on a real native sidebar close", async ({
  page,
}, testInfo) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: toClipEditor,
    open: (shell) => shell.locator(LAUNCHER).click(),
    extraParams: { viewDestroy: "1" },
  });
  const opened = await shellSnapshot(page);
  expect(opened.surfaceStatus).toBe("expanded");
  // The real "native sidebar close" callback the fixture's `extensionManager` gives back from
  // `registerSidebarTab` -- the same seam `createSidebarHost` wires ComfyUI's own tab-close
  // gesture to -- not a direct call into the session's internal action.
  await page.evaluate(() => window.nleShellHarness.destroyView());
  const destroyed = await shellSnapshot(page);
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.view_destroy_cleanup",
    executed: true,
    facts: {
      surfaceStatus: destroyed.surfaceStatus,
      lastCloseReason: destroyed.lastCloseReason,
      bounds: JSON.stringify(destroyed.bounds),
      generation: destroyed.generation,
    },
    missing: [],
  });
  expect(destroyed.surfaceStatus).toBe("compact_ready");
  expect(destroyed.lastCloseReason).toBe("view_destroy");
  expect(destroyed.bounds).toEqual({ width: 0, height: 0 });
  expect(destroyed.generation).toBe(opened.generation);
});
