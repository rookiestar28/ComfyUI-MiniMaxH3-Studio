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

test("M24-05 supported host projects explicit guide readiness without model generation", async ({
  context,
  page,
}) => {
  test.setTimeout(90_000);
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
  const appModeContainer = await openH3AppModeTab(
    page,
    "h3-context-m24-05-guide-readiness",
  );
  await waitForHostGraphSettled(page);
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );

  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: {
        app: { app: any };
        api: { api: { addEventListener: Function; queuePrompt: Function } };
      };
      __h3M24Events?: Array<{ node: unknown; workspace: unknown }>;
      __h3M24QueueCalls?: number;
    };
    const api = runtime.comfyAPI.api.api;
    runtime.__h3M24Events = [];
    runtime.__h3M24QueueCalls = 0;
    const originalQueuePrompt = api.queuePrompt;
    api.queuePrompt = function (...args: unknown[]) {
      runtime.__h3M24QueueCalls = (runtime.__h3M24QueueCalls ?? 0) + 1;
      return originalQueuePrompt.apply(this, args);
    };
    api.addEventListener("executed", (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      const output = detail?.output as
        Record<string, unknown[]> | null | undefined;
      const workspace = output?.sidebar_workspace?.[0];
      if (workspace !== undefined)
        runtime.__h3M24Events?.push({ node: detail?.node, workspace });
    });
  });

  type ApiPrompt = Record<
    string,
    { class_type: string; inputs: Record<string, unknown> }
  >;
  const incompletePrompt: ApiPrompt = {
    "1": {
      class_type: "comfyui_h3_context.H3Context.Request",
      inputs: {
        task_mode: "t2va",
        user_intent: "A baker opens a quiet bakery before sunrise.",
        duration_seconds: 5,
      },
    },
    "2": {
      class_type: "comfyui_h3_context.H3Context.ReferenceRegistry",
      inputs: {},
    },
    "3": {
      class_type: "comfyui_h3_context.H3Context.IntentGraphProducer",
      inputs: {
        request: ["1", 0],
        reference_registry: ["2", 0],
        subject_label: "the baker",
        action_description: "opens the bakery",
        complete_silence: false,
      },
    },
    "4": {
      class_type: "comfyui_h3_context.H3Context.Plan",
      inputs: {
        request: ["1", 0],
        reference_registry: ["2", 0],
        intent_graph: ["3", 0],
      },
    },
    "5": {
      class_type: "comfyui_h3_context.H3Context.Compiler",
      inputs: { plan: ["4", 0] },
    },
    "6": {
      class_type: "comfyui_h3_context.H3Context.Validator",
      inputs: { plan: ["4", 0], prompt_document: ["5", 2] },
    },
    "7": {
      class_type: "comfyui_h3_context.H3Context.NativeH3Adapter",
      inputs: { report: ["6", 1] },
    },
    "8": {
      class_type: "comfyui_h3_context.H3Context.ProductShell",
      inputs: { report: ["6", 1], native_h3_wiring: ["7", 1] },
    },
    "9": {
      class_type: "MiniMaxH3ImageToVideo",
      inputs: { prompt: ["8", 0], width: 512, height: 512, length: 124 },
    },
  };
  const readyPrompt = structuredClone(incompletePrompt);
  readyPrompt["3"].inputs.complete_silence = true;
  const workflowIdentity = "m24-05-guide-readiness-model-free-v1";

  await page.evaluate(
    async ({ promptValue, identity }) => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      await app.loadApiJson(promptValue, identity);
    },
    { promptValue: incompletePrompt, identity: workflowIdentity },
  );
  // IMPORTANT: loadApiJson returns before the supported host finishes attaching its
  // workflow authority. Capturing it earlier makes a later stable attachment look like drift.
  await waitForHostGraphSettled(page);
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M24Workflow?: unknown;
      __h3M24OpenTabs?: number;
    };
    const app = runtime.comfyAPI.app.app;
    const store = app.extensionManager?.workflow;
    const activeWorkflow = store?.activeWorkflow;
    if (
      activeWorkflow === null ||
      typeof activeWorkflow !== "object" ||
      Array.isArray(activeWorkflow) ||
      !Array.isArray(store?.openWorkflows) ||
      !store.openWorkflows.includes(activeWorkflow)
    )
      throw new Error("the supported host did not expose an active workflow");
    runtime.__h3M24Workflow = activeWorkflow;
    runtime.__h3M24OpenTabs = store.openWorkflows.length;
  });

  const ownedPromptBinding = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const graph = app.graph.serialize() as {
      nodes?: Array<Record<string, unknown>>;
      links?: unknown[][];
    };
    const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
    const links = Array.isArray(graph.links) ? graph.links : [];
    const shell = nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.ProductShell",
    );
    const anchor = nodes.find((node) => node.type === "MiniMaxH3ImageToVideo");
    const promptInput = (
      Array.isArray(anchor?.inputs) ? anchor.inputs : []
    ).find(
      (input) =>
        input !== null &&
        typeof input === "object" &&
        !Array.isArray(input) &&
        (input as Record<string, unknown>).name === "prompt",
    ) as Record<string, unknown> | undefined;
    const link = links.find((row) => row[0] === promptInput?.link);
    const shellOutputs = Array.isArray(shell?.outputs) ? shell.outputs : [];
    const anchorInputs = Array.isArray(anchor?.inputs) ? anchor.inputs : [];
    const output =
      link === undefined ? undefined : shellOutputs[Number(link[2])];
    const input =
      link === undefined ? undefined : anchorInputs[Number(link[4])];
    return {
      shell_id: shell?.id ?? null,
      anchor_id: anchor?.id ?? null,
      origin_id: link?.[1] ?? null,
      target_id: link?.[3] ?? null,
      origin_slot:
        output !== null && typeof output === "object"
          ? ((output as Record<string, unknown>).name ?? null)
          : null,
      target_slot:
        input !== null && typeof input === "object"
          ? ((input as Record<string, unknown>).name ?? null)
          : null,
    };
  });
  expect(ownedPromptBinding).toEqual({
    shell_id: ownedPromptBinding.shell_id,
    anchor_id: ownedPromptBinding.anchor_id,
    origin_id: ownedPromptBinding.shell_id,
    target_id: ownedPromptBinding.anchor_id,
    origin_slot: "prompt",
    target_slot: "prompt",
  });
  expect(ownedPromptBinding.shell_id).not.toBeNull();
  expect(ownedPromptBinding.anchor_id).not.toBeNull();

  const executionPrompt = structuredClone(incompletePrompt);
  delete executionPrompt["9"];
  expect(
    Object.values(executionPrompt).some((node) =>
      /^MiniMaxH3(?:Image|Reference)ToVideo$/.test(node.class_type),
    ),
  ).toBe(false);
  const submitModelFree = async (promptValue: ApiPrompt) =>
    page.evaluate(
      async ({ endpoint, value }) => {
        const api = (
          window as unknown as {
            comfyAPI: { api: { api: { clientId: string } } };
          }
        ).comfyAPI.api.api;
        const response = await fetch(endpoint, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ client_id: api.clientId, prompt: value }),
        });
        return { status: response.status, text: await response.text() };
      },
      { endpoint: new URL("/prompt", hostUrl).href, value: promptValue },
    );

  const incompleteResponse = await submitModelFree(executionPrompt);
  expect(incompleteResponse.status, incompleteResponse.text).toBe(200);
  const incompleteReceipt = JSON.parse(incompleteResponse.text) as {
    prompt_id?: unknown;
  };
  await page.waitForFunction(
    () =>
      ((window as unknown as { __h3M24Events?: unknown[] }).__h3M24Events
        ?.length ?? 0) >= 1,
  );
  const incompleteEvent = await page.evaluate(
    () =>
      (
        window as unknown as {
          __h3M24Events?: Array<{ node: unknown; workspace: unknown }>;
        }
      ).__h3M24Events?.at(-1) ?? null,
  );
  expect(incompleteEvent?.node).toBe("8");
  const incompleteWorkspace = decodeSidebarWorkspaceProjection(
    incompleteEvent?.workspace,
  );
  expect(incompleteWorkspace.correlation.prompt_id).toBe(
    incompleteReceipt.prompt_id,
  );
  expect(incompleteWorkspace.correlation.execution_node_id).toBe("8");
  expect(incompleteWorkspace.task_mode).toBe("t2va");
  expect(incompleteWorkspace.lifecycle).toBe("ready");
  expect(incompleteWorkspace.guide_conformance).toEqual({
    schema: "h3.context.guide_conformance.v2",
    readiness: "incomplete",
    reasons: ["fidelity.soundscape.unspecified"],
  });
  await expect(
    appModeContainer.locator('[data-guide-readiness="incomplete"]'),
  ).toHaveCount(1);

  const readyExecutionPrompt = structuredClone(readyPrompt);
  delete readyExecutionPrompt["9"];
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M24Workflow?: unknown;
      __h3M24OpenTabs?: number;
    };
    const app = runtime.comfyAPI.app.app;
    const store = app.extensionManager?.workflow;
    if (
      store?.activeWorkflow !== runtime.__h3M24Workflow ||
      !Array.isArray(store?.openWorkflows) ||
      store.openWorkflows.length !== runtime.__h3M24OpenTabs
    )
      throw new Error("the guide transition lost its captured workflow tab");
    const intent =
      app.graph.getNodeById?.(3) ?? app.graph.getNodeById?.("3") ?? null;
    const widget = intent?.widgets?.find(
      (candidate: { name?: unknown }) => candidate?.name === "complete_silence",
    );
    if (
      intent?.type !== "comfyui_h3_context.H3Context.IntentGraphProducer" ||
      widget === undefined ||
      widget.value !== false
    )
      throw new Error("the explicit complete-silence control is unavailable");
    // IMPORTANT: loadApiJson opens another workflow on the supported host. Change the
    // owned widget in place or this readiness transition invalidates workflow identity.
    widget.value = true;
    app.graph.change?.();
    app.graph.setDirtyCanvas?.(true, true);
    if (
      store.activeWorkflow !== runtime.__h3M24Workflow ||
      store.openWorkflows.length !== runtime.__h3M24OpenTabs
    )
      throw new Error("the guide transition changed the captured workflow tab");
  });
  const readyResponse = await submitModelFree(readyExecutionPrompt);
  expect(readyResponse.status, readyResponse.text).toBe(200);
  const readyReceipt = JSON.parse(readyResponse.text) as {
    prompt_id?: unknown;
  };
  await page.waitForFunction(
    () =>
      ((window as unknown as { __h3M24Events?: unknown[] }).__h3M24Events
        ?.length ?? 0) >= 2,
  );
  const readyEvent = await page.evaluate(
    () =>
      (
        window as unknown as {
          __h3M24Events?: Array<{ node: unknown; workspace: unknown }>;
        }
      ).__h3M24Events?.at(-1) ?? null,
  );
  expect(readyEvent?.node).toBe("8");
  const readyWorkspace = decodeSidebarWorkspaceProjection(
    readyEvent?.workspace,
  );
  expect(readyWorkspace.correlation.prompt_id).toBe(readyReceipt.prompt_id);
  expect(readyWorkspace.correlation.prompt_id).not.toBe(
    incompleteWorkspace.correlation.prompt_id,
  );
  expect(readyWorkspace.task_mode).toBe("t2va");
  expect(readyWorkspace.lifecycle).toBe("ready");
  expect(readyWorkspace.guide_conformance).toEqual({
    schema: "h3.context.guide_conformance.v2",
    readiness: "ready",
    reasons: [],
  });
  await expect(
    appModeContainer.locator('[data-guide-readiness="ready"]'),
  ).toHaveCount(1);

  const hostReceipt = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M24Workflow?: unknown;
      __h3M24OpenTabs?: number;
      __h3M24QueueCalls?: number;
    };
    const store = runtime.comfyAPI.app.app.extensionManager.workflow;
    return {
      workflow_identity_retained:
        store.activeWorkflow === runtime.__h3M24Workflow,
      open_tab_count: store.openWorkflows.length,
      expected_open_tab_count: runtime.__h3M24OpenTabs,
      queue_call_count: runtime.__h3M24QueueCalls ?? 0,
    };
  });
  expect(hostReceipt).toEqual({
    workflow_identity_retained: true,
    open_tab_count: hostReceipt.expected_open_tab_count,
    expected_open_tab_count: hostReceipt.expected_open_tab_count,
    queue_call_count: 0,
  });
  expect(hostReceipt.open_tab_count).toBeGreaterThan(0);
  expect(networkAttribution.snapshot()).toMatchObject({
    interactionRemoteCount: 0,
    interactionProviderCount: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
});
