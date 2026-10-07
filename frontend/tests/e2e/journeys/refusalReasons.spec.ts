import { expect, test } from "../fixtures/h3Page";

test("typed anchor and generation refusals replace generic browser copy", async ({
  page,
}) => {
  await page.goto("/?mode=refusal-reasons");
  const sidebar = page.locator("#refusal-reason-sidebar");

  await expect(sidebar.getByText("MiniMaxH3ImageToVideo")).toBeVisible();
  await expect(
    sidebar.getByText(/cannot be bound and run as a complete H3 flow/i),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Show generation refusal" }).click();
  const alert = sidebar.getByRole("alert");
  await expect(alert).toContainText(
    "The supported host seam is unavailable; native nodes remain available.",
  );
  await expect(alert).toContainText("Generation cannot start:");
  await expect(alert).toContainText("video model, audio VAE");
  await expect(alert.locator("p")).toHaveCount(2);
});
