import { expect, test, type Page } from "../fixtures/h3Page";

type HarnessSnapshot = Readonly<{
  queueCount: number;
  coordinatorActions: readonly string[];
}>;

type HarnessCommand =
  | "dispatchBootstrap"
  | "dropHostSocket"
  | "reconnectingHostSocket"
  | "restoreHostSocket";

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

async function dispatch(page: Page, action: HarnessCommand): Promise<void> {
  await page.evaluate((requested) => {
    const harness = (
      window as unknown as {
        h3ManagedEntryHarness?: Record<string, () => void>;
      }
    ).h3ManagedEntryHarness;
    if (harness === undefined)
      throw new Error("managed entry harness is absent");
    harness[requested]!();
  }, action);
}

/**
 * Choose the run state the coordinator has on record when the reconnect reads it back.
 *
 * The double always answers `read_managed_run` with the disposition `current`, exactly as the real
 * adapter does; only the recorded sequence projection differs between these cases.
 */
async function setReconcileRecord(
  page: Page,
  record: "running" | "succeeded" | "unknown",
): Promise<void> {
  await page.evaluate((requested) => {
    const harness = (
      window as unknown as {
        h3ManagedEntryHarness?: {
          setReconcileRecord(value: string): void;
        };
      }
    ).h3ManagedEntryHarness;
    if (harness === undefined)
      throw new Error("managed entry harness is absent");
    harness.setReconcileRecord(requested);
  }, record);
}

/** Drive the real Sidebar entry to a live managed run owned by the host. */
async function reachGeneratingRun(page: Page): Promise<void> {
  const intent = page.getByRole("textbox", { name: "Intent" });
  await expect(intent).toBeVisible();
  await intent.fill("A synthetic eight-second managed H3 browser journey.");
  await page
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("8");
  await expect(page.getByText("Delivers 8 s (192 frames).")).toBeVisible();
  const start = page.getByRole("button", {
    name: "Apply and queue current H3 graph",
  });
  await expect(start).toBeEnabled();
  await start.click();
  await expect.poll(async () => (await snapshot(page)).queueCount).toBe(1);
  await dispatch(page, "dispatchBootstrap");
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions)
    .toEqual(["prepare_managed_run", "submit_managed_run"]);
  await expect(
    page.locator('[data-app-mode-phase="generating"]'),
  ).toBeVisible();
}

test("a socket drop during a managed run reconciles instead of failing the run", async ({
  page,
}) => {
  await page.goto("/?mode=managed-entry");
  await reachGeneratingRun(page);

  await dispatch(page, "dropHostSocket");
  const interrupted = page.locator('[data-shell-status="host_unavailable"]');
  await expect(interrupted).toBeVisible();
  await expect(interrupted).toHaveAttribute("data-host-availability", "lost");
  await expect(interrupted).toHaveAttribute(
    "data-shell-interrupted",
    "working",
  );
  // A lost host is not an App Mode failure and offers nothing that could queue.
  await expect(page.locator('[data-shell-status="error"]')).toHaveCount(0);
  await expect(page.locator('[data-h3-focus-key="app-submit"]')).toBeDisabled();
  expect((await snapshot(page)).queueCount).toBe(1);

  await dispatch(page, "reconnectingHostSocket");
  await expect(interrupted).toHaveAttribute(
    "data-host-availability",
    "reconnecting",
  );

  await dispatch(page, "restoreHostSocket");
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions)
    .toEqual(["prepare_managed_run", "submit_managed_run", "read_managed_run"]);
  await expect(
    page.locator('[data-app-mode-phase="generating"]'),
  ).toBeVisible();
  await expect(
    page.locator('[data-shell-status="host_unavailable"]'),
  ).toHaveCount(0);
  // The reconnect read the run back; it never queued a second prompt.
  expect((await snapshot(page)).queueCount).toBe(1);
});

test("a reconnect refuses when the backend no longer knows the run", async ({
  page,
}) => {
  await page.goto("/?mode=managed-entry");
  await reachGeneratingRun(page);

  await setReconcileRecord(page, "unknown");
  await dispatch(page, "dropHostSocket");
  await expect(
    page.locator('[data-shell-status="host_unavailable"]'),
  ).toBeVisible();
  await dispatch(page, "restoreHostSocket");

  await expect(page.locator('[data-shell-status="error"]')).toBeVisible();
  await expect(
    page.getByText("This run's state could not be confirmed safely."),
  ).toBeVisible();
  expect((await snapshot(page)).queueCount).toBe(1);
});

test("a reconnect terminalizes from the run state the coordinator already recorded", async ({
  page,
}) => {
  await page.goto("/?mode=managed-entry");
  await reachGeneratingRun(page);

  // The terminal reached the coordinator and was recorded, but its reply never came back before
  // the socket dropped, so the shell is still generating while the record says succeeded. The
  // reconnect reads that record rather than the read's disposition, which is always `current`.
  await setReconcileRecord(page, "succeeded");
  await dispatch(page, "dropHostSocket");
  await expect(
    page.locator('[data-shell-status="host_unavailable"]'),
  ).toBeVisible();
  await dispatch(page, "restoreHostSocket");

  await expect(page.locator('[data-shell-status="projected"]')).toBeVisible();
  await expect(page.locator('[data-shell-status="error"]')).toHaveCount(0);
  expect((await snapshot(page)).queueCount).toBe(1);
});
