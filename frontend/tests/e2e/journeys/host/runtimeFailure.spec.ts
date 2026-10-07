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

test("M23-39 accepted managed generation reports its real host execution failure", async ({
  context,
  page,
}) => {
  test.skip(
    !m23RuntimeFailureAuthorized,
    "M23-39 requires the owner-designated runtime-only arithmetic failure subject",
  );
  test.setTimeout(10 * 60_000);
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

  const initialCanvas = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M2339InitialCanvas?: string;
      __h3M2339InitialLoaderWidgets?: string[];
    };
    const app = runtime.comfyAPI.app.app;
    const serialized = app.graph.serialize();
    const loaderTypes = new Set([
      "UNETLoader",
      "CLIPLoader",
      "LoraLoaderModelOnly",
    ]);
    runtime.__h3M2339InitialCanvas = JSON.stringify(serialized);
    runtime.__h3M2339InitialLoaderWidgets = (serialized?.nodes ?? [])
      .filter((node: { type?: unknown }) =>
        loaderTypes.has(String(node.type ?? "")),
      )
      .map((node: { id?: unknown; type?: unknown; widgets_values?: unknown }) =>
        JSON.stringify([
          String(node.id ?? ""),
          String(node.type ?? ""),
          Array.isArray(node.widgets_values) ? node.widgets_values : [],
        ]),
      )
      .sort();
    return {
      nodeCount: Array.isArray(serialized?.nodes)
        ? serialized.nodes.length
        : -1,
      loaderCount: (serialized?.nodes ?? []).filter(
        (node: { type?: unknown }) => loaderTypes.has(String(node.type ?? "")),
      ).length,
    };
  });
  expect(initialCanvas.nodeCount).toBeGreaterThanOrEqual(0);
  expect(initialCanvas.loaderCount).toBeGreaterThanOrEqual(0);

  const container = await openH3AppModeTab(
    page,
    "h3-context-m23-39-runtime-failure",
  );
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await watchHostExecution(page);
  await page.evaluate(() => {
    type CompiledNode = {
      class_type?: unknown;
      inputs?: Record<string, unknown>;
    };
    type CompiledPrompt = {
      output?: Record<string, CompiledNode>;
      workflow?: unknown;
    };
    type FailureReceipt = {
      queueCalls: number;
      fullGenerationEnvelope: boolean;
      productShells: number;
      requests: number;
      nativeGenerators: number;
      sinks: number;
      loaderCounts: Record<string, number>;
      loaderInputsPreserved: boolean;
      runtimeDependencyInjected: boolean;
      queuedProjectionAssetFree: boolean;
      queuedLoaderCount: number;
    };
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any }; api: { api: any } };
      __h3M2339FailureReceipt?: FailureReceipt;
      __h3M2339LoaderInputs?: string[];
    };
    const api = runtime.comfyAPI.api.api;
    const originalQueuePrompt = api.queuePrompt.bind(api);
    const loaderTypes = [
      "UNETLoader",
      "CLIPLoader",
      "LoraLoaderModelOnly",
    ] as const;
    const loaderInputs = (output: Record<string, CompiledNode>): string[] =>
      Object.entries(output)
        .filter(([, node]) => loaderTypes.includes(node.class_type as never))
        .map(([id, node]) =>
          JSON.stringify([id, node.class_type, node.inputs ?? {}]),
        )
        .sort();
    runtime.__h3M2339FailureReceipt = {
      queueCalls: 0,
      fullGenerationEnvelope: false,
      productShells: 0,
      requests: 0,
      nativeGenerators: 0,
      sinks: 0,
      loaderCounts: {},
      loaderInputsPreserved: false,
      runtimeDependencyInjected: false,
      queuedProjectionAssetFree: false,
      queuedLoaderCount: -1,
    };
    api.queuePrompt = async function (
      batch: unknown,
      compiled: CompiledPrompt,
    ) {
      const receipt = runtime.__h3M2339FailureReceipt!;
      receipt.queueCalls += 1;
      const originalOutput = compiled.output ?? {};
      const originalNodes = Object.values(originalOutput);
      const productShells = originalNodes.filter(
        (node) =>
          node.class_type === "comfyui_h3_context.H3Context.ProductShell",
      ).length;
      const requests = originalNodes.filter(
        (node) => node.class_type === "comfyui_h3_context.H3Context.Request",
      ).length;
      const nativeGenerators = originalNodes.filter((node) =>
        new Set(["MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"]).has(
          String(node.class_type ?? ""),
        ),
      ).length;
      const sinks = originalNodes.filter(
        (node) => node.class_type === "SaveVideo",
      ).length;
      const loaderCounts = Object.fromEntries(
        loaderTypes.map((type) => [
          type,
          originalNodes.filter((node) => node.class_type === type).length,
        ]),
      );
      Object.assign(receipt, {
        productShells,
        requests,
        nativeGenerators,
        sinks,
        loaderCounts,
        fullGenerationEnvelope:
          productShells === 1 &&
          requests === 1 &&
          nativeGenerators === 1 &&
          sinks === 1 &&
          loaderTypes.every((type) => loaderCounts[type] === 1),
      });
      if (!receipt.fullGenerationEnvelope)
        throw new Error("the M23-39 generation envelope is not qualified");

      const output = structuredClone(originalOutput);
      const requestEntry = Object.entries(output).find(
        ([, node]) =>
          node.class_type === "comfyui_h3_context.H3Context.Request",
      );
      if (requestEntry === undefined)
        throw new Error("the M23-39 Request dependency is unavailable");
      let nextId = 1;
      while (Object.hasOwn(output, String(nextId))) nextId += 1;
      const primitiveId = String(nextId++);
      while (Object.hasOwn(output, String(nextId))) nextId += 1;
      const mathId = String(nextId);
      output[primitiveId] = {
        class_type: "PrimitiveFloat",
        inputs: { value: 1 },
      };
      output[mathId] = {
        class_type: "ComfyMathExpression",
        inputs: { expression: "1/0", "values.a": [primitiveId, 0] },
      };
      const requestInputs = requestEntry[1].inputs ?? {};
      requestEntry[1].inputs = {
        ...requestInputs,
        duration_seconds: [mathId, 0],
      };

      const beforeLoaderInputs = loaderInputs(originalOutput);
      const afterLoaderInputs = loaderInputs(output);
      runtime.__h3M2339LoaderInputs = beforeLoaderInputs;
      receipt.loaderInputsPreserved =
        JSON.stringify(beforeLoaderInputs) ===
        JSON.stringify(afterLoaderInputs);
      receipt.runtimeDependencyInjected =
        Array.isArray(requestEntry[1].inputs.duration_seconds) &&
        String(requestEntry[1].inputs.duration_seconds[0]) === mathId &&
        requestEntry[1].inputs.duration_seconds[1] === 0;
      if (!receipt.loaderInputsPreserved || !receipt.runtimeDependencyInjected)
        throw new Error("the M23-39 runtime injection violated its boundary");

      const productShellEntry = Object.entries(output).find(
        ([, node]) =>
          node.class_type === "comfyui_h3_context.H3Context.ProductShell",
      );
      if (productShellEntry === undefined)
        throw new Error("the M23-39 ProductShell closure is unavailable");
      const included = new Set<string>();
      const pending = [productShellEntry[0]];
      while (pending.length > 0) {
        const nodeId = pending.pop()!;
        if (included.has(nodeId)) continue;
        const node = output[nodeId];
        if (node === undefined)
          throw new Error("the M23-39 closure references a missing node");
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
      const assetFreeTypes = new Set([
        "comfyui_h3_context.H3Context.Request",
        "comfyui_h3_context.H3Context.Plan",
        "comfyui_h3_context.H3Context.Compiler",
        "comfyui_h3_context.H3Context.Validator",
        "comfyui_h3_context.H3Context.NativeH3Adapter",
        "comfyui_h3_context.H3Context.ProductShell",
        "comfyui_h3_context.H3Context.ReferenceRegistry",
        "PrimitiveFloat",
        "PrimitiveInt",
        "ComfyMathExpression",
      ]);
      receipt.queuedLoaderCount = projectionTypes.filter((type) =>
        loaderTypes.includes(type as never),
      ).length;
      receipt.queuedProjectionAssetFree =
        projectionTypes.length > 0 &&
        projectionTypes.every((type) => assetFreeTypes.has(type)) &&
        receipt.queuedLoaderCount === 0 &&
        !projectionTypes.includes("SaveVideo") &&
        !projectionTypes.some((type) =>
          new Set(["MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"]).has(
            type,
          ),
        );
      if (!receipt.queuedProjectionAssetFree)
        throw new Error("the M23-39 runtime projection is not asset-free");

      // CRITICAL: inject only after the product has compiled and admitted the
      // full generation. The fixed expression must remain a host runtime error;
      // moving it into validation would regress this row to queue_failed. Queue
      // only ProductShell's backward closure so this attribution proof cannot
      // validate, rename or load any model weight from the visible canvas.
      return await originalQueuePrompt(batch, {
        ...compiled,
        output: projectionOutput,
      });
    };
  });

  await container
    .getByRole("textbox", { name: "Intent" })
    .fill("A fixed managed generation used only to classify a host terminal.");
  const submit = container.locator('[data-h3-focus-key="app-submit"]');
  await expect(submit).toBeEnabled({ timeout: 30_000 });
  if (initialCanvas.nodeCount > 0) {
    await expect(submit).toHaveText("Replace canvas and start H3 App Mode");
    const originalCanvasPreserved = await page.evaluate(() => {
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any } };
        __h3M2339InitialCanvas?: string;
        __h3M2339InitialLoaderWidgets?: string[];
      };
      const serialized = runtime.comfyAPI.app.app.graph.serialize();
      const loaderTypes = new Set([
        "UNETLoader",
        "CLIPLoader",
        "LoraLoaderModelOnly",
      ]);
      const loaderWidgets = (serialized?.nodes ?? [])
        .filter((node: { type?: unknown }) =>
          loaderTypes.has(String(node.type ?? "")),
        )
        .map(
          (node: { id?: unknown; type?: unknown; widgets_values?: unknown }) =>
            JSON.stringify([
              String(node.id ?? ""),
              String(node.type ?? ""),
              Array.isArray(node.widgets_values) ? node.widgets_values : [],
            ]),
        )
        .sort();
      return (
        JSON.stringify(serialized) === runtime.__h3M2339InitialCanvas &&
        JSON.stringify(loaderWidgets) ===
          JSON.stringify(runtime.__h3M2339InitialLoaderWidgets ?? [])
      );
    });
    // CRITICAL: the replacement label is the consent boundary. A dirty canvas
    // and its user-selected loader widgets must stay byte-for-byte unchanged
    // until that explicitly labelled action is taken.
    expect(originalCanvasPreserved).toBe(true);
  } else await expect(submit).toHaveText("Start H3 App Mode");
  const fromSubmission = (await sampleProgress(page)).submitted;
  await submit.click();
  await expect
    .poll(async () => (await sampleProgress(page, fromSubmission)).started, {
      timeout: 180_000,
    })
    .toBeGreaterThan(0);
  const result = await awaitHostExecution(page, fromSubmission, 180_000);
  expect(result.errors).toHaveLength(1);
  expect(result.outputs).toEqual([]);

  await expect(container.locator('[data-shell-status="error"]')).toHaveCount(
    1,
    { timeout: 60_000 },
  );
  await expect(container.locator('[role="alert"] > p')).toHaveText(
    "The H3 execution failed on the host.",
  );
  const visibleFailure = (await container.textContent()) ?? "";
  for (const forbidden of [
    "1/0",
    "division by zero",
    "ZeroDivisionError",
    "Traceback",
    "execution_failed",
    "queue_failed",
    "ambiguous_host_ownership",
  ])
    expect(visibleFailure).not.toContain(forbidden);

  const receipt = await page.evaluate(async (submissionOffset) => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M2339FailureReceipt?: Record<string, unknown>;
      __h3M2339LoaderInputs?: string[];
      __h3Sample?: { promptIds?: string[] };
      __h3M2339Diagnostics?: string;
    };
    const current = await runtime.comfyAPI.app.app.graphToPrompt();
    const loaderTypes = new Set([
      "UNETLoader",
      "CLIPLoader",
      "LoraLoaderModelOnly",
    ]);
    const currentLoaderInputs = Object.entries(current.output ?? {})
      .filter(([, node]) =>
        loaderTypes.has(
          String((node as { class_type?: unknown }).class_type ?? ""),
        ),
      )
      .map(([id, node]) => {
        const candidate = node as {
          class_type?: unknown;
          inputs?: Record<string, unknown>;
        };
        return JSON.stringify([
          id,
          candidate.class_type,
          candidate.inputs ?? {},
        ]);
      })
      .sort();
    return {
      failure: runtime.__h3M2339FailureReceipt,
      canvasLoaderInputsPreserved:
        JSON.stringify(currentLoaderInputs) ===
        JSON.stringify(runtime.__h3M2339LoaderInputs ?? []),
      promptIds: (runtime.__h3Sample?.promptIds ?? []).slice(submissionOffset),
    };
  }, fromSubmission);
  expect(receipt.failure).toEqual({
    queueCalls: 1,
    fullGenerationEnvelope: true,
    productShells: 1,
    requests: 1,
    nativeGenerators: 1,
    sinks: 1,
    loaderCounts: {
      UNETLoader: 1,
      CLIPLoader: 1,
      LoraLoaderModelOnly: 1,
    },
    loaderInputsPreserved: true,
    runtimeDependencyInjected: true,
    queuedProjectionAssetFree: true,
    queuedLoaderCount: 0,
  });
  expect(receipt.canvasLoaderInputsPreserved).toBe(true);
  expect(receipt.promptIds).toHaveLength(1);

  const acceptedPromptId = receipt.promptIds[0]!;
  const historyResponse = await page.request.get(
    new URL(`/history/${encodeURIComponent(acceptedPromptId)}`, hostUrl).href,
  );
  expect(historyResponse.ok()).toBe(true);
  const history = (await historyResponse.json()) as Record<
    string,
    { prompt?: unknown[]; status?: { status_str?: unknown } }
  >;
  const accepted = history[acceptedPromptId];
  const acceptedOutput = accepted?.prompt?.[2] as
    Record<string, { class_type?: unknown }> | undefined;
  const acceptedTypes = Object.values(acceptedOutput ?? {}).map((node) =>
    String(node.class_type ?? ""),
  );
  expect(accepted?.status?.status_str).toBe("error");
  expect(
    acceptedTypes.filter((type) => type === "ComfyMathExpression"),
  ).toHaveLength(1);
  expect(
    acceptedTypes.filter(
      (type) => type === "comfyui_h3_context.H3Context.ProductShell",
    ),
  ).toHaveLength(1);
  expect(acceptedTypes.filter((type) => type === "SaveVideo")).toHaveLength(0);
  expect(
    acceptedTypes.filter((type) =>
      new Set(["UNETLoader", "CLIPLoader", "LoraLoaderModelOnly"]).has(type),
    ),
  ).toHaveLength(0);
  expect(
    acceptedTypes.filter((type) =>
      new Set(["MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"]).has(type),
    ),
  ).toHaveLength(0);

  await page.evaluate(() => {
    const runtime = window as unknown as { __h3M2339Diagnostics?: string };
    runtime.__h3M2339Diagnostics = undefined;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (payload: string) => {
          runtime.__h3M2339Diagnostics = payload;
        },
      },
    });
  });
  await container.getByRole("button", { name: "Copy diagnostics" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { __h3M2339Diagnostics?: string })
            .__h3M2339Diagnostics,
      ),
    )
    .not.toBeUndefined();
  const diagnostics = await page.evaluate(
    () =>
      (window as unknown as { __h3M2339Diagnostics?: string })
        .__h3M2339Diagnostics ?? "",
  );
  expect(diagnostics).toContain(
    "error app_mode code=execution_failed category=transaction",
  );
  expect(diagnostics).toContain("state error code=execution_failed");
  for (const forbidden of [
    "1/0",
    "division by zero",
    "ZeroDivisionError",
    "Traceback",
    "queue_failed",
    "ambiguous_host_ownership",
  ])
    expect(diagnostics).not.toContain(forbidden);

  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);
});
