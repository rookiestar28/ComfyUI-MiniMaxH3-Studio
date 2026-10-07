import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { createReadStream } from "node:fs";
import { mkdir, readFile, realpath, stat, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import {
  expect,
  test,
  type BrowserContext,
  type Locator,
  type Page,
} from "@playwright/test";

import {
  decodeGenerationProfile,
  familyProfileForTaskMode,
} from "../../../src/contracts/generationProfileCodec";
import {
  decodeProviderIntentResult,
  PROVIDER_SETTINGS_SCHEMA,
  PROVIDER_SETTINGS_REQUEST_SCHEMA,
  type ProviderSettingsProjection,
} from "../../../src/contracts/providerSettingsCodec";
import { decodeSidebarWorkspaceProjection } from "../../../src/contracts/sidebarWorkspaceCodec";
import { APP_MODE_ARTIFACT_PREFIX_ROOT } from "../../../src/host/appMode";
import {
  INPUT_GEOMETRY_RECEIPT_SCHEMA,
  INPUT_GEOMETRY_ROUTE,
} from "../../../src/host/inputGeometry";
import {
  MAX_MEDIA_PREVIEW_BYTES,
  MEDIA_PREVIEW_REQUEST_SCHEMA,
  MEDIA_PREVIEW_ROUTE,
} from "../../../src/host/productionMediaPreview";
import {
  readOfficialAssetInventory,
  resolveOfficialAssets,
} from "../../../src/host/officialAssetResolution";
import { projectionFromOutput } from "../../../src/host/sidebarHost";
import {
  I2VA_SCALE_NODE_TYPE,
  I2VA_SIZE_NODE_TYPE,
  OFFICIAL_LENGTH_EXPRESSION,
} from "../../../src/host/templateMaterialization";
import {
  CANDIDATE_BACKEND_HOST_ROOT_ENV,
  CANDIDATE_BUNDLE_PATH_ENV,
  CANDIDATE_BUNDLE_SHA256_ENV,
  CandidateInitiatorNetworkAttribution,
  H3NetworkAttribution,
  loadCandidateBundleEnvironment,
  verifyCandidateBackendRuntimeEnvironment,
  waitForStartupNetworkQuiet,
} from "../helpers/candidateBundleHarness";
import {
  diffGraphSurroundings,
  type SurroundingsDiffReport,
} from "../../support/surroundingsDiff";

import {
  candidateBundle,
  candidateBundleResourceUrl,
  hostUrl,
} from "./candidate";
export function monitorH3Network(
  page: Page,
  allowedOrigin: string,
): H3NetworkAttribution {
  const attribution = new H3NetworkAttribution(allowedOrigin);
  page.on("request", (request) => attribution.observeRequest(request.url()));
  return attribution;
}

export async function monitorCandidateInitiatorNetwork(
  context: BrowserContext,
  page: Page,
  allowedOrigin: string,
): Promise<CandidateInitiatorNetworkAttribution | null> {
  if (candidateBundle === null) return null;
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const attribution = new CandidateInitiatorNetworkAttribution(
    allowedOrigin,
    candidateBundleResourceUrl(hostUrl),
  );
  try {
    const session = await context.newCDPSession(page);
    await session.send("Network.enable");
    session.on("Network.requestWillBeSent", (event) =>
      attribution.observeRequestWillBeSent(event),
    );
  } catch {
    throw new Error("candidate network attribution unavailable");
  }
  return attribution;
}

export async function waitForH3Registration(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
      .comfyAPI?.app?.app;
    const tabCount =
      app?.extensionManager
        ?.getSidebarTabs?.()
        .filter((tab: { id: string }) => tab.id === "h3-context").length ?? 0;
    return tabCount === 1;
  });
}

export async function beginSettledH3InteractionPhase(
  page: Page,
  attribution: H3NetworkAttribution,
  candidateAttribution: CandidateInitiatorNetworkAttribution | null,
): Promise<void> {
  await page.evaluate(
    () =>
      new Promise<void>((resolvePromise) =>
        requestAnimationFrame(() =>
          requestAnimationFrame(() => resolvePromise()),
        ),
      ),
  );
  await waitForStartupNetworkQuiet(attribution, (milliseconds) =>
    page.waitForTimeout(milliseconds),
  );
  attribution.beginH3InteractionPhase();
  candidateAttribution?.beginH3InteractionPhase();
}

export function expectCandidateInteractionNetworkLocal(
  attribution: CandidateInitiatorNetworkAttribution | null,
): void {
  if (attribution === null) return;
  const evidence = attribution.snapshot();
  console.log(`H3_CONTEXT_CANDIDATE_NETWORK=${JSON.stringify(evidence)}`);
  expect(evidence.candidateInteractionRemoteCount).toBe(0);
  expect(evidence.candidateInteractionProviderCount).toBe(0);
}
export type FrontendPerformanceReceipt = {
  schema: string;
  source: string;
  mount_ms: number | null;
  refresh_ms: number | null;
  decode_ms: number | null;
  render_ms: number | null;
  projection_peak_bytes: number;
  projection_update_count: number;
  graph_event_count: number;
  refresh_count: number;
  coalesced_event_count: number;
  cleanup_verified: boolean;
};

export async function captureM17CanonicalIdentity(page: Page) {
  return page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3Executed?: Array<{
        node?: unknown;
        promptFingerprint?: unknown;
        reportFingerprint?: unknown;
        correlation?: unknown;
        workspace?: unknown;
      }>;
    };
    const app = runtime.comfyAPI.app.app;
    const executed = runtime.__h3Executed ?? [];
    const projected = executed.find((event) => event.node === "6");
    const workspace =
      projected?.workspace !== null && typeof projected?.workspace === "object"
        ? (projected.workspace as Record<string, unknown>)
        : {};
    return {
      graph: JSON.stringify(app.graph.serialize()),
      workspace: {
        workspace_id: workspace.workspace_id ?? null,
        report_revision: workspace.report_revision ?? null,
        report_fingerprint: workspace.report_fingerprint ?? null,
        prompt_fingerprint: workspace.prompt_fingerprint ?? null,
      },
      transaction: projected?.correlation ?? null,
      queue: {
        executed_count: executed.length,
        prompt_id:
          projected?.correlation !== null &&
          typeof projected?.correlation === "object"
            ? ((projected.correlation as Record<string, unknown>).prompt_id ??
              null)
            : null,
      },
      artifact: {
        prompt_fingerprint: projected?.promptFingerprint ?? null,
        report_fingerprint: projected?.reportFingerprint ?? null,
      },
    };
  });
}
// M23-26 registration phase Seven.
// prettier-ignore
import type { HostOfficialAssetManifest, CandidateInjectionState, LayoutState, AppModePhase, LayoutGridKind, LayoutVariant, LayoutPlacement, LayoutWidthMode, LayoutLocale, LayoutTheme, LayoutLifecycle, HostPersistenceSnapshot, LayoutCaptureEvidence, LayoutEvidenceJoin, LayoutEvidenceRow, LayoutCell, LayoutStateContract, LayoutRect, LayoutOwnerStyle, LayoutControlGeometry, LayoutNavigationTab, LayoutHeaderGeometry, LayoutFocusTargetKind, LayoutMeasurement, SerializedGraph, ManagedRouteReceipt, HostAssetResolutionReceipt, SampleProgress, RealI2vaArtifactMetadata, RealI2vaEnvironment } from "./environment";
// prettier-ignore
import type { runRegistrationPhaseSix } from "./execution";
export async function runRegistrationPhaseSeven(
  state: Awaited<ReturnType<typeof runRegistrationPhaseSix>>,
) {
  // prettier-ignore
  const { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity } = state.shared;
  // prettier-ignore
  const { context, page, testInfo, allowedOrigin, networkAttribution, candidateNetworkAttribution, captureInteractionNetworkPhase, interactionErrors, initialInjectionCount, initialStorageKeys, registryEvidence, coInstallDefinitions, workflowTemplateDialog, appModeContainer, shellStatus, awaitAppModePhase, visualDirectory, snapshotHostStorage, snapshotHostPersistence, seedHostPersistenceSentinel, originalHostPersistence, initialHostStorage, hostPersistenceBefore, storageSentinelDigest, hostPersistenceBeforeDigest, snapshotHostOwner, layoutEvidence, captureManifest, visualPhase, candidateIdentity, hostReportPath, captureSettledScreenshot, applyLayoutCell, captureLayoutMatrix, projectedWorkspace, resetLayoutMatrixPresentation, networkAfterLayoutFixturePrep, cancelAction, preCancellationGraph, cancellationReason, cancellationReceipt, appModeQueueRequests, appModeNetworkShapes, appModeResponseShapes, appModeExecutedShapes, layoutTaskMode, layoutDuration, appModeEntry, appModePromptTypes, appModeGraph, metadata, githubLink, metadataStyles, networkAfterAppModeStart, fixture, prompt, executionPrompt, networkAfterDirectGraphLoad, response, responseBody, promptId, executionReceipt, projectionInventory, graphBeforeSetupEdit, executedBeforeSetupEdit, networkBeforeSetupEdit, networkAfterSetupEdit, networkAfterDirectPromptProjection, networkAfterProjectedLayoutMatrix, initialWorkspace, promptEditor, initialStageResponse, initialStageResult, initialStageBody, initialStageProjection, staleResponse, downloadEvent, download, downloadPath, transferText, transfer, networkAfterWorkspaceActions } = state;
  const appModePrompt = await page.evaluate(async (promptValue) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    // The preceding model-free execution removes the native generation node.
    // Re-materialize the complete public fixture before the App Mode parity run;
    // a graph without its generation anchor is intentionally not queueable.
    app.loadApiJson(promptValue, "h3-product-shell-app-mode-e2e");
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
    const appGraph = app.graph.serialize();
    appGraph.extra = { ...(appGraph.extra ?? {}), linearMode: true };
    await app.loadGraphData(appGraph);
    if (app.rootGraph.extra?.linearMode !== true)
      throw new Error("App Mode did not activate");
    const compiled = await app.graphToPrompt();
    const requestInputs = compiled.output["1"]?.inputs;
    // loadApiJson materializes the FLOAT minimum as a widget value even though the canonical
    // API fixture omits duration_seconds; remove only that host-loader sentinel before queueing.
    if (
      requestInputs?.duration_seconds === 0 &&
      requestInputs.frame_count !== undefined
    )
      delete requestInputs.duration_seconds;
    return {
      output: compiled.output,
      outputKeys: Object.keys(compiled.output),
      workflow: compiled.workflow,
    };
  }, fixture.prompt);
  expect(appModePrompt.outputKeys).toContain("6");
  const appModeResponse = await page.evaluate(
    async ({ endpoint, promptValue }) => {
      const clientId = (
        window as unknown as {
          comfyAPI: { api: { api: { clientId: string } } };
        }
      ).comfyAPI.api.api.clientId;
      const result = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ client_id: clientId, prompt: promptValue }),
      });
      return { status: result.status, text: await result.text() };
    },
    {
      endpoint: new URL("/prompt", hostUrl).href,
      promptValue: appModePrompt.output,
    },
  );
  expect(appModeResponse.status, appModeResponse.text).toBe(200);
  await page.waitForFunction(
    () =>
      (
        (window as unknown as { __h3Executed?: Array<{ node?: unknown }> })
          .__h3Executed ?? []
      ).filter((event) => event.node === "6").length >= 2,
  );
  const appModeParity = await page.evaluate(() => {
    const executed = (
      window as unknown as {
        __h3Executed?: Array<{
          node?: unknown;
          promptFingerprint?: unknown;
          reportFingerprint?: unknown;
          workspace?: { workspace_id?: unknown; prompt_text?: unknown };
        }>;
      }
    ).__h3Executed?.filter((event) => event.node === "6");
    return { direct: executed?.[0], appMode: executed?.at(-1) };
  });
  expect(appModeParity.appMode?.promptFingerprint).toBe(
    appModeParity.direct?.promptFingerprint,
  );
  expect(appModeParity.appMode?.reportFingerprint).toBe(
    appModeParity.direct?.reportFingerprint,
  );
  expect(appModeParity.appMode?.workspace?.workspace_id).toMatch(/^ws_/);
  expect(appModeParity.appMode?.workspace?.workspace_id).not.toBe(
    appModeParity.direct?.workspace?.workspace_id,
  );
  await expect(shellStatus).toHaveText("ready");
  await page.getByRole("tab", { name: "Audit / Validate" }).click();
  const repeatedPrompt = String(appModeParity.appMode?.workspace?.prompt_text);
  await page
    .getByRole("textbox", { name: "Prompt revision" })
    .fill(`${repeatedPrompt}\nCamera: Hold.`);
  await page.getByLabel("Revision reason").fill("Repeated host execution");
  const repeatedActionRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().endsWith("/h3-context/v1/sidebar/action"),
  );
  await page.getByRole("button", { name: "Stage revision" }).click();
  const repeatedActionBody = (await repeatedActionRequest).postDataJSON() as {
    workspace_id?: unknown;
  };
  expect(repeatedActionBody.workspace_id).toBe(
    appModeParity.appMode?.workspace?.workspace_id,
  );
  await expect(
    page.getByText("Validation or media receipt is required before export"),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Validate revision" })
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(page.getByText("Current revision is ready")).toBeVisible();
  const networkAfterDirectProjection = captureInteractionNetworkPhase(
    "after_direct_projection",
  );

  const subgraph = JSON.parse(
    await readFile(
      resolve(process.cwd(), "../subgraphs/H3 Product Shell Boundary.json"),
      "utf8",
    ),
  ) as Record<string, unknown>;
  const subgraphPrompt = await page.evaluate(
    async ({ boundary, completePrompt }) => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      // The preceding model-free execution intentionally removes the native
      // generation node. Restore the complete public fixture before exercising
      // the boundary wrapper; an incomplete post-execution graph is never a
      // valid "Queue current H3 graph" target.
      app.loadApiJson(completePrompt, "h3-product-shell-subgraph-e2e");
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
      const directGraph = app.graph.serialize();
      const directShell = directGraph.nodes.find(
        (node: { id: unknown }) => node.id === 6,
      );
      if (directShell === undefined)
        throw new Error("direct ProductShell is absent");
      const boundaryRecord = boundary as {
        nodes: Array<Record<string, unknown>>;
        definitions: Record<string, unknown>;
      };
      const outerShell = {
        ...structuredClone(boundaryRecord.nodes[0]),
        id: 6,
        pos: directShell.pos,
        order: directShell.order,
        inputs: directShell.inputs,
        outputs: directShell.outputs,
      };
      const composedGraph = {
        ...directGraph,
        nodes: [
          ...directGraph.nodes.filter((node: { id: unknown }) => node.id !== 6),
          outerShell,
        ],
        definitions: structuredClone(boundaryRecord.definitions),
      };
      await app.loadGraphData(composedGraph);
      const compiled = await app.graphToPrompt();
      const requestInputs = compiled.output["1"]?.inputs;
      // loadApiJson materializes the FLOAT minimum as a widget value even though the canonical
      // API fixture omits duration_seconds; remove only that host-loader sentinel before queueing.
      if (
        requestInputs?.duration_seconds === 0 &&
        requestInputs.frame_count !== undefined
      )
        delete requestInputs.duration_seconds;
      const productShellIds = Object.entries(
        compiled.output as Record<string, { class_type?: unknown }>,
      )
        .filter(
          ([, value]) =>
            value.class_type === "comfyui_h3_context.H3Context.ProductShell",
        )
        .map(([id]) => id);
      if (productShellIds.length !== 1 || !productShellIds[0]?.startsWith("6:"))
        throw new Error(
          "compiled Subgraph ProductShell identity is not unique",
        );
      return {
        output: compiled.output,
        workflow: compiled.workflow,
        outputKeys: Object.keys(compiled.output),
        productShellId: productShellIds[0],
      };
    },
    { boundary: subgraph, completePrompt: fixture.prompt },
  );
  expect(subgraphPrompt.outputKeys).toContain(subgraphPrompt.productShellId);
  const subgraphDebug = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const graph = app.graph.serialize();
    const definitions = graph.definitions as
      { subgraphs?: Array<Record<string, unknown>> } | undefined;
    return {
      status: document.querySelector(
        '#h3-context-e2e-container .h3-context-status[role="status"]',
      )?.textContent,
      text: document.querySelector("#h3-context-e2e-container")?.textContent,
      definition_keys:
        definitions === undefined ? null : Object.keys(definitions).sort(),
      subgraph_keys: definitions?.subgraphs?.map((value) =>
        Object.keys(value).sort(),
      ),
      subgraph_shape: definitions?.subgraphs?.map((value) => ({
        inputs: Array.isArray(value.inputs)
          ? value.inputs.map((input: Record<string, unknown>) => ({
              name: input.name,
              type: input.type,
              keys: Object.keys(input).sort(),
            }))
          : null,
        outputs: Array.isArray(value.outputs)
          ? value.outputs.map((output: Record<string, unknown>) => ({
              name: output.name,
              type: output.type,
              keys: Object.keys(output).sort(),
            }))
          : null,
        nodes: Array.isArray(value.nodes)
          ? value.nodes.map((node: Record<string, unknown>) => ({
              type: node.type,
              keys: Object.keys(node).sort(),
              input_keys: Array.isArray(node.inputs)
                ? node.inputs.map((port: Record<string, unknown>) =>
                    Object.keys(port).sort(),
                  )
                : null,
              output_keys: Array.isArray(node.outputs)
                ? node.outputs.map((port: Record<string, unknown>) =>
                    Object.keys(port).sort(),
                  )
                : null,
            }))
          : null,
        link_count: Array.isArray(value.links) ? value.links.length : null,
        link_keys: Array.isArray(value.links)
          ? value.links.map((link: Record<string, unknown>) =>
              Object.keys(link).sort(),
            )
          : null,
      })),
      trace: (
        window as unknown as {
          __h3HostProjectionTrace?: Array<Record<string, unknown>>;
        }
      ).__h3HostProjectionTrace?.slice(-8),
    };
  });
  await expect(shellStatus).toHaveText("interactive");
  await expect(
    page
      .locator("#h3-context-e2e-container")
      .getByRole("button", { name: "Queue current H3 graph" }),
    JSON.stringify(subgraphDebug),
  ).toBeVisible();
  const subgraphResponse = await page.evaluate(
    async ({ endpoint, promptValue }) => {
      const clientId = (
        window as unknown as {
          comfyAPI: { api: { api: { clientId: string } } };
        }
      ).comfyAPI.api.api.clientId;
      const result = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ client_id: clientId, prompt: promptValue }),
      });
      return { status: result.status, text: await result.text() };
    },
    {
      endpoint: new URL("/prompt", hostUrl).href,
      promptValue: subgraphPrompt.output,
    },
  );
  expect(subgraphResponse.status, subgraphResponse.text).toBe(200);
  await page.waitForFunction(
    (productShellId) =>
      (
        (window as unknown as { __h3Executed?: Array<{ node?: unknown }> })
          .__h3Executed ?? []
      ).some((event) => event.node === productShellId),
    subgraphPrompt.productShellId,
  );
  const executionParity = await page.evaluate((productShellId) => {
    const executed =
      (
        window as unknown as {
          __h3Executed?: Array<{
            node?: unknown;
            displayNode?: unknown;
            promptFingerprint?: unknown;
            reportFingerprint?: unknown;
            correlation?: unknown;
          }>;
        }
      ).__h3Executed ?? [];
    return {
      direct: executed.find((event) => event.node === "6"),
      subgraph: executed.find((event) => event.node === productShellId),
    };
  }, subgraphPrompt.productShellId);
  expect(executionParity.subgraph?.correlation).toMatchObject({
    execution_node_id: subgraphPrompt.productShellId,
  });
  expect(executionParity.subgraph?.promptFingerprint).toBe(
    executionParity.direct?.promptFingerprint,
  );
  expect(executionParity.subgraph?.reportFingerprint).toBe(
    executionParity.direct?.reportFingerprint,
  );
  await expect(shellStatus).toHaveText("ready");
  const networkAfterSubgraphProjection = captureInteractionNetworkPhase(
    "after_subgraph_projection",
  );

  // M17-00: drive the supported extension setting and followed Comfy.Locale
  // event while preserving the exact projected draft, page, focus, and root.
  const repeatedSetupLauncherCount = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    ) as { setup?: () => void } | undefined;
    if (typeof extension?.setup !== "function")
      throw new Error("M17 entry setup seam is absent");
    extension.setup();
    extension.setup();
    return app.extensionManager
      .getSidebarTabs()
      .filter((candidate: { id: string }) => candidate.id === "h3-context")
      .length;
  });
  expect(repeatedSetupLauncherCount).toBe(1);
  await setSupportedH3Language(page, "en");
  await appModeContainer.getByRole("tab", { name: "Audit / Validate" }).click();
  const projectedPrompt = appModeContainer.getByRole("textbox", {
    name: "Prompt revision",
  });
  const projectedReason = appModeContainer.getByRole("textbox", {
    name: "Revision reason",
  });
  await projectedPrompt.fill("M17 transient projected draft");
  await projectedReason.fill("M17 transient projected reason");
  await projectedReason.focus();
  await page.evaluate(() => {
    const owner = document.querySelector<HTMLElement>(
      "#h3-context-e2e-container .h3c",
    );
    if (owner === null) throw new Error("M17 product owner is absent");
    (
      window as unknown as { __h3M17ProductOwner?: HTMLElement }
    ).__h3M17ProductOwner = owner;
  });
  const m17CanonicalIdentityBeforeLocale =
    await captureM17CanonicalIdentity(page);
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { api: { api: { queuePrompt: Function } } };
      __h3M17OriginalQueuePrompt?: Function;
      __h3M17QueueCount?: number;
    };
    const api = runtime.comfyAPI.api.api;
    runtime.__h3M17OriginalQueuePrompt = api.queuePrompt;
    runtime.__h3M17QueueCount = 0;
    api.queuePrompt = function (...args: unknown[]) {
      runtime.__h3M17QueueCount = (runtime.__h3M17QueueCount ?? 0) + 1;
      return runtime.__h3M17OriginalQueuePrompt?.apply(this, args);
    };
  });
  for (const [locale, auditLabel] of [
    ["zh-TW", "稽核／驗證"],
    ["zh-CN", "审核／验证"],
    ["en", "Audit / Validate"],
  ] as const) {
    await setSupportedH3Language(page, locale);
    await expect(
      appModeContainer.getByRole("tab", { name: auditLabel }),
    ).toHaveAttribute("aria-selected", "true");
    const localeState = await page.evaluate(() => {
      const current = document.querySelector<HTMLElement>(
        "#h3-context-e2e-container .h3c",
      );
      const original = (
        window as unknown as { __h3M17ProductOwner?: HTMLElement }
      ).__h3M17ProductOwner;
      if (current !== original)
        throw new Error("locale switch replaced the product owner");
      return {
        focus: (document.activeElement as HTMLElement | null)?.dataset
          .h3FocusKey,
        page: current?.querySelector<HTMLElement>(
          '[data-h3-focus-key="page-context"]',
        )?.ariaCurrent,
      };
    });
    expect(localeState).toEqual({ focus: "workspace-reason", page: "page" });
    expect(await captureM17CanonicalIdentity(page)).toEqual(
      m17CanonicalIdentityBeforeLocale,
    );
  }
  await setSupportedH3Language(page, "auto", "zh_CN");
  await expect(
    appModeContainer.getByRole("tab", { name: "审核／验证" }),
  ).toHaveAttribute("aria-selected", "true");
  await setSupportedH3Language(page, "en");

  const performanceBeforeDestroy = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    ) as
      { h3PerformanceReceipt?: () => FrontendPerformanceReceipt } | undefined;
    if (typeof extension?.h3PerformanceReceipt !== "function")
      throw new Error("H3 performance receipt is absent");
    return extension.h3PerformanceReceipt();
  });
  expect(performanceBeforeDestroy).toMatchObject({
    schema: "h3.frontend.performance.v1",
    source: "browser_user_timing",
    cleanup_verified: false,
  });
  expect(performanceBeforeDestroy.mount_ms).toBeGreaterThanOrEqual(0);
  expect(performanceBeforeDestroy.render_ms).toBeGreaterThanOrEqual(0);
  expect(performanceBeforeDestroy.refresh_ms).toBeGreaterThanOrEqual(0);
  expect(performanceBeforeDestroy.decode_ms).toBeGreaterThanOrEqual(0);
  expect(performanceBeforeDestroy.projection_peak_bytes).toBeGreaterThan(0);
  expect(performanceBeforeDestroy.projection_peak_bytes).toBeLessThanOrEqual(
    131_072,
  );
  expect(performanceBeforeDestroy.projection_update_count).toBeGreaterThan(0);
  expect(JSON.stringify(performanceBeforeDestroy)).not.toContain(
    "Preserve the exact",
  );

  const m17CanonicalIdentityBeforeClose =
    await captureM17CanonicalIdentity(page);
  const ordinaryRefreshBeforeClose = performanceBeforeDestroy.refresh_count;

  expect(
    await page.evaluate(
      () =>
        (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey ??
        null,
    ),
  ).toBe("workspace-reason");
  await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    tab.destroy();
    if (
      !app.extensionManager
        .getSidebarTabs()
        .some((candidate: { id: string }) => candidate.id === "h3-context")
    )
      throw new Error("view close removed the H3 launcher");
  });
  await expect(page.locator("style[data-h3-context]")).toHaveCount(0);
  await expect(page.locator("#h3-context-e2e-container")).toBeEmpty();
  const restoredOwners = await page.evaluate(() => {
    const panel = document.querySelector<HTMLElement>(
      "#h3-context-e2e-host-panel",
    );
    const content = document.querySelector<HTMLElement>(
      "#h3-context-e2e-content-owner",
    );
    if (panel === null || content === null)
      throw new Error("host width owners are absent after destroy");
    return {
      panel: {
        minWidth: panel.style.minWidth,
        width: panel.style.width,
        flexBasis: panel.style.flexBasis,
      },
      contentMinWidth: content.style.minWidth,
    };
  });
  expect(restoredOwners).toEqual({
    panel: { minWidth: "11px", width: "280px", flexBasis: "280px" },
    contentMinWidth: "17px",
  });
  const ownerRestored =
    restoredOwners.panel.minWidth === "11px" &&
    restoredOwners.panel.width === "280px" &&
    restoredOwners.panel.flexBasis === "280px" &&
    restoredOwners.contentMinWidth === "17px";
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
      throw new Error("H3 launcher cannot reopen the view");
    tab.render(container);
  });
  await expect(
    appModeContainer.getByRole("tab", { name: "Audit / Validate" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    appModeContainer.getByRole("textbox", { name: "Prompt revision" }),
  ).toHaveValue("M17 transient projected draft");
  await expect(
    appModeContainer.getByRole("textbox", { name: "Revision reason" }),
  ).toHaveValue("M17 transient projected reason");
  await expect(
    appModeContainer.locator('[data-h3-focus-key="workspace-reason"]'),
  ).toBeFocused();
  const selectedPageAfterProjectedRemount = await appModeContainer
    .locator('[data-h3-focus-key="page-context"]')
    .getAttribute("aria-current");
  if (selectedPageAfterProjectedRemount !== "page")
    throw new Error("selected page changed across remount");
  expect(await captureM17CanonicalIdentity(page)).toEqual(
    m17CanonicalIdentityBeforeClose,
  );
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __h3M17QueueCount?: number })
          .__h3M17QueueCount ?? 0,
    ),
  ).toBe(0);
  const ordinaryRefreshAfterReopen = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const extension = app.extensions.find(
      (candidate: { name?: unknown }) =>
        candidate.name === "comfyui-h3-context.product-shell.v1",
    ) as
      { h3PerformanceReceipt?: () => FrontendPerformanceReceipt } | undefined;
    if (typeof extension?.h3PerformanceReceipt !== "function")
      throw new Error("H3 performance receipt is absent after ordinary reopen");
    return extension.h3PerformanceReceipt().refresh_count;
  });
  expect(ordinaryRefreshAfterReopen - ordinaryRefreshBeforeClose).toBe(1);

  // prettier-ignore
  return { ...state, appModePrompt, appModeResponse, appModeParity, repeatedPrompt, repeatedActionRequest, repeatedActionBody, networkAfterDirectProjection, subgraph, subgraphPrompt, subgraphDebug, subgraphResponse, executionParity, networkAfterSubgraphProjection, repeatedSetupLauncherCount, projectedPrompt, projectedReason, m17CanonicalIdentityBeforeLocale, performanceBeforeDestroy, m17CanonicalIdentityBeforeClose, ordinaryRefreshBeforeClose, restoredOwners, ownerRestored, selectedPageAfterProjectedRemount, ordinaryRefreshAfterReopen };
}
