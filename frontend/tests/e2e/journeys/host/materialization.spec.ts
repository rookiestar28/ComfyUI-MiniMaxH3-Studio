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

test("M17-20 supported host qualifies every basis and materializes the pinned template", async ({
  context,
  page,
}, testInfo) => {
  test.setTimeout(120_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const managedRouteReceipts = monitorManagedRouteResponses(page);
  const managedPreparations: Array<{
    graphFingerprint: string;
    compiledPromptFingerprint: string;
    ownedProjectionFingerprint: string;
    ownedNodeIds: string[];
    ownedLinkIds: string[];
    anchorNodeId: string;
  }> = [];
  page.on("request", (request) => {
    if (
      !new URL(request.url()).pathname.endsWith(
        "/h3-context/v1/generation/coordinator",
      )
    )
      return;
    try {
      const body = JSON.parse(request.postData() ?? "null") as {
        action?: unknown;
        payload?: { observation?: Record<string, unknown> };
      } | null;
      if (body?.action !== "prepare_managed_run") return;
      const observation = body.payload?.observation ?? {};
      managedPreparations.push({
        graphFingerprint: String(observation.graph_fingerprint ?? ""),
        compiledPromptFingerprint: String(
          observation.compiled_prompt_fingerprint ?? "",
        ),
        ownedProjectionFingerprint: String(
          observation.owned_projection_fingerprint ?? "",
        ),
        ownedNodeIds: Array.isArray(observation.owned_node_ids)
          ? observation.owned_node_ids.map(String)
          : [],
        ownedLinkIds: Array.isArray(observation.owned_link_ids)
          ? observation.owned_link_ids.map(String)
          : [],
        anchorNodeId: String(observation.native_anchor_node_id ?? ""),
      });
    } catch {
      managedPreparations.push({
        graphFingerprint: "invalid",
        compiledPromptFingerprint: "invalid",
        ownedProjectionFingerprint: "invalid",
        ownedNodeIds: [],
        ownedLinkIds: [],
        anchorNodeId: "invalid",
      });
    }
  });
  const initialInjectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  const packNames = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const names = Array.isArray(app.extensions)
      ? app.extensions
          .map((extension: { name?: unknown }) => extension?.name)
          .filter(
            (name: unknown): name is string =>
              typeof name === "string" &&
              /^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$/.test(name),
          )
      : [];
    const unique = [...new Set(names)].sort();
    if (unique.length > 512)
      throw new Error("the extension-name census exceeds its evidence bound");
    return unique;
  });
  await setSupportedH3Language(page, "en");

  // The projection is decoded by the shipped decoder, so a payload this build
  // would refuse in the browser cannot pass here either.
  const wire = await page.evaluate(async () => {
    const response = await fetch("/h3-context/v1/generation/profile", {
      method: "GET",
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`profile route ${response.status}`);
    return await response.json();
  });
  const profile = decodeGenerationProfile(wire);
  expect(profile.families.map((entry) => entry.templateName)).toEqual([
    "video_minimax_h3_t2v",
    "video_minimax_h3_i2v",
    "video_minimax_h3_r2v",
  ]);
  const textToVideo = familyProfileForTaskMode(profile, "t2va");
  expect(textToVideo?.templateName).toBe("video_minimax_h3_t2v");
  // Whatever this host reports, it must not be drift: the served template bytes
  // are the ones this build pinned, or materializing from them is unqualified.
  expect(textToVideo?.disposition).not.toBe("template_drift");

  await instrumentHostGraphLoads(page);
  await waitForHostGraphSettled(page);
  const container = await openH3AppModeTab(page, "h3-context-m17-20-container");
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  await page.evaluate(() => {
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
      __h3QueueCount?: number;
      __h3QueuePhases?: string[];
      __h3ManagedPromptId?: string;
      __h3NativePromptReceipt?: Record<string, unknown>;
      __h3GraphToPromptReceipt?: {
        calls: number;
        succeeded: number;
        failed: number;
      };
      __h3CompiledShape?: Record<string, unknown>;
      __h3HostProjectionTrace?: Array<Record<string, unknown>>;
      __h3ProjectionTrace?: Array<Record<string, unknown>>;
      __h3ExecutedOutput?: unknown;
    };
    const app = runtime.comfyAPI.app.app;
    const api = runtime.comfyAPI.api.api;
    const originalQueuePrompt = api.queuePrompt;
    const originalGraphToPrompt = app.graphToPrompt?.bind(app);
    if (typeof originalQueuePrompt !== "function")
      throw new Error("the public host queue seam is unavailable");
    if (typeof originalGraphToPrompt !== "function")
      throw new Error("the public host compile seam is unavailable");
    runtime.__h3QueueCount = 0;
    runtime.__h3QueuePhases = [];
    runtime.__h3HostProjectionTrace = [];
    runtime.__h3ProjectionTrace = [];
    runtime.__h3ExecutedOutput = undefined;
    api.addEventListener("executed", (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      const output = detail?.output;
      if (
        output !== null &&
        typeof output === "object" &&
        !Array.isArray(output) &&
        Array.isArray((output as Record<string, unknown>).schema) &&
        Array.isArray((output as Record<string, unknown>).sidebar_workspace)
      )
        runtime.__h3ExecutedOutput = output;
    });
    runtime.__h3GraphToPromptReceipt = { calls: 0, succeeded: 0, failed: 0 };
    app.graphToPrompt = async (...args: unknown[]) => {
      runtime.__h3GraphToPromptReceipt!.calls += 1;
      try {
        const compiled = (await originalGraphToPrompt(
          ...args,
        )) as CompiledPrompt;
        const output = compiled.output ?? {};
        const entries = Object.entries(output);
        const byId = new Map(entries);
        const generation = entries.find(([, node]) =>
          new Set(["MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"]).has(
            String(node.class_type ?? ""),
          ),
        );
        const isLink = (value: unknown): value is [string | number, number] =>
          Array.isArray(value) &&
          value.length === 2 &&
          (typeof value[0] === "string" || typeof value[0] === "number") &&
          Number.isSafeInteger(value[1]);
        runtime.__h3CompiledShape = {
          outputNodeCount: entries.length,
          generationCount: generation === undefined ? 0 : 1,
          managedProductShellNodeId:
            entries.find(
              ([, node]) =>
                node.class_type === "comfyui_h3_context.H3Context.ProductShell",
            )?.[0] ?? null,
          officialLinkedDimensions:
            isLink(generation?.[1].inputs?.width) &&
            isLink(generation?.[1].inputs?.height) &&
            String(generation[1].inputs?.width?.[0]) ===
              String(generation[1].inputs?.height?.[0]) &&
            generation[1].inputs?.width?.[1] === 0 &&
            generation[1].inputs?.height?.[1] === 1 &&
            byId.get(String(generation[1].inputs?.width?.[0]))?.class_type ===
              "ResolutionSelector",
        };
        runtime.__h3GraphToPromptReceipt!.succeeded += 1;
        return compiled;
      } catch (error) {
        runtime.__h3GraphToPromptReceipt!.failed += 1;
        throw error;
      }
    };
    const countType = (types: readonly string[], type: string): number =>
      types.filter((candidate) => candidate === type).length;
    const requiredBootstrapSingletons = [
      "comfyui_h3_context.H3Context.Request",
      "comfyui_h3_context.H3Context.Plan",
      "comfyui_h3_context.H3Context.Compiler",
      "comfyui_h3_context.H3Context.Validator",
      "comfyui_h3_context.H3Context.NativeH3Adapter",
      "comfyui_h3_context.H3Context.ProductShell",
    ];
    const optionalBootstrapSingletons = [
      "comfyui_h3_context.H3Context.ReferenceRegistry",
    ];
    const assetFreeBootstrapTypes = new Set([
      ...requiredBootstrapSingletons,
      ...optionalBootstrapSingletons,
      "PrimitiveFloat",
      "PrimitiveInt",
    ]);
    const nativePromptReceipt = (
      compiled: CompiledPrompt,
    ): Record<string, unknown> => {
      const output = compiled.output ?? {};
      const byId = new Map(Object.entries(output));
      const request = Object.values(output).find(
        (node) => node.class_type === "comfyui_h3_context.H3Context.Request",
      )?.inputs;
      const generation = Object.values(output).find(
        (node) => node.class_type === "MiniMaxH3ImageToVideo",
      )?.inputs;
      const scalarTerminal = (
        input: unknown,
      ): { source: string | null; value: unknown } => {
        let current = input;
        let source: string | null = null;
        for (let hop = 0; hop < 8; hop += 1) {
          if (!Array.isArray(current)) return { source, value: current };
          if (current.length !== 2 || current[1] !== 0)
            return { source: null, value: null };
          const nodeId = String(current[0]);
          const node = byId.get(nodeId);
          if (node === undefined) return { source: null, value: null };
          source = nodeId;
          if (Object.hasOwn(node.inputs ?? {}, "value"))
            current = node.inputs?.value;
          else if (Object.hasOwn(node.inputs ?? {}, "values.a"))
            current = node.inputs?.["values.a"];
          else return { source, value: null };
        }
        return { source: null, value: null };
      };
      const linkedNode = (input: unknown): CompiledNode | undefined =>
        Array.isArray(input) && input.length === 2 && input[1] === 0
          ? byId.get(String(input[0]))
          : undefined;
      const sameLink = (left: unknown, right: unknown): boolean =>
        Array.isArray(left) &&
        Array.isArray(right) &&
        left.length === 2 &&
        right.length === 2 &&
        String(left[0]) === String(right[0]) &&
        left[1] === right[1];
      const scheduler = Object.values(output).find(
        (node) =>
          node.class_type === "BasicScheduler" &&
          linkedNode(node.inputs?.steps)?.class_type === "ComfySwitchNode",
      );
      const stepSwitch = linkedNode(scheduler?.inputs?.steps);
      const stepControl = scalarTerminal(stepSwitch?.inputs?.switch);
      const baseSteps = scalarTerminal(stepSwitch?.inputs?.on_false).value;
      const turboSteps = scalarTerminal(stepSwitch?.inputs?.on_true).value;
      const loraSwitch = Object.values(output).find(
        (node) =>
          node.class_type === "ComfySwitchNode" &&
          linkedNode(node.inputs?.on_true)?.class_type ===
            "LoraLoaderModelOnly",
      );
      const requestDuration = scalarTerminal(request?.duration_seconds);
      const nativePromptInput = generation?.prompt;
      const nativePromptProducer = linkedNode(nativePromptInput);
      const nativeLength = generation?.length;
      const nativeLengthNode =
        Array.isArray(nativeLength) &&
        nativeLength.length === 2 &&
        nativeLength[1] === 1
          ? byId.get(String(nativeLength[0]))
          : undefined;
      const nativeDurationOperand = scalarTerminal(
        nativeLengthNode?.inputs?.["values.a"] ?? nativeLengthNode?.inputs?.a,
      );
      return {
        taskMode: request?.task_mode,
        anchorPromptBindingOwned:
          Array.isArray(nativePromptInput) &&
          nativePromptInput.length === 2 &&
          nativePromptInput[1] === 0 &&
          nativePromptProducer?.class_type ===
            "comfyui_h3_context.H3Context.ProductShell",
        requestDurationSeconds: requestDuration.value,
        nativeDurationOperandSeconds: nativeDurationOperand.value,
        nativeLengthOutputSlot:
          Array.isArray(nativeLength) && nativeLength.length === 2
            ? nativeLength[1]
            : null,
        nativeLengthExpression: nativeLengthNode?.inputs?.expression ?? null,
        sharedDurationSource:
          requestDuration.source !== null &&
          requestDuration.source === nativeDurationOperand.source,
        // These describe the pinned replacement candidate. They are not
        // values App Mode may overwrite on an existing user-selected canvas.
        templateTurboMode: stepControl.value,
        templateBaseSteps: baseSteps,
        templateTurboSteps: turboSteps,
        templateEffectiveSteps:
          stepControl.value === false
            ? baseSteps
            : stepControl.value === true
              ? turboSteps
              : null,
        templateTurboLoraSharesControl:
          stepSwitch !== undefined &&
          loraSwitch !== undefined &&
          sameLink(stepSwitch.inputs?.switch, loraSwitch.inputs?.switch),
      };
    };
    // M23-19: the product submits one full managed prompt. This host-only
    // harness records that exact envelope, then sends only ProductShell's
    // backward closure to avoid loading model assets while preserving the
    // prompt id and real backend projection used by the product lifecycle.
    api.queuePrompt = async function (
      batch: unknown,
      compiled: CompiledPrompt,
    ) {
      runtime.__h3QueueCount = (runtime.__h3QueueCount ?? 0) + 1;
      const output = compiled.output ?? {};
      const types = Object.values(output).map((node) =>
        String(node.class_type ?? ""),
      );
      const productShellEntry = Object.entries(output).find(
        ([, node]) =>
          node.class_type === "comfyui_h3_context.H3Context.ProductShell",
      );
      const hasProductShell = productShellEntry !== undefined;
      const hasNative = types.some((type) =>
        new Set(["MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"]).has(
          type,
        ),
      );
      const hasSink = types.includes("SaveVideo");
      if (!hasProductShell || !hasNative || !hasSink)
        throw new Error("the single managed queue envelope is not qualified");

      runtime.__h3NativePromptReceipt = nativePromptReceipt(compiled);
      const included = new Set<string>();
      const pending = [productShellEntry[0]];
      while (pending.length > 0) {
        const nodeId = pending.pop()!;
        if (included.has(nodeId)) continue;
        const node = output[nodeId];
        if (node === undefined)
          throw new Error("the ProductShell closure references a missing node");
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
      const projectionOutput = Object.fromEntries(
        Object.entries(output).filter(([nodeId]) => included.has(nodeId)),
      );
      const projectionTypes = Object.values(projectionOutput).map((node) =>
        String(node.class_type ?? ""),
      );
      const queuePromptSource =
        Function.prototype.toString.call(originalQueuePrompt);
      const workflow = compiled.workflow;
      const hasContentFreeWorkflow =
        typeof workflow === "object" &&
        workflow !== null &&
        !Array.isArray(workflow) &&
        Object.getPrototypeOf(workflow) === Object.prototype &&
        Reflect.ownKeys(workflow).length === 8 &&
        (workflow as Record<string, unknown>).last_node_id === 0 &&
        (workflow as Record<string, unknown>).last_link_id === 0 &&
        (workflow as Record<string, unknown>).version === 0.4 &&
        ["nodes", "links", "groups"].every((key) => {
          const value = (workflow as Record<string, unknown>)[key];
          return Array.isArray(value) && value.length === 0;
        }) &&
        ["config", "extra"].every((key) => {
          const value = (workflow as Record<string, unknown>)[key];
          return (
            typeof value === "object" &&
            value !== null &&
            !Array.isArray(value) &&
            Reflect.ownKeys(value).length === 0
          );
        });
      const safeAssetFreeClosure =
        hasContentFreeWorkflow &&
        projectionTypes.length > 0 &&
        projectionTypes.length <= 128 &&
        projectionTypes.every((type) => assetFreeBootstrapTypes.has(type)) &&
        requiredBootstrapSingletons.every(
          (type) => countType(projectionTypes, type) === 1,
        ) &&
        optionalBootstrapSingletons.every(
          (type) => countType(projectionTypes, type) <= 1,
        );
      if (runtime.__h3CompiledShape !== undefined)
        Object.assign(runtime.__h3CompiledShape, {
          bootstrapProductShellNodeId: productShellEntry[0],
          bootstrapNodeCount: projectionTypes.length,
          bootstrapWorkflowPlain: hasContentFreeWorkflow,
          bootstrapUnadmittedTypes: [...new Set(projectionTypes)]
            .filter((type) => !assetFreeBootstrapTypes.has(type))
            .sort(),
          bootstrapSingletonCounts: Object.fromEntries(
            [
              ...requiredBootstrapSingletons,
              ...optionalBootstrapSingletons,
            ].map((type) => [type, countType(projectionTypes, type)]),
          ),
          queuePromptArity: originalQueuePrompt.length,
          queuePromptName: originalQueuePrompt.name,
          queuePromptOwnProperty: Object.hasOwn(api, "queuePrompt"),
          queuePromptMatchesPrototype:
            Object.getPrototypeOf(api)?.queuePrompt === originalQueuePrompt,
          queuePromptLooksLikeTransport:
            queuePromptSource.includes("/prompt") ||
            queuePromptSource.includes("fetchApi"),
          queuePromptLooksLikeAppQueue:
            queuePromptSource.includes("queueItems") ||
            queuePromptSource.includes("graphToPrompt"),
          queuePromptWrapperClass: queuePromptSource.includes("seed_widgets")
            ? "easy_use_seed"
            : queuePromptSource.includes("widget_idx_map")
              ? "inspire_widget_index"
              : queuePromptSource.includes("queueNodeIds")
                ? "rgthree_queue_nodes"
                : queuePromptSource.includes("promptsMap")
                  ? "rgthree_prompt_service"
                  : "unknown_wrapper",
        });
      if (!safeAssetFreeClosure)
        throw new Error(
          "the ProductShell projection closure is not asset-free",
        );

      runtime.__h3QueuePhases?.push("managed_projection_call_through");
      const queued = await originalQueuePrompt.call(this, batch, {
        ...compiled,
        output: projectionOutput,
      });
      const candidate =
        queued !== null && typeof queued === "object"
          ? (queued as Record<string, unknown>)
          : {};
      if (typeof candidate.prompt_id === "string")
        runtime.__h3ManagedPromptId = candidate.prompt_id;
      return queued;
    };
  });
  await container
    .getByRole("textbox", { name: "Intent" })
    .fill("A red kite crosses the sky while the camera follows its arc.");
  await container
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("8");
  await expect(
    container.getByText("Delivers 8 s (192 frames).", { exact: true }),
  ).toBeVisible();
  if (textToVideo?.disposition === "missing_asset")
    await expect(
      container.locator('[data-h3-generation-blocker="missing_asset"]'),
    ).toHaveCount(1);
  expect(
    await container
      .locator("[data-shell-status]")
      .getAttribute("data-shell-status"),
  ).not.toBe("error");
  await resetHostGraphLoads(page);
  await container.locator('[data-h3-focus-key="app-submit"]').click();
  if (textToVideo?.disposition === "missing_asset")
    await expect(
      container.locator('[data-shell-status="working"]'),
    ).toHaveCount(0, { timeout: 60_000 });
  else {
    try {
      await waitForAppModeQueue(
        page,
        "h3-context-m17-20-container",
        "__h3QueueCount",
        1,
        60_000,
      );
    } catch {
      // Let an in-flight public transport settle before classifying a local
      // queue-seam failure; this remains below every product timeout.
      await page.waitForTimeout(3_000);
      const executedOutput = await page.evaluate(
        () =>
          (window as unknown as { __h3ExecutedOutput?: unknown })
            .__h3ExecutedOutput,
      );
      let decodeFailure = "executed_output_absent";
      if (executedOutput !== undefined) {
        try {
          projectionFromOutput(executedOutput);
          decodeFailure = "none";
        } catch (error) {
          const message = error instanceof Error ? error.message : typeof error;
          decodeFailure = /^[A-Za-z0-9_.\[\] -]{1,160}$/.test(message)
            ? message
            : "unclassified_decode_failure";
        }
      }
      throw new Error(
        `content-free managed route diagnostic: ${JSON.stringify({
          routes: managedRouteReceipts,
          decodeFailure,
          surface: await appModeQueueDiagnostic(
            page,
            "h3-context-m17-20-container",
            "__h3QueueCount",
          ),
        })}`,
      );
    }
  }
  // The run must not have abandoned its own work: exactly one canvas write, and
  // no report that the transaction was cancelled. Both are only observable on a
  // real host, which announces the write back to every extension.
  expect(await hostGraphLoads(page)).toBe(1);
  expect(
    await container
      .locator("[data-shell-status]")
      .getAttribute("data-shell-reason"),
  ).not.toBe("cancelled");

  const graph = await readVisibleGraph(page);
  const surroundingsBaseline = structuredClone(graph);
  if (candidateBundle !== null)
    expectCompleteHostAssetResolution(
      await hostAssetResolutionReceipt(page, "image_to_video"),
    );
  const topLevel = graph.nodes ?? [];
  const inner = subgraphNodes(graph);
  const sinks = topLevel.filter((node) => node.type === "SaveVideo");
  expect(sinks).toHaveLength(1);
  const prefix = (sinks[0]?.widgets_values as unknown[])?.[0];
  expect(typeof prefix).toBe("string");
  expect(String(prefix).startsWith(`${APP_MODE_ARTIFACT_PREFIX_ROOT}/`)).toBe(
    true,
  );
  // The complete flow the item exists to produce: model loading, the native
  // anchor, sampling, decode and a container, not a conditioning stub.
  for (const type of [
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "SamplerCustomAdvanced",
    "VAEDecode",
    "CreateVideo",
    "MiniMaxH3ImageToVideo",
  ])
    expect(inner.some((node) => node.type === type)).toBe(true);
  expect(
    topLevel.some(
      (node) => node.type === "comfyui_h3_context.H3Context.ProductShell",
    ),
  ).toBe(true);

  const queueCount = await page.evaluate(
    () =>
      (window as unknown as { __h3QueueCount?: number }).__h3QueueCount ?? 0,
  );
  if (textToVideo?.disposition === "missing_asset") {
    // D2, both halves. The submission is refused and nothing is queued; the
    // canvas the refusal produced is kept and offered as a queue target, which
    // is the remediation the refusal names -- resolve the model on that canvas
    // and run the graph you can see.
    expect(queueCount).toBe(0);
    await expect(
      container.getByRole("button", { name: /queue current H3 graph/i }),
    ).toBeEnabled({ timeout: 30_000 });
  } else {
    expect(queueCount).toBe(1);
    expect(
      await page.evaluate(
        () =>
          (window as unknown as { __h3QueuePhases?: string[] })
            .__h3QueuePhases ?? [],
      ),
    ).toEqual(["managed_projection_call_through"]);
    expect(
      await page.evaluate(
        () =>
          (
            window as unknown as {
              __h3NativePromptReceipt?: Record<string, unknown>;
            }
          ).__h3NativePromptReceipt ?? null,
      ),
    ).toMatchObject({
      taskMode: "t2va",
      anchorPromptBindingOwned: true,
      requestDurationSeconds: 8,
      nativeDurationOperandSeconds: 8,
      nativeLengthOutputSlot: 1,
      nativeLengthExpression: OFFICIAL_LENGTH_EXPRESSION,
      sharedDurationSource: true,
      templateTurboMode: false,
      templateBaseSteps: 20,
      templateTurboSteps: 8,
      templateEffectiveSteps: 20,
      templateTurboLoraSharesControl: true,
    });
    await expect(
      container.locator('[data-app-mode-phase="generating"]'),
    ).toHaveCount(1);
    await container.getByRole("button", { name: "Production" }).click();
    await expect(
      container.getByText("0 of 1 segments", { exact: true }),
    ).toBeVisible();
    await expect(
      container.getByText("Sequence authority is unavailable", {
        exact: true,
      }),
    ).toHaveCount(0);
    await container
      .getByRole("button", { name: "Context", exact: true })
      .click();
    const graphLoadsBeforeKeep = await hostGraphLoads(page);
    // M23-07: finish the intercepted, content-free submission through the real
    // host event seam. The private diagnostic fields must never cross into the
    // product shell, and one admitted click must remain one queue attempt.
    await page.evaluate(() => {
      const runtime = window as unknown as {
        comfyAPI: { api: { api: EventTarget } };
        __h3ManagedPromptId?: string;
      };
      const api = runtime.comfyAPI.api.api;
      if (typeof runtime.__h3ManagedPromptId !== "string")
        throw new Error("the managed prompt id was not captured");
      api.dispatchEvent(
        new CustomEvent("execution_error", {
          detail: {
            prompt_id: runtime.__h3ManagedPromptId,
            exception_message: "m23-08-private-host-diagnostic",
            traceback: ["m23-08-private-host-traceback"],
            prompt: { private: true },
          },
        }),
      );
    });
    await expect(container.locator('[data-shell-status="error"]')).toHaveCount(
      1,
    );
    await expect(container.getByRole("alert")).toContainText(
      "The H3 execution failed on the host.",
    );
    await expect(container).not.toContainText("m23-08-private-host-diagnostic");
    await expect(
      container.getByRole("button", { name: "Retry H3 App Mode" }),
    ).toBeVisible();
    await expect(
      container.getByRole("button", { name: "Continue with native nodes" }),
    ).toBeVisible();
    // M23-11: the recovery action and the dirty-canvas Keep button share this
    // handler. Exercise it on the real serialized H3 canvas: the immediate
    // refresh may update recognition, but cannot reopen the decision, load a
    // graph or submit another queue request.
    await container
      .getByRole("button", { name: "Continue with native nodes" })
      .click();
    await expect(
      container.locator('[data-shell-reason="native_preference"]'),
    ).toHaveCount(1);
    await expect(
      container.getByRole("button", { name: /queue current H3 graph/i }),
    ).toBeEnabled();
    await expect(
      container.getByRole("textbox", { name: "Intent" }),
    ).toBeVisible();
    expect(await hostGraphLoads(page)).toBe(graphLoadsBeforeKeep);
    expect(
      await page.evaluate(
        () =>
          (window as unknown as { __h3QueueCount?: number }).__h3QueueCount ??
          0,
      ),
    ).toBe(1);
    expect(
      await privateDiagnosticLeakReceipt(page, "h3-context-m17-20-container", [
        "__h3QueueCount",
        "__h3QueuePhases",
        "__h3NativePromptReceipt",
      ]),
    ).toEqual({ shellState: false, storage: false, evidence: false });

    // Exercise the same supported-host canvas through the existing route. Refuse
    // only the next Production creation after the one managed submission's
    // ProductShell projection. M23-25 (reconciled by M23-37): the existing route
    // validates a detached candidate and writes it exactly once into the captured
    // workflow object before the queue — one load, no new tab. Once queue
    // ownership crossed, user parameters stay on that workflow; a second load
    // would create a duplicate tab and falsely imply that the accepted host
    // submission had been rolled back.
    // M23-32: the aggregate owns Production creation, so the refusal that models
    // "the backend failed after the host accepted the prompt" is now the
    // coordinator's `prepare_managed_run`, not the Production registry's
    // `create_workspace_from_context`. The two are the same point in the run:
    // prepare carries the bootstrap `prompt_id`, so it runs after that queue has
    // crossed. Keying this filter on the removed action does not fail — it simply
    // matches nothing, injects no fault, and lets the run succeed, so the
    // vacuity guard below must stay ahead of the error-surface wait.
    let refusedExistingPrepare = false;
    await page.route(
      "**/h3-context/v1/generation/coordinator",
      async (route) => {
        let action: unknown;
        try {
          action = (
            JSON.parse(route.request().postData() ?? "null") as {
              action?: unknown;
            } | null
          )?.action;
        } catch {
          action = undefined;
        }
        if (action === "prepare_managed_run" && !refusedExistingPrepare) {
          refusedExistingPrepare = true;
          await route.fulfill({
            status: 500,
            contentType: "application/json",
            body: "{}",
          });
          return;
        }
        await route.continue();
      },
    );
    await resetHostGraphLoads(page);
    await container
      .getByRole("textbox", { name: "Intent" })
      .fill("A silver kite circles once while the camera holds its horizon.");
    await container.locator('[data-h3-focus-key="app-submit"]').click();
    await expect
      .poll(() => refusedExistingPrepare, { timeout: 30_000 })
      .toBe(true);
    await expect(container.locator('[data-shell-status="error"]')).toHaveCount(
      1,
      { timeout: 60_000 },
    );
    const existingRouteDiagnostic = await appModeQueueDiagnostic(
      page,
      "h3-context-m17-20-container",
      "__h3QueueCount",
    );
    if (
      existingRouteDiagnostic.queueCount !== 2 ||
      existingRouteDiagnostic.errorClass !== "ambiguous_host_ownership" ||
      existingRouteDiagnostic.graphLoads !== 1
    )
      throw new Error(
        `content-free existing-route diagnostic: ${JSON.stringify(existingRouteDiagnostic)}`,
      );
    expect(existingRouteDiagnostic).toMatchObject({
      queueCount: 2,
      errorClass: "ambiguous_host_ownership",
      graphLoads: 1,
    });
    expect(refusedExistingPrepare).toBe(true);
    expect(existingRouteDiagnostic.errorClass).not.toBe("rollback_failed");
    expect(
      await page.evaluate(
        () =>
          (window as unknown as { __h3QueuePhases?: string[] })
            .__h3QueuePhases ?? [],
      ),
    ).toEqual([
      "managed_projection_call_through",
      "managed_projection_call_through",
    ]);
    // M23-25 (reconciled by M23-37): the existing route writes its validated
    // candidate exactly once, into the captured workflow object, so the one
    // recorded load targets that object and no tab is opened.
    const workflowAuthority = await hostWorkflowAuthorityReceipt(page);
    expect(workflowAuthority).toEqual({
      activeStable: true,
      openWorkflowDelta: 0,
      graphLoadWorkflowMatches: [true],
    });
    const openWorkflow = await page.evaluate(() => {
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any } };
        __h3GraphWorkflowAuthority?: object | null;
      };
      const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
      return {
        openCount: Array.isArray(store?.openWorkflows)
          ? store.openWorkflows.length
          : -1,
        sameWorkflow:
          store?.activeWorkflow === runtime.__h3GraphWorkflowAuthority,
      };
    });
    expect(openWorkflow).toEqual({ openCount: 1, sameWorkflow: true });

    expect(managedPreparations).toHaveLength(2);
    const firstPreparation = managedPreparations[0]!;
    const secondPreparation = managedPreparations[1]!;
    for (const fingerprint of [
      firstPreparation.graphFingerprint,
      firstPreparation.compiledPromptFingerprint,
      firstPreparation.ownedProjectionFingerprint,
      secondPreparation.graphFingerprint,
      secondPreparation.compiledPromptFingerprint,
      secondPreparation.ownedProjectionFingerprint,
    ])
      expect(fingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(secondPreparation.ownedProjectionFingerprint).toBe(
      firstPreparation.ownedProjectionFingerprint,
    );
    expect(secondPreparation.ownedNodeIds).toEqual(
      firstPreparation.ownedNodeIds,
    );
    expect(secondPreparation.ownedLinkIds).toEqual(
      firstPreparation.ownedLinkIds,
    );
    expect(firstPreparation.ownedNodeIds.length).toBeGreaterThan(0);
    expect(firstPreparation.ownedLinkIds.length).toBeGreaterThan(0);

    const finalGraph = await readVisibleGraph(page);
    const finalNativePromptReceipt = await page.evaluate(
      () =>
        (
          window as unknown as {
            __h3NativePromptReceipt?: Record<string, unknown>;
          }
        ).__h3NativePromptReceipt ?? null,
    );
    const anchorPromptBindingOwned =
      finalNativePromptReceipt?.anchorPromptBindingOwned === true;
    expect(anchorPromptBindingOwned).toBe(true);
    const visibleAnchorNodeId = secondPreparation.anchorNodeId.split(
      ":",
      1,
    )[0]!;
    expect(visibleAnchorNodeId).toMatch(/^\d{1,20}$/);

    // IMPORTANT (M23-49 requalification): retain an executed surroundings diff and the direct
    // anchor prompt-link check beside the queue/workflow receipts. An import-only helper or a
    // service-health probe recreates the false closeout this row is correcting.
    const surroundingsEvidence = await page.evaluate(diffGraphSurroundings, {
      beforeValue: surroundingsBaseline,
      afterValue: finalGraph,
      reference: {
        ownedNodeIds: firstPreparation.ownedNodeIds,
        ownedLinkIds: firstPreparation.ownedLinkIds,
        anchorNodeId: firstPreparation.anchorNodeId.split(":", 1)[0]!,
        ownedProjectionEqual:
          firstPreparation.ownedProjectionFingerprint ===
          secondPreparation.ownedProjectionFingerprint,
      },
    });
    expect(surroundingsEvidence.counts.owned).toBe(0);
    const m23Evidence = {
      schema: "h3.context.m23_49_owned_identity_acceptance.v1",
      owned: {
        projectionFingerprint: firstPreparation.ownedProjectionFingerprint,
        nodeIds: firstPreparation.ownedNodeIds,
        linkIds: firstPreparation.ownedLinkIds,
        visibleAnchorNodeId,
        compiledAnchorNodeId: firstPreparation.anchorNodeId,
        anchorPromptBindingOwned,
      },
      workflow: { ...workflowAuthority, ...openWorkflow },
      queue: {
        calls: existingRouteDiagnostic.queueCount,
        phases: [
          "managed_projection_call_through",
          "managed_projection_call_through",
        ],
      },
      surroundings: surroundingsEvidence,
      packNames,
    };
    const serializedEvidence = JSON.stringify(m23Evidence);
    expect(serializedEvidence).not.toMatch(/[A-Za-z]:[\\/]/);
    expect(serializedEvidence).not.toMatch(/(?:Users|home)[\\/]/i);
    console.log(`H3_CONTEXT_M23_49_EVIDENCE=${serializedEvidence}`);
    await testInfo.attach("m23-49-owned-identity-evidence", {
      body: Buffer.from(`${serializedEvidence}\n`, "utf8"),
      contentType: "application/json",
    });
  }
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  // What this repository owns is what its own bundle asks for, and it must ask
  // for nothing off the host. A page-wide count cannot say that here: the
  // supplied host runs other node packs, and theirs load fonts and CDN scripts
  // whenever they please. The provider count stays page-wide, because no pack on
  // this canvas has any business reaching a generation provider during an H3
  // interaction.
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  const attribution = networkAttribution.snapshot();
  expect(attribution.interactionProviderCount).toBe(0);
});
