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

test("M23-29 exact restarted host keeps I2VA authoring topology but admits by source identity", async ({
  context,
  page,
}) => {
  test.setTimeout(120_000);
  test.skip(
    candidateBundle === null ||
      candidateBackendRuntime === null ||
      candidateBackendMode !== "exact",
    "M23-29 acceptance requires an exact frontend and backend candidate",
  );
  test.skip(
    typeof m23I2vaInputLocator !== "string" || m23I2vaInputLocator.length === 0,
    "M23-29 acceptance requires one explicitly supplied host input fixture",
  );
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const geometryResponses: Array<Record<string, unknown>> = [];
  const geometryRequests: Array<Record<string, unknown>> = [];
  const preparations: Array<Record<string, unknown>> = [];
  page.on("response", (response) => {
    if (!new URL(response.url()).pathname.endsWith(INPUT_GEOMETRY_ROUTE))
      return;
    void response
      .json()
      .then((value: unknown) => {
        const wire =
          value !== null && typeof value === "object" && !Array.isArray(value)
            ? (value as Record<string, unknown>)
            : {};
        geometryResponses.push({
          status: response.status(),
          keys: Object.keys(wire).sort(),
          schema: wire.schema,
          receiptHandleShape:
            typeof wire.receipt_handle === "string" &&
            /^ig_[A-Za-z0-9_-]{32,96}$/.test(wire.receipt_handle),
          sourceFingerprintShape:
            typeof wire.source_fingerprint === "string" &&
            /^sha256:[0-9a-f]{64}$/.test(wire.source_fingerprint),
        });
      })
      .catch(() => {
        geometryResponses.push({ status: response.status(), invalid: true });
      });
  });
  page.on("request", (request) => {
    const requestUrl = new URL(request.url());
    if (requestUrl.pathname.endsWith(INPUT_GEOMETRY_ROUTE))
      geometryRequests.push({
        method: request.method(),
        sameOrigin: requestUrl.origin === allowedOrigin,
        routeSuffix: true,
        // The sidebar posts through the host's own API base (`/api` on the
        // supplied frontend); the route itself is the repository's.
        exactPath:
          requestUrl.pathname === INPUT_GEOMETRY_ROUTE ||
          requestUrl.pathname === `/api${INPUT_GEOMETRY_ROUTE}`,
        body: JSON.parse(request.postData() ?? "null"),
      });
    if (!requestUrl.pathname.endsWith("/h3-context/v1/generation/coordinator"))
      return;
    try {
      const action = JSON.parse(request.postData() ?? "null") as {
        action?: unknown;
        payload?: { observation?: Record<string, unknown> };
      } | null;
      if (action?.action !== "prepare_managed_run") return;
      const observation = action.payload?.observation ?? {};
      const source =
        observation.source_identity !== null &&
        typeof observation.source_identity === "object" &&
        !Array.isArray(observation.source_identity)
          ? (observation.source_identity as Record<string, unknown>)
          : {};
      preparations.push({
        schema: observation.schema,
        expectedFrames: observation.expected_frames,
        sourceKeys: Object.keys(source).sort(),
        sourceFingerprint: source.source_fingerprint,
      });
    } catch {
      preparations.push({ invalid: true });
    }
  });

  const initialInjectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  await setSupportedH3Language(page, "en");
  await instrumentHostGraphLoads(page);
  // IMPORTANT: ComfyUI restores its startup workflow after extension setup. Let
  // that host-owned write settle before configuring the private acceptance graph,
  // or the restore can erase the designated source after the test has observed it.
  await waitForHostGraphSettled(page);
  const tablessStart = await page.evaluate(async () => {
    const store = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app.extensionManager?.workflow;
    const active = store?.activeWorkflow;
    const open = Array.isArray(store?.openWorkflows)
      ? store.openWorkflows
      : null;
    let startupShape = "already_tabless";
    if (active !== null || open?.length !== 0) {
      if (
        active === null ||
        typeof active !== "object" ||
        open === null ||
        open.length !== 1 ||
        open[0] !== active ||
        active.isTemporary !== true ||
        typeof store?.closeWorkflow !== "function"
      )
        return {
          startupShape: "unsupported",
          activeIsNull: false,
          openCount: -1,
        };
      // IMPORTANT: the supported host may restore one default temporary tab after
      // extension setup. Close only that exact public-store object so this row still
      // exercises the product's canonical null-to-one workflow bootstrap.
      await store.closeWorkflow(active);
      store.activeWorkflow = null;
      startupShape = "closed_single_temporary";
    }
    return {
      startupShape,
      activeIsNull: store?.activeWorkflow === null,
      openCount: Array.isArray(store?.openWorkflows)
        ? store.openWorkflows.length
        : -1,
    };
  });
  expect(["already_tabless", "closed_single_temporary"]).toContain(
    tablessStart.startupShape,
  );
  expect(tablessStart).toMatchObject({ activeIsNull: true, openCount: 0 });
  await resetHostGraphLoads(page);
  await page.evaluate((locator) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    // IMPORTANT: this setup is graph content only. Calling loadGraphData without
    // an explicit workflow would pre-create the identity this row must test.
    app.graph.configure({
      last_node_id: 1,
      last_link_id: 0,
      nodes: [
        {
          id: 1,
          type: "LoadImage",
          pos: [0, 0],
          size: [315, 314],
          flags: {},
          order: 0,
          mode: 0,
          inputs: [],
          outputs: [
            { name: "IMAGE", type: "IMAGE", links: [] },
            { name: "MASK", type: "MASK", links: [] },
          ],
          properties: { "Node name for S&R": "LoadImage" },
          widgets_values: [locator, "image"],
        },
      ],
      links: [],
      groups: [],
      config: {},
      extra: {},
      version: 0.4,
    });
    app.graph.setDirtyCanvas?.(true, true);
  }, m23I2vaInputLocator!);
  await waitForHostGraphSettled(page);
  const container = await openH3AppModeTab(page, "h3-context-m23-18");
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await page.evaluate(
    ({ scaleType, sizeType }) => {
      type CompiledNode = {
        class_type?: unknown;
        inputs?: Record<string, unknown>;
      };
      type CompiledPrompt = {
        output?: Record<string, CompiledNode>;
        workflow?: unknown;
      };
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any }; api: { api: any } };
        __h3M2318Compile?: Record<string, unknown>;
        __h3M2318QueuePhases?: string[];
        __h3M2318Restore?: () => void;
      };
      const app = runtime.comfyAPI.app.app;
      const api = runtime.comfyAPI.api.api;
      const rawGraphToPrompt = app.graphToPrompt;
      const originalGraphToPrompt = rawGraphToPrompt?.bind(app);
      const originalQueuePrompt = api.queuePrompt;
      if (typeof originalGraphToPrompt !== "function")
        throw new Error("the public host compile seam is unavailable");
      if (typeof originalQueuePrompt !== "function")
        throw new Error("the public host queue seam is unavailable");
      runtime.__h3M2318QueuePhases = [];
      runtime.__h3M2318Restore = () => {
        app.graphToPrompt = rawGraphToPrompt;
        api.queuePrompt = originalQueuePrompt;
      };
      app.graphToPrompt = async (...args: unknown[]) => {
        const compiled = (await originalGraphToPrompt(
          ...args,
        )) as CompiledPrompt;
        const byId = new Map(Object.entries(compiled.output ?? {}));
        const generation = Object.values(compiled.output ?? {}).find(
          (node) => node.class_type === "MiniMaxH3ImageToVideo",
        );
        const link = (
          value: unknown,
          slot: number,
        ): CompiledNode | undefined =>
          Array.isArray(value) &&
          value.length === 2 &&
          value[1] === slot &&
          (typeof value[0] === "string" || typeof value[0] === "number")
            ? byId.get(String(value[0]))
            : undefined;
        if (generation !== undefined) {
          const widthSize = link(generation.inputs?.width, 0);
          const heightSize = link(generation.inputs?.height, 1);
          const scale = link(widthSize?.inputs?.image, 0);
          const scaleSource = link(scale?.inputs?.image, 0);
          const firstFrameSource = link(generation.inputs?.first_frame, 0);
          runtime.__h3M2318Compile = {
            correctedSourceSizeTopology:
              widthSize !== undefined &&
              widthSize === heightSize &&
              widthSize.class_type === sizeType &&
              scale?.class_type === scaleType &&
              scaleSource?.class_type === "LoadImage" &&
              scaleSource === firstFrameSource,
            selectorStillAuthoritative:
              widthSize?.class_type === "ResolutionSelector",
            upscaleMethod: scale?.inputs?.upscale_method,
            megapixels: scale?.inputs?.megapixels,
            resolutionSteps: scale?.inputs?.resolution_steps,
          };
        }
        return compiled;
      };
      api.queuePrompt = async function (
        batch: unknown,
        compiled: CompiledPrompt,
      ) {
        const output = compiled.output ?? {};
        const types = Object.values(output).map((node) =>
          String(node.class_type ?? ""),
        );
        const productShellEntry = Object.entries(output).find(
          ([, node]) =>
            node.class_type === "comfyui_h3_context.H3Context.ProductShell",
        );
        const hasNative = types.includes("MiniMaxH3ImageToVideo");
        const hasSink = types.includes("SaveVideo");
        if (productShellEntry === undefined || !hasNative || !hasSink)
          throw new Error("the M23-18 single managed prompt is not qualified");

        const included = new Set<string>();
        const pending = [productShellEntry[0]];
        while (pending.length > 0) {
          const nodeId = pending.pop()!;
          if (included.has(nodeId)) continue;
          const node = output[nodeId];
          if (node === undefined)
            throw new Error(
              "the M23-18 ProductShell closure references a missing node",
            );
          included.add(nodeId);
          for (const value of Object.values(node.inputs ?? {}))
            if (
              Array.isArray(value) &&
              value.length === 2 &&
              (typeof value[0] === "string" || typeof value[0] === "number") &&
              Object.hasOwn(output, String(value[0]))
            )
              pending.push(String(value[0]));
        }
        runtime.__h3M2318QueuePhases?.push("managed_projection_call_through");
        return await originalQueuePrompt.call(this, batch, {
          ...compiled,
          output: Object.fromEntries(
            Object.entries(output).filter(([nodeId]) => included.has(nodeId)),
          ),
        });
      };
    },
    { scaleType: I2VA_SCALE_NODE_TYPE, sizeType: I2VA_SIZE_NODE_TYPE },
  );

  let hostEvidence: {
    compile: Record<string, unknown> | null;
    phases: string[];
  };
  try {
    await container.getByLabel("Task mode").selectOption("i2va");
    const source = container.getByLabel("First frame source");
    type M23ImageSourceJoin = {
      appSame: boolean;
      apiSame: boolean;
      graphSame: boolean;
      graphConfiguring: boolean;
      serializedIds: string[];
      renderedIds: string[];
    };
    const readImageSourceJoin = (): Promise<M23ImageSourceJoin> =>
      page.evaluate(async () => {
        const runtime = window as unknown as {
          comfyAPI: { app: { app: any }; api: { api: any } };
        };
        const importPublicHostModule = (
          specifier: string,
        ): Promise<Record<string, any>> => import(specifier);
        const [appModule, apiModule] = await Promise.all([
          importPublicHostModule("/scripts/app.js"),
          importPublicHostModule("/scripts/api.js"),
        ]);
        const app = runtime.comfyAPI.app.app;
        const serialized = app.graph.serialize() as {
          nodes?: Array<{ id?: unknown; type?: unknown }>;
        };
        const serializedIds = [
          ...new Set(
            (Array.isArray(serialized.nodes) ? serialized.nodes : [])
              .filter((node) => node.type === "LoadImage")
              .map((node) => node.id)
              .filter(
                (id): id is string | number =>
                  (typeof id === "number" &&
                    Number.isSafeInteger(id) &&
                    id >= 0) ||
                  (typeof id === "string" &&
                    /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(id)),
              )
              .map(String),
          ),
        ].sort((left, right) => left.localeCompare(right));
        const renderedIds = [
          ...document.querySelectorAll<HTMLSelectElement>(
            '#h3-context-m23-18 [data-h3-focus-key="app-first-frame"]',
          ),
        ].flatMap((select) =>
          [...select.options]
            .map((option) => option.value)
            .filter((value) => value.length > 0),
        );
        return {
          appSame: appModule.app === app,
          apiSame: apiModule.api === runtime.comfyAPI.api.api,
          graphSame: appModule.app?.graph === app.graph,
          graphConfiguring: app.configuringGraph === true,
          serializedIds,
          renderedIds,
        };
      });
    await expect.poll(readImageSourceJoin).toEqual({
      appSame: true,
      apiSame: true,
      graphSame: true,
      graphConfiguring: false,
      serializedIds: ["1"],
      renderedIds: ["1"],
    });
    await source.selectOption({ index: 1 });
    await container
      .getByRole("textbox", { name: "Intent" })
      .fill("Preserve the supplied portrait source geometry exactly.");
    await container
      .getByRole("spinbutton", { name: "Clip duration (seconds)" })
      .fill("8");
    await expect(
      container.getByText("Delivers 8 s (192 frames).", { exact: true }),
    ).toBeVisible();
    await resetHostGraphLoads(page);
    await expect(
      container.locator('[data-h3-focus-key="app-submit"]'),
    ).toBeEnabled();
    await container.locator('[data-h3-focus-key="app-submit"]').click();
    await expect
      .poll(
        async () =>
          await page.evaluate(
            () =>
              (window as unknown as { __h3M2318QueuePhases?: string[] })
                .__h3M2318QueuePhases?.length ?? 0,
          ),
        { timeout: 60_000 },
      )
      .toBe(1);
    await expect.poll(() => geometryResponses.length).toBe(1);
    expect(geometryRequests).toEqual([
      {
        method: "POST",
        sameOrigin: true,
        routeSuffix: true,
        exactPath: true,
        body: {
          schema: "h3.context.input_geometry.request.v2",
          locator: m23I2vaInputLocator,
        },
      },
    ]);
    await expect.poll(() => preparations.length).toBe(1);
    hostEvidence = await page.evaluate(() => {
      const runtime = window as unknown as {
        __h3M2318Compile?: Record<string, unknown>;
        __h3M2318QueuePhases?: string[];
      };
      return {
        compile: runtime.__h3M2318Compile ?? null,
        phases: runtime.__h3M2318QueuePhases ?? [],
      };
    });
  } finally {
    await page.evaluate(() => {
      const runtime = window as unknown as { __h3M2318Restore?: () => void };
      runtime.__h3M2318Restore?.();
      delete runtime.__h3M2318Restore;
    });
  }

  expect(hostEvidence).toEqual({
    compile: {
      correctedSourceSizeTopology: true,
      selectorStillAuthoritative: false,
      upscaleMethod: "nearest-exact",
      megapixels: 0.8,
      resolutionSteps: 32,
    },
    phases: ["managed_projection_call_through"],
  });
  expect(geometryResponses).toEqual([
    {
      status: 200,
      keys: ["receipt_handle", "schema", "source_fingerprint"],
      schema: INPUT_GEOMETRY_RECEIPT_SCHEMA,
      receiptHandleShape: true,
      sourceFingerprintShape: true,
    },
  ]);
  expect(preparations).toEqual([
    {
      schema: "h3.context.prepared_graph_observation.v4",
      expectedFrames: 192,
      sourceKeys: ["receipt_handle", "schema", "source_fingerprint"],
      sourceFingerprint: expect.stringMatching(/^sha256:[0-9a-f]{64}$/),
    },
  ]);
  // M23-25 (reconciled by M23-37 on the supplied host): from a tabless start
  // the one candidate write is the explicit-null 0-to-1 bootstrap itself, so
  // the run performs exactly one host load, which attaches exactly one
  // workflow; nothing is loaded ahead of it and nothing after the queue.
  expect(await hostGraphLoads(page)).toBe(1);
  expect(await hostWorkflowAuthorityReceipt(page)).toEqual({
    activeStable: true,
    openWorkflowDelta: 1,
    graphLoadWorkflowMatches: [true],
  });
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);
});
