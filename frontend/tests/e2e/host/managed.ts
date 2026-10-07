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

import { hostUrl } from "./candidate";
/**
 * M17-20 supported-host lane.
 *
 * Plan section 5 lane 6 samples the exact runtime candidate on the supplied
 * pinned host. Two things are sampled here and they are deliberately separate.
 *
 * The first test asks whether the backend qualification and the materialization
 * are real on this host: the projection is decoded by the shipped decoder rather
 * than eyeballed, the pinned template is loaded onto the canvas, and the graph
 * that lands is a complete flow ending in one artifact sink at the location this
 * run derived. It does not execute anything -- the queue seam is counted, not
 * called through -- because execution is the separately authorized sample below.
 *
 * The second test is that sample (AC-M17-20-08). It runs only when the
 * maintainer has authorized it for this session and named the installed weights,
 * because it loads tens of gigabytes and produces a real file. Absent that
 * authorization it is skipped and the criterion stays NOT_RUN, which is what the
 * plan requires rather than a weaker substitute.
 */

export const SAMPLE_AUTHORIZED = process.env.H3_CONTEXT_WEIGHT_SAMPLE === "1";
export const SAMPLE_UNET = process.env.H3_CONTEXT_SAMPLE_UNET ?? "";
export const SAMPLE_CLIP = process.env.H3_CONTEXT_SAMPLE_CLIP ?? "";

export type SerializedGraph = {
  nodes?: Array<Record<string, unknown>>;
  links?: unknown[][];
  definitions?: { subgraphs?: Array<Record<string, unknown>> };
};

/**
 * Count the canvas writes the host performs, whoever asks for them.
 *
 * A run that materializes writes the canvas exactly once. A second write is the
 * rollback, and a rollback here would mean the run abandoned the graph it had
 * just produced -- which is precisely the failure a stubbed graph seam cannot
 * show, because only a real host announces a configure back to its extensions.
 */
export async function instrumentHostGraphLoads(page: Page): Promise<void> {
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3GraphLoads?: number;
      __h3GraphLoadsInFlight?: number;
      __h3GraphWorkflowAuthority?: object | null;
      __h3GraphWorkflowOpenCount?: number;
      __h3GraphWorkflowTargets?: boolean[];
    };
    const app = runtime.comfyAPI.app.app;
    const workflowStore = app.extensionManager?.workflow;
    const activeWorkflow = workflowStore?.activeWorkflow;
    if (
      !Array.isArray(workflowStore?.openWorkflows) ||
      !(
        activeWorkflow === null ||
        (typeof activeWorkflow === "object" && !Array.isArray(activeWorkflow))
      ) ||
      (activeWorkflow === null && workflowStore.openWorkflows.length !== 0)
    )
      throw new Error("the public host workflow store is unavailable");
    runtime.__h3GraphLoads = 0;
    runtime.__h3GraphLoadsInFlight = 0;
    runtime.__h3GraphWorkflowAuthority = activeWorkflow;
    runtime.__h3GraphWorkflowOpenCount = workflowStore.openWorkflows.length;
    runtime.__h3GraphWorkflowTargets = [];
    const original = app.loadGraphData.bind(app);
    app.loadGraphData = async function (...args: unknown[]) {
      runtime.__h3GraphLoads = (runtime.__h3GraphLoads ?? 0) + 1;
      runtime.__h3GraphLoadsInFlight =
        (runtime.__h3GraphLoadsInFlight ?? 0) + 1;
      const authorityAtCall = runtime.__h3GraphWorkflowAuthority ?? null;
      const targetMatches = args.length >= 4 && args[3] === authorityAtCall;
      runtime.__h3GraphWorkflowTargets?.push(targetMatches);
      try {
        const result = await original(...args);
        // The guarded App Mode bootstrap starts with explicit null authority. Once
        // ComfyUI has attached exactly one workflow, follow that returned object so
        // every subsequent materialization/rollback target can be checked exactly.
        if (authorityAtCall === null && targetMatches) {
          const attached = workflowStore.activeWorkflow;
          if (
            attached !== null &&
            typeof attached === "object" &&
            !Array.isArray(attached) &&
            workflowStore.openWorkflows.length ===
              (runtime.__h3GraphWorkflowOpenCount ?? 0) + 1 &&
            workflowStore.openWorkflows.includes(attached)
          )
            runtime.__h3GraphWorkflowAuthority = attached;
        }
        return result;
      } finally {
        runtime.__h3GraphLoadsInFlight = Math.max(
          0,
          (runtime.__h3GraphLoadsInFlight ?? 1) - 1,
        );
      }
    };
  });
}

export async function hostGraphLoads(page: Page): Promise<number> {
  return (
    (await page.evaluate(
      () => (window as unknown as { __h3GraphLoads?: number }).__h3GraphLoads,
    )) ?? 0
  );
}

export async function resetHostGraphLoads(page: Page): Promise<void> {
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3GraphLoads?: number;
      __h3GraphWorkflowAuthority?: object | null;
      __h3GraphWorkflowOpenCount?: number;
      __h3GraphWorkflowTargets?: boolean[];
    };
    const workflowStore = runtime.comfyAPI.app.app.extensionManager?.workflow;
    const activeWorkflow = workflowStore?.activeWorkflow;
    if (
      !Array.isArray(workflowStore?.openWorkflows) ||
      !(
        activeWorkflow === null ||
        (typeof activeWorkflow === "object" && !Array.isArray(activeWorkflow))
      ) ||
      (activeWorkflow === null && workflowStore.openWorkflows.length !== 0)
    )
      throw new Error("the public host workflow store is unavailable");
    runtime.__h3GraphLoads = 0;
    runtime.__h3GraphWorkflowAuthority = activeWorkflow;
    runtime.__h3GraphWorkflowOpenCount = workflowStore.openWorkflows.length;
    runtime.__h3GraphWorkflowTargets = [];
  });
}

export async function hostWorkflowAuthorityReceipt(page: Page): Promise<{
  activeStable: boolean;
  openWorkflowDelta: number;
  graphLoadWorkflowMatches: boolean[];
}> {
  return await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3GraphWorkflowAuthority?: object | null;
      __h3GraphWorkflowOpenCount?: number;
      __h3GraphWorkflowTargets?: boolean[];
    };
    const workflowStore = runtime.comfyAPI.app.app.extensionManager?.workflow;
    if (!Array.isArray(workflowStore?.openWorkflows))
      throw new Error("the public host workflow store is unavailable");
    return {
      activeStable:
        workflowStore.activeWorkflow === runtime.__h3GraphWorkflowAuthority,
      openWorkflowDelta:
        workflowStore.openWorkflows.length -
        (runtime.__h3GraphWorkflowOpenCount ?? 0),
      graphLoadWorkflowMatches: [...(runtime.__h3GraphWorkflowTargets ?? [])],
    };
  });
}

export async function supportedHostQueueCounts(
  page: Page,
): Promise<{ running: number; pending: number }> {
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const response = await page.request.get(new URL("/queue", hostUrl).href);
  expect(response.ok()).toBe(true);
  const wire = (await response.json()) as {
    queue_running?: unknown;
    queue_pending?: unknown;
  };
  if (!Array.isArray(wire.queue_running) || !Array.isArray(wire.queue_pending))
    throw new Error("the supplied host queue response is malformed");
  return {
    running: wire.queue_running.length,
    pending: wire.queue_pending.length,
  };
}

export type ManagedRouteReceipt = Readonly<{
  route: "host_prompt" | "production_action" | "sequence_coordinator";
  action: string;
  stage: "request" | "response" | "request_failed";
  status: number | null;
}>;

export function monitorManagedRouteResponses(
  page: Page,
): ManagedRouteReceipt[] {
  const receipts: ManagedRouteReceipt[] = [];
  const actions = new Set([
    "create_workspace_from_context",
    "prepare_sequence",
    "prepare_managed_run",
    "submit_managed_run",
    "close_managed_run",
    "read_managed_run",
    "release_sequence",
    "release_workspace",
  ]);
  const classify = (url: string): ManagedRouteReceipt["route"] | undefined => {
    const pathname = new URL(url).pathname;
    if (pathname.endsWith("/h3-context/v1/production/action"))
      return "production_action";
    if (pathname.endsWith("/h3-context/v1/generation/coordinator"))
      return "sequence_coordinator";
    if (pathname.endsWith("/prompt")) return "host_prompt";
    return undefined;
  };
  const action = (
    route: ManagedRouteReceipt["route"],
    postData: string | null,
  ): string => {
    if (route === "host_prompt") return "queue_prompt";
    try {
      const body = JSON.parse(postData ?? "null") as {
        action?: unknown;
      } | null;
      if (typeof body?.action === "string" && actions.has(body.action))
        return body.action;
    } catch {
      // The receipt is deliberately content-free; malformed bodies stay opaque.
    }
    return "unknown";
  };
  page.on("request", (request) => {
    const route = classify(request.url());
    if (route === undefined) return;
    receipts.push({
      route,
      action: action(route, request.postData()),
      stage: "request",
      status: null,
    });
  });
  page.on("response", (response) => {
    const route = classify(response.url());
    if (route === undefined) return;
    receipts.push({
      route,
      action: action(route, response.request().postData()),
      stage: "response",
      status: response.status(),
    });
  });
  page.on("requestfailed", (request) => {
    const route = classify(request.url());
    if (route === undefined) return;
    receipts.push({
      route,
      action: action(route, request.postData()),
      stage: "request_failed",
      status: null,
    });
  });
  return receipts;
}

export async function appModeQueueDiagnostic(
  page: Page,
  containerId: string,
  counterKey: "__h3M1515Queue" | "__h3QueueCount" | "__h3M2319QueueKinds",
): Promise<{
  queueCount: number;
  shellStatus: string | null;
  shellReason: string | null;
  appModeState: string | null;
  appModePhase: string | null;
  generationBlocker: string | null;
  errorClass: string;
  graphLoads: number;
  compileCalls: number;
  compileSucceeded: number;
  compileFailed: number;
  compileShape: Record<string, unknown>;
  queuePhases: readonly unknown[];
  managedTrace: readonly unknown[];
  hostProjectionTrace: readonly unknown[];
  projectionTrace: readonly unknown[];
  submitCount: number;
  submitDisabled: boolean | null;
  keepCount: number;
}> {
  return await page.evaluate(
    ({ key, targetId }) => {
      const runtime = window as unknown as Record<string, unknown>;
      const value = runtime[key];
      const queueCount = Array.isArray(value)
        ? value.length
        : typeof value === "number" && Number.isSafeInteger(value)
          ? value
          : 0;
      const container = document.getElementById(targetId);
      const shell = container?.querySelector<HTMLElement>(
        "[data-shell-status]",
      );
      const appMode = container?.querySelector<HTMLElement>(
        "[data-app-mode-state]",
      );
      const blocker = container?.querySelector<HTMLElement>(
        "[data-h3-generation-blocker]",
      );
      const errorText = container
        ?.querySelector<HTMLElement>('[role="alert"] > p')
        ?.textContent?.trim();
      const knownErrors = {
        incompatible_seam:
          "The supported host seam is unavailable; native nodes remain available.",
        compile_failed: "The visible H3 graph could not be compiled.",
        queue_failed: "The normal ComfyUI queue rejected this run.",
        execution_failed: "The H3 execution failed on the host.",
        execution_interrupted: "The H3 execution was interrupted on the host.",
        projection_missing:
          "The H3 execution finished, but its verified Context result was not received.",
        stale_graph:
          "The visible canvas changed during the run; retry with the current graph.",
        projection_mismatch: "The returned result did not match this run.",
        ambiguous_host_ownership:
          "ComfyUI may own this generation, but its Production correlation could not be confirmed. Do not retry automatically.",
        artifact_verification_failed:
          "The saved H3 output could not be verified safely. The original host output was left unchanged.",
        rollback_failed: "The original canvas could not be restored safely.",
      } as const;
      const errorClass =
        Object.entries(knownErrors).find(
          ([, message]) => message === errorText,
        )?.[0] ??
        (errorText !== undefined && Object.hasOwn(knownErrors, errorText)
          ? errorText
          : errorText === undefined
            ? "absent"
            : "unknown_safe_surface");
      const compileReceipt =
        runtime.__h3GraphToPromptReceipt !== null &&
        typeof runtime.__h3GraphToPromptReceipt === "object" &&
        !Array.isArray(runtime.__h3GraphToPromptReceipt)
          ? (runtime.__h3GraphToPromptReceipt as Record<string, unknown>)
          : {};
      const compileShape =
        runtime.__h3CompiledShape !== null &&
        typeof runtime.__h3CompiledShape === "object" &&
        !Array.isArray(runtime.__h3CompiledShape)
          ? (runtime.__h3CompiledShape as Record<string, unknown>)
          : {};
      const submits = container?.querySelectorAll<HTMLButtonElement>(
        '[data-h3-focus-key="app-submit"]',
      );
      const submit = submits?.[0];
      const managedWire = (() => {
        try {
          const raw = localStorage.getItem("h3-context.managed-journal.v1");
          if (raw === null) return undefined;
          const parsed = JSON.parse(raw) as { entries?: unknown };
          return Array.isArray(parsed.entries) ? parsed.entries : undefined;
        } catch {
          return undefined;
        }
      })();
      return {
        queueCount,
        shellStatus: shell?.dataset.shellStatus ?? null,
        shellReason: shell?.dataset.shellReason ?? null,
        appModeState: appMode?.dataset.appModeState ?? null,
        appModePhase: appMode?.dataset.appModePhase ?? null,
        generationBlocker: blocker?.dataset.h3GenerationBlocker ?? null,
        errorClass,
        graphLoads:
          typeof runtime.__h3GraphLoads === "number"
            ? runtime.__h3GraphLoads
            : 0,
        compileCalls:
          typeof compileReceipt.calls === "number" ? compileReceipt.calls : 0,
        compileSucceeded:
          typeof compileReceipt.succeeded === "number"
            ? compileReceipt.succeeded
            : 0,
        compileFailed:
          typeof compileReceipt.failed === "number" ? compileReceipt.failed : 0,
        compileShape,
        queuePhases: Array.isArray(runtime.__h3QueuePhases)
          ? runtime.__h3QueuePhases.slice(-8)
          : [],
        managedTrace: managedWire?.slice(-16) ?? [],
        hostProjectionTrace: Array.isArray(runtime.__h3HostProjectionTrace)
          ? runtime.__h3HostProjectionTrace
              .filter(
                (entry) =>
                  entry !== null &&
                  typeof entry === "object" &&
                  !Array.isArray(entry) &&
                  (entry as Record<string, unknown>).stage !== "graph",
              )
              .slice(-8)
          : [],
        projectionTrace: Array.isArray(runtime.__h3ProjectionTrace)
          ? runtime.__h3ProjectionTrace.slice(-8)
          : [],
        submitCount: submits?.length ?? 0,
        submitDisabled:
          submit instanceof HTMLButtonElement ? submit.disabled : null,
        keepCount:
          container?.querySelectorAll('[data-h3-focus-key="app-keep-canvas"]')
            .length ?? 0,
      };
    },
    { key: counterKey, targetId: containerId },
  );
}

export async function waitForAppModeQueue(
  page: Page,
  containerId: string,
  counterKey: "__h3M1515Queue" | "__h3QueueCount",
  expectedCount: number,
  timeoutMilliseconds: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMilliseconds;
  let diagnostic = await appModeQueueDiagnostic(page, containerId, counterKey);
  while (
    diagnostic.queueCount !== expectedCount &&
    diagnostic.shellStatus !== "error" &&
    Date.now() < deadline
  ) {
    await page.waitForTimeout(100);
    diagnostic = await appModeQueueDiagnostic(page, containerId, counterKey);
  }
  expect(JSON.stringify(diagnostic)).toContain(
    `"queueCount":${String(expectedCount)}`,
  );
}

export async function privateDiagnosticLeakReceipt(
  page: Page,
  containerId: string,
  boundedEvidenceKeys: readonly string[],
): Promise<{ shellState: boolean; storage: boolean; evidence: boolean }> {
  return await page.evaluate(
    ({ targetId, evidenceKeys }) => {
      const sentinels = [
        "m23-08-private-host-diagnostic",
        "m23-08-private-host-traceback",
      ];
      const includesSentinel = (value: unknown): boolean => {
        let encoded = "";
        try {
          encoded = JSON.stringify(value) ?? "";
        } catch {
          return true;
        }
        return sentinels.some((sentinel) => encoded.includes(sentinel));
      };
      const container = document.getElementById(targetId);
      const shell = container?.querySelector<HTMLElement>(
        "[data-shell-status]",
      );
      // The reducer is bundle-private. Its bounded public state projection is
      // the status/reason plus rendered shell tree; scan that exact projection.
      const shellStateProjection = {
        status: shell?.dataset.shellStatus ?? null,
        reason: shell?.dataset.shellReason ?? null,
        html: container?.innerHTML ?? "",
      };
      const h3OwnedStorage = (storage: Storage): Record<string, string> =>
        Object.fromEntries(
          Object.keys(storage)
            .filter((key) => /^h3(?:-context)?[.:_-]/i.test(key))
            .map((key) => [key, storage.getItem(key) ?? ""]),
        );
      const runtime = window as unknown as Record<string, unknown>;
      const boundedEvidence = Object.fromEntries(
        evidenceKeys.map((key) => [key, runtime[key]]),
      );
      return {
        shellState: includesSentinel(shellStateProjection),
        storage: includesSentinel({
          local: h3OwnedStorage(localStorage),
          session: h3OwnedStorage(sessionStorage),
        }),
        evidence: includesSentinel(boundedEvidence),
      };
    },
    { targetId: containerId, evidenceKeys: [...boundedEvidenceKeys] },
  );
}

/**
 * Wait for the host to finish opening the workflow it opens by itself.
 *
 * ComfyUI restores a workflow shortly after the extension registers, and a run
 * started before that lands is overwritten by the restore. The host publishes no
 * "startup finished" signal, so the wait is for its first canvas write followed
 * by a graph serialization that stops changing -- which is also what the user
 * waits for before touching anything. It is bounded: a host that never writes
 * one is not a reason to hang, and the assertions that follow read the canvas
 * they actually got.
 */
export async function waitForHostGraphSettled(page: Page): Promise<void> {
  const snapshot = async (): Promise<string> =>
    await page.evaluate(() => {
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any } };
        __h3GraphLoads?: number;
        __h3GraphLoadsInFlight?: number;
      };
      const app = runtime.comfyAPI.app.app;
      const graph = app.graph?.serialize?.() ?? {};
      const workflowStore = app.extensionManager?.workflow;
      const activeWorkflow = workflowStore?.activeWorkflow;
      const openWorkflows = workflowStore?.openWorkflows;
      const storeConsistent =
        Array.isArray(openWorkflows) &&
        ((activeWorkflow === null && openWorkflows.length === 0) ||
          (activeWorkflow !== null &&
            typeof activeWorkflow === "object" &&
            !Array.isArray(activeWorkflow) &&
            openWorkflows.includes(activeWorkflow)));
      return JSON.stringify({
        nodes: (graph.nodes ?? []).map((node: { type?: string }) => node.type),
        subgraphs: (graph.definitions?.subgraphs ?? []).length,
        loads: runtime.__h3GraphLoads ?? 0,
        inFlight: runtime.__h3GraphLoadsInFlight ?? 0,
        storeConsistent,
        openCount: Array.isArray(openWorkflows) ? openWorkflows.length : -1,
        activeIsNull: activeWorkflow === null,
      });
    });
  let previous: string | undefined;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const current = await snapshot();
    const parsed = JSON.parse(current) as {
      loads: number;
      inFlight: number;
      storeConsistent: boolean;
    };
    if (
      previous !== undefined &&
      current === previous &&
      parsed.loads > 0 &&
      parsed.inFlight === 0 &&
      parsed.storeConsistent
    )
      return;
    previous = current;
    await page.waitForTimeout(500);
  }
}
// M23-26 registration phase Four.
// prettier-ignore
import type { HostOfficialAssetManifest, CandidateInjectionState, LayoutState, AppModePhase, LayoutGridKind, LayoutVariant, LayoutPlacement, LayoutWidthMode, LayoutLocale, LayoutTheme, LayoutLifecycle, HostPersistenceSnapshot, LayoutCaptureEvidence, LayoutEvidenceJoin, LayoutEvidenceRow, LayoutCell, LayoutStateContract, LayoutRect, LayoutOwnerStyle, LayoutControlGeometry, LayoutNavigationTab, LayoutHeaderGeometry, LayoutFocusTargetKind, LayoutMeasurement, HostAssetResolutionReceipt, SampleProgress, RealI2vaArtifactMetadata, RealI2vaEnvironment, FrontendPerformanceReceipt } from "./environment";
// prettier-ignore
import type { runRegistrationPhaseThree } from "./layout";
export async function runRegistrationPhaseFour(
  state: Awaited<ReturnType<typeof runRegistrationPhaseThree>>,
) {
  // prettier-ignore
  const { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity } = state.shared;
  // prettier-ignore
  const { context, page, testInfo, allowedOrigin, networkAttribution, candidateNetworkAttribution, captureInteractionNetworkPhase, interactionErrors, initialInjectionCount, initialStorageKeys, registryEvidence, coInstallDefinitions, workflowTemplateDialog, appModeContainer, shellStatus, awaitAppModePhase, visualDirectory, snapshotHostStorage, snapshotHostPersistence, seedHostPersistenceSentinel, originalHostPersistence, initialHostStorage, hostPersistenceBefore, storageSentinelDigest, hostPersistenceBeforeDigest, snapshotHostOwner, layoutEvidence, captureManifest, visualPhase, candidateIdentity, hostReportPath, captureSettledScreenshot, applyLayoutCell, captureLayoutMatrix, projectedWorkspace, resetLayoutMatrixPresentation } = state;
  await captureLayoutMatrix("interactive");
  await resetLayoutMatrixPresentation("empty-canvas");
  const networkAfterLayoutFixturePrep = captureInteractionNetworkPhase(
    "after_layout_fixture_prep",
  );
  // Cancellation must abort a late graph compilation and leave the empty canvas
  // untouched; the normal start path below then exercises the same public seam.
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any }; api: { api: any } };
      __h3CancellationQueueCount?: number;
      __h3OriginalGraphToPrompt?: Function;
      __h3OriginalQueuePrompt?: Function;
      __h3ReleaseCancellationGate?: () => void;
    };
    const app = runtime.comfyAPI.app.app;
    const api = runtime.comfyAPI.api.api;
    runtime.__h3OriginalGraphToPrompt = app.graphToPrompt;
    runtime.__h3OriginalQueuePrompt = api.queuePrompt;
    runtime.__h3CancellationQueueCount = 0;
    const originalGraphToPrompt = runtime.__h3OriginalGraphToPrompt;
    const originalQueuePrompt = runtime.__h3OriginalQueuePrompt;
    if (
      typeof originalGraphToPrompt !== "function" ||
      typeof originalQueuePrompt !== "function"
    )
      throw new Error("host App Mode seams are unavailable");
    const cancellationGate = new Promise<void>((resolve) => {
      runtime.__h3ReleaseCancellationGate = resolve;
    });
    app.graphToPrompt = async function (...args: unknown[]) {
      await cancellationGate;
      return originalGraphToPrompt.apply(this, args);
    };
    api.queuePrompt = function (...args: unknown[]) {
      runtime.__h3CancellationQueueCount =
        (runtime.__h3CancellationQueueCount ?? 0) + 1;
      return originalQueuePrompt.apply(this, args);
    };
  });
  await appModeContainer
    .getByRole("textbox", { name: "Intent" })
    .fill("This request is cancelled before queueing.");
  await appModeContainer
    .getByRole("button", { name: "Start H3 App Mode" })
    .click();
  await awaitAppModePhase("working");
  // IMPORTANT: target Cancel by owned identity; M25-36 intentionally keeps New project enabled
  // while a run retains its captured destination, so counting every enabled action is ambiguous.
  const cancelAction = appModeContainer.locator(
    '[data-h3-focus-key="app-cancel"]',
  );
  await expect(cancelAction).toHaveCount(1);
  await expect(cancelAction).toBeVisible();
  await captureLayoutMatrix("working");
  await awaitAppModePhase("working");
  const preCancellationGraph = await page.evaluate(async () => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
    };
    const graph = runtime.comfyAPI.app.app.graph.serialize() as {
      nodes?: unknown[];
    };
    const serialized = JSON.stringify(graph);
    if (serialized === undefined)
      throw new Error("pre-cancellation graph identity is unavailable");
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(serialized),
    );
    return {
      graph_fingerprint: Array.from(new Uint8Array(digest))
        .map((part) => part.toString(16).padStart(2, "0"))
        .join(""),
      node_count: Array.isArray(graph.nodes) ? graph.nodes.length : 0,
    };
  });
  await cancelAction.click();
  const cancellationReason = appModeContainer.locator(
    '[data-shell-reason="cancelled"]',
  );
  await expect(cancellationReason).toHaveCount(1);
  await expect(cancellationReason).toBeVisible();
  const cancellationReceipt = await page.evaluate(async () => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3CancellationQueueCount?: number;
    };
    const graph = runtime.comfyAPI.app.app.graph.serialize() as {
      nodes?: unknown[];
    };
    const serialized = JSON.stringify(graph);
    if (serialized === undefined)
      throw new Error("cancellation graph identity is unavailable");
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(serialized),
    );
    return {
      queue_count: runtime.__h3CancellationQueueCount ?? 0,
      graph_fingerprint: Array.from(new Uint8Array(digest))
        .map((part) => part.toString(16).padStart(2, "0"))
        .join(""),
      node_count: Array.isArray(graph.nodes) ? graph.nodes.length : 0,
    };
  });
  expect(cancellationReceipt.queue_count).toBe(0);
  expect(cancellationReceipt.node_count).toBe(preCancellationGraph.node_count);
  expect(cancellationReceipt.graph_fingerprint).toBe(
    preCancellationGraph.graph_fingerprint,
  );
  await page.evaluate(() => {
    const runtime = window as unknown as {
      __h3ReleaseCancellationGate?: () => void;
    };
    runtime.__h3ReleaseCancellationGate?.();
    runtime.__h3ReleaseCancellationGate = undefined;
  });
  await captureLayoutMatrix("cancelled");
  await resetLayoutMatrixPresentation("empty-canvas", "cancelled");
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any }; api: { api: any } };
      __h3OriginalGraphToPrompt?: Function;
      __h3OriginalQueuePrompt?: Function;
    };
    if (runtime.__h3OriginalGraphToPrompt !== undefined)
      runtime.comfyAPI.app.app.graphToPrompt =
        runtime.__h3OriginalGraphToPrompt;
    if (runtime.__h3OriginalQueuePrompt !== undefined)
      runtime.comfyAPI.api.api.queuePrompt = runtime.__h3OriginalQueuePrompt;
  });
  await appModeContainer
    .getByRole("textbox", { name: "Intent" })
    .fill("A red kite crosses the sky while the camera follows its arc.");
  // prettier-ignore
  return { ...state, networkAfterLayoutFixturePrep, cancelAction, preCancellationGraph, cancellationReason, cancellationReceipt };
}
