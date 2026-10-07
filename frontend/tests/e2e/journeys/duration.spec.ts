import { expect, test } from "../fixtures/h3Page";

test.beforeEach(async ({ page }) => {
  await page.goto("/?mode=duration-authority");
});

test("existing-graph Sidebar authoring remains visible and authoritative", async ({
  page,
}) => {
  const intent = page.getByRole("textbox", { name: "Intent" });
  const duration = page.getByRole("spinbutton", {
    name: "Clip duration (seconds)",
  });
  const start = page.getByRole("button", {
    name: "Apply and queue current H3 graph",
  });

  await expect(
    page.getByRole("button", { name: "Continue with native nodes" }),
  ).toHaveCount(0);

  await expect(intent).toBeVisible();
  await intent.fill("A synthetic eight-second existing-canvas request.");
  const intentBox = await intent.boundingBox();
  const sidebarBox = await page
    .locator("#duration-authority-sidebar")
    .boundingBox();
  expect(intentBox).not.toBeNull();
  expect(sidebarBox).not.toBeNull();
  expect(intentBox!.height).toBeGreaterThanOrEqual(160);
  expect(
    await intent.evaluate((element) => getComputedStyle(element).resize),
  ).toBe("vertical");
  expect(intentBox!.x).toBeGreaterThanOrEqual(sidebarBox!.x);
  expect(intentBox!.x + intentBox!.width).toBeLessThanOrEqual(
    sidebarBox!.x + sidebarBox!.width,
  );

  await expect(duration).toHaveValue("5");
  await expect(duration).toHaveAttribute("min", "4");
  await expect(duration).toHaveAttribute("max", "15");
  await expect(duration).toHaveAttribute("step", "1");

  await duration.fill("6");
  await expect(duration).toHaveValue("6");
  await expect(page.getByText("Delivers 7 s (158 frames).")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("6.583");

  await duration.fill("8");
  await expect(duration).toHaveValue("8");
  await expect(page.getByText("Delivers 8 s (192 frames).")).toBeVisible();
  await expect(start).toBeEnabled();
  await start.click();
  await expect(page.locator("#duration-authority-submission")).toHaveText(
    JSON.stringify({
      user_intent: "A synthetic eight-second existing-canvas request.",
      duration_milliseconds: 8000,
      frame_count: 192,
      use_existing: true,
    }),
  );
  await expect(page.locator("#duration-authority-submission-count")).toHaveText(
    "1",
  );

  await page.getByRole("button", { name: "Edit App Mode setup" }).click();
  await expect(page.locator("#duration-authority-state")).toHaveText(
    "editing_setup",
  );
  await expect(
    page.getByRole("button", { name: "Continue with native nodes" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Deliver exact success" }).click();
  await expect(page.locator("#duration-authority-terminal")).toHaveText(
    "accepted",
  );
  await expect(page.locator("#duration-authority-state")).toHaveText(
    "editing_setup",
  );

  await page
    .getByRole("button", { name: "Apply and queue current H3 graph" })
    .click();
  await expect(page.locator("#duration-authority-submission-count")).toHaveText(
    "2",
  );
  await expect(page.locator("#duration-authority-submission")).toContainText(
    '"use_existing":true',
  );
});
