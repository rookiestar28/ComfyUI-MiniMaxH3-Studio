import { expect, test } from "../fixtures/h3Page";
import { sidebarCopy, SUPPORTED_LOCALES } from "../../../src/i18n/catalog";

test("ordered references fit the sidebar floor and remain keyboard operable in every locale", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 704, height: 1100 });
  for (const locale of SUPPORTED_LOCALES) {
    const text = sidebarCopy(locale);
    await page.goto("/?mode=reference-picker");
    await page.getByLabel("Picker locale").selectOption(locale);
    const picker = page.locator(".h3-reference-source-fieldset");
    await expect(picker).toBeVisible();
    await picker.screenshot({
      path: testInfo.outputPath(`${locale}-before.png`),
    });
    const add = page.getByRole("combobox", {
      name: text.referenceImages,
      exact: true,
    });
    for (const id of ["10", "11", "12"]) await add.selectOption(id);
    await expect(add.locator('option[value="10"]')).toHaveCount(0);
    const up = page.getByRole("button", {
      name: `${text.referenceActions.moveUp} <Picture 2>`,
      exact: true,
    });
    await up.focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("button", {
        name: `${text.referenceActions.moveUp} <Picture 1>`,
        exact: true,
      }),
    ).toBeFocused();
    await expect(picker.locator(".h3-reference-row").first()).toHaveAttribute(
      "data-h3-reference-source",
      "11",
    );
    await page
      .getByRole("button", {
        name: `${text.referenceActions.moveDown} <Picture 1>`,
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("button", {
        name: `${text.referenceActions.moveDown} <Picture 2>`,
        exact: true,
      }),
    ).toBeFocused();
    await page
      .getByRole("button", {
        name: `${text.referenceActions.remove} <Picture 2>`,
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("button", {
        name: `${text.referenceActions.remove} <Picture 2>`,
        exact: true,
      }),
    ).toBeFocused();
    await page.getByLabel("Picker preset").selectOption("sample");
    await expect(picker.locator(".h3-reference-row")).toHaveCount(6);
    await expect(picker.locator(".h3-reference-counter")).toHaveText(
      text.referenceActions.counter
        .replace("{files}", "8")
        .replace("{audio}", "3"),
    );
    const geometry = await picker.evaluate((element) => {
      const rows = [
        ...element.querySelectorAll<HTMLElement>(".h3-reference-row"),
      ];
      return {
        overflow: element.scrollWidth - element.clientWidth,
        rowOverflow: rows.map((row) => row.scrollWidth - row.clientWidth),
        labelWrap: rows.map((row) => {
          const labels = row.querySelector<HTMLElement>(
            ".h3-reference-labels",
          )!;
          return labels.scrollHeight - labels.clientHeight;
        }),
        actionWrap: rows.map((row) => {
          const tops = [...row.querySelectorAll("button")].map(
            (button) => button.getBoundingClientRect().top,
          );
          return Math.max(...tops) - Math.min(...tops);
        }),
        actions: rows.flatMap((row) =>
          [...row.querySelectorAll("button")].map((button) => ({
            width: button.getBoundingClientRect().width,
            height: button.getBoundingClientRect().height,
          })),
        ),
      };
    });
    expect(geometry.overflow).toBeLessThanOrEqual(0);
    expect(geometry.rowOverflow.every((value) => value <= 0)).toBe(true);
    expect(geometry.labelWrap.every((value) => value <= 0)).toBe(true);
    expect(geometry.actionWrap.every((value) => value <= 1)).toBe(true);
    expect(
      geometry.actions.every(
        (value) => value.width >= 24 && value.height >= 24,
      ),
    ).toBe(true);
    await picker.screenshot({
      path: testInfo.outputPath(`${locale}-after.png`),
    });
    for (const preset of ["audio-only", "overflow"]) {
      await page.getByLabel("Picker preset").selectOption(preset);
      const start = page.getByRole("button", {
        name: text.startAppMode,
        exact: true,
      });
      await expect(start).toBeDisabled();
      // The handler must reject a restored invalid draft even if a form submit bypasses the button.
      await start.evaluate((button) =>
        button
          .closest("form")!
          .dispatchEvent(
            new Event("submit", { bubbles: true, cancelable: true }),
          ),
      );
      await expect(page.locator("#reference-picker-starts")).toHaveText("0");
      await expect(page.locator("#h3-app-mode-source-blocker")).toBeVisible();
    }
  }
});
