import { describe, expect, it, vi } from "vitest";

import {
  assertM2508AuthorizedMediaRuntimePrecondition,
  assertM2508ExactHostCapability,
  buildM2508AuthorizedMediaRuntimeProbe,
  buildM2508HostBootstrap,
  ensureM2508CapturedWorkflowAuthority,
  installM2508ManagedExecutionProjection,
  isM2508HostGraphLifecycleReady,
  M2508_OWNED_NODE_IDS,
  materializeM2508VisiblePromptInCapturedWorkflow,
} from "./e2e/host/m25_08Bootstrap";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";

const LOCATOR = "m25_08_acceptance/a68c69bf/m25_08_authorized_video_v1.mp4";

class SyntheticDetachedGraph {
  nodes: Array<{ id?: string | number; type: string }> = [];
  add(node: { id?: string | number; type: string }) {
    this.nodes.push(node);
  }
  arrange() {}
  getNodeById(id: string | number) {
    return this.nodes.find((node) => node.id === id) ?? null;
  }
  serialize() {
    return { nodes: this.nodes.map((node) => ({ ...node })) };
  }
}

function installSyntheticDetachedGraphRuntime() {
  const previous = (globalThis as { LiteGraph?: unknown }).LiteGraph;
  Object.assign(globalThis, {
    LiteGraph: {
      createNode: (type: string) => ({
        type,
        inputs: [],
        widgets: [],
        connect: vi.fn(),
      }),
    },
  });
  return {
    rootGraph: Object.assign(
      { serialize: () => ({ nodes: [] as unknown[] }) },
      { constructor: SyntheticDetachedGraph },
    ),
    restore: () => {
      if (previous === undefined)
        Reflect.deleteProperty(globalThis, "LiteGraph");
      else Object.assign(globalThis, { LiteGraph: previous });
    },
  };
}

describe("M25-08 supplied-host model-free bootstrap", () => {
  it("requires both graph and canvas before supplied-host graph writes", () => {
    Object.assign(window, { comfyAPI: { app: { app: {} } } });
    expect(isM2508HostGraphLifecycleReady()).toBe(false);

    Object.assign(window, {
      comfyAPI: { app: { app: { graph: { serialize: () => ({}) } } } },
    });
    expect(isM2508HostGraphLifecycleReady()).toBe(false);

    Object.assign(window, {
      comfyAPI: { app: { app: { graph: {}, canvas: {} } } },
    });
    expect(isM2508HostGraphLifecycleReady()).toBe(false);

    Object.assign(window, {
      comfyAPI: {
        app: { app: { graph: { serialize: () => ({}) }, canvas: {} } },
      },
    });
    expect(isM2508HostGraphLifecycleReady()).toBe(true);
  });

  it("keeps both browser-evaluated workflow callbacks self-contained", () => {
    expect(isM2508HostGraphLifecycleReady.toString()).not.toContain(
      "m2508HostApp",
    );
    expect(ensureM2508CapturedWorkflowAuthority.toString()).not.toContain(
      "m2508HostApp",
    );
    const materializeSource =
      materializeM2508VisiblePromptInCapturedWorkflow.toString();
    expect(materializeSource).not.toContain("m2508HostApp");
    expect(materializeSource).not.toContain(
      "ensureM2508CapturedWorkflowAuthority",
    );
  });

  it("refuses to capture null/empty before the single materialization lifecycle", async () => {
    const rawApp = {
      extensionManager: {
        workflow: { activeWorkflow: {}, openWorkflows: [] as object[] },
      },
      graph: { serialize: () => ({ nodes: [], links: [] }) },
    };
    const host = fixtureBackedAppModeHost(
      rawApp,
      {},
      {
        workflowVariant: "null_empty_exact",
      },
    );
    const app = host.app;
    const workflow = app.extensionManager.workflow;
    Object.assign(window, { comfyAPI: { app: { app } } });

    await expect(ensureM2508CapturedWorkflowAuthority()).rejects.toThrow(
      "M25-08 workflow authority is not materialized",
    );
    expect(workflow.activeWorkflow).toBeNull();
    expect(workflow.openWorkflows).toEqual([]);
    expect(host.workflowBehaviour?.trace).toEqual([]);
  });

  it("preserves an existing active authority without creating a workflow", async () => {
    const rawApp = {
      extensionManager: {
        workflow: { activeWorkflow: {}, openWorkflows: [] as object[] },
      },
    };
    const host = fixtureBackedAppModeHost(
      rawApp,
      {},
      {
        workflowVariant: "existing_active",
      },
    );
    const app = host.app;
    Object.assign(window, { comfyAPI: { app: { app } } });

    await expect(ensureM2508CapturedWorkflowAuthority()).resolves.toBe(
      host.workflowBehaviour?.authority,
    );
    expect(host.workflowBehaviour?.trace).toEqual([]);
  });

  it("refuses null authority when any foreign workflow is already open", async () => {
    const rawApp = {
      extensionManager: {
        workflow: { activeWorkflow: {}, openWorkflows: [] as object[] },
      },
    };
    const host = fixtureBackedAppModeHost(
      rawApp,
      {},
      {
        workflowVariant: "foreign_open_inconsistent",
      },
    );
    const app = host.app;
    Object.assign(window, { comfyAPI: { app: { app } } });

    await expect(ensureM2508CapturedWorkflowAuthority()).rejects.toThrow(
      "M25-08 workflow authority state is inconsistent",
    );
    expect(host.workflowBehaviour?.trace).toEqual([]);
  });

  it("requires exact active ownership for both stable node IDs", () => {
    for (const nodeId of M2508_OWNED_NODE_IDS) {
      expect(() =>
        assertM2508ExactHostCapability(nodeId, {
          [nodeId]: {
            python_module: "custom_nodes.ComfyUI-MiniMaxH3-Context",
          },
        }),
      ).not.toThrow();
    }
  });

  it.each([
    "custom_nodes.ComfyUI-MiniMaxH3-Context.backup-m25-08",
    "custom_nodes.foreign-pack",
    "",
  ])("rejects a non-exact active module before queue: %s", (pythonModule) => {
    const nodeId = M2508_OWNED_NODE_IDS[0];
    expect(() =>
      assertM2508ExactHostCapability(nodeId, {
        [nodeId]: { python_module: pythonModule },
      }),
    ).toThrow("M25-08 active node module is not exact");
  });

  it("rejects a missing owned capability before queue", () => {
    expect(() =>
      assertM2508ExactHostCapability(M2508_OWNED_NODE_IDS[1], {}),
    ).toThrow("M25-08 active node module is not exact");
  });

  it("proves the authorized media runtime before the one-shot host queue", () => {
    expect(buildM2508AuthorizedMediaRuntimeProbe()).toEqual({
      schema: "h3.context.authoring_source_preview.request.v1",
      requestId: "m25-08-authorized-media-runtime",
      workspaceHandle: "m25-08-runtime-preflight-never-issued",
      referenceRevision: 1,
      timelineRevision: 1,
      timelineContentFingerprint: `sha256:${"0".repeat(64)}`,
      clipId: "m25-08-runtime-preflight",
    });
    expect(() =>
      assertM2508AuthorizedMediaRuntimePrecondition(404, {
        schema: "h3.context.authoring_source_preview.error.v1",
        requestId: "m25-08-authorized-media-runtime",
        reason: "authority_mismatch",
      }),
    ).not.toThrow();
  });

  it.each([
    [
      422,
      {
        schema: "h3.context.authoring_source_preview.error.v1",
        requestId: "m25-08-authorized-media-runtime",
        reason: "unsupported",
      },
      "M25-08 authorized media runtime is unavailable",
    ],
    [
      200,
      {
        schema: "h3.context.authoring_source_preview.error.v1",
        requestId: "m25-08-authorized-media-runtime",
        reason: "authority_mismatch",
      },
      "M25-08 authorized media runtime precondition is inconclusive",
    ],
    [
      404,
      {
        schema: "h3.context.authoring_source_preview.error.v1",
        requestId: "m25-08-authorized-media-runtime",
        reason: "authority_mismatch",
        extra: true,
      },
      "M25-08 authorized media runtime precondition is inconclusive",
    ],
  ] as const)(
    "fails closed on an unqualified media-runtime receipt %#",
    (status, wire, message) => {
      expect(() =>
        assertM2508AuthorizedMediaRuntimePrecondition(status, wire),
      ).toThrow(message);
    },
  );

  it("retains the authorized VIDEO graph but excludes native generation from execution", () => {
    const bootstrap = buildM2508HostBootstrap(LOCATOR);

    expect(bootstrap.visiblePrompt["2"]).toEqual({
      class_type: "LoadVideo",
      inputs: { file: LOCATOR },
    });
    expect(bootstrap.visiblePrompt["3"]?.inputs.videos).toEqual(["2", 0]);
    expect(bootstrap.visiblePrompt["1"]?.inputs.duration_seconds).toEqual([
      "10",
      0,
    ]);
    expect(bootstrap.visiblePrompt[bootstrap.productShellNodeId]).toMatchObject(
      {
        class_type: "comfyui_h3_context.H3Context.ProductShell",
      },
    );
    expect(bootstrap.visiblePrompt[bootstrap.nativeAnchorNodeId]).toMatchObject(
      {
        class_type: "MiniMaxH3ReferenceToVideo",
        inputs: {
          prompt: [bootstrap.productShellNodeId, 0],
          length: ["11", 1],
          ref_videos: [["2", 0]],
        },
      },
    );

    expect(
      bootstrap.executionPrompt[bootstrap.nativeAnchorNodeId],
    ).toBeUndefined();
    expect(
      Object.values(bootstrap.executionPrompt).filter((node) =>
        node.class_type.startsWith("MiniMaxH3"),
      ),
    ).toEqual([]);
    expect(
      Object.values(bootstrap.executionPrompt).map((node) => node.class_type),
    ).toEqual([
      "comfyui_h3_context.H3Context.Request",
      "LoadVideo",
      "comfyui_h3_context.H3Context.ReferenceRegistry",
      "comfyui_h3_context.H3Context.Plan",
      "comfyui_h3_context.H3Context.Compiler",
      "comfyui_h3_context.H3Context.Validator",
      "comfyui_h3_context.H3Context.NativeH3Adapter",
      "comfyui_h3_context.H3Context.ProductShell",
      "PrimitiveFloat",
      "ComfyMathExpression",
    ]);
  });

  it("installs a deterministic non-mutating model-free execution projection", () => {
    const bootstrap = buildM2508HostBootstrap(LOCATOR);
    const queuePrompt = vi.fn();
    Object.assign(window, {
      comfyAPI: { api: { api: { queuePrompt } } },
    });

    installM2508ManagedExecutionProjection(bootstrap.executionPrompt);
    const runtime = window as unknown as {
      comfyAPI: {
        api: {
          api: {
            queuePrompt(batch: unknown, compiled: unknown): Promise<unknown>;
          };
        };
      };
      __h3M2508ManagedExecutionProjection?: (
        compiled: Record<string, unknown>,
      ) => Record<string, unknown>;
      __h3M2508ExecutionIdentityTrace?: unknown[];
      __h3ProjectionTrace?: unknown[];
      __h3HostProjectionTrace?: unknown[];
    };
    const compiled = {
      output: {
        ...bootstrap.executionPrompt,
        [bootstrap.nativeAnchorNodeId]:
          bootstrap.visiblePrompt[bootstrap.nativeAnchorNodeId],
      },
      workflow: { nodes: [] },
    };
    const before = structuredClone(compiled);
    const project = runtime.__h3M2508ManagedExecutionProjection;
    expect(project).toBeTypeOf("function");

    const first = project!(compiled);
    const second = project!(compiled);

    expect(first).toEqual(second);
    expect(first).toMatchObject({
      output: bootstrap.executionPrompt,
    });
    expect(
      (first as { output: Record<string, unknown> }).output[
        bootstrap.nativeAnchorNodeId
      ],
    ).toBeUndefined();
    expect(compiled).toEqual(before);
    expect(first).not.toBe(compiled);
    expect((first as { output: unknown }).output).not.toBe(compiled.output);
    expect(queuePrompt).not.toHaveBeenCalled();
    expect(runtime.__h3M2508ExecutionIdentityTrace).toEqual([]);
    expect(runtime.__h3ProjectionTrace).toEqual([]);
    expect(runtime.__h3HostProjectionTrace).toEqual([]);
  });

  it("accepts only ComfyUI's closed title metadata before exact projection", () => {
    const bootstrap = buildM2508HostBootstrap(LOCATOR);
    installM2508ManagedExecutionProjection(bootstrap.executionPrompt);
    const project = (
      window as unknown as {
        __h3M2508ManagedExecutionProjection: (
          compiled: Record<string, unknown>,
        ) => Record<string, unknown>;
      }
    ).__h3M2508ManagedExecutionProjection;
    const output = Object.fromEntries(
      Object.entries(bootstrap.executionPrompt).map(([id, node]) => [
        id,
        { ...structuredClone(node), _meta: { title: `Node ${id}` } },
      ]),
    );
    output[bootstrap.nativeAnchorNodeId] = {
      ...structuredClone(
        bootstrap.visiblePrompt[bootstrap.nativeAnchorNodeId]!,
      ),
      _meta: { title: "Native anchor" },
    };

    expect(project({ output, workflow: {} })).toMatchObject({
      output: bootstrap.executionPrompt,
    });
  });

  it.each([{ title: "Request", extra: true }, { title: 1 }])(
    "rejects non-closed ComfyUI metadata: %j",
    (_meta) => {
      const bootstrap = buildM2508HostBootstrap(LOCATOR);
      installM2508ManagedExecutionProjection(bootstrap.executionPrompt);
      const project = (
        window as unknown as {
          __h3M2508ManagedExecutionProjection: (
            compiled: Record<string, unknown>,
          ) => Record<string, unknown>;
        }
      ).__h3M2508ManagedExecutionProjection;
      const output = structuredClone(bootstrap.executionPrompt) as Record<
        string,
        Record<string, unknown>
      >;
      output["1"]!._meta = _meta;
      output[bootstrap.nativeAnchorNodeId] =
        bootstrap.visiblePrompt[bootstrap.nativeAnchorNodeId]!;

      expect(() => project({ output, workflow: {} })).toThrow(
        "M25-08 model-free execution projection is not exact",
      );
    },
  );

  it.each([
    ["wrong native id", "12", "MiniMaxH3ReferenceToVideo"],
    ["wrong native type", "9", "MiniMaxH3ImageToVideo"],
  ])(
    "rejects a %s before managed transport",
    (_label, nativeId, nativeType) => {
      const bootstrap = buildM2508HostBootstrap(LOCATOR);
      installM2508ManagedExecutionProjection(bootstrap.executionPrompt);
      const project = (
        window as unknown as {
          __h3M2508ManagedExecutionProjection: (
            compiled: Record<string, unknown>,
          ) => Record<string, unknown>;
        }
      ).__h3M2508ManagedExecutionProjection;
      expect(() =>
        project({
          output: {
            ...bootstrap.executionPrompt,
            [nativeId]: { class_type: nativeType, inputs: {} },
          },
          workflow: {},
        }),
      ).toThrow("M25-08 native execution boundary is not exact");
    },
  );

  it.each(["missing", "extra", "modified"] as const)(
    "rejects a %s allowlisted execution node before managed transport",
    (mode) => {
      const bootstrap = buildM2508HostBootstrap(LOCATOR);
      installM2508ManagedExecutionProjection(bootstrap.executionPrompt);
      const project = (
        window as unknown as {
          __h3M2508ManagedExecutionProjection: (
            compiled: Record<string, unknown>,
          ) => Record<string, unknown>;
        }
      ).__h3M2508ManagedExecutionProjection;
      const output = structuredClone(bootstrap.executionPrompt) as Record<
        string,
        { class_type: string; inputs: Record<string, unknown> }
      >;
      if (mode === "missing") delete output["1"];
      if (mode === "extra")
        output["12"] = { class_type: "PrimitiveFloat", inputs: {} };
      if (mode === "modified") output["1"]!.class_type = "ForeignRequest";
      output[bootstrap.nativeAnchorNodeId] =
        bootstrap.visiblePrompt[bootstrap.nativeAnchorNodeId]!;

      expect(() => project({ output, workflow: {} })).toThrow(
        "M25-08 model-free execution projection is not exact",
      );
    },
  );

  it.each([
    [
      "authorized VIDEO locator",
      "2",
      "file",
      "m25_08_acceptance/deadbeef/m25_08_authorized_video_v1.mp4",
    ],
    ["nested prompt link", "1", "duration_seconds", ["11", 1]],
  ] as const)(
    "rejects a same-class %s mutation before managed transport",
    (_label, nodeId, inputName, inputValue) => {
      const bootstrap = buildM2508HostBootstrap(LOCATOR);
      installM2508ManagedExecutionProjection(bootstrap.executionPrompt);
      const project = (
        window as unknown as {
          __h3M2508ManagedExecutionProjection: (
            compiled: Record<string, unknown>,
          ) => Record<string, unknown>;
        }
      ).__h3M2508ManagedExecutionProjection;
      const output = structuredClone(bootstrap.executionPrompt) as Record<
        string,
        { class_type: string; inputs: Record<string, unknown> }
      >;
      output[nodeId]!.inputs[inputName] = inputValue;
      output[bootstrap.nativeAnchorNodeId] =
        bootstrap.visiblePrompt[bootstrap.nativeAnchorNodeId]!;

      expect(() => project({ output, workflow: {} })).toThrow(
        "M25-08 model-free execution projection is not exact",
      );
    },
  );

  it.each([
    "A:/comfyui/input/m25_08_authorized_video_v1.mp4",
    "../m25_08_authorized_video_v1.mp4",
    "https://example.invalid/m25_08_authorized_video_v1.mp4",
    "m25_08_acceptance/a68c69bf/not_authorized.mp4",
  ])(
    "rejects a locator outside the single authorized fixture binding: %s",
    (value) => {
      expect(() => buildM2508HostBootstrap(value)).toThrow(
        "M25-08 VIDEO locator is not authorized",
      );
    },
  );

  it("materializes an existing authority through one detached graph transaction", async () => {
    const authority = { path: "workflows/existing.json" };
    const openWorkflows = [authority];
    const calls: string[] = [];
    const graphWire = { nodes: [] as Array<Record<string, unknown>> };
    const prompt = {
      "1": { class_type: "SyntheticFixtureNode", inputs: {} },
    };
    const detachedRuntime = installSyntheticDetachedGraphRuntime();
    const loadApiJson = vi.fn(async () => {
      throw new Error("existing authority must not use loadApiJson");
    });
    const rawApp = {
      extensionManager: {
        workflow: { activeWorkflow: authority, openWorkflows },
      },
      graph: { serialize: () => graphWire },
      rootGraph: detachedRuntime.rootGraph,
      canvas: {},
      loadApiJson,
      loadGraphData: async (...args: unknown[]) => {
        calls.push("graph");
        expect(args.slice(1)).toEqual([true, true, authority]);
        const serialized = args[0] as { nodes: Array<Record<string, unknown>> };
        graphWire.nodes = serialized.nodes;
      },
    };
    const app = fixtureBackedAppModeHost(rawApp, {}).app;
    Object.assign(window, { comfyAPI: { app: { app } } });

    try {
      await materializeM2508VisiblePromptInCapturedWorkflow(prompt);
    } finally {
      detachedRuntime.restore();
    }

    expect(calls).toEqual(["graph"]);
    expect(loadApiJson).not.toHaveBeenCalled();
  });

  it("lets a fresh host publish the materialized workflow before active authority", async () => {
    const authority = {
      path: "workflows/m25-08-authorized-video-acceptance.json",
    };
    const graphWire = { nodes: [] as Array<Record<string, unknown>> };
    const prompt = {
      "8": { class_type: "SyntheticFixtureNode", inputs: {} },
    };
    const loadGraphData = vi.fn(async () => undefined);
    const rawApp = {
      extensionManager: {
        workflow: {
          activeWorkflow: null as object | null,
          openWorkflows: [] as object[],
        },
      },
      graph: { serialize: () => graphWire },
      loadApiJson: vi.fn(async () => undefined),
      loadGraphData,
    };
    const host = fixtureBackedAppModeHost(
      rawApp,
      {},
      {
        workflowVariant: "null_empty_exact",
      },
    );
    const app = host.app;
    const workflow = app.extensionManager.workflow;
    app.loadApiJson = vi.fn(async (...args: unknown[]) => {
      expect(args).toEqual([prompt, "m25-08-authorized-video-acceptance.json"]);
      graphWire.nodes = [{ id: 8, type: "SyntheticFixtureNode" }];
      workflow.openWorkflows.push(authority);
      setTimeout(() => {
        workflow.activeWorkflow = authority;
      }, 0);
    });
    Object.assign(window, { comfyAPI: { app: { app } } });

    await materializeM2508VisiblePromptInCapturedWorkflow(prompt);

    expect(workflow.activeWorkflow).toBe(authority);
    expect(workflow.openWorkflows).toEqual([authority]);
    expect(app.loadApiJson).toHaveBeenCalledTimes(1);
    expect(loadGraphData).not.toHaveBeenCalled();
  });

  it("fails closed before graph materialization when active identity drifts", async () => {
    const authority = {};
    const workflow = { activeWorkflow: authority, openWorkflows: [authority] };
    let graphCalls = 0;
    const detachedRuntime = installSyntheticDetachedGraphRuntime();
    const rawApp = {
      extensionManager: { workflow },
      graph: { serialize: () => ({}) },
      rootGraph: detachedRuntime.rootGraph,
      loadApiJson: async () => undefined,
      loadGraphData: async () => {
        graphCalls += 1;
        workflow.activeWorkflow = {};
      },
    };
    const app = fixtureBackedAppModeHost(rawApp, {}).app;
    Object.assign(window, { comfyAPI: { app: { app } } });

    try {
      await expect(
        materializeM2508VisiblePromptInCapturedWorkflow({
          "1": { class_type: "SyntheticFixtureNode", inputs: {} },
        }),
      ).rejects.toThrow("M25-08 workflow authority changed");
    } finally {
      detachedRuntime.restore();
    }
    expect(graphCalls).toBe(1);
  });

  it("fails closed when the open-tab count drifts during graph materialization", async () => {
    const authority = {};
    const workflow = { activeWorkflow: authority, openWorkflows: [authority] };
    const detachedRuntime = installSyntheticDetachedGraphRuntime();
    const rawApp = {
      extensionManager: { workflow },
      graph: { serialize: () => ({}) },
      rootGraph: detachedRuntime.rootGraph,
      loadApiJson: async () => undefined,
      loadGraphData: async () => {
        workflow.openWorkflows.push({});
      },
    };
    const app = fixtureBackedAppModeHost(rawApp, {}).app;
    Object.assign(window, { comfyAPI: { app: { app } } });
    try {
      await expect(
        materializeM2508VisiblePromptInCapturedWorkflow({
          "1": { class_type: "SyntheticFixtureNode", inputs: {} },
        }),
      ).rejects.toThrow("M25-08 workflow authority changed");
    } finally {
      detachedRuntime.restore();
    }
  });
});
