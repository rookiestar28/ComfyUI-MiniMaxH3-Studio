import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import { expect, test } from "../../host/fixture";

import { runRegistrationPhaseOne } from "../../host/candidate";
import { runRegistrationPhaseTwo } from "../../host/environment";
import { runRegistrationPhaseThree } from "../../host/layout";
import { runRegistrationPhaseFour } from "../../host/managed";
import { runRegistrationPhaseFive } from "../../host/assets";
import { runRegistrationPhaseSix } from "../../host/execution";
import * as shared from "../../host/environment";
import { runRegistrationPhaseSeven } from "../../host/network";
import type { FrontendPerformanceReceipt } from "../../host/network";

test("exact host registers, renders, updates, destroys, and stays network-local", async ({
  context,
  page,
}, testInfo) => {
  const phaseOne = await runRegistrationPhaseOne({
    shared,
    context,
    page,
    testInfo,
  });
  const phaseTwo = await runRegistrationPhaseTwo(phaseOne);
  const phaseThree = await runRegistrationPhaseThree(phaseTwo);
  const phaseFour = await runRegistrationPhaseFour(phaseThree);
  const phaseFive = await runRegistrationPhaseFive(phaseFour);
  const phaseSix = await runRegistrationPhaseSix(phaseFive);
  const phaseSeven = await runRegistrationPhaseSeven(phaseSix);
  // prettier-ignore
  const { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity } = phaseSeven.shared;
  // prettier-ignore
  const { allowedOrigin, networkAttribution, candidateNetworkAttribution, captureInteractionNetworkPhase, interactionErrors, initialInjectionCount, initialStorageKeys, registryEvidence, coInstallDefinitions, workflowTemplateDialog, appModeContainer, shellStatus, awaitAppModePhase, visualDirectory, snapshotHostStorage, snapshotHostPersistence, seedHostPersistenceSentinel, originalHostPersistence, initialHostStorage, hostPersistenceBefore, storageSentinelDigest, hostPersistenceBeforeDigest, snapshotHostOwner, layoutEvidence, captureManifest, visualPhase, candidateIdentity, hostReportPath, captureSettledScreenshot, applyLayoutCell, captureLayoutMatrix, projectedWorkspace, resetLayoutMatrixPresentation, networkAfterLayoutFixturePrep, cancelAction, preCancellationGraph, cancellationReason, cancellationReceipt, appModeQueueRequests, appModeNetworkShapes, appModeResponseShapes, appModeExecutedShapes, layoutTaskMode, layoutDuration, appModeEntry, appModePromptTypes, appModeGraph, metadata, githubLink, metadataStyles, networkAfterAppModeStart, fixture, prompt, executionPrompt, networkAfterDirectGraphLoad, response, responseBody, promptId, executionReceipt, projectionInventory, graphBeforeSetupEdit, executedBeforeSetupEdit, networkBeforeSetupEdit, networkAfterSetupEdit, networkAfterDirectPromptProjection, networkAfterProjectedLayoutMatrix, initialWorkspace, promptEditor, initialStageResponse, initialStageResult, initialStageBody, initialStageProjection, staleResponse, downloadEvent, download, downloadPath, transferText, transfer, networkAfterWorkspaceActions, appModePrompt, appModeResponse, appModeParity, repeatedPrompt, repeatedActionRequest, repeatedActionBody, networkAfterDirectProjection, subgraph, subgraphPrompt, subgraphDebug, subgraphResponse, executionParity, networkAfterSubgraphProjection, repeatedSetupLauncherCount, projectedPrompt, projectedReason, m17CanonicalIdentityBeforeLocale, performanceBeforeDestroy, m17CanonicalIdentityBeforeClose, ordinaryRefreshBeforeClose, restoredOwners, ownerRestored, selectedPageAfterProjectedRemount, ordinaryRefreshAfterReopen } = phaseSeven;
  const coalescingProbe = await page.evaluate(async () => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    ) as { h3PerformanceReceipt: () => FrontendPerformanceReceipt };
    const events = app.graph?.events as EventTarget | undefined;
    if (typeof events?.dispatchEvent !== "function")
      throw new Error("supported host graph events are unavailable");
    const before = extension.h3PerformanceReceipt();
    for (const eventName of ["change", "graphChanged", "configured"])
      events.dispatchEvent(new Event(eventName));
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 50));
    return { before, after: extension.h3PerformanceReceipt() };
  });
  expect(
    coalescingProbe.after.graph_event_count -
      coalescingProbe.before.graph_event_count,
  ).toBe(3);
  expect(
    coalescingProbe.after.refresh_count - coalescingProbe.before.refresh_count,
  ).toBe(1);
  expect(
    coalescingProbe.after.coalesced_event_count -
      coalescingProbe.before.coalesced_event_count,
  ).toBe(2);

  await page.evaluate(async (promptValue) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    // Restore a complete graph after the model-free execution cleanup. The
    // incomplete projection graph is intentionally rejected as a queue target.
    app.loadApiJson(promptValue, "h3-product-shell-direct-reload-e2e");
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
    // loadApiJson materializes the graph but does not guarantee the public
    // configure/loaded lifecycle hooks that drive the sidebar rescan. Re-load
    // the serialized public graph so the host emits one deterministic refresh.
    await app.loadGraphData(app.graph.serialize());
  }, fixture.prompt);
  await expect(shellStatus).toHaveText("interactive");
  await expect(
    page
      .locator("#h3-context-e2e-container")
      .getByRole("button", { name: "Queue current H3 graph" }),
  ).toBeVisible();

  await page.evaluate(async () => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const node = (id: number) => ({
      id,
      type: "comfyui_h3_context.H3Context.ProductShell",
      pos: [100 * id, 100],
      size: [420, 180],
      flags: {},
      order: id,
      mode: 0,
      inputs: [],
      outputs: [],
      properties: {},
      widgets_values: [],
    });
    await app.loadGraphData({
      last_node_id: 9,
      last_link_id: 0,
      nodes: [node(6), node(9)],
      links: [],
      groups: [],
      config: {},
      extra: {},
      version: 0.4,
    });
  });
  try {
    await expect(shellStatus).toHaveText("interactive");
  } catch (error) {
    const shellInventory = await page.evaluate(() => ({
      container_count: document.querySelectorAll("#h3-context-e2e-container")
        .length,
      panel_count: document.querySelectorAll("#h3-context-e2e-host-panel")
        .length,
      sidebar_ids: (
        (
          window as unknown as { comfyAPI: { app: { app: any } } }
        ).comfyAPI.app.app.extensionManager?.getSidebarTabs?.() ?? []
      ).map((tab: { id?: unknown }) => tab.id),
      body_h3: document.body.innerText.includes("H3 Context"),
    }));
    throw new Error(
      `ambiguous graph status probe failed: ${JSON.stringify(shellInventory)}`,
      { cause: error },
    );
  }
  // Reconciled by M23-37: the decision copy for a canvas that already holds
  // nodes is the keep-or-replace sentence; the retired copy said "ambiguous".
  await expect(
    page.getByText("This canvas contains nodes", { exact: false }),
  ).toBeVisible();
  await captureLayoutMatrix("dirty-decision");
  await resetLayoutMatrixPresentation("presentation-only");

  // Remove only the public queue seam on an empty/interactive Base graph. The
  // controller must reject this before graph mutation, yielding stable error
  // recovery UI without a compiled-prompt restore/configure race.
  await page.evaluate(async () => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    await app.loadGraphData({
      last_node_id: 0,
      last_link_id: 0,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
      version: 0.4,
    });
  });
  await expect(shellStatus).toHaveText("interactive");
  await expect(
    page
      .locator("#h3-context-e2e-container")
      .getByRole("button", { name: "Start H3 App Mode" }),
  ).toBeVisible();
  await expect(
    page
      .locator("#h3-context-e2e-container")
      .getByRole("button", { name: "Start H3 App Mode" }),
  ).toBeEnabled();

  // The queue-seam refusal is this phase's subject; clear the stale i2va
  // source the earlier real run left in the draft so the start reaches the
  // queue capability check instead of refusing on a gone source node.
  await appModeContainer
    .locator('[data-h3-focus-key="app-task-mode"]')
    .selectOption("t2va");
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { api: { api: any } };
      __h3OriginalErrorQueuePrompt?: Function;
    };
    const api = runtime.comfyAPI.api.api;
    runtime.__h3OriginalErrorQueuePrompt = api.queuePrompt;
    api.queuePrompt = undefined;
  });
  await page
    .locator("#h3-context-e2e-container")
    .getByRole("button", { name: "Start H3 App Mode" })
    .click();
  await expect(shellStatus).toHaveText("error");
  await captureLayoutMatrix("real-error");
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { api: { api: any } };
      __h3OriginalErrorQueuePrompt?: Function;
    };
    if (runtime.__h3OriginalErrorQueuePrompt !== undefined)
      runtime.comfyAPI.api.api.queuePrompt =
        runtime.__h3OriginalErrorQueuePrompt;
  });

  // Exercise native-preference close/reopen independently from projected state.
  await page.evaluate(async () => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    await app.loadGraphData({
      last_node_id: 0,
      last_link_id: 0,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
      version: 0.4,
    });
  });
  await appModeContainer
    .getByRole("button", { name: "Continue with native nodes" })
    .click();
  const nativeIntent = appModeContainer.getByRole("textbox", {
    name: "Intent",
  });
  await nativeIntent.fill("M17 native preference remount draft");
  await nativeIntent.focus();
  const nativeRefreshBeforeClose = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    ) as
      { h3PerformanceReceipt?: () => FrontendPerformanceReceipt } | undefined;
    if (typeof extension?.h3PerformanceReceipt !== "function")
      throw new Error("H3 performance receipt is absent before native reopen");
    return extension.h3PerformanceReceipt().refresh_count;
  });
  const nativeCanonicalIdentityBeforeClose =
    await captureM17CanonicalIdentity(page);
  await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    const container = document.querySelector<HTMLElement>(
      "#h3-context-e2e-container",
    );
    if (tab === undefined || container === null)
      throw new Error("native-preference remount target is absent");
    tab.destroy();
    tab.render(container);
  });
  await expect(
    appModeContainer.getByRole("textbox", { name: "Intent" }),
  ).toHaveValue("M17 native preference remount draft");
  await expect(
    appModeContainer.locator('[data-h3-focus-key="app-intent"]'),
  ).toBeFocused();
  expect(
    await page.evaluate(() => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      return app.extensionManager
        .getSidebarTabs()
        .filter((candidate: { id: string }) => candidate.id === "h3-context")
        .length;
    }),
  ).toBe(1);
  const nativeRefreshAfterReopen = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    ) as
      { h3PerformanceReceipt?: () => FrontendPerformanceReceipt } | undefined;
    if (typeof extension?.h3PerformanceReceipt !== "function")
      throw new Error("H3 performance receipt is absent after native reopen");
    return extension.h3PerformanceReceipt().refresh_count;
  });
  expect(nativeRefreshAfterReopen - nativeRefreshBeforeClose).toBe(1);
  expect(await captureM17CanonicalIdentity(page)).toEqual(
    nativeCanonicalIdentityBeforeClose,
  );
  expect(
    await page.evaluate(() => {
      const runtime = window as unknown as {
        comfyAPI: { api: { api: { queuePrompt: Function } } };
        __h3M17OriginalQueuePrompt?: Function;
        __h3M17QueueCount?: number;
      };
      const count = runtime.__h3M17QueueCount ?? 0;
      if (runtime.__h3M17OriginalQueuePrompt !== undefined)
        runtime.comfyAPI.api.api.queuePrompt =
          runtime.__h3M17OriginalQueuePrompt;
      runtime.__h3M17OriginalQueuePrompt = undefined;
      runtime.__h3M17QueueCount = undefined;
      return count;
    }),
  ).toBe(0);

  const networkAfterLocaleRemountErrorRecovery = captureInteractionNetworkPhase(
    "after_locale_remount_error_recovery",
  );
  const networkBeforeDisposal =
    captureInteractionNetworkPhase("before_disposal");
  await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const disposedTab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    ) as { h3DisposeExtension?: () => void } | undefined;
    if (typeof extension?.h3DisposeExtension !== "function")
      throw new Error("whole-extension disposal seam is absent");
    extension.h3DisposeExtension();
    if (
      app.extensionManager
        .getSidebarTabs()
        .some((candidate: { id: string }) => candidate.id === "h3-context")
    )
      throw new Error("disposed H3 launcher remains registered");
    const container = document.querySelector<HTMLElement>(
      "#h3-context-e2e-container",
    );
    if (disposedTab === undefined || container === null)
      throw new Error("disposed late-render probe is unavailable");
    let lateRenderRejected = false;
    try {
      disposedTab.render(container);
    } catch {
      lateRenderRejected = true;
    }
    if (!lateRenderRejected)
      throw new Error("disposed H3 launcher accepted a late render");
  });
  const performanceAfterDestroy = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    ) as { h3PerformanceReceipt: () => FrontendPerformanceReceipt };
    return extension.h3PerformanceReceipt();
  });
  expect(performanceAfterDestroy.cleanup_verified).toBe(true);
  const networkAfterCleanup = captureInteractionNetworkPhase("after_cleanup");
  for (const row of layoutEvidence) {
    row.observerCleanup = performanceAfterDestroy.cleanup_verified;
    row.ownerRestored = ownerRestored;
  }
  const layoutReportPath = process.env.H3_CONTEXT_LAYOUT_REPORT;
  if (layoutReportPath !== undefined) {
    const candidate = process.env.H3_CONTEXT_IMPLEMENTATION_COMMIT;
    if (candidate === undefined)
      throw new Error(
        "H3_CONTEXT_IMPLEMENTATION_COMMIT is required for layout evidence",
      );
    expect(layoutEvidence).toHaveLength(layoutMatrix.length * 6);
    expect(new Set(layoutEvidence.map((row) => row.testId)).size).toBe(
      layoutEvidence.length,
    );
    expect(
      layoutEvidence.every(
        (row) => row.observerCleanup === true && row.ownerRestored === true,
      ),
    ).toBe(true);
    expect(captureManifest).toHaveLength(layoutEvidence.length * 2);
    expect(new Set(captureManifest.map((capture) => capture.path)).size).toBe(
      captureManifest.length,
    );
    for (const capture of captureManifest) {
      const captureDigest = createHash("sha256")
        .update(await readFile(capture.path))
        .digest("hex");
      expect(captureDigest).toBe(capture.sha256);
    }
    const captureManifestSha256 = createHash("sha256")
      .update(JSON.stringify(captureManifest), "utf8")
      .digest("hex");
    for (const row of layoutEvidence) {
      if (row.evidenceJoin === undefined)
        throw new Error("layout evidence row is missing its evidence join");
      row.evidenceJoin.captureManifestSha256 = captureManifestSha256;
    }
    const layoutReport = {
      schema: "h3.frontend.layout.v2",
      candidate,
      reportContentSha256: null as string | null,
      hostReport: {
        path: hostReportPath,
        contentSha256: process.env.H3_CONTEXT_HOST_REPORT_SHA256 ?? null,
      },
      host: {
        revision: process.env.H3_CONTEXT_HOST_REVISION ?? "not-provided",
        frontend: process.env.H3_CONTEXT_FRONTEND_VERSION ?? "not-provided",
        browser: "chromium",
      },
      contract: {
        coordinate_frames: ["browser_viewport", "host_panel", "h3_sidebar"],
        topology: [
          "left",
          "right",
          "unified",
          "per-tab",
          "opposite-panel",
          "available-space",
        ],
        accessibility: [
          "en",
          "zh-TW",
          "light",
          "dark",
          "200%-root",
          "reduced-motion",
          "forced-colors",
        ],
        lifecycle: ["fresh", "remount", "live-resize", "destroy", "reload"],
        privacy: ["storage-values-digested", "no-raw-content", "network-local"],
      },
      rows: layoutEvidence,
      captures: captureManifest,
      storage: {
        values_redacted: true,
        before: initialHostStorage,
        after: await snapshotHostStorage(),
      },
      hostPersistence: {
        keyFamily: HOST_PERSISTENCE_KEY_FAMILY,
        before: hostPersistenceBefore,
        after: await snapshotHostPersistence(),
        sentinelDigest: hostPersistenceBeforeDigest,
      },
      cleanup: {
        destroy: true,
        reload: true,
        storage_unchanged: layoutEvidence.every(
          (row) => row.storageUnchanged === true,
        ),
      },
    };
    const canonicalLayoutReportForDigest = (report: unknown): string => {
      const normalized = JSON.parse(JSON.stringify(report)) as {
        reportContentSha256: string | null;
        rows: Array<{ evidenceJoin?: { reportContentSha256: string | null } }>;
      };
      normalized.reportContentSha256 = null;
      for (const row of normalized.rows) {
        if (row.evidenceJoin !== undefined)
          row.evidenceJoin.reportContentSha256 = null;
      }
      return JSON.stringify(normalized);
    };
    const reportContentSha256 = createHash("sha256")
      .update(canonicalLayoutReportForDigest(layoutReport), "utf8")
      .digest("hex");
    layoutReport.reportContentSha256 = reportContentSha256;
    for (const row of layoutEvidence) {
      if (row.evidenceJoin === undefined)
        throw new Error("layout evidence row is missing its evidence join");
      row.evidenceJoin.reportContentSha256 = reportContentSha256;
      expect(row.evidenceJoin.candidate).toBe(candidate);
      expect(row.evidenceJoin.reportContentSha256).toBe(reportContentSha256);
    }
    expect(
      createHash("sha256")
        .update(canonicalLayoutReportForDigest(layoutReport), "utf8")
        .digest("hex"),
    ).toBe(reportContentSha256);
    const serializedReport = `${JSON.stringify(layoutReport, null, 2)}\n`;
    await mkdir(dirname(layoutReportPath), { recursive: true });
    await writeFile(layoutReportPath, serializedReport, "utf8");
    const reportDigest = createHash("sha256")
      .update(serializedReport, "utf8")
      .digest("hex");
    console.log(
      `H3_CONTEXT_LAYOUT_REPORT=${JSON.stringify({
        path: layoutReportPath,
        sha256: reportDigest,
        contentSha256: reportContentSha256,
        hostReport: layoutReport.hostReport,
        hostReportContentSha256: layoutReport.hostReport.contentSha256,
        rows: layoutEvidence.length,
        captures: captureManifest.length,
        candidate,
      })}`,
    );
  }
  await page.evaluate((original) => {
    for (const [key, value] of Object.entries(original)) {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    }
  }, originalHostPersistence);
  const restoredHostPersistence = await page.evaluate((keys) => {
    const values: Record<string, string | null> = {};
    for (const key of keys) values[key] = localStorage.getItem(key);
    return values;
  }, HOST_PERSISTENCE_KEY_FAMILY);
  expect(restoredHostPersistence).toEqual(originalHostPersistence);
  console.log(
    `H3_CONTEXT_PERFORMANCE_RECEIPT=${JSON.stringify(performanceAfterDestroy)}`,
  );

  const networkBeforeLocalityAssertion = captureInteractionNetworkPhase(
    "before_locality_assertion",
  );
  const networkPhaseEvidence = [
    networkAfterLayoutFixturePrep,
    networkAfterAppModeStart,
    networkAfterDirectGraphLoad,
    networkAfterDirectPromptProjection,
    networkAfterProjectedLayoutMatrix,
    networkAfterWorkspaceActions,
    networkAfterDirectProjection,
    networkAfterSubgraphProjection,
    networkAfterLocaleRemountErrorRecovery,
    networkBeforeDisposal,
    networkAfterCleanup,
    networkBeforeLocalityAssertion,
  ];
  expect(networkPhaseEvidence).toEqual([
    {
      phase: "after_layout_fixture_prep",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_app_mode_start",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_direct_graph_load",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_direct_prompt_projection",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_projected_layout_matrix",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_workspace_actions",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_direct_projection",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_subgraph_projection",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_locale_remount_error_recovery",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "before_disposal",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "after_cleanup",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
    {
      phase: "before_locality_assertion",
      remoteCount: expect.any(Number),
      providerCount: expect.any(Number),
    },
  ]);
  for (const [index, evidence] of networkPhaseEvidence.entries()) {
    expect(Number.isInteger(evidence.remoteCount)).toBe(true);
    expect(Number.isInteger(evidence.providerCount)).toBe(true);
    expect(evidence.remoteCount).toBeGreaterThanOrEqual(0);
    expect(evidence.providerCount).toBeGreaterThanOrEqual(0);
    expect(evidence.providerCount).toBeLessThanOrEqual(evidence.remoteCount);
    if (index > 0) {
      expect(evidence.remoteCount).toBeGreaterThanOrEqual(
        networkPhaseEvidence[index - 1]!.remoteCount,
      );
      expect(evidence.providerCount).toBeGreaterThanOrEqual(
        networkPhaseEvidence[index - 1]!.providerCount,
      );
    }
  }
  console.log(
    `H3_CONTEXT_NETWORK_PHASES=${JSON.stringify(networkPhaseEvidence)}`,
  );
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  networkAttribution.pauseForHostInitialization();
  candidateNetworkAttribution?.pauseForHostInitialization();
  interactionErrors.active = false;
  interactionErrors.consoleErrorCount = 0;
  interactionErrors.pageErrorCount = 0;
  const reloadInjectionCount = candidateInjectionCount(context);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => {
    const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
      .comfyAPI?.app?.app;
    return app?.extensionManager
      ?.getSidebarTabs?.()
      .some((tab: { id: string }) => tab.id === "h3-context");
  });
  await assertCandidateBundleInjection(page, context, reloadInjectionCount);
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  interactionErrors.active = true;
  await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    const splitter = document.createElement("div");
    splitter.id = "h3-context-e2e-reload-splitter";
    splitter.className = "p-splitter";
    splitter.dataset.pcName = "splitter";
    const panel = document.createElement("div");
    panel.id = "h3-context-e2e-reload-panel";
    panel.className = "p-splitterpanel side-bar-panel";
    panel.style.width = "260px";
    panel.style.minWidth = "13px";
    panel.style.flexBasis = "260px";
    const content = document.createElement("div");
    content.className = "sidebar-content-container";
    content.style.minWidth = "19px";
    const container = document.createElement("div");
    container.id = "h3-context-e2e-reload-container";
    content.append(container);
    panel.append(content);
    const central = document.createElement("div");
    central.className = "p-splitterpanel";
    const opposite = document.createElement("div");
    opposite.className = "p-splitterpanel side-bar-panel";
    opposite.style.display = "none";
    splitter.append(panel, central, opposite);
    document.body.append(splitter);
    tab.render(container);
    tab.destroy();
  });
  await expect(page.locator("style[data-h3-context]")).toHaveCount(0);
  await expect(page.locator("#h3-context-e2e-reload-container")).toBeEmpty();
  await expect
    .poll(() =>
      page.locator("#h3-context-e2e-reload-panel").evaluate((element) => ({
        minWidth: (element as HTMLElement).style.minWidth,
        width: (element as HTMLElement).style.width,
        flexBasis: (element as HTMLElement).style.flexBasis,
      })),
    )
    .toEqual({ minWidth: "13px", width: "260px", flexBasis: "260px" });
  const finalStorageKeys = await page.evaluate(() => ({
    local: Object.keys(localStorage)
      .filter((key) => /h3|provider|credential|ollama|minimax/i.test(key))
      .sort(),
    session: Object.keys(sessionStorage)
      .filter((key) => /h3|provider|credential|ollama|minimax/i.test(key))
      .sort(),
  }));
  // M23-37: the M23-21 journal mirror is the one key this repository may add
  // during the row; nothing else may appear or disappear.
  const journalKey = "h3-context.managed-journal.v1";
  const sessionContinuityKeys = new Set([
    "h3.context.production.workspace_handle.v1",
    "h3.context.production.destination.v1",
  ]);
  const addedLocalKeys = finalStorageKeys.local.filter(
    (key) => !initialStorageKeys.local.includes(key),
  );
  expect(addedLocalKeys.every((key) => key === journalKey)).toBe(true);
  expect({
    local: finalStorageKeys.local.filter((key) => key !== journalKey),
    // IMPORTANT: compare after excluding bounded owned continuity keys; M25-36 persists the
    // workflow destination across reload, so treating every new session key as a leak is invalid.
    session: finalStorageKeys.session.filter(
      (key) => !sessionContinuityKeys.has(key),
    ),
  }).toEqual({
    local: initialStorageKeys.local.filter((key) => key !== journalKey),
    session: initialStorageKeys.session.filter(
      (key) => !sessionContinuityKeys.has(key),
    ),
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(interactionErrors.pageErrorCount).toBe(0);
  expect(interactionErrors.consoleErrorCount).toBe(0);
});
