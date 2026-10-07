import { expect, test, type Page } from "../fixtures/h3Page";

import { syntheticPromptUuid } from "../../support/queuePromptTestDouble";

const managedPromptId = syntheticPromptUuid(201);
const secondManagedPromptId = syntheticPromptUuid(202);

type HarnessSnapshot = Readonly<{
  queueCount: number;
  queueNodeTypes: readonly (readonly string[])[];
  queueFrameCounts: readonly number[];
  durationResolutionRequests: readonly number[];
  coordinatorExpectedFrames: readonly number[];
  coordinatorActions: readonly string[];
  productionActions: readonly string[];
  authoringActions: readonly string[];
  importCount: number;
  lifecycleEvents: readonly string[];
  managedRequestIds: readonly string[];
  requestLedgerIds: readonly string[];
  replayCollisionCount: number;
  queuePromptWrapperMode: "none" | "mutate_in_place" | "forward_copy";
  projectWorkspaceHandle: string | null;
  projectSegmentCount: number;
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
  action:
    | "dispatchBootstrap"
    | "dispatchTerminal"
    | "dispatchInterruption"
    | "dispatchArtifact"
    | "dropHostSocket"
    | "restoreHostSocket"
    | "destroySidebar"
    | "renderSidebar",
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

test("a reused custom mount releases H3 before a delayed destroy", async ({
  page,
}) => {
  await page.goto("/?mode=managed-entry");
  const container = page.locator("#managed-entry-sidebar");
  await expect(
    container.locator('[data-shell-status="interactive"]'),
  ).toBeVisible();
  await expect
    .poll(() =>
      container.evaluate((node) => ({
        container: (node as HTMLElement).style.minWidth,
        content: (node.parentElement as HTMLElement | null)?.style.minWidth,
        panel: (node.parentElement?.parentElement as HTMLElement | null)?.style
          .minWidth,
      })),
    )
    .toEqual({ container: "704px", content: "704px", panel: "704px" });

  await container.evaluate((node) => {
    const replacement = document.createElement("section");
    replacement.dataset.foreignExtension = "";
    replacement.textContent = "foreign sidebar";
    node.replaceChildren(replacement);
  });
  const replacement = container.locator(":scope > [data-foreign-extension]");
  await expect(replacement).toHaveText("foreign sidebar");
  await expect
    .poll(() =>
      container.evaluate((node) => ({
        container: (node as HTMLElement).style.cssText,
        content: (node.parentElement as HTMLElement | null)?.style.cssText,
        markerCount: node.querySelectorAll("[data-h3-context-mount]").length,
        panel: (node.parentElement?.parentElement as HTMLElement | null)?.style
          .cssText,
        styleCount: document.querySelectorAll("style[data-h3-context]").length,
      })),
    )
    .toEqual({
      container: "",
      content: "",
      markerCount: 0,
      panel: "",
      styleCount: 0,
    });

  await dispatch(page, "destroySidebar");
  await expect(replacement).toHaveText("foreign sidebar");

  await dispatch(page, "renderSidebar");
  await expect(
    container.locator('[data-shell-status="interactive"]'),
  ).toBeVisible();
  await expect(replacement).toHaveCount(0);
  await dispatch(page, "destroySidebar");
  await expect(container).toBeEmpty();
});

async function runManagedLifecycle(page: Page): Promise<HarnessSnapshot> {
  const intent = page.getByRole("textbox", { name: "Intent" });
  const duration = page.getByRole("spinbutton", {
    name: "Clip duration (seconds)",
  });
  const start = page.getByRole("button", {
    name: "Apply and queue current H3 graph",
  });
  await expect(intent).toBeVisible();
  await intent.fill("A synthetic eight-second managed H3 browser journey.");
  await duration.fill("8");
  await expect(page.getByText("Delivers 8 s (192 frames).")).toBeVisible();
  await expect(start).toBeEnabled();
  await start.click();

  await expect.poll(async () => (await snapshot(page)).queueCount).toBe(1);
  await expect(
    page.locator('[data-app-mode-phase="generating"]'),
  ).toBeVisible();
  const prepared = await snapshot(page);
  expect(prepared.coordinatorActions).toEqual([]);
  expect(prepared.productionActions).not.toContain(
    "create_workspace_from_context",
  );
  expect(prepared.queueNodeTypes[0]).toContain("MiniMaxH3ImageToVideo");
  expect(prepared.queueNodeTypes[0]).not.toContain("MiniMaxH3ReferenceToVideo");
  expect(prepared.queueNodeTypes[0]).toContain("SaveVideo");

  await dispatch(page, "dispatchBootstrap");
  await expect(
    page.locator('[data-app-mode-phase="generating"]'),
  ).toBeVisible();
  await expect
    .poll(async () => {
      const current = await snapshot(page);
      return {
        queues: current.queueCount,
        actions: current.coordinatorActions,
      };
    })
    .toEqual({
      queues: 1,
      actions: ["prepare_managed_run", "submit_managed_run"],
    });
  const generating = await snapshot(page);
  expect(generating.queueNodeTypes[0]).toContain("MiniMaxH3ImageToVideo");
  expect(generating.queueNodeTypes[0]).toContain("SaveVideo");
  expect(
    generating.lifecycleEvents.filter(
      (event) => event !== "production:read_accumulated_project",
    ),
  ).toEqual([
    `queue:${managedPromptId}`,
    "coordinator:prepare_managed_run",
    "coordinator:submit_managed_run",
  ]);

  await dispatch(page, "dispatchTerminal");
  await expect(
    page.locator('[data-app-mode-phase="verifying_output"]'),
  ).toBeVisible();
  await expect(page.locator('[data-shell-status="projected"]')).toHaveCount(0);
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions)
    .toEqual([
      "prepare_managed_run",
      "submit_managed_run",
      "close_managed_run",
    ]);

  await dispatch(page, "dropHostSocket");
  await expect(
    page.locator('[data-shell-status="host_unavailable"]'),
  ).toBeVisible();
  await dispatch(page, "restoreHostSocket");
  await expect(
    page.locator('[data-app-mode-phase="verifying_output"]'),
  ).toBeVisible();
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions)
    .toEqual([
      "prepare_managed_run",
      "submit_managed_run",
      "close_managed_run",
      "read_managed_run",
    ]);

  await dispatch(page, "dispatchArtifact");
  await expect(page.locator('[data-shell-status="projected"]')).toBeVisible();
  await page.getByRole("button", { name: "Production" }).click();
  await expect(
    page.getByText("1 of 1 segments", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Sequence authority is unavailable")).toHaveCount(
    0,
  );
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions)
    .toEqual([
      "prepare_managed_run",
      "submit_managed_run",
      "close_managed_run",
      "read_managed_run",
      "close_managed_run",
    ]);
  const completed = await snapshot(page);
  expect(completed.managedRequestIds).toHaveLength(5);
  expect(new Set(completed.managedRequestIds).size).toBe(5);
  expect(completed.replayCollisionCount).toBe(0);
  return completed;
}

async function runAccumulatingMember(
  page: Page,
  intentValue: string,
  expectedQueueCount: number,
  expectedOrdinal: number,
  expectedStartLabel = "Start H3 App Mode",
): Promise<HarnessSnapshot> {
  const intent = page.getByRole("textbox", { name: "Intent" });
  await expect(intent).toBeVisible();
  const coordinatorActionCount = (await snapshot(page)).coordinatorActions
    .length;
  await intent.fill(intentValue);
  await page
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("8");
  await expect(page.getByText("Delivers 8 s (192 frames).")).toBeVisible();

  const start = page.locator('[data-h3-focus-key="app-submit"]');
  await expect(start).toHaveText(expectedStartLabel);
  await expect(start).toBeEnabled();
  await start.click();
  if (expectedStartLabel === "Start H3 App Mode") {
    await expect(
      page.locator('[data-shell-reason="canvas_ready"]'),
    ).toBeVisible();
    expect((await snapshot(page)).queueCount).toBe(expectedQueueCount - 1);
    await expect(start).toHaveText("Apply and queue current H3 graph");
    await start.click();
  }
  await expect
    .poll(async () => (await snapshot(page)).queueCount)
    .toBe(expectedQueueCount);
  await expect(
    page.locator('[data-app-mode-phase="generating"]'),
  ).toBeVisible();

  await dispatch(page, "dispatchBootstrap");
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions.length)
    .toBe(coordinatorActionCount + 2);
  await dispatch(page, "dispatchTerminal");
  await expect(
    page.locator('[data-app-mode-phase="verifying_output"]'),
  ).toBeVisible();
  await dispatch(page, "dispatchArtifact");
  await expect(page.locator('[data-shell-status="projected"]')).toBeVisible();

  await page.getByRole("button", { name: "Production" }).click();
  await expect(
    page.getByText(`${expectedOrdinal} of ${expectedOrdinal} segments`, {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText(`Added segment ${expectedOrdinal}`, { exact: true }),
  ).toBeVisible();
  const current = await snapshot(page);
  expect(current.projectSegmentCount).toBe(expectedOrdinal);
  return current;
}

test("successive Starts and a reload append to one stable Production project", async ({
  page,
}) => {
  await page.goto(
    "/?mode=managed-entry&queueWrapper=mutate_in_place&initialGraph=empty",
  );
  const first = await runAccumulatingMember(
    page,
    "A synthetic eight-second managed H3 browser journey.",
    1,
    1,
  );
  expect(first.productionActions[0]).toBe("admit_generation_destination_v2");
  expect(first.projectWorkspaceHandle).not.toBeNull();
  expect(first.lifecycleEvents.slice(0, 5)).toEqual([
    "production:admit_generation_destination_v2",
    `queue:${managedPromptId}`,
    "coordinator:prepare_managed_run",
    "production:read_accumulated_project",
    "coordinator:submit_managed_run",
  ]);

  await page.getByRole("button", { name: "Context", exact: true }).click();
  await page.getByRole("button", { name: "Edit App Mode setup" }).click();
  await expect(
    page.locator('[data-shell-status="editing_setup"]'),
  ).toBeVisible();
  const second = await runAccumulatingMember(
    page,
    "A synthetic eight-second managed H3 browser journey.",
    2,
    2,
    "Apply and queue current H3 graph",
  );
  expect(second.projectWorkspaceHandle).toBe(first.projectWorkspaceHandle);
  expect(
    second.productionActions.filter(
      (action) => action === "admit_generation_destination_v2",
    ),
  ).toHaveLength(2);
  const secondAdmissionIndex = second.lifecycleEvents.lastIndexOf(
    "production:admit_generation_destination_v2",
  );
  expect(
    second.lifecycleEvents.slice(
      secondAdmissionIndex,
      secondAdmissionIndex + 5,
    ),
  ).toEqual([
    "production:admit_generation_destination_v2",
    `queue:${secondManagedPromptId}`,
    "coordinator:prepare_managed_run",
    "production:read_accumulated_project",
    "coordinator:submit_managed_run",
  ]);

  await page.reload();
  await expect(page.locator('[data-shell-status="interactive"]')).toBeVisible();
  const adopted = await snapshot(page);
  expect(adopted.projectWorkspaceHandle).toBe(first.projectWorkspaceHandle);
  expect(adopted.projectSegmentCount).toBe(2);
  const third = await runAccumulatingMember(
    page,
    "A synthetic eight-second managed H3 browser journey.",
    1,
    3,
  );
  expect(third.projectWorkspaceHandle).toBe(first.projectWorkspaceHandle);
  expect(third.projectSegmentCount).toBe(3);
});

test("an interrupted first attempt stays empty and the next ordinary Start commits once", async ({
  page,
}) => {
  await page.goto(
    "/?mode=managed-entry&queueWrapper=mutate_in_place&initialGraph=empty",
  );
  await page
    .getByRole("textbox", { name: "Intent" })
    .fill("A synthetic eight-second managed H3 browser journey.");
  await page
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("8");
  await expect(page.getByText("Delivers 8 s (192 frames).")).toBeVisible();
  const firstStart = page.locator('[data-h3-focus-key="app-submit"]');
  await expect(firstStart).toBeEnabled();
  await firstStart.click();
  await expect(
    page.locator('[data-shell-reason="canvas_ready"]'),
  ).toBeVisible();
  expect((await snapshot(page)).queueCount).toBe(0);
  await firstStart.click();
  await expect.poll(async () => (await snapshot(page)).queueCount).toBe(1);
  await dispatch(page, "dispatchBootstrap");
  await dispatch(page, "dispatchInterruption");
  await expect(page.locator('[data-shell-status="error"]')).toBeVisible();

  const failed = await snapshot(page);
  expect(failed.projectSegmentCount).toBe(0);
  expect(failed.queueCount).toBe(1);
  const projectHandle = failed.projectWorkspaceHandle;
  expect(projectHandle).not.toBeNull();
  await page.getByRole("button", { name: "Production" }).click();
  await expect(page.getByText("No generated segments yet.")).toBeVisible();
  await expect(page.getByText("Latest attempt: Cancelled.")).toBeVisible();

  await page.getByRole("button", { name: "Context", exact: true }).click();
  await page.getByRole("button", { name: "Edit App Mode setup" }).click();
  await expect(page.locator('[data-shell-status="interactive"]')).toBeVisible();
  await page
    .getByRole("textbox", { name: "Intent" })
    .fill("A synthetic eight-second managed H3 browser journey.");
  const nextStart = page.locator('[data-h3-focus-key="app-submit"]');
  await expect(nextStart).toBeEnabled();
  await nextStart.click();
  await expect
    .poll(
      async () =>
        (await snapshot(page)).productionActions.filter(
          (action) => action === "admit_generation_destination_v2",
        ).length,
    )
    .toBe(2);
  await expect.poll(async () => (await snapshot(page)).queueCount).toBe(2);
  await dispatch(page, "dispatchBootstrap");
  await expect
    .poll(async () => (await snapshot(page)).coordinatorActions.slice(-2))
    .toEqual(["prepare_managed_run", "submit_managed_run"]);
  await dispatch(page, "dispatchTerminal");
  await expect(
    page.locator('[data-app-mode-phase="verifying_output"]'),
  ).toBeVisible();
  await dispatch(page, "dispatchArtifact");
  await expect(page.locator('[data-shell-status="projected"]')).toBeVisible();
  await page.getByRole("button", { name: "Production" }).click();
  await expect(
    page.getByText("1 of 1 segments", { exact: true }),
  ).toBeVisible();

  // Prepare the same-session editor explicitly before testing the import action. The fixture
  // answers the real Authoring and import clients with eligible synthetic media authority; this
  // setup does not queue another generation and does not stand in for M25-40 cold-entry recovery.
  // M25-44: the Clip editor function's project summary owns the explicit start.
  await page.getByRole("tab", { name: "Clip editor" }).click();
  const summary = page.getByRole("region", {
    name: "Clip editor project",
    exact: true,
  });
  await summary
    .getByRole("button", { name: "Start authoring from this context" })
    .click();
  await expect(
    summary.getByRole("button", { name: "Refresh workspace" }),
  ).toBeEnabled();
  // An explicit open of the full editor initializes the timeline history, so the import below
  // adopts an editor that already has one (the compact editor's "Load professional timeline" did
  // this before M25-44).
  await page.locator('[data-h3-nle-entry="open"]').click();
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  await expect(overlay).toHaveAttribute("data-h3-nle-state", "expanded");
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  // B-M2561-01: since M25-48 the Production import lives in the open editor's Media pane.
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  const importSelected = overlay.locator(
    '[data-h3-nle-control="asset.import_production"]',
  );
  await expect(importSelected).toBeEnabled();
  await importSelected.click();
  await expect(
    page.getByRole("tab", { name: "Clip editor", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  // IMPORTANT (B-M2544-07): a succeeded import is published before its confirming history read,
  // and in the editor no tab switch separates the click from the sample, so wait for the recorded
  // list rather than sampling it once.
  await expect
    .poll(async () => (await snapshot(page)).authoringActions.length)
    .toBe(5);

  const succeeded = await snapshot(page);
  expect(succeeded.projectWorkspaceHandle).toBe(projectHandle);
  expect(succeeded.projectSegmentCount).toBe(1);
  expect(succeeded.queueCount).toBe(2);
  expect(
    succeeded.productionActions.filter(
      (action) => action === "admit_generation_destination_v2",
    ),
  ).toHaveLength(2);
  // M25-40: import prepares through the live Production owner (adopting this same-lineage editor),
  // reads the accepted history before posting, then reads it again to confirm the receipt assets.
  expect(succeeded.authoringActions).toEqual([
    "create_authoring_workspace",
    "initialize_timeline_history",
    "ensure_authoring_from_production",
    "read_timeline_history",
    "read_timeline_history",
  ]);
  expect(succeeded.importCount).toBe(1);
  expect(succeeded.queueCount).toBe(2);
});

test("canonical 15-second route resolution carries 362 frames through one actual queue", async ({
  page,
}) => {
  await page.goto("/?mode=managed-entry&durationSeconds=15");
  const duration = page.getByRole("spinbutton", {
    name: "Clip duration (seconds)",
  });
  await page
    .getByRole("textbox", { name: "Intent" })
    .fill("A synthetic 15-second managed H3 browser journey.");
  await duration.fill("15");
  await expect(page.getByText("Delivers 15 s (362 frames).")).toBeVisible();

  await page
    .getByRole("button", { name: "Apply and queue current H3 graph" })
    .click();
  await expect.poll(async () => (await snapshot(page)).queueCount).toBe(1);
  let current = await snapshot(page);
  expect(
    current.durationResolutionRequests.filter((value) => value === 15),
  ).toHaveLength(1);
  expect(current.queueFrameCounts).toEqual([362]);

  await dispatch(page, "dispatchBootstrap");
  await expect
    .poll(async () => (await snapshot(page)).coordinatorExpectedFrames)
    .toEqual([362]);
  current = await snapshot(page);
  expect(current.queueCount).toBe(1);
  expect(current.queueFrameCounts).toEqual([362]);
});

test("actual Sidebar entry owns verified Production success across noisy queue wrappers and a browser reload", async ({
  page,
}) => {
  await page.goto("/?mode=managed-entry&queueWrapper=mutate_in_place");
  const first = await runManagedLifecycle(page);
  expect(first.requestLedgerIds).toHaveLength(5);
  expect(first.queuePromptWrapperMode).toBe("mutate_in_place");

  // The backend replay ledger outlives a browser document. Reloading therefore
  // reproduces the real collision boundary while the second installed-wrapper
  // family still exercises src/entry and the same one-call queue contract.
  await page.evaluate(() => {
    const next = new URL(window.location.href);
    next.searchParams.set("queueWrapper", "forward_copy");
    window.history.replaceState(null, "", next);
  });
  await page.reload();
  const second = await runAccumulatingMember(
    page,
    "A synthetic eight-second managed H3 browser journey.",
    1,
    2,
    "Apply and queue current H3 graph",
  );
  expect(second.requestLedgerIds).toHaveLength(9);
  expect(second.queuePromptWrapperMode).toBe("forward_copy");
  expect(new Set(second.requestLedgerIds).size).toBe(9);
  expect(
    second.requestLedgerIds.every((requestId) =>
      /^managed\.[0-9a-f]{16}\./.test(requestId),
    ),
  ).toBe(true);
});

for (const disposition of ["missing_asset", "asset_relocated"]) {
  test(`Sidebar prepares an advisory ${disposition} canvas before a manual queue`, async ({
    page,
  }) => {
    await page.goto(
      `/?mode=managed-entry&initialGraph=empty&generationProfile=${disposition}`,
    );
    await page
      .getByRole("textbox", { name: "Intent" })
      .fill("A synthetic eight-second managed H3 browser journey.");
    await page
      .getByRole("spinbutton", { name: "Clip duration (seconds)" })
      .fill("8");
    const submit = page.locator('[data-h3-focus-key="app-submit"]');
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect(
      page.getByText("Canvas ready. Check its settings, then queue manually."),
    ).toBeVisible();
    const prepared = await snapshot(page);
    expect(prepared.queueCount).toBe(0);
    expect(prepared.productionActions).toEqual([]);
    expect(prepared.coordinatorActions).toEqual([]);
    await expect(submit).toHaveText("Apply and queue current H3 graph");
    await submit.click();
    await expect.poll(async () => (await snapshot(page)).queueCount).toBe(1);
    await expect(
      page.locator('[data-app-mode-phase="generating"]'),
    ).toBeVisible();
  });
}

test("existing managed generation never inherits a relocated materialize notice", async ({
  page,
}) => {
  await page.goto(
    "/?mode=managed-entry&generationProfile=asset_relocated&queueWrapper=mutate_in_place",
  );
  await page
    .getByRole("textbox", { name: "Intent" })
    .fill("A synthetic eight-second managed H3 browser journey.");
  await page
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("8");
  await expect(page.getByText("Delivers 8 s (192 frames).")).toBeVisible();

  const submit = page.locator('[data-h3-focus-key="app-submit"]');
  await expect(submit).toBeEnabled();
  await submit.click();

  await expect.poll(async () => (await snapshot(page)).queueCount).toBe(1);
  await expect(
    page.locator('[data-app-mode-phase="generating"]'),
  ).toBeVisible();
  await expect(page.locator("#h3-app-mode-generation-blocker")).toHaveCount(0);
  await expect(page.locator(".h3-app-mode-actions")).not.toHaveAttribute(
    "data-h3-action-scope",
    "materialize",
  );
  await expect(submit).not.toHaveAttribute(
    "aria-describedby",
    /h3-app-mode-generation-blocker/,
  );
});

test("Replace prepares a ready canvas and native model refusal ends manual queue work", async ({
  page,
}) => {
  await page.goto(
    "/?mode=managed-entry&initialGraph=dirty&queueReject=true&generationProfile=asset_relocated",
  );
  await page
    .getByRole("textbox", { name: "Intent" })
    .fill("A synthetic eight-second managed H3 browser journey.");
  await page
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("8");
  const submit = page.locator('[data-h3-focus-key="app-submit"]');
  await expect(submit).toHaveText("Replace canvas and start H3 App Mode");
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(
    page.locator('[data-shell-reason="canvas_ready"]'),
  ).toBeVisible();
  const prepared = await snapshot(page);
  expect(prepared.queueCount).toBe(0);
  expect(prepared.productionActions).toEqual([]);
  expect(prepared.coordinatorActions).toEqual([]);
  await dispatch(page, "destroySidebar");
  await dispatch(page, "renderSidebar");
  await expect(submit).toHaveText("Apply and queue current H3 graph");
  await submit.click();
  await expect(page.locator('[data-shell-status="error"]')).toBeVisible();
  await expect(page.locator('[data-shell-status="working"]')).toHaveCount(0);
  await expect(
    page.getByText(/ComfyUI rejected this graph before accepting it/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Retry H3 App Mode" }),
  ).toBeEnabled();
  expect((await snapshot(page)).queueCount).toBe(1);
});
