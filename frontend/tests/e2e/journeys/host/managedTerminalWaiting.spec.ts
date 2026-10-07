import { expect, test } from "../../host/fixture";

import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import {
  assertCandidateBundleInjection,
  awaitHostExecution,
  beginSettledH3InteractionPhase,
  candidateBackendMode,
  candidateBackendRuntime,
  candidateBundle,
  candidateInjectionCount,
  expectCandidateInteractionNetworkLocal,
  hostUrl,
  instrumentHostGraphLoads,
  m23TerminalWaitingArtifactLocator,
  m23TerminalWaitingAuthorized,
  monitorCandidateInitiatorNetwork,
  monitorH3Network,
  monitorManagedRouteResponses,
  openH3AppModeTab,
  reportedArtifactNames,
  sampleProgress,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
  watchHostExecution,
} from "../../host/environment";

type CloseReceipt = Readonly<{
  artifact: "null" | "locator" | "invalid";
  status: number;
  disposition: string;
  jobState: string;
  completed: number;
  runState: string;
  artifactAuthority: boolean;
  geometry: Readonly<{
    format: string;
    frameCount: number;
    width: number;
    height: number;
  }> | null;
}>;

function installM2332ManagedExecutionProjection(): void {
  type CompiledNode = {
    class_type?: unknown;
    inputs?: Record<string, unknown>;
  };
  type CompiledPrompt = {
    output?: Record<string, CompiledNode>;
    workflow?: unknown;
  };
  type QueueReceipt = {
    projectorCalls: number;
    fullEnvelopeCalls: number;
    queueCalls: number;
    projectionStable: boolean;
    queueMatchesProjection: boolean;
    delegatedUnchanged: boolean;
    queuedAssetFree: boolean;
    queuedSourceCount: number;
    queuedLoaderCount: number;
    queuedNativeGeneratorCount: number;
    queuedSinkCount: number;
    queuedProductShellCount: number;
    originalSinkNodeId: string;
  };
  const runtime = window as unknown as {
    comfyAPI: { api: { api: any } };
    __h3M2508ManagedExecutionProjection?: (
      compiled: CompiledPrompt,
    ) => CompiledPrompt;
    __h3M2508ExecutionIdentityTrace?: Array<Record<string, unknown>>;
    __h3M2332QueueReceipt?: QueueReceipt;
  };
  const api = runtime.comfyAPI.api.api;
  if (typeof api.queuePrompt !== "function")
    throw new Error("the public host queue seam is unavailable");
  const originalQueuePrompt = api.queuePrompt.bind(api);
  const loaders = new Set(["UNETLoader", "CLIPLoader", "LoraLoaderModelOnly"]);
  const nativeGenerators = new Set([
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
  ]);
  const sources = new Set(["LoadImage", "LoadVideo"]);
  const allowedProjectionTypes = new Set([
    "comfyui_h3_context.H3Context.Request",
    "comfyui_h3_context.H3Context.Plan",
    "comfyui_h3_context.H3Context.Compiler",
    "comfyui_h3_context.H3Context.Validator",
    "comfyui_h3_context.H3Context.NativeH3Adapter",
    "comfyui_h3_context.H3Context.ProductShell",
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    "PrimitiveFloat",
    "PrimitiveInt",
  ]);
  const canonicalJson = (candidate: unknown): string | null => {
    const seen = new WeakSet<object>();
    let members = 0;
    const visit = (value: unknown, depth: number): string | null => {
      members += 1;
      if (members > 4096 || depth > 32) return null;
      if (value === null) return "null";
      if (typeof value === "string" || typeof value === "boolean")
        return JSON.stringify(value);
      if (typeof value === "number")
        return Number.isFinite(value) ? JSON.stringify(value) : null;
      if (typeof value !== "object" || seen.has(value)) return null;
      seen.add(value);
      if (Array.isArray(value)) {
        if (value.length > 512) return null;
        const parts = value.map((entry) => visit(entry, depth + 1));
        return parts.some((entry) => entry === null)
          ? null
          : `[${parts.join(",")}]`;
      }
      const object = value as Record<string, unknown>;
      const keys = Object.keys(object).sort();
      if (keys.length > 256) return null;
      const parts = keys.map((key) => {
        const encoded = visit(object[key], depth + 1);
        return encoded === null ? null : `${JSON.stringify(key)}:${encoded}`;
      });
      return parts.some((entry) => entry === null)
        ? null
        : `{${parts.join(",")}}`;
    };
    return visit(candidate, 0);
  };
  const receipt: QueueReceipt = {
    projectorCalls: 0,
    fullEnvelopeCalls: 0,
    queueCalls: 0,
    projectionStable: true,
    queueMatchesProjection: false,
    delegatedUnchanged: false,
    queuedAssetFree: false,
    queuedSourceCount: 0,
    queuedLoaderCount: 0,
    queuedNativeGeneratorCount: 0,
    queuedSinkCount: 0,
    queuedProductShellCount: 0,
    originalSinkNodeId: "",
  };
  runtime.__h3M2332QueueReceipt = receipt;
  runtime.__h3M2508ExecutionIdentityTrace = [];
  let projectedCanonical: string | undefined;

  // CRITICAL: install the deterministic projection at the webdriver-gated product seam.
  // Moving this closure into queuePrompt would make validation and execution identities differ.
  runtime.__h3M2508ManagedExecutionProjection = (
    compiledValue: CompiledPrompt,
  ): CompiledPrompt => {
    const compiled = structuredClone(compiledValue);
    const output = compiled.output;
    if (output === undefined)
      throw new Error("the M23-32 compiled execution output is unavailable");
    const rows = Object.entries(output);
    const count = (types: ReadonlySet<string>): number =>
      rows.filter(([, node]) => types.has(String(node.class_type ?? "")))
        .length;
    const productShells = rows.filter(
      ([, node]) =>
        node.class_type === "comfyui_h3_context.H3Context.ProductShell",
    );
    const sinks = rows.filter(([, node]) => node.class_type === "SaveVideo");
    if (
      count(sources) !== 0 ||
      count(loaders) !== 3 ||
      count(nativeGenerators) !== 1 ||
      productShells.length !== 1 ||
      sinks.length !== 1
    )
      throw new Error("the M23-32 full generation envelope is unavailable");
    const sinkNodeId = sinks[0]![0];
    if (
      receipt.originalSinkNodeId !== "" &&
      receipt.originalSinkNodeId !== sinkNodeId
    )
      throw new Error("the M23-32 original artifact sink identity changed");
    receipt.originalSinkNodeId = sinkNodeId;
    receipt.fullEnvelopeCalls += 1;

    const included = new Set<string>();
    const pending = [productShells[0]![0]];
    while (pending.length > 0) {
      const nodeId = pending.pop()!;
      if (included.has(nodeId)) continue;
      const node = output[nodeId];
      if (node === undefined)
        throw new Error("the M23-32 closure references a missing node");
      included.add(nodeId);
      for (const value of Object.values(node.inputs ?? {}))
        if (
          Array.isArray(value) &&
          value.length === 2 &&
          (typeof value[0] === "string" || typeof value[0] === "number") &&
          Object.hasOwn(output, String(value[0]))
        )
          pending.push(String(value[0]));
      if (included.size > 128)
        throw new Error("the M23-32 execution closure exceeded its node bound");
    }
    const projectionOutput = Object.fromEntries(
      rows
        .filter(([nodeId]) => included.has(nodeId))
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([nodeId, node]) => [nodeId, structuredClone(node)]),
    );
    const projectionTypes = Object.values(projectionOutput).map((node) =>
      String(node.class_type ?? ""),
    );
    if (
      projectionTypes.length === 0 ||
      projectionTypes.some((type) => !allowedProjectionTypes.has(type)) ||
      projectionTypes.filter(
        (type) => type === "comfyui_h3_context.H3Context.ProductShell",
      ).length !== 1
    )
      throw new Error("the M23-32 runtime projection is not asset-free");
    const projected = { ...compiled, output: projectionOutput };
    const canonical = canonicalJson(projected);
    if (canonical === null)
      throw new Error("the M23-32 runtime projection is not canonical JSON");
    receipt.projectorCalls += 1;
    if (projectedCanonical === undefined) projectedCanonical = canonical;
    else receipt.projectionStable &&= projectedCanonical === canonical;
    return projected;
  };

  api.queuePrompt = async function (batch: unknown, compiled: CompiledPrompt) {
    receipt.queueCalls += 1;
    const delegated = structuredClone(compiled);
    const compiledCanonical = canonicalJson(compiled);
    const delegatedCanonical = canonicalJson(delegated);
    receipt.queueMatchesProjection =
      compiledCanonical !== null && compiledCanonical === projectedCanonical;
    receipt.delegatedUnchanged =
      compiledCanonical !== null && compiledCanonical === delegatedCanonical;
    const queuedRows = Object.entries(delegated.output ?? {});
    const countQueued = (types: ReadonlySet<string>): number =>
      queuedRows.filter(([, node]) => types.has(String(node.class_type ?? "")))
        .length;
    receipt.queuedSourceCount = countQueued(sources);
    receipt.queuedLoaderCount = countQueued(loaders);
    receipt.queuedNativeGeneratorCount = countQueued(nativeGenerators);
    receipt.queuedSinkCount = countQueued(new Set(["SaveVideo"]));
    receipt.queuedProductShellCount = countQueued(
      new Set(["comfyui_h3_context.H3Context.ProductShell"]),
    );
    receipt.queuedAssetFree =
      queuedRows.length > 0 &&
      queuedRows.every(([, node]) =>
        allowedProjectionTypes.has(String(node.class_type ?? "")),
      ) &&
      receipt.queuedSourceCount === 0 &&
      receipt.queuedLoaderCount === 0 &&
      receipt.queuedNativeGeneratorCount === 0 &&
      receipt.queuedSinkCount === 0 &&
      receipt.queuedProductShellCount === 1;
    if (
      !receipt.projectionStable ||
      !receipt.queueMatchesProjection ||
      !receipt.delegatedUnchanged ||
      !receipt.queuedAssetFree
    )
      throw new Error("the M23-32 validated queue identity is not exact");
    return await originalQueuePrompt(batch, delegated);
  };
}

test("M23-32 real host success waits for a later verified artifact", async ({
  context,
  page,
}) => {
  test.skip(
    !m23TerminalWaitingAuthorized ||
      m23TerminalWaitingArtifactLocator === null ||
      candidateBundle === null ||
      candidateBackendRuntime === null ||
      candidateBackendMode !== "exact",
    "M23-32 requires the explicit fixture locator and an exact installed candidate",
  );
  test.setTimeout(10 * 60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");

  const queueBefore = await supportedHostQueueCounts(page);
  expect(queueBefore).toEqual({ running: 0, pending: 0 });
  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const routeReceipts = monitorManagedRouteResponses(page);
  const closeResponses: CloseReceipt[] = [];
  const closeRequests: Array<{
    artifact: CloseReceipt["artifact"];
    queuePromptId: string;
    outputNodeId: string | null;
  }> = [];
  const preparations: Array<{
    expectedFrames: number;
    sourceIdentity: unknown;
    ownedProjectionFingerprint: string;
    ownedNodeIds: string[];
    ownedLinkIds: string[];
    anchorNodeId: string;
  }> = [];
  const artifactKind = (payload: unknown): CloseReceipt["artifact"] => {
    if (payload === null) return "null";
    if (
      payload !== null &&
      typeof payload === "object" &&
      !Array.isArray(payload)
    )
      return "locator";
    return "invalid";
  };
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
        payload?: Record<string, unknown>;
      } | null;
      if (body?.action === "prepare_managed_run") {
        const observation = (body.payload?.observation ?? {}) as Record<
          string,
          unknown
        >;
        preparations.push({
          expectedFrames: Number(observation.expected_frames),
          sourceIdentity: observation.source_identity,
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
      }
      if (body?.action !== "close_managed_run") return;
      const artifact = body.payload?.artifact;
      const artifactRecord =
        artifact !== null &&
        typeof artifact === "object" &&
        !Array.isArray(artifact)
          ? (artifact as Record<string, unknown>)
          : undefined;
      closeRequests.push({
        artifact: artifactKind(artifact),
        queuePromptId: String(body.payload?.queue_prompt_id ?? ""),
        outputNodeId:
          artifactRecord === undefined
            ? null
            : String(artifactRecord.output_node_id ?? ""),
      });
    } catch {
      // A malformed coordinator body is exposed by the missing bounded receipt.
    }
  });
  page.on("response", (response) => {
    if (
      !new URL(response.url()).pathname.endsWith(
        "/h3-context/v1/generation/coordinator",
      )
    )
      return;
    let requestBody: {
      action?: unknown;
      payload?: Record<string, unknown>;
    } | null;
    try {
      requestBody = JSON.parse(response.request().postData() ?? "null") as {
        action?: unknown;
        payload?: Record<string, unknown>;
      } | null;
    } catch {
      return;
    }
    if (requestBody?.action !== "close_managed_run") return;
    const requestArtifact = requestBody.payload?.artifact;
    void response
      .json()
      .then((value: unknown) => {
        const wire = value as Record<string, any>;
        const segment = Array.isArray(wire.production?.segments)
          ? wire.production.segments[0]
          : undefined;
        const geometry = segment?.delivered_geometry;
        closeResponses.push({
          artifact: artifactKind(requestArtifact),
          status: response.status(),
          disposition: String(wire.disposition ?? ""),
          jobState: String(wire.sequence?.progress?.[0]?.state ?? ""),
          completed: Number(wire.production?.run?.completed ?? -1),
          runState: String(wire.production?.run?.state ?? ""),
          artifactAuthority:
            wire.artifact_authority !== null &&
            typeof wire.artifact_authority === "object",
          geometry:
            geometry !== null && typeof geometry === "object"
              ? {
                  format: String(geometry.format ?? ""),
                  frameCount: Number(geometry.frame_count ?? -1),
                  width: Number(geometry.width ?? -1),
                  height: Number(geometry.height ?? -1),
                }
              : null,
        });
      })
      .catch(() => undefined);
  });

  const initialInjectionCount = candidateInjectionCount(context);
  const browserTabCount = context.pages().length;
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  await instrumentHostGraphLoads(page);
  await waitForHostGraphSettled(page);
  const workflowBefore = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M2332WorkflowAuthority?: object | null;
    };
    const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
    if (!Array.isArray(store?.openWorkflows))
      throw new Error("the public host workflow store is unavailable");
    runtime.__h3M2332WorkflowAuthority = store.activeWorkflow;
    return {
      openCount: store.openWorkflows.length,
      activeKnown:
        store.activeWorkflow === null ||
        (typeof store.activeWorkflow === "object" &&
          !Array.isArray(store.activeWorkflow)),
    };
  });
  expect(workflowBefore.activeKnown).toBe(true);

  const container = await openH3AppModeTab(
    page,
    "h3-context-m23-32-terminal-waiting",
  );
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await watchHostExecution(page);
  await page.evaluate(installM2332ManagedExecutionProjection);
  const projectionBoundary = await page.evaluate(() => ({
    webdriver: navigator.webdriver === true,
    projector:
      typeof (
        window as unknown as {
          __h3M2508ManagedExecutionProjection?: unknown;
        }
      ).__h3M2508ManagedExecutionProjection === "function",
  }));
  // CRITICAL: fail before the queue click if the automation-only projection cannot be selected;
  // continuing would submit the visible native H3 envelope and could start model/GPU execution.
  expect(projectionBoundary).toEqual({ webdriver: true, projector: true });

  await container
    .locator('[data-h3-focus-key="app-intent"]')
    .fill("Verify a successful host terminal before its output event arrives.");
  await container
    .locator('[data-h3-focus-key="app-duration-seconds"]')
    .fill("8");
  await expect(
    container.getByText("Delivers 8 s (192 frames).", { exact: true }),
  ).toBeVisible();
  const submit = container.locator('[data-h3-focus-key="app-submit"]');
  await expect(submit).toBeEnabled({ timeout: 30_000 });
  const fromSubmission = (await sampleProgress(page)).submitted;
  await submit.click();
  await expect
    .poll(async () => (await sampleProgress(page, fromSubmission)).started, {
      timeout: 180_000,
    })
    .toBe(1);
  const result = await awaitHostExecution(page, fromSubmission, 180_000);
  expect(result.errors).toEqual([]);
  expect(reportedArtifactNames(result.outputs)).toEqual([]);
  try {
    await expect.poll(() => closeResponses.length, { timeout: 60_000 }).toBe(1);
  } catch (error) {
    console.info(
      "M23_32_TERMINAL_WAITING_DIAGNOSTIC " +
        JSON.stringify({
          routes: routeReceipts.map((receipt) => ({
            route: receipt.route,
            action: receipt.action,
            stage: receipt.stage,
          })),
          preparations: preparations.length,
          closeRequests: closeRequests.length,
          shellStatus: await container
            .locator("[data-shell-status]")
            .first()
            .getAttribute("data-shell-status"),
          phase: await container
            .locator("[data-app-mode-phase]")
            .first()
            .getAttribute("data-app-mode-phase")
            .catch(() => null),
        }),
    );
    throw error;
  }
  expect(closeResponses[0]).toEqual({
    artifact: "null",
    status: 200,
    disposition: "verification_pending",
    jobState: "running",
    completed: 0,
    runState: "running",
    artifactAuthority: false,
    geometry: null,
  });
  expect(closeRequests).toHaveLength(1);
  expect(closeRequests[0]?.artifact).toBe("null");
  await expect(
    container.locator('[data-app-mode-phase="verifying_output"]'),
  ).toHaveCount(1);
  await expect(container.locator('[data-shell-status="working"]')).toHaveCount(
    1,
  );
  await expect(
    container.locator('[data-shell-status="projected"]'),
  ).toHaveCount(0);

  const executionIdentity = await page.evaluate((offset) => {
    const runtime = window as unknown as {
      __h3Sample?: { promptIds?: string[] };
      __h3M2508ExecutionIdentityTrace?: Array<Record<string, unknown>>;
      __h3M2332QueueReceipt?: {
        projectorCalls: number;
        fullEnvelopeCalls: number;
        queueCalls: number;
        projectionStable: boolean;
        queueMatchesProjection: boolean;
        delegatedUnchanged: boolean;
        queuedAssetFree: boolean;
        queuedSourceCount: number;
        queuedLoaderCount: number;
        queuedNativeGeneratorCount: number;
        queuedSinkCount: number;
        queuedProductShellCount: number;
        originalSinkNodeId: string;
      };
    };
    const promptIds = (runtime.__h3Sample?.promptIds ?? []).slice(offset);
    return {
      promptIds,
      queue: runtime.__h3M2332QueueReceipt,
      trace: [...(runtime.__h3M2508ExecutionIdentityTrace ?? [])],
    };
  }, fromSubmission);
  expect(executionIdentity.promptIds).toHaveLength(1);
  expect(executionIdentity.queue).toEqual({
    projectorCalls: 2,
    fullEnvelopeCalls: 2,
    queueCalls: 1,
    projectionStable: true,
    queueMatchesProjection: true,
    delegatedUnchanged: true,
    queuedAssetFree: true,
    queuedSourceCount: 0,
    queuedLoaderCount: 0,
    queuedNativeGeneratorCount: 0,
    queuedSinkCount: 0,
    queuedProductShellCount: 1,
    originalSinkNodeId: expect.stringMatching(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/),
  });
  expect(executionIdentity.trace).toHaveLength(3);
  expect(executionIdentity.trace.map((entry) => entry.stage)).toEqual([
    "final_projection",
    "preflight_projection",
    "queue_envelope",
  ]);
  expect(
    new Set(executionIdentity.trace.map((entry) => entry.fingerprint)).size,
  ).toBe(1);
  const promptId = executionIdentity.promptIds[0]!;
  expect(closeRequests[0]?.queuePromptId).toBe(promptId);
  const historyResponse = await page.request.get(
    new URL(`/history/${encodeURIComponent(promptId)}`, hostUrl).href,
  );
  expect(historyResponse.ok()).toBe(true);
  const history = (await historyResponse.json()) as Record<
    string,
    { prompt?: unknown[]; status?: { status_str?: unknown } }
  >;
  const accepted = history[promptId];
  const acceptedOutput = accepted?.prompt?.[2] as
    Record<string, { class_type?: unknown }> | undefined;
  const acceptedTypes = Object.values(acceptedOutput ?? {}).map((node) =>
    String(node.class_type ?? ""),
  );
  expect(accepted?.status?.status_str).toBe("success");
  expect(
    acceptedTypes.filter(
      (type) => type === "comfyui_h3_context.H3Context.ProductShell",
    ),
  ).toHaveLength(1);
  for (const absent of [
    "SaveVideo",
    "UNETLoader",
    "CLIPLoader",
    "LoraLoaderModelOnly",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "LoadImage",
    "LoadVideo",
  ])
    expect(acceptedTypes).not.toContain(absent);

  expect(preparations).toHaveLength(1);
  const preparation = preparations[0]!;
  expect(preparation.expectedFrames).toBe(192);
  expect(preparation.sourceIdentity).toBeNull();
  expect(preparation.ownedProjectionFingerprint).toMatch(
    /^sha256:[0-9a-f]{64}$/,
  );
  const graphBeforeLateArtifact = await page.evaluateHandle(() =>
    structuredClone(
      (
        window as unknown as { comfyAPI: { app: { app: any } } }
      ).comfyAPI.app.app.graph.serialize(),
    ),
  );

  // The host already emitted the successful terminal above. This stock-shape event is
  // deliberately test-controlled and names a pre-seeded fixture; it exercises only the
  // late delivery/verification join and is never evidence of model generation.
  await page.evaluate(
    ({ acceptedPromptId, outputNodeId, locator }) => {
      const api = (
        window as unknown as { comfyAPI: { api: { api: EventTarget } } }
      ).comfyAPI.api.api;
      api.dispatchEvent(
        new CustomEvent("executed", {
          detail: {
            prompt_id: acceptedPromptId,
            node: outputNodeId,
            output: { images: [locator], animated: [true] },
          },
        }),
      );
    },
    {
      acceptedPromptId: promptId,
      outputNodeId: executionIdentity.queue!.originalSinkNodeId,
      locator: m23TerminalWaitingArtifactLocator!,
    },
  );
  await expect.poll(() => closeResponses.length, { timeout: 60_000 }).toBe(2);
  expect(closeRequests).toHaveLength(2);
  expect(closeRequests[1]).toEqual({
    artifact: "locator",
    queuePromptId: promptId,
    outputNodeId: executionIdentity.queue!.originalSinkNodeId,
  });
  expect(closeResponses[1]).toEqual({
    artifact: "locator",
    status: 200,
    disposition: "succeeded",
    jobState: "succeeded",
    completed: 1,
    runState: "succeeded",
    artifactAuthority: true,
    geometry: { format: "mp4", frameCount: 192, width: 512, height: 512 },
  });
  await expect(
    container.locator('[data-shell-status="projected"]'),
  ).toHaveCount(1, { timeout: 60_000 });
  await container.getByRole("button", { name: "Production" }).click();
  await expect(
    container.getByText("1 of 1 segments", { exact: true }),
  ).toBeVisible();
  await expect(
    container.locator('[data-segment-index][data-state="succeeded"]'),
  ).toHaveCount(1);

  const graphAfterLateArtifact = await page.evaluateHandle(() =>
    (
      window as unknown as { comfyAPI: { app: { app: any } } }
    ).comfyAPI.app.app.graph.serialize(),
  );
  const surroundings = await page.evaluate(diffGraphSurroundings, {
    beforeValue: graphBeforeLateArtifact,
    afterValue: graphAfterLateArtifact,
    reference: {
      ownedNodeIds: preparation.ownedNodeIds,
      ownedLinkIds: preparation.ownedLinkIds,
      anchorNodeId: preparation.anchorNodeId,
      ownedProjectionEqual: false,
    },
  });
  await graphBeforeLateArtifact.dispose();
  await graphAfterLateArtifact.dispose();
  expect(surroundings.counts.owned).toBe(0);

  const workflowAfter = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M2332WorkflowAuthority?: object | null;
    };
    const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
    return {
      openCount: Array.isArray(store?.openWorkflows)
        ? store.openWorkflows.length
        : -1,
      activeStable:
        store?.activeWorkflow === runtime.__h3M2332WorkflowAuthority,
    };
  });
  expect(workflowAfter).toEqual({
    openCount: workflowBefore.openCount,
    activeStable: true,
  });
  expect(context.pages()).toHaveLength(browserTabCount);

  const requestActions = routeReceipts
    .filter((receipt) => receipt.stage === "request")
    .map((receipt) => `${receipt.route}:${receipt.action}`)
    .filter((value) =>
      new Set([
        "sequence_coordinator:prepare_managed_run",
        "host_prompt:queue_prompt",
        "sequence_coordinator:submit_managed_run",
        "sequence_coordinator:close_managed_run",
      ]).has(value),
    );
  expect(requestActions).toEqual([
    "host_prompt:queue_prompt",
    "sequence_coordinator:prepare_managed_run",
    "sequence_coordinator:submit_managed_run",
    "sequence_coordinator:close_managed_run",
    "sequence_coordinator:close_managed_run",
  ]);
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);

  const evidence = {
    schema: "h3.context.m23_32_terminal_waiting_host_evidence.v1",
    actualHostTerminal: "success",
    lateEventSource: "test_fixture",
    closeDispositions: closeResponses.map((receipt) => receipt.disposition),
    sourceIdentityPresent: preparation.sourceIdentity !== null,
    expectedFrames: preparation.expectedFrames,
    queueCalls: executionIdentity.queue!.queueCalls,
    executionIdentityStages: executionIdentity.trace.map(
      (entry) => entry.stage,
    ),
    executionIdentityEqual:
      new Set(executionIdentity.trace.map((entry) => entry.fingerprint))
        .size === 1,
    delegatedUnchanged: executionIdentity.queue!.delegatedUnchanged,
    openWorkflowDelta: workflowAfter.openCount - workflowBefore.openCount,
    browserTabDelta: context.pages().length - browserTabCount,
    surroundings,
  };
  const serializedEvidence = JSON.stringify(evidence);
  expect(serializedEvidence).not.toMatch(/[A-Za-z]:[\\/]/);
  expect(serializedEvidence).not.toMatch(/(?:Users|home)[\\/]/i);
  console.info(`M23_32_TERMINAL_WAITING_EVIDENCE ${serializedEvidence}`);
});
