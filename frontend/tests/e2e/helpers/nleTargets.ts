import { expect, type Page } from "@playwright/test";

import { NLE_REFERENCE_UI_CONTRACT_V1 } from "../../../src/contracts/nleReferenceUiContract";

/**
 * M25-61: the editor's control floor depends on the page's primary pointer. A coarse pointer
 * (Playwright `hasTouch: true` in Chromium) keeps the 44 px touch floor; a fine pointer admits the
 * 30 px icon target and never less than the 24 px minimum. Specs read the floor from the page and
 * the contract, never from a literal, so each mode is measured against its own rule.
 */
export async function nleTargetFloor(
  page: Page,
  kind: "control" | "icon" = "control",
): Promise<number> {
  const coarse = await page.evaluate(
    () => matchMedia("(pointer: coarse)").matches,
  );
  const targets = NLE_REFERENCE_UI_CONTRACT_V1.controlTargetPx;
  if (coarse) return targets.coarse;
  return kind === "icon" ? targets.fine : targets.minimum;
}

/** The lane origin (header plus lead-in) the timeline publishes for pointer arithmetic. */
export async function nleLaneOrigin(page: Page): Promise<number> {
  const value = await page
    .locator('[data-h3-nle-region="timeline"]')
    .first()
    .getAttribute("data-h3-nle-lane-origin-px");
  const origin = Number(value);
  if (!Number.isFinite(origin) || origin <= 0)
    throw new Error("timeline_lane_origin_unavailable");
  return origin;
}

/**
 * M25-62 (R7, B-M2562-05): the one pinned exception to the fine-pointer floor. A selected clip's
 * edge grips are 8 px wide by design, the thin trim handles of a desktop editor. WCAG 2.2 AA 2.5.8
 * admits them under its "Equivalent" exception: the same edit is available through 30 px
 * toolbar controls (Trim start and Trim end to the playhead, with the ruler placing the
 * playhead), and each grip stays keyboard operable. Only this exact shape is excused -- the grip
 * class, exactly 8 px wide, still at least the floor tall, and never under a coarse pointer, where
 * the 44 px grips apply. Anything else below the floor still fails a census.
 */
export type NleGripException = Readonly<{ selector: string; widthPx: number }>;

export async function nleFineGripException(
  page: Page,
): Promise<NleGripException | null> {
  const coarse = await page.evaluate(
    () => matchMedia("(pointer: coarse)").matches,
  );
  return coarse ? null : { selector: ".h3-nle-grip", widthPx: 8 };
}

/**
 * B-M2563-09: the corner grip paints above the timeline's bottom-right corner, so the lanes' scroll
 * viewport must end above it. A lane under the grip loses its presses (a marquee, a clip) to the
 * resize control. Checked as a box overlap and as the element under the viewport's last point,
 * so call it with no menu or popover open.
 */
export async function expectGripClearOfLanes(page: Page): Promise<void> {
  const corner = await page
    .locator('[data-h3-nle-surface="overlay_v1"]')
    .evaluate((dialog) => {
      const rect = (element: Element) => {
        const box = element.getBoundingClientRect();
        return [box.left, box.top, box.right, box.bottom].map(Math.round);
      };
      const grip = dialog.querySelector('[data-h3-nle-action="resize"]')!;
      const lanes = dialog.querySelector(
        '[role="grid"][aria-label="Timeline tracks"]',
      )!;
      const box = lanes.getBoundingClientRect();
      const hit = document.elementFromPoint(box.right - 2, box.bottom - 2);
      return {
        grip: rect(grip),
        lanes: rect(lanes),
        lastPointInLanes: hit !== null && lanes.contains(hit),
        hit:
          hit === null
            ? null
            : `${hit.tagName.toLowerCase()}.${String(hit.className)}`,
      };
    });
  const detail = JSON.stringify(corner);
  const [gripLeft, gripTop, gripRight, gripBottom] = corner.grip;
  const [laneLeft, laneTop, laneRight, laneBottom] = corner.lanes;
  const overlaps =
    gripLeft! < laneRight! &&
    laneLeft! < gripRight! &&
    gripTop! < laneBottom! &&
    laneTop! < gripBottom!;
  expect(overlaps, detail).toBe(false);
  expect(corner.lastPointInLanes, detail).toBe(true);
}
