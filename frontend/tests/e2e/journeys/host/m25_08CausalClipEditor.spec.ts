import { expect, test } from "../../host/fixture";
import { createHash } from "node:crypto";

import {
  assertM2508AuthorizedMediaRuntimePrecondition,
  assertM2508ExactHostCapability,
  buildM2508AuthorizedMediaRuntimeProbe,
  buildM2508HostBootstrap,
  ensureM2508CapturedWorkflowAuthority,
  installM2508ManagedExecutionProjection,
  isM2508HostGraphLifecycleReady,
  M2508_AUTHORIZED_MEDIA_RUNTIME_ROUTE,
  M2508_OWNED_NODE_IDS,
  materializeM2508VisiblePromptInCapturedWorkflow,
} from "../../host/m25_08Bootstrap";

import {
  assertCandidateBundleInjection,
  beginSettledH3InteractionPhase,
  candidateBackendMode,
  candidateBackendRuntime,
  candidateBundle,
  candidateInjectionCount,
  expectCandidateInteractionNetworkLocal,
  hostUrl,
  hostWorkflowAuthorityReceipt,
  instrumentHostGraphLoads,
  monitorCandidateInitiatorNetwork,
  monitorH3Network,
  openH3AppModeTab,
  readVisibleGraph,
  resetHostGraphLoads,
  subgraphNodes,
  waitForHostGraphSettled,
  waitForH3Registration,
} from "../../host/environment";
import {
  classifyM2508CoordinatorAction,
  classifyM2508PostRoute,
  normalizeM2508HostApiPath,
} from "../../host/m25_08RequestClassification";

const FIXTURE_AUTHORIZED =
  process.env.H3_CONTEXT_M25_08_AUTHORIZED_VIDEO_V1 === "1";
const VIDEO_LOCATOR = process.env.H3_CONTEXT_M25_08_VIDEO_LOCATOR?.trim() ?? "";
const SUMMARY = { name: "Clip editor project", exact: true } as const;
const LEGACY_EDITOR = { name: "Reference & timeline authoring" } as const;

function m2508CanonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean")
    return JSON.stringify(value);
  if (typeof value === "number" && Number.isFinite(value))
    return JSON.stringify(value);
  if (Array.isArray(value))
    return `[${value.map((entry) => m2508CanonicalJson(entry)).join(",")}]`;
  if (typeof value !== "object")
    throw new Error("M25-08 submitted prompt is not canonical JSON");
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${m2508CanonicalJson(object[key])}`)
    .join(",")}}`;
}

function m2508PromptSha256(value: unknown): string {
  return createHash("sha256")
    .update(m2508CanonicalJson(value), "utf8")
    .digest("hex");
}

test("M25-08 exact candidate runs the authorized selected-VIDEO host journey", async ({
  context,
  page,
}, testInfo) => {
  test.setTimeout(90_000);
  if (candidateBundle === null) {
    testInfo.annotations.push({
      type: "m25-08-host",
      description: "NOT_RUN: exact candidate bundle is not bound",
    });
    test.skip(true, "exact candidate bundle binding is required");
    return;
  }
  if (candidateBackendMode !== "exact" || candidateBackendRuntime === null) {
    testInfo.annotations.push({
      type: "m25-08-host",
      description: "NOT_RUN: exact candidate backend runtime is not bound",
    });
    test.skip(true, "exact candidate backend runtime parity is required");
    return;
  }
  if (!FIXTURE_AUTHORIZED) {
    testInfo.annotations.push({
      type: "m25-08-host",
      description: "NOT_RUN: m25_08_authorized_video_v1 is not authorized",
    });
    test.skip(
      true,
      "the privacy-safe source fixture must be explicitly authorized",
    );
    return;
  }
  if (VIDEO_LOCATOR.length === 0) {
    testInfo.annotations.push({
      type: "m25-08-host",
      description: "NOT_RUN: authorized VIDEO locator is not bound",
    });
    test.skip(true, "the authorized VIDEO locator binding is required");
    return;
  }
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  try {
    for (const nodeId of M2508_OWNED_NODE_IDS) {
      const capabilityUrl = new URL(
        `/object_info/${encodeURIComponent(nodeId)}`,
        hostUrl,
      );
      const response = await page.request.get(capabilityUrl.href);
      if (!response.ok())
        throw new Error("M25-08 active node module is not exact");
      assertM2508ExactHostCapability(nodeId, await response.json());
    }
    const mediaRuntimeResponse = await page.request.post(
      new URL(M2508_AUTHORIZED_MEDIA_RUNTIME_ROUTE, hostUrl).href,
      {
        headers: {
          "content-type": "application/json",
          Origin: allowedOrigin,
        },
        data: buildM2508AuthorizedMediaRuntimeProbe(),
      },
    );
    const mediaRuntimeWire = (await mediaRuntimeResponse.json()) as unknown;
    assertM2508AuthorizedMediaRuntimePrecondition(
      mediaRuntimeResponse.status(),
      mediaRuntimeWire,
    );
    console.log(
      `M25_08_MEDIA_RUNTIME_PRECONDITION=${JSON.stringify({
        status: mediaRuntimeResponse.status(),
        reason:
          mediaRuntimeWire !== null &&
          typeof mediaRuntimeWire === "object" &&
          !Array.isArray(mediaRuntimeWire)
            ? (mediaRuntimeWire as { reason?: unknown }).reason
            : null,
        queueCalls: 0,
      })}`,
    );
  } catch (error) {
    testInfo.annotations.push({
      type: "m25-08-host",
      description:
        error instanceof Error &&
        error.message === "M25-08 authorized media runtime is unavailable"
          ? "NOT_RUN: authorized media runtime is unavailable"
          : "NOT_RUN: exact host preconditions are not bound",
    });
    throw error;
  }
  const bootstrap = buildM2508HostBootstrap(VIDEO_LOCATOR);

  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const routeCounts = {
    authoringAction: 0,
    authoringPreview: 0,
    queue: 0,
    provider: 0,
    generation: 0,
    coordinator: 0,
  };
  const preconditionCounts = {
    queue: 0,
    provider: 0,
    generation: 0,
    coordinator: 0,
  };
  const preconditionCoordinatorActions: string[] = [];
  let preconditionPromptCensus:
    Array<{ id: string; classType: string }> | undefined;
  let preconditionPromptSha256: string | undefined;
  let preconditionPromptAckId: string | undefined;
  let requestPhase: "precondition" | "journey" = "precondition";
  const actionBodies: Array<Record<string, unknown>> = [];
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    const path = new URL(request.url()).pathname;
    const logicalPath = normalizeM2508HostApiPath(path);
    const routeClass = classifyM2508PostRoute(path);
    if (routeClass === "queue") {
      if (requestPhase === "precondition") {
        preconditionCounts.queue += 1;
        if (logicalPath === "/prompt") {
          const body = request.postDataJSON() as unknown;
          const prompt =
            body !== null && typeof body === "object" && !Array.isArray(body)
              ? (body as { prompt?: unknown }).prompt
              : null;
          if (
            prompt !== null &&
            typeof prompt === "object" &&
            !Array.isArray(prompt)
          ) {
            // IMPORTANT: retain only a content digest outside the request callback. An ID/type
            // census cannot detect a same-class VIDEO locator or nested-link substitution.
            preconditionPromptSha256 = m2508PromptSha256(prompt);
            preconditionPromptCensus = Object.entries(
              prompt as Record<string, unknown>,
            )
              .map(([id, value]) => ({
                id,
                classType:
                  value !== null &&
                  typeof value === "object" &&
                  !Array.isArray(value)
                    ? String(
                        (value as { class_type?: unknown }).class_type ?? "",
                      )
                    : "",
              }))
              .sort((left, right) => left.id.localeCompare(right.id));
          }
        }
      } else routeCounts.queue += 1;
      return;
    }
    if (routeClass === "provider") {
      if (requestPhase === "precondition") preconditionCounts.provider += 1;
      else routeCounts.provider += 1;
      return;
    }
    if (routeClass === "generation") {
      if (requestPhase === "precondition") preconditionCounts.generation += 1;
      else routeCounts.generation += 1;
      return;
    }
    if (routeClass === "coordinator") {
      if (requestPhase === "precondition") {
        preconditionCounts.coordinator += 1;
        preconditionCoordinatorActions.push(
          classifyM2508CoordinatorAction(request.postDataJSON() as unknown),
        );
      } else routeCounts.coordinator += 1;
      return;
    }
    if (requestPhase === "precondition") return;
    if (path.endsWith("/h3-context/v1/authoring/action")) {
      routeCounts.authoringAction += 1;
      const body = request.postDataJSON() as unknown;
      if (body !== null && typeof body === "object" && !Array.isArray(body))
        actionBodies.push(body as Record<string, unknown>);
    } else if (path.endsWith("/h3-context/v1/authoring/media-preview")) {
      routeCounts.authoringPreview += 1;
    }
  });
  page.on("response", async (response) => {
    const request = response.request();
    if (
      requestPhase !== "precondition" ||
      request.method() !== "POST" ||
      normalizeM2508HostApiPath(new URL(request.url()).pathname) !== "/prompt"
    )
      return;
    try {
      const body = (await response.json()) as unknown;
      if (body !== null && typeof body === "object" && !Array.isArray(body)) {
        const promptId = (body as { prompt_id?: unknown }).prompt_id;
        if (typeof promptId === "string" && promptId.length > 0)
          preconditionPromptAckId = promptId;
      }
    } catch {
      // The explicit missing acknowledgement assertion below remains authoritative.
    }
  });

  const injectionBefore = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await page.waitForFunction(isM2508HostGraphLifecycleReady);
  await assertCandidateBundleInjection(page, context, injectionBefore);
  await instrumentHostGraphLoads(page);
  const container = await openH3AppModeTab(
    page,
    "h3-context-m25-08-acceptance",
  );
  await expect(page.locator("#h3-context-m25-08-acceptance")).toHaveCount(1);
  // IMPORTANT: settle ComfyUI's workflow-template lifecycle before resetting the receipt;
  // capturing authority earlier includes the dialog's foreign loads and makes the bootstrap
  // transaction appear to target a stale workflow.
  await waitForHostGraphSettled(page);
  await resetHostGraphLoads(page);
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { api: { api: { addEventListener: Function } } };
      __h3M2508Executed?: Array<{
        node?: unknown;
        promptId?: unknown;
        workspacePresent: boolean;
      }>;
      __h3M2508Terminal?: string[];
    };
    runtime.__h3M2508Executed = [];
    runtime.__h3M2508Terminal = [];
    const api = runtime.comfyAPI.api.api;
    api.addEventListener("executed", (event: Event) => {
      const detail = (event as CustomEvent<Record<string, unknown>>).detail;
      const output = detail?.output as
        Record<string, unknown[]> | null | undefined;
      runtime.__h3M2508Executed?.push({
        node: detail?.node,
        promptId: detail?.prompt_id,
        workspacePresent: output?.sidebar_workspace?.[0] !== undefined,
      });
    });
    for (const name of ["execution_error", "execution_interrupted"])
      api.addEventListener(name, () => runtime.__h3M2508Terminal?.push(name));
  });
  await page.evaluate(
    materializeM2508VisiblePromptInCapturedWorkflow,
    bootstrap.visiblePrompt,
  );
  await page.evaluate(ensureM2508CapturedWorkflowAuthority);
  expect(await hostWorkflowAuthorityReceipt(page)).toEqual({
    activeStable: true,
    openWorkflowDelta: 0,
    graphLoadWorkflowMatches: [true],
  });
  // Bind the journey receipt only after ComfyUI has completed its two-phase public workflow
  // activation. The one subsequent load belongs to the product's existing-graph transaction.
  await resetHostGraphLoads(page);
  await page.evaluate(
    installM2508ManagedExecutionProjection,
    bootstrap.executionPrompt,
  );
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
  // continuing would submit the visible native H3 anchor and could start model/GPU execution.
  expect(projectionBoundary).toEqual({ webdriver: true, projector: true });
  await container
    .getByRole("combobox", { name: "Task mode" })
    .selectOption("ref2va");
  await container
    .getByRole("combobox", { name: "Reference videos" })
    .selectOption("2");
  await container
    .getByRole("textbox", { name: "Intent" })
    .fill(
      "Preserve the supplied synthetic reference video while the camera remains steady.",
    );
  await container
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("4");
  // IMPORTANT: the backend H3 lattice resolves requested 4 s to 107 frames (4458 ms).
  // Do not replace this authority with requested-seconds * playback FPS; that produced false 96.
  await expect(
    container.getByText("Delivers 4 s (107 frames).", { exact: true }),
  ).toBeVisible();
  const managedQueue = container.getByRole("button", {
    name: "Apply and queue current H3 graph",
  });
  await expect(managedQueue).toBeEnabled();
  expect(await hostWorkflowAuthorityReceipt(page)).toEqual({
    activeStable: true,
    openWorkflowDelta: 0,
    graphLoadWorkflowMatches: [],
  });
  // CRITICAL: queue through the product transaction so prompt/graph currentness is established.
  // A raw `/prompt` fetch produces a real backend event but is correctly rejected by entry.tsx.
  await managedQueue.click();
  await page.waitForFunction(
    (productShellNodeId) =>
      (
        (
          window as unknown as {
            __h3M2508Executed?: Array<{
              node?: unknown;
              workspacePresent: boolean;
            }>;
          }
        ).__h3M2508Executed ?? []
      ).some(
        (event) =>
          event.node === productShellNodeId && event.workspacePresent === true,
      ),
    bootstrap.productShellNodeId,
  );
  await page.waitForFunction(async () => {
    const response = await fetch("/queue");
    if (!response.ok) return false;
    const queue = (await response.json()) as Record<string, unknown>;
    return (
      Array.isArray(queue.queue_pending) &&
      queue.queue_pending.length === 0 &&
      Array.isArray(queue.queue_running) &&
      queue.queue_running.length === 0
    );
  });
  // IMPORTANT: the managed bootstrap consumes its ProductShell projection before the ordinary
  // onProjection acceptance trace. Waiting for `accepted` there times out a successful managed run;
  // the registered Production page is the observable managed-ready boundary for this journey.
  await expect(container.locator("nav [data-page-id]")).toHaveCount(3, {
    timeout: 60_000,
  });
  const preconditionReceipt = await page.evaluate(
    async (nativeAnchorNodeId) => {
      const runtime = window as unknown as {
        __h3M2508Executed?: Array<{
          node?: unknown;
          promptId?: unknown;
          workspacePresent: boolean;
        }>;
        __h3M2508Terminal?: string[];
        __h3M2508ExecutionIdentityTrace?: Array<Record<string, unknown>>;
        __h3ProjectionTrace?: Array<Record<string, unknown>>;
        __h3HostProjectionTrace?: Array<Record<string, unknown>>;
      };
      const queue = (await (await fetch("/queue")).json()) as Record<
        string,
        unknown
      >;
      const pending = Array.isArray(queue.queue_pending)
        ? queue.queue_pending.length
        : -1;
      const running = Array.isArray(queue.queue_running)
        ? queue.queue_running.length
        : -1;
      const executed = runtime.__h3M2508Executed ?? [];
      return {
        pending,
        running,
        nativeAnchorExecuted: executed.some(
          (event) => event.node === nativeAnchorNodeId,
        ),
        productShellWorkspaceCount: executed.filter(
          (event) => event.workspacePresent,
        ).length,
        productShellPromptIds: executed
          .filter((event) => event.workspacePresent)
          .map((event) => event.promptId),
        terminals: [...(runtime.__h3M2508Terminal ?? [])],
        executionIdentityTrace: [
          ...(runtime.__h3M2508ExecutionIdentityTrace ?? []),
        ],
        projectionStages: (runtime.__h3ProjectionTrace ?? []).map(
          (entry) => entry.stage,
        ),
        hostProjectionStages: (runtime.__h3HostProjectionTrace ?? []).map(
          (entry) => entry.stage,
        ),
      };
    },
    bootstrap.nativeAnchorNodeId,
  );
  expect(preconditionCounts).toEqual({
    queue: 1,
    provider: 0,
    generation: 0,
    coordinator: 2,
  });
  expect(preconditionCoordinatorActions).toEqual([
    "prepare_managed_run",
    "submit_managed_run",
  ]);
  await expect
    .poll(() => preconditionPromptAckId, { timeout: 5_000 })
    .toMatch(/^[0-9a-f-]{8,64}$/i);
  if (preconditionPromptAckId === undefined)
    throw new Error("M25-08 queue acknowledgement is unavailable");
  expect(preconditionPromptCensus).toEqual(
    Object.entries(bootstrap.executionPrompt)
      .map(([id, node]) => ({ id, classType: node.class_type }))
      .sort((left, right) => left.id.localeCompare(right.id)),
  );
  expect(preconditionPromptSha256).toBe(
    m2508PromptSha256(bootstrap.executionPrompt),
  );
  expect(
    preconditionPromptCensus?.some(({ classType }) =>
      classType.startsWith("MiniMaxH3"),
    ),
  ).toBe(false);
  expect(preconditionReceipt).toMatchObject({
    pending: 0,
    running: 0,
    nativeAnchorExecuted: false,
    productShellWorkspaceCount: 1,
    productShellPromptIds: [preconditionPromptAckId],
    terminals: [],
  });
  expect(preconditionReceipt.executionIdentityTrace).toHaveLength(3);
  expect(
    preconditionReceipt.executionIdentityTrace.map((entry) => entry.stage),
  ).toEqual(["final_projection", "preflight_projection", "queue_envelope"]);
  expect(
    new Set(
      preconditionReceipt.executionIdentityTrace.map(
        (entry) => entry.fingerprint,
      ),
    ).size,
  ).toBe(1);
  expect(preconditionReceipt.projectionStages).toEqual([]);
  expect(preconditionReceipt.hostProjectionStages).toContain("forward");
  requestPhase = "journey";
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateAttribution,
  );

  const graph = await readVisibleGraph(page);
  const topLevelNodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const nodes = [...topLevelNodes, ...subgraphNodes(graph)] as Array<
    Record<string, unknown>
  >;
  const productShells = topLevelNodes.filter(
    (node) => node.type === "comfyui_h3_context.H3Context.ProductShell",
  );
  const registries = nodes.filter(
    (node) => node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
  );
  expect(productShells).toHaveLength(1);
  expect(registries.length).toBeGreaterThanOrEqual(1);
  const nativeAnchors = topLevelNodes.filter(
    (node) =>
      node.type === "MiniMaxH3ImageToVideo" ||
      node.type === "MiniMaxH3ReferenceToVideo",
  );
  expect(nativeAnchors).toHaveLength(1);
  const anchorInputs = nativeAnchors[0]?.inputs;
  expect(Array.isArray(anchorInputs)).toBe(true);
  const promptInput = (anchorInputs as Array<Record<string, unknown>>).find(
    (input) => input.name === "prompt",
  );
  const graphLinks = Array.isArray(graph.links) ? graph.links : [];
  const promptLink = graphLinks.find(
    (row) => Array.isArray(row) && row[0] === promptInput?.link,
  );
  const shellOutputs = Array.isArray(productShells[0]?.outputs)
    ? productShells[0].outputs
    : [];
  expect(promptLink).toBeDefined();
  expect(promptLink?.[1]).toBe(productShells[0]?.id);
  expect(promptLink?.[3]).toBe(nativeAnchors[0]?.id);
  expect(
    (shellOutputs[Number(promptLink?.[2])] as Record<string, unknown>)?.name,
  ).toBe("prompt");
  expect(
    (anchorInputs as Array<Record<string, unknown>>)[Number(promptLink?.[4])]
      ?.name,
  ).toBe("prompt");

  const pages = container.locator("nav [data-page-id]");
  await expect(pages).toHaveCount(3);
  expect(
    await pages.evaluateAll((buttons) =>
      buttons.map((button) => button.getAttribute("data-page-id")),
    ),
  ).toEqual(["context", "production", "settings"]);
  await container.locator('nav [data-page-id="production"]').click();

  const tabs = container.getByRole("tablist", { name: "Production functions" });
  expect(
    await tabs
      .locator("[role=tab]")
      .evaluateAll((items) =>
        items.map((item) => item.getAttribute("data-h3-director-function")),
      ),
  ).toEqual(["production_workbench", "clip_editor"]);
  const production = tabs.getByRole("tab", { name: "Production" });
  const clipEditor = tabs.getByRole("tab", { name: "Clip editor" });
  await production.focus();
  await page.keyboard.press("ArrowRight");
  await expect(clipEditor).toBeFocused();
  await expect(production).toHaveAttribute("aria-selected", "true");
  await expect(container.getByRole("region", SUMMARY)).toHaveCount(0);
  await page.keyboard.press("Enter");
  await expect(clipEditor).toHaveAttribute("aria-selected", "true");

  // M25-44 (one NLE): the Clip editor function shows the full-editor launcher and the project
  // summary. The compact editor this journey used to drive (selected-VIDEO preview, playhead,
  // per-clip move) is retired; those behaviours are proven on the full editor by
  // `nleWorkspaceHost` and `nleCausalJourney`. The summary owns the causal lifecycle.
  const editor = container.getByRole("region", SUMMARY);
  await expect(container.getByRole("region", LEGACY_EDITOR)).toHaveCount(0);
  const createResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith(
        "/h3-context/v1/authoring/action",
      ),
  );
  await editor
    .getByRole("button", { name: "Start authoring from this context" })
    .click();
  const createResponse = await createResponsePromise;
  expect(createResponse.status()).toBe(201);
  await expect.poll(() => routeCounts.authoringAction).toBe(1);
  expect(actionBodies[0]?.action).toBe("create_authoring_workspace");
  console.log(
    `M25_08_CREATE_RECEIPT=${JSON.stringify({
      status: createResponse.status(),
      actionCalls: routeCounts.authoringAction,
    })}`,
  );
  await expect(editor.getByRole("status")).toHaveText(
    "Authoring workspace ready. Open the full editor to load its timeline.",
  );
  // No preview, audio or pairing surface stands in for the editor in the sidebar.
  await expect(container.locator("video")).toHaveCount(0);
  await expect(container.locator("[data-kind=audio]")).toHaveCount(0);
  await expect(
    container.getByRole("button", { name: /volume|mute|pair|unlink/i }),
  ).toHaveCount(0);
  await expect(container.locator("[data-h3-nle-control]")).toHaveCount(0);

  await editor.getByRole("button", { name: "Refresh workspace" }).click();
  await expect.poll(() => routeCounts.authoringAction).toBe(2);
  expect(actionBodies[1]?.action).toBe("read_projection");

  await container.locator('nav [data-page-id="context"]').click();
  await container.locator('nav [data-page-id="production"]').click();
  await expect(
    container.getByRole("tab", { name: "Clip editor" }),
  ).toHaveAttribute("aria-selected", "true");
  const remounted = container.getByRole("region", SUMMARY);
  await remounted.getByRole("button", { name: "Release workspace" }).click();
  await remounted.getByRole("button", { name: "Confirm release" }).click();
  await expect(remounted.getByRole("status")).toHaveText(
    "The authoring workspace was released.",
  );

  expect(routeCounts.authoringAction).toBe(3);
  expect(routeCounts.authoringPreview).toBe(0);
  expect(routeCounts.queue).toBe(0);
  expect(routeCounts.provider).toBe(0);
  expect(routeCounts.generation).toBe(0);
  expect(routeCounts.coordinator).toBe(0);
  const workflowReceipt = await hostWorkflowAuthorityReceipt(page);
  console.log(`M25_08_WORKFLOW_RECEIPT=${JSON.stringify(workflowReceipt)}`);
  expect(workflowReceipt.activeStable).toBe(true);
  expect(workflowReceipt.openWorkflowDelta).toBe(0);
  expect(workflowReceipt.graphLoadWorkflowMatches).toEqual([true]);
  expect(networkAttribution.snapshot()).toMatchObject({
    interactionRemoteCount: 0,
    interactionProviderCount: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateAttribution);
});
