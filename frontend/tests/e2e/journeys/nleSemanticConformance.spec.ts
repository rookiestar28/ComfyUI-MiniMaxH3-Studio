// M25-20 semantic conformance journey, on the REAL integrated shell.
//
// This is the browser half of the preview/final comparison. It starts from the one host tab and the
// real three-page shell, activates `clip_editor` by hand, opens exactly one `overlay_v1`, drives a
// real canonical edit through the real authoring client into the real Python core, and then reads
// landmarks off the canvas the real monitor presented to — only after the transport says the frame
// is presented. It closes the overlay and observes the frozen return-focus destination.
//
// What this journey is not: it is not the conformance report. It proves that the browser side can
// be observed semantically on the accepted runtime, which is the precondition the plan sets before
// the corpus may be run. A DOM-presence or screenshot assertion cannot stand in for that, and if
// this journey cannot execute, M25-20 is BLOCKED rather than passed with a smaller claim.

import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { grip } from "../helpers/nleCanonical";
import { recordShellObservation } from "../helpers/nleSemanticShellEvidence";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

const OVERLAY = '[data-h3-nle-surface="overlay_v1"]';
// The product's own launcher, distinct from the harness page's convenience button.
const LAUNCHER = '[data-h3-nle-entry="open"]';
// The monitor's own canvas, by its role marker: since M25-49 the media bin's card art canvases
// precede the monitor in document order, so the first canvas under the overlay surface is a
// zero-backing thumbnail, not the presented composition.
const MONITOR_CANVAS =
  '[data-h3-nle-surface="overlay_v1"] canvas[data-h3-nle-canvas="composition"]';

/** The global shell identity row: three ordered pages, exactly one current, two functions. */
async function assertGlobalShellIdentity(
  page: Page,
  testInfo: TestInfo,
): Promise<void> {
  const navigation = page.getByRole("navigation", { name: "H3 Context pages" });
  await expect(navigation).toHaveCount(1);
  const pageIds = await navigation
    .locator("button[data-page-id]")
    .evaluateAll((buttons) =>
      buttons.map((button) => button.getAttribute("data-page-id")),
    );
  expect(pageIds).toEqual(["context", "production", "settings"]);
  // Exactly one current page. Two would make "which page am I on" unanswerable for assistive
  // technology, and zero would make the shell's own state unobservable.
  const currentCount = await navigation
    .locator('[aria-current="page"]')
    .count();
  expect(currentCount).toBe(1);
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.global_shell_identity",
    executed: true,
    facts: {
      pageIds: pageIds.join(","),
      currentPageCount: currentCount,
      overlayPresentBeforeActivation: false,
    },
    missing: [],
  });
}

/** Enter Production and activate the clip editor with the keyboard, not by mounting it. */
async function activateClipEditor(
  page: Page,
  testInfo: TestInfo,
): Promise<void> {
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  const tablist = page.getByRole("tablist", { name: "Production functions" });
  const functionIds = await tablist
    .locator("[role=tab]")
    .evaluateAll((tabs) =>
      tabs.map((tab) => tab.getAttribute("data-h3-director-function")),
    );
  expect(functionIds).toEqual(["production_workbench", "clip_editor"]);
  const workbench = tablist.getByRole("tab", { name: "Production" });
  const clipEditor = tablist.getByRole("tab", { name: "Clip editor" });
  await expect(workbench).toHaveAttribute("aria-selected", "true");
  await workbench.focus();
  await page.keyboard.press("ArrowRight");
  await expect(clipEditor).toBeFocused();
  // Roving focus alone must not switch the function: the activation is the Enter, not the arrow.
  await expect(workbench).toHaveAttribute("aria-selected", "true");
  const arrowActivated = await clipEditor.getAttribute("aria-selected");
  await page.keyboard.press("Enter");
  await expect(clipEditor).toHaveAttribute("aria-selected", "true");
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.function_switch",
    executed: true,
    facts: {
      functionIds: functionIds.join(","),
      arrowAloneActivated: arrowActivated === "true",
      enterActivated: true,
    },
    missing: [],
  });
}

test("the accepted runtime presents an edited composition that can be observed semantically", async ({
  page,
}, testInfo) => {
  const forbidden: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      request.method() === "POST" &&
      (path === "/prompt" || path === "/queue")
    ) {
      forbidden.push(path);
    }
  });

  const oracle = await openIntegratedShell(page, "smoke", {
    beforeOpen: async (shell) => {
      await assertGlobalShellIdentity(shell, testInfo);
      await activateClipEditor(shell, testInfo);
      // The launcher appears with the function, not with the page: an overlay that could be
      // reached before the user activated the clip editor would not be an explicit launcher.
      await expect(shell.locator(LAUNCHER)).toHaveCount(1);
    },
    open: async (shell) => shell.locator(LAUNCHER).click(),
  });

  // Exactly one overlay, and opening again is idempotent rather than additive.
  await expect(page.locator(OVERLAY)).toHaveCount(1);
  const openedGeneration = (await shellSnapshot(page)).generation;
  await page.evaluate(() => window.nleShellHarness.open());
  await expect(page.locator(OVERLAY)).toHaveCount(1);
  const duplicateOpenGeneration = (await shellSnapshot(page)).generation;
  expect(duplicateOpenGeneration).toBe(openedGeneration);
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.duplicate_open",
    executed: true,
    facts: {
      openedGeneration,
      generationAfterSecondOpen: duplicateOpenGeneration,
      overlayCountAfterSecondOpen: 1,
    },
    missing: [],
  });

  // A real canonical edit through the real client into the real core.
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  const before = (await shellSnapshot(page)).timelineSnapshot!;
  const beforeClip = before.clips.find((clip) => clip.clipId === "clip-0")!;

  const handle = page.locator(grip).filter({ visible: true }).first();
  await handle.focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);

  const after = (await shellSnapshot(page)).timelineSnapshot!;
  const afterClip = after.clips.find((clip) => clip.clipId === "clip-0")!;
  expect(afterClip.durationFrames).toBe(beforeClip.durationFrames - 1);
  // The render input moved, so the composition identity must have moved with it. A changed
  // revision alone would not prove that; the content fingerprint is what the renderer consumes.
  expect(after.publicFingerprint).not.toBe(before.publicFingerprint);

  // The monitor presents to a real canvas with real backing dimensions. Landmarks are read only
  // after the transport reports a presented frame — reading a target frame or a receipt instead
  // would measure an intention rather than an observation.
  // M25-63: the monitor status is a diagnostic held in the top bar, not shown; the monitor
  // publishing it is the fact, so it is read as attached with its text.
  await expect(
    page.locator('[data-h3-nle-status="monitor"]').first(),
  ).toBeAttached();
  await expect(
    page.locator('[data-h3-nle-status="monitor"]').first(),
  ).toHaveText(/\S/);
  const canvas = page.locator(MONITOR_CANVAS).first();
  await expect(canvas).toBeVisible();
  const backing = await canvas.evaluate((element) => {
    const target = element as HTMLCanvasElement;
    return { width: target.width, height: target.height };
  });
  expect(backing.width).toBeGreaterThan(0);
  expect(backing.height).toBeGreaterThan(0);

  const measure = async () =>
    canvas.evaluate((element) => {
      const target = element as HTMLCanvasElement;
      const context = target.getContext("2d", { willReadFrequently: true });
      if (context === null) return null;
      const image = context.getImageData(0, 0, target.width, target.height);
      let opaque = 0;
      const distinct = new Set<string>();
      for (let index = 0; index < image.data.length; index += 4) {
        if (image.data[index + 3] > 0) opaque += 1;
        if (distinct.size < 64) {
          distinct.add(
            `${image.data[index]},${image.data[index + 1]},${image.data[index + 2]}`,
          );
        }
      }
      return { opaque, distinct: distinct.size, total: image.data.length / 4 };
    });

  // CRITICAL: poll until the canvas actually carries a presented frame. Sampling once, right after
  // the element becomes visible, reads the cleared surface before the first composited frame lands
  // and yields a single flat colour — which would then be compared as though it were the render.
  // The bound is the assertion: if presentation never happens the journey fails, and M25-20 is
  // BLOCKED rather than passed on an empty observation.
  await expect
    .poll(async () => (await measure())?.distinct ?? 0, { timeout: 20_000 })
    .toBeGreaterThan(1);
  const sample = await measure();
  expect(sample).not.toBeNull();
  expect(sample!.opaque).toBeGreaterThan(0);

  // Navigation and observation started no generation work of any kind.
  expect(forbidden).toEqual([]);
  expect((await shellSnapshot(page)).queuedPrompts).toBe(0);
  expect((await shellSnapshot(page)).renderJobRequests).toBe(0);
  expect(oracle.transactions).toHaveLength(2);

  // Close and observe the frozen return-focus destination. Escape is handled by the dialog, so
  // focus has to be inside it — after a committed trim the grip's row has re-rendered and focus is
  // no longer guaranteed to be there. Placing it on a real control first keeps this a close test
  // rather than an accidental test of where focus happened to land.
  const launcher = page.locator(LAUNCHER);
  await page.locator('[data-h3-nle-action="close"]').focus();
  await expect(page.locator('[data-h3-nle-action="close"]')).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  const closed = await shellSnapshot(page);
  expect(closed.closeReasons.at(-1)).toBe("escape");
  const launcherFocusedAfterEscape = await launcher.evaluate(
    (element) => element === document.activeElement,
  );
  await expect(launcher).toBeFocused();
  // Closing releases what it mounted; a leaked owner is what makes a second open unobservable.
  expect(closed.released).toBe(closed.mounted);
  await recordShellObservation(testInfo, {
    case_id: "ui_invariant.overlay_close_return_focus.escape",
    executed: true,
    facts: {
      closeReason: closed.closeReasons.at(-1) ?? null,
      launcherFocused: launcherFocusedAfterEscape,
    },
    missing: [],
  });
});
