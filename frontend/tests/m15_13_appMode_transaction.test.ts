import { describe, expect, it, vi } from "vitest";

import {
  createAppModeController,
  type AppModeCompiledPrompt,
} from "../src/host/appMode";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import { loadSyntheticTemplate } from "./support/templateFixture";
import { loadAvailableProfile } from "./support/generationProfileFixture";
import { fixtureBackedAppModeApi } from "./support/hostSeamTestDouble";
import {
  acceptedQueueResponse,
  syntheticPromptUuid,
} from "./support/queuePromptTestDouble";
import {
  spliceContextPipeline,
  type Json,
} from "../src/host/templateMaterialization";

const loadTemplate = loadSyntheticTemplate;
const loadProfile = loadAvailableProfile;

const inputs = {
  task_mode: "t2va" as const,
  user_intent: "A bounded safe intent.",
  duration_milliseconds: 5167,
  frame_count: 124,
};

const compiled: AppModeCompiledPrompt = {
  output: createQualifiedBasePrompt(
    inputs,
    {},
    {
      sharedDurationSource: true,
    },
  ),
  workflow: {},
};

function existingAuthoringGraph(): Json {
  const materialized = spliceContextPipeline(
    loadSyntheticTemplate("video_minimax_h3_t2v") as Json,
    {
      taskMode: inputs.task_mode,
      userIntent: inputs.user_intent,
      durationSeconds: inputs.duration_milliseconds / 1000,
    },
  );
  const nodes = materialized.workflow.nodes as Json[];
  const math = nodes.find((node) => node.type === "ComfyMathExpression")!;
  const nativeLength = (materialized.workflow.links as unknown[][]).find(
    (link) =>
      String(link[1]) === String(math.id) &&
      String(link[3]) === String(materialized.anchorNodeId),
  )!;
  nativeLength[2] = 1;
  math.outputs = [
    { name: "FLOAT", type: "FLOAT", links: [] },
    { name: "INT", type: "INT", links: [nativeLength[0]] },
  ];
  return materialized.workflow;
}

function createMutableApp() {
  const original = { nodes: [{ id: "foreign", type: "Foreign.Node" }] };
  let graph: unknown = original;
  const liveNodes = new Map<string, Record<string, unknown>>();
  const rebuildLiveNodes = (value: unknown) => {
    liveNodes.clear();
    const nodes =
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      Array.isArray((value as { nodes?: unknown }).nodes)
        ? ((value as { nodes: Json[] }).nodes ?? [])
        : [];
    for (const node of nodes) {
      const name =
        node.type === "comfyui_h3_context.H3Context.Request"
          ? "user_intent"
          : node.type === "PrimitiveFloat"
            ? "value"
            : undefined;
      const index = name === "user_intent" ? 1 : 0;
      if (
        name === undefined ||
        !Array.isArray(node.widgets_values) ||
        node.id === undefined
      )
        continue;
      const widgetValues = node.widgets_values as Json[];
      const widget = {
        name,
        value: widgetValues[index],
        callback: vi.fn((next: unknown) => {
          widgetValues[index] = next as Json;
        }),
      };
      liveNodes.set(String(node.id), {
        id: node.id,
        type: node.type,
        widgets: [widget],
        onWidgetChanged: vi.fn(),
      });
    }
  };
  const graphSeam = {
    constructor: function FixtureDetachedGraph(serialized?: unknown): object {
      return (serialized ?? {}) as object;
    },
    serialize: () => graph,
    getNodeById: (id: string | number) => liveNodes.get(String(id)),
    change: vi.fn(),
    setDirtyCanvas: vi.fn(),
  };
  const activeWorkflow = { path: "workflows/transaction-active.json" };
  const workflowStore = {
    activeWorkflow,
    openWorkflows: [activeWorkflow] as object[],
  };
  return {
    original,
    extensionManager: { workflow: workflowStore },
    get graph() {
      return graphSeam;
    },
    loadApiJson: vi.fn(() => {
      graph = {
        nodes: [{ id: 1, type: "comfyui_h3_context.H3Context.Request" }],
      };
    }),
    loadGraphData: vi.fn((value: unknown) => {
      graph = value;
      rebuildLiveNodes(value);
    }),
    setGraph(value: unknown) {
      graph = value;
      rebuildLiveNodes(value);
    },
  };
}

describe("M15-13 App Mode transaction safety", () => {
  it("leaves the original graph untouched when detached compilation fails", async () => {
    const app = createMutableApp();
    const api = fixtureBackedAppModeApi({ queuePrompt: vi.fn() });
    const graphToPrompt = vi
      .fn()
      .mockRejectedValue(new Error("private compile detail"));
    const bound = createAppModeController({ ...app, graphToPrompt }, api, {
      loadTemplate,
      loadProfile,
    });

    await expect(
      bound.start(inputs, { replaceExisting: true }),
    ).rejects.toMatchObject({
      code: "compile_failed",
    });
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(app.graph.serialize()).toEqual(app.original);
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("does not turn whole-graph restore equality into a workflow authority criterion", async () => {
    const app = createMutableApp();
    const api = fixtureBackedAppModeApi({ queuePrompt: vi.fn() });
    // The seam writes the candidate but silently ignores the restore triggered
    // by a post-write identity-bind refusal. D12 does not promote whole-graph
    // equality into an authority gate; exact workflow and owned projection do.
    let applied = 0;
    const bound = createAppModeController(
      {
        ...app,
        loadGraphData: vi.fn((value: unknown) => {
          if (applied++ === 0) app.setGraph(value);
        }),
        graphToPrompt: vi.fn().mockResolvedValue(compiled),
      },
      api,
      { loadTemplate, loadProfile },
    );

    await expect(
      bound.start(inputs, {
        replaceExisting: true,
        prepareManaged: async (prepared) => ({
          expectedIdentity: {
            graphFingerprint: prepared.observation.graph_fingerprint,
            compiledPromptFingerprint:
              prepared.observation.compiled_prompt_fingerprint,
          },
          bindCanvasIdentity: () => {
            throw new Error("post-write identity bind refusal");
          },
          onQueueSubmitted: () => undefined,
          onQueueAccepted: () => undefined,
          onQueueFailed: () => undefined,
        }),
      }),
    ).rejects.toMatchObject({ code: "compile_failed" });
    expect(applied).toBe(2);
    expect(app.graph.serialize()).not.toEqual(app.original);
    expect(app.extensionManager.workflow.activeWorkflow).toBe(
      app.extensionManager.workflow.openWorkflows[0],
    );
    expect(app.extensionManager.workflow.openWorkflows).toHaveLength(1);
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("does not overwrite foreign graph drift exposed during detached compilation", async () => {
    const app = createMutableApp();
    const existing = existingAuthoringGraph();
    app.setGraph(existing);
    const foreign = { nodes: [{ id: "mutated", type: "Foreign.Node" }] };
    const api = fixtureBackedAppModeApi({ queuePrompt: vi.fn() });
    const graphToPrompt = vi.fn(() => {
      app.setGraph(foreign);
      return Promise.resolve(compiled);
    });
    const bound = createAppModeController({ ...app, graphToPrompt }, api, {
      loadTemplate,
      loadProfile,
    });

    await expect(
      bound.start(inputs, { useExisting: true }),
    ).rejects.toMatchObject({ code: "incompatible_graph" });
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(app.graph.serialize()).toEqual(foreign);
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("keeps the submitted graph when an unclassified queue operation rejects", async () => {
    const app = createMutableApp();
    const api = fixtureBackedAppModeApi({
      queuePrompt: vi.fn().mockRejectedValue(new Error("queue detail")),
    });
    const bound = createAppModeController(
      {
        ...app,
        graphToPrompt: vi.fn().mockResolvedValue(compiled),
      },
      api,
      { loadTemplate, loadProfile },
    );

    await expect(
      bound.start(inputs, { replaceExisting: true }),
    ).rejects.toMatchObject({
      code: "ambiguous_host_ownership",
      recovery: "use_native",
    });
    // Once, to materialize. A queue failure after submission must not add a
    // second call putting the user's previous canvas back underneath it.
    expect(app.loadGraphData).toHaveBeenCalledOnce();
    expect(app.graph.serialize()).not.toEqual(app.original);
  });

  it("keeps the submitted graph and queue identity if local abort happens after submission", async () => {
    const app = createMutableApp();
    let resolveQueue: ((value: unknown) => void) | undefined;
    const api = fixtureBackedAppModeApi({
      queuePrompt: vi.fn(
        () =>
          new Promise((resolve) => {
            resolveQueue = resolve;
          }),
      ),
    });
    const bound = createAppModeController(
      { ...app, graphToPrompt: vi.fn().mockResolvedValue(compiled) },
      api,
      { loadTemplate, loadProfile },
    );
    const abort = new AbortController();
    const pending = bound.start(inputs, {
      replaceExisting: true,
      signal: abort.signal,
    });
    await vi.waitFor(() => expect(api.queuePrompt).toHaveBeenCalledOnce());
    abort.abort();
    resolveQueue?.(acceptedQueueResponse(1));

    await expect(pending).resolves.toMatchObject({
      queuePromptId: syntheticPromptUuid(1),
      route: "replace",
    });
    expect(app.loadGraphData).toHaveBeenCalledOnce();
    expect(app.graph.serialize()).not.toEqual(app.original);
  });

  it("rejects a graph changed during compilation without queueing stale work", async () => {
    const app = createMutableApp();
    const api = fixtureBackedAppModeApi({ queuePrompt: vi.fn() });
    const graphToPrompt = vi.fn(() => {
      app.setGraph({ nodes: [{ id: "late", type: "Foreign.Node" }] });
      return Promise.resolve(compiled);
    });
    const bound = createAppModeController({ ...app, graphToPrompt }, api, {
      loadTemplate,
      loadProfile,
    });

    await expect(
      bound.start(inputs, { replaceExisting: true }),
    ).rejects.toMatchObject({
      code: "stale_graph",
    });
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });
});
