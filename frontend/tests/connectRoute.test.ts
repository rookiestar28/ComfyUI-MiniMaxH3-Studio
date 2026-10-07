import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createAppModeController,
  type AppModeInputs,
} from "../src/host/appMode";
import {
  admitCanvasForConnect,
  listNativeAnchors,
  spliceContextPipeline,
  type Json,
} from "../src/host/templateMaterialization";
import {
  H3_SHELL_MANIFEST,
  inspectVisibleH3Graph,
} from "../src/host/graphAdapter";
import { communityCanvas, foreignNodes } from "./support/connectFixture";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import { loadSyntheticTemplate } from "./support/templateFixture";
import {
  ASSET_RELOCATED,
  generationProfile,
  loadAvailableProfile,
  MISSING_ASSET,
  TEMPLATE_DRIFT,
  UNSUPPORTED_HOST,
} from "./support/generationProfileFixture";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";
import { existingManagedHostGraphFixture } from "./support/managedHostGraphFixture";
import {
  acceptedQueueResponse,
  syntheticPromptUuid,
  wrapQueuePrompt,
  type QueuePromptDelegate,
} from "./support/queuePromptTestDouble";

/**
 * M17-20 phase 5, D13 tiers A/B/C.
 *
 * The connect route joins a Context to a graph this repository did not build.
 * That makes the ownership boundary the whole subject: the repository owns the
 * splice seam and anchor-level alignment, and nothing else on the canvas. These
 * rows assert both halves — that the seam works, and that everything outside it
 * is left exactly as the user assembled it.
 */

const loadTemplate = vi.fn(loadSyntheticTemplate);
const loadProfile = vi.fn(loadAvailableProfile);

beforeEach(() => {
  loadTemplate.mockClear();
  loadProfile.mockClear();
});

const t2va: AppModeInputs = {
  task_mode: "t2va",
  user_intent: "A red kite crosses the sky while the camera follows its arc.",
  duration_milliseconds: 5167,
  frame_count: 124,
};

function alreadyOwnedCanvas(location: "root" | "definition"): Json {
  const canvas = communityCanvas();
  const owned = {
    id: 900,
    type: "comfyui_h3_context.H3Context.Request",
  };
  if (location === "root") (canvas.nodes as Json[]).push(owned);
  else
    canvas.definitions = {
      subgraphs: [
        {
          id: "owned-subgraph",
          nodes: [owned],
        },
      ],
    };
  return canvas;
}

type UnsafeQueueMetadataCase = Readonly<{
  label: string;
  build: () => Readonly<{
    canvas: Json;
    accessorRead: () => boolean;
    cloneable: boolean;
  }>;
}>;

function unsafeQueueMetadataCase(
  label: string,
  mutate: (canvas: Json, markAccessorRead: () => void) => void,
  cloneable = true,
): UnsafeQueueMetadataCase {
  return {
    label,
    build: () => {
      const canvas = communityCanvas();
      let accessorRead = false;
      mutate(canvas, () => {
        accessorRead = true;
      });
      return {
        canvas,
        accessorRead: () => accessorRead,
        cloneable,
      };
    },
  };
}

const unsafeQueueMetadataCases: readonly UnsafeQueueMetadataCase[] = [
  unsafeQueueMetadataCase("an undefined installed member", (canvas) => {
    canvas.widget_idx_map = undefined;
  }),
  unsafeQueueMetadataCase("an unrelated root member", (canvas) => {
    canvas.private_queue_metadata = { "20": 0 };
  }),
  unsafeQueueMetadataCase("a prototype-bearing map", (canvas) => {
    canvas.seed_widgets = Object.assign(Object.create({ inherited: 0 }), {
      "20": 0,
    });
  }),
  unsafeQueueMetadataCase(
    "an accessor inside an installed map",
    (canvas, markAccessorRead) => {
      const map: Json = {};
      Object.defineProperty(map, "20", {
        enumerable: true,
        get() {
          markAccessorRead();
          return 0;
        },
      });
      canvas.seed_widgets = map;
    },
    false,
  ),
  unsafeQueueMetadataCase(
    "a hidden root accessor",
    (canvas, markAccessorRead) => {
      Object.defineProperty(canvas, "widget_idx_map", {
        enumerable: false,
        get() {
          markAccessorRead();
          return { "20": { seed: 0 } };
        },
      });
    },
    false,
  ),
  unsafeQueueMetadataCase("an unknown widget name", (canvas) => {
    canvas.widget_idx_map = { "20": { cfg: 0 } };
  }),
  unsafeQueueMetadataCase("an out-of-range widget index", (canvas) => {
    canvas.seed_widgets = { "20": 4 };
  }),
  unsafeQueueMetadataCase("a missing root node id", (canvas) => {
    canvas.seed_widgets = { missing: 0 };
  }),
  unsafeQueueMetadataCase("a subgraph-only node id", (canvas) => {
    canvas.definitions = {
      subgraphs: [
        {
          id: "foreign-subgraph",
          nodes: [
            { id: "nested-only", type: "Foreign.Node", widgets_values: [0] },
          ],
        },
      ],
    };
    canvas.seed_widgets = { "nested-only": 0 };
  }),
];

function hostOn(
  canvas: Json,
  queuePrompt: QueuePromptDelegate = vi
    .fn()
    .mockResolvedValue(acceptedQueueResponse(1)),
) {
  let graph: unknown = canvas;
  const activeWorkflow: object = { path: "workflows/connect-active.json" };
  const workflowStore = {
    activeWorkflow,
    openWorkflows: [activeWorkflow] as object[],
  };
  const { app, api } = fixtureBackedAppModeHost(
    {
      extensionManager: { workflow: workflowStore },
      loadApiJson: vi.fn(),
      loadGraphData: vi.fn(
        (
          value: unknown,
          _clean = true,
          _restoreView = true,
          workflow: object | null = null,
        ) => {
          graph = value;
          if (workflow === null) {
            const temporary = {
              path: "workflows/unexpected-connect-copy.json",
            };
            workflowStore.openWorkflows.push(temporary);
            workflowStore.activeWorkflow = temporary;
            return;
          }
          workflowStore.activeWorkflow = workflow;
        },
      ),
      graphToPrompt: vi.fn(),
      graph: { serialize: () => graph },
    },
    { queuePrompt },
  );
  return {
    activeWorkflow,
    app,
    api,
    graph: () => graph as Json,
    workflowStore,
  };
}

function opaqueForeignWidgetProjection(canvas: Json): unknown {
  const nodes = Array.isArray(canvas.nodes) ? (canvas.nodes as Json[]) : [];
  return nodes
    .filter((node) => node.id === 1 || node.id === 3)
    .map((node) =>
      structuredClone({
        id: node.id,
        type: node.type,
        widgets_values: node.widgets_values,
      }),
    );
}

function compiledFor(
  inputs: AppModeInputs,
  opaqueSources: Parameters<typeof createQualifiedBasePrompt>[1] = {},
) {
  return async () => ({
    output: createQualifiedBasePrompt(inputs, opaqueSources, {
      filenamePrefix: "their-project/take-1",
    }),
    workflow: {},
  });
}

function compiledConnectFrameFor(
  inputs: AppModeInputs,
  opaqueSources: Parameters<typeof createQualifiedBasePrompt>[1],
  mutate?: (output: Record<string, unknown>) => void,
) {
  return async () => {
    const output = createQualifiedBasePrompt(inputs, opaqueSources, {
      filenamePrefix: "their-project/take-1",
    });
    mutate?.(output);
    return { output, workflow: {} };
  };
}

describe("the canvas census decides which tier a graph is in", () => {
  it("reports one anchor as connectable", () => {
    const admission = admitCanvasForConnect(communityCanvas());
    expect(admission.tier).toBe("connect");
    expect(admission.anchors).toEqual([
      {
        nodeId: 20,
        anchorType: "MiniMaxH3ImageToVideo",
        nested: false,
        taskMode: "t2va",
      },
    ]);
  });

  it("requires a designation when several anchors are present", () => {
    const admission = admitCanvasForConnect(
      communityCanvas([{ id: 20 }, { id: 21 }]),
    );
    expect(admission.tier).toBe("designate");
    expect(admission.anchors).toHaveLength(2);
  });

  it("leaves a graph with no anchor to replace or keep", () => {
    // Tier C. This repository has nothing to say about such a canvas, and
    // offering to connect would be offering to invent a target.
    const canvas = communityCanvas();
    canvas.nodes = (canvas.nodes as Json[]).filter(
      (node) => !String(node.type).startsWith("MiniMaxH3"),
    );
    expect(admitCanvasForConnect(canvas)).toMatchObject({
      tier: "unavailable",
      anchors: [],
    });
  });

  it.each(["root", "definition"] as const)(
    "does not offer Connect when %s ownership already exists",
    (location) => {
      expect(admitCanvasForConnect(alreadyOwnedCanvas(location))).toMatchObject(
        {
          tier: "unavailable",
          anchors: [],
        },
      );
    },
  );

  it.each(unsafeQueueMetadataCases)(
    "does not offer Connect for $label",
    ({ build }) => {
      const { canvas, accessorRead } = build();
      expect(admitCanvasForConnect(canvas)).toMatchObject({
        tier: "unavailable",
        anchors: [],
      });
      expect(accessorRead()).toBe(false);
    },
  );

  it("keeps exact installed queue metadata compatible with Connect", () => {
    const canvas = communityCanvas();
    canvas.widget_idx_map = {
      "3": { seed: 0, noise_seed: 0, sampler_name: 1, scheduler: 1 },
    };
    canvas.seed_widgets = { "20": 0 };
    expect(admitCanvasForConnect(canvas).tier).toBe("connect");
  });

  it.each([
    [[], "t2va"],
    [["first_frame"], "i2va"],
    [["last_frame"], "l2va"],
    [["first_frame", "last_frame"], "fl2va"],
  ] as const)(
    "derives the task mode from the frame roles the anchor already has linked (%s)",
    (frames, expected) => {
      const canvas = communityCanvas([{ id: 20, frames }]);
      expect(listNativeAnchors(canvas)[0]?.taskMode).toBe(expected);
    },
  );

  it("reads a reference anchor as ref2va", () => {
    const canvas = communityCanvas([
      { id: 20, type: "MiniMaxH3ReferenceToVideo" },
    ]);
    expect(listNativeAnchors(canvas)[0]).toMatchObject({
      anchorType: "MiniMaxH3ReferenceToVideo",
      taskMode: "ref2va",
    });
  });
});

describe("the splice touches the anchor's prompt and nothing else", () => {
  it("rewires the designated anchor and leaves every foreign node untouched", () => {
    // The canvas carries the official duration chain, so a splice that wrote the
    // authored duration into the user's primitive would fail this row rather
    // than pass it unnoticed.
    const canvas = communityCanvas([{ id: 20, durationSeconds: 10 }]);
    const before = foreignNodes(canvas);
    const spliced = spliceContextPipeline(canvas, {
      taskMode: "t2va",
      userIntent: t2va.user_intent,
      durationSeconds: 5.167,
      connect: true,
    });
    expect(foreignNodes(spliced.workflow)).toEqual(before);

    const anchor = (spliced.workflow.nodes as Json[]).find(
      (node) => node.type === "MiniMaxH3ImageToVideo",
    )!;
    const prompt = (anchor.inputs as Json[]).find(
      (input) => input.name === "prompt",
    );
    expect(typeof prompt?.link).toBe("number");
    // The shell's own STRING output is what now feeds it.
    const link = (spliced.workflow.links as unknown[][]).find(
      (entry) => entry[0] === prompt?.link,
    )!;
    expect(link[1]).toBe(spliced.promptNodeId);
    expect(link[5]).toBe("STRING");
  });

  it("writes no artifact location, because the sink is not ours", () => {
    const spliced = spliceContextPipeline(communityCanvas(), {
      taskMode: "t2va",
      userIntent: t2va.user_intent,
      durationSeconds: 5.167,
      connect: true,
      artifactPrefix: "video/h3-context/session/deadbeefdeadbeef",
    });
    expect(spliced.artifactPrefixApplied).toBe(false);
    const sink = (spliced.workflow.nodes as Json[]).find(
      (node) => node.type === "SaveVideo",
    )!;
    expect((sink.widgets_values as unknown[])[0]).toBe("their-project/take-1");
  });

  it("does not write the authored duration into the user's own graph", () => {
    // D13. The materialized route writes the duration into the template's own
    // primitive; a graph the user assembled owns its length, and reaching into
    // it would both mutate a foreign node and make the disagreement check below
    // unreachable -- a graph forced into agreement always agrees.
    const canvas = communityCanvas([{ id: 20, durationSeconds: 10 }]);
    const spliced = spliceContextPipeline(canvas, {
      taskMode: "t2va",
      userIntent: t2va.user_intent,
      durationSeconds: 5.167,
      connect: true,
    });
    expect(spliced.durationApplied).toBe(false);
    const primitive = (spliced.workflow.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    expect((primitive.widgets_values as unknown[])[0]).toBe(10);
  });

  it("still writes the duration on the materialized route", () => {
    // The gate is about the connect route only. Removing the write entirely
    // would leave every materialized graph at the template's sample duration.
    const spliced = spliceContextPipeline(
      loadSyntheticTemplate("video_minimax_h3_t2v") as Json,
      {
        taskMode: "t2va",
        userIntent: t2va.user_intent,
        durationSeconds: 5.167,
      },
    );
    expect(spliced.durationApplied).toBe(true);
    const primitive = (spliced.workflow.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    expect((primitive.widgets_values as unknown[])[0]).toBe(5.167);
  });

  it("splices into the designated anchor when several exist", () => {
    const spliced = spliceContextPipeline(
      communityCanvas([{ id: 20 }, { id: 21 }]),
      {
        taskMode: "t2va",
        userIntent: t2va.user_intent,
        durationSeconds: 5.167,
        connect: true,
        designatedAnchorId: 21,
      },
    );
    expect(spliced.anchorNodeId).toBe(21);
    const other = (spliced.workflow.nodes as Json[]).find(
      (node) => node.id === 20,
    )!;
    expect(
      (other.inputs as Json[]).some((input) => input.name === "prompt"),
    ).toBe(false);
  });

  it.each([
    ["no anchor at all", undefined, "no_anchor"],
    ["several and no designation", undefined, "ambiguous_anchor"],
    ["a designation that is not an anchor", 999, "unknown_anchor"],
  ])("refuses %s", (label, designated, code) => {
    const canvas =
      label === "no anchor at all"
        ? (() => {
            const empty = communityCanvas();
            empty.nodes = (empty.nodes as Json[]).filter(
              (node) => !String(node.type).startsWith("MiniMaxH3"),
            );
            return empty;
          })()
        : communityCanvas(
            label === "several and no designation"
              ? [{ id: 20 }, { id: 21 }]
              : [{ id: 20 }],
          );
    expect(() =>
      spliceContextPipeline(canvas, {
        taskMode: "t2va",
        userIntent: t2va.user_intent,
        durationSeconds: 5.167,
        connect: true,
        designatedAnchorId: designated,
      }),
    ).toThrow(expect.objectContaining({ code }));
  });

  it("refuses an anchor that targets a different task mode", () => {
    // The anchor already has a first frame linked, so it is an i2va target. A
    // t2va request against it would queue a graph that does not do what was
    // asked, and the anchor is the authority because it is what the host reads.
    expect(() =>
      spliceContextPipeline(
        communityCanvas([{ id: 20, frames: ["first_frame"] }]),
        {
          taskMode: "t2va",
          userIntent: t2va.user_intent,
          durationSeconds: 5.167,
          connect: true,
        },
      ),
    ).toThrow(expect.objectContaining({ code: "task_mode_mismatch" }));
  });

  it.each(["root", "definition"] as const)(
    "refuses a second Connect before cloning or mutating %s ownership",
    (location) => {
      const canvas = alreadyOwnedCanvas(location);
      const before = structuredClone(canvas);
      expect(() =>
        spliceContextPipeline(canvas, {
          taskMode: "t2va",
          userIntent: t2va.user_intent,
          durationSeconds: 5.167,
          connect: true,
        }),
      ).toThrow(expect.objectContaining({ code: "already_connected" }));
      expect(canvas).toEqual(before);
    },
  );

  it.each(unsafeQueueMetadataCases)(
    "refuses $label before cloning or mutating the foreign canvas",
    ({ build }) => {
      const { canvas, accessorRead, cloneable } = build();
      const before = cloneable ? structuredClone(canvas) : undefined;
      expect(() =>
        spliceContextPipeline(canvas, {
          taskMode: "t2va",
          userIntent: t2va.user_intent,
          durationSeconds: 5.167,
          connect: true,
        }),
      ).toThrow(expect.objectContaining({ code: "malformed_template" }));
      if (before !== undefined) expect(canvas).toEqual(before);
      expect(accessorRead()).toBe(false);
    },
  );

  it("allows one foreign Connect and then owns exactly one pipeline", () => {
    const canvas = communityCanvas();
    expect(admitCanvasForConnect(canvas).tier).toBe("connect");
    const connected = spliceContextPipeline(canvas, {
      taskMode: "t2va",
      userIntent: t2va.user_intent,
      durationSeconds: 5.167,
      connect: true,
    }).workflow;
    const ownedTypes = (connected.nodes as Json[])
      .map((node) => String(node.type ?? ""))
      .filter((type) => type.startsWith("comfyui_h3_context.H3Context."));
    expect(new Set(ownedTypes).size).toBe(7);
    expect(ownedTypes).toHaveLength(7);
    expect(admitCanvasForConnect(connected).tier).toBe("unavailable");
  });
});

describe("App Mode's connect route", () => {
  const refusalProfiles = [
    ["asset_relocated", () => generationProfile({ text: ASSET_RELOCATED })],
    ["missing_asset", () => generationProfile({ text: MISSING_ASSET })],
    ["template_drift", () => generationProfile({ text: TEMPLATE_DRIFT })],
    ["unsupported_host", () => generationProfile({ text: UNSUPPORTED_HOST })],
    [
      "profile_unavailable",
      () => {
        throw new Error("synthetic unreadable profile");
      },
    ],
    [
      "unsupported_task_mode",
      () => {
        const available = loadAvailableProfile();
        return {
          ...available,
          families: available.families.map((family) => ({
            ...family,
            taskModes: family.taskModes.filter((mode) => mode !== "t2va"),
          })),
        };
      },
    ],
  ] as const;

  it.each(["mutate", "clone"] as const)(
    "connects, queues once, and reports the route through a %s wrapper",
    async (wrapperMode) => {
      const delegate = vi.fn().mockResolvedValue(acceptedQueueResponse(1));
      const host = hostOn(
        communityCanvas(),
        wrapQueuePrompt(delegate, wrapperMode),
      );
      host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
      const result = await createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(t2va, { connectExisting: {} });
      expect(result.route).toBe("connect");
      expect(delegate).toHaveBeenCalledTimes(1);
      expect(host.app.loadGraphData.mock.calls[0]?.[3]).toBe(
        host.workflowStore.activeWorkflow,
      );
      expect(host.workflowStore.openWorkflows).toEqual([
        host.workflowStore.activeWorkflow,
      ]);
      // No template was read: the canvas is the graph.
      expect(loadTemplate).not.toHaveBeenCalled();
    },
  );

  it("root-compiles a definitions-carrying Connect candidate on the captured workflow", async () => {
    const canvas = communityCanvas();
    canvas.definitions = {
      subgraphs: [
        {
          id: "foreign-connect-definition",
          nodes: [],
          links: [],
          inputs: [],
          outputs: [],
        },
      ],
    };
    const foreignBefore = opaqueForeignWidgetProjection(canvas);
    const host = hostOn(canvas);
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
    const createDetachedGraph = vi.fn((candidate: unknown) => candidate);

    const result = await createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile,
      createDetachedGraph,
    }).start(t2va, { connectExisting: {} });

    expect(result.route).toBe("connect");
    expect(createDetachedGraph).not.toHaveBeenCalled();
    expect(host.app.loadGraphData).toHaveBeenCalledOnce();
    expect(host.app.loadGraphData.mock.invocationCallOrder[0]).toBeLessThan(
      host.app.graphToPrompt.mock.invocationCallOrder[0]!,
    );
    expect(host.app.graphToPrompt).toHaveBeenCalledOnce();
    expect(host.app.graphToPrompt.mock.calls[0]).toEqual([]);
    expect(host.app.loadGraphData.mock.calls[0]?.[3]).toBe(host.activeWorkflow);
    expect(host.workflowStore.activeWorkflow).toBe(host.activeWorkflow);
    expect(host.workflowStore.openWorkflows).toEqual([host.activeWorkflow]);
    expect(
      ((host.graph().definitions as Json).subgraphs as Json[]).map(
        (definition) => definition.id,
      ),
    ).toEqual(["foreign-connect-definition"]);
    // The candidate must preserve caller-owned model/sampler values opaquely;
    // their content is never an admission rule or an expected default here.
    expect(opaqueForeignWidgetProjection(host.graph())).toEqual(foreignBefore);
    expect(host.api.queuePrompt).toHaveBeenCalledOnce();
    expect(loadProfile).not.toHaveBeenCalled();
    expect(loadTemplate).not.toHaveBeenCalled();
  });

  it("refuses managed Connect before a definitions candidate is written", async () => {
    const canvas = communityCanvas();
    canvas.definitions = {
      subgraphs: [
        {
          id: "foreign-managed-connect-definition",
          nodes: [],
          links: [],
          inputs: [],
          outputs: [],
        },
      ],
    };
    const host = hostOn(canvas);
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));

    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(t2va, {
        connectExisting: {},
        prepareManaged: vi.fn(),
      }),
    ).rejects.toMatchObject({ code: "invalid_request" });

    expect(host.app.loadGraphData).not.toHaveBeenCalled();
    expect(host.app.graphToPrompt).not.toHaveBeenCalled();
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it("connects once with exact installed queue metadata left opaque", async () => {
    const canvas = communityCanvas();
    canvas.widget_idx_map = {
      "3": { seed: 0, noise_seed: 0, sampler_name: 1, scheduler: 1 },
    };
    canvas.seed_widgets = { "20": 0 };
    const host = hostOn(canvas);
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));

    const result = await createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile,
    }).start(t2va, { connectExisting: {} });

    expect(result.route).toBe("connect");
    expect(host.graph()).toMatchObject({
      widget_idx_map: canvas.widget_idx_map,
      seed_widgets: canvas.seed_widgets,
    });
    expect(host.app.loadGraphData).toHaveBeenCalledTimes(1);
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
  });

  it("accepts a host wrapper without taking ownership of the foreign duration source", async () => {
    const host = hostOn(communityCanvas());
    const output = createQualifiedBasePrompt(
      t2va,
      {},
      {
        filenamePrefix: "their-project/take-1",
        sharedDurationSource: true,
      },
    );
    output["47"] = {
      class_type: "PrimitiveFloat",
      inputs: { value: ["48", 0] },
    };
    output["48"] = {
      class_type: "PrimitiveFloat",
      inputs: { value: t2va.duration_milliseconds / 1000 },
    };
    (output["46"] as { inputs: Record<string, unknown> }).inputs["values.a"] = [
      "47",
      0,
    ];
    host.app.graphToPrompt.mockResolvedValue({ output, workflow: {} });

    const result = await createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile,
    }).start(t2va, { connectExisting: {} });

    expect(result.route).toBe("connect");
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
    expect(loadTemplate).not.toHaveBeenCalled();
  });

  it.each(refusalProfiles)(
    "does not read or apply the %s materialization admission on connect",
    async (_reason, refusedProfile) => {
      const host = hostOn(communityCanvas());
      const routeProfile = vi.fn(refusedProfile);
      host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
      const result = await createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile: routeProfile,
      }).start(t2va, { connectExisting: {} });
      expect(result.route).toBe("connect");
      expect(routeProfile).not.toHaveBeenCalled();
      expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
      expect(loadTemplate).not.toHaveBeenCalled();
    },
  );

  it.each(["new", "replace"] as const)(
    "keeps unsupported-host admission blocking the retained %s route",
    async (route) => {
      const canvas =
        route === "new"
          ? ({ nodes: [], links: [] } as Json)
          : communityCanvas();
      const host = hostOn(canvas);
      const routeProfile = vi.fn(() =>
        generationProfile({ text: UNSUPPORTED_HOST }),
      );
      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile: routeProfile,
        }).start(t2va, route === "replace" ? { replaceExisting: true } : {}),
      ).rejects.toMatchObject({ code: "incompatible_seam" });
      expect(routeProfile).toHaveBeenCalledTimes(1);
      expect(host.app.loadGraphData).not.toHaveBeenCalled();
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
    },
  );

  it("does not apply unsupported-host materialization admission to useExisting", async () => {
    const fixture = existingManagedHostGraphFixture(t2va);
    const host = hostOn(fixture.workflow);
    host.app.graphToPrompt.mockResolvedValue(fixture.compiled);
    const routeProfile = vi.fn(() =>
      generationProfile({ text: UNSUPPORTED_HOST }),
    );

    const result = await createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile: routeProfile,
    }).start(t2va, { useExisting: true });

    expect(result.route).toBe("existing");
    expect(routeProfile).not.toHaveBeenCalled();
    expect(host.app.loadGraphData).toHaveBeenCalledTimes(1);
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
    expect(loadTemplate).not.toHaveBeenCalled();
  });

  it("uses an i2va canvas's graph-owned media without a caller source id", async () => {
    const inputs: AppModeInputs = { ...t2va, task_mode: "i2va" };
    const host = hostOn(communityCanvas([{ id: 20, frames: ["first_frame"] }]));
    host.app.graphToPrompt.mockImplementation(
      compiledConnectFrameFor(
        { ...inputs, first_frame_source: "17" },
        {
          first_frame: {
            class_type: "LoadImage",
            inputs: { image: "synthetic-frame.png" },
          },
        },
      ),
    );
    const result = await createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, { connectExisting: {} });
    expect(result.route).toBe("connect");
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
    expect(loadTemplate).not.toHaveBeenCalled();
  });

  it.each([
    ["i2va", "mutate", ["first_frame"], { first_frame_source: "9" }],
    ["i2va", "clone", ["first_frame"], { first_frame_source: "9" }],
    ["l2va", "mutate", ["last_frame"], { last_frame_source: "9" }],
    ["l2va", "clone", ["last_frame"], { last_frame_source: "9" }],
    [
      "fl2va",
      "mutate",
      ["first_frame", "last_frame"],
      { first_frame_source: "9", last_frame_source: "10" },
    ],
    [
      "fl2va",
      "clone",
      ["first_frame", "last_frame"],
      { first_frame_source: "9", last_frame_source: "10" },
    ],
  ] as const)(
    "declares the real %s connect frame edges through one typed Registry and a %s wrapper",
    async (taskMode, wrapperMode, frames, compiledSources) => {
      const inputs: AppModeInputs = { ...t2va, task_mode: taskMode };
      const canvas = communityCanvas([{ id: 20, frames }]);
      const spliced = spliceContextPipeline(canvas, {
        taskMode,
        userIntent: inputs.user_intent,
        durationSeconds: inputs.duration_milliseconds / 1000,
        connect: true,
      });
      const ownedNodes = (spliced.workflow.nodes as Json[]).filter((node) =>
        String(node.type).startsWith("comfyui_h3_context."),
      );
      const registry = ownedNodes.find(
        (node) =>
          node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
      )!;
      expect(registry).toBeDefined();
      const plan = ownedNodes.find(
        (node) => node.type === "comfyui_h3_context.H3Context.Plan",
      )!;
      expect(
        ((plan.inputs as Json[]) ?? []).find(
          (input) => input.name === "reference_registry",
        )?.link,
      ).toEqual(expect.any(Number));
      for (const role of frames) {
        const anchor = (spliced.workflow.nodes as Json[]).find(
          (node) => node.id === 20,
        )!;
        const anchorLinkId = ((anchor.inputs as Json[]) ?? []).find(
          (input) => input.name === role,
        )?.link;
        const registryLinkId = ((registry.inputs as Json[]) ?? []).find(
          (input) => input.name === role,
        )?.link;
        const anchorEdge = (spliced.workflow.links as unknown[][]).find(
          (edge) => edge[0] === anchorLinkId,
        )!;
        const registryEdge = (spliced.workflow.links as unknown[][]).find(
          (edge) => edge[0] === registryLinkId,
        )!;
        expect(registryEdge.slice(1, 3)).toEqual(anchorEdge.slice(1, 3));
        expect(spliced.ownedLinkIds).toContain(registryLinkId);
      }

      const opaqueSources = Object.fromEntries(
        frames.map((role) => [
          role,
          {
            class_type: "LoadImage",
            inputs: { image: `synthetic-${role}.png` },
          },
        ]),
      );
      const delegate = vi.fn().mockResolvedValue(acceptedQueueResponse(1));
      const host = hostOn(canvas, wrapQueuePrompt(delegate, wrapperMode));
      host.app.graphToPrompt.mockImplementation(
        compiledConnectFrameFor(
          { ...inputs, ...compiledSources },
          opaqueSources,
        ),
      );
      const result = await createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(inputs, { connectExisting: {} });
      expect(result.route).toBe("connect");
      expect(delegate).toHaveBeenCalledTimes(1);
      expect(loadTemplate).not.toHaveBeenCalled();
    },
  );

  it("keeps the connected frame branch opaque while proving Registry/native link parity", async () => {
    const inputs: AppModeInputs = { ...t2va, task_mode: "i2va" };
    const canvas = communityCanvas([{ id: 20, frames: ["first_frame"] }]);
    const host = hostOn(canvas);
    host.app.graphToPrompt.mockImplementation(
      compiledConnectFrameFor(
        { ...inputs, first_frame_source: "9" },
        {
          first_frame: {
            class_type: "LoadImage",
            inputs: { image: "synthetic-first-frame.png" },
          },
        },
        (output) => {
          (output["9"] as Json).class_type = "SomeoneElsesImageSource";
          output["90"] = {
            class_type: "LoadImage",
            inputs: { image: "unrelated.png" },
          };
        },
      ),
    );
    const result = await createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, { connectExisting: {} });
    expect(result.route).toBe("connect");
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
  });

  it("preserves every caller-selected foreign loader widget while connecting", () => {
    const canvas = communityCanvas();
    const selectedLoaders = [
      {
        id: 90,
        type: "UNETLoader",
        widgets_values: ["owner-selected-unet.safetensors", "default"],
      },
      {
        id: 91,
        type: "CLIPLoader",
        widgets_values: ["owner-selected-clip.safetensors", "wan", "default"],
      },
      {
        id: 92,
        type: "LoraLoaderModelOnly",
        widgets_values: ["owner-selected-lora.safetensors", 0.75],
      },
    ] as Json[];
    (canvas.nodes as Json[]).push(...selectedLoaders);
    const before = structuredClone(selectedLoaders);

    const spliced = spliceContextPipeline(canvas, {
      taskMode: "t2va",
      userIntent: t2va.user_intent,
      durationSeconds: t2va.duration_milliseconds / 1000,
      connect: true,
    });
    const after = (spliced.workflow.nodes as Json[]).filter((node) =>
      new Set([90, 91, 92]).has(Number(node.id)),
    );

    // CRITICAL: Connect owns only the prompt edge. Re-resolving or normalizing
    // foreign loader widgets here silently replaces the model the user chose.
    expect(after).toEqual(before);
  });

  it("refuses a compiled Connect graph whose typed Registry no longer matches the native frame edge", async () => {
    const inputs: AppModeInputs = { ...t2va, task_mode: "i2va" };
    const canvas = communityCanvas([{ id: 20, frames: ["first_frame"] }]);
    const before = structuredClone(canvas);
    const host = hostOn(canvas);
    host.app.graphToPrompt.mockImplementation(
      compiledConnectFrameFor(
        { ...inputs, first_frame_source: "9" },
        {
          first_frame: {
            class_type: "SomeoneElsesImageSource",
            inputs: {},
          },
        },
        (output) => {
          output["90"] = {
            class_type: "AnotherImageSource",
            inputs: {},
          };
          const registry = Object.values(output).find(
            (candidate) =>
              (candidate as Json).class_type ===
              "comfyui_h3_context.H3Context.ReferenceRegistry",
          ) as Json;
          (registry.inputs as Json).first_frame = ["90", 0];
        },
      ),
    );
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(inputs, { connectExisting: {} }),
    ).rejects.toMatchObject({ code: "compile_failed" });
    // B-M1605-EXIST-01: a host-serialized canvas carries its root graph id, so
    // the candidate is written before its root compile and the refusal restores
    // the captured workflow rather than staying zero-write.
    expect(host.app.loadGraphData).toHaveBeenCalledTimes(2);
    expect(host.app.loadGraphData.mock.calls[1]?.[0]).toEqual(before);
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
    expect(host.graph()).toEqual(before);
  });

  it("treats a missing source-output backlink as a missing Connect frame and never mutates", () => {
    const canvas = communityCanvas([{ id: 20, frames: ["first_frame"] }]);
    const source = (canvas.nodes as Json[]).find((node) => node.id === 2)!;
    ((source.outputs as Json[])[0] as Json).links = [];
    const before = structuredClone(canvas);
    expect(() =>
      spliceContextPipeline(canvas, {
        taskMode: "i2va",
        userIntent: t2va.user_intent,
        durationSeconds: t2va.duration_milliseconds / 1000,
        connect: true,
      }),
    ).toThrow(expect.objectContaining({ code: "connect_missing_first_frame" }));
    expect(canvas).toEqual(before);
  });

  it.each([
    ["i2va", [], "connect_missing_first_frame"],
    ["l2va", [], "connect_missing_last_frame"],
    ["fl2va", ["first_frame"], "connect_missing_last_frame"],
    ["fl2va", ["last_frame"], "connect_missing_first_frame"],
  ] as const)(
    "refuses %s with %s before writing when required frame wiring is absent",
    async (taskMode, frames, reason) => {
      const inputs: AppModeInputs = { ...t2va, task_mode: taskMode };
      const canvas = communityCanvas([{ id: 20, frames }]);
      const before = structuredClone(canvas);
      const host = hostOn(canvas);
      host.app.graphToPrompt.mockImplementation(compiledFor(inputs));
      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile,
        }).start(inputs, { connectExisting: {} }),
      ).rejects.toMatchObject({
        code: "incompatible_graph",
        reason: { kind: reason },
      });
      expect(host.app.loadGraphData).not.toHaveBeenCalled();
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
      expect(host.graph()).toEqual(before);
    },
  );

  it("rejects caller-owned media fields on connect before mutation or queue", async () => {
    const inputs: AppModeInputs = {
      ...t2va,
      task_mode: "i2va",
      first_frame_source: "17",
    };
    const host = hostOn(communityCanvas([{ id: 20, frames: ["first_frame"] }]));
    host.app.graphToPrompt.mockImplementation(compiledFor(inputs));
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(inputs, { connectExisting: {} }),
    ).rejects.toMatchObject({ code: "invalid_request" });
    expect(host.app.loadGraphData).not.toHaveBeenCalled();
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses a canvas with no anchor and never touches it", async () => {
    const canvas = communityCanvas();
    canvas.nodes = (canvas.nodes as Json[]).filter(
      (node) => !String(node.type).startsWith("MiniMaxH3"),
    );
    const host = hostOn(canvas);
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(t2va, { connectExisting: {} }),
    ).rejects.toMatchObject({
      code: "incompatible_graph",
      reason: {
        kind: "anchor_missing",
        requiredNode: "MiniMaxH3ImageToVideo",
      },
    });
    expect(host.app.loadGraphData).not.toHaveBeenCalled();
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses a designation that is not an anchor with the required native node", async () => {
    const host = hostOn(communityCanvas());
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(t2va, { connectExisting: { anchorNodeId: 999 } }),
    ).rejects.toMatchObject({
      code: "incompatible_graph",
      reason: {
        kind: "anchor_missing",
        requiredNode: "MiniMaxH3ImageToVideo",
      },
    });
    expect(host.app.loadGraphData).not.toHaveBeenCalled();
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it("maps duplicate ownership to bounded graph guidance with zero mutation", async () => {
    const canvas = alreadyOwnedCanvas("root");
    const before = structuredClone(canvas);
    const host = hostOn(canvas);
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(t2va, { connectExisting: {} }),
    ).rejects.toMatchObject({
      code: "incompatible_graph",
      message:
        "incompatible_graph: this canvas already contains an H3 Context pipeline; queue the current H3 graph instead",
    });
    expect(host.graph()).toEqual(before);
    expect(host.app.loadGraphData).not.toHaveBeenCalled();
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it.each(unsafeQueueMetadataCases)(
    "rejects $label before snapshot, canvas mutation, compilation, or queue",
    async ({ build }) => {
      const { canvas, accessorRead } = build();
      const host = hostOn(canvas);
      host.app.graphToPrompt.mockImplementation(compiledFor(t2va));

      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile,
        }).start(t2va, { connectExisting: {} }),
      ).rejects.toMatchObject({ code: "incompatible_graph" });

      expect(accessorRead()).toBe(false);
      expect(host.app.loadGraphData).not.toHaveBeenCalled();
      expect(host.app.graphToPrompt).not.toHaveBeenCalled();
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
    },
  );

  it("refuses an undesignated multi-anchor canvas with zero mutation", async () => {
    const host = hostOn(communityCanvas([{ id: 20 }, { id: 21 }]));
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(t2va, { connectExisting: {} }),
    ).rejects.toMatchObject({ code: "incompatible_graph" });
    expect(host.app.loadGraphData).not.toHaveBeenCalled();
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses a mode the anchor does not target, with zero queue", async () => {
    const host = hostOn(communityCanvas([{ id: 20, frames: ["first_frame"] }]));
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(t2va, { connectExisting: {} }),
    ).rejects.toMatchObject({ code: "incompatible_graph" });
    expect(host.app.loadGraphData).not.toHaveBeenCalled();
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it("does not inspect a foreign canvas length branch", async () => {
    const host = hostOn(communityCanvas());
    host.app.graphToPrompt.mockImplementation(
      compiledFor({ ...t2va, frame_count: 141 }),
    );
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(t2va, { connectExisting: {} }),
    ).resolves.toMatchObject({ queuePromptId: syntheticPromptUuid(1) });
    expect(host.api.queuePrompt).toHaveBeenCalledOnce();
  });

  it("leaves a canvas the adapter still admits", async () => {
    // After connecting, the graph must be one App Mode can bind again, or the
    // next run would have to replace what the user assembled. This is the
    // open-world census from phase 2c doing its job on a graph that is mostly
    // foreign.
    const host = hostOn(communityCanvas());
    host.app.graphToPrompt.mockImplementation(compiledFor(t2va));
    await createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile,
    }).start(t2va, { connectExisting: {} });
    expect(
      inspectVisibleH3Graph(host.graph(), H3_SHELL_MANIFEST),
    ).toMatchObject({ status: "ready", existingGraphCompatible: true });
  });

  it("is mutually exclusive with the other two routes", async () => {
    const host = hostOn(communityCanvas());
    for (const other of [{ useExisting: true }, { replaceExisting: true }])
      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile,
        }).start(t2va, { connectExisting: {}, ...other }),
      ).rejects.toMatchObject({ code: "invalid_request" });
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });
});
