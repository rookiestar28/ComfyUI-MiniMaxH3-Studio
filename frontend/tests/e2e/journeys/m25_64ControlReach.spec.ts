// M25-64 A64-5 (B-M2564-02): every primary control stays reachable beside the splitters.
//
// Each separator is a 44 px target centred on its 4 px gutter (M25-21), so it overlaps each
// neighbouring region by 20 px, and it is the element under the pointer there. A control inside
// that band loses the part it overlaps: a press there starts a splitter drag, and a double press
// resets the layout. This journey holds every enabled control of the editor, at each size of the
// A64-5 matrix, to two rules:
//   1. the control is the element under its own centre;
//   2. through that centre, at least 24 CSS px of it (WCAG 2.5.8's minimum target), or all of it
//      where it is smaller, is clear of every splitter, horizontally and vertically.
// Controls inside a scroll area are brought into view first, as focus would bring them. The part
// of a control its clipping ancestors hide is not part of its target: a transform handle is
// centred on the layer's edge and only its inner half lies inside the clipped picture.
//
// One pinned exception to rule 1: the transform move surface is the whole layer and the handles
// lie on it by design (M25-53 D-3), so a small layer's centre can be a handle. Rule 2 still holds
// for it, and `nleTransformOverlay.spec.ts` drags it.

import { expect, test, type Page } from "@playwright/test";

import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";

const SIZES = [
  [720, 480],
  [960, 600],
  [1280, 720],
  [1402, 868],
  [1600, 900],
  [1920, 1080],
] as const;

type Finding = Readonly<{
  control: string;
  reason: string;
}>;

async function findings(page: Page): Promise<Finding[]> {
  return page.evaluate(() => {
    const MIN = 24;
    const selector =
      'button, input, select, textarea, [role="tab"], [role="slider"], [role="spinbutton"]';
    const dialog = document.querySelector<HTMLElement>(".h3-nle-dialog")!;
    const name = (element: HTMLElement) =>
      element.getAttribute("data-h3-nle-control") ??
      element.getAttribute("data-h3-nle-transform-handle") ??
      element.getAttribute("aria-label") ??
      (element.textContent ?? "").trim().slice(0, 24) ??
      element.tagName;
    const clipped = (element: HTMLElement) => {
      const box = element.getBoundingClientRect();
      let left = box.left;
      let top = box.top;
      let right = box.right;
      let bottom = box.bottom;
      for (
        let parent = element.parentElement;
        parent !== null && parent !== document.body;
        parent = parent.parentElement
      ) {
        const style = getComputedStyle(parent);
        if (style.overflowX !== "visible" || style.overflowY !== "visible") {
          const clip = parent.getBoundingClientRect();
          left = Math.max(left, clip.left);
          top = Math.max(top, clip.top);
          right = Math.min(right, clip.right);
          bottom = Math.min(bottom, clip.bottom);
        }
      }
      return { left, top, right, bottom };
    };
    const own = (element: HTMLElement, x: number, y: number) => {
      const hit = document.elementFromPoint(x, y);
      return hit !== null && (hit === element || element.contains(hit));
    };
    const splitter = (x: number, y: number) =>
      document.elementFromPoint(x, y)?.closest("[data-h3-nle-splitter]") !=
      null;
    const out: { control: string; reason: string }[] = [];
    for (const element of dialog.querySelectorAll<HTMLElement>(selector)) {
      if (element.closest("[data-h3-nle-splitter]") !== null) continue;
      if ((element as HTMLButtonElement).disabled) continue;
      if (element.closest("[hidden], [inert], .h3-nle-vh") !== null) continue;
      const style = getComputedStyle(element);
      if (style.visibility === "hidden" || style.pointerEvents === "none")
        continue;
      if (element.getClientRects().length === 0) continue;
      element.scrollIntoView({ block: "nearest", inline: "nearest" });
      const box = clipped(element);
      const width = box.right - box.left;
      const height = box.bottom - box.top;
      if (width < 1 || height < 1) continue;
      const cx = box.left + width / 2;
      const cy = box.top + height / 2;
      const surface =
        element.getAttribute("data-h3-nle-transform-handle") === "move";
      if (!surface && !own(element, cx, cy)) {
        const hit = document.elementFromPoint(cx, cy);
        out.push({
          control: name(element),
          reason: `centre is ${hit?.tagName}.${String(hit?.className).slice(0, 32)}`,
        });
        continue;
      }
      let across = 0;
      for (let x = Math.floor(box.left) + 0.5; x < box.right; x += 1)
        if (!splitter(x, cy)) across += 1;
      let down = 0;
      for (let y = Math.floor(box.top) + 0.5; y < box.bottom; y += 1)
        if (!splitter(cx, y)) down += 1;
      const needAcross = Math.min(MIN, Math.floor(width));
      const needDown = Math.min(MIN, Math.floor(height));
      if (across < needAcross || down < needDown)
        out.push({
          control: name(element),
          reason: `clear ${across}x${down} of ${Math.round(width)}x${Math.round(height)}`,
        });
    }
    return out;
  });
}

for (const [width, height] of SIZES) {
  test(`A64-5: every editor control keeps a usable target beside the splitters at ${width} x ${height}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    await openIntegratedShell(page, "smoke");
    await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
      "Monitor paused.",
      { timeout: 60_000 },
    );
    // A selected clip puts the transform handles on the picture and the property rows in the
    // inspector, the two surfaces the splitters border most closely.
    await page
      .locator(
        '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
      )
      .click();
    await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
    await expect(
      page.locator('[data-h3-nle-transform-overlay="clip-0"]'),
    ).toBeVisible();
    expect(await findings(page)).toEqual([]);
    // The bin's other two panes put their own controls beside the same splitters.
    for (const pane of ["text", "sequence"] as const) {
      await page.locator(`[data-h3-nle-pane="${pane}"]`).click();
      await expect(
        page.locator(`[data-h3-nle-pane="${pane}"]`),
      ).toHaveAttribute("aria-selected", "true");
      expect(await findings(page), pane).toEqual([]);
    }
  });
}
