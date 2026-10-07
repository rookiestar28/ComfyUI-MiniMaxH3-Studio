import { readFile } from "node:fs/promises";
import { isAbsolute } from "node:path";

import { expect, test, type Locator, type Page } from "../../host/fixture";

// prettier-ignore
import { MAX_MEDIA_PREVIEW_BYTES, MEDIA_PREVIEW_REQUEST_SCHEMA, MEDIA_PREVIEW_ROUTE, } from "../../../../src/host/productionMediaPreview";
// prettier-ignore
import { appModeQueueDiagnostic, assertCandidateBundleInjection, beginSettledH3InteractionPhase, candidateInjectionCount, expectCandidateInteractionNetworkLocal, hostUrl, instrumentHostGraphLoads, monitorCandidateInitiatorNetwork, monitorH3Network, openH3AppModeTab, reportedArtifactNames, sampleProgress, SAMPLE_AUTHORIZED, setSupportedH3Language, supportedHostQueueCounts, waitForH3Registration, waitForHostGraphSettled, watchHostExecution, } from "../../host/environment";

/**
 * M24-07 / M24-08 existing route and M24-09 preview discoverability on the supplied host.
 *
 * The owner's own expanded I2VA workflow (already carrying the H3 Context nodes) is loaded onto the
 * canvas, the Sidebar authors intent and duration, and the graph is queued exactly once through the
 * ordinary queue with one real inference. Only repository-owned facts are asserted: the authored
 * intent and shared duration root, the native length's dependency on that root, one queue call,
 * the captured workflow identity and open-tab count, and that the owner's model and sampling inputs
 * reach execution as the owner set them. The Production clip openers are then exercised one click
 * at a time. The workflow path is private input: nothing from it is logged or retained.
 */

const WORKFLOW_PATH = process.env.H3_CONTEXT_M24_EXISTING_WORKFLOW_PATH ?? "";
const PRODUCTION_ACTION_ROUTE = "/h3-context/v1/production/action";
const INTENT = "A calm, steady shot that holds on the subject in soft light.";
const PLACEHOLDER_INTENT = "Placeholder intent before Sidebar authoring.";

type WorkflowNode = {
  id: number;
  type: string;
  widgets_values?: unknown;
  inputs?: Array<{ name?: string; link?: number | null }>;
};
type Workflow = { nodes: WorkflowNode[]; links: unknown[][] };
type PromptNode = { class_type?: unknown; inputs?: Record<string, unknown> };

/**
 * Model and sampling inputs the owner set, by node id, in executed-prompt input names.
 *
 * IMPORTANT: a widget the owner converted to a linked input executes the linked value, and its
 * serialized widget value is stale. Comparing that value with the executed link reports an owner
 * setting as changed, so linked inputs are left out (B-M1605-HARNESS-02).
 */
function ownerSettings(
  workflow: Workflow,
): Record<string, Record<string, unknown>> {
  const names: Record<string, readonly string[]> = {
    UNETLoader: ["unet_name", "weight_dtype"],
    CLIPLoader: ["clip_name", "type"],
    VAELoader: ["vae_name"],
    LoraLoaderModelOnly: ["lora_name", "strength_model"],
    KSamplerSelect: ["sampler_name"],
    BasicScheduler: ["scheduler", "steps", "denoise"],
    RandomNoise: ["noise_seed"],
  };
  const settings: Record<string, Record<string, unknown>> = {};
  for (const node of workflow.nodes) {
    const keys = names[node.type];
    if (keys === undefined || !Array.isArray(node.widgets_values)) continue;
    const linked = new Set(
      (node.inputs ?? [])
        .filter((input) => input.link !== null && input.link !== undefined)
        .map((input) => input.name),
    );
    settings[String(node.id)] = Object.fromEntries(
      keys.flatMap((key, index) =>
        linked.has(key)
          ? []
          : [[key, (node.widgets_values as unknown[])[index]] as const],
      ),
    );
  }
  return settings;
}

async function workflowReceipt(page: Page) {
  return await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any } };
      __h3M24WorkflowAuthority?: object;
    };
    const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
    return {
      openCount: Array.isArray(store?.openWorkflows)
        ? store.openWorkflows.length
        : -1,
      sameWorkflow:
        runtime.__h3M24WorkflowAuthority !== undefined &&
        runtime.__h3M24WorkflowAuthority === store?.activeWorkflow,
    };
  });
}

async function queueCalls(page: Page): Promise<number> {
  return await page.evaluate(
    () =>
      (window as unknown as { __h3M24QueueCalls?: number }).__h3M24QueueCalls ??
      0,
  );
}

test("M24-07/M24-08 existing expanded graph queues once and M24-09 openers preview without actions", async ({
  context,
  page,
}) => {
  test.skip(
    !SAMPLE_AUTHORIZED || WORKFLOW_PATH === "" || !isAbsolute(WORKFLOW_PATH),
    "requires the standing weight-sample designation and the owner's existing workflow path",
  );
  test.setTimeout(45 * 60_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const workflow = JSON.parse(
    await readFile(WORKFLOW_PATH, "utf-8"),
  ) as Workflow;
  // IMPORTANT: the owner's intent text is private. Replace it before the graph reaches the page so
  // no failure snapshot can carry it; the placeholder also differs from INTENT, so the executed
  // intent still proves the Sidebar wrote it.
  const requestNodes = workflow.nodes.filter(
    (node) => node.type === "comfyui_h3_context.H3Context.Request",
  );
  expect(requestNodes).toHaveLength(1);
  const requestWidgets = requestNodes[0]!.widgets_values;
  if (!Array.isArray(requestWidgets) || typeof requestWidgets[1] !== "string")
    throw new Error("the existing Request has no intent widget");
  requestWidgets[1] = PLACEHOLDER_INTENT;
  const settings = ownerSettings(workflow);
  expect(Object.keys(settings).length).toBeGreaterThanOrEqual(4);

  const allowedOrigin = new URL(hostUrl).origin;
  const networkAttribution = monitorH3Network(page, allowedOrigin);
  const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const promptPosts: number[] = [];
  const productionActions: number[] = [];
  const previewRequests: Array<{ handle: string }> = [];
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() !== "POST") return;
    if (pathname === "/prompt" || pathname === "/api/prompt")
      promptPosts.push(Date.now());
    if (pathname.endsWith(PRODUCTION_ACTION_ROUTE))
      productionActions.push(Date.now());
    // IMPORTANT (B-M1605-HARNESS-03): the host's fetchApi prefixes `/api`, so owned routes are
    // matched by suffix; an exact pathname never sees a request.
    if (pathname.endsWith(MEDIA_PREVIEW_ROUTE)) {
      let handle = "";
      try {
        const body = request.postDataJSON() as { output_handle?: unknown };
        if (typeof body.output_handle === "string") handle = body.output_handle;
      } catch {
        handle = "invalid";
      }
      previewRequests.push({ handle });
    }
  });

  // Diagnostic only: record the fixed `code: the ...` messages of the product's internal errors, so a
  // refusal names the check that fired. Anything else (possible user content) is never recorded.
  await page.addInitScript(() => {
    const messages: string[] = [];
    (
      window as unknown as { __h3M24ErrorMessages?: string[] }
    ).__h3M24ErrorMessages = messages;
    // Code-authored sentences (`code: the ...`, which may name Production) and bare client error
    // codes such as `invalid_response`.
    const safe =
      /^(?:[a-z_]{1,40}: the [A-Za-z0-9 ,.'()/-]{1,156}|[a-z_]{1,40})$/;
    window.Error = new Proxy(window.Error, {
      construct(target, args, newTarget) {
        if (
          typeof args[0] === "string" &&
          safe.test(args[0]) &&
          messages.length < 64
        )
          messages.push(args[0]);
        return Reflect.construct(target, args, newTarget);
      },
    });
  });
  const initialInjectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, initialInjectionCount);
  await setSupportedH3Language(page, "en");
  await instrumentHostGraphLoads(page);
  await waitForHostGraphSettled(page);

  // The owner opens their own workflow; that tab is the identity the run must keep.
  const loaded = await page.evaluate(async (graph) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    await app.loadGraphData(graph);
    await new Promise((settle) => setTimeout(settle, 0));
    return (app.graph?._nodes ?? []).length as number;
  }, workflow);
  expect(loaded).toBe(workflow.nodes.length);
  await waitForHostGraphSettled(page);
  const captured = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: { app: { app: any }; api: { api: any } };
      __h3M24WorkflowAuthority?: object;
      __h3M24QueueCalls?: number;
    };
    const store = runtime.comfyAPI.app.app.extensionManager?.workflow;
    const active = store?.activeWorkflow;
    if (active === null || typeof active !== "object") return -1;
    runtime.__h3M24WorkflowAuthority = active;
    runtime.__h3M24QueueCalls = 0;
    const api = runtime.comfyAPI.api.api;
    const original = api.queuePrompt.bind(api);
    api.queuePrompt = async function (...args: unknown[]) {
      runtime.__h3M24QueueCalls = (runtime.__h3M24QueueCalls ?? 0) + 1;
      return await original(...args);
    };
    return Array.isArray(store?.openWorkflows)
      ? store.openWorkflows.length
      : -1;
  });
  expect(captured).toBeGreaterThan(0);
  const openCountBefore = captured;

  const container = await openH3AppModeTab(page, "h3-context-m24-existing");
  await beginSettledH3InteractionPhase(
    page,
    networkAttribution,
    candidateNetworkAttribution,
  );
  await watchHostExecution(page);

  // M24-07: the expanded existing graph is admitted and the Sidebar authors the shared duration.
  const submit = container.locator('[data-h3-focus-key="app-submit"]');
  await expect(submit).toHaveText("Apply and queue current H3 graph", {
    timeout: 60_000,
  });
  await container.locator('[data-h3-focus-key="app-intent"]').fill(INTENT);
  await container
    .locator('[data-h3-focus-key="app-duration-seconds"]')
    .fill("8");
  await expect(
    container.getByText("Delivers 8 s (192 frames).", { exact: true }),
  ).toBeVisible();
  await expect(submit).toBeEnabled({ timeout: 120_000 });
  const promptPostsBefore = promptPosts.length;
  const fromSubmission = (await sampleProgress(page)).submitted;
  // IMPORTANT: one click is one explicit operator start. Never retry it inside this live row.
  await submit.click();
  // Content-free diagnostics (state codes, phases, redacted journal) if the queue is not reached.
  const submissionDeadline = Date.now() + 180_000;
  for (;;) {
    if ((await sampleProgress(page)).submitted - fromSubmission >= 1) break;
    const diagnostic = await appModeQueueDiagnostic(
      page,
      "h3-context-m24-existing",
      "__h3QueueCount",
    );
    if (diagnostic.shellStatus === "error" || Date.now() >= submissionDeadline)
      throw new Error(
        `existing-route submission did not reach the queue: ${JSON.stringify({
          ...diagnostic,
          queueCalls: await queueCalls(page),
          // Which values the visible owned nodes hold now: the placeholder the canvas was loaded
          // with, or the Sidebar's authored intent (a candidate value that reached the canvas).
          visibleOwned: await page.evaluate(
            ({ requestId, authored, placeholder }) => {
              const app = (
                window as unknown as { comfyAPI: { app: { app: any } } }
              ).comfyAPI.app.app;
              const graph = app.graph.serialize() as {
                nodes: Array<{ id: number; widgets_values?: unknown }>;
              };
              const request = graph.nodes.find((node) => node.id === requestId);
              const intent = Array.isArray(request?.widgets_values)
                ? request.widgets_values[1]
                : undefined;
              return {
                intent:
                  intent === placeholder
                    ? "placeholder"
                    : intent === authored
                      ? "authored"
                      : "other",
                floats: graph.nodes
                  .filter(
                    (node) =>
                      Array.isArray(node.widgets_values) &&
                      (node as { type?: string }).type === "PrimitiveFloat",
                  )
                  .map((node) => (node.widgets_values as unknown[])[0]),
              };
            },
            {
              requestId: requestNodes[0]!.id,
              authored: INTENT,
              placeholder: PLACEHOLDER_INTENT,
            },
          ),
          errorMessages: await page.evaluate(
            () =>
              (window as unknown as { __h3M24ErrorMessages?: string[] })
                .__h3M24ErrorMessages ?? [],
          ),
          promptPosts: promptPosts.length - promptPostsBefore,
        })}`,
      );
    await page.waitForTimeout(500);
  }
  await expect
    .poll(async () => (await sampleProgress(page, fromSubmission)).started, {
      timeout: 180_000,
    })
    .toBe(1);
  await expect
    .poll(async () => (await sampleProgress(page, fromSubmission)).finished, {
      timeout: 40 * 60_000,
      intervals: [1_000, 2_000, 5_000],
    })
    .toBe(1);
  const progress = await sampleProgress(page, fromSubmission);
  expect(progress.errors).toEqual([]);
  expect(reportedArtifactNames(progress.outputs).length).toBeGreaterThan(0);
  // Content-free diagnostics (state codes, phases, projection traces) if no projection arrives.
  const projectionDeadline = Date.now() + 180_000;
  for (;;) {
    if (
      (await container.locator('[data-shell-status="projected"]').count()) === 1
    )
      break;
    const diagnostic = await appModeQueueDiagnostic(
      page,
      "h3-context-m24-existing",
      "__h3QueueCount",
    );
    if (diagnostic.shellStatus === "error" || Date.now() >= projectionDeadline)
      throw new Error(
        `existing-route run finished without a projection: ${JSON.stringify({
          ...diagnostic,
          queueCalls: await queueCalls(page),
          errorMessages: await page.evaluate(
            () =>
              (window as unknown as { __h3M24ErrorMessages?: string[] })
                .__h3M24ErrorMessages ?? [],
          ),
          promptPosts: promptPosts.length - promptPostsBefore,
        })}`,
      );
    await page.waitForTimeout(1_000);
  }
  expect(await queueCalls(page)).toBe(1);
  expect(promptPosts.length - promptPostsBefore).toBe(1);
  expect(await workflowReceipt(page)).toEqual({
    openCount: openCountBefore,
    sameWorkflow: true,
  });

  // The owned projection, read from the prompt the host actually executed.
  const promptIds = await page.evaluate(
    (from) =>
      (
        (window as unknown as { __h3Sample?: { promptIds?: string[] } })
          .__h3Sample?.promptIds ?? []
      ).slice(from),
    fromSubmission,
  );
  expect(promptIds).toHaveLength(1);
  const history = (await (
    await page.request.get(
      new URL(`/history/${encodeURIComponent(promptIds[0]!)}`, hostUrl).href,
    )
  ).json()) as Record<string, { prompt?: unknown[] }>;
  const executed = (history[promptIds[0]!]?.prompt?.[2] ?? {}) as Record<
    string,
    PromptNode
  >;
  const byType = (type: string) =>
    Object.entries(executed).filter(([, node]) => node.class_type === type);
  const requests = byType("comfyui_h3_context.H3Context.Request");
  const anchors = byType("MiniMaxH3ImageToVideo");
  expect(requests).toHaveLength(1);
  expect(anchors).toHaveLength(1);
  const requestInputs = requests[0]![1].inputs ?? {};
  // A boolean, so a failure never prints the executed intent text.
  expect(requestInputs.user_intent === INTENT).toBe(true);
  const durationLink = requestInputs.duration_seconds;
  expect(Array.isArray(durationLink)).toBe(true);
  const rootId = String((durationLink as unknown[])[0]);
  expect(executed[rootId]?.class_type).toBe("PrimitiveFloat");
  expect(executed[rootId]?.inputs?.value).toBe(8);
  // The native length depends transitively on the same shared root.
  const dependsOnRoot = (start: unknown): boolean => {
    const seen = new Set<string>();
    const queue: unknown[] = [start];
    while (queue.length > 0 && seen.size < 256) {
      const value = queue.shift();
      if (!Array.isArray(value) || value.length !== 2) continue;
      const id = String(value[0]);
      if (id === rootId) return true;
      if (seen.has(id)) continue;
      seen.add(id);
      for (const input of Object.values(executed[id]?.inputs ?? {}))
        queue.push(input);
    }
    return false;
  };
  expect(dependsOnRoot(anchors[0]![1].inputs?.length)).toBe(true);
  // Only the names of changed settings are reported, never their values.
  const changedSettings = Object.entries(settings).flatMap(([id, expected]) =>
    Object.entries(expected)
      .filter(([key, value]) => executed[id]?.inputs?.[key] !== value)
      .map(([key]) => `${id}.${key}`),
  );
  expect(changedSettings).toEqual([]);

  // M24-08 / M24-09: Production previews the result without a second queue or a Production action.
  await container.locator('[data-h3-focus-key="page-production"]').click();
  await expect(
    container.getByText("1 of 1 segments", { exact: true }),
  ).toBeVisible({ timeout: 60_000 });
  const scrollOwner = page.locator(".sidebar-content-container").last();

  const exercise = async (
    opener: Locator,
    key: "Enter" | "Space",
    playerLabel: string,
  ) => {
    await opener.scrollIntoViewIfNeeded();
    await opener.focus();
    const before = {
      queue: await queueCalls(page),
      prompts: promptPosts.length,
      actions: productionActions.length,
      previews: previewRequests.length,
    };
    const response = page.waitForResponse(
      (candidate) =>
        candidate.request().method() === "POST" &&
        new URL(candidate.url()).pathname.endsWith(MEDIA_PREVIEW_ROUTE),
      { timeout: 60_000 },
    );
    // Diagnostic only: whether the key reached the opener as an activation. Content-free facts.
    const activation = await opener.evaluate((node, pressed) => {
      const facts = {
        disabled: (node as HTMLButtonElement).disabled,
        focused: document.activeElement === node,
        keydownPrevented: null as boolean | null,
        keyupPrevented: null as boolean | null,
        clicked: false,
      };
      const name = pressed === "Space" ? " " : pressed;
      window.addEventListener(
        "keydown",
        (event) => {
          if (event.key === name)
            facts.keydownPrevented = event.defaultPrevented;
        },
        { once: true },
      );
      window.addEventListener(
        "keyup",
        (event) => {
          if (event.key === name) facts.keyupPrevented = event.defaultPrevented;
        },
        { once: true },
      );
      node.addEventListener("click", () => (facts.clicked = true), {
        once: true,
      });
      (
        window as unknown as { __h3M24Activation?: typeof facts }
      ).__h3M24Activation = facts;
      return { disabled: facts.disabled, focused: facts.focused };
    }, key);
    await page.keyboard.press(key);
    const previewResponse = await response.catch(async (error: unknown) => {
      const after = await page.evaluate(() => ({
        ...(window as unknown as { __h3M24Activation?: object })
          .__h3M24Activation,
        panel: document.getElementById("h3p-preview-panel") !== null,
        alerts: document.querySelectorAll(
          '#h3-context-m24-existing [role="alert"]',
        ).length,
      }));
      throw new Error(
        `opener ${playerLabel} (${key}) produced no preview response: ${JSON.stringify(
          {
            before: activation,
            after,
            previews: previewRequests.length - before.previews,
            actions: productionActions.length - before.actions,
          },
        )}`,
        { cause: error },
      );
    });
    const contentLength = Number(
      await previewResponse.headerValue("content-length"),
    );
    expect({
      status: previewResponse.status(),
      schema: (previewResponse.request().postDataJSON() as { schema?: unknown })
        .schema,
      contentType: await previewResponse.headerValue("content-type"),
      noStore: await previewResponse.headerValue("cache-control"),
      bounded:
        Number.isSafeInteger(contentLength) &&
        contentLength > 0 &&
        contentLength <= MAX_MEDIA_PREVIEW_BYTES,
    }).toEqual({
      status: 200,
      schema: MEDIA_PREVIEW_REQUEST_SCHEMA,
      contentType: "video/mp4",
      noStore: "no-store",
      bounded: true,
    });
    const player = container.getByLabel(playerLabel);
    await expect(player).toBeVisible({ timeout: 60_000 });
    await expect(container.locator("#h3p-preview-panel")).toBeFocused();
    // M24-09 (as its hermetic journey measures it): the player lies wholly inside the scrollport
    // that owns it, which is the nearest scrolling ancestor, and inside the window. Only rounded
    // rectangle numbers and the owner's class token are reported.
    const geometry = await player.evaluate((node) => {
      let owner = node.parentElement;
      while (owner !== null) {
        const style = getComputedStyle(owner);
        if (
          /(auto|scroll)/.test(style.overflowY) &&
          owner.scrollHeight > owner.clientHeight
        )
          break;
        owner = owner.parentElement;
      }
      const target = node.getBoundingClientRect();
      const bounds = owner?.getBoundingClientRect() ?? {
        top: 0,
        bottom: window.innerHeight,
        left: 0,
        right: window.innerWidth,
      };
      const inside = (box: {
        top: number;
        bottom: number;
        left: number;
        right: number;
      }) =>
        target.top >= box.top - 1 &&
        target.bottom <= box.bottom + 1 &&
        target.left >= box.left - 1 &&
        target.right <= box.right + 1;
      return {
        contained: inside(bounds),
        inWindow: inside({
          top: 0,
          bottom: window.innerHeight,
          left: 0,
          right: window.innerWidth,
        }),
        detail: {
          owner: owner === null ? "document" : owner.className.split(" ")[0],
          topDelta: Math.round(target.top - bounds.top),
          bottomDelta: Math.round(bounds.bottom - target.bottom),
          playerHeight: Math.round(target.height),
          ownerHeight: Math.round(bounds.bottom - bounds.top),
          lastSidebarContainerContains:
            [...document.querySelectorAll(".sidebar-content-container")]
              .at(-1)
              ?.contains(node) === true,
        },
      };
    });
    expect(
      { contained: geometry.contained, inWindow: geometry.inWindow },
      JSON.stringify(geometry.detail),
    ).toEqual({ contained: true, inWindow: true });
    expect({
      queue: (await queueCalls(page)) - before.queue,
      prompts: promptPosts.length - before.prompts,
      actions: productionActions.length - before.actions,
      previews: previewRequests.length - before.previews,
    }).toEqual({ queue: 0, prompts: 0, actions: 0, previews: 1 });
    const handle = previewRequests[previewRequests.length - 1]!.handle;
    expect(handle).toMatch(/^out_[A-Za-z0-9_-]{16,128}$/);
    await container.getByRole("button", { name: "Close preview" }).click();
    await expect(player).toHaveCount(0);
    await expect(opener).toBeFocused();
    return handle;
  };

  await expect(scrollOwner).toBeVisible();
  const sequenceOpener = container
    .locator('section[aria-labelledby="h3p-sequence"]')
    .getByRole("button", { name: "Preview segment 1" });
  const segmentOpener = container
    .locator('section[aria-labelledby="h3p-segment"]')
    .getByRole("button", { name: "Preview segment 1" });
  await expect(sequenceOpener).toHaveCount(1);
  await expect(segmentOpener).toHaveCount(1);
  const sequenceHandle = await exercise(
    sequenceOpener,
    "Enter",
    "Segment 1 preview",
  );
  const segmentHandle = await exercise(
    segmentOpener,
    "Space",
    "Segment 1 preview",
  );
  // Both openers name the same segment output.
  expect(segmentHandle).toBe(sequenceHandle);
  const aggregateOpener = container.getByRole("button", {
    name: "Preview aggregate output",
    exact: true,
  });
  const aggregateCount = await aggregateOpener.count();
  test.info().annotations.push({
    type: "aggregate-opener-count",
    description: String(aggregateCount),
  });
  if (aggregateCount === 1)
    await exercise(aggregateOpener, "Enter", "Aggregate output preview");

  expect(await workflowReceipt(page)).toEqual({
    openCount: openCountBefore,
    sameWorkflow: true,
  });
  expect(await queueCalls(page)).toBe(1);
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
  expect(networkAttribution.snapshot().interactionProviderCount).toBe(0);
});
