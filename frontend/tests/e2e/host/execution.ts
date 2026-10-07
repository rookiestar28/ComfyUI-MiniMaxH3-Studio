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

export async function watchHostExecution(page: Page): Promise<void> {
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { api: { api: any } };
      __h3Sample?: {
        events: Array<{
          type: string;
          promptId: string;
          output?: unknown;
          error?: { node_type: unknown; exception_type: unknown };
        }>;
        promptIds: string[];
      };
    };
    runtime.__h3Sample = { events: [], promptIds: [] };
    const api = runtime.comfyAPI.api.api;
    const original = api.queuePrompt.bind(api);
    api.queuePrompt = async function (...args: unknown[]) {
      const result = await original(...args);
      const promptId = (result as { prompt_id?: unknown } | undefined)
        ?.prompt_id;
      if (typeof promptId === "string" && promptId.length > 0)
        runtime.__h3Sample?.promptIds.push(promptId);
      return result;
    };
    const promptIdOf = (event: Event): string => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      return typeof detail?.prompt_id === "string" ? detail.prompt_id : "";
    };
    api.addEventListener("execution_start", (event: Event) =>
      runtime.__h3Sample?.events.push({
        type: "start",
        promptId: promptIdOf(event),
      }),
    );
    api.addEventListener("executed", (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      if (detail?.output !== undefined && detail?.output !== null)
        runtime.__h3Sample?.events.push({
          type: "output",
          promptId: promptIdOf(event),
          output: detail.output,
        });
    });
    api.addEventListener("execution_error", (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      runtime.__h3Sample?.events.push({
        type: "finished",
        promptId: promptIdOf(event),
        error: {
          node_type: detail?.node_type,
          exception_type: detail?.exception_type,
        },
      });
    });
    api.addEventListener("execution_success", (event: Event) =>
      runtime.__h3Sample?.events.push({
        type: "finished",
        promptId: promptIdOf(event),
      }),
    );
  });
}

export type SampleProgress = {
  submitted: number;
  started: number;
  finished: number;
  outputs: unknown[];
  errors: unknown[];
};

/**
 * This run's events, scoped to the submissions made since `fromSubmission`.
 *
 * Each revision has to be measured on its own: a second revision that wrote
 * nothing would otherwise be credited with the first revision's artifact, which
 * is exactly the claim AC-M17-20-04 exists to make impossible.
 */
export async function sampleProgress(
  page: Page,
  fromSubmission = 0,
): Promise<SampleProgress> {
  return (await page.evaluate((from) => {
    const sample = (
      window as unknown as {
        __h3Sample?: {
          events: Array<{
            type: string;
            promptId: string;
            output?: unknown;
            error?: unknown;
          }>;
          promptIds: string[];
        };
      }
    ).__h3Sample;
    const all = sample?.promptIds ?? [];
    const mine = new Set(all.slice(from));
    const events = (sample?.events ?? []).filter((event) =>
      mine.has(event.promptId),
    );
    return {
      submitted: all.length,
      started: events.filter((event) => event.type === "start").length,
      finished: events.filter((event) => event.type === "finished").length,
      outputs: events
        .filter((event) => event.type === "output")
        .map((event) => event.output),
      errors: events
        .filter((event) => event.error !== undefined)
        .map((event) => event.error),
    };
  }, fromSubmission)) as SampleProgress;
}

export async function awaitHostExecution(
  page: Page,
  fromSubmission: number,
  timeoutMs: number,
): Promise<{ outputs: unknown[]; errors: unknown[] }> {
  await expect
    .poll(async () => (await sampleProgress(page, fromSubmission)).finished, {
      timeout: timeoutMs,
      intervals: [1_000, 2_000, 5_000],
    })
    .toBeGreaterThan(0);
  const progress = await sampleProgress(page, fromSubmission);
  return { outputs: progress.outputs, errors: progress.errors };
}

/** Every filename the host reported writing, whatever shape the output took. */
export function reportedArtifactNames(outputs: unknown[]): string[] {
  const names: string[] = [];
  const walk = (value: unknown): void => {
    if (Array.isArray(value)) {
      for (const entry of value) walk(entry);
      return;
    }
    if (value === null || typeof value !== "object") return;
    const record = value as Record<string, unknown>;
    const filename = record.filename;
    const subfolder = record.subfolder;
    if (typeof filename === "string")
      names.push(
        typeof subfolder === "string" && subfolder.length > 0
          ? `${subfolder}/${filename}`
          : filename,
      );
    for (const entry of Object.values(record)) walk(entry);
  };
  walk(outputs);
  return names;
}

export type RealI2vaArtifactMetadata = Readonly<{
  width: number;
  height: number;
  frames: number;
  fps: number;
  format: string;
  codec: string;
  size: number;
  modifiedMilliseconds: number;
  digest: string;
}>;

export type RealI2vaEnvironment = Readonly<{
  hostRoot: string;
  hostPython: string;
}>;

export async function preflightRealI2vaEnvironment(
  hostRoot: string,
  hostPython: string,
): Promise<RealI2vaEnvironment> {
  try {
    if (!isAbsolute(hostRoot) || !isAbsolute(hostPython)) throw new Error();
    const [canonicalHostRoot, canonicalHostPython] = await Promise.all([
      realpath(hostRoot),
      realpath(hostPython),
    ]);
    const canonicalOutputRoot = await realpath(
      resolve(canonicalHostRoot, "output"),
    );
    const relativeOutput = relative(canonicalHostRoot, canonicalOutputRoot);
    const [host, output, python] = await Promise.all([
      stat(canonicalHostRoot),
      stat(canonicalOutputRoot),
      stat(canonicalHostPython),
    ]);
    if (
      !host.isDirectory() ||
      !output.isDirectory() ||
      !python.isFile() ||
      relativeOutput.length === 0 ||
      relativeOutput === ".." ||
      relativeOutput.startsWith(`..${sep}`) ||
      isAbsolute(relativeOutput)
    )
      throw new Error();
    execFileSync(canonicalHostPython, ["-c", "import av"], {
      stdio: ["ignore", "ignore", "ignore"],
      timeout: 30_000,
      windowsHide: true,
    });
    return { hostRoot: canonicalHostRoot, hostPython: canonicalHostPython };
  } catch {
    // IMPORTANT: never attach the underlying filesystem/process error; it may
    // contain the supplied host, Python, or private artifact path.
    throw new Error("the live I2VA environment preflight failed safely");
  }
}

export async function inspectRealI2vaArtifactUnsafe(
  hostRoot: string,
  hostPython: string,
  reportedName: string,
): Promise<RealI2vaArtifactMetadata> {
  if (!isAbsolute(hostRoot) || !isAbsolute(hostPython))
    throw new Error(
      "the live artifact inspector requires explicit absolute host paths",
    );
  const normalized = reportedName.replaceAll("\\", "/");
  const components = normalized.split("/");
  if (
    normalized.length === 0 ||
    normalized.length > 512 ||
    components.length > 16 ||
    components.some(
      (component) =>
        component.length === 0 ||
        component === "." ||
        component === ".." ||
        component.length > 192 ||
        component.includes(":") ||
        /[\u0000-\u001f]/.test(component),
    )
  )
    throw new Error("the host reported an unsafe artifact locator");
  if (!normalized.toLowerCase().endsWith(".mp4"))
    throw new Error("the host artifact is not an MP4 locator");
  const outputRoot = resolve(hostRoot, "output");
  const artifactPath = resolve(outputRoot, ...components);
  const relativeArtifact = relative(outputRoot, artifactPath);
  if (
    relativeArtifact.length === 0 ||
    relativeArtifact === ".." ||
    relativeArtifact.startsWith(`..${sep}`) ||
    isAbsolute(relativeArtifact)
  )
    throw new Error("the host artifact escaped the explicit output root");
  const [canonicalOutputRoot, canonicalArtifactPath, canonicalHostPython] =
    await Promise.all([
      realpath(outputRoot),
      realpath(artifactPath),
      realpath(hostPython),
    ]);
  const canonicalRelativeArtifact = relative(
    canonicalOutputRoot,
    canonicalArtifactPath,
  );
  if (
    canonicalRelativeArtifact.length === 0 ||
    canonicalRelativeArtifact === ".." ||
    canonicalRelativeArtifact.startsWith(`..${sep}`) ||
    isAbsolute(canonicalRelativeArtifact)
  )
    throw new Error(
      "the host artifact resolves outside the explicit output root",
    );
  const [file, pythonFile] = await Promise.all([
    stat(canonicalArtifactPath),
    stat(canonicalHostPython),
  ]);
  if (!file.isFile() || file.size <= 0 || file.size > 1024 * 1024 * 1024)
    throw new Error("the host artifact is not a bounded nonempty regular file");
  if (!pythonFile.isFile())
    throw new Error(
      "the explicitly supplied host Python is not a regular file",
    );

  const metadataScript = [
    "import av,json,sys",
    "container=av.open(sys.argv[1])",
    "stream=next((item for item in container.streams if item.type=='video'),None)",
    "assert stream is not None",
    "frames=int(stream.frames or 0)",
    "if frames <= 0:",
    "    frames=sum(1 for _ in container.decode(video=0))",
    "rate=stream.average_rate or stream.base_rate or 0",
    "print(json.dumps({'width':int(stream.width),'height':int(stream.height),'frames':frames,'fps':float(rate),'format':str(container.format.name or ''),'codec':str(stream.codec_context.name or '')},separators=(',',':')))",
    "container.close()",
  ].join("\n");
  const inspected = JSON.parse(
    execFileSync(
      canonicalHostPython,
      ["-c", metadataScript, canonicalArtifactPath],
      {
        encoding: "utf8",
        maxBuffer: 64 * 1024,
        stdio: ["ignore", "pipe", "ignore"],
        timeout: 120_000,
        windowsHide: true,
      },
    ).trim(),
  ) as Omit<
    RealI2vaArtifactMetadata,
    "size" | "modifiedMilliseconds" | "digest"
  >;
  const digest = await new Promise<string>((resolveDigest, rejectDigest) => {
    const hash = createHash("sha256");
    const stream = createReadStream(canonicalArtifactPath);
    stream.on("error", rejectDigest);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolveDigest(hash.digest("hex")));
  });
  return {
    ...inspected,
    size: file.size,
    modifiedMilliseconds: file.mtimeMs,
    digest,
  };
}

export async function inspectRealI2vaArtifact(
  hostRoot: string,
  hostPython: string,
  reportedName: string,
): Promise<RealI2vaArtifactMetadata> {
  try {
    return await inspectRealI2vaArtifactUnsafe(
      hostRoot,
      hostPython,
      reportedName,
    );
  } catch {
    // IMPORTANT: keep the artifact locator, canonical paths, child stderr, and
    // digest out of Playwright output even when the supplied host fails.
    throw new Error("the live I2VA artifact could not be verified safely");
  }
}
// M23-26 registration phase Six.
// prettier-ignore
import type { HostOfficialAssetManifest, CandidateInjectionState, LayoutState, AppModePhase, LayoutGridKind, LayoutVariant, LayoutPlacement, LayoutWidthMode, LayoutLocale, LayoutTheme, LayoutLifecycle, HostPersistenceSnapshot, LayoutCaptureEvidence, LayoutEvidenceJoin, LayoutEvidenceRow, LayoutCell, LayoutStateContract, LayoutRect, LayoutOwnerStyle, LayoutControlGeometry, LayoutNavigationTab, LayoutHeaderGeometry, LayoutFocusTargetKind, LayoutMeasurement, SerializedGraph, ManagedRouteReceipt, HostAssetResolutionReceipt, FrontendPerformanceReceipt } from "./environment";
// prettier-ignore
import type { runRegistrationPhaseFive } from "./assets";
export async function runRegistrationPhaseSix(
  state: Awaited<ReturnType<typeof runRegistrationPhaseFive>>,
) {
  // prettier-ignore
  const { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity } = state.shared;
  // prettier-ignore
  const { context, page, testInfo, allowedOrigin, networkAttribution, candidateNetworkAttribution, captureInteractionNetworkPhase, interactionErrors, initialInjectionCount, initialStorageKeys, registryEvidence, coInstallDefinitions, workflowTemplateDialog, appModeContainer, shellStatus, awaitAppModePhase, visualDirectory, snapshotHostStorage, snapshotHostPersistence, seedHostPersistenceSentinel, originalHostPersistence, initialHostStorage, hostPersistenceBefore, storageSentinelDigest, hostPersistenceBeforeDigest, snapshotHostOwner, layoutEvidence, captureManifest, visualPhase, candidateIdentity, hostReportPath, captureSettledScreenshot, applyLayoutCell, captureLayoutMatrix, projectedWorkspace, resetLayoutMatrixPresentation, networkAfterLayoutFixturePrep, cancelAction, preCancellationGraph, cancellationReason, cancellationReceipt, appModeQueueRequests, appModeNetworkShapes, appModeResponseShapes, appModeExecutedShapes, layoutTaskMode, layoutDuration, appModeEntry, appModePromptTypes, appModeGraph, metadata, githubLink, metadataStyles, networkAfterAppModeStart } = state;
  await page.evaluate(() => {
    const api = (
      window as unknown as {
        comfyAPI: { api: { api: EventTarget } };
        __h3Executed?: unknown[];
        __h3ExecutionTerminal?: unknown[];
      }
    ).comfyAPI.api.api;
    (window as unknown as { __h3Executed: unknown[] }).__h3Executed = [];
    (
      window as unknown as { __h3ExecutionTerminal: unknown[] }
    ).__h3ExecutionTerminal = [];
    api.addEventListener("executed", (event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      const output = detail?.output as
        Record<string, unknown[]> | null | undefined;
      (window as unknown as { __h3Executed: unknown[] }).__h3Executed.push({
        detailKeys: Object.keys(detail ?? {}).sort(),
        displayNode: detail?.display_node,
        node: detail?.node,
        promptId: detail?.prompt_id,
        promptFingerprint: output?.prompt_fingerprint?.[0],
        reportFingerprint: output?.report_fingerprint?.[0],
        correlation: output?.correlation?.[0],
        workspace: output?.sidebar_workspace?.[0],
        outputKeys:
          detail?.output !== null && typeof detail?.output === "object"
            ? Object.keys(detail.output).sort()
            : [],
      });
    });
    for (const type of ["execution_success", "execution_error"] as const) {
      api.addEventListener(type, (event) => {
        const detail = (event as CustomEvent<Record<string, unknown>>).detail;
        (
          window as unknown as { __h3ExecutionTerminal: unknown[] }
        ).__h3ExecutionTerminal.push({
          type,
          promptId: detail?.prompt_id,
          node: detail?.node_id,
          nodeType: detail?.node_type,
          exceptionType: detail?.exception_type,
        });
      });
    }
  });

  const fixture = JSON.parse(
    await readFile(
      resolve(process.cwd(), "../workflows/m15_03_product_shell_base.json"),
      "utf8",
    ),
  ) as { prompt: Record<string, unknown> };
  const prompt = structuredClone(fixture.prompt);
  const executionPrompt = structuredClone(prompt);
  // Keep the complete graph visible so the sidebar can authenticate the
  // ProductShell anchor. Unrelated terminal outputs are omitted only from this
  // bounded execution payload so completion is owned by the node under test.
  delete executionPrompt["7"];
  delete executionPrompt["8"];
  await page.evaluate(async (promptValue) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    app.loadApiJson(promptValue, "h3-product-shell-direct-e2e");
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
  }, prompt);
  const networkAfterDirectGraphLoad = captureInteractionNetworkPhase(
    "after_direct_graph_load",
  );
  const response = await page.evaluate(
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
      promptValue: executionPrompt,
    },
  );
  expect(response.status, response.text).toBe(200);
  const responseBody = JSON.parse(response.text) as { prompt_id?: unknown };
  expect(typeof responseBody.prompt_id, response.text).toBe("string");
  const promptId = responseBody.prompt_id as string;
  await page.waitForFunction((expectedPromptId) => {
    const runtime = window as unknown as {
      __h3Executed?: Array<{ node?: unknown; promptId?: unknown }>;
      __h3ExecutionTerminal?: Array<{ promptId?: unknown }>;
    };
    return (
      (runtime.__h3Executed ?? []).some(
        (event) => event.node === "6" && event.promptId === expectedPromptId,
      ) ||
      (runtime.__h3ExecutionTerminal ?? []).some(
        (event) => event.promptId === expectedPromptId,
      )
    );
  }, promptId);
  const executionReceipt = await page.evaluate((expectedPromptId) => {
    const runtime = window as unknown as {
      __h3Executed?: Array<{ node?: unknown; promptId?: unknown }>;
      __h3ExecutionTerminal?: Array<Record<string, unknown>>;
    };
    return {
      targetExecuted: (runtime.__h3Executed ?? []).some(
        (event) => event.node === "6" && event.promptId === expectedPromptId,
      ),
      terminal: (runtime.__h3ExecutionTerminal ?? []).filter(
        (event) => event.promptId === expectedPromptId,
      ),
    };
  }, promptId);
  expect(
    executionReceipt.targetExecuted,
    JSON.stringify(executionReceipt),
  ).toBe(true);
  const projectionInventory = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    return {
      executed: (window as unknown as { __h3Executed?: unknown[] })
        .__h3Executed,
      status: document.querySelector(
        '#h3-context-e2e-container .h3-context-status[role="status"]',
      )?.textContent,
      text: document.querySelector("#h3-context-e2e-container")?.textContent,
    };
  });
  expect(
    projectionInventory.status,
    JSON.stringify({ projectionInventory, response }),
  ).toBe("ready");
  projectedWorkspace.id = await page.evaluate(() => {
    const executed = (
      window as unknown as {
        __h3Executed?: Array<{ node?: unknown; workspace?: unknown }>;
      }
    ).__h3Executed;
    const workspace = executed?.find((event) => event.node === "6")?.workspace;
    const workspaceId =
      workspace !== null && typeof workspace === "object"
        ? (workspace as { workspace_id?: unknown }).workspace_id
        : undefined;
    if (typeof workspaceId !== "string" || !/^ws_/.test(workspaceId))
      throw new Error("projected workspace identity is absent");
    return workspaceId;
  });
  const graphBeforeSetupEdit = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    return JSON.stringify(app.graph.serialize());
  });
  const executedBeforeSetupEdit = await page.evaluate(
    () =>
      (window as unknown as { __h3Executed?: unknown[] }).__h3Executed
        ?.length ?? 0,
  );
  const networkBeforeSetupEdit = captureInteractionNetworkPhase(
    "before_m17_19_setup_edit",
  );
  await expect(
    appModeContainer.getByRole("button", { name: "Edit App Mode setup" }),
  ).toBeVisible();
  await appModeContainer
    .getByRole("button", { name: "Edit App Mode setup" })
    .click();
  await expect(
    appModeContainer.locator('[data-shell-status="editing_setup"]'),
  ).toHaveCount(1);
  await expect(
    appModeContainer.getByRole("textbox", { name: "Intent" }),
  ).toBeVisible();
  await expect(
    appModeContainer.locator('[data-h3-focus-key="app-stage-intent"]'),
  ).toBeFocused();
  expect(
    await page.evaluate(() => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      return JSON.stringify(app.graph.serialize());
    }),
  ).toBe(graphBeforeSetupEdit);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __h3Executed?: unknown[] }).__h3Executed
          ?.length ?? 0,
    ),
  ).toBe(executedBeforeSetupEdit);
  const networkAfterSetupEdit = captureInteractionNetworkPhase(
    "after_m17_19_setup_edit",
  );
  expect(networkAfterSetupEdit).toMatchObject({
    remoteCount: networkBeforeSetupEdit.remoteCount,
    providerCount: networkBeforeSetupEdit.providerCount,
  });
  await appModeContainer.getByRole("button", { name: "Cancel edit" }).click();
  await expect(
    appModeContainer.locator('[data-shell-status="projected"]'),
  ).toHaveCount(1);
  await expect(
    appModeContainer.locator('[data-h3-focus-key="edit-app-mode-setup"]'),
  ).toBeFocused();
  expect(
    await page.evaluate(() => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      return JSON.stringify(app.graph.serialize());
    }),
  ).toBe(graphBeforeSetupEdit);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __h3Executed?: unknown[] }).__h3Executed
          ?.length ?? 0,
    ),
  ).toBe(executedBeforeSetupEdit);
  await expect(page.getByText("MANUAL_ONLY_SCOPED")).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(5);
  await page.getByRole("tab", { name: "Intent / Mode" }).click();
  await expect(page.getByText("available")).toBeVisible();
  await expect(page.getByText("t2va, i2va, fl2va, l2va, ref2va")).toBeVisible();
  await expect(page.getByText("STRING")).toBeVisible();
  await expect(page.getByText("4–15")).toBeVisible();
  await expect(page.getByText("within_limit")).toBeVisible();
  await page.getByRole("tab", { name: "Understand / Plan" }).click();
  await expect(page.getByText("124")).toBeVisible();
  await expect(page.getByText("Effective frame count")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Validate revision" }),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: "Export JSON" })).toBeEnabled();
  const networkAfterDirectPromptProjection = captureInteractionNetworkPhase(
    "after_direct_prompt_projection",
  );

  await captureLayoutMatrix("projected");
  await resetLayoutMatrixPresentation("preserve-workspace");
  const networkAfterProjectedLayoutMatrix = captureInteractionNetworkPhase(
    "after_projected_layout_matrix",
  );

  const initialWorkspace = await page.evaluate(() => {
    const executed = (
      window as unknown as {
        __h3Executed?: Array<{ node?: unknown; workspace?: unknown }>;
      }
    ).__h3Executed;
    return executed?.find((event) => event.node === "6")?.workspace as {
      workspace_id: string;
      report_revision: number;
      report_fingerprint: string;
      prompt_text: string;
    };
  });
  expect(initialWorkspace.workspace_id).toMatch(/^ws_/);
  await page.getByRole("tab", { name: "Audit / Validate" }).click();
  const promptEditor = page.getByRole("textbox", { name: "Prompt revision" });
  await promptEditor.fill(`${initialWorkspace.prompt_text}\nCamera: Slow arc.`);
  await page.getByLabel("Revision reason").fill("Browser E2E revision");
  const initialStageResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/h3-context/v1/sidebar/action"),
  );
  await page.getByRole("button", { name: "Stage revision" }).click();
  const initialStageResult = await initialStageResponse;
  const initialStageBody = await initialStageResult.text();
  expect(initialStageResult.status(), initialStageBody).toBe(200);
  const initialStageProjection = JSON.parse(initialStageBody);
  expect(initialStageProjection).toMatchObject({
    schema: "h3.context.sidebar.workspace.v2",
    lifecycle: "stale",
    validation_status: "not_run",
  });
  expect(() =>
    decodeSidebarWorkspaceProjection(initialStageProjection),
  ).not.toThrow();
  await page.getByRole("tab", { name: "Understand / Plan" }).click();
  await expect(
    page.getByRole("list", { name: "Creative addition values" }),
  ).toContainText("Browser E2E revision");
  await page.getByRole("tab", { name: "Execute / Export" }).click();
  await expect(
    page.getByRole("list", { name: "Proposal diff lines" }),
  ).toContainText("Camera: Slow arc.");
  await expect(
    page.getByText("Validation or media receipt is required before export"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Validate revision" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Export JSON" }),
  ).toBeDisabled();

  const staleResponse = await page.evaluate(async (workspace) => {
    const result = await fetch("/h3-context/v1/sidebar/action", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        schema: "h3.context.sidebar.action.v2",
        workspace_id: workspace.workspace_id,
        expected_revision: workspace.report_revision,
        expected_report_fingerprint: workspace.report_fingerprint,
        action: "validate",
        payload: {},
      }),
    });
    return { status: result.status, body: await result.json() };
  }, initialWorkspace);
  expect(staleResponse).toEqual({
    status: 409,
    body: { error: "stale_action" },
  });

  await page
    .getByRole("button", { name: "Validate revision" })
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(page.getByText("Current revision is ready")).toBeVisible();
  await expect(page.getByRole("button", { name: "Export JSON" })).toBeEnabled();

  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export JSON" }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("h3-context-prompt.json");
  const downloadPath = await download.path();
  if (downloadPath === null) throw new Error("export download path is absent");
  const transferText = await readFile(downloadPath, "utf8");
  const transfer = JSON.parse(transferText) as Record<string, unknown>;
  expect(transfer.schema).toBe("h3.context.sidebar.transfer.v1");
  expect(Object.keys(transfer).sort()).toEqual([
    "profile",
    "prompt_fingerprint",
    "prompt_text",
    "report_fingerprint",
    "report_id",
    "report_revision",
    "schema",
    "task_mode",
  ]);

  await page
    .locator('#h3-context-e2e-container input[type="file"]')
    .setInputFiles({
      name: "h3-context-prompt.json",
      mimeType: "application/json",
      buffer: Buffer.from(transferText, "utf8"),
    });
  await expect(
    page.getByText("Validation or media receipt is required before export"),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Validate revision" })
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(page.getByText("Current revision is ready")).toBeVisible();
  const networkAfterWorkspaceActions = captureInteractionNetworkPhase(
    "after_workspace_actions",
  );

  // prettier-ignore
  return { ...state, fixture, prompt, executionPrompt, networkAfterDirectGraphLoad, response, responseBody, promptId, executionReceipt, projectionInventory, graphBeforeSetupEdit, executedBeforeSetupEdit, networkBeforeSetupEdit, networkAfterSetupEdit, networkAfterDirectPromptProjection, networkAfterProjectedLayoutMatrix, initialWorkspace, promptEditor, initialStageResponse, initialStageResult, initialStageBody, initialStageProjection, staleResponse, downloadEvent, download, downloadPath, transferText, transfer, networkAfterWorkspaceActions };
}
