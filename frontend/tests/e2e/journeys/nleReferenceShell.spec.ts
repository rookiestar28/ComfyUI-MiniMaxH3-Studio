// M25-44: the reference NLE shell on the REAL integrated shell (`frontend/e2e/nleShell.tsx`),
// measured in the browser's layout engine. Four regions -- media bin, preview monitor and
// inspector over a full-width timeline -- separated by three splitters; the final-video card in the
// chrome bar's Export popover; one editing surface. Expected proportions come from the master
// plan's reference measurements (R1 21.0 %, R2 52.5 %, R3 25.6 % of the width; top band 62.2 % of
// the height), never from the geometry module under test.

import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  expectGripClearOfLanes,
  nleFineGripException,
  nleTargetFloor,
} from "../helpers/nleTargets";
import { NLE_REFERENCE_UI_CONTRACT_V1 } from "../../../src/contracts/nleReferenceUiContract";

import {
  EXPORT_BUTTON,
  EXPORT_POPOVER,
  openExportPanel,
} from "../helpers/nleExport";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

const OVERLAY = '[data-h3-nle-surface="overlay_v1"]';
const ROOT = "[data-h3-nle-root]";
const LAUNCHER = '[data-h3-nle-entry="open"]';
const LAUNCHER_FOCUS = '[data-h3-focus-key="nle-open-overlay"]';
const CLOSE = '[data-h3-nle-action="close"]';
const NAVIGATION = { name: "H3 Context pages" } as const;
const AREAS = ["bin", "monitor", "inspector", "timeline"] as const;
type Area = (typeof AREAS)[number];
type Splitter = "bin_monitor" | "monitor_inspector" | "top_timeline";
type Box = Readonly<{ x: number; y: number; width: number; height: number }>;
type Layout = Readonly<
  Record<Area, Box> & {
    dialog: Box;
    stage: Box &
      Readonly<{
        clientWidth: number;
        clientHeight: number;
        scrollWidth: number;
        scrollHeight: number;
      }>;
    tier: string | null;
    areaCount: number;
    paneModes: number;
  }
>;

function splitter(page: Page, id: Splitter): Locator {
  return page.locator(`${OVERLAY} [data-h3-nle-splitter="${id}"]`);
}

function gutter(page: Page, id: Splitter): Locator {
  return page.locator(`${OVERLAY} [data-h3-nle-gutter="${id}"]`);
}

/** Every region box, the dialog and the stage, read from the live layout in one pass. */
function measure(page: Page): Promise<Layout> {
  return page.evaluate((overlay) => {
    const dialog = document.querySelector<HTMLElement>(overlay)!;
    const box = (element: Element) => {
      const rect = element.getBoundingClientRect();
      return {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      };
    };
    const area = (id: string) =>
      box(dialog.querySelector(`[data-h3-nle-area="${id}"]`)!);
    const stage = dialog.querySelector<HTMLElement>(".h3-nle-stage")!;
    return {
      dialog: box(dialog),
      stage: {
        ...box(stage),
        clientWidth: stage.clientWidth,
        clientHeight: stage.clientHeight,
        scrollWidth: stage.scrollWidth,
        scrollHeight: stage.scrollHeight,
      },
      bin: area("bin"),
      monitor: area("monitor"),
      inspector: area("inspector"),
      timeline: area("timeline"),
      tier: dialog
        .querySelector("[data-h3-nle-shell]")!
        .getAttribute("data-h3-nle-tier"),
      areaCount: dialog.querySelectorAll("[data-h3-nle-area]").length,
      paneModes: document.querySelectorAll("[data-pane-mode]").length,
    };
  }, OVERLAY);
}

async function toClipEditor(page: Page) {
  await page
    .getByRole("navigation", NAVIGATION)
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
  await expect(page.locator(LAUNCHER)).toBeVisible();
}

/** A real mouse drag from the splitter's centre along its axis. */
async function drag(page: Page, id: Splitter, delta: number) {
  const box = (await splitter(page, id).boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  const horizontal = id === "top_timeline";
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(
    horizontal ? x : x + delta,
    horizontal ? y + delta : y,
    { steps: 6 },
  );
  await page.mouse.up();
}

const near = (actual: number, expected: number, tolerance = 1) =>
  expect(Math.abs(actual - expected)).toBeLessThanOrEqual(tolerance);

/** Storage and the URL never carry the layout (session memory only). */
async function layoutPersistedAnywhere(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const found: string[] = [];
    for (const store of [window.localStorage, window.sessionStorage])
      for (let index = 0; index < store.length; index += 1) {
        const key = store.key(index)!;
        const value = store.getItem(key) ?? "";
        if (/layout|"inspector"\s*:|"top"\s*:|nle\.overlay/u.test(key + value))
          found.push(key);
      }
    if (/layout|inspector|bin=|top=/u.test(location.href))
      found.push(location.href);
    return found;
  });
}

test("reference shell opens at viewport bounds with exactly four regions in proportion", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  const layout = await measure(page);
  // Bounds are the viewport minus the 16 px margin on each side.
  near(layout.dialog.width, 1402 - 32);
  near(layout.dialog.height, 868 - 32);
  near(layout.dialog.x, 16);
  near(layout.dialog.y, 16);
  expect(layout.areaCount).toBe(4);
  expect(layout.paneModes).toBe(0);
  expect(layout.tier).toBe("standard");
  const width = layout.stage.clientWidth;
  const height = layout.stage.clientHeight;
  const share = (value: number, total: number) => (value / total) * 100;
  expect(Math.abs(share(layout.bin.width, width) - 21.0)).toBeLessThanOrEqual(
    2,
  );
  expect(
    Math.abs(share(layout.monitor.width, width) - 52.5),
  ).toBeLessThanOrEqual(2);
  expect(
    Math.abs(share(layout.inspector.width, width) - 25.6),
  ).toBeLessThanOrEqual(2);
  expect(Math.abs(share(layout.bin.height, height) - 62.2)).toBeLessThanOrEqual(
    2,
  );
  // R1-R3 share the top band; R4 spans the whole workspace width under it.
  near(layout.monitor.height, layout.bin.height);
  near(layout.inspector.height, layout.bin.height);
  near(layout.timeline.width, width);
  expect(layout.timeline.y).toBeGreaterThan(layout.bin.y + layout.bin.height);
  expect(layout.bin.x + layout.bin.width).toBeLessThanOrEqual(layout.monitor.x);
  expect(layout.monitor.x + layout.monitor.width).toBeLessThanOrEqual(
    layout.inspector.x,
  );
  // Nothing overflows the stage at the reference size.
  expect(layout.stage.scrollWidth).toBeLessThanOrEqual(width);
  expect(layout.stage.scrollHeight).toBeLessThanOrEqual(height);
  // Exactly the three splitters.
  await expect(page.locator(`${OVERLAY} [role="separator"]`)).toHaveCount(3);
  const nodes = await page
    .locator(ROOT)
    .evaluate((root) => root.querySelectorAll("*").length);
  expect(nodes).toBeLessThanOrEqual(1_500);
});

test("each splitter moves by pointer and keys and resets to its default", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  const initial = await measure(page);

  // S1: the media bin grows and the monitor gives up exactly the dragged distance.
  await drag(page, "bin_monitor", 60);
  let now = await measure(page);
  near(now.bin.width - initial.bin.width, 60);
  near(now.monitor.width - initial.monitor.width, -60);
  near(now.inspector.width, initial.inspector.width);
  await drag(page, "bin_monitor", -60);
  now = await measure(page);
  near(now.bin.width, initial.bin.width);

  // S2: moving right shrinks the inspector and grows the monitor.
  await drag(page, "monitor_inspector", 60);
  now = await measure(page);
  near(now.inspector.width - initial.inspector.width, -60);
  near(now.monitor.width - initial.monitor.width, 60);
  near(now.bin.width, initial.bin.width);
  await drag(page, "monitor_inspector", -60);

  // S3: the top band grows and the timeline gives up the same height.
  await drag(page, "top_timeline", 60);
  now = await measure(page);
  near(now.bin.height - initial.bin.height, 60);
  near(now.timeline.height - initial.timeline.height, -60);
  await drag(page, "top_timeline", -60);
  now = await measure(page);
  near(now.bin.height, initial.bin.height);
  near(now.inspector.width, initial.inspector.width);

  // Each splitter's primary pane and the key that grows it: R1 grows rightwards, R3 grows when its
  // splitter moves left, the top band grows downwards.
  const keys: ReadonlyArray<
    readonly [
      Splitter,
      "ArrowRight" | "ArrowLeft" | "ArrowDown",
      (layout: Layout) => number,
    ]
  > = [
    ["bin_monitor", "ArrowRight", (layout) => layout.bin.width],
    ["monitor_inspector", "ArrowLeft", (layout) => layout.inspector.width],
    ["top_timeline", "ArrowDown", (layout) => layout.bin.height],
  ];
  for (const [id, forward, size] of keys) {
    const control = splitter(page, id);
    await expect(control).toHaveAttribute(
      "aria-orientation",
      id === "top_timeline" ? "horizontal" : "vertical",
    );
    await expect(control).toHaveAttribute("aria-controls", /\S/);
    const start = size(await measure(page));
    await control.focus();
    await page.keyboard.press(forward);
    near(size(await measure(page)) - start, 16);
    await page.keyboard.press(`Shift+${forward}`);
    near(size(await measure(page)) - start, 80);
    await page.keyboard.press("Home");
    const atHome = await control.evaluate((element) => [
      element.getAttribute("aria-valuenow"),
      element.getAttribute("aria-valuemin"),
    ]);
    // Home is the primary pane's minimum.
    expect(atHome[0]).toBe(atHome[1]);
    await page.keyboard.press("End");
    const atEnd = await control.evaluate((element) => [
      element.getAttribute("aria-valuenow"),
      element.getAttribute("aria-valuemax"),
    ]);
    expect(atEnd[0]).toBe(atEnd[1]);
    const limit = await measure(page);
    // At the maximum a further growing step changes nothing.
    await page.keyboard.press(forward);
    near(size(await measure(page)), size(limit));
    await page.keyboard.press("Enter");
    near(size(await measure(page)), start);
    await page.keyboard.press("End");
    await control.dblclick();
    near(size(await measure(page)), start);
    for (const [name, value] of [
      ["aria-valuenow", await control.getAttribute("aria-valuenow")],
      ["aria-valuemin", await control.getAttribute("aria-valuemin")],
      ["aria-valuemax", await control.getAttribute("aria-valuemax")],
    ] as const)
      expect(Number.isInteger(Number(value)), name).toBe(true);
  }

  // The layout is session memory: it survives a close and an explicit reopen in the same mount
  // and never reaches storage or the URL.
  await splitter(page, "bin_monitor").focus();
  await page.keyboard.press("Shift+ArrowRight");
  const moved = await measure(page);
  await page.locator(CLOSE).click();
  await expect(page.locator(ROOT)).toHaveCount(0);
  await page
    .locator("#root")
    .getByRole("button", { name: "Open full editor" })
    .click();
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
  const reopened = await measure(page);
  near(reopened.bin.width, moved.bin.width);
  expect((await shellSnapshot(page)).generation).toBe(2);
  expect(await layoutPersistedAnywhere(page)).toEqual([]);
});

test("export popover holds the final video card and returns focus", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", { render: true });
  const exportButton = page.locator(EXPORT_BUTTON);
  const popover = page.locator(EXPORT_POPOVER);
  const region = page.getByRole("region", { name: "Final video", exact: true });
  await expect(exportButton).toHaveAttribute("aria-expanded", "false");
  // The render card is mounted with its hooks but reachable only through Export.
  await expect(page.locator(`${OVERLAY} [data-h3-nle-render]`)).toHaveCount(1);
  await expect(region).toHaveCount(0);
  await expect(page.locator(`${OVERLAY} footer`)).toHaveCount(0);

  await exportButton.click();
  await expect(exportButton).toHaveAttribute("aria-expanded", "true");
  await expect(popover).toBeVisible();
  await expect(region).toBeVisible();
  // Escape inside the popover closes the popover only and returns focus to Export.
  await region.getByRole("button", { name: "Render final video" }).focus();
  await page.keyboard.press("Escape");
  await expect(popover).toBeHidden();
  await expect(region).toHaveCount(0);
  await expect(exportButton).toBeFocused();
  await expect(page.locator(ROOT)).toHaveCount(1);

  // A press outside the popover and its button closes it too.
  await openExportPanel(page);
  await page.locator(`${OVERLAY} .h3-nle-header h2`).click();
  await expect(popover).toBeHidden();

  // With the popover closed, Escape belongs to the dialog again.
  await exportButton.focus();
  await page.keyboard.press("Escape");
  await expect(page.locator(ROOT)).toHaveCount(0);
  expect((await shellSnapshot(page)).lastCloseReason).toBe("escape");
  expect((await shellSnapshot(page)).renderJobRequests).toBe(0);
});

/** A host pack's floating toolbar at the top right with the highest z-index a browser keeps. */
async function addForeignFloat(page: Page, id: string) {
  await page.evaluate((name) => {
    const element = document.createElement("div");
    element.id = name;
    element.style.cssText =
      "position:absolute;top:20px;right:10px;width:290px;height:32px;z-index:2147483647;background:#2b2b2b";
    document.body.append(element);
  }, id);
}

// B-M2544-09: on the supplied host an installed pack's floating toolbar (absolute, top right,
// z-index 9999999999, which the browser clamps to 2147483647) sat over the chrome bar's Export and
// Close once the dialog opened at the viewport's edges. The dialog must stay above any foreign
// z-index, whether the foreign element was attached before or after it opened, without touching
// the foreign element.
test("the dialog stays above a foreign element at the highest z-index", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", {
    render: true,
    beforeOpen: (target) => addForeignFloat(target, "foreign-before"),
  });
  await addForeignFloat(page, "foreign-after");
  // The owned root stays the editor's `h3-sidebar` inline-size container: never narrower than the
  // dialog, so no narrow-sidebar container rule applies inside the editor.
  const widths = await page.locator(ROOT).evaluate((root) => ({
    root: root.getBoundingClientRect().width,
    dialog: root.querySelector("[role='dialog']")!.getBoundingClientRect()
      .width,
    container: getComputedStyle(root).containerType,
  }));
  expect(widths.container).toBe("inline-size");
  expect(widths.root).toBeGreaterThanOrEqual(widths.dialog);
  for (const selector of [EXPORT_BUTTON, CLOSE]) {
    const covered = await page
      .locator(`${OVERLAY} ${selector}`)
      .evaluate((button) => {
        const rect = button.getBoundingClientRect();
        const hit = document.elementFromPoint(
          rect.x + rect.width / 2,
          rect.y + rect.height / 2,
        );
        return hit !== null && !button.contains(hit) ? (hit.id ?? "") : null;
      });
    expect(covered, selector).toBeNull();
  }
  await page.locator(EXPORT_BUTTON).click();
  await expect(page.locator(EXPORT_BUTTON)).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await page.keyboard.press("Escape");
  await expect(page.locator(EXPORT_POPOVER)).toBeHidden();
  await page.locator(CLOSE).click();
  await expect(page.locator(ROOT)).toHaveCount(0);
  expect((await shellSnapshot(page)).lastCloseReason).toBe("explicit_close");
  // The foreign elements are untouched.
  expect(
    await page.evaluate(() =>
      ["foreign-before", "foreign-after"].map(
        (id) => document.getElementById(id)?.style.zIndex ?? null,
      ),
    ),
  ).toEqual(["2147483647", "2147483647"]);
});

test("clip editor tab has one editing surface and close returns focus to the launcher", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke", {
    beforeOpen: toClipEditor,
    expandOverlay: false,
  });
  const panel = page.locator('[data-h3-director-panel="clip_editor"]');
  await expect(panel.locator(LAUNCHER)).toBeVisible();
  const summary = panel.getByRole("region", {
    name: "Clip editor project",
    exact: true,
  });
  await expect(summary).toBeVisible();
  // No compact editor: none of its surface, timeline list or per-clip edit controls exist.
  await expect(page.locator("section.h3a")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Reference & timeline authoring" }),
  ).toHaveCount(0);
  await expect(panel.locator("[data-h3-nle-control]")).toHaveCount(0);
  await expect(
    panel.locator('[role="slider"],input[type="range"]'),
  ).toHaveCount(0);

  await page.locator(LAUNCHER).click();
  await expect(page.locator(OVERLAY)).toHaveAttribute(
    "data-h3-nle-state",
    "expanded",
  );
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
  // The one editing surface is the overlay: timeline edit controls exist only inside it.
  expect(
    await page.locator(`${OVERLAY} [data-h3-nle-control]`).count(),
  ).toBeGreaterThan(0);
  expect(
    await page
      .locator("[data-h3-nle-control]")
      .evaluateAll(
        (controls, overlay) =>
          controls.filter((control) => control.closest(overlay) === null)
            .length,
        OVERLAY,
      ),
  ).toBe(0);
  await page.locator(CLOSE).click();
  await expect(page.locator(ROOT)).toHaveCount(0);
  await expect(page.locator(LAUNCHER_FOCUS)).toBeFocused();
  expect((await shellSnapshot(page)).lastCloseReason).toBe("explicit_close");
});

/**
 * M25-63 (A63-1): the top bar's DOM inventory. Visible: the title, the save indicator, Export and
 * Close, in that order, in one row of `topBarHeightPx` under a fine pointer. Kept but not shown:
 * the surface status (still announced) and the monitor and audio diagnostics with their attributes
 * (neither shown nor announced).
 */
async function topBarInventory(page: Page) {
  return page.locator(`${OVERLAY} .h3-nle-header`).evaluate((header) => {
    const shown = (element: Element) => {
      const rect = element.getBoundingClientRect();
      return (
        rect.width > 1 &&
        rect.height > 1 &&
        element.closest("[hidden],[aria-hidden='true']") === null &&
        getComputedStyle(element).visibility !== "hidden"
      );
    };
    const box = (element: Element | null) => {
      if (element === null) return null;
      const rect = element.getBoundingClientRect();
      return {
        top: Math.round(rect.top),
        bottom: Math.round(rect.bottom),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      };
    };
    const visible = [...header.children]
      .filter(shown)
      .filter((element) => !element.classList.contains("h3-nle-header-space"))
      .map((element) => ({
        tag: element.tagName.toLowerCase(),
        key:
          element.getAttribute("data-h3-nle-action") ??
          (element.classList.contains("h3-nle-save") ? "save" : null),
        text: (element as HTMLElement).innerText.trim(),
        name: element.getAttribute("aria-label"),
      }));
    const surface = header.querySelector('[data-h3-nle-status="surface"]');
    const diagnostics = header.querySelector("[data-h3-nle-diagnostics]");
    return {
      visible,
      header: box(header),
      surface: {
        box: box(surface),
        role: surface?.getAttribute("role") ?? null,
        live: surface?.getAttribute("aria-live") ?? null,
        text: surface?.textContent ?? null,
      },
      diagnostics: {
        hidden: diagnostics === null ? null : !shown(diagnostics),
        ariaHidden: diagnostics?.getAttribute("aria-hidden") ?? null,
        monitor:
          diagnostics?.querySelector('[data-h3-nle-status="monitor"]')
            ?.textContent ?? null,
        audio:
          diagnostics
            ?.querySelector('[data-h3-nle-status="audio"]')
            ?.getAttribute("data-h3-nle-audio-state") ?? null,
      },
      shownText: [...header.querySelectorAll("*")]
        .filter(shown)
        .map((element) => (element as HTMLElement).innerText)
        .join(" "),
      save: header
        .querySelector(".h3-nle-save")
        ?.getAttribute("data-h3-nle-save-state"),
      close: box(header.querySelector('[data-h3-nle-action="close"]')),
    };
  });
}

test("A63-1: the top bar shows exactly the title, the save indicator, Export and Close", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await expect(
    page.locator(`${OVERLAY} [data-h3-nle-status="timeline"]`),
  ).toHaveAttribute("data-h3-nle-save-state", "saved");
  await expect(
    page.locator(`${OVERLAY} [data-h3-nle-status="monitor"]`),
  ).toHaveText("Monitor paused.");
  const bar = await topBarInventory(page);
  const detail = JSON.stringify(bar);
  expect(bar.visible, detail).toEqual([
    { tag: "h2", key: null, text: "Clip editor", name: null },
    { tag: "span", key: "save", text: "Saved", name: null },
    { tag: "button", key: "export", text: "Export", name: null },
    { tag: "button", key: "close", text: "", name: "Close full editor" },
  ]);
  expect(bar.header!.height, detail).toBe(
    NLE_REFERENCE_UI_CONTRACT_V1.topBarHeightPx,
  );
  // No visible status sentence, revision or chip: the only shown text is the four items'.
  expect(bar.shownText, detail).not.toMatch(/\d/);
  expect(bar.shownText, detail).not.toMatch(/Monitor|ready|Suspended/);
  // The surface status stays in place, announced but occupying no visible box.
  expect(bar.surface, detail).toMatchObject({
    role: "status",
    live: "polite",
    text: "Full editor ready.",
  });
  expect(bar.surface.box!.width, detail).toBeLessThanOrEqual(1);
  expect(bar.surface.box!.height, detail).toBeLessThanOrEqual(1);
  // The diagnostics keep their attributes and text for tests, and are neither shown nor announced.
  expect(bar.diagnostics, detail).toEqual({
    hidden: true,
    ariaHidden: "true",
    monitor: "Monitor paused.",
    audio: "suspended",
  });
  // Close is an icon button at the icon target, with its name, hint and focus key unchanged.
  const close = page.locator(CLOSE);
  await expect(close).toHaveAttribute("title", "Escape closes the full editor");
  await expect(close).toHaveAttribute("data-h3-focus-key", "nle-close-overlay");
  const icon = await nleTargetFloor(page, "icon");
  expect(bar.close!.width, detail).toBeGreaterThanOrEqual(icon);
  expect(bar.close!.height, detail).toBeGreaterThanOrEqual(icon);
  // The resize control is the corner grip at the icon target, flush with the dialog's corner.
  const corner = await page.locator(OVERLAY).evaluate((dialog) => {
    const grip = dialog.querySelector('[data-h3-nle-action="resize"]')!;
    const a = grip.getBoundingClientRect();
    const b = dialog.getBoundingClientRect();
    const style = getComputedStyle(dialog);
    // Flush with the inner corner: the only gap is the dialog's own border.
    return {
      right: Math.round(b.right - a.right - parseFloat(style.borderRightWidth)),
      bottom: Math.round(
        b.bottom - a.bottom - parseFloat(style.borderBottomWidth),
      ),
      width: Math.round(a.width),
      height: Math.round(a.height),
      glyph: grip.querySelector('[data-h3-nle-icon="cornerGrip"]') !== null,
    };
  });
  expect(corner, JSON.stringify(corner)).toEqual({
    right: 0,
    bottom: 0,
    width: icon,
    height: icon,
    glyph: true,
  });
  await expectGripClearOfLanes(page);
});

test.describe("A63-1 under a coarse pointer", () => {
  test.use({ hasTouch: true });

  test("the top bar keeps the same inventory and grows to the 44 px targets", async ({
    page,
  }) => {
    await openIntegratedShell(page, "smoke");
    expect(
      await page.evaluate(() => matchMedia("(pointer: coarse)").matches),
    ).toBe(true);
    await expect(
      page.locator(`${OVERLAY} [data-h3-nle-status="timeline"]`),
    ).toHaveAttribute("data-h3-nle-save-state", "saved");
    const bar = await topBarInventory(page);
    const detail = JSON.stringify(bar);
    expect(
      bar.visible.map((item) => item.key ?? item.tag),
      detail,
    ).toEqual(["h2", "save", "export", "close"]);
    const floor = NLE_REFERENCE_UI_CONTRACT_V1.controlTargetPx.coarse;
    expect(bar.close!.width, detail).toBeGreaterThanOrEqual(floor);
    expect(bar.close!.height, detail).toBeGreaterThanOrEqual(floor);
    expect(bar.header!.height, detail).toBeGreaterThanOrEqual(floor);
    const exportBox = (await page.locator(EXPORT_BUTTON).boundingBox())!;
    expect(exportBox.height).toBeGreaterThanOrEqual(floor);
    await expectGripClearOfLanes(page);
  });
});

test("the narrow tier at 720 x 480 keeps every region at its minimum with 44 px controls", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 480 });
  await openIntegratedShell(page, "smoke");
  const layout = await measure(page);
  expect(layout.tier).toBe("narrow");
  near(layout.dialog.width, 704);
  expect(layout.stage.scrollWidth).toBeLessThanOrEqual(
    layout.stage.clientWidth,
  );
  expect(layout.bin.width).toBeGreaterThanOrEqual(160 - 0.5);
  expect(layout.monitor.width).toBeGreaterThanOrEqual(280 - 0.5);
  expect(layout.inspector.width).toBeGreaterThanOrEqual(240 - 0.5);
  expect(layout.bin.height).toBeGreaterThanOrEqual(240 - 0.5);
  expect(layout.timeline.height).toBeGreaterThanOrEqual(160 - 0.5);
  // The vertical floor (M25-53): the dialog takes the 480 px viewport less its 8 px margins,
  // the timeline sits exactly at its 160 px minimum, the top band absorbs the rest above its
  // own minimum, and the stage has nothing left to scroll.
  near(layout.dialog.height, 464);
  near(layout.timeline.height, 160);
  expect(layout.stage.scrollHeight).toBeLessThanOrEqual(
    layout.stage.clientHeight,
  );

  // S1 and S2 move through their remaining travel by keys and by pointer.
  for (const id of ["bin_monitor", "monitor_inspector"] as const) {
    const control = splitter(page, id);
    const size = (value: Layout) =>
      id === "bin_monitor" ? value.bin.width : value.inspector.width;
    await control.focus();
    await page.keyboard.press("Home");
    const min = size(await measure(page));
    await page.keyboard.press("End");
    const max = size(await measure(page));
    expect(max).toBeGreaterThan(min);
    await page.keyboard.press("Home");
    await drag(page, id, id === "bin_monitor" ? 60 : -60);
    near(size(await measure(page)), max);
    const after = await measure(page);
    expect(after.monitor.width).toBeGreaterThanOrEqual(280 - 0.5);
    expect(after.stage.scrollWidth).toBeLessThanOrEqual(
      after.stage.clientWidth,
    );
    await page.keyboard.press("Enter");
  }

  // Every visible control keeps the editor's floor for this pointer (M25-61 contract).
  const floor = await nleTargetFloor(page);
  const grip = await nleFineGripException(page);
  const small = await page.locator(OVERLAY).evaluate(
    (dialog, { floor, grip }) =>
      [
        ...dialog.querySelectorAll<HTMLElement>(
          'button,[role="tab"],select,input:not([type="checkbox"]):not([type="radio"])',
        ),
      ]
        .filter((element) => element.getClientRects().length > 0)
        .map((element) => {
          const rect = element.getBoundingClientRect();
          // M25-62 (B-M2562-05): only the pinned 8 px fine grip is excused its width.
          const excused =
            grip !== null &&
            element.matches(grip.selector) &&
            Math.round(rect.width) === grip.widthPx;
          return {
            name:
              element.getAttribute("aria-label") ??
              element.textContent?.trim().slice(0, 40) ??
              element.tagName,
            width: excused ? floor : Math.round(rect.width),
            height: Math.round(rect.height),
          };
        })
        .filter((entry) => entry.width < floor || entry.height < floor),
    { floor, grip },
  );
  expect(small).toEqual([]);
  for (const id of ["bin_monitor", "monitor_inspector"] as const) {
    const box = (await splitter(page, id).boundingBox())!;
    expect(box.width).toBeGreaterThanOrEqual(44);
  }
  expect(
    (await splitter(page, "top_timeline").boundingBox())!.height,
  ).toBeGreaterThanOrEqual(44);

  // M25-63: the bin tabs keep the label under the icon at this tier too, each inside its own
  // tab box, with the accessible name and the hover description unchanged.
  for (const [name, pane] of [
    ["Media", "assets"],
    ["Sequence", "sequence"],
  ] as const) {
    const tab = page.getByRole("tab", { name, exact: true });
    await expect(tab).toHaveAttribute("data-h3-nle-pane", pane);
    await expect(tab).toHaveAttribute("title", /\S/);
    expect((await tab.innerText()).trim()).toBe(name);
    const fits = await tab.evaluate((element) => {
      const label = element.querySelector(".h3-nle-tab-label")!;
      const a = label.getBoundingClientRect();
      const b = element.getBoundingClientRect();
      return a.left >= b.left && a.right <= b.right && a.bottom <= b.bottom;
    });
    expect(fits).toBe(true);
  }

  // The Sequence tab at a 160 px bin scrolls inside the bin and adds no stage overflow.
  await splitter(page, "bin_monitor").focus();
  await page.keyboard.press("Home");
  await page.getByRole("tab", { name: "Sequence", exact: true }).click();
  const sequence = await page.locator(OVERLAY).evaluate((dialog) => {
    const bin = dialog.querySelector('[data-h3-nle-area="bin"]')!;
    const scroller = bin.querySelector<HTMLElement>(".h3-nle-area-scroll")!;
    const stage = dialog.querySelector<HTMLElement>(".h3-nle-stage")!;
    return {
      bin: Math.round(bin.getBoundingClientRect().width),
      content: scroller.scrollWidth,
      viewport: scroller.clientWidth,
      stageOverflow: stage.scrollWidth - stage.clientWidth,
    };
  });
  near(sequence.bin, 160);
  expect(sequence.content).toBeGreaterThan(sequence.viewport);
  expect(sequence.stageOverflow).toBeLessThanOrEqual(0);
});

test("the standard tier at 960 x 600 has no overflow", async ({ page }) => {
  await page.setViewportSize({ width: 960, height: 600 });
  await openIntegratedShell(page, "smoke");
  const layout = await measure(page);
  expect(layout.tier).toBe("standard");
  expect(layout.stage.scrollWidth).toBeLessThanOrEqual(
    layout.stage.clientWidth,
  );
  expect(layout.stage.scrollHeight).toBeLessThanOrEqual(
    layout.stage.clientHeight,
  );
  expect(layout.bin.width).toBeGreaterThanOrEqual(200 - 0.5);
  expect(layout.monitor.width).toBeGreaterThanOrEqual(320 - 0.5);
  expect(layout.inspector.width).toBeGreaterThanOrEqual(280 - 0.5);
});

test("below the scroll floor the stage is the only horizontal scroller and focus stays in view", async ({
  page,
}) => {
  // A 656 px viewport leaves a 640 px workspace (8 px compact margin), under the 688 px floor.
  await page.setViewportSize({ width: 656, height: 600 });
  await openIntegratedShell(page, "smoke");
  const layout = await measure(page);
  expect(layout.tier).toBe("scroll_floor");
  expect(layout.stage.scrollWidth).toBeGreaterThan(layout.stage.clientWidth);
  const outer = await page.evaluate((overlay) => {
    const dialog = document.querySelector<HTMLElement>(overlay)!;
    return {
      dialog: dialog.scrollWidth - dialog.clientWidth,
      document:
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    };
  }, OVERLAY);
  expect(outer.dialog).toBeLessThanOrEqual(0);
  expect(outer.document).toBeLessThanOrEqual(0);
  // Every region is reachable by scrolling the stage.
  await page
    .locator(`${OVERLAY} .h3-nle-stage`)
    .evaluate((stage) => stage.scrollTo({ left: stage.scrollWidth }));
  const scrolled = await measure(page);
  expect(scrolled.inspector.x + scrolled.inspector.width).toBeLessThanOrEqual(
    scrolled.stage.x + scrolled.stage.width + 1,
  );
  await page
    .locator(`${OVERLAY} .h3-nle-stage`)
    .evaluate((stage) => stage.scrollTo({ left: 0 }));
  // Tab through the controls: each focused element inside the stage is inside its scrollport.
  await page.locator(`${OVERLAY} .h3-nle-header h2`).focus();
  const outside: string[] = [];
  for (let press = 0; press < 60; press += 1) {
    await page.keyboard.press("Tab");
    const placement = await page.evaluate((overlay) => {
      const active = document.activeElement as HTMLElement | null;
      const stage = document.querySelector<HTMLElement>(
        `${overlay} .h3-nle-stage`,
      )!;
      if (active === null || !stage.contains(active)) return null;
      const port = stage.getBoundingClientRect();
      const rect = active.getBoundingClientRect();
      const visible =
        rect.right > port.left &&
        rect.left < port.right &&
        rect.bottom > port.top &&
        rect.top < port.bottom;
      return visible
        ? null
        : (active.getAttribute("aria-label") ?? active.tagName);
    }, OVERLAY);
    if (placement !== null) outside.push(placement);
  }
  expect(outside).toEqual([]);
});

test("a point 21 px from a splitter's gutter hits the region, not the splitter", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  const hit = (x: number, y: number) =>
    page.evaluate(
      ({ x, y }) => {
        const element = document.elementFromPoint(x, y);
        return {
          splitter: element?.closest("[data-h3-nle-splitter]") !== null,
          area:
            element
              ?.closest("[data-h3-nle-area]")
              ?.getAttribute("data-h3-nle-area") ?? null,
        };
      },
      { x, y },
    );
  for (const [id, before, after] of [
    ["bin_monitor", "bin", "monitor"],
    ["monitor_inspector", "monitor", "inspector"],
  ] as const) {
    const track = (await gutter(page, id).boundingBox())!;
    const y = track.y + track.height / 2;
    expect(await hit(track.x - 21, y)).toEqual({
      splitter: false,
      area: before,
    });
    expect(await hit(track.x + track.width + 21, y)).toEqual({
      splitter: false,
      area: after,
    });
    expect((await hit(track.x - 19, y)).splitter).toBe(true);
  }
  const band = (await gutter(page, "top_timeline").boundingBox())!;
  const x = band.x + band.width / 2;
  expect(await hit(x, band.y + band.height + 21)).toEqual({
    splitter: false,
    area: "timeline",
  });
  expect((await hit(x, band.y - 19)).splitter).toBe(true);
});
