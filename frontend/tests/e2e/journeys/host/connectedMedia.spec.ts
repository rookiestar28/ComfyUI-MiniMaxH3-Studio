import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { createReadStream } from "node:fs";
import { mkdir, readFile, realpath, stat, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

// prettier-ignore
import { expect, test, type BrowserContext, type Locator, type Page, } from "../../host/fixture";

// prettier-ignore
import { decodeGenerationProfile, familyProfileForTaskMode, } from "../../../../src/contracts/generationProfileCodec";
// prettier-ignore
import { decodeProviderIntentResult, PROVIDER_SETTINGS_SCHEMA, PROVIDER_SETTINGS_REQUEST_SCHEMA, type ProviderSettingsProjection, } from "../../../../src/contracts/providerSettingsCodec";
import { decodeSidebarWorkspaceProjection } from "../../../../src/contracts/sidebarWorkspaceCodec";
import { APP_MODE_ARTIFACT_PREFIX_ROOT } from "../../../../src/host/appMode";
// prettier-ignore
import { INPUT_GEOMETRY_RECEIPT_SCHEMA, INPUT_GEOMETRY_ROUTE, } from "../../../../src/host/inputGeometry";
// prettier-ignore
import { MAX_MEDIA_PREVIEW_BYTES, MEDIA_PREVIEW_REQUEST_SCHEMA, MEDIA_PREVIEW_ROUTE, } from "../../../../src/host/productionMediaPreview";
// prettier-ignore
import { readOfficialAssetInventory, resolveOfficialAssets, } from "../../../../src/host/officialAssetResolution";
import { projectionFromOutput } from "../../../../src/host/sidebarHost";
// prettier-ignore
import { I2VA_SCALE_NODE_TYPE, I2VA_SIZE_NODE_TYPE, OFFICIAL_LENGTH_EXPRESSION, } from "../../../../src/host/templateMaterialization";
// prettier-ignore
import { CANDIDATE_BACKEND_HOST_ROOT_ENV, CANDIDATE_BUNDLE_PATH_ENV, CANDIDATE_BUNDLE_SHA256_ENV, CandidateInitiatorNetworkAttribution, H3NetworkAttribution, loadCandidateBundleEnvironment, verifyCandidateBackendRuntimeEnvironment, waitForStartupNetworkQuiet, } from "../../helpers/candidateBundleHarness";
// prettier-ignore
import { diffGraphSurroundings, type SurroundingsDiffReport, } from "../../../support/surroundingsDiff";

// prettier-ignore
import { hostUrl, m23I2vaInputLocator, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, type HostOfficialAssetManifest, type CandidateInjectionState, type FrontendPerformanceReceipt, type LayoutState, type AppModePhase, type LayoutGridKind, type LayoutVariant, type LayoutPlacement, type LayoutWidthMode, type LayoutLocale, type LayoutTheme, type LayoutLifecycle, type HostPersistenceSnapshot, type LayoutCaptureEvidence, type LayoutEvidenceJoin, type LayoutEvidenceRow, type LayoutCell, type LayoutStateContract, type LayoutRect, type LayoutOwnerStyle, type LayoutControlGeometry, type LayoutNavigationTab, type LayoutHeaderGeometry, type LayoutFocusTargetKind, type LayoutMeasurement, type SerializedGraph, type ManagedRouteReceipt, type HostAssetResolutionReceipt, type SampleProgress, type RealI2vaArtifactMetadata, type RealI2vaEnvironment, } from "../../host/environment";

test("M17-30 supported host connects graph-owned media and intercepts exactly one queue", async ({
  context,
  page,
}) => {
  test.setTimeout(120_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const initialInjectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  await instrumentHostGraphLoads(page);
  await waitForHostGraphSettled(page);

  const prepared = await page.evaluate(async () => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
    };
    const app = runtime.comfyAPI.app.app;
    const response = await fetch("/templates/video_minimax_h3_i2v.json", {
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`I2VA template ${response.status}`);
    const template = await response.json();
    // The template's loader identity is public package data. It is compiled but
    // never executed or returned as evidence, so no media bytes are read.
    await app.loadGraphData(template);
    // This setup is the user's graph authority, not a route-side rewrite. The
    // official template promotes its duration primitive as `value_1` on the
    // visible subgraph instance, so set that visible widget to the sidebar's
    // backend-derived default. The route must then preserve it unchanged.
    const durationWidget = (app.graph?._nodes ?? [])
      .flatMap((node: { widgets?: unknown[] }) => node.widgets ?? [])
      .find((widget: { name?: unknown }) => widget.name === "value_1") as
      | {
          value?: unknown;
          callback?: (value: number) => void;
        }
      | undefined;
    if (durationWidget === undefined)
      throw new Error("I2VA visible duration widget is unavailable");
    durationWidget.value = 5.167;
    durationWidget.callback?.(5.167);
    app.graph.change?.();
    await new Promise((settle) => setTimeout(settle, 0));
    const serialized = app.graph.serialize();
    const nodes = serialized?.nodes ?? [];
    const innerNodes = (serialized?.definitions?.subgraphs ?? []).flatMap(
      (definition: { nodes?: unknown[] }) => definition.nodes ?? [],
    );
    const allNodes = [...nodes, ...innerNodes];
    const durationSeconds = durationWidget.value;
    if (
      typeof durationSeconds !== "number" ||
      !Number.isFinite(durationSeconds)
    )
      throw new Error("I2VA template duration authority is unavailable");
    return {
      nativeAnchors: allNodes.filter(
        (node: { type?: unknown }) => node.type === "MiniMaxH3ImageToVideo",
      ).length,
      imageSources: allNodes.filter(
        (node: { type?: unknown }) => node.type === "LoadImage",
      ).length,
      contextNodes: allNodes.filter((node: { type?: unknown }) =>
        String(node.type ?? "").startsWith("comfyui_h3_context."),
      ).length,
      durationSeconds,
    };
  });
  expect(prepared.nativeAnchors).toBe(1);
  expect(prepared.imageSources).toBeGreaterThanOrEqual(1);
  expect(prepared.contextNodes).toBe(0);

  const container = await openH3AppModeTab(page, "h3-context-m17-30-container");
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await container.getByLabel("Task mode").selectOption("i2va");
  await expect(container.getByLabel("First frame source")).toHaveValue("");
  expect(prepared.durationSeconds).toBe(5.167);

  const connectSurface = await container.evaluate((element) => {
    const shell = element.querySelector<HTMLElement>("[data-shell-status]");
    const buttons = [...element.querySelectorAll("button")].map(
      (button) => button.textContent?.trim() ?? "",
    );
    return {
      status: shell?.dataset.shellStatus ?? null,
      reason: shell?.dataset.shellReason ?? null,
      connectButtonCount: buttons.filter((text) =>
        text.includes("Connect and queue"),
      ).length,
      queueButtonCount: buttons.filter((text) =>
        text.includes("Queue current H3 graph"),
      ).length,
      replaceButtonCount: buttons.filter((text) =>
        text.includes("Replace canvas"),
      ).length,
      connectDisabled:
        element.querySelector<HTMLButtonElement>(
          '[data-h3-focus-key="app-connect"]',
        )?.disabled ?? null,
      blocker:
        element.querySelector<HTMLElement>("#h3-app-mode-connect-blocker")
          ?.textContent ?? null,
    };
  });
  expect(
    connectSurface.connectButtonCount,
    JSON.stringify(connectSurface),
  ).toBe(1);
  const connect = container.getByRole("button", {
    name: "Connect and queue current canvas",
  });
  expect(connectSurface.connectDisabled, JSON.stringify(connectSurface)).toBe(
    false,
  );
  await expect(connect).toHaveAttribute(
    "aria-describedby",
    "h3-app-mode-connect-boundary",
  );
  await expect(
    container.locator("#h3-app-mode-connect-boundary"),
  ).toContainText("selected H3 generation node");
  await expect(
    container.locator("#h3-app-mode-connect-boundary"),
  ).toContainText("queues the current canvas once");
  await expect(
    container.locator("#h3-app-mode-connect-boundary"),
  ).toContainText("matching frame-role declaration");

  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { api: { api: any } };
      __h3M1730Queue?: Array<{
        requestNodes: number;
        registryNodes: number;
        nativeAnchors: number;
        sinks: number;
      }>;
      __h3M1730Compile?: {
        calls: number;
        status: "pending" | "compiled" | "error";
        requestNodes: number;
        registryNodes: number;
        nativeAnchors: number;
        productShells: number;
        sinks: number;
        requestTaskMode: unknown;
        firstFrameLinked: boolean;
        registryFirstFrameLinked: boolean;
        registryMatchesAnchor: boolean;
        planRegistryLinked: boolean;
        lastFrameLinked: boolean;
        lengthKind: string;
        derivedDurationSeconds: unknown;
      };
      __h3M1730Restore?: () => void;
      __h3M1730SeamsRestored?: boolean;
    };
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const originalGraphToPrompt = app.graphToPrompt;
    const originalQueuePrompt = runtime.comfyAPI.api.api.queuePrompt;
    runtime.__h3M1730SeamsRestored = false;
    runtime.__h3M1730Restore = () => {
      app.graphToPrompt = originalGraphToPrompt;
      runtime.comfyAPI.api.api.queuePrompt = originalQueuePrompt;
      runtime.__h3M1730SeamsRestored = true;
    };
    runtime.__h3M1730Compile = {
      calls: 0,
      status: "pending",
      requestNodes: 0,
      registryNodes: 0,
      nativeAnchors: 0,
      productShells: 0,
      sinks: 0,
      requestTaskMode: null,
      firstFrameLinked: false,
      registryFirstFrameLinked: false,
      registryMatchesAnchor: false,
      planRegistryLinked: false,
      lastFrameLinked: false,
      lengthKind: "missing",
      derivedDurationSeconds: null,
    };
    app.graphToPrompt = async (...args: unknown[]) => {
      runtime.__h3M1730Compile!.calls += 1;
      try {
        const compiled = await originalGraphToPrompt.apply(app, args);
        const nodes = Object.values(compiled?.output ?? {}) as Array<{
          class_type?: unknown;
          inputs?: Record<string, unknown>;
        }>;
        const request = nodes.find(
          (node) => node.class_type === "comfyui_h3_context.H3Context.Request",
        );
        const anchor = nodes.find(
          (node) => node.class_type === "MiniMaxH3ImageToVideo",
        );
        const registry = nodes.find(
          (node) =>
            node.class_type ===
            "comfyui_h3_context.H3Context.ReferenceRegistry",
        );
        const plan = nodes.find(
          (node) => node.class_type === "comfyui_h3_context.H3Context.Plan",
        );
        const registryId = Object.entries(compiled?.output ?? {}).find(
          ([, node]) =>
            (node as { class_type?: unknown }).class_type ===
            "comfyui_h3_context.H3Context.ReferenceRegistry",
        )?.[0];
        const anchorFirst = anchor?.inputs?.first_frame;
        const registryFirst = registry?.inputs?.first_frame;
        const length = anchor?.inputs?.length;
        const byId = new Map(
          Object.entries(compiled?.output ?? {}).map(([id, node]) => [
            id,
            node,
          ]),
        );
        const math = Array.isArray(length)
          ? byId.get(String(length[0]))
          : undefined;
        const operand = (
          math as { inputs?: Record<string, unknown> } | undefined
        )?.inputs?.["values.a"];
        const primitive = Array.isArray(operand)
          ? byId.get(String(operand[0]))
          : undefined;
        Object.assign(runtime.__h3M1730Compile!, {
          status: "compiled",
          requestNodes: nodes.filter(
            (node) =>
              node.class_type === "comfyui_h3_context.H3Context.Request",
          ).length,
          registryNodes: nodes.filter(
            (node) =>
              node.class_type ===
              "comfyui_h3_context.H3Context.ReferenceRegistry",
          ).length,
          nativeAnchors: nodes.filter(
            (node) => node.class_type === "MiniMaxH3ImageToVideo",
          ).length,
          productShells: nodes.filter(
            (node) =>
              node.class_type === "comfyui_h3_context.H3Context.ProductShell",
          ).length,
          sinks: nodes.filter((node) => node.class_type === "SaveVideo").length,
          requestTaskMode: request?.inputs?.task_mode,
          firstFrameLinked: Array.isArray(anchorFirst),
          registryFirstFrameLinked: Array.isArray(registryFirst),
          registryMatchesAnchor:
            Array.isArray(anchorFirst) &&
            Array.isArray(registryFirst) &&
            String(anchorFirst[0]) === String(registryFirst[0]) &&
            anchorFirst[1] === registryFirst[1],
          planRegistryLinked:
            Array.isArray(plan?.inputs?.reference_registry) &&
            String(plan.inputs.reference_registry[0]) === registryId &&
            plan.inputs.reference_registry[1] === 0,
          lastFrameLinked: Array.isArray(anchor?.inputs?.last_frame),
          lengthKind: Array.isArray(length) ? "link" : typeof length,
          derivedDurationSeconds: (
            primitive as { inputs?: { value?: unknown } } | undefined
          )?.inputs?.value,
        });
        return compiled;
      } catch (error) {
        runtime.__h3M1730Compile!.status = "error";
        throw error;
      }
    };
    runtime.__h3M1730Queue = [];
    // CRITICAL: do not call through. This lane proves the host compile and the
    // product's irreversible-boundary call, not model execution.
    runtime.comfyAPI.api.api.queuePrompt = (
      _batch: unknown,
      compiled: {
        output?: Record<
          string,
          { class_type?: unknown; inputs?: Record<string, unknown> }
        >;
      },
    ) => {
      const nodes = Object.values(compiled.output ?? {});
      runtime.__h3M1730Queue?.push({
        requestNodes: nodes.filter(
          (node) => node.class_type === "comfyui_h3_context.H3Context.Request",
        ).length,
        registryNodes: nodes.filter(
          (node) =>
            node.class_type ===
            "comfyui_h3_context.H3Context.ReferenceRegistry",
        ).length,
        nativeAnchors: nodes.filter(
          (node) => node.class_type === "MiniMaxH3ImageToVideo",
        ).length,
        sinks: nodes.filter((node) => node.class_type === "SaveVideo").length,
      });
      return Promise.resolve({
        prompt_id: "00000000-0000-4000-8000-000000000030",
        number: -30,
        node_errors: {},
      });
    };
  });
  await resetHostGraphLoads(page);
  const outcome = await (async () => {
    try {
      await connect.click();
      await page.waitForTimeout(2_000);
      return await container.evaluate((element) => {
        const runtime = window as unknown as {
          __h3M1730Queue?: unknown[];
          __h3GraphLoads?: number;
          __h3M1730Compile?: unknown;
        };
        const shell = element.querySelector<HTMLElement>("[data-shell-status]");
        return {
          queueCount: runtime.__h3M1730Queue?.length ?? 0,
          graphLoads: runtime.__h3GraphLoads ?? 0,
          status: shell?.dataset.shellStatus ?? null,
          reason: shell?.dataset.shellReason ?? null,
          alert:
            element.querySelector<HTMLElement>('[role="alert"]')?.textContent,
          compile: runtime.__h3M1730Compile,
        };
      });
    } finally {
      await page.evaluate(() => {
        const runtime = window as unknown as {
          __h3M1730Restore?: () => void;
        };
        runtime.__h3M1730Restore?.();
        delete runtime.__h3M1730Restore;
      });
    }
  })();
  const seamsRestored = await page.evaluate(() => {
    const runtime = window as unknown as {
      __h3M1730SeamsRestored?: boolean;
    };
    const restored = runtime.__h3M1730SeamsRestored;
    delete runtime.__h3M1730SeamsRestored;
    return restored;
  });
  expect(seamsRestored).toBe(true);
  expect(outcome.queueCount, JSON.stringify(outcome)).toBe(1);
  expect(outcome.graphLoads).toBe(1);
  expect(outcome.compile).toEqual({
    calls: 1,
    status: "compiled",
    requestNodes: 1,
    registryNodes: 1,
    nativeAnchors: 1,
    productShells: 1,
    sinks: 1,
    requestTaskMode: "i2va",
    firstFrameLinked: true,
    registryFirstFrameLinked: true,
    registryMatchesAnchor: true,
    planRegistryLinked: true,
    lastFrameLinked: false,
    lengthKind: "link",
    derivedDurationSeconds: 5.167,
  });

  const receipt = await page.evaluate(
    () =>
      (
        window as unknown as {
          __h3M1730Queue?: Array<{
            requestNodes: number;
            registryNodes: number;
            nativeAnchors: number;
            sinks: number;
          }>;
        }
      ).__h3M1730Queue?.[0],
  );
  expect(receipt).toEqual({
    requestNodes: 1,
    registryNodes: 1,
    nativeAnchors: 1,
    sinks: 1,
  });
  const connected = await readVisibleGraph(page);
  expect(
    (connected.nodes ?? []).filter(
      (node) => node.type === "comfyui_h3_context.H3Context.Request",
    ),
  ).toHaveLength(1);
  expect(
    (connected.nodes ?? []).filter(
      (node) => node.type === "comfyui_h3_context.H3Context.ProductShell",
    ),
  ).toHaveLength(1);
  expect(
    (connected.nodes ?? []).filter(
      (node) => node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
    ),
  ).toHaveLength(1);
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);
});
