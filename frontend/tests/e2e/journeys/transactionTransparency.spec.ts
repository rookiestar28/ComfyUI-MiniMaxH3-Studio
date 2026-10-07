import { expect, test } from "../fixtures/h3Page";

test("transaction truth remains interactive, localizable and stable across remount", async ({
  page,
}) => {
  await page.goto("/");
  const region = page.getByRole("region", { name: "Generation transaction" });
  await expect(region).toBeVisible();
  await expect(region.getByText("prepared")).toBeVisible();
  await expect(region.getByText("segment.1")).toBeVisible();
  await region.getByRole("button", { name: "Confirm native queue" }).click();
  await expect(page.locator("#last-action")).toHaveText("confirm_native_queue");

  await page.getByLabel("Locale").selectOption("zh-TW");
  await expect(page.getByRole("region", { name: "生成交易" })).toBeVisible();
  await page.getByRole("button", { name: "Show unknown ownership" }).click();
  await expect(page.getByRole("alert")).toContainText("擁有權仍不明");
  await expect(page.getByRole("button", { name: "重新執行" })).toHaveCount(0);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("region", { name: "生成交易" })).toBeInViewport();
  await page.getByRole("button", { name: "Remount sidebar" }).click();
  await expect(page.getByRole("region", { name: "生成交易" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("擁有權仍不明");
});

test("semantic proposal disclosure refetches and clears content on close", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Understand intent" }).click();
  await expect(page.getByText("Synthetic morning scene")).toBeVisible();
  await expect(page.getByRole("button", { name: /edit/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /regenerate/i })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Accept proposal" }).click();
  await expect(page.getByText(/accepted/)).toBeVisible();
  await page.getByRole("button", { name: "Close intent review" }).click();
  await expect(page.getByText("Synthetic morning scene")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Understand intent" }),
  ).toBeFocused();
  await page.getByRole("button", { name: "Understand intent" }).click();
  await expect(page.getByText("Synthetic morning scene")).toBeVisible();
  await expect(page.locator("#proposal-read-count")).toHaveText("2");
});

test("semantic proposal clarification uses the exact admitted label", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Prepare clarification" }).click();
  await page.getByRole("button", { name: "Understand intent" }).click();
  await expect(
    page.getByText("Confirm synthetic scene continuity"),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: /Confirm synthetic scene continuity/ })
    .fill("Keep the synthetic scene continuous");
  await page.getByRole("button", { name: "Submit clarifications" }).click();
  await expect(
    page.getByRole("button", { name: "Accept proposal" }),
  ).toBeVisible();
  await expect(page.locator("#proposal-action-count")).toHaveText("1");
});

test("semantic proposal reject and cancel are terminal single-attempt actions", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Understand intent" }).click();
  await page.getByRole("button", { name: "Reject proposal" }).click();
  await expect(page.getByText(/rejected/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Reject proposal" }),
  ).toHaveCount(0);

  await page.reload();
  await page.getByRole("button", { name: "Understand intent" }).click();
  await page.getByRole("button", { name: "Cancel proposal" }).click();
  await expect(page.getByText(/cancelled/)).toBeVisible();
  await expect(page.locator("#proposal-action-count")).toHaveText("1");
});

test("semantic proposal failure is retryable and busy actions stay singular", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Fail next proposal read" }).click();
  await page.getByRole("button", { name: "Understand intent" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: "Read current proposal" }).click();
  await expect(page.getByText("Synthetic morning scene")).toBeVisible();

  await page.getByRole("button", { name: "Delay proposal responses" }).click();
  await page.getByRole("button", { name: "Accept proposal" }).click();
  await expect(
    page.getByRole("button", { name: "Accept proposal" }),
  ).toBeDisabled();
  await expect(page.locator("#proposal-action-count")).toHaveText("1");
  await expect(page.getByText(/accepted/)).toBeVisible();
});

test("semantic proposal suppresses late results after close and cross-handle replacement", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Delay proposal responses" }).click();
  await page.getByRole("button", { name: "Understand intent" }).click();
  await page.getByRole("button", { name: "Close intent review" }).click();
  await page.waitForTimeout(400);
  await expect(page.getByText("Synthetic morning scene")).toHaveCount(0);

  await page.getByRole("button", { name: "Understand intent" }).click();
  await page.getByRole("button", { name: "Switch proposal handle" }).click();
  await page.waitForTimeout(400);
  await expect(page.getByText("Synthetic morning scene")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
});
