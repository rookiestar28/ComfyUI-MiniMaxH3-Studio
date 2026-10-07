import { expect, test, type Page } from "@playwright/test";
import type { ManagedSequenceHarnessSnapshot } from "../../../e2e/managedSequence";

async function installManagedSequenceHarness(page: Page): Promise<void> {
  await page.goto("/?mode=managed-sequence");
  await expect(
    page.getByRole("button", { name: "Generate approved sequence" }),
  ).toBeVisible();
  await expect(page.locator("#managed-status")).toHaveText("idle");
}

async function snapshot(page: Page): Promise<ManagedSequenceHarnessSnapshot> {
  return page.evaluate(() => {
    if (window.h3ManagedSequenceHarness === undefined)
      throw new Error("managed harness unavailable");
    return window.h3ManagedSequenceHarness.snapshot();
  });
}

async function sendTerminalPair(page: Page, promptId: string): Promise<void> {
  await page.evaluate(async (exactPromptId) => {
    if (window.h3ManagedSequenceHarness === undefined)
      throw new Error("managed harness unavailable");
    await window.h3ManagedSequenceHarness.sendTerminalPair(exactPromptId);
  }, promptId);
}

test("explicit start and exact terminal authority advance one serial child at a time", async ({
  page,
}) => {
  await installManagedSequenceHarness(page);
  expect((await snapshot(page)).queueCalls).toBe(0);

  await page
    .getByRole("button", { name: "Generate approved sequence" })
    .click();
  await expect(page.locator("#managed-status")).toHaveText("active");
  expect((await snapshot(page)).queueCalls).toBe(1);

  await sendTerminalPair(page, "prompt.foreign");
  expect((await snapshot(page)).queueCalls).toBe(1);
  await sendTerminalPair(page, "prompt.1");

  await expect.poll(async () => (await snapshot(page)).queueCalls).toBe(2);
  const current = await snapshot(page);
  expect(current.runner.activeQueuePromptId).toBe("prompt.2");
  expect(current.actions.indexOf("child:release_sequence")).toBeLessThan(
    current.actions.indexOf("parent:record_terminal"),
  );
  expect(current.actions.indexOf("parent:record_terminal")).toBeLessThan(
    current.actions.indexOf("queue:segment.2"),
  );
});

test("detach revokes successor effects while the exact current child may terminalize", async ({
  page,
}) => {
  await installManagedSequenceHarness(page);
  await page
    .getByRole("button", { name: "Generate approved sequence" })
    .click();
  await expect(page.locator("#managed-status")).toHaveText("active");

  await page.evaluate(async () => {
    if (window.h3ManagedSequenceHarness === undefined)
      throw new Error("managed harness unavailable");
    await window.h3ManagedSequenceHarness.detach();
  });
  await sendTerminalPair(page, "prompt.1");

  const current = await snapshot(page);
  expect(current.queueCalls).toBe(1);
  expect(current.runner).toMatchObject({
    attached: false,
    parentState: "paused_client_absent",
    activeSegmentId: null,
  });
});
