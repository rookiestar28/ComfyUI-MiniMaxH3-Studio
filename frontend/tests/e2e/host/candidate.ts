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
  type TestInfo,
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

export const hostUrl = process.env.H3_CONTEXT_HOST_URL;
export const m23I2vaInputLocator =
  process.env.H3_CONTEXT_M23_18_I2VA_INPUT_LOCATOR;
export const m23RealI2vaAuthorized =
  process.env.H3_CONTEXT_M23_19_REAL_I2VA === "1";
export const m23RealI2vaHostPython =
  process.env.H3_CONTEXT_M23_19_HOST_PYTHON ?? "";
export const m23RuntimeFailureAuthorized =
  process.env.H3_CONTEXT_M23_39_RUNTIME_FAILURE ===
  "comfy_math_divide_by_zero_v1";
export const m23TerminalWaitingAuthorized =
  process.env.H3_CONTEXT_M23_32_TERMINAL_WAITING ===
  "real_success_then_fixture_artifact_v1";
export type M23TerminalWaitingArtifactLocator = Readonly<{
  filename: string;
  subfolder: string;
  type: "output";
}>;
function decodeM23TerminalWaitingArtifactLocator(
  value: string | undefined,
): M23TerminalWaitingArtifactLocator | null {
  if (value === undefined) return null;
  let wire: unknown;
  try {
    wire = JSON.parse(value);
  } catch {
    throw new Error("the M23-32 late-artifact locator is malformed");
  }
  if (wire === null || typeof wire !== "object" || Array.isArray(wire))
    throw new Error("the M23-32 late-artifact locator is malformed");
  const record = wire as Record<string, unknown>;
  const filename = record.filename;
  const subfolder = record.subfolder;
  if (
    Object.keys(record).sort().join() !== "filename,subfolder,type" ||
    typeof filename !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(filename) ||
    typeof subfolder !== "string" ||
    subfolder.length > 192 ||
    (subfolder.length > 0 &&
      !subfolder
        .split("/")
        .every((part) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(part))) ||
    record.type !== "output"
  )
    throw new Error("the M23-32 late-artifact locator is malformed");
  return Object.freeze({ filename, subfolder, type: "output" });
}
export const m23TerminalWaitingArtifactLocator =
  decodeM23TerminalWaitingArtifactLocator(
    process.env.H3_CONTEXT_M23_32_LATE_ARTIFACT_LOCATOR,
  );
export const m22ReadOnlyComposition =
  process.env.H3_CONTEXT_M22_16_READ_ONLY_COMPOSITION === "1";
export const repositoryRoot = fileURLToPath(
  new URL("../../../..", import.meta.url),
);
export type HostOfficialAssetManifest = Readonly<{
  materialization_families: Readonly<
    Record<"image_to_video" | "reference_to_video", readonly string[]>
  >;
  slots: readonly Readonly<{
    slot: string;
    loader_type: string;
    widget_name: string;
    template_default: string;
    accepted_basenames: readonly string[];
  }>[];
}>;
export const officialAssetManifest = JSON.parse(
  await readFile(
    resolve(
      repositoryRoot,
      "comfyui_h3_context/contracts/official_h3_assets_v2.json",
    ),
    "utf8",
  ),
) as HostOfficialAssetManifest;
export const candidateBackendMode =
  process.env.H3_CONTEXT_CANDIDATE_BACKEND_MODE ?? "exact";
if (!new Set(["exact", "frontend_only_host_graph"]).has(candidateBackendMode))
  throw new Error("candidate backend mode is invalid");
export const candidateBundle = loadCandidateBundleEnvironment({
  repositoryRoot,
  environment: process.env,
});
export const candidateBackendRuntime =
  candidateBundle === null ||
  candidateBackendMode === "frontend_only_host_graph"
    ? null
    : verifyCandidateBackendRuntimeEnvironment({
        repositoryRoot,
        environment: process.env,
      });
export const CANDIDATE_BUNDLE_RESOURCE_PATH =
  "/extensions/ComfyUI-MiniMaxH3-Context/h3-context-sidebar.js";
export const expectedCoInstallSidebarIds = JSON.parse(
  process.env.H3_CONTEXT_EXPECTED_SIDEBAR_IDS ?? "[]",
) as string[];
export const repositoryUrl =
  "https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio";
export type CandidateInjectionState = {
  count: number;
  hashes: Set<string>;
  resourcePaths: Set<string>;
};
export const candidateInjectionStates = new WeakMap<
  BrowserContext,
  CandidateInjectionState
>();

export function candidateBundleResourceUrl(requiredHostUrl: string): string {
  const origin = new URL(requiredHostUrl).origin;
  return new URL(CANDIDATE_BUNDLE_RESOURCE_PATH, `${origin}/`).href;
}

// IMPORTANT: this is a plain function invoked by the auto fixture in ./fixture.ts, never a
// module-level `test.beforeEach`. A hook declared at the top level of a shared module is
// registered only by the first spec file that imports the module in a worker; every later
// file in that worker reuses the cached module and gets no hook, so the candidate bundle is
// never routed and assertCandidateBundleInjection fails with an undefined state (observed
// 2026-09-03 on the first multi-file supplied-host row after the M23-26 split).
export async function registerCandidateInjection(
  context: BrowserContext,
  testInfo: TestInfo,
): Promise<void> {
  if (candidateBundle === null) {
    // Ordinary developer runs may inspect the installed extension, but cannot
    // claim exact-candidate acceptance without both binding environment values.
    testInfo.annotations.push({
      type: "candidate-binding",
      description: `non-acceptance: ${CANDIDATE_BUNDLE_PATH_ENV} and ${CANDIDATE_BUNDLE_SHA256_ENV} are absent`,
    });
    return;
  }
  if (candidateBackendMode === "frontend_only_host_graph")
    testInfo.annotations.push({
      type: "candidate-backend-runtime",
      description:
        "not-bound: exact frontend candidate against supplied compatible host backend",
    });
  else {
    if (candidateBackendRuntime === null)
      throw new Error("candidate backend runtime identity is unavailable");
    testInfo.annotations.push({
      type: "candidate-backend-runtime",
      description: `exact files=${candidateBackendRuntime.candidateFileCount} inventory=${candidateBackendRuntime.inventorySha256}`,
    });
  }
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const state: CandidateInjectionState = {
    count: 0,
    hashes: new Set(),
    resourcePaths: new Set(),
  };
  candidateInjectionStates.set(context, state);
  const exactResourceUrl = candidateBundleResourceUrl(hostUrl);
  await context.route(exactResourceUrl, async (route) => {
    const requestUrl = route.request().url();
    if (requestUrl !== exactResourceUrl)
      throw new Error("candidate bundle route received a non-exact resource");
    state.count += 1;
    state.hashes.add(candidateBundle.sha256);
    state.resourcePaths.add(new URL(requestUrl).pathname);
    await route.fulfill({
      status: 200,
      body: candidateBundle.bytes,
      contentType: "text/javascript; charset=utf-8",
      headers: {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  });
}

export function candidateInjectionCount(context: BrowserContext): number {
  return candidateInjectionStates.get(context)?.count ?? 0;
}

export async function assertCandidateBundleInjection(
  page: Page,
  context: BrowserContext,
  countBeforeNavigation: number,
): Promise<void> {
  if (candidateBundle === null) return;
  const state = candidateInjectionStates.get(context);
  expect(state?.count).toBe(countBeforeNavigation + 1);
  expect([...(state?.hashes ?? [])]).toEqual([candidateBundle.sha256]);
  expect([...(state?.resourcePaths ?? [])]).toEqual([
    CANDIDATE_BUNDLE_RESOURCE_PATH,
  ]);
  const pageEvidence = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    return {
      registryCount: app.extensionManager
        .getSidebarTabs()
        .filter((tab: { id: string }) => tab.id === "h3-context").length,
    };
  });
  expect(pageEvidence.registryCount).toBe(1);
  // CRITICAL: do not use PerformanceResourceTiming as candidate admission. A supplied host may
  // clear or evict that optional browser buffer after the exact route was fulfilled, producing a
  // false missing-candidate failure. The route count/hash/path above plus the live registry are
  // the authoritative request-byte and execution-effect proof.
}
// M23-26 registration phase One.
// prettier-ignore
import type { LayoutState, AppModePhase, LayoutGridKind, LayoutVariant, LayoutPlacement, LayoutWidthMode, LayoutLocale, LayoutTheme, LayoutLifecycle, HostPersistenceSnapshot, LayoutCaptureEvidence, LayoutEvidenceJoin, LayoutEvidenceRow, LayoutCell, LayoutStateContract, LayoutRect, LayoutOwnerStyle, LayoutControlGeometry, LayoutNavigationTab, LayoutHeaderGeometry, LayoutFocusTargetKind, LayoutMeasurement, SerializedGraph, ManagedRouteReceipt, HostAssetResolutionReceipt, SampleProgress, RealI2vaArtifactMetadata, RealI2vaEnvironment, FrontendPerformanceReceipt } from "./environment";
export async function runRegistrationPhaseOne(state: {
  shared: typeof import("./environment");
  context: BrowserContext;
  page: Page;
  testInfo: import("@playwright/test").TestInfo;
}) {
  // prettier-ignore
  const { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity } = state.shared;
  // prettier-ignore
  const { context, page, testInfo } = state;
  // M23-40: this row's managed run executes the sidebar-default t2va path for
  // real (see the queuePrompt guard below), so it requires explicit sample
  // authority and the M23-19 generation budget, but no private media source.
  test.skip(
    !SAMPLE_AUTHORIZED,
    "the layout row's real managed run requires explicit weight-sample authority",
  );
  test.setTimeout(30 * 60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const captureInteractionNetworkPhase = (phase: string) => {
    const snapshot = networkAttribution.snapshot();
    return {
      phase,
      remoteCount: snapshot.interactionRemoteCount,
      providerCount: snapshot.interactionProviderCount,
    };
  };
  const interactionErrors = {
    active: false,
    consoleErrorCount: 0,
    pageErrorCount: 0,
  };
  page.on("console", (message) => {
    if (interactionErrors.active && message.type() === "error")
      interactionErrors.consoleErrorCount += 1;
  });
  page.on("pageerror", () => {
    if (interactionErrors.active) interactionErrors.pageErrorCount += 1;
  });

  const initialInjectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  // IMPORTANT: host locale is persistent user state; copy-bearing assertions
  // must establish their presentation language rather than inherit it.
  await setSupportedH3Language(page, "en");
  const initialStorageKeys = await page.evaluate(() => ({
    local: Object.keys(localStorage)
      .filter((key) => /h3|provider|credential|ollama|minimax/i.test(key))
      .sort(),
    session: Object.keys(sessionStorage)
      .filter((key) => /h3|provider|credential|ollama|minimax/i.test(key))
      .sort(),
  }));
  const registryEvidence = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    return {
      h3TabCount: app.extensionManager
        .getSidebarTabs()
        .filter((tab: { id: string }) => tab.id === "h3-context").length,
      h3ExtensionCount: Array.isArray(app.extensions)
        ? app.extensions.filter(
            (extension: { name?: unknown }) =>
              extension.name === "comfyui-h3-context.product-shell.v1",
          ).length
        : 0,
    };
  });
  expect(registryEvidence).toEqual({ h3TabCount: 1, h3ExtensionCount: 1 });
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  for (const sidebarId of expectedCoInstallSidebarIds) {
    const registered = await page.evaluate((id) => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      return app.extensionManager
        .getSidebarTabs()
        .some((tab: { id: string }) => tab.id === id);
    }, sidebarId);
    expect(registered, `expected co-installed sidebar ${sidebarId}`).toBe(true);
  }
  const coInstallDefinitions = await page.evaluate((sidebarIds) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    return sidebarIds.map((sidebarId) => {
      const tab = app.extensionManager
        .getSidebarTabs()
        .find((candidate: { id: string }) => candidate.id === sidebarId);
      if (
        tab === undefined ||
        tab.type !== "custom" ||
        typeof tab.render !== "function"
      )
        throw new Error(`co-installation tab is not operational: ${sidebarId}`);
      return {
        sidebarId,
        type: tab.type,
        render: typeof tab.render,
      };
    });
  }, expectedCoInstallSidebarIds);
  expect(coInstallDefinitions).toEqual(
    expectedCoInstallSidebarIds.map((sidebarId) => ({
      sidebarId,
      type: "custom",
      render: "function",
    })),
  );
  await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    if (tab === undefined || tab.type !== "custom")
      throw new Error("H3 custom tab is absent");
    const splitter = document.createElement("div");
    splitter.id = "h3-context-e2e-splitter";
    splitter.className = "p-splitter";
    splitter.dataset.pcName = "splitter";
    splitter.style.position = "fixed";
    splitter.style.inset = "100px 0 0 58px";
    splitter.style.zIndex = "0";
    const panel = document.createElement("div");
    panel.id = "h3-context-e2e-host-panel";
    panel.className = "p-splitterpanel side-bar-panel";
    panel.style.position = "fixed";
    panel.style.inset = "100px auto 0 58px";
    panel.style.zIndex = "2000";
    panel.style.width = "280px";
    panel.style.minWidth = "11px";
    panel.style.flexBasis = "280px";
    panel.style.overflow = "auto";
    panel.style.background = "#202124";
    panel.dataset.h3WidthMode = "unified";
    const central = document.createElement("div");
    central.id = "h3-context-e2e-central-panel";
    central.className = "p-splitterpanel";
    central.style.position = "fixed";
    central.style.inset = "100px 0 0 58px";
    central.style.zIndex = "1";
    const canvas = document.createElement("div");
    canvas.id = "h3-context-e2e-canvas";
    canvas.className = "graph-canvas-panel";
    canvas.dataset.h3ContextCanvas = "true";
    canvas.style.position = "fixed";
    canvas.style.inset = "100px 0 0 58px";
    canvas.style.zIndex = "1";
    const opposite = document.createElement("div");
    opposite.id = "h3-context-e2e-opposite-panel";
    opposite.className = "p-splitterpanel side-bar-panel";
    opposite.style.position = "fixed";
    opposite.style.inset = "100px 0 0 auto";
    opposite.style.width = "320px";
    opposite.style.minWidth = "320px";
    opposite.style.flexBasis = "320px";
    opposite.style.zIndex = "1000";
    opposite.style.display = "none";
    const content = document.createElement("div");
    content.id = "h3-context-e2e-content-owner";
    content.className = "sidebar-content-container";
    content.style.width = "100%";
    content.style.minWidth = "17px";
    const container = document.createElement("div");
    container.id = "h3-context-e2e-container";
    container.style.width = "28rem";
    content.append(container);
    panel.append(content);
    central.append(canvas);
    splitter.append(panel, central, opposite);
    document.body.append(splitter);
    tab.render(container);
  });
  await expect(
    page.getByRole("heading", { name: "MiniMax H3 Studio" }),
  ).toBeVisible();
  const workflowTemplateDialog = page.locator(
    '[role="dialog"][aria-labelledby="global-workflow-template-selector"]',
  );
  if (await workflowTemplateDialog.isVisible()) {
    await page.keyboard.press("Escape");
    await expect(workflowTemplateDialog).toBeHidden();
  }
  const appModeContainer = page.locator("#h3-context-e2e-container");
  const shellStatus = appModeContainer.locator(
    '.h3-context-status[role="status"]',
  );
  const awaitAppModePhase = async (expected: AppModePhase): Promise<void> => {
    const phaseLocator = appModeContainer.locator(
      `[data-shell-status="${expected}"]`,
    );
    await expect(phaseLocator).toHaveCount(1);
    await expect(phaseLocator).toBeVisible();
  };
  await expect(shellStatus).toHaveText("interactive");
  await expect(appModeContainer.getByLabel("Task mode")).toHaveValue("t2va");
  await expect(
    appModeContainer.getByLabel("Task mode").locator("option"),
  ).toHaveCount(5);
  await expect(
    appModeContainer.getByRole("textbox", { name: "Intent" }),
  ).toBeVisible();
  await expect(
    appModeContainer.getByRole("spinbutton", {
      name: "Clip duration (seconds)",
    }),
  ).toHaveValue("5");
  await expect(
    appModeContainer.getByRole("button", { name: "Start H3 App Mode" }),
  ).toBeEnabled();
  await expect(appModeContainer.getByRole("tab")).toHaveCount(5);
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  interactionErrors.active = true;
  const visualDirectory = process.env.H3_CONTEXT_VISUAL_DIR;
  if (visualDirectory !== undefined)
    await mkdir(visualDirectory, { recursive: true });
  const snapshotHostStorage = async (): Promise<{
    local: Record<string, string>;
    session: Record<string, string>;
  }> =>
    page.evaluate(async () => {
      const digestStorage = async (
        storage: Storage,
      ): Promise<Record<string, string>> => {
        const digest = async (value: string): Promise<string> => {
          const bytes = await crypto.subtle.digest(
            "SHA-256",
            new TextEncoder().encode(value),
          );
          return Array.from(new Uint8Array(bytes))
            .map((part) => part.toString(16).padStart(2, "0"))
            .join("");
        };
        const result: Record<string, string> = {};
        for (const key of Object.keys(storage)
          .filter(
            (candidate) =>
              /splitter|sidebar|panel|h3-context/i.test(candidate) &&
              // M23-21 discloses exactly one repository key: the bounded
              // managed-run journal mirror, written on every lifecycle
              // transition this row itself drives. Every other key must
              // stay byte-identical across the row (M23-37).
              candidate !== "h3-context.managed-journal.v1",
          )
          .sort())
          result[key] = await digest(storage.getItem(key) ?? "");
        return result;
      };
      return {
        local: await digestStorage(localStorage),
        session: await digestStorage(sessionStorage),
      };
    });
  const snapshotHostPersistence = async (): Promise<HostPersistenceSnapshot> =>
    page.evaluate(async (keys) => {
      const digest = async (value: string): Promise<string> => {
        const bytes = await crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(value),
        );
        return Array.from(new Uint8Array(bytes))
          .map((part) => part.toString(16).padStart(2, "0"))
          .join("");
      };
      const values: Record<string, string> = {};
      for (const key of keys) {
        const value = localStorage.getItem(key);
        if (value !== null) values[key] = await digest(value);
      }
      return {
        local: values,
        session: {},
        source: "comfyui_frontend_v1.48.7",
        seeded: keys.every((key) => values[key] !== undefined),
      };
    }, HOST_PERSISTENCE_KEY_FAMILY);
  const seedHostPersistenceSentinel = async (): Promise<void> => {
    await page.evaluate(
      ({ keys, sentinel }) => {
        for (const key of keys) {
          // The fixture owns this redacted numeric PrimeVue value only for the
          // probe; the exact pre-test value is restored before reload/cleanup.
          localStorage.setItem(key, sentinel);
          const raw = localStorage.getItem(key);
          if (raw !== sentinel)
            throw new Error(`host persistence sentinel was not stored: ${key}`);
          const parsed: unknown = JSON.parse(raw);
          if (
            !Array.isArray(parsed) ||
            parsed.length !== 2 ||
            parsed.some(
              (value) => typeof value !== "number" || !Number.isFinite(value),
            )
          )
            throw new Error(
              `host persistence value is not a numeric array: ${key}`,
            );
        }
      },
      {
        keys: HOST_PERSISTENCE_KEY_FAMILY,
        sentinel: HOST_PERSISTENCE_SENTINEL_VALUE,
      },
    );
  };
  const originalHostPersistence = await page.evaluate((keys) => {
    const values: Record<string, string | null> = {};
    for (const key of keys) values[key] = localStorage.getItem(key);
    return values;
  }, HOST_PERSISTENCE_KEY_FAMILY);
  await seedHostPersistenceSentinel();
  const initialHostStorage = await snapshotHostStorage();
  const hostPersistenceBefore = await snapshotHostPersistence();
  expect(Object.keys(hostPersistenceBefore.local)).not.toHaveLength(0);
  const storageSentinelDigest = (snapshot: HostPersistenceSnapshot): string =>
    createHash("sha256").update(JSON.stringify(snapshot), "utf8").digest("hex");
  const hostPersistenceBeforeDigest = storageSentinelDigest(
    hostPersistenceBefore,
  );
  const snapshotHostOwner = async (): Promise<LayoutOwnerStyle> =>
    page.evaluate(() => {
      const panel = document.querySelector<HTMLElement>(
        "#h3-context-e2e-host-panel",
      );
      if (panel === null) throw new Error("layout host owner is absent");
      return {
        minWidth: panel.style.minWidth,
        width: panel.style.width,
        flexBasis: panel.style.flexBasis,
      };
    });
  const layoutEvidence: LayoutEvidenceRow[] = [];
  const captureManifest: LayoutCaptureEvidence[] = [];
  const visualPhase = process.env.H3_CONTEXT_VISUAL_PHASE ?? "candidate";
  const candidateIdentity =
    process.env.H3_CONTEXT_IMPLEMENTATION_COMMIT ?? "not-provided";
  const hostReportPath = process.env.H3_CONTEXT_HOST_REPORT ?? null;
  const captureSettledScreenshot = async (
    target: Locator,
    path: string,
  ): Promise<void> => {
    await expect(target).toHaveCount(1, { timeout: 5_000 });
    await expect(target).toBeVisible({ timeout: 5_000 });
    const clip = await target.evaluate(async (element) => {
      if (!element.isConnected)
        throw new Error("capture target detached before settle");
      const readRect = (): {
        x: number;
        y: number;
        width: number;
        height: number;
      } => {
        const rect = element.getBoundingClientRect();
        return {
          x: rect.left,
          y: rect.top,
          width: rect.width,
          height: rect.height,
        };
      };
      let previous = readRect();
      let stableFrames = 0;
      for (let frame = 0; frame < 8; frame += 1) {
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        );
        if (!element.isConnected)
          throw new Error("capture target detached during settle");
        const current = readRect();
        const stable =
          Math.abs(current.x - previous.x) <= 1 &&
          Math.abs(current.y - previous.y) <= 1 &&
          Math.abs(current.width - previous.width) <= 1 &&
          Math.abs(current.height - previous.height) <= 1;
        stableFrames = stable ? stableFrames + 1 : 0;
        previous = current;
        if (stableFrames >= 2) break;
      }
      if (stableFrames < 2)
        throw new Error("capture target did not settle within eight frames");
      if (previous.width <= 0 || previous.height <= 0)
        throw new Error("capture target has no capture area");
      return previous;
    });
    await page.screenshot({
      path,
      clip,
    });
  };
  const applyLayoutCell = async (
    cell: LayoutCell,
    options: Readonly<{ rerender?: boolean }> = {},
  ): Promise<void> => {
    await page.evaluate((target) => {
      const panel = document.querySelector<HTMLElement>(
        "#h3-context-e2e-host-panel",
      );
      const opposite = document.querySelector<HTMLElement>(
        "#h3-context-e2e-opposite-panel",
      );
      const splitter = document.querySelector<HTMLElement>(
        "#h3-context-e2e-splitter",
      );
      const central = document.querySelector<HTMLElement>(
        "#h3-context-e2e-central-panel",
      );
      const root = document.documentElement;
      if (
        panel === null ||
        opposite === null ||
        splitter === null ||
        central === null
      )
        throw new Error("layout topology fixture is absent");
      if (
        !panel.classList.contains("p-splitterpanel") ||
        !panel.classList.contains("side-bar-panel") ||
        !central.classList.contains("p-splitterpanel") ||
        !opposite.classList.contains("p-splitterpanel") ||
        !opposite.classList.contains("side-bar-panel")
      )
        throw new Error("layout fixture lost pinned splitter owner topology");
      const topOffset = target.accessibilityVariant ? "200px" : "100px";
      panel.style.inset =
        target.placement === "left"
          ? `${topOffset} auto 0 58px`
          : `${topOffset} 0 0 auto`;
      panel.dataset.h3WidthMode = target.widthMode;
      splitter.dataset.h3WidthMode = target.widthMode;
      central.dataset.h3WidthMode = target.widthMode;
      opposite.dataset.h3WidthMode = target.widthMode;
      opposite.style.width = target.viewportWidth <= 480 ? "122px" : "320px";
      opposite.style.minWidth = target.viewportWidth <= 480 ? "122px" : "320px";
      opposite.style.flexBasis =
        target.viewportWidth <= 480 ? "122px" : "320px";
      opposite.style.display = target.oppositePanel ? "block" : "none";
      opposite.style.inset =
        target.placement === "left"
          ? `${topOffset} 0 0 auto`
          : `${topOffset} auto 0 58px`;
      root.lang = target.locale;
      root.dataset.h3Theme = target.theme;
    }, cell);
    await page.emulateMedia({
      colorScheme: cell.theme,
      reducedMotion: cell.accessibilityVariant ? "reduce" : "no-preference",
      forcedColors: cell.accessibilityVariant ? "active" : "none",
    });
    await page.evaluate(
      (target) => {
        document.documentElement.style.fontSize = target.accessibilityVariant
          ? "200%"
          : "";
        const app = (window as unknown as { comfyAPI: { app: { app: any } } })
          .comfyAPI.app.app;
        if (target.rerender !== false) {
          const graphEvents = app.graph?.events as EventTarget | undefined;
          if (typeof graphEvents?.dispatchEvent !== "function")
            throw new Error("presentation rerender graph seam is absent");
          graphEvents.dispatchEvent(new Event("graphChanged"));
        }
        if (target.lifecycle !== "remount") return;
        const tab = app.extensionManager
          .getSidebarTabs()
          .find((candidate: { id: string }) => candidate.id === "h3-context");
        const mount = document.querySelector<HTMLElement>(
          "#h3-context-e2e-container",
        );
        if (tab === undefined || mount === null)
          throw new Error("remount topology target is absent");
        tab.render(mount);
      },
      { ...cell, rerender: options.rerender ?? true },
    );
    await page.waitForTimeout(0);
  };
  // prettier-ignore
  return { ...state, allowedOrigin, networkAttribution, candidateNetworkAttribution, captureInteractionNetworkPhase, interactionErrors, initialInjectionCount, initialStorageKeys, registryEvidence, coInstallDefinitions, workflowTemplateDialog, appModeContainer, shellStatus, awaitAppModePhase, visualDirectory, snapshotHostStorage, snapshotHostPersistence, seedHostPersistenceSentinel, originalHostPersistence, initialHostStorage, hostPersistenceBefore, storageSentinelDigest, hostPersistenceBeforeDigest, snapshotHostOwner, layoutEvidence, captureManifest, visualPhase, candidateIdentity, hostReportPath, captureSettledScreenshot, applyLayoutCell };
}
