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
import { decodeProductionAuthoringImportResponse } from "../../../../src/contracts/productionAuthoringImportCodec";
import {
  decodeTimelineHistoryProjection,
  decodeTimelineReceipt,
  encodeAuthoringAction,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import { APP_MODE_ARTIFACT_PREFIX_ROOT } from "../../../../src/host/appMode";
// prettier-ignore
import { MAX_MEDIA_PREVIEW_BYTES, MEDIA_PREVIEW_REQUEST_SCHEMA, MEDIA_PREVIEW_ROUTE, } from "../../../../src/host/productionMediaPreview";
// prettier-ignore
import { readOfficialAssetInventory, resolveOfficialAssets, } from "../../../../src/host/officialAssetResolution";
import { projectionFromOutput } from "../../../../src/host/sidebarHost";
import { OFFICIAL_LENGTH_EXPRESSION } from "../../../../src/host/templateMaterialization";
// prettier-ignore
import { CANDIDATE_BACKEND_HOST_ROOT_ENV, CANDIDATE_BUNDLE_PATH_ENV, CANDIDATE_BUNDLE_SHA256_ENV, CandidateInitiatorNetworkAttribution, H3NetworkAttribution, loadCandidateBundleEnvironment, verifyCandidateBackendRuntimeEnvironment, waitForStartupNetworkQuiet, } from "../../helpers/candidateBundleHarness";
// prettier-ignore
import { diffGraphSurroundings, type SurroundingsDiffReport, } from "../../../support/surroundingsDiff";

// prettier-ignore
import { hostUrl, m23RealI2vaAuthorized, m23RealI2vaHostPython, m23RuntimeFailureAuthorized, m22ReadOnlyComposition, repositoryRoot, officialAssetManifest, candidateBackendMode, candidateBundle, candidateBackendRuntime, CANDIDATE_BUNDLE_RESOURCE_PATH, expectedCoInstallSidebarIds, repositoryUrl, candidateInjectionStates, candidateBundleResourceUrl, candidateInjectionCount, assertCandidateBundleInjection, monitorH3Network, monitorCandidateInitiatorNetwork, waitForH3Registration, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, captureM17CanonicalIdentity, setSupportedH3Language, HOST_PERSISTENCE_KEY_FAMILY, HOST_PERSISTENCE_SENTINEL_VALUE, layoutMatrix, layoutStateContracts, layoutStateCriteria, layoutDiagnostic, assertLayoutMeasurement, SAMPLE_AUTHORIZED, SAMPLE_UNET, SAMPLE_CLIP, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, monitorManagedRouteResponses, appModeQueueDiagnostic, waitForAppModeQueue, privateDiagnosticLeakReceipt, waitForHostGraphSettled, openH3AppModeTab, readVisibleGraph, subgraphNodes, compiledLoaderAssets, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, watchHostExecution, sampleProgress, awaitHostExecution, reportedArtifactNames, preflightRealI2vaEnvironment, inspectRealI2vaArtifactUnsafe, inspectRealI2vaArtifact, type HostOfficialAssetManifest, type CandidateInjectionState, type FrontendPerformanceReceipt, type LayoutState, type AppModePhase, type LayoutGridKind, type LayoutVariant, type LayoutPlacement, type LayoutWidthMode, type LayoutLocale, type LayoutTheme, type LayoutLifecycle, type HostPersistenceSnapshot, type LayoutCaptureEvidence, type LayoutEvidenceJoin, type LayoutEvidenceRow, type LayoutCell, type LayoutStateContract, type LayoutRect, type LayoutOwnerStyle, type LayoutControlGeometry, type LayoutNavigationTab, type LayoutHeaderGeometry, type LayoutFocusTargetKind, type LayoutMeasurement, type SerializedGraph, type ManagedRouteReceipt, type HostAssetResolutionReceipt, type SampleProgress, type RealI2vaArtifactMetadata, type RealI2vaEnvironment, } from "../../host/environment";

test("M25-38 supplied host excludes failed Starts and commits ordinary Start plus Retry outputs once", async ({
  context,
  page,
}, testInfo) => {
  test.skip(
    !m23RealI2vaAuthorized ||
      !m23RuntimeFailureAuthorized ||
      candidateBundle === null ||
      candidateBackendRuntime === null ||
      candidateBackendMode !== "exact" ||
      m23RealI2vaHostPython.length === 0,
    "the failure-safe live T2VA row requires explicit success/failure authorization and an exact candidate",
  );
  test.setTimeout(30 * 60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const hostRoot = process.env[CANDIDATE_BACKEND_HOST_ROOT_ENV];
  if (hostRoot === undefined)
    throw new Error("the explicit host root is required");
  const liveEnvironment = await preflightRealI2vaEnvironment(
    hostRoot,
    m23RealI2vaHostPython,
  );
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const managedResponseDiagnostics: Array<{
    route: "production" | "coordinator";
    action: string;
    status: number;
    category: string | null;
    disposition: string | null;
    schema: string | null;
  }> = [];
  const managedPreparations: Array<{
    graphFingerprint: string;
    compiledPromptFingerprint: string;
    ownedProjectionFingerprint: string;
    ownedNodeIds: string[];
    ownedLinkIds: string[];
    anchorNodeId: string;
  }> = [];
  const successfulPreparations: typeof managedPreparations = [];
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
  page.on("response", (response) => {
    const pathname = new URL(response.url()).pathname;
    const route: "production" | "coordinator" | undefined = pathname.endsWith(
      "/h3-context/v1/production/action",
    )
      ? "production"
      : pathname.endsWith("/h3-context/v1/generation/coordinator")
        ? "coordinator"
        : undefined;
    if (route === undefined) return;
    let action = "unavailable";
    try {
      const request = JSON.parse(response.request().postData() ?? "null") as {
        action?: unknown;
      };
      if (typeof request?.action === "string") action = request.action;
    } catch {
      // The fixed fallback is sufficient for a content-free failure trace.
    }
    const receipt: (typeof managedResponseDiagnostics)[number] = {
      route,
      action,
      status: response.status(),
      category: null,
      disposition: null,
      schema: null,
    };
    managedResponseDiagnostics.push(receipt);
    void response
      .json()
      .then((body: unknown) => {
        if (body === null || typeof body !== "object" || Array.isArray(body))
          return;
        const wire = body as Record<string, unknown>;
        if (typeof wire.category === "string") receipt.category = wire.category;
        if (typeof wire.disposition === "string")
          receipt.disposition = wire.disposition;
        if (typeof wire.schema === "string") receipt.schema = wire.schema;
      })
      .catch(() => undefined);
  });
  const initialInjectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  const packCensus = await page.evaluate(() => {
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
  await instrumentHostGraphLoads(page);
  await waitForHostGraphSettled(page);
  const workflowAuthority = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M2319WorkflowAuthority?: object;
    };
    const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
    const active = store?.activeWorkflow;
    const open = Array.isArray(store?.openWorkflows)
      ? store.openWorkflows
      : null;
    if (
      active === null ||
      typeof active !== "object" ||
      open === null ||
      open.length !== 1 ||
      open[0] !== active
    )
      return { captured: false, openCount: open?.length ?? -1 };
    runtime.__h3M2319WorkflowAuthority = active;
    return { captured: true, openCount: open.length };
  });
  expect(workflowAuthority).toEqual({ captured: true, openCount: 1 });
  await resetHostGraphLoads(page);
  await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    app.graph.configure({
      last_node_id: 0,
      last_link_id: 0,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
      version: 0.4,
    });
    app.graph.setDirtyCanvas?.(true, true);
  });
  await waitForHostGraphSettled(page);
  const container = await openH3AppModeTab(page, "h3-context-m23-19-live");
  for (const focusKey of ["page-context", "page-production"] as const)
    await expect(
      container.locator(`[data-h3-focus-key="${focusKey}"]`),
    ).toHaveCount(1);
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await watchHostExecution(page);
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { api: { api: any } };
      __h3M2319QueueKinds?: string[];
      __h3M2319WorkflowAuthority?: object;
      __h3M2538FailNext?: boolean;
    };
    runtime.__h3M2319QueueKinds = [];
    const api = runtime.comfyAPI.api.api;
    const original = api.queuePrompt.bind(api);
    api.queuePrompt = async function (...args: unknown[]) {
      const envelope = args[1] as
        | {
            output?: Record<
              string,
              { class_type?: unknown; inputs?: Record<string, unknown> }
            >;
          }
        | undefined;
      const types = Object.values(envelope?.output ?? {}).map((node) =>
        String(node.class_type ?? ""),
      );
      const native =
        types.includes("comfyui_h3_context.H3Context.ProductShell") &&
        types.includes("MiniMaxH3ImageToVideo") &&
        types.includes("SaveVideo");
      if (!native)
        throw new Error(
          "the live queue envelope is not one managed T2VA prompt",
        );
      let submitted = args;
      let kind = "native";
      if (runtime.__h3M2538FailNext === true) {
        runtime.__h3M2538FailNext = false;
        const output = structuredClone(envelope?.output ?? {});
        const request = Object.entries(output).find(
          ([, node]) =>
            node.class_type === "comfyui_h3_context.H3Context.Request",
        );
        const shell = Object.entries(output).find(
          ([, node]) =>
            node.class_type === "comfyui_h3_context.H3Context.ProductShell",
        );
        if (request === undefined || shell === undefined)
          throw new Error("the controlled failure authority is unavailable");
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
        request[1].inputs = {
          ...((request[1] as { inputs?: Record<string, unknown> }).inputs ??
            {}),
          duration_seconds: [mathId, 0],
        };
        const included = new Set<string>();
        const pending = [shell[0]];
        while (pending.length > 0) {
          const nodeId = pending.pop()!;
          if (included.has(nodeId)) continue;
          const node = output[nodeId];
          if (node === undefined)
            throw new Error("the controlled failure closure is invalid");
          included.add(nodeId);
          for (const value of Object.values(
            (node as { inputs?: Record<string, unknown> }).inputs ?? {},
          ))
            if (
              Array.isArray(value) &&
              value.length === 2 &&
              (typeof value[0] === "string" || typeof value[0] === "number") &&
              Object.hasOwn(output, String(value[0]))
            )
              pending.push(String(value[0]));
        }
        submitted = [
          args[0],
          {
            ...(args[1] as object),
            output: Object.fromEntries(
              Object.entries(output).filter(([id]) => included.has(id)),
            ),
          },
          ...args.slice(2),
        ];
        kind = "runtime_failure";
      }
      const result = await original(...submitted);
      runtime.__h3M2319QueueKinds?.push(kind);
      return result;
    };
  });

  const managedJournalBaselineSequence = await page.evaluate(() => {
    try {
      const raw = localStorage.getItem("h3-context.managed-journal.v1");
      if (raw === null) return 0;
      const parsed = JSON.parse(raw) as { entries?: unknown };
      if (!Array.isArray(parsed.entries)) return 0;
      return parsed.entries.reduce((maximum, value) => {
        if (value === null || typeof value !== "object") return maximum;
        const sequence = (value as { seq?: unknown }).seq;
        return typeof sequence === "number" && Number.isSafeInteger(sequence)
          ? Math.max(maximum, sequence)
          : maximum;
      }, 0);
    } catch {
      return 0;
    }
  });

  let surroundingsEvidence: SurroundingsDiffReport | undefined;
  let surroundingsBaseline:
    Awaited<ReturnType<Page["evaluateHandle"]>> | undefined;
  const startRun = async (
    ordinal: number,
    trigger: "ordinary" | "retry" = "ordinary",
  ): Promise<{
    reportedName: string;
    metadata: RealI2vaArtifactMetadata;
    openWorkflowDelta: number;
    queueKinds: string[];
    previewedHandles: string[];
  }> => {
    const preparationOffset = managedPreparations.length;
    const authorityBefore = await page.evaluate(() => {
      const store = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app.extensionManager?.workflow;
      return {
        openCount: Array.isArray(store?.openWorkflows)
          ? store.openWorkflows.length
          : -1,
        sameWorkflow:
          (
            window as unknown as {
              __h3M2319WorkflowAuthority?: object;
            }
          ).__h3M2319WorkflowAuthority === store?.activeWorkflow,
      };
    });
    expect(authorityBefore.sameWorkflow).toBe(true);
    const queueKindOffset = await page.evaluate(
      () =>
        (window as unknown as { __h3M2319QueueKinds?: string[] })
          .__h3M2319QueueKinds?.length ?? 0,
    );
    if (ordinal === 2 && trigger === "ordinary") {
      await container.locator('[data-h3-focus-key="page-context"]').click();
      const edit = container.locator(
        '[data-h3-focus-key="edit-app-mode-setup"]',
      );
      await expect(edit).toBeVisible({ timeout: 30_000 });
      await edit.click();
    }
    if (trigger === "ordinary") {
      const taskMode = container.locator('[data-h3-focus-key="app-task-mode"]');
      await expect(taskMode).toBeVisible({ timeout: 30_000 });
      await taskMode.selectOption("t2va");
      await container
        .locator('[data-h3-focus-key="app-intent"]')
        .fill("Preserve the supplied source geometry in this controlled run.");
      await container
        .locator('[data-h3-focus-key="app-duration-seconds"]')
        .fill("8");
      await expect(
        container.getByText("Delivers 8 s (192 frames).", { exact: true }),
      ).toBeVisible();
    }

    const fromSubmission = (await sampleProgress(page)).submitted;
    const submit = container.locator(
      trigger === "ordinary"
        ? '[data-h3-focus-key="app-submit"]'
        : '[data-h3-focus-key="error-recovery"]',
    );
    await expect(submit).toBeEnabled({ timeout: 120_000 });
    // IMPORTANT: one click is one explicit operator start. Never retry the click
    // inside this live row; a delayed prompt must fail instead of risking a duplicate.
    await submit.click();
    const submissionDeadline = Date.now() + 180_000;
    for (;;) {
      const submitted = (await sampleProgress(page)).submitted - fromSubmission;
      if (submitted === 1) break;
      const diagnostic = await appModeQueueDiagnostic(
        page,
        "h3-context-m23-19-live",
        "__h3M2319QueueKinds",
      );
      if (diagnostic.shellStatus === "error") {
        const identity = await page.evaluate(() => {
          const runtime = window as unknown as {
            comfyAPI: { app: { app: any } };
            __h3M2319WorkflowAuthority?: object;
          };
          const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
          return {
            sameWorkflow:
              runtime.__h3M2319WorkflowAuthority === store?.activeWorkflow,
            openCount: Array.isArray(store?.openWorkflows)
              ? store.openWorkflows.length
              : -1,
          };
        });
        await page.waitForTimeout(100);
        throw new Error(
          `M23-19 live submission failed before host ownership: ${diagnostic.errorClass}/${diagnostic.shellReason}; trace=${JSON.stringify(diagnostic.managedTrace)}; identity=${JSON.stringify(identity)}; responses=${JSON.stringify(managedResponseDiagnostics)}`,
        );
      }
      if (Date.now() >= submissionDeadline)
        throw new Error(
          `M23-19 live submission timed out: ${diagnostic.shellStatus}/${diagnostic.shellReason}; trace=${JSON.stringify(diagnostic.managedTrace)}`,
        );
      await page.waitForTimeout(250);
    }
    await expect
      .poll(async () => (await sampleProgress(page, fromSubmission)).started, {
        timeout: 180_000,
      })
      .toBe(1);
    await expect
      .poll(async () => (await sampleProgress(page, fromSubmission)).finished, {
        timeout: 20 * 60_000,
        intervals: [1_000, 2_000, 5_000],
      })
      .toBe(1);
    const progress = await sampleProgress(page, fromSubmission);
    expect(progress.errors).toEqual([]);
    const names = reportedArtifactNames(progress.outputs);
    if (names.length !== 1)
      throw new Error("the live run did not report exactly one artifact");
    try {
      await expect(
        container.locator('[data-shell-status="projected"]'),
      ).toHaveCount(1, { timeout: 180_000 });
    } catch (error) {
      await page.waitForTimeout(100);
      const diagnostic = await appModeQueueDiagnostic(
        page,
        "h3-context-m23-19-live",
        "__h3M2319QueueKinds",
      );
      console.info(
        "M25_37_ACCUMULATION_DIAGNOSTIC " +
          JSON.stringify({
            shellStatus: diagnostic.shellStatus,
            shellReason: diagnostic.shellReason,
            errorClass: diagnostic.errorClass,
            managedTrace: diagnostic.managedTrace,
            responses: managedResponseDiagnostics,
          }),
      );
      throw error;
    }
    await container.locator('[data-h3-focus-key="page-production"]').click();
    await expect(
      container.getByText(`${ordinal} of ${ordinal} segments`, { exact: true }),
    ).toBeVisible({
      timeout: 60_000,
    });
    await expect(
      container.locator('[data-segment-index][data-state="succeeded"]'),
    ).toHaveCount(ordinal);
    await expect(
      container.getByText(`Project 1 · ${ordinal} segments`, { exact: true }),
    ).toBeVisible();
    await expect(
      container.getByText(`Added segment ${ordinal}`, { exact: true }),
    ).toBeVisible();
    await expect(container).not.toContainText(
      "Sequence authority is unavailable",
    );
    await expect(container.locator('[role="alert"]')).toHaveCount(0);

    const previewedHandles: string[] = [];
    for (let previewOrdinal = 1; previewOrdinal <= ordinal; previewOrdinal++) {
      // IMPORTANT: preview only the already-authorized outputs. Every preview
      // must consume the bounded owned route without crossing queuePrompt.
      const queueCountBeforePreview = await page.evaluate(
        () =>
          (window as unknown as { __h3M2319QueueKinds?: string[] })
            .__h3M2319QueueKinds?.length ?? 0,
      );
      const previewResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          // B-M1605-HARNESS-03: the host's fetchApi prefixes `/api`.
          new URL(response.url()).pathname.endsWith(MEDIA_PREVIEW_ROUTE),
        { timeout: 60_000 },
      );
      const clipOpeners = container.getByRole("button", {
        name: `Preview segment ${previewOrdinal}`,
      });
      await expect(clipOpeners).toHaveCount(2);
      await clipOpeners.first().click();
      const previewResponse = await previewResponsePromise;
      const requestBody = previewResponse.request().postDataJSON() as {
        schema?: unknown;
        output_handle?: unknown;
      };
      const contentLengthText =
        await previewResponse.headerValue("content-length");
      const contentLength = Number(contentLengthText);
      expect({
        status: previewResponse.status(),
        schema: requestBody.schema,
        opaqueHandle:
          typeof requestBody.output_handle === "string" &&
          /^out_[A-Za-z0-9_-]{16,128}$/.test(requestBody.output_handle),
        contentType: await previewResponse.headerValue("content-type"),
        noStore: await previewResponse.headerValue("cache-control"),
        bounded:
          Number.isSafeInteger(contentLength) &&
          contentLength > 0 &&
          contentLength <= MAX_MEDIA_PREVIEW_BYTES,
      }).toEqual({
        status: 200,
        schema: MEDIA_PREVIEW_REQUEST_SCHEMA,
        opaqueHandle: true,
        contentType: "video/mp4",
        noStore: "no-store",
        bounded: true,
      });
      if (typeof requestBody.output_handle !== "string")
        throw new Error(
          "the preview request did not carry an opaque output handle",
        );
      previewedHandles.push(requestBody.output_handle);
      await expect(
        container.getByLabel(`Segment ${previewOrdinal} preview`),
      ).toBeVisible({ timeout: 60_000 });
      expect(
        await page.evaluate(
          () =>
            (window as unknown as { __h3M2319QueueKinds?: string[] })
              .__h3M2319QueueKinds?.length ?? 0,
        ),
      ).toBe(queueCountBeforePreview);
      await container.getByRole("button", { name: "Close preview" }).click();
      await expect(
        container.getByLabel(`Segment ${previewOrdinal} preview`),
      ).toHaveCount(0);
    }

    const workflowReceipt = await page.evaluate(() => {
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any } };
        __h3M2319WorkflowAuthority?: object;
      };
      const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
      const active = store?.activeWorkflow;
      return {
        openCount: Array.isArray(store?.openWorkflows)
          ? store.openWorkflows.length
          : -1,
        sameWorkflow: runtime.__h3M2319WorkflowAuthority === active,
      };
    });
    expect(workflowReceipt).toEqual({ openCount: 1, sameWorkflow: true });
    await expect
      .poll(() => managedPreparations.length)
      .toBe(preparationOffset + 1);
    const preparation = managedPreparations[preparationOffset]!;
    successfulPreparations.push(preparation);
    for (const fingerprint of [
      preparation.graphFingerprint,
      preparation.compiledPromptFingerprint,
      preparation.ownedProjectionFingerprint,
    ])
      expect(fingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(preparation.ownedNodeIds.every((id) => typeof id === "string")).toBe(
      true,
    );
    expect(preparation.ownedLinkIds.every((id) => typeof id === "string")).toBe(
      true,
    );
    if (ordinal === 1) {
      surroundingsBaseline = await page.evaluateHandle(() => {
        const app = (window as unknown as { comfyAPI: { app: { app: any } } })
          .comfyAPI.app.app;
        return structuredClone(app.graph.serialize());
      });
    } else {
      const firstPreparation = successfulPreparations[0]!;
      if (surroundingsBaseline === undefined)
        throw new Error("the first surroundings snapshot is unavailable");
      const currentGraph = await page.evaluateHandle(() => {
        const app = (window as unknown as { comfyAPI: { app: { app: any } } })
          .comfyAPI.app.app;
        return app.graph.serialize();
      });
      surroundingsEvidence = await page.evaluate(diffGraphSurroundings, {
        beforeValue: surroundingsBaseline,
        afterValue: currentGraph,
        reference: {
          ownedNodeIds: firstPreparation.ownedNodeIds,
          ownedLinkIds: firstPreparation.ownedLinkIds,
          anchorNodeId: firstPreparation.anchorNodeId,
          ownedProjectionEqual:
            firstPreparation.ownedProjectionFingerprint ===
            preparation.ownedProjectionFingerprint,
        },
      });
      await currentGraph.dispose();
      await surroundingsBaseline.dispose();
      surroundingsBaseline = undefined;
    }
    const queueKinds = await page.evaluate(
      (offset) =>
        (
          (window as unknown as { __h3M2319QueueKinds?: string[] })
            .__h3M2319QueueKinds ?? []
        ).slice(offset),
      queueKindOffset,
    );
    expect(queueKinds).toEqual(["native"]);
    const metadata = await inspectRealI2vaArtifact(
      liveEnvironment.hostRoot,
      liveEnvironment.hostPython,
      names[0]!,
    );
    // The frame count is the one geometry fact this repository authored (the
    // sidebar duration); width and height are recorded as observed.
    expect({
      geometryObserved: metadata.width > 0 && metadata.height > 0,
      frames: metadata.frames,
      fpsMatches: Math.abs(metadata.fps - 24) < 0.01,
      mp4: metadata.format.includes("mp4"),
      codecPresent: metadata.codec.length > 0,
      nonempty: metadata.size > 0,
    }).toEqual({
      geometryObserved: true,
      frames: 192,
      fpsMatches: true,
      mp4: true,
      codecPresent: true,
      nonempty: true,
    });
    return {
      reportedName: names[0]!,
      metadata,
      openWorkflowDelta: workflowReceipt.openCount - authorityBefore.openCount,
      queueKinds,
      previewedHandles,
    };
  };

  const failRun = async (
    committedSegments: number,
    recovery: "edit" | "retry",
  ): Promise<void> => {
    if (committedSegments > 0) {
      await container.locator('[data-h3-focus-key="page-context"]').click();
      const edit = container.locator(
        '[data-h3-focus-key="edit-app-mode-setup"]',
      );
      await expect(edit).toBeVisible({ timeout: 30_000 });
      await edit.click();
    }
    const taskMode = container.locator('[data-h3-focus-key="app-task-mode"]');
    await expect(taskMode).toBeVisible({ timeout: 30_000 });
    await taskMode.selectOption("t2va");
    await container
      .locator('[data-h3-focus-key="app-intent"]')
      .fill("Exercise one owned host runtime failure without publishing it.");
    await container
      .locator('[data-h3-focus-key="app-duration-seconds"]')
      .fill("8");
    const queueOffset = await page.evaluate(() => {
      const runtime = window as unknown as {
        __h3M2319QueueKinds?: string[];
        __h3M2538FailNext?: boolean;
      };
      runtime.__h3M2538FailNext = true;
      return runtime.__h3M2319QueueKinds?.length ?? 0;
    });
    const fromSubmission = (await sampleProgress(page)).submitted;
    const submit = container.locator('[data-h3-focus-key="app-submit"]');
    await expect(submit).toBeEnabled({ timeout: 120_000 });
    await submit.click();
    await expect
      .poll(async () => (await sampleProgress(page, fromSubmission)).started, {
        timeout: 180_000,
      })
      .toBe(1);
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
    expect(
      await page.evaluate(
        (offset) =>
          (
            (window as unknown as { __h3M2319QueueKinds?: string[] })
              .__h3M2319QueueKinds ?? []
          ).slice(offset),
        queueOffset,
      ),
    ).toEqual(["runtime_failure"]);
    await container.locator('[data-h3-focus-key="page-production"]').click();
    if (committedSegments === 0) {
      await expect(
        container.getByText("No generated segments yet.", { exact: true }),
      ).toBeVisible({ timeout: 60_000 });
      await expect(
        container.locator('[data-testid="production-empty-project"] + p'),
      ).toHaveText("Latest attempt: Failed.");
      await expect(container.locator("[data-segment-index]")).toHaveCount(0);
    } else {
      await expect(
        container.getByText(
          `${committedSegments} of ${committedSegments} segments`,
          { exact: true },
        ),
      ).toBeVisible({ timeout: 60_000 });
      await expect(
        container.locator('[data-segment-index][data-state="succeeded"]'),
      ).toHaveCount(committedSegments);
      await expect(
        container.getByText(`Project 1 · ${committedSegments} segments`, {
          exact: true,
        }),
      ).toBeVisible();
    }
    await container.locator('[data-h3-focus-key="page-context"]').click();
    const recoveryControl = container.locator(
      recovery === "retry"
        ? '[data-h3-focus-key="error-recovery"]'
        : '[data-h3-focus-key="edit-app-mode-setup"]',
    );
    await expect(recoveryControl).toBeVisible({ timeout: 30_000 });
    if (recovery === "edit") await recoveryControl.click();
  };

  await failRun(0, "edit");
  const first = await startRun(1);
  const importNativeOutput = async (
    outputHandle: string,
    expectedEnsureStatus: number,
    insert: boolean,
  ) => {
    const queueBeforeImport = await page.evaluate(
      () =>
        (window as unknown as { __h3M2319QueueKinds?: string[] })
          .__h3M2319QueueKinds ?? [],
    );
    const ensured = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith(
          "/h3-context/v1/authoring/action",
        ) &&
        response.request().postDataJSON()?.action ===
          "ensure_authoring_from_production",
      { timeout: 60_000 },
    );
    const imported = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith(
          "/h3-context/v1/production/authoring-import",
        ),
      { timeout: 60_000 },
    );
    const importControl = container.locator(
      '[data-h3-nle-control="asset.import_production"]',
    );
    await expect(importControl).toBeEnabled();
    await importControl.click();
    expect((await ensured).status()).toBe(expectedEnsureStatus);
    const response = await imported;
    expect(response.status()).toBe(200);
    const result = decodeProductionAuthoringImportResponse(
      await response.json(),
    );
    expect(result.receipt.rows).toHaveLength(1);
    expect(result.receipt.rows[0]!.outputHandle).toBe(outputHandle);
    const assetId = result.receipt.rows[0]!.assetId;
    await container.locator('[data-h3-nle-entry="open"]').click();
    const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
    await expect(overlay).toBeVisible({ timeout: 30_000 });
    const asset = overlay.locator(
      `[data-h3-nle-region="asset-bin"] [data-h3-nle-asset="${assetId}"]`,
    );
    await expect(asset).toBeVisible();
    let clipCount = -1;
    if (insert) {
      const inserted = page.waitForResponse(
        (reply) =>
          new URL(reply.url()).pathname.endsWith(
            "/h3-context/v1/authoring/action",
          ) &&
          reply.request().postDataJSON()?.action ===
            "apply_timeline_transaction",
        { timeout: 60_000 },
      );
      await asset.locator('[data-h3-nle-control="asset.insert"]').click();
      const insertResponse = await inserted;
      expect(insertResponse.status()).toBe(200);
      const insertedSnapshot = decodeTimelineReceipt(
        await insertResponse.json(),
      ).snapshot;
      clipCount = insertedSnapshot.clips.length;
      expect(clipCount).toBe(1);
      // AC40-07 usable preview: the monitor decodes the normalized native clip into visible
      // pixels at a seeked frame inside it. An insertion receipt alone proves no playback.
      const insertedClip = insertedSnapshot.clips[0]!;
      expect(insertedClip.assetId).toBe(assetId);
      const monitorCanvas = overlay.locator(".h3-nle-monitor canvas");
      await expect(monitorCanvas).toHaveCount(1, { timeout: 60_000 });
      const seekFrame = insertedClip.startFrame + 12;
      const seek = overlay.locator('[data-h3-nle-control="transport.seek"]');
      await seek.fill(String(seekFrame));
      await expect(seek).toHaveAttribute(
        "aria-valuetext",
        new RegExp(`Frame ${seekFrame} of `),
      );
      await expect
        .poll(
          () =>
            monitorCanvas.evaluate((canvas) => {
              const target = canvas as HTMLCanvasElement;
              const data = target
                .getContext("2d")!
                .getImageData(0, 0, target.width, target.height).data;
              let colors = 0;
              for (let index = 0; index < data.length; index += 64)
                if (data[index]! + data[index + 1]! + data[index + 2]! > 40)
                  colors++;
              return colors;
            }),
          { timeout: 60_000 },
        )
        .toBeGreaterThan(100);
    }
    await overlay.locator('[data-h3-nle-action="close"]').click();
    await expect(overlay).toHaveCount(0);
    await container
      .locator('[data-h3-director-function="production_workbench"]')
      .click();
    await expect(
      container.locator('[data-h3-director-panel="production_workbench"]'),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () =>
          (window as unknown as { __h3M2319QueueKinds?: string[] })
            .__h3M2319QueueKinds ?? [],
      ),
    ).toEqual(queueBeforeImport);
    return {
      assetId,
      editorHandle: result.authoringProjection.workspaceHandle,
      clipCount,
    };
  };
  const coldImport =
    process.env.H3_CONTEXT_M25_40_NATIVE_IMPORT === "1"
      ? await importNativeOutput(first.previewedHandles[0]!, 201, true)
      : null;
  await failRun(1, "retry");
  const second = await startRun(2, "retry");
  if (coldImport !== null) {
    // IMPORTANT: a ready timeline item also holds its ▶ preview button, so a bare
    // `button` descendant makes nth(1) segment 1's preview and the click never sends
    // set_selection. Only the data-segment-index button carries aria-pressed selection.
    const segments = container.locator(
      '[data-testid="production-timeline-segment"] > button[data-segment-index]',
    );
    await expect(segments).toHaveCount(2);
    const secondSegment = segments.nth(1);
    if (
      (await segments.nth(0).getAttribute("aria-pressed")) !== "false" ||
      (await secondSegment.getAttribute("aria-pressed")) !== "true"
    ) {
      const selected = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname.endsWith(
            "/h3-context/v1/production/action",
          ) && response.request().postDataJSON()?.action === "set_selection",
        { timeout: 60_000 },
      );
      await secondSegment.click();
      expect((await selected).status()).toBe(200);
    }
    await expect(segments.nth(0)).toHaveAttribute("aria-pressed", "false");
    await expect(secondSegment).toHaveAttribute("aria-pressed", "true");
    const continuingImport = await importNativeOutput(
      second.previewedHandles[1]!,
      200,
      false,
    );
    expect(continuingImport.editorHandle).toBe(coldImport.editorHandle);
    expect(continuingImport.assetId).not.toBe(coldImport.assetId);
    const retainedEdit = await page.evaluate(
      async (body) => {
        const response = await fetch(
          new URL("/h3-context/v1/authoring/action", location.origin),
          {
            method: "POST",
            credentials: "same-origin",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        return { status: response.status, body: await response.json() };
      },
      encodeAuthoringAction(
        "m25-40-read-retained-native-edit",
        "read_timeline_history",
        {
          workspace_handle: coldImport.editorHandle,
        },
      ),
    );
    expect(retainedEdit.status).toBe(200);
    const history = decodeTimelineHistoryProjection(retainedEdit.body);
    expect(history.snapshot.clips).toHaveLength(1);
    expect(history.snapshot.assets.map((asset) => asset.assetId)).toEqual(
      expect.arrayContaining([coldImport.assetId, continuingImport.assetId]),
    );
  }
  await expect(
    container.getByText(
      "No intent proposal was supplied for the selected segments.",
      { exact: true },
    ),
  ).toHaveCount(1);
  await expect(container.locator(".h3-semantic-review")).toHaveCount(0);
  const queueKindsBeforeNavigation = await page.evaluate(
    () =>
      (window as unknown as { __h3M2319QueueKinds?: string[] })
        .__h3M2319QueueKinds ?? [],
  );
  await container.locator('[data-h3-focus-key="page-context"]').click();
  await container.locator('[data-h3-focus-key="page-production"]').click();
  await expect(
    container.getByText(
      "No intent proposal was supplied for the selected segments.",
      { exact: true },
    ),
  ).toHaveCount(1);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __h3M2319QueueKinds?: string[] })
          .__h3M2319QueueKinds ?? [],
    ),
  ).toEqual(queueKindsBeforeNavigation);
  if (first.reportedName === second.reportedName)
    throw new Error("the second live run reused the first artifact locator");
  expect(first.openWorkflowDelta).toBe(0);
  expect(second.openWorkflowDelta).toBe(0);
  expect(first.queueKinds).toEqual(["native"]);
  expect(second.queueKinds).toEqual(["native"]);
  expect(first.previewedHandles).toHaveLength(1);
  expect(second.previewedHandles).toHaveLength(2);
  expect(second.previewedHandles[0]).toBe(first.previewedHandles[0]);
  expect(second.previewedHandles[1]).not.toBe(first.previewedHandles[0]);
  expect(successfulPreparations).toHaveLength(2);
  // IMPORTANT: a ProductShell bootstrap failure settles its Production admission
  // before coordinator ownership exists; counting it as prepared would invent a member.
  expect(managedPreparations).toEqual(successfulPreparations);
  expect(successfulPreparations[1]?.ownedProjectionFingerprint).toBe(
    successfulPreparations[0]?.ownedProjectionFingerprint,
  );
  expect(successfulPreparations[1]?.ownedNodeIds).toEqual(
    successfulPreparations[0]?.ownedNodeIds,
  );
  expect(successfulPreparations[1]?.ownedLinkIds).toEqual(
    successfulPreparations[0]?.ownedLinkIds,
  );
  expect(surroundingsEvidence).toBeDefined();
  expect(surroundingsEvidence?.counts.owned).toBe(0);
  const firstFinal = await inspectRealI2vaArtifact(
    liveEnvironment.hostRoot,
    liveEnvironment.hostPython,
    first.reportedName,
  );
  expect({
    sizeStable: firstFinal.size === first.metadata.size,
    modifiedStable:
      firstFinal.modifiedMilliseconds === first.metadata.modifiedMilliseconds,
    digestStable: firstFinal.digest === first.metadata.digest,
  }).toEqual({ sizeStable: true, modifiedStable: true, digestStable: true });
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __h3M2319QueueKinds?: string[] })
          .__h3M2319QueueKinds ?? [],
    ),
  ).toEqual(["runtime_failure", "native", "runtime_failure", "native"]);
  const managedStages = await page.evaluate((baselineSequence) => {
    try {
      const raw = localStorage.getItem("h3-context.managed-journal.v1");
      if (raw === null) return [];
      const parsed = JSON.parse(raw) as { entries?: unknown };
      if (!Array.isArray(parsed.entries)) return [];
      return parsed.entries.flatMap((value) => {
        if (value === null || typeof value !== "object") return [];
        const entry = value as {
          seq?: unknown;
          kind?: unknown;
          name?: unknown;
        };
        return typeof entry.seq === "number" &&
          entry.seq > baselineSequence &&
          entry.kind === "stage" &&
          typeof entry.name === "string"
          ? [entry.name]
          : [];
      });
    } catch {
      return [];
    }
  }, managedJournalBaselineSequence);
  expect(
    managedStages.filter((stage) => stage === "bootstrap_started"),
  ).toHaveLength(4);
  expect(
    managedStages.filter((stage) => stage === "aggregate_prepare_returned"),
  ).toHaveLength(2);
  expect(managedStages).not.toContain("managed_bootstrap_stale");
  const d12Evidence = {
    schema: "h3.context.m23_19_owned_identity_acceptance.v1",
    graphFingerprints: managedPreparations.map(
      (preparation) => preparation.graphFingerprint,
    ),
    compiledPromptFingerprints: managedPreparations.map(
      (preparation) => preparation.compiledPromptFingerprint,
    ),
    ownedProjectionFingerprints: managedPreparations.map(
      (preparation) => preparation.ownedProjectionFingerprint,
    ),
    ownedNodeIds: managedPreparations[0]!.ownedNodeIds,
    ownedLinkIds: managedPreparations[0]!.ownedLinkIds,
    workflow: {
      firstOpenDelta: first.openWorkflowDelta,
      secondOpenDelta: second.openWorkflowDelta,
      sameActiveObject: true,
    },
    queueKinds: [first.queueKinds, second.queueKinds],
    surroundings: surroundingsEvidence!,
    packNames: packCensus,
  };
  const serializedD12Evidence = JSON.stringify(d12Evidence);
  expect(serializedD12Evidence).not.toMatch(/[A-Za-z]:[\\/]/);
  expect(serializedD12Evidence).not.toMatch(/(?:Users|home)[\\/]/i);
  await testInfo.attach("m23-19-owned-identity-evidence", {
    body: Buffer.from(`${serializedD12Evidence}\n`, "utf8"),
    contentType: "application/json",
  });
  console.info(`M23_19_OWNED_IDENTITY_EVIDENCE ${serializedD12Evidence}`);
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);
});

/**
 * M17-30 supported-host row.
 *
 * The official I2VA template is loaded as the visible canvas so the native
 * anchor, media wiring and artifact sink are graph-owned, not selected by this
 * repository's materialization route. The candidate sidebar must
 * offer connect with no sidebar media selection and despite any materialization
 * admission notice. M23-38 also requires the intercepted envelope to carry one
 * typed frame Registry whose source link exactly matches the native anchor.
 * The page-local queue seam is replaced and never called through: this verifies
 * one real host splice and one submission attempt while loading no model and
 * producing no media.
 */
