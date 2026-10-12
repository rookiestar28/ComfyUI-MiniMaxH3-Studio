import { expect, test } from "@playwright/test";
import { openIntegratedShell, shellSurface } from "../helpers/nleShell";

for (const locale of ["en", "zh-TW", "zh-CN"] as const) {
  for (const state of ["standard", "narrow", "dense"] as const) {
    test(`timeline component parity ${locale} ${state}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({
        width: state === "narrow" ? 704 : 1402,
        height: 868,
      });
      const oracle = await openIntegratedShell(
        page,
        state === "dense" ? "virtualized" : "smoke",
        { extraParams: { locale }, expandOverlay: false },
      );
      await page
        .locator("#root")
        .getByRole("button", { name: "Open full editor" })
        .click();
      const overlay = page.locator(shellSurface);
      await expect(
        overlay.locator('[data-h3-nle-control="transport.play"]'),
      ).toBeEnabled();
      const timeline = overlay.locator('[data-h3-nle-region="timeline"]');
      await expect(timeline.getByRole("grid")).toBeVisible();
      const rows = timeline.locator("[data-h3-nle-track]");
      const clips = timeline.locator("[data-h3-nle-clip]");
      expect(await rows.count()).toBeGreaterThan(0);
      expect(await rows.count()).toBeLessThanOrEqual(8);
      expect(await clips.count()).toBeGreaterThan(0);
      expect(await clips.count()).toBeLessThanOrEqual(96);
      for (const control of ["transport.snap", "transport.ripple"]) {
        const button = timeline.locator(`[data-h3-nle-control="${control}"]`);
        const original = await button.getAttribute("aria-pressed");
        await button.click();
        await expect(button).toHaveAttribute(
          "aria-pressed",
          original === "true" ? "false" : "true",
        );
        await button.click();
        await expect(button).toHaveAttribute("aria-pressed", original!);
      }
      const before = oracle.transactions.length;
      await clips
        .first()
        .locator('[data-h3-nle-control="selection.set"]')
        .click({
          modifiers: ["Control"],
        });
      await expect.poll(() => oracle.transactions.length).toBe(before + 1);
      const ruler = timeline.locator('[data-h3-nle-control="transport.seek"]');
      await ruler.press("Home");
      await ruler.press("ArrowRight");
      await expect(ruler).toHaveAttribute("aria-valuenow", "1");
      await ruler.press("Home");
      await expect(ruler).toHaveAttribute("aria-valuenow", "0");
      await ruler.blur();
      const geometry = await timeline.evaluate((element) => {
        const block = element.getBoundingClientRect();
        return [
          ...element.querySelectorAll<HTMLElement>("[data-h3-nle-control]"),
        ].map((control) => {
          const rect = control.getBoundingClientRect();
          return {
            control: control.dataset.h3NleControl,
            label: control.getAttribute("aria-label"),
            disabled: control.getAttribute("aria-disabled"),
            x: Math.round(rect.x - block.x),
            y: Math.round(rect.y - block.y),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
          };
        });
      });
      await testInfo.attach(`timeline-${locale}-${state}-geometry`, {
        contentType: "application/json",
        body: JSON.stringify(geometry),
      });
      await testInfo.attach(`timeline-${locale}-${state}`, {
        contentType: "image/png",
        body: await timeline.screenshot(),
      });
    });
  }
}
