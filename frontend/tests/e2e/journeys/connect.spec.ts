import { expect, test } from "../fixtures/h3Page";

const GUIDANCE = "#h3-app-mode-connect-boundary";

test.beforeEach(async ({ page }) => {
  await page.goto("/?mode=connect-guidance");
});

test("connect guidance is neutral, responsive, and localized", async ({
  page,
}) => {
  for (const [width, locale] of [
    [704, "en"],
    [480, "zh-TW"],
  ] as const) {
    await page.setViewportSize({ width, height: 900 });
    await page.reload();
    await page.getByLabel("Guidance locale").selectOption(locale);

    const guidance = page.locator(GUIDANCE);
    await expect(guidance).toBeVisible();
    await expect(guidance).toHaveClass(/h3-app-mode-connect-guidance/);
    await expect(guidance).not.toHaveClass(/h3-app-mode-source-blocker/);
    await expect(guidance).not.toHaveAttribute("role", /.+/);
    await expect(guidance).not.toHaveAttribute("aria-live", /.+/);
    if (locale === "en") {
      await expect(guidance).toContainText("matching frame-role declaration");
      await expect(guidance).toContainText("queues the current canvas once");
      await expect(guidance).toContainText(
        "preserves every existing node setting and link",
      );
    } else {
      await expect(guidance).toContainText("相符的畫面角色宣告");
      await expect(guidance).toContainText("把目前畫布加入一次佇列");
      await expect(guidance).toContainText("保留所有既有節點設定與連線");
    }

    const presentation = await guidance.evaluate((node) => {
      const element = node as HTMLElement;
      const probe = document.createElement("span");
      probe.style.color = "var(--dg)";
      element.append(probe);
      const dangerColor = getComputedStyle(probe).color;
      probe.remove();
      const style = getComputedStyle(element);
      return {
        color: style.color,
        borderColor: style.borderLeftColor,
        dangerColor,
      };
    });
    expect(presentation.color).not.toBe(presentation.dangerColor);
    expect(presentation.borderColor).not.toBe(presentation.dangerColor);

    const overflow = await page
      .locator("#connect-guidance-sidebar")
      .evaluate((element) => element.scrollWidth - element.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  }
});

test("guidance follows the authoritative connect lifecycle", async ({
  page,
}) => {
  const guidance = page.locator(GUIDANCE);
  await expect(guidance).toBeVisible();

  await page
    .getByRole("button", { name: "Connect and queue current canvas" })
    .click();
  await expect(page.locator("#connect-guidance-state")).toHaveText("working");
  await expect(page.locator("#connect-guidance-start-count")).toHaveText("1");
  await expect(guidance).toHaveCount(0);

  await page.getByRole("button", { name: "Complete host projection" }).click();
  await expect(page.locator("#connect-guidance-state")).toHaveText("projected");
  await expect(guidance).toHaveCount(0);

  await page.getByRole("button", { name: "Remount sidebar" }).click();
  await expect(page.locator("#connect-guidance-state")).toHaveText("projected");
  await expect(guidance).toHaveCount(0);

  await page.getByRole("button", { name: "Refresh projected canvas" }).click();
  await expect(page.locator("#connect-guidance-state")).toHaveText("projected");
  await expect(guidance).toHaveCount(0);

  await page.getByRole("button", { name: "New canvas decision" }).click();
  await expect(page.locator("#connect-guidance-state")).toHaveText(
    "interactive",
  );
  await expect(guidance).toBeVisible();
});
