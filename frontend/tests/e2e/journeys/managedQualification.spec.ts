import { expect, test } from "@playwright/test";

test("readiness requires an explicit action and a held response cannot enable reads", async ({
  page,
}) => {
  const requests: string[] = [];
  await page.route(
    "**/h3-context/v1/production/planning/action",
    async (route) => {
      const request = route.request().postDataJSON();
      requests.push(request.action);
      await route.fulfill({
        json: {
          schema: "h3.context.managed_readiness.v1",
          request_id: request.request_id,
          status: "held",
          reason: "qualification_host_unqualified",
          qualification_fingerprint: null,
          qualification: null,
        },
      });
    },
  );
  await page.goto("/managedQualification.html");
  await expect(page.locator("#status")).toHaveText("idle");
  expect(requests).toEqual([]);
  await page.getByRole("button", { name: "Check readiness" }).click();
  await expect(page.locator("#status")).toHaveText("held");
  await expect(
    page.getByRole("button", { name: "Read current readiness" }),
  ).toBeDisabled();
  expect(requests).toEqual(["prepare_managed_readiness"]);
  await page.reload();
  await expect(page.locator("#status")).toHaveText("idle");
  expect(requests).toEqual(["prepare_managed_readiness"]);
});
