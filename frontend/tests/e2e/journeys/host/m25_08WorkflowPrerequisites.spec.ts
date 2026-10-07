import { expect, test } from "../../host/fixture";

import { HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS } from "../../../fixtures/hostBehaviourCatalogue";
import {
  buildM2508HostBootstrap,
  ensureM2508CapturedWorkflowAuthority,
  materializeM2508VisiblePromptInCapturedWorkflow,
} from "../../host/m25_08Bootstrap";
import {
  assertCandidateBundleInjection,
  candidateBackendMode,
  candidateBackendRuntime,
  candidateBundle,
  candidateInjectionCount,
  hostUrl,
  instrumentHostGraphLoads,
  openH3AppModeTab,
  waitForHostGraphSettled,
  waitForH3Registration,
} from "../../host/environment";
import { classifyM2508PostRoute } from "../../host/m25_08RequestClassification";

type M2508ShapeDiff = Readonly<{
  path: string;
  expectedKind: string;
  actualKind: string;
}>;

function m2508ValueKind(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return `array:${value.length}`;
  return typeof value === "object"
    ? `object:${Object.keys(value as Record<string, unknown>)
        .sort()
        .join(",")}`
    : typeof value;
}

function m2508ShapeDiff(
  expected: unknown,
  actual: unknown,
  path = "$",
  rows: M2508ShapeDiff[] = [],
): M2508ShapeDiff[] {
  if (rows.length >= 64) return rows;
  if (Object.is(expected, actual)) return rows;
  if (Array.isArray(expected) && Array.isArray(actual)) {
    if (expected.length !== actual.length)
      rows.push({
        path,
        expectedKind: m2508ValueKind(expected),
        actualKind: m2508ValueKind(actual),
      });
    for (
      let index = 0;
      index < Math.min(expected.length, actual.length);
      index += 1
    )
      m2508ShapeDiff(expected[index], actual[index], `${path}[${index}]`, rows);
    return rows;
  }
  if (
    expected !== null &&
    actual !== null &&
    typeof expected === "object" &&
    typeof actual === "object" &&
    !Array.isArray(expected) &&
    !Array.isArray(actual)
  ) {
    const expectedObject = expected as Record<string, unknown>;
    const actualObject = actual as Record<string, unknown>;
    for (const key of new Set([
      ...Object.keys(expectedObject),
      ...Object.keys(actualObject),
    ]))
      m2508ShapeDiff(
        expectedObject[key],
        actualObject[key],
        `${path}.${key}`,
        rows,
      );
    return rows;
  }
  rows.push({
    path,
    expectedKind: m2508ValueKind(expected),
    actualKind: m2508ValueKind(actual),
  });
  return rows;
}

function m2508OwnedPromptProjection(
  value: unknown,
  template: Readonly<
    Record<
      string,
      { class_type: string; inputs: Readonly<Record<string, unknown>> }
    >
  >,
): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error("M25-08 compiled prompt is unavailable");
  const projected: Record<string, unknown> = {};
  const actual = value as Record<string, unknown>;
  for (const [id, expected] of Object.entries(template)) {
    const anchor = expected.class_type.startsWith("MiniMaxH3");
    if (!anchor && !expected.class_type.startsWith("comfyui_h3_context."))
      continue;
    const candidate = actual[id];
    if (
      candidate === null ||
      typeof candidate !== "object" ||
      Array.isArray(candidate)
    )
      throw new Error("M25-08 compiled node is unavailable");
    // IMPORTANT: shared-host metadata and foreign node widgets may change between starts.
    // Compare the written H3 inputs and the native anchor's prompt binding, never whole prompts.
    const node = candidate as Record<string, unknown>;
    const inputs = node.inputs as Record<string, unknown> | undefined;
    projected[id] = {
      class_type: node.class_type,
      inputs: Object.fromEntries(
        (anchor ? ["prompt"] : Object.keys(expected.inputs)).map((key) => [
          key,
          inputs?.[key],
        ]),
      ),
    };
  }
  return projected;
}

test("M25-08 exact host satisfies workflow lifecycle prerequisite rows", async ({
  context,
  page,
}, testInfo) => {
  test.setTimeout(60_000);
  if (
    candidateBundle === null ||
    candidateBackendMode !== "exact" ||
    candidateBackendRuntime === null
  ) {
    testInfo.annotations.push({
      type: "m25-08-host-prerequisite",
      description: "NOT_RUN: exact candidate runtime is not bound",
    });
    test.skip(true, "exact candidate bundle and backend parity are required");
    return;
  }
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");

  const routeCounts = {
    queue: 0,
    provider: 0,
    generation: 0,
    coordinator: 0,
  };
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    const path = new URL(request.url()).pathname;
    const routeClass = classifyM2508PostRoute(path);
    if (routeClass !== "other") routeCounts[routeClass] += 1;
  });

  const injectionBefore = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injectionBefore);

  const initial = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const store = app.extensionManager?.workflow;
    return {
      activeNull: store?.activeWorkflow === null,
      openCount: Array.isArray(store?.openWorkflows)
        ? store.openWorkflows.length
        : -1,
      hasCreate: typeof store?.createNewTemporary === "function",
      hasOpen: typeof store?.openWorkflow === "function",
    };
  });
  expect(initial).toEqual({
    activeNull: true,
    openCount: 0,
    hasCreate: true,
    hasOpen: true,
  });

  await instrumentHostGraphLoads(page);
  await openH3AppModeTab(page, "h3-context-m25-08-prerequisite");
  // IMPORTANT: mirror the causal journey's dialog/lifecycle order here; materializing before the
  // workflow-template dialog settles only qualifies null/empty and misses existing-active seams.
  await waitForHostGraphSettled(page);
  const settledAuthority = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const store = app.extensionManager.workflow;
    return {
      activeIsMember: store.openWorkflows.includes(store.activeWorkflow),
      openCount: store.openWorkflows.length,
      activePath: store.activeWorkflow?.path,
    };
  });
  expect(settledAuthority.activeIsMember).toBe(true);
  expect(settledAuthority.openCount).toBe(1);
  expect(settledAuthority.activePath).toMatch(/^workflows\/.+\.json$/);

  const bootstrap = buildM2508HostBootstrap(
    "m25_08_acceptance/00000000/m25_08_authorized_video_v1.mp4",
  );
  await page.evaluate(
    materializeM2508VisiblePromptInCapturedWorkflow,
    bootstrap.visiblePrompt,
  );
  await page.evaluate(ensureM2508CapturedWorkflowAuthority);
  const afterFirst = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const store = app.extensionManager.workflow;
    return {
      activeIsMember:
        typeof store.activeWorkflow === "object" &&
        store.activeWorkflow !== null &&
        store.openWorkflows.includes(store.activeWorkflow),
      openCount: store.openWorkflows.length,
      activePath: store.activeWorkflow?.path,
    };
  });
  expect(afterFirst).toEqual(settledAuthority);

  await page.evaluate(ensureM2508CapturedWorkflowAuthority);
  const afterSecond = await page.evaluate(() => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const store = app.extensionManager.workflow;
    return {
      activeIsMember: store.openWorkflows.includes(store.activeWorkflow),
      openCount: store.openWorkflows.length,
      activePath: store.activeWorkflow?.path,
      extensionNames: (app.extensions ?? [])
        .map((extension: { name?: unknown }) => extension.name)
        .filter((name: unknown): name is string => typeof name === "string")
        .sort(),
      sidebarIds: (app.extensionManager.getSidebarTabs?.() ?? [])
        .map((tab: { id?: unknown }) => tab.id)
        .filter((id: unknown): id is string => typeof id === "string")
        .sort(),
    };
  });
  expect(afterSecond.activeIsMember).toBe(true);
  expect(afterSecond.openCount).toBe(1);
  expect(afterSecond.activePath).toBe(afterFirst.activePath);
  expect(
    afterSecond.sidebarIds.filter((id: string) => id === "h3-context"),
  ).toEqual(["h3-context"]);
  const foreignExtensionCount = afterSecond.extensionNames.filter(
    (name: string) => !name.startsWith("comfyui-h3-context."),
  ).length;
  const foreignSidebarCount = afterSecond.sidebarIds.filter(
    (id: string) => id !== "h3-context",
  ).length;
  expect(foreignExtensionCount).toBeGreaterThan(0);
  expect(foreignSidebarCount).toBeGreaterThan(0);
  expect(HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS).toContain("null_empty_exact");
  expect(HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS).toContain("existing_active");
  expect(routeCounts).toEqual({
    queue: 0,
    provider: 0,
    generation: 0,
    coordinator: 0,
  });

  const materialized = await page.evaluate(() => {
    const app = (
      window as unknown as {
        comfyAPI: {
          app: {
            app: {
              graph: { serialize(): unknown };
              rootGraph: { serialize(): unknown };
              canvas: { graph?: unknown };
              extensionManager?: {
                workflow?: {
                  activeWorkflow?: { activeState?: unknown } | null;
                };
              };
            };
          };
        };
      }
    ).comfyAPI.app.app;
    const ownedTypes = new Set([
      "comfyui_h3_context.H3Context.ReferenceRegistry",
      "comfyui_h3_context.H3Context.ProductShell",
    ]);
    const project = (value: unknown) => {
      const nodes =
        value !== null &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        Array.isArray((value as { nodes?: unknown }).nodes)
          ? ((value as { nodes: unknown[] }).nodes as Array<
              Record<string, unknown>
            >)
          : [];
      return nodes
        .filter(
          (node) => typeof node.type === "string" && ownedTypes.has(node.type),
        )
        .map((node) => ({ id: node.id, type: node.type }))
        .sort((left, right) => String(left.id).localeCompare(String(right.id)));
    };
    const summarize = (value: unknown) => {
      const nodes =
        value !== null &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        Array.isArray((value as { nodes?: unknown }).nodes)
          ? ((value as { nodes: unknown[] }).nodes as Array<
              Record<string, unknown>
            >)
          : [];
      return nodes
        .map((node) => ({ id: node.id, type: node.type }))
        .sort((left, right) => String(left.id).localeCompare(String(right.id)));
    };
    const root = app.rootGraph.serialize();
    const activeState =
      app.extensionManager?.workflow?.activeWorkflow?.activeState;
    return {
      graphIsRootGraph: app.graph === app.rootGraph,
      canvasUsesRootGraph: app.canvas.graph === app.rootGraph,
      rootOwnedProjection: project(root),
      graphOwnedProjection: project(app.graph.serialize()),
      activeOwnedProjection: project(activeState),
      rootNodeSummary: summarize(root),
      activeNodeSummary: summarize(activeState),
    };
  });
  console.log(
    `M25_08_MATERIALIZATION_PREREQUISITE=${JSON.stringify(materialized)}`,
  );
  expect(materialized.graphIsRootGraph).toBe(true);
  expect(materialized.canvasUsesRootGraph).toBe(true);
  expect(materialized.rootOwnedProjection).toEqual([
    {
      id: 3,
      type: "comfyui_h3_context.H3Context.ReferenceRegistry",
    },
    {
      id: 8,
      type: "comfyui_h3_context.H3Context.ProductShell",
    },
  ]);
  expect(materialized.graphOwnedProjection).toEqual(
    materialized.rootOwnedProjection,
  );
  expect(materialized.activeOwnedProjection).toEqual(
    materialized.rootOwnedProjection,
  );
  const compiledPrompt = await page.evaluate(async () => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    return (await app.graphToPrompt()).output as unknown;
  });
  const compiledPromptDiff = m2508ShapeDiff(
    m2508OwnedPromptProjection(
      bootstrap.visiblePrompt,
      bootstrap.visiblePrompt,
    ),
    m2508OwnedPromptProjection(compiledPrompt, bootstrap.visiblePrompt),
  );
  console.log(
    `M25_08_COMPILED_PROMPT_DIFF=${JSON.stringify(compiledPromptDiff)}`,
  );
  expect(compiledPromptDiff).toEqual([]);
  const surroundingPaths = m2508ShapeDiff(
    bootstrap.visiblePrompt,
    compiledPrompt,
  ).map((row) => row.path);
  const surroundingBuckets = { metadata: 0, foreignInputs: 0, structure: 0 };
  for (const path of surroundingPaths) {
    const bucket = path.includes("._meta")
      ? "metadata"
      : path.includes(".inputs.")
        ? "foreignInputs"
        : "structure";
    surroundingBuckets[bucket]++;
  }
  console.log(
    `M25_08_PROMPT_SURROUNDINGS=${JSON.stringify({ boundedTo: 64, buckets: surroundingBuckets, paths: surroundingPaths })}`,
  );
  expect(routeCounts).toEqual({
    queue: 0,
    provider: 0,
    generation: 0,
    coordinator: 0,
  });

  console.log(
    `M25_08_WORKFLOW_PREREQUISITES=${JSON.stringify({
      firstStart: initial,
      consecutiveStarts: {
        firstOpenCount: afterFirst.openCount,
        secondOpenCount: afterSecond.openCount,
        stablePath: afterSecond.activePath === afterFirst.activePath,
      },
      foreignPacksPresent: {
        extensionCount: afterSecond.extensionNames.length,
        foreignExtensionCount,
        foreignSidebarCount,
        sidebarIds: afterSecond.sidebarIds,
      },
      catalogueVariants: HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS,
      materialized,
      routeCounts,
    })}`,
  );
});
