import { expect, test, type Page } from "../fixtures/h3Page";

import { syntheticPromptUuid } from "../../support/queuePromptTestDouble";

const managedPromptId = syntheticPromptUuid(201);

type HarnessSnapshot = Readonly<{
  queueCount: number;
  coordinatorActions: readonly string[];
}>;

async function snapshot(page: Page): Promise<HarnessSnapshot> {
  return page.evaluate(() => {
    const harness = (
      window as unknown as {
        h3ManagedEntryHarness?: { snapshot(): HarnessSnapshot };
      }
    ).h3ManagedEntryHarness;
    if (harness === undefined)
      throw new Error("managed entry harness is absent");
    return harness.snapshot();
  });
}

async function dispatch(
  page: Page,
  action: "dispatchBootstrap" | "dispatchTerminal" | "dispatchArtifact",
): Promise<void> {
  await page.evaluate((requestedAction) => {
    const harness = (
      window as unknown as {
        h3ManagedEntryHarness?: Record<string, () => void>;
      }
    ).h3ManagedEntryHarness;
    if (harness === undefined)
      throw new Error("managed entry harness is absent");
    harness[requestedAction]!();
  }, action);
}

test("failed managed run copies a bounded redacted causal journal", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const runtime = window as unknown as { copiedDiagnostics?: string };
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (payload: string) => {
          runtime.copiedDiagnostics = payload;
        },
      },
    });
  });
  await page.goto("/?mode=managed-diagnostics");

  const intent = page.getByRole("textbox", { name: "Intent" });
  const duration = page.getByRole("spinbutton", {
    name: "Clip duration (seconds)",
  });
  const start = page.getByRole("button", {
    name: "Apply and queue current H3 graph",
  });
  const privateIntent = "A synthetic eight-second managed H3 browser journey.";
  await intent.fill(privateIntent);
  await duration.fill("8");
  await expect(page.getByText("Delivers 8 s (192 frames).")).toBeVisible();
  await expect(start).toBeEnabled();
  await start.click();
  await expect.poll(async () => (await snapshot(page)).queueCount).toBe(1);
  await expect(
    page.locator('[data-app-mode-phase="generating"]'),
  ).toBeVisible();

  await dispatch(page, "dispatchBootstrap");
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions)
    .toEqual(["prepare_managed_run", "submit_managed_run"]);
  await dispatch(page, "dispatchTerminal");
  // The exact success is recorded before SaveVideo exists, while verified completion still waits
  // for the matching artifact and its separate close request.
  await expect(
    page.locator('[data-app-mode-phase="verifying_output"]'),
  ).toBeVisible();
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions)
    .toEqual([
      "prepare_managed_run",
      "submit_managed_run",
      "close_managed_run",
    ]);
  await dispatch(page, "dispatchArtifact");

  await expect(page.locator('[data-shell-status="error"]')).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Retry output verification" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Copy diagnostics" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { copiedDiagnostics?: string })
            .copiedDiagnostics,
      ),
    )
    .not.toBeUndefined();

  const payload = await page.evaluate(
    () =>
      (window as unknown as { copiedDiagnostics?: string }).copiedDiagnostics ??
      "",
  );
  expect(payload).toContain("stage bootstrap_started");
  expect(payload).toContain("stage aggregate_prepare_returned");
  expect(payload).toContain(
    "error sequence_coordinator category=artifact_store_unavailable",
  );
  expect(payload).toContain("state error code=artifact_store_unavailable");
  expect(payload).not.toContain(privateIntent);
  expect(payload).not.toContain(managedPromptId);
  expect(payload).not.toContain('"nodes"');

  const storedBytes = await page.evaluate(() => {
    const value = localStorage.getItem("h3-context.managed-journal.v1") ?? "";
    return new TextEncoder().encode(value).byteLength;
  });
  expect(storedBytes).toBeGreaterThan(0);
  expect(storedBytes).toBeLessThanOrEqual(32 * 1024);
});
