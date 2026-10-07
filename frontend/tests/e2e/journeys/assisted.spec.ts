import { expect, test } from "../fixtures/h3Page";
import { mkdirSync } from "node:fs";
import { resolve, sep } from "node:path";
import {
  stageChromeCatalog,
  stageLabelsCatalog,
} from "../../../src/i18n/catalog";

function captureDirectory(): string | undefined {
  const value = process.env.H3_ASSISTED_CAPTURE_DIR;
  if (value === undefined) return undefined;
  const planning = resolve(process.cwd(), "..", ".planning") + sep;
  const directory = resolve(value);
  if (!directory.startsWith(planning))
    throw new Error("capture directory is outside private workspace evidence");
  mkdirSync(directory, { recursive: true });
  return directory;
}

test("instruction keyboard journey and comparable synthetic audit captures", async ({
  page,
}) => {
  const directory = captureDirectory();
  for (const width of [704, 1100]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/?mode=assisted");
    await page.getByRole("tab", { name: "Audit / Validate" }).click();
    const editor = page.getByRole("textbox", { name: "Prompt revision" });
    const original = await editor.inputValue();
    const foldout = page.getByRole("button", { name: "Revision instruction" });
    await foldout.focus();
    await foldout.press("Enter");
    await page
      .getByRole("textbox", { name: "Revision instruction" })
      .fill(
        "Describe the lighting while preserving the current facts. 中文 😀",
      );
    await expect(page.locator("#assisted-action-count")).toHaveText("0");
    if (directory !== undefined)
      await page.screenshot({
        path: resolve(directory, `audit-${width}.png`),
        fullPage: true,
      });
    await page
      .getByRole("button", { name: "Refine prompt", exact: true })
      .click();
    await expect(page.locator("#assisted-last-action")).toHaveText(
      "refine_prompt",
    );
    await expect(page.getByLabel("Assisted prompt proposal")).toBeVisible();
    await expect(page.locator("#canonical-prompt")).toHaveText(original);
    if (directory !== undefined)
      await page.screenshot({
        path: resolve(directory, `proposal-${width}.png`),
        fullPage: true,
      });
    const horizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(horizontalOverflow).toBe(false);
  }
});

test("Reader and comparison retain literal text, keyboard focus and zero view actions in all locales", async ({
  page,
}) => {
  const directory = captureDirectory();
  for (const locale of ["en", "zh-TW", "zh-CN"] as const) {
    const text = stageChromeCatalog[locale];
    for (const width of [704, 1100]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(`/?mode=assisted&locale=${locale}`);
      await page
        .getByRole("tab", { name: stageLabelsCatalog[locale].audit })
        .click();
      const editor = page.getByRole("textbox", { name: text.promptRevision });
      const original = await editor.inputValue();
      await page
        .getByRole("button", { name: text.reader, exact: true })
        .click();
      const reader = page.getByRole("region", { name: text.promptReader });
      await expect(reader.locator("pre")).toHaveText(original);
      await expect(page.locator("#assisted-action-count")).toHaveText("0");
      if (directory !== undefined)
        await page.screenshot({
          path: resolve(directory, `reader-${locale}-${width}.png`),
          fullPage: true,
        });
      await reader.press("Escape");
      await expect(editor).toBeFocused();
      await expect(editor).toHaveValue(original);
      await page.getByRole("button", { name: text.optimizePrompt }).click();
      await expect(page.getByLabel(text.assistedReview)).toBeVisible();
      await editor.fill(
        "  New local edit 中文 😀 <Picture 1>\n<script>literal</script>  ",
      );
      await page
        .getByRole("button", { name: text.compareCurrentReport })
        .click();
      const comparison = page.getByRole("region", {
        name: text.promptComparison,
      });
      await expect(
        comparison
          .getByRole("region", { name: text.currentReport })
          .locator("pre"),
      ).toHaveText(original);
      await expect(
        comparison
          .getByRole("region", { name: text.aiCandidate })
          .locator("pre"),
      ).toContainText("A deliberate slow push");
      const positions = await comparison
        .locator(".h3-prompt-comparison > section")
        .evaluateAll((panels) =>
          panels.map((panel) => {
            const b = panel.getBoundingClientRect();
            return { left: b.left, top: b.top };
          }),
        );
      if (width === 704)
        expect(positions[1]!.top).toBeGreaterThan(positions[0]!.top);
      else expect(positions[1]!.left).toBeGreaterThan(positions[0]!.left);
      await expect(page.locator("#assisted-action-count")).toHaveText("1");
      if (directory !== undefined)
        await page.screenshot({
          path: resolve(directory, `compare-${locale}-${width}.png`),
          fullPage: true,
        });
      await page.getByRole("button", { name: text.closeComparison }).click();
      await expect(editor).toHaveValue(
        "  New local edit 中文 😀 <Picture 1>\n<script>literal</script>  ",
      );
      await expect(page.locator("#assisted-action-count")).toHaveText("1");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > window.innerWidth,
        ),
      ).toBe(false);
    }
  }
});

test("deferred Refine retains newer text and instruction, with pending captures and cancellation", async ({
  page,
}) => {
  const directory = captureDirectory();
  for (const locale of ["en", "zh-TW", "zh-CN"] as const) {
    const text = stageChromeCatalog[locale];
    for (const width of [704, 1100]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(`/?mode=assisted&locale=${locale}&defer_assisted=1`);
      await page
        .getByRole("tab", { name: stageLabelsCatalog[locale].audit })
        .click();
      const editor = page.getByRole("textbox", { name: text.promptRevision });
      const original = await editor.inputValue();
      await page
        .getByRole("button", { name: text.revisionInstruction, exact: true })
        .click();
      const instruction = page.getByRole("textbox", {
        name: text.revisionInstruction,
      });
      await instruction.fill("Clarify the lighting within the current facts.");
      await page
        .getByRole("button", { name: text.refinePrompt, exact: true })
        .click();
      await expect(page.locator("#assisted-action-count")).toHaveText("1");
      await expect(editor).toHaveValue(original);
      const newerPrompt = "Newer local revision 中文 😀 <Picture 1>";
      const newerInstruction = "Keep my newer instruction 中文 😀";
      await editor.fill(newerPrompt);
      await instruction.fill(newerInstruction);
      await expect(page.locator("#canonical-prompt")).toHaveText(original);
      if (directory !== undefined)
        await page.screenshot({
          path: resolve(directory, `pending-${locale}-${width}.png`),
          fullPage: true,
        });
      await page
        .getByRole("button", { name: "Finish deferred assisted request" })
        .click();
      await expect(page.getByLabel(text.assistedReview)).toBeVisible();
      await expect(editor).toHaveValue(newerPrompt);
      await expect(instruction).toHaveValue(newerInstruction);
      await expect(page.locator("#canonical-prompt")).toHaveText(original);
      await page
        .getByRole("button", { name: text.compareCurrentReport })
        .click();
      await expect(page.getByText(text.comparisonKeepsLocalEdit)).toBeVisible();
      if (directory !== undefined)
        await page.screenshot({
          path: resolve(directory, `newer-edit-review-${locale}-${width}.png`),
          fullPage: true,
        });
      await page.getByRole("button", { name: text.closeComparison }).click();
      await expect(editor).toHaveValue(newerPrompt);
      await page.getByRole("button", { name: text.rejectProposal }).click();
      await editor.fill(original);
      await page
        .getByRole("button", { name: text.refinePrompt, exact: true })
        .click();
      await expect(page.locator("#assisted-action-count")).toHaveText("3");
      await page.getByRole("button", { name: text.cancelOptimization }).click();
      await expect(page.locator("#assisted-last-action")).toHaveText(
        "cancel_assisted_execution",
      );
      await expect(page.getByLabel(text.assistedReview)).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "Finish deferred assisted request" }),
      ).toBeDisabled();
      await expect(instruction).toHaveValue(newerInstruction);
      await expect(editor).toHaveValue(original);
      await expect(page.locator("#canonical-prompt")).toHaveText(original);
      await expect(page.locator("#assisted-action-count")).toHaveText("4");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > window.innerWidth,
        ),
      ).toBe(false);
    }
  }
});

test("Optimize creates a review proposal and only Accept changes canonical prompt", async ({
  page,
}) => {
  await page.goto("/?mode=assisted");
  await page.getByRole("tab", { name: "Audit / Validate" }).click();
  const canonical = page.locator("#canonical-prompt");
  const before = await canonical.textContent();

  await page.getByRole("button", { name: "Optimize prompt" }).click();
  await expect(page.getByLabel("Assisted prompt proposal")).toBeVisible();
  await expect(canonical).toHaveText(before ?? "");

  const editor = page.getByRole("textbox", { name: "Prompt revision" });
  await expect(editor).toContainText("A deliberate slow push");
  await editor.fill("Edited proposal from explicit review.");
  await page.getByRole("button", { name: "Update proposal" }).click();
  await expect(canonical).toHaveText(before ?? "");

  await page.getByRole("button", { name: "Accept proposal" }).click();
  await expect(canonical).toHaveText("Edited proposal from explicit review.");
  await expect(page.getByLabel("Assisted prompt proposal")).toHaveCount(0);
});

test("one-tap tokens preserve caret authority and bounded 704px overflow", async ({
  page,
}) => {
  await page.goto("/?mode=assisted");
  await page.getByRole("tab", { name: "Audit / Validate" }).click();
  const toolbar = page.getByRole("toolbar", {
    name: "Reference token toolbar",
  });
  await expect(toolbar.getByRole("button")).toHaveCount(64);
  await expect(
    page
      .getByRole("combobox", { name: "Reference token picker" })
      .getByRole("option"),
  ).toHaveCount(65);
  const overflow = await toolbar.evaluate((element) => {
    const style = getComputedStyle(element);
    const bounds = element.getBoundingClientRect();
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowY: style.overflowY,
      visibleRows: new Set(
        [...element.querySelectorAll("button")]
          .filter((button) => {
            const row = button.getBoundingClientRect();
            return row.bottom > bounds.top && row.top < bounds.bottom;
          })
          .map((button) => Math.round(button.getBoundingClientRect().top)),
      ).size,
    };
  });
  expect(overflow.clientHeight).toBeLessThanOrEqual(70);
  expect(overflow.scrollHeight).toBeGreaterThan(overflow.clientHeight);
  expect(overflow.overflowY).toBe("auto");
  expect(overflow.visibleRows).toBe(2);

  const editor = page.getByRole("textbox", { name: "Prompt revision" });
  await editor.fill("Opening closing");
  await editor.evaluate((element) => {
    if (!(element instanceof HTMLTextAreaElement))
      throw new Error("prompt editor is not a textarea");
    element.focus();
    element.setSelectionRange(8, 8);
  });
  const before = await page.locator("#workspace-fingerprint").textContent();
  const chip = page.getByRole("button", {
    name: "Insert backend reference <Subject 1>, Subject, declared subject 1",
  });
  await chip.focus();
  await chip.press("Enter");

  await expect(editor).toHaveValue("Opening <Subject 1> closing");
  await expect(editor).toBeFocused();
  expect(
    await editor.evaluate((element) =>
      element instanceof HTMLTextAreaElement ? element.selectionStart : -1,
    ),
  ).toBe(19);
  await expect(page.locator("#workspace-fingerprint")).not.toHaveText(
    before ?? "",
  );
  await page.getByRole("button", { name: "Validate revision" }).click();
  await expect(page.locator("#validation-status")).toHaveText("passed");
  await expect(
    page.locator('[data-code="reference.invalid_label"]'),
  ).toHaveCount(0);
});
