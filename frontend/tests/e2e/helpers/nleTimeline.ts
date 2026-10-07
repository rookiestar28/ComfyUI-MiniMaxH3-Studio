import { expect, type Locator, type Page } from "@playwright/test";

export const playheadSlider = (page: Page): Locator =>
  page.getByRole("slider", { name: "Playhead", exact: true });

export async function playheadFrame(slider: Locator): Promise<number> {
  return Number(await slider.getAttribute("aria-valuenow"));
}

async function rulerGeometry(page: Page, slider: Locator) {
  const timeline = page.locator('[data-h3-nle-region="timeline"]');
  // IMPORTANT: revealing a virtualized track can scroll the outer workspace as well as its grid.
  // Raw mouse coordinates from an off-viewport ruler hit another surface and leave frame zero.
  await slider.scrollIntoViewIfNeeded();
  const surface = await slider.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return {
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
      max: Number(element.getAttribute("aria-valuemax") ?? 0),
    };
  });
  return {
    ...surface,
    // M25-61: frame 0 sits at the published lane origin (header plus lead-in), not the header edge.
    origin: Number(await timeline.getAttribute("data-h3-nle-lane-origin-px")),
    scale: Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
    viewStart: Number(await timeline.getAttribute("data-h3-nle-view-start")),
  };
}

/**
 * GUARD (M25-64, B-M2561-04): the ruler is pressed with no wait for the transport to be "steady".
 * An accepted edit -- a selection included -- still rebinds the monitor, but the ruler stays
 * available and a press sent during the rebind is held for the new owner. M25-61 had added a
 * 150 ms steady-state wait here to step around the dropped press; restoring one would hide that
 * defect from every journey that seeks.
 */
/**
 * M25-64 A64-8 (B-M2561-04): one ruler press at `frame`, with no wait for the transport to be
 * steady first. Resolves to the pressed frame; the caller asserts where the playhead lands.
 */
export async function pressRuler(
  page: Page,
  slider: Locator,
  frame: number,
): Promise<number> {
  const geometry = await rulerGeometry(page, slider);
  const target = Math.min(geometry.max, Math.max(0, Math.round(frame)));
  const x =
    geometry.x +
    geometry.origin +
    (target - geometry.viewStart) * geometry.scale;
  await page.mouse.click(x, geometry.y + geometry.height / 2);
  return target;
}

export async function seekPlayhead(
  page: Page,
  slider: Locator,
  frame: number,
  touch = false,
  fitAttempted = false,
  scrollAttempted = false,
): Promise<number> {
  const geometry = await rulerGeometry(page, slider);
  const target = Math.min(geometry.max, Math.max(0, Math.round(frame)));
  if (
    !Number.isFinite(geometry.origin) ||
    !Number.isFinite(geometry.scale) ||
    !Number.isFinite(geometry.viewStart)
  )
    throw new Error("playhead_geometry_unavailable");
  const x =
    geometry.x +
    geometry.origin +
    (target - geometry.viewStart) * geometry.scale;
  if (x < geometry.x + geometry.origin || x > geometry.x + geometry.width - 1) {
    if (!fitAttempted) {
      await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
      return seekPlayhead(page, slider, target, touch, true);
    }
    if (!scrollAttempted) {
      // IMPORTANT: Fit preserves the scale floor; a long timeline still needs its real
      // viewport scrollbar. A forced off-ruler click can hit an unrelated editor action.
      // The full-size equivalent is in More; hidden range inputs are not pointer targets.
      const more = page.locator('[data-h3-nle-control="toolbar.more"]');
      const scroll = page.locator('[data-h3-nle-control="transport.scroll"]');
      const reveal = !(await scroll.isVisible());
      if (reveal) {
        if (touch) await more.tap();
        else await more.click();
      }
      await expect(scroll).toBeVisible();
      await scroll.scrollIntoViewIfNeeded();
      const box = (await scroll.boundingBox())!;
      const maximum = Number(await scroll.getAttribute("max"));
      const visibleFrames = (geometry.width - geometry.origin) / geometry.scale;
      const start = Math.max(0, Math.min(maximum, target - visibleFrames / 2));
      if (!Number.isFinite(maximum) || maximum <= 0)
        throw new Error(`playhead_scroll_unavailable:${target}`);
      const inset = Math.min(10, box.width / 4);
      const x = box.x + inset + (box.width - 2 * inset) * (start / maximum);
      if (touch) await page.touchscreen.tap(x, box.y + box.height / 2);
      else await page.mouse.click(x, box.y + box.height / 2);
      if (reveal) {
        if (touch) await more.tap();
        else await more.click();
      }
      return seekPlayhead(page, slider, target, touch, true, true);
    }
    throw new Error(`playhead_target_outside_view:${target}`);
  }
  const y = geometry.y + geometry.height / 2;
  if (touch) await page.touchscreen.tap(x, y);
  else await page.mouse.click(x, y);
  await expect.poll(() => playheadFrame(slider)).toBe(target);
  return target;
}

export async function seekPlayheadFraction(
  page: Page,
  slider: Locator,
  fraction: number,
  touch = false,
): Promise<number> {
  const maximum = Number(await slider.getAttribute("aria-valuemax"));
  return seekPlayhead(
    page,
    slider,
    maximum * Math.min(1, Math.max(0, fraction)),
    touch,
  );
}
