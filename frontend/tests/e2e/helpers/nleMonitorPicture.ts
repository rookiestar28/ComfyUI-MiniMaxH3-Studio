import { expect, type Page } from "@playwright/test";

/**
 * How the monitor's picture is read, shared by the hermetic journey and the supplied-host row.
 *
 * Both observe the same shipped shell through the same `data-h3-nle-*` projection, so they read it
 * with the same code. A host row with its own copy of this reader would be free to drift from the
 * hermetic one and answer a slightly different question about the same pixels -- and a fit rule
 * that holds under one reader and not the other is indistinguishable from a product defect until
 * somebody compares the two readers by hand. M25-45 lost three separate debugging rounds to facts
 * about a source stated somewhere other than the source; this is the same shape, so it has one
 * statement.
 */

export const NLE_OVERLAY = '[data-h3-nle-surface="overlay_v1"]';

export type Splitter = "bin_monitor" | "monitor_inspector" | "top_timeline";

export type Picture = Readonly<{
  area: Readonly<{ width: number; height: number; x: number; y: number }>;
  composition: Readonly<{ width: number; height: number }>;
  canvas: Readonly<{ width: number; height: number; x: number; y: number }>;
  backing: Readonly<{ width: number; height: number }>;
  aspect: number;
  view: string | null;
}>;

/** The picture area, the canvas inside it and the composition the area declares, in one pass. */
export function readPicture(page: Page): Promise<Picture> {
  return page.evaluate((overlay) => {
    const dialog = document.querySelector<HTMLElement>(overlay)!;
    const area = dialog.querySelector<HTMLElement>("[data-h3-nle-picture]")!;
    const canvas = area.querySelector<HTMLCanvasElement>("canvas")!;
    const [width, height] = (area.dataset.h3NlePicture ?? "0x0")
      .split("x")
      .map(Number);
    const areaBox = area.getBoundingClientRect();
    const canvasBox = canvas.getBoundingClientRect();
    return {
      area: {
        width: areaBox.width,
        height: areaBox.height,
        x: areaBox.x,
        y: areaBox.y,
      },
      composition: { width: width!, height: height! },
      canvas: {
        width: canvasBox.width,
        height: canvasBox.height,
        x: canvasBox.x,
        y: canvasBox.y,
      },
      backing: { width: canvas.width, height: canvas.height },
      aspect: width! / height!,
      view: dialog
        .querySelector("[data-h3-nle-view]")!
        .getAttribute("data-h3-nle-view"),
    };
  }, NLE_OVERLAY);
}

/** A real mouse drag from the splitter's centre along its axis. */
export async function dragSplitter(
  page: Page,
  id: Splitter,
  delta: number,
): Promise<void> {
  const handle = page.locator(`${NLE_OVERLAY} [data-h3-nle-splitter="${id}"]`);
  const box = (await handle.boundingBox())!;
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

export const near = (actual: number, expected: number, tolerance = 1) =>
  expect(
    Math.abs(actual - expected),
    `${actual} vs ${expected}`,
  ).toBeLessThanOrEqual(tolerance);

/**
 * The picture after the layout AND the backing have settled.
 *
 * IMPORTANT: a gesture changes the area synchronously, and the picture follows it one frame later
 * through the `ResizeObserver` and the React commit it schedules. Reading immediately after a drag
 * catches the canvas mid-flight, with the previous measurement's inline height and a width its
 * `max-width` has already clipped -- an aspect that never renders for a user and never lasts. The
 * contract is the settled layout, so poll for it rather than sampling the transition.
 *
 * GUARD (B-M2545-42): the CSS box settles FIRST and the backing store follows it, so a reader that
 * waits only for the layout hands back a settled rectangle over a backing that is still the
 * compositor's floor. On the supplied host that window is seconds wide, because the backing is
 * written as the surface opens its source: measured directly, a row read 320 x 180 and the same
 * canvas carried 724 x 408 **4,913 ms later**, with no gesture in between. The hermetic shell
 * closes the same window in a frame or two, which is why twelve picture cases never saw it and a
 * supplied-host row did.
 *
 * The wait is on the PRODUCT'S OWN STATE, never on the expected value. The monitor says when it has
 * a live surface -- `[data-h3-nle-status="monitor"]` reaching a presented status -- and the backing
 * is written as part of reaching it; quiescence alone is not enough, because the floor sits
 * perfectly still for those five seconds and two equal readings would accept it. Polling until the
 * backing equals what the caller is about to assert would be worse still: it would wait for the
 * answer and then check it, making every assertion through this helper tautological. So the value
 * stays entirely in the caller's assertions and only the readiness moves here.
 */
export async function settledPicture(
  page: Page,
  where: string,
): Promise<Picture> {
  await expect
    .poll(
      async () => {
        const shot = await readPicture(page);
        const width = Math.min(shot.area.width, shot.area.height * shot.aspect);
        const height = Math.min(
          shot.area.height,
          shot.area.width / shot.aspect,
        );
        return (
          Math.abs(shot.canvas.width - width) <= 1 &&
          Math.abs(shot.canvas.height - height) <= 1
        );
      },
      { message: where },
    )
    .toBe(true);
  let previous: string | null = null;
  await expect
    .poll(
      async () => {
        const presented = await page.evaluate((overlay) => {
          const dialog = document.querySelector<HTMLElement>(overlay);
          const status = dialog?.querySelector<HTMLElement>(
            '[data-h3-nle-status="monitor"]',
          );
          return (status?.textContent ?? "").trim();
        }, NLE_OVERLAY);
        // A monitor that is still opening, blocked or empty has not written a backing for this
        // box yet, and its own words say so. Only a live surface is read.
        if (!/paused|playing/i.test(presented)) {
          previous = null;
          return false;
        }
        const { backing } = await readPicture(page);
        const seen = `${backing.width}x${backing.height}`;
        const stable =
          previous === seen && backing.width > 0 && backing.height > 0;
        previous = seen;
        return stable;
      },
      { message: `${where}: backing settled behind a live monitor` },
    )
    .toBe(true);
  return readPicture(page);
}

/** The fit rule itself, stated against the composition the area declares it is fitting. */
export function expectFit(shot: Picture, where: string): void {
  const width = Math.min(shot.area.width, shot.area.height * shot.aspect);
  const height = Math.min(shot.area.height, shot.area.width / shot.aspect);
  const seen = `${where}: picture ${shot.canvas.width}x${shot.canvas.height} in area ${shot.area.width}x${shot.area.height} at ${shot.aspect}`;
  expect(Math.abs(shot.canvas.width - width), seen).toBeLessThanOrEqual(1);
  expect(Math.abs(shot.canvas.height - height), seen).toBeLessThanOrEqual(1);
  // M25-61: the canvas sits at the monitor's own whole-pixel box, never at a flex-centred half
  // pixel; the transform overlay and its alignment buttons are placed from that same box.
  const left = shot.canvas.x - shot.area.x;
  const top = shot.canvas.y - shot.area.y;
  expect(
    Math.abs(left - Math.round((shot.area.width - shot.canvas.width) / 2)),
    `${seen}: left ${left}`,
  ).toBeLessThanOrEqual(0.02);
  expect(
    Math.abs(top - Math.round((shot.area.height - shot.canvas.height) / 2)),
    `${seen}: top ${top}`,
  ).toBeLessThanOrEqual(0.02);
}

/**
 * A content-free summary of one observation, for a supplied-host row's evidence.
 *
 * Sizes and the declared composition only: the host's canvas may be showing a maintainer's own
 * media, so nothing here reads a pixel, a title, a path or an identifier.
 */
export function summarizePicture(shot: Picture): Record<string, number | null> {
  return {
    area_width: Math.round(shot.area.width),
    area_height: Math.round(shot.area.height),
    canvas_width: Math.round(shot.canvas.width),
    canvas_height: Math.round(shot.canvas.height),
    backing_width: shot.backing.width,
    backing_height: shot.backing.height,
    composition_width: shot.composition.width,
    composition_height: shot.composition.height,
  };
}

/**
 * A real canvas context loss and its restoration, through the two DOM events the composition
 * session listens for, so the monitor's unavailable state is the runtime's own.
 */
export async function loseCanvasContext(
  page: Page,
  lost: boolean,
): Promise<void> {
  await page.evaluate((isLost) => {
    const canvas = document.querySelector<HTMLCanvasElement>(
      "[data-h3-nle-root] .h3-nle-monitor canvas",
    );
    if (canvas === null) throw new Error("no monitor canvas");
    canvas.dispatchEvent(
      new Event(isLost ? "contextlost" : "contextrestored", {
        cancelable: true,
      }),
    );
  }, lost);
}

/**
 * M25-64 (A64-1, the B-M2563-09 rule): the monitor's visible overlay covers no control that stays
 * active beneath it. The transport row and every enabled transform handle are checked twice: their
 * box never meets the overlay's box, and the point of the control nearest the overlay's centre
 * still hits the control. Call it with no menu or description open.
 */
export async function expectOverlayClearOfControls(page: Page): Promise<void> {
  const report = await page.evaluate((overlay) => {
    const dialog = document.querySelector<HTMLElement>(overlay)!;
    const card = dialog.querySelector<HTMLElement>("[data-h3-nle-overlay]");
    if (card === null) return null;
    const box = card.getBoundingClientRect();
    const cx = box.left + box.width / 2;
    const cy = box.top + box.height / 2;
    const controls = [
      ...dialog.querySelectorAll<HTMLButtonElement>(
        ".h3-nle-transport button, button[data-h3-nle-transform-handle], button.h3-nle-transform-hit",
      ),
    ].filter((control) => !control.disabled);
    return {
      overlay: [box.left, box.top, box.right, box.bottom].map(Math.round),
      controls: controls.map((control) => {
        const rect = control.getBoundingClientRect();
        // Inset past the control's rounded corner: hit testing follows `border-radius`, so the
        // box's own corner pixel belongs to whatever is behind it.
        const inset = Math.min(8, rect.width / 4, rect.height / 4);
        const x = Math.min(Math.max(cx, rect.left + inset), rect.right - inset);
        const y = Math.min(Math.max(cy, rect.top + inset), rect.bottom - inset);
        const hit = document.elementFromPoint(x, y);
        return {
          name:
            control.getAttribute("data-h3-nle-control") ??
            control.getAttribute("data-h3-nle-transform-handle") ??
            control.className,
          box: [rect.left, rect.top, rect.right, rect.bottom].map(Math.round),
          hitsControl: hit !== null && control.contains(hit),
        };
      }),
    };
  }, NLE_OVERLAY);
  expect(report, "no overlay is shown").not.toBeNull();
  const detail = JSON.stringify(report);
  const [left, top, right, bottom] = report!.overlay;
  expect(report!.controls.length, detail).toBeGreaterThan(0);
  for (const control of report!.controls) {
    const [cl, ct, cr, cb] = control.box;
    const overlaps = left! < cr! && cl! < right! && top! < cb! && ct! < bottom!;
    expect(overlaps, `${control.name} ${detail}`).toBe(false);
    expect(control.hitsControl, `${control.name} ${detail}`).toBe(true);
  }
}
