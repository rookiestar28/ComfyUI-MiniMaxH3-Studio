// M25-45: the monitor's picture and its transport, on the REAL integrated shell
// (`frontend/e2e/nleShell.tsx`) in the browser's own layout engine.
//
// The fit rule is asserted against the composition the picture area declares it is fitting
// (`data-h3-nle-picture="WxH"`), never against the geometry module under test, and every
// measurement is a real dragged layout rather than a computed one.

import { expect, test, type Locator, type Page } from "@playwright/test";
import { nleTargetFloor } from "../helpers/nleTargets";

import {
  NLE_OVERLAY as OVERLAY,
  dragSplitter as drag,
  expectFit,
  expectOverlayClearOfControls,
  loseCanvasContext,
  near,
  readPicture as picture,
  settledPicture as settled,
} from "../helpers/nleMonitorPicture";
import { openIntegratedShell } from "../helpers/nleShell";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

const MONITOR_STATUS = '[data-h3-nle-status="monitor"]';

function control(page: Page, id: string): Locator {
  return page.locator(`${OVERLAY} [data-h3-nle-control="${id}"]`);
}

async function paused(page: Page) {
  await expect(page.locator(MONITOR_STATUS)).toHaveText("Monitor paused.", {
    timeout: 60_000,
  });
}

test("the picture fits the composition and follows the splitters while width-limited", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await paused(page);
  const first = await settled(page, "default layout");
  // The default layout gives the monitor a wide, short area: the fit is limited by its width.
  expect(first.area.width).toBeLessThanOrEqual(
    first.area.height * first.aspect + 1,
  );
  expectFit(first, "default layout");
  near(first.canvas.width, first.area.width);
  // The whole point of the item: a picture that is actually large.
  expect(first.canvas.width).toBeGreaterThanOrEqual(700);
  // The backing store follows the picture and the device pixel ratio, never above the composition
  // itself and never above the frozen cap. This fixture's composition is 320 x 180, so its own
  // size is the ceiling here; the 1280-level rows belong to the semantic corpus.
  //
  // GUARD: that ceiling is what makes this an exact equality. `computePreviewSize` rounds a
  // scaled backing to an EVEN number of pixels, so once the composition is wider than the picture
  // the scale drops below 1 and this expectation misses by one about half the time.
  expect(first.backing.width).toBe(
    Math.min(first.composition.width, Math.round(first.canvas.width)),
  );
  expect(first.backing.width).toBeLessThanOrEqual(1280);
  expect(first.backing.height).toBeLessThanOrEqual(1280);
  expect(first.backing.width * first.backing.height).toBeLessThanOrEqual(
    921_600,
  );

  // S3 down: a taller area that is still width-limited leaves the picture alone and letterboxes.
  await drag(page, "top_timeline", 100);
  const taller = await settled(page, "S3 down");
  expectFit(taller, "S3 down");
  expect(taller.area.height).toBeGreaterThan(first.area.height + 50);
  near(taller.canvas.width, first.canvas.width);
  near(taller.canvas.height, first.canvas.height);
  expect(taller.area.height - taller.canvas.height).toBeGreaterThan(
    first.area.height - first.canvas.height,
  );

  // S1: the picture tracks the pane one for one in both directions. The gesture makes its own
  // headroom first, because a splitter already at a region's minimum simply refuses part of a
  // drag, and that clamp is the layout's business rather than the picture's.
  await drag(page, "bin_monitor", 60);
  const narrower = await settled(page, "S1 right");
  expectFit(narrower, "S1 right");
  near(narrower.canvas.width, taller.canvas.width - 60, 2);
  await drag(page, "bin_monitor", -60);
  const restored = await settled(page, "S1 back");
  expectFit(restored, "S1 back");
  near(restored.canvas.width, taller.canvas.width, 2);

  // S2: the same, from the other side, and from the same starting widths.
  await drag(page, "monitor_inspector", -60);
  const smaller = await settled(page, "S2 left");
  expectFit(smaller, "S2 left");
  near(smaller.canvas.width, taller.canvas.width - 60, 2);
  await drag(page, "monitor_inspector", 60);
  const other = await settled(page, "S2 back");
  expectFit(other, "S2 back");
  near(other.canvas.width, taller.canvas.width, 2);
});

test("a height-limited area drives the picture by its height alone", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await paused(page);
  // Drag the top band to its minimum: the monitor is then short and wide, and the fit is limited
  // by its height rather than its width.
  await drag(page, "top_timeline", -400);
  const short = await settled(page, "top band minimum");
  expectFit(short, "top band at its minimum");
  expect(short.area.height).toBeLessThan(short.area.width / short.aspect + 1);
  near(short.canvas.height, short.area.height);
  near(short.canvas.width, short.area.height * short.aspect);
  // Centred, with the letterbox on the other axis only.
  near(
    short.canvas.x - short.area.x,
    (short.area.width - short.canvas.width) / 2,
  );
  near(short.canvas.y - short.area.y, 0);

  // Height-limited: a wider monitor changes nothing.
  await drag(page, "bin_monitor", -60);
  const wider = await settled(page, "height-limited, wider");
  expectFit(wider, "height-limited, wider pane");
  near(wider.canvas.height, short.canvas.height);
  near(wider.canvas.width, short.canvas.width);

  // And more height grows the picture by exactly the height the area gained, for as long as the
  // area stays height-limited. AC45-01 says the same thing about width: past the aspect-fit limit
  // the other axis takes over, and expecting linear growth through that point is the error.
  await drag(page, "top_timeline", 50);
  const taller = await settled(page, "height-limited, taller");
  expect(taller.area.height).toBeLessThan(
    taller.area.width / taller.aspect + 1,
  );
  near(
    taller.canvas.height - wider.canvas.height,
    taller.area.height - wider.area.height,
    2,
  );
});

test("a portrait composition is height-limited in the default layout", async ({
  page,
}) => {
  // AC45-02's second fixture. 180 x 320 is the smoke shape's own pixel count turned on its side,
  // so the only thing that differs from the case above is the aspect -- and that alone is enough
  // to put the default layout on the height-limited branch, with no drag to get there.
  await openIntegratedShell(page, "portrait");
  await paused(page);
  const first = await settled(page, "portrait default layout");
  expect(first.composition).toEqual({ width: 180, height: 320 });
  expectFit(first, "portrait default layout");
  expect(first.area.height).toBeLessThan(first.area.width / first.aspect + 1);
  near(first.canvas.height, first.area.height);
  near(first.canvas.width, first.area.height * first.aspect);
  near(
    first.canvas.x - first.area.x,
    (first.area.width - first.canvas.width) / 2,
  );
  near(first.canvas.y - first.area.y, 0);

  // S3 down by 100 px: the picture takes the whole of the height the area gained.
  await drag(page, "top_timeline", 100);
  const taller = await settled(page, "portrait S3 down");
  expectFit(taller, "portrait S3 down");
  near(taller.area.height - first.area.height, 100, 2);
  near(taller.canvas.height - first.canvas.height, 100, 2);

  // S1 and S2 leave a height-limited picture alone, in both directions.
  for (const [splitter, delta] of [
    ["bin_monitor", 60],
    ["bin_monitor", -60],
    ["monitor_inspector", -60],
    ["monitor_inspector", 60],
  ] as const) {
    await drag(page, splitter, delta);
    const shot = await settled(page, `portrait ${splitter} ${delta}`);
    expectFit(shot, `portrait ${splitter} ${delta}`);
    near(shot.canvas.height, taller.canvas.height);
    near(shot.canvas.width, taller.canvas.width);
  }
});

const TIERS = [
  { width: 1402, height: 868 },
  { width: 960, height: 600 },
  { width: 720, height: 480 },
] as const;
const LOCALES = ["en", "zh_hant", "zh_hans"] as const;

/**
 * M25-64 (row #25, R7): the transport's layout read from the page -- the five buttons, the
 * timecode parts that are shown, and the row's own inner width -- in one pass.
 */
function transportLayout(page: Page) {
  return page.evaluate((overlay) => {
    const dialog = document.querySelector<HTMLElement>(overlay)!;
    const row = dialog.querySelector<HTMLElement>(".h3-nle-transport")!;
    const style = getComputedStyle(row);
    const rowBox = row.getBoundingClientRect();
    const box = (element: Element) => {
      const rect = element.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height,
      };
    };
    const part = (name: string) => {
      const element = row.querySelector<HTMLElement>(
        `[data-h3-nle-timecode-part="${name}"]`,
      )!;
      return { shown: element.getClientRects().length > 0, box: box(element) };
    };
    const output = row.querySelector<HTMLElement>("[data-h3-nle-timecode]")!;
    return {
      row: box(row),
      inner:
        rowBox.width -
        parseFloat(style.paddingLeft) -
        parseFloat(style.paddingRight),
      buttons: [...row.querySelectorAll<HTMLButtonElement>("button")].map(
        (button) => ({
          control: button.getAttribute("data-h3-nle-control") ?? "",
          name: button.getAttribute("aria-label") ?? "",
          ...box(button),
        }),
      ),
      current: part("current"),
      total: part("total"),
      label: output.getAttribute("aria-label") ?? "",
      title: output.getAttribute("title") ?? "",
    };
  }, OVERLAY);
}

type TransportLayout = Awaited<ReturnType<typeof transportLayout>>;

/**
 * The layout once it has stopped moving: a viewport change reaches the region shares and then the
 * row a frame or two later, and a read in between measures a monitor that is still resizing.
 */
async function settledTransportLayout(page: Page): Promise<TransportLayout> {
  let previous = "";
  let layout: TransportLayout | null = null;
  await expect
    .poll(async () => {
      layout = await transportLayout(page);
      const current = JSON.stringify(layout);
      const stable = current === previous;
      previous = current;
      return stable;
    })
    .toBe(true);
  return layout!;
}

/** Every R7 rule of the transport row, at whatever width the monitor has now. */
async function expectTransportRow(
  page: Page,
  layout: TransportLayout,
  context: string,
) {
  const detail = `${context}: ${JSON.stringify(layout)}`;
  expect(
    layout.buttons.map((button) => button.control),
    detail,
  ).toEqual([
    "transport.step_back",
    layout.buttons[1]!.control === "transport.pause"
      ? "transport.pause"
      : "transport.play",
    "transport.step_forward",
    "transport.view",
    "transport.fullscreen",
  ]);
  // One row: every control is centred on the same line (Play is larger than the others), and
  // nothing leaves the row's box.
  const centre = (item: { top: number; bottom: number }) =>
    (item.top + item.bottom) / 2;
  const line = centre(layout.buttons[0]!);
  const floor = await nleTargetFloor(page, "icon");
  for (const button of layout.buttons) {
    expect(Math.abs(centre(button) - line), detail).toBeLessThanOrEqual(1);
    expect(button.width, detail).toBeGreaterThanOrEqual(floor);
    expect(button.height, detail).toBeGreaterThanOrEqual(floor);
    expect(button.name.length, detail).toBeGreaterThan(0);
    expect(button.left, detail).toBeGreaterThanOrEqual(layout.row.left);
    expect(button.right, detail).toBeLessThanOrEqual(layout.row.right);
  }
  // The round Play is at least 36 px and never smaller than its neighbours.
  const play = layout.buttons[1]!;
  expect(play.width, detail).toBeGreaterThanOrEqual(36);
  expect(play.width, detail).toBeGreaterThanOrEqual(layout.buttons[0]!.width);
  // Timecode left of the play group, view group right of it, with no overlap anywhere.
  expect(layout.current.shown, detail).toBe(true);
  expect(layout.current.box.right, detail).toBeLessThanOrEqual(
    layout.buttons[0]!.left,
  );
  for (let index = 1; index < layout.buttons.length; index += 1)
    expect(layout.buttons[index]!.left, detail).toBeGreaterThanOrEqual(
      layout.buttons[index - 1]!.right,
    );
  // R7: the total is shown only on a row at least 360 px wide inside; it always stays in the
  // timecode's accessible name and tooltip.
  expect(layout.total.shown, detail).toBe(layout.inner >= 360);
  if (layout.total.shown)
    expect(layout.total.box.right, detail).toBeLessThanOrEqual(
      layout.buttons[0]!.left,
    );
  const timecode = /\d{2}:\d{2}:\d{2}:\d{2}/gu;
  expect(layout.label.match(timecode), detail).toHaveLength(2);
  expect(layout.title, detail).toBe(layout.label);
}

for (const locale of LOCALES)
  test(`the transport is one unwrapped row with a readable timecode in every tier (${locale})`, async ({
    page,
  }) => {
    await openIntegratedShell(page, "smoke", { extraParams: { locale } });
    await expect(
      page.locator(`${OVERLAY} [data-h3-nle-timecode]`),
    ).toBeVisible();
    for (const viewport of TIERS) {
      await page.setViewportSize({ ...viewport });
      const buttons = page.locator(`${OVERLAY} .h3-nle-transport button`);
      await expect(buttons).toHaveCount(5);
      // The contract is the settled layout: a resize reaches the tiles a frame later, and reading
      // them in the transition catches a row that has not reflowed yet.
      await expect
        .poll(
          async () =>
            new Set(
              await buttons.evaluateAll((nodes) =>
                nodes.map((node) => {
                  const rect = node.getBoundingClientRect();
                  return Math.round(rect.top + rect.height / 2);
                }),
              ),
            ).size,
          { message: `${locale} ${viewport.width}x${viewport.height}` },
        )
        .toBe(1);
      const layout = await settledTransportLayout(page);
      await expectTransportRow(
        page,
        layout,
        `${locale} ${viewport.width}x${viewport.height} ${JSON.stringify(await rowGeometry(page))}`,
      );
      // At the widest tier the side columns have room, so the play group is centred in the row.
      if (viewport.width === TIERS[0].width) {
        const play = layout.buttons[1]!;
        near(
          (play.left + play.right) / 2,
          (layout.row.left + layout.row.right) / 2,
        );
      }
    }
  });

test("A64-2: at the 280 px monitor minimum the transport is one row and the total moves to the name", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 480 });
  await openIntegratedShell(page, "smoke");
  await paused(page);
  // Shrink the monitor from both sides until its own minimum stops the splitters.
  await drag(page, "bin_monitor", 600);
  await drag(page, "monitor_inspector", -600);
  const monitor = page.locator(`${OVERLAY} [data-h3-nle-area="monitor"]`);
  await expect
    .poll(async () => Math.round((await monitor.boundingBox())!.width))
    .toBeLessThanOrEqual(282);
  const width = Math.round((await monitor.boundingBox())!.width);
  expect(width).toBeGreaterThanOrEqual(278);
  const layout = await settledTransportLayout(page);
  await expectTransportRow(page, layout, `monitor ${width}`);
  expect(layout.inner).toBeLessThan(360);
  expect(layout.total.shown).toBe(false);
  // The shortcuts still act from the monitor at this width.
  const seek = playheadSlider(page);
  const start = await playheadFrame(seek);
  await page.locator(`${OVERLAY} .h3-nle-monitor`).focus();
  await page.keyboard.press(".");
  await expect.poll(() => playheadFrame(seek)).toBe(start + 1);
});

test("A64-1: the empty-timeline overlay is centred, has no action and covers no active control", async ({
  page,
}) => {
  await page.goto("/nleWorkspace.html?media=1&zeroClips=1");
  await page.getByRole("button", { name: "Open full editor" }).click();
  const overlay = page.locator(`${OVERLAY} [data-h3-nle-overlay="empty"]`);
  await expect(overlay).toBeVisible();
  await expect(overlay).toHaveAttribute("role", "status");
  await expect(overlay.locator("[data-h3-nle-overlay-title]")).toHaveText(
    "Nothing on the timeline yet",
  );
  await expect(overlay.locator("[data-h3-nle-overlay-detail]")).toContainText(
    "+",
  );
  await expect(overlay.locator("button")).toHaveCount(0);
  const header = page.locator(`${OVERLAY} [data-h3-nle-monitor-header]`);
  await expect(header.locator("[data-h3-nle-monitor-title]")).toHaveText(
    "Preview",
  );
  await expect(header.locator("[data-h3-nle-monitor-facts]")).toHaveText(
    /^\d+ × \d+ · 24 fps$/u,
  );
  await expectOverlayClearOfControls(page);
});

test("A64-1: the preview-unavailable overlay offers Retry on the recover path and covers no active control", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await paused(page);
  await loseCanvasContext(page, true);
  const overlay = page.locator(
    `${OVERLAY} [data-h3-nle-overlay="unavailable"]`,
  );
  await expect(overlay).toBeVisible();
  await expect(overlay).toHaveAttribute("role", "status");
  await expect(overlay.locator("[data-h3-nle-overlay-title]")).toHaveText(
    "Preview unavailable",
  );
  // M25-64 (A64-6): the overlay gives a plain reason; the status sentence stays the diagnostics.
  await expect(overlay.locator("[data-h3-nle-overlay-detail]")).toHaveText(
    "The browser could not draw the picture.",
  );
  await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
    "Monitor unavailable: canvas unavailable.",
  );
  const retry = overlay.locator("button");
  await expect(retry).toHaveCount(1);
  await expect(retry).toHaveAttribute(
    "data-h3-nle-control",
    "transport.recover",
  );
  await expect(retry).toHaveText("Retry");
  await expectOverlayClearOfControls(page);
  await loseCanvasContext(page, false);
  await retry.click();
  await paused(page);
  await expect(page.locator(`${OVERLAY} [data-h3-nle-overlay]`)).toHaveCount(0);
});

/** The transport's own boxes, for a failure message that says which part did not fit. */
function rowGeometry(page: Page) {
  return page.evaluate((overlay) => {
    const dialog = document.querySelector<HTMLElement>(overlay)!;
    const box = (selector: string) => {
      const element = dialog.querySelector<HTMLElement>(selector);
      if (element === null) return null;
      const rect = element.getBoundingClientRect();
      return {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        top: Math.round(rect.top),
      };
    };
    return {
      monitor: box('[data-h3-nle-area="monitor"]'),
      transport: box(".h3-nle-transport"),
      group: box(".h3-nle-transport .h3-icon-group"),
      row: box(".h3-nle-transport .h3-icon-row"),
      timecode: box("[data-h3-nle-timecode]"),
    };
  }, OVERLAY);
}

test("nothing in the workspace overflows the dialog below the scroll floor", async ({
  page,
}) => {
  // The same setup as the reference shell's floor case: a 656 px viewport leaves a 640 px
  // workspace, under the 688 px floor, and the stage is the only thing allowed to scroll.
  await page.setViewportSize({ width: 656, height: 600 });
  await openIntegratedShell(page, "smoke");
  await paused(page);
  const report = await page.evaluate((overlay) => {
    const dialog = document.querySelector<HTMLElement>(overlay)!;
    const stage = dialog.querySelector<HTMLElement>(".h3-nle-stage")!;
    const limit = dialog.getBoundingClientRect().right;
    const offenders: { selector: string; over: number; width: number }[] = [];
    for (const element of dialog.querySelectorAll<HTMLElement>("*")) {
      if (stage.contains(element)) continue; // the stage is the one scrollport
      const rect = element.getBoundingClientRect();
      if (rect.width === 0 || rect.right <= limit + 1) continue;
      offenders.push({
        selector: `${element.tagName.toLowerCase()}.${element.className}`,
        over: Math.round(rect.right - limit),
        width: Math.round(rect.width),
      });
    }
    const header = dialog.querySelector<HTMLElement>(".h3-nle-header")!;
    const children = [...header.children].map((child) => ({
      selector: `${child.tagName.toLowerCase()}.${child.className}`,
      width: Math.round(child.getBoundingClientRect().width),
    }));
    return {
      overflow: dialog.scrollWidth - dialog.clientWidth,
      width: Math.round(dialog.getBoundingClientRect().width),
      offenders,
      children,
    };
  }, OVERLAY);
  expect(report.offenders, JSON.stringify(report)).toEqual([]);
  expect(report.overflow, JSON.stringify(report)).toBeLessThanOrEqual(0);
});

test("the transport row sits under the picture area and seek authority lives on the timeline ruler", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await paused(page);
  const buttons = page.locator(`${OVERLAY} .h3-nle-transport button`);
  await expect(buttons).toHaveCount(5);
  const boxes = await buttons.evaluateAll((nodes) =>
    nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      return {
        width: rect.width,
        height: rect.height,
        top: rect.top,
        name: node.getAttribute("aria-label") ?? "",
      };
    }),
  );
  const top = Math.min(...boxes.map((box) => box.top));
  const shot = await settled(page, "transport row");
  // The picture area ends above the transport: the row takes its own height, never the area's.
  expect(shot.area.y + shot.area.height).toBeLessThanOrEqual(top + 1);
  await expect(
    page
      .locator(".h3-nle-monitor")
      .locator('[data-h3-nle-control="transport.seek"]'),
  ).toHaveCount(0);
  const seek = playheadSlider(page);
  await expect(seek).toBeVisible();
  expect((await seek.boundingBox())!.height).toBeGreaterThanOrEqual(
    await nleTargetFloor(page),
  );
});

// M25-45 (B-M2545-32): two facts the transport row owns, and neither had a fast pin. The row
// M25-46 moves seek authority to the timeline ruler. A ruler scrub pauses once before seeking and
// stays paused; an explicit Play then resumes from the accepted target. This is intentionally
// different from the removed monitor range's M25-45 manual-move behavior.
test("a ruler seek during playback pauses, then explicit Play resumes from its target", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await paused(page);
  const status = page.locator(MONITOR_STATUS);
  await control(page, "transport.play").click();
  // The first play starts a decoder, which is why the Space case allows the same window here.
  await expect(status).toHaveText("Monitor playing.", { timeout: 30_000 });
  await expect(control(page, "transport.play")).toHaveCount(0);
  await expect(control(page, "transport.pause")).toHaveCount(1);

  const target = 5 * 24;
  const seek = playheadSlider(page);
  await seekPlayhead(page, seek, target);
  await expect(status).toHaveText("Monitor paused.");
  await expect(control(page, "transport.play")).toHaveCount(1);
  await expect(control(page, "transport.pause")).toHaveCount(0);

  await control(page, "transport.play").click();
  await expect
    .poll(() => playheadFrame(seek), {
      timeout: 20_000,
    })
    .toBeGreaterThan(target);
  await expect(status).toHaveText("Monitor playing.");
  await expect(control(page, "transport.play")).toHaveCount(0);

  await control(page, "transport.pause").click();
  await expect(status).toHaveText("Monitor paused.");
  await expect(control(page, "transport.play")).toHaveCount(1);
  await expect(control(page, "transport.pause")).toHaveCount(0);
});

test("fit and 100% change only the view, and issue no composition transaction", async ({
  page,
}) => {
  const oracle = await openIntegratedShell(page, "smoke");
  await paused(page);
  const before = oracle.transactions.length;
  const fit = await settled(page, "fit");
  expect(fit.view).toBe("fit");

  await control(page, "transport.view").click();
  await expect(
    page.locator(`${OVERLAY} [data-h3-nle-view="actual"]`),
  ).toHaveCount(1);
  // The view flips in the render; the picture is re-measured in the effect that follows it, so
  // the size settles one frame later.
  await expect
    .poll(async () => (await picture(page)).canvas.width)
    .toBe(Math.min(Math.round(fit.area.width), fit.composition.width));
  const actual = await picture(page);
  // 100% is one composition pixel per CSS pixel, clipped by the area -- never an upscale.
  expect(actual.canvas.width).toBeLessThanOrEqual(actual.area.width + 1);
  near(
    actual.canvas.height,
    Math.min(actual.area.height, actual.composition.height),
    2,
  );

  await control(page, "transport.view").click();
  await expect.poll(async () => (await picture(page)).view).toBe("fit");
  await expect
    .poll(async () => Math.round((await picture(page)).canvas.width))
    .toBe(Math.round(fit.canvas.width));
  const back = await picture(page);
  near(back.canvas.width, fit.canvas.width, 2);
  // A view is not an edit.
  expect(oracle.transactions.length).toBe(before);
});

test("full screen either takes the monitor or refuses boundedly, and Escape leaves it first", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await paused(page);
  await control(page, "transport.fullscreen").click();
  const owned = await page.evaluate((overlay) => {
    const element = document.fullscreenElement;
    return (
      element !== null &&
      document.querySelector(overlay)!.contains(element) &&
      element.classList.contains("h3-nle-monitor")
    );
  }, OVERLAY);
  if (owned) {
    await expect(control(page, "transport.fullscreen")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // Escape belongs to full screen before it belongs to the dialog.
    await page.keyboard.press("Escape");
    await expect
      .poll(() => page.evaluate(() => document.fullscreenElement !== null))
      .toBe(false);
    await expect(page.locator(OVERLAY)).toBeVisible();
  } else {
    // A refusal is announced and changes nothing about the monitor.
    await expect(
      page.locator(`${OVERLAY} [data-h3-nle-status="fullscreen"]`),
    ).not.toHaveText("");
  }
  await paused(page);
  expect(await page.locator(OVERLAY).count()).toBe(1);
});

test("the monitor owns Space and the frame-step keys without disturbing the host", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await paused(page);
  const seek = playheadSlider(page);
  const monitor = page.locator(`${OVERLAY} .h3-nle-monitor`);
  await monitor.focus();
  await expect(monitor).toBeFocused();
  const start = await playheadFrame(seek);

  await page.keyboard.press(".");
  await expect.poll(() => playheadFrame(seek)).toBe(start + 1);
  await page.keyboard.press(",");
  await expect.poll(() => playheadFrame(seek)).toBe(start);

  await page.keyboard.press(" ");
  await expect(page.locator(MONITOR_STATUS)).toHaveText("Monitor playing.", {
    timeout: 30_000,
  });
  await page.keyboard.press(" ");
  await paused(page);

  // The view and full-screen controls activate from the keyboard, each exactly once: Space on a
  // focused button is the button's own activation, and the monitor's Space shortcut stands aside
  // rather than toggling playback in the same press.
  await control(page, "transport.view").focus();
  // Focus opens the control's own description, which is what an icon-only row owes its user.
  // M25-64: the view pair is its own group, with its own description.
  const tip = page.locator(
    `${OVERLAY} .h3-nle-transport [data-h3-nle-group="transport_view"] [role="tooltip"]`,
  );
  await expect(tip).toBeVisible();
  expect((await tip.textContent())?.trim().length ?? 0).toBeGreaterThan(0);
  await page.keyboard.press("Enter");
  await expect(
    page.locator(`${OVERLAY} [data-h3-nle-view="actual"]`),
  ).toHaveCount(1);
  await page.keyboard.press(" ");
  await expect(page.locator(`${OVERLAY} [data-h3-nle-view="fit"]`)).toHaveCount(
    1,
  );
  await paused(page);

  await control(page, "transport.fullscreen").focus();
  await page.keyboard.press("Enter");
  if (await page.evaluate(() => document.fullscreenElement !== null)) {
    await page.keyboard.press("Escape");
    await expect
      .poll(() => page.evaluate(() => document.fullscreenElement !== null))
      .toBe(false);
  }
  // The overlay is still open: a consumed transport key never reached the dialog's Escape or the
  // page behind it.
  await expect(page.locator(OVERLAY)).toBeVisible();
  await paused(page);
});
