import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  APP_MODE_MODE_CAPABILITIES,
  APP_MODE_WORKFLOW_ID,
  AppModeError,
  appModeArtifactPrefix,
  createAppModeController,
  compiledPromptMatchesRequestedRoute,
  compiledPromptMatchesVisibleGraph,
  fingerprint,
  graphFingerprint,
  isCompatibleExistingPrompt,
  listAppModeImageSources,
  listAppModeMediaSources,
  normalizeAppModeCompiledPrompt,
  OFFICIAL_LENGTH_EXPRESSION,
  qualifyAppMode,
  type AppModeCompiledPrompt,
  type AppModeDetachedGraphFactory,
  type AppModeInputs,
  type ManagedAppModeCanvasIdentity,
} from "../src/host/appMode";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import {
  loadSyntheticTemplate,
  materializedGraphSummary,
  syntheticTemplate,
} from "./support/templateFixture";
import {
  spliceContextPipeline,
  type Json,
} from "../src/host/templateMaterialization";
import {
  generationProfile,
  loadAvailableProfile,
  MISSING_ASSET,
  TEMPLATE_DRIFT,
  UNSUPPORTED_HOST,
} from "./support/generationProfileFixture";
import referenceAssistant from "../../subgraphs/H3 Context Assistant - Reference.json";
import baseAssistant from "../../subgraphs/H3 Context Assistant - Base.json";
import { H3_NODE_TYPES } from "../src/host/graphAdapter";
import {
  fixtureBackedAppModeApi,
  fixtureBackedAppModeHost,
  createNoisyHostExtension,
} from "./support/hostSeamTestDouble";
import { existingManagedHostGraphFixture } from "./support/managedHostGraphFixture";
import { existingI2vaScalerRegistryFixture } from "./support/existingRouteVariantFixture";
import {
  configureCandidateWithGraphIdWidgetStorePublication,
  configureDefinitionsCandidateWithLiveStorePublication,
  createHostWorkflowBehaviourFixture,
} from "./support/hostBehaviourCatalogue";
import {
  acceptedQueueResponse,
  rejectedQueueError,
  syntheticPromptUuid,
  wrapQueuePrompt,
  type QueuePromptDelegate,
} from "./support/queuePromptTestDouble";

/**
 * The template seam, spied.
 *
 * `loadGraphData` is both the materialization seam and the rollback seam, so
 * "was the canvas replaced" can no longer be read from it. Whether the template
 * was fetched answers that question exactly once per attempt.
 */
const loadTemplate = vi.fn(loadSyntheticTemplate);
/**
 * The profile seam, answering "yes" for every family.
 *
 * M17-20 D1 makes generation admission a backend decision App Mode has to ask
 * for, so a route test that did not inject one would be exercising the refusal
 * rather than the route. The refusal has its own rows.
 */
const loadProfile = vi.fn(loadAvailableProfile);

const createMaterializedPrompt = (
  inputs: Parameters<typeof createQualifiedBasePrompt>[0],
  opaqueSources: Parameters<typeof createQualifiedBasePrompt>[1] = {},
  options: Parameters<typeof createQualifiedBasePrompt>[2] = {},
) =>
  createQualifiedBasePrompt(inputs, opaqueSources, {
    ...options,
    sharedDurationSource: true,
  });

function existingGraphRouteFixture(
  inputs: Parameters<typeof createQualifiedBasePrompt>[0],
  options: Parameters<typeof createQualifiedBasePrompt>[2] = {},
): { workflow: Json; compiled: AppModeCompiledPrompt } {
  return existingManagedHostGraphFixture(inputs, options);
}

function ownedAuthoringProjection(workflow: Json): Readonly<{
  userIntent: unknown;
  durationSeconds: unknown;
}> {
  const nodes = Array.isArray(workflow.nodes) ? (workflow.nodes as Json[]) : [];
  const links = Array.isArray(workflow.links)
    ? (workflow.links as unknown[][])
    : [];
  const request = nodes.find((node) => node.type === H3_NODE_TYPES.request);
  const durationInput = Array.isArray(request?.inputs)
    ? (request.inputs as Json[]).find(
        (input) => input.name === "duration_seconds",
      )
    : undefined;
  const durationLink = links.find(
    (link) => String(link[0]) === String(durationInput?.link),
  );
  const durationSource = nodes.find(
    (node) => String(node.id) === String(durationLink?.[1]),
  );
  return Object.freeze({
    userIntent: Array.isArray(request?.widgets_values)
      ? request.widgets_values[1]
      : undefined,
    durationSeconds: Array.isArray(durationSource?.widgets_values)
      ? durationSource.widgets_values[0]
      : undefined,
  });
}

type DefinitionsExistingCompile = (
  state: Readonly<{
    compiled: AppModeCompiledPrompt;
    visible: () => Json;
  }>,
  detached?: unknown,
) => AppModeCompiledPrompt | Promise<AppModeCompiledPrompt>;

function definitionsExistingHost(
  inputs: AppModeInputs,
  compile?: DefinitionsExistingCompile,
) {
  const originalInputs: AppModeInputs = {
    ...inputs,
    user_intent: "The canvas-owned authoring value before Sidebar submission.",
    duration_milliseconds: 5000,
    frame_count: 120,
  };
  const originalFixture = existingGraphRouteFixture(originalInputs);
  const requestedFixture = existingGraphRouteFixture(inputs);
  const before = structuredClone(originalFixture.workflow);
  before.definitions = {
    subgraphs: [
      {
        id: "79dd8a95-existing-subgraph-instance",
        nodes: [],
        links: [],
        inputs: [],
        outputs: [],
      },
    ],
  };
  let visible = structuredClone(before);
  const activeWorkflow = { path: "workflows/definitions-existing.json" };
  const workflowStore: {
    activeWorkflow: object;
    openWorkflows: object[];
  } = {
    activeWorkflow,
    openWorkflows: [activeWorkflow],
  };
  const loadGraphData = vi.fn(
    (
      candidate: unknown,
      _clean = true,
      _restoreView = true,
      workflow: object | null = null,
    ) => {
      visible = structuredClone(candidate) as Json;
      if (workflow === null) {
        const duplicate = { path: "workflows/unexpected-copy.json" };
        workflowStore.openWorkflows.push(duplicate);
        workflowStore.activeWorkflow = duplicate;
      } else {
        workflowStore.activeWorkflow = workflow;
      }
    },
  );
  const createDetachedGraph = vi.fn((candidate: unknown) =>
    configureDefinitionsCandidateWithLiveStorePublication(
      candidate,
      (configured) => {
        visible = structuredClone(configured) as Json;
      },
    ),
  );
  const graphToPrompt = vi.fn((detached?: unknown) =>
    compile === undefined
      ? requestedFixture.compiled
      : compile(
          {
            compiled: requestedFixture.compiled,
            visible: () => visible,
          },
          detached,
        ),
  );
  const { app, api } = fixtureBackedAppModeHost(
    {
      extensionManager: { workflow: workflowStore },
      loadApiJson: vi.fn(),
      loadGraphData,
      graphToPrompt,
      graph: liveAuthoringGraph(() => visible),
    },
    { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(3)) },
  );
  return {
    activeWorkflow,
    api,
    app,
    before,
    createDetachedGraph,
    graphToPrompt,
    loadGraphData,
    visible: () => visible,
    workflowStore,
  };
}

function liveAuthoringGraph(serialize: () => Json) {
  const change = vi.fn();
  const setDirtyCanvas = vi.fn();
  const widgetCallback = vi.fn();
  const onWidgetChanged = vi.fn();
  const widgetNames = (node: Json): string[] =>
    node.type === H3_NODE_TYPES.request
      ? ["task_mode", "user_intent", "duration_seconds"]
      : node.type === "PrimitiveFloat"
        ? ["value"]
        : [];
  return {
    serialize,
    getNodeById(id: string | number) {
      const node = (serialize().nodes as Json[] | undefined)?.find(
        (candidate) => String(candidate.id) === String(id),
      );
      if (node === undefined) return undefined;
      const values = Array.isArray(node.widgets_values)
        ? node.widgets_values
        : [];
      return {
        type: node.type,
        widgets: widgetNames(node).map((name, index) => ({
          name,
          get value() {
            return values[index];
          },
          set value(value: unknown) {
            values[index] = value;
            const named = node.widgets_values_named;
            if (
              named !== null &&
              typeof named === "object" &&
              !Array.isArray(named)
            )
              (named as Json)[name] = value;
          },
          callback: widgetCallback,
        })),
        onWidgetChanged,
      };
    },
    change,
    setDirtyCanvas,
    receipt: { change, setDirtyCanvas, widgetCallback, onWidgetChanged },
  };
}

beforeEach(() => {
  loadTemplate.mockClear();
  loadProfile.mockClear();
  loadProfile.mockImplementation(loadAvailableProfile);
});

const {
  request: requestNodeType,
  plan: planNodeType,
  compiler: compilerNodeType,
  validator: validatorNodeType,
  nativeAdapter: nativeAdapterNodeType,
  productShell: productShellNodeType,
  preview: previewNodeType,
  imageGeneration: imageGenerationNodeType,
} = H3_NODE_TYPES;

function createQualifiedReferencePrompt() {
  const output = createQualifiedBasePrompt({
    task_mode: "t2va",
    user_intent: "Preserve the supplied reference pictures.",
    duration_milliseconds: 5167,
    frame_count: 124,
  }) as Record<string, Record<string, unknown>>;
  output["1"] = {
    ...output["1"],
    inputs: {
      ...(output["1"].inputs as Record<string, unknown>),
      task_mode: "ref2va",
    },
  };
  output["2"] = {
    ...output["2"],
    inputs: { request: ["1", 0], reference_registry: ["9", 0] },
  };
  output["6"] = {
    class_type: "MiniMaxH3ReferenceToVideo",
    inputs: {
      prompt: ["8", 0],
      width: 512,
      height: 512,
      length: 124,
      ref_image_size: "match",
      ref_images: [
        ["10", 0],
        ["11", 0],
      ],
    },
  };
  output["9"] = {
    class_type: "comfyui_h3_context.H3Context.ReferenceRegistry",
    inputs: {
      images: [
        ["10", 0],
        ["11", 0],
      ],
    },
  };
  output["10"] = {
    class_type: "LoadImage",
    inputs: { image: "fixture-a.png" },
  };
  output["11"] = {
    class_type: "LoadImage",
    inputs: { image: "fixture-b.png" },
  };
  return output;
}

function createQualifiedExternalReferencePrompt() {
  const output = createQualifiedReferencePrompt();
  delete output["10"];
  delete output["11"];
  output["6"] = {
    ...output["6"],
    inputs: {
      ...(output["6"].inputs as Record<string, unknown>),
      ref_images: [
        ["-10", 3],
        ["-10", 4],
      ],
      ref_videos: [["12", 0]],
      ref_audios: [["-10", 6]],
    },
  };
  output["9"] = {
    ...output["9"],
    inputs: {
      images: [
        ["-10", 3],
        ["-10", 4],
      ],
      videos: [["-10", 5]],
      audios: [["-10", 6]],
    },
  };
  output["12"] = {
    class_type: "GetVideoComponents",
    inputs: { video: ["-10", 5] },
  };
  return output;
}

function composePromptOutput(
  output: Record<string, unknown>,
  prefix = "6",
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(output).map(([id, node]) => {
      const nodeRecord = node as Record<string, unknown>;
      const nodeInputs = (nodeRecord.inputs ?? {}) as Record<string, unknown>;
      return [
        `${prefix}:${id}`,
        {
          ...nodeRecord,
          inputs: Object.fromEntries(
            Object.entries(nodeInputs).map(([name, value]) => [
              name,
              Array.isArray(value) && value.length === 2
                ? [`${prefix}:${value[0]}`, value[1]]
                : value,
            ]),
          ),
        },
      ];
    }),
  );
}

describe("H3 App Mode host seam", () => {
  it.each(["t2va", "i2va", "fl2va", "l2va", "ref2va"] as const)(
    "prepares %s on the canvas without acquiring generation or queue authority",
    async (taskMode) => {
      const inputs: AppModeInputs = {
        task_mode: taskMode,
        user_intent: "Inspect the prepared canvas before manual generation.",
        duration_milliseconds: 5167,
        frame_count: 124,
        ...(taskMode === "i2va" || taskMode === "fl2va"
          ? { first_frame_source: "17" }
          : {}),
        ...(taskMode === "l2va" || taskMode === "fl2va"
          ? { last_frame_source: "18" }
          : {}),
        ...(taskMode === "ref2va" ? { reference_image_sources: ["17"] } : {}),
      };
      const first = { class_type: "LoadImage", inputs: { image: "first.png" } };
      const last = { class_type: "LoadImage", inputs: { image: "last.png" } };
      const opaque = {
        ...(inputs.first_frame_source ? { first_frame: first } : {}),
        ...(inputs.last_frame_source ? { last_frame: last } : {}),
        ...(taskMode === "ref2va" ? { reference_images: [first] } : {}),
      };
      const graphToPrompt = vi.fn();
      if (taskMode !== "t2va")
        graphToPrompt.mockResolvedValueOnce({
          output: { "17": first, "18": last },
          workflow: {},
        });
      graphToPrompt.mockResolvedValue({
        output: createMaterializedPrompt(inputs, opaque),
        workflow: {},
      });
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData: vi.fn(),
          graphToPrompt,
          graph: {
            serialize: () => ({
              nodes: [
                { id: 17, type: "LoadImage", widgets_values: ["first.png"] },
                { id: 18, type: "LoadImage", widgets_values: ["last.png"] },
              ],
            }),
          },
        },
        {
          queuePrompt: vi
            .fn()
            .mockRejectedValue(rejectedQueueError("synthetic model")),
        },
      );
      const prepareManaged = vi.fn();
      const controller = createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      });
      const result = await controller.prepareCanvas(inputs, {
        replaceExisting: true,
        prepareManaged,
      });
      expect(result).toMatchObject({
        route: "replace",
        ownedNodeIds: expect.arrayContaining([expect.any(String)]),
      });
      expect(result).not.toHaveProperty("queuePromptId");
      expect(result).not.toHaveProperty("queueResult");
      expect(app.loadGraphData).toHaveBeenCalledOnce();
      expect(
        materializedGraphSummary(app.loadGraphData.mock.calls[0]?.[0])
          .promptIsLinked,
      ).toBe(true);
      expect(prepareManaged).not.toHaveBeenCalled();
      expect(api.queuePrompt).not.toHaveBeenCalled();
    },
  );

  describe("M23-25 detached validate-before-write", () => {
    const emptyCanvas = (): Json => ({
      last_node_id: 0,
      last_link_id: 0,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
      version: 0.4,
    });

    it("compiles the detached materialization candidate before its one canvas write", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent:
          "Validate the complete candidate before one visible write.",
        duration_milliseconds: 5167,
        frame_count: 124,
      };
      let visible = emptyCanvas();
      const loadGraphData = vi.fn((candidate: unknown) => {
        visible = structuredClone(candidate) as Json;
      });
      const createDetachedGraph = vi.fn((candidate: unknown) => ({
        candidate: structuredClone(candidate),
      }));
      const graphToPrompt = vi.fn((graph?: unknown) => ({
        output: createMaterializedPrompt(inputs),
        workflow:
          (graph as { candidate?: unknown } | undefined)?.candidate ?? visible,
      }));
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData,
          graphToPrompt,
          graph: { serialize: () => visible },
        },
        { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(1)) },
      );

      await createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
        createDetachedGraph,
      }).start(inputs);

      expect(createDetachedGraph).toHaveBeenCalledOnce();
      expect(graphToPrompt).toHaveBeenCalledOnce();
      expect(graphToPrompt.mock.calls[0]?.[0]).toBe(
        createDetachedGraph.mock.results[0]?.value,
      );
      expect(createDetachedGraph.mock.invocationCallOrder[0]).toBeLessThan(
        loadGraphData.mock.invocationCallOrder[0]!,
      );
      expect(loadGraphData).toHaveBeenCalledOnce();
      expect(api.queuePrompt).toHaveBeenCalledOnce();
    });

    it("refuses a detached compile envelope without writing or restoring the canvas", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent:
          "A malformed detached prompt must leave the canvas untouched.",
        duration_milliseconds: 5167,
        frame_count: 124,
      };
      const visible = emptyCanvas();
      const loadGraphData = vi.fn();
      const createDetachedGraph = vi.fn((candidate: unknown) => ({
        candidate,
      }));
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData,
          graphToPrompt: vi.fn().mockResolvedValue({
            output: {},
            workflow: emptyCanvas(),
          }),
          graph: { serialize: () => visible },
        },
        { queuePrompt: vi.fn() },
      );

      await expect(
        createAppModeController(app, api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph,
        }).start(inputs),
      ).rejects.toMatchObject({ code: "compile_failed" });
      expect(createDetachedGraph).toHaveBeenCalledOnce();
      expect(loadGraphData).not.toHaveBeenCalled();
      expect(api.queuePrompt).not.toHaveBeenCalled();
    });

    it("refuses a missing live detached constructor before any canvas write", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "A host without a detached graph class must fail closed.",
        duration_milliseconds: 5167,
        frame_count: 124,
      };
      const loadGraphData = vi.fn();
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData,
          graphToPrompt: vi.fn(),
          graph: { serialize: () => emptyCanvas() },
        },
        { queuePrompt: vi.fn() },
      );
      Object.defineProperty(app.graph!, "constructor", {
        configurable: true,
        value: undefined,
      });

      await expect(
        createAppModeController(app, api, {
          loadTemplate,
          loadProfile,
        }).start(inputs),
      ).rejects.toMatchObject({ code: "incompatible_seam" });
      expect(app.graphToPrompt).not.toHaveBeenCalled();
      expect(loadGraphData).not.toHaveBeenCalled();
      expect(api.queuePrompt).not.toHaveBeenCalled();
    });

    it("refuses a throwing detached constructor before any canvas write", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent:
          "A rejecting detached constructor must leave no canvas trace.",
        duration_milliseconds: 5167,
        frame_count: 124,
      };
      const loadGraphData = vi.fn();
      const createDetachedGraph = vi.fn(() => {
        throw new Error("private detached constructor detail");
      });
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData,
          graphToPrompt: vi.fn(),
          graph: { serialize: () => emptyCanvas() },
        },
        { queuePrompt: vi.fn() },
      );

      await expect(
        createAppModeController(app, api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph,
        }).start(inputs),
      ).rejects.toMatchObject({ code: "incompatible_seam" });
      expect(createDetachedGraph).toHaveBeenCalledOnce();
      expect(app.graphToPrompt).not.toHaveBeenCalled();
      expect(loadGraphData).not.toHaveBeenCalled();
      expect(api.queuePrompt).not.toHaveBeenCalled();
    });

    it("validates and commits an existing-canvas candidate with one bounded write", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "One detached existing-canvas authoring transaction.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const fixture = existingGraphRouteFixture(inputs);
      let visible = structuredClone(fixture.workflow);
      const loadGraphData = vi.fn((candidate: unknown) => {
        visible = structuredClone(candidate) as Json;
      });
      const createDetachedGraph = vi.fn((candidate: unknown) => ({
        candidate,
      }));
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData,
          graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
          graph: liveAuthoringGraph(() => visible),
        },
        {
          queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(2)),
        },
      );

      await createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
        createDetachedGraph,
      }).start(inputs, { useExisting: true });

      expect(createDetachedGraph).toHaveBeenCalledOnce();
      expect(loadGraphData).toHaveBeenCalledOnce();
      expect(api.queuePrompt).toHaveBeenCalledOnce();
    });

    it("B-M1605-EXIST-01 writes an id-carrying existing graph without definitions before its root compile", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "Sidebar authoring for an expanded existing graph.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const fixture = existingGraphRouteFixture(inputs);
      const liveGraphId = "4f0d2c2e-2a4b-4c8e-9e36-5d0b8e1a7c11";
      let visible: Json = {
        ...structuredClone(fixture.workflow),
        id: liveGraphId,
      };
      const request = (visible.nodes as Json[]).find(
        (node) => node.type === H3_NODE_TYPES.request,
      )!;
      (request.widgets_values as unknown[])[1] =
        "An earlier intent on the canvas.";
      const loadGraphData = vi.fn((candidate: unknown) => {
        visible = structuredClone(candidate) as Json;
      });
      const createDetachedGraph = vi.fn((candidate: unknown) =>
        configureCandidateWithGraphIdWidgetStorePublication(
          candidate,
          liveGraphId,
          (configured) => {
            visible = structuredClone(configured) as Json;
          },
        ),
      );
      const graphToPrompt = vi.fn().mockResolvedValue(fixture.compiled);
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData,
          graphToPrompt,
          graph: liveAuthoringGraph(() => visible),
        },
        {
          queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(2)),
        },
      );

      const result = await createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
        createDetachedGraph,
      }).start(inputs, { useExisting: true });

      expect(result.route).toBe("existing");
      expect(createDetachedGraph).not.toHaveBeenCalled();
      expect(loadGraphData).toHaveBeenCalledOnce();
      expect(loadGraphData.mock.invocationCallOrder[0]).toBeLessThan(
        graphToPrompt.mock.invocationCallOrder[0]!,
      );
      expect(graphToPrompt.mock.calls[0]).toEqual([]);
      expect(api.queuePrompt).toHaveBeenCalledOnce();
      expect(ownedAuthoringProjection(visible)).toEqual({
        userIntent: inputs.user_intent,
        durationSeconds: 8,
      });
    });

    it("defers non-owned existing pipeline rejection to host compilation", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "Let ComfyUI judge the user's remaining inference graph.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const fixture = existingGraphRouteFixture(inputs);
      const visible = structuredClone(fixture.workflow);
      const loadGraphData = vi.fn();
      const createDetachedGraph = vi.fn((candidate: unknown) => ({
        candidate,
      }));
      const graphToPrompt = vi
        .fn()
        .mockRejectedValue(new Error("content-free host compile refusal"));
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData,
          graphToPrompt,
          graph: liveAuthoringGraph(() => visible),
        },
        { queuePrompt: vi.fn() },
      );

      await expect(
        createAppModeController(app, api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph,
        }).start(inputs, { useExisting: true }),
      ).rejects.toMatchObject({ code: "compile_failed", source: "compile" });
      expect(createDetachedGraph).toHaveBeenCalledOnce();
      expect(graphToPrompt).toHaveBeenCalledOnce();
      expect(loadGraphData).not.toHaveBeenCalled();
      expect(api.queuePrompt).not.toHaveBeenCalled();
      expect(loadProfile).not.toHaveBeenCalled();
    });

    it("catalogues definitions configure as a live-store publication", () => {
      const candidate = {
        definitions: { subgraphs: [{ id: "catalogued-definition" }] },
        nodes: [{ id: 1, widgets_values: ["candidate"] }],
      };
      let live = { nodes: [{ id: 1, widgets_values: ["original"] }] };

      const configured = configureDefinitionsCandidateWithLiveStorePublication(
        candidate,
        (published) => {
          live = published;
        },
      );

      expect(live).toEqual(configured);
      expect(live).not.toBe(configured);
    });

    it("root-compiles a definitions-carrying existing candidate on the same workflow before one queue", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent:
          "Sidebar-owned authoring for the existing definitions graph.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const host = definitionsExistingHost(inputs);
      const onHostLoadBeforeCompile = vi.fn();

      const result = await createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
        createDetachedGraph: host.createDetachedGraph,
      }).start(inputs, { useExisting: true, onHostLoadBeforeCompile });

      expect(result.route).toBe("existing");
      expect(host.createDetachedGraph).not.toHaveBeenCalled();
      expect(onHostLoadBeforeCompile).toHaveBeenCalledOnce();
      expect(host.loadGraphData).toHaveBeenCalledOnce();
      expect(host.loadGraphData.mock.calls[0]?.[3]).toBe(host.activeWorkflow);
      expect(host.loadGraphData.mock.invocationCallOrder[0]).toBeLessThan(
        host.graphToPrompt.mock.invocationCallOrder[0]!,
      );
      expect(host.graphToPrompt).toHaveBeenCalledOnce();
      expect(host.graphToPrompt.mock.calls[0]).toEqual([]);
      expect(ownedAuthoringProjection(host.visible())).toEqual({
        userIntent: inputs.user_intent,
        durationSeconds: 8,
      });
      expect(
        ((host.visible().definitions as Json).subgraphs as Json[]).map(
          (definition) => definition.id,
        ),
      ).toEqual(["79dd8a95-existing-subgraph-instance"]);
      expect(host.workflowStore.activeWorkflow).toBe(host.activeWorkflow);
      expect(host.workflowStore.openWorkflows).toEqual([host.activeWorkflow]);
      expect(host.api.queuePrompt).toHaveBeenCalledOnce();
    });

    it("restores a definitions-carrying existing graph when root compilation throws", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "A root compile refusal must restore this transaction.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const host = definitionsExistingHost(inputs, async () => {
        throw new Error("content-free root compile refusal");
      });

      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph: host.createDetachedGraph,
        }).start(inputs, { useExisting: true }),
      ).rejects.toMatchObject({ code: "compile_failed" });

      expect(host.createDetachedGraph).not.toHaveBeenCalled();
      expect(host.graphToPrompt).toHaveBeenCalledOnce();
      expect(host.loadGraphData).toHaveBeenCalledTimes(2);
      expect(host.loadGraphData.mock.calls.map((call) => call[3])).toEqual([
        host.activeWorkflow,
        host.activeWorkflow,
      ]);
      expect(ownedAuthoringProjection(host.visible())).toEqual(
        ownedAuthoringProjection(host.before),
      );
      expect(host.workflowStore.activeWorkflow).toBe(host.activeWorkflow);
      expect(host.workflowStore.openWorkflows).toEqual([host.activeWorkflow]);
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
    });

    it("restores a definitions-carrying existing graph when root compilation is cancelled", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "A cancelled root compile must restore this transaction.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      let resolveCompile: (() => void) | undefined;
      const host = definitionsExistingHost(
        inputs,
        ({ compiled }) =>
          new Promise<AppModeCompiledPrompt>((resolve) => {
            resolveCompile = () => resolve(compiled);
          }),
      );
      const abort = new AbortController();
      const pending = createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
        createDetachedGraph: host.createDetachedGraph,
      }).start(inputs, { useExisting: true, signal: abort.signal });

      await vi.waitFor(() => expect(host.graphToPrompt).toHaveBeenCalledOnce());
      expect(host.loadGraphData).toHaveBeenCalledOnce();
      abort.abort();
      resolveCompile?.();
      await expect(pending).rejects.toMatchObject({ code: "cancelled" });

      expect(host.createDetachedGraph).not.toHaveBeenCalled();
      expect(host.graphToPrompt).toHaveBeenCalledOnce();
      expect(host.loadGraphData).toHaveBeenCalledTimes(2);
      expect(host.loadGraphData.mock.calls.map((call) => call[3])).toEqual([
        host.activeWorkflow,
        host.activeWorkflow,
      ]);
      expect(ownedAuthoringProjection(host.visible())).toEqual(
        ownedAuthoringProjection(host.before),
      );
      expect(host.workflowStore.activeWorkflow).toBe(host.activeWorkflow);
      expect(host.workflowStore.openWorkflows).toEqual([host.activeWorkflow]);
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
    });

    it("restores a partial definitions write when the same-workflow host load rejects", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "A partial host write must return to the captured graph.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const host = definitionsExistingHost(inputs);
      host.loadGraphData.mockImplementationOnce((candidate: unknown) => {
        const live = host.visible();
        for (const key of Object.keys(live)) delete live[key];
        Object.assign(live, structuredClone(candidate));
        throw new Error("content-free partial host load refusal");
      });

      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph: host.createDetachedGraph,
        }).start(inputs, { useExisting: true }),
      ).rejects.toMatchObject({ code: "compile_failed" });

      expect(host.graphToPrompt).not.toHaveBeenCalled();
      expect(host.loadGraphData).toHaveBeenCalledTimes(2);
      expect(host.loadGraphData.mock.calls.map((call) => call[3])).toEqual([
        host.activeWorkflow,
        host.activeWorkflow,
      ]);
      expect(ownedAuthoringProjection(host.visible())).toEqual(
        ownedAuthoringProjection(host.before),
      );
      expect(host.workflowStore.activeWorkflow).toBe(host.activeWorkflow);
      expect(host.workflowStore.openWorkflows).toEqual([host.activeWorkflow]);
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
    });

    it("restores and refuses a concurrent owned edit after definitions root compilation", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent:
          "The candidate must not overwrite a concurrent owned edit.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const host = definitionsExistingHost(inputs, ({ compiled, visible }) => {
        const request = (visible().nodes as Json[]).find(
          (node) => node.type === H3_NODE_TYPES.request,
        );
        if (!Array.isArray(request?.widgets_values))
          throw new Error("fixture request widgets are unavailable");
        request.widgets_values[1] = "A concurrent owned authoring edit.";
        return compiled;
      });

      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph: host.createDetachedGraph,
        }).start(inputs, { useExisting: true }),
      ).rejects.toMatchObject({ code: "stale_graph" });

      expect(host.createDetachedGraph).not.toHaveBeenCalled();
      expect(host.graphToPrompt).toHaveBeenCalledOnce();
      expect(host.loadGraphData).toHaveBeenCalledTimes(2);
      expect(ownedAuthoringProjection(host.visible())).toEqual(
        ownedAuthoringProjection(host.before),
      );
      expect(host.workflowStore.activeWorkflow).toBe(host.activeWorkflow);
      expect(host.workflowStore.openWorkflows).toEqual([host.activeWorkflow]);
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
    });

    it("restores and refuses a concurrent shared-duration edit after definitions root compilation", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent:
          "The candidate must not overwrite a concurrent duration edit.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const host = definitionsExistingHost(inputs, ({ compiled, visible }) => {
        const workflow = visible();
        const nodes = workflow.nodes as Json[];
        const links = workflow.links as unknown[][];
        const request = nodes.find(
          (node) => node.type === H3_NODE_TYPES.request,
        );
        const durationInput = (request?.inputs as Json[] | undefined)?.find(
          (input) => input.name === "duration_seconds",
        );
        const durationLink = links.find(
          (link) => String(link[0]) === String(durationInput?.link),
        );
        const durationSource = nodes.find(
          (node) => String(node.id) === String(durationLink?.[1]),
        );
        if (!Array.isArray(durationSource?.widgets_values))
          throw new Error("fixture duration source is unavailable");
        durationSource.widgets_values[0] = 13;
        return compiled;
      });

      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph: host.createDetachedGraph,
        }).start(inputs, { useExisting: true }),
      ).rejects.toMatchObject({ code: "stale_graph" });

      expect(host.createDetachedGraph).not.toHaveBeenCalled();
      expect(host.graphToPrompt).toHaveBeenCalledOnce();
      expect(host.loadGraphData).toHaveBeenCalledTimes(2);
      expect(ownedAuthoringProjection(host.visible())).toEqual(
        ownedAuthoringProjection(host.before),
      );
      expect(host.workflowStore.activeWorkflow).toBe(host.activeWorkflow);
      expect(host.workflowStore.openWorkflows).toEqual([host.activeWorkflow]);
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
    });

    it("restores a definitions-carrying existing graph when managed preparation refuses", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "A managed preparation refusal must restore this graph.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const host = definitionsExistingHost(inputs);

      await expect(
        createAppModeController(host.app, host.api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph: host.createDetachedGraph,
        }).start(inputs, {
          useExisting: true,
          prepareManaged: vi
            .fn()
            .mockRejectedValue(new Error("content-free preparation refusal")),
        }),
      ).rejects.toMatchObject({ code: "queue_failed" });

      expect(host.createDetachedGraph).not.toHaveBeenCalled();
      expect(host.graphToPrompt).toHaveBeenCalledOnce();
      expect(host.loadGraphData).toHaveBeenCalledTimes(2);
      expect(ownedAuthoringProjection(host.visible())).toEqual(
        ownedAuthoringProjection(host.before),
      );
      expect(host.workflowStore.activeWorkflow).toBe(host.activeWorkflow);
      expect(host.workflowStore.openWorkflows).toEqual([host.activeWorkflow]);
      expect(host.api.queuePrompt).not.toHaveBeenCalled();
    });

    it("completes managed preflight refusal before any candidate write", async () => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "A rejected managed preflight must not touch the canvas.",
        duration_milliseconds: 5167,
        frame_count: 124,
      };
      const visible = emptyCanvas();
      const loadGraphData = vi.fn();
      const createDetachedGraph = vi.fn((candidate: unknown) => ({
        candidate,
      }));
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData,
          graphToPrompt: vi.fn().mockResolvedValue({
            output: createMaterializedPrompt(inputs),
            workflow: emptyCanvas(),
          }),
          graph: { serialize: () => visible },
        },
        { queuePrompt: vi.fn() },
      );

      await expect(
        createAppModeController(app, api, {
          loadTemplate,
          loadProfile,
          createDetachedGraph,
        }).start(inputs, {
          prepareManaged: vi
            .fn()
            .mockRejectedValue(new Error("bounded managed preflight refusal")),
        }),
      ).rejects.toMatchObject({ code: "queue_failed" });
      expect(createDetachedGraph).toHaveBeenCalledOnce();
      expect(loadGraphData).not.toHaveBeenCalled();
      expect(api.queuePrompt).not.toHaveBeenCalled();
    });
  });

  it("qualifies a shared duration through the host wrapper and rejects split authority", () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A precise eight-second shot.",
      duration_milliseconds: 8000,
      frame_count: 192,
    };
    const output = createQualifiedBasePrompt(inputs) as Record<
      string,
      Record<string, unknown>
    >;
    output["50"] = { class_type: "PrimitiveFloat", inputs: { value: 8 } };
    output["51"] = {
      class_type: "ComfyMathExpression",
      inputs: {
        expression: OFFICIAL_LENGTH_EXPRESSION,
        "values.a": ["50", 0],
      },
    };
    (output["1"]!.inputs as Record<string, unknown>).duration_seconds = [
      "50",
      0,
    ];
    (output["6"]!.inputs as Record<string, unknown>).length = ["51", 1];
    const compiled = { output } as AppModeCompiledPrompt;

    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "materialize"),
    ).toBe(true);
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "existing"),
    ).toBe(true);

    output["52"] = {
      class_type: "PrimitiveFloat",
      inputs: { value: ["50", 0] },
    };
    (output["51"]!.inputs as Record<string, unknown>)["values.a"] = ["52", 0];
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "materialize"),
    ).toBe(true);
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "existing"),
    ).toBe(true);

    output["53"] = { class_type: "PrimitiveFloat", inputs: { value: 8 } };
    (output["1"]!.inputs as Record<string, unknown>).duration_seconds = [
      "53",
      0,
    ];
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "materialize"),
    ).toBe(true);
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "existing"),
    ).toBe(true);

    output["52"] = {
      class_type: "PrimitiveFloat",
      inputs: { value: ["52", 0] },
    };
    (output["1"]!.inputs as Record<string, unknown>).duration_seconds = [
      "52",
      0,
    ];
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "materialize"),
    ).toBe(false);

    output["52"] = {
      class_type: "PrimitiveFloat",
      inputs: { value: ["50", 1] },
    };
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "materialize"),
    ).toBe(false);

    output["52"] = {
      class_type: "Reroute",
      inputs: { value: ["50", 0] },
    };
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "materialize"),
    ).toBe(false);

    for (const [id, target] of [
      ["52", "54"],
      ["54", "55"],
      ["55", "56"],
      ["56", "57"],
      ["57", "50"],
    ] as const)
      output[id] = {
        class_type: "PrimitiveFloat",
        inputs: { value: [target, 0] },
      };
    expect(
      compiledPromptMatchesRequestedRoute(compiled, inputs, "materialize"),
    ).toBe(false);
  });

  it("publishes closed direct-use capability records for every non-reference mode", () => {
    expect(APP_MODE_MODE_CAPABILITIES).toEqual([
      expect.objectContaining({ task_mode: "t2va", required_asset_roles: [] }),
      expect.objectContaining({
        task_mode: "i2va",
        required_asset_roles: ["first_frame"],
      }),
      expect.objectContaining({
        task_mode: "fl2va",
        required_asset_roles: ["first_frame", "last_frame"],
      }),
      expect.objectContaining({
        task_mode: "l2va",
        required_asset_roles: ["last_frame"],
      }),
      expect.objectContaining({
        task_mode: "ref2va",
        required_asset_roles: ["reference"],
      }),
    ]);
    expect(APP_MODE_MODE_CAPABILITIES).toHaveLength(5);
    expect(
      APP_MODE_MODE_CAPABILITIES.every(
        (record) =>
          record.schema === "h3.app_mode_capability.v1" &&
          record.enabled === true &&
          record.disabled_reason === null &&
          record.graph_version === 1 &&
          record.surface_qualified === true,
      ),
    ).toBe(true);
    expect(APP_MODE_MODE_CAPABILITIES.at(-1)).toEqual(
      expect.objectContaining({
        task_mode: "ref2va",
        graph_profile: "h3.reference.direct.v1",
        evidence_revision: "m15-16.reference-direct.v1",
      }),
    );
  });

  it("lists only safe visible image-node identities", () => {
    expect(
      listAppModeImageSources({
        nodes: [
          {
            id: 17,
            type: "LoadImage",
            widgets_values: ["private-name.png"],
          },
          { id: 18, type: "ForeignImage", private_path: "C:/private" },
        ],
      }),
    ).toEqual([{ node_id: "17", label: "Image node 17" }]);
  });

  it("lists content-free visible image, video, and audio identities", () => {
    expect(
      listAppModeMediaSources({
        nodes: [
          { id: 17, type: "LoadImage", widgets_values: ["secret.png"] },
          { id: 22, type: "LoadVideo", widgets_values: ["secret.mp4"] },
          { id: 31, type: "LoadAudio", widgets_values: ["secret.wav"] },
          { id: 99, type: "Foreign.Media", path: "C:/private" },
        ],
      }),
    ).toEqual([
      { node_id: "31", label: "Audio node 31", kind: "audio", output_slot: 0 },
      { node_id: "17", label: "Image node 17", kind: "image", output_slot: 0 },
      { node_id: "22", label: "Video node 22", kind: "video", output_slot: 0 },
    ]);
  });

  it("builds dense Ref2VA bindings with paired video audio", () => {
    const prompt = createQualifiedBasePrompt(
      {
        task_mode: "ref2va",
        user_intent: "Preserve all selected references.",
        duration_milliseconds: 5167,
        frame_count: 124,
        reference_image_sources: ["17"],
        reference_video_sources: ["22"],
        reference_audio_sources: ["31"],
      },
      {
        reference_images: [
          { class_type: "LoadImage", inputs: { image: "private-a.png" } },
        ],
        reference_videos: [
          { class_type: "LoadVideo", inputs: { file: "private.mp4" } },
        ],
        reference_audios: [
          { class_type: "LoadAudio", inputs: { audio: "private.wav" } },
        ],
      },
    ) as Record<string, Record<string, unknown>>;
    // M17-17: the registry hears about the reference video's own soundtrack.
    // Before this item the pairing existed only on the anchor, so the registry
    // that owns canonical reference identity derived no soundtrack at all for a
    // graph that submitted one.
    expect(prompt["9"]?.inputs).toEqual({
      images: ["10", 0],
      videos: ["11", 0],
      "paired_audios.paired_audio0": ["13", 1],
      audios: ["12", 0],
    });
    expect(prompt["6"]?.inputs).toEqual(
      expect.objectContaining({
        "ref_images.ref_image_0": ["10", 0],
        "ref_videos.ref_video_0": ["13", 0],
        "ref_video_audios.ref_video_audio_0": ["13", 1],
        "ref_audios.ref_audio_0": ["12", 0],
      }),
    );
    expect(prompt["13"]).toEqual({
      class_type: "GetVideoComponents",
      inputs: { video: ["11", 0] },
    });
    expect(isCompatibleExistingPrompt({ output: prompt, workflow: {} })).toBe(
      true,
    );
    expect(() =>
      createQualifiedBasePrompt(
        {
          task_mode: "ref2va",
          user_intent: "Reject unsupported direct cardinality.",
          duration_milliseconds: 5167,
          frame_count: 124,
          reference_image_sources: ["17", "18"],
        },
        {},
      ),
    ).toThrow(/cardinality|source roles/i);
  });

  it.each(["mutate", "clone"] as const)(
    "qualifies opaque Ref2VA sources and queues once through a %s wrapper",
    async (wrapperMode) => {
      const inputs = {
        task_mode: "ref2va" as const,
        user_intent: "Use visible references only.",
        duration_milliseconds: 5167,
        frame_count: 124,
        reference_image_sources: ["17"],
        reference_video_sources: ["22"],
        reference_audio_sources: ["31"],
      };
      const serialized = {
        nodes: [
          { id: 17, type: "LoadImage", widgets_values: ["secret.png"] },
          { id: 22, type: "LoadVideo", widgets_values: ["secret.mp4"] },
          { id: 31, type: "LoadAudio", widgets_values: ["secret.wav"] },
        ],
      };
      const opaque = {
        reference_images: [
          { class_type: "LoadImage", inputs: { image: "secret.png" } },
        ],
        reference_videos: [
          { class_type: "LoadVideo", inputs: { file: "secret.mp4" } },
        ],
        reference_audios: [
          { class_type: "LoadAudio", inputs: { audio: "secret.wav" } },
        ],
      };
      const expected = createMaterializedPrompt(inputs, opaque);
      const delegate = vi.fn().mockResolvedValue(acceptedQueueResponse(4));
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData: vi.fn(),
          graphToPrompt: vi
            .fn()
            .mockResolvedValueOnce({
              output: {
                "17": opaque.reference_images[0],
                "22": opaque.reference_videos[0],
                "31": opaque.reference_audios[0],
              },
              workflow: {},
            })
            .mockResolvedValueOnce({ output: expected, workflow: {} }),
          graph: { serialize: () => serialized },
        },
        { queuePrompt: wrapQueuePrompt(delegate, wrapperMode) },
      );
      const controller = createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      });

      await controller.start(inputs, { replaceExisting: true });

      // The canvas is now the pinned reference template with the context pipeline
      // spliced into its prompt, not a prompt this module assembled.
      expect(loadTemplate).toHaveBeenCalledWith("video_minimax_h3_r2v");
      expect(app.loadGraphData).toHaveBeenCalledOnce();
      expect(
        materializedGraphSummary(app.loadGraphData.mock.calls[0]?.[0]),
      ).toEqual({
        anchorType: "MiniMaxH3ReferenceToVideo",
        requestWidgets: ["ref2va", inputs.user_intent, 5.167],
        promptIsLinked: true,
        sinks: 1,
      });
      expect(delegate).toHaveBeenCalledOnce();
    },
  );

  it.each([
    ["i2va", "first_frame", undefined],
    ["fl2va", "first_frame", "last_frame"],
    ["l2va", "last_frame", undefined],
  ] as const)(
    "builds a qualified %s route from opaque host-owned image sources",
    (taskMode, firstRole, secondRole) => {
      const inputs = {
        task_mode: taskMode,
        user_intent: "Preserve visible source ownership.",
        duration_milliseconds: 5167,
        frame_count: 124,
        first_frame_source:
          taskMode === "i2va" || taskMode === "fl2va" ? "17" : undefined,
        last_frame_source:
          taskMode === "l2va" || taskMode === "fl2va" ? "18" : undefined,
      };
      const prompt = createQualifiedBasePrompt(inputs, {
        first_frame:
          taskMode === "i2va" || taskMode === "fl2va"
            ? { class_type: "LoadImage", inputs: { image: "opaque-a" } }
            : undefined,
        last_frame:
          taskMode === "l2va" || taskMode === "fl2va"
            ? { class_type: "LoadImage", inputs: { image: "opaque-b" } }
            : undefined,
      }) as Record<string, Record<string, unknown>>;
      const generation = prompt["6"]!.inputs as Record<string, unknown>;
      expect(generation[firstRole]).toEqual(["9", 0]);
      if (secondRole !== undefined)
        expect(generation[secondRole]).toEqual(["10", 0]);
      expect(isCompatibleExistingPrompt({ output: prompt, workflow: {} })).toBe(
        true,
      );
    },
  );

  it("requires one exact frame source identity across native and typed consumers", () => {
    const inputs = {
      task_mode: "i2va" as const,
      user_intent: "Preserve one selected first frame.",
      duration_milliseconds: 5167,
      frame_count: 124,
      first_frame_source: "17",
    };
    const prompt = createMaterializedPrompt(inputs, {
      first_frame: {
        class_type: "LoadImage",
        inputs: { image: "opaque-a" },
      },
    }) as Record<string, Record<string, unknown>>;
    const compiled = { output: prompt, workflow: {} };
    const materialized = {
      firstFrame: 9,
      referenceImages: [],
      referenceVideos: [],
      referenceAudios: [],
    };
    expect(isCompatibleExistingPrompt(compiled)).toBe(true);
    expect(
      compiledPromptMatchesRequestedRoute(
        compiled,
        inputs,
        "materialize",
        materialized,
      ),
    ).toBe(true);

    prompt["12"] = {
      class_type: "LoadImage",
      inputs: { image: "different-opaque-source" },
    };
    (prompt["11"]!.inputs as Record<string, unknown>).first_frame = ["12", 0];
    expect(isCompatibleExistingPrompt(compiled)).toBe(false);
    expect(
      compiledPromptMatchesRequestedRoute(
        compiled,
        inputs,
        "materialize",
        materialized,
      ),
    ).toBe(false);
  });

  it("fails closed on every malformed typed frame ownership shape", () => {
    const inputs = {
      task_mode: "i2va" as const,
      user_intent: "Keep the typed frame binding exact.",
      duration_milliseconds: 5167,
      frame_count: 124,
      first_frame_source: "17",
    };
    const source = {
      first_frame: {
        class_type: "LoadImage",
        inputs: { image: "opaque-a" },
      },
    };
    const materialized = {
      firstFrame: 9,
      referenceImages: [],
      referenceVideos: [],
      referenceAudios: [],
    };
    const base = () =>
      createMaterializedPrompt(inputs, source) as Record<
        string,
        Record<string, unknown>
      >;
    const refuses = (output: Record<string, Record<string, unknown>>) => {
      const compiled = { output, workflow: {} };
      expect(isCompatibleExistingPrompt(compiled)).toBe(false);
      expect(
        compiledPromptMatchesRequestedRoute(
          compiled,
          inputs,
          "materialize",
          materialized,
        ),
      ).toBe(false);
    };

    const missing = base();
    (missing["11"]!.inputs as Record<string, unknown>) = {};
    refuses(missing);

    const wrongRole = base();
    (wrongRole["11"]!.inputs as Record<string, unknown>) = {
      last_frame: ["9", 0],
    };
    refuses(wrongRole);

    const wrongSlot = base();
    (wrongSlot["11"]!.inputs as Record<string, unknown>).first_frame = ["9", 1];
    refuses(wrongSlot);

    const duplicate = base();
    duplicate["12"] = {
      class_type: "comfyui_h3_context.H3Context.ReferenceRegistry",
      inputs: { first_frame: ["9", 0] },
    };
    expect(
      isCompatibleExistingPrompt({ output: duplicate, workflow: {} }),
    ).toBe(false);

    const textOnly = {
      task_mode: "t2va" as const,
      user_intent: "Keep text to video free of stray frames.",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    const stray = createMaterializedPrompt(textOnly) as Record<
      string,
      Record<string, unknown>
    >;
    stray["11"] = {
      class_type: "comfyui_h3_context.H3Context.ReferenceRegistry",
      inputs: {},
    };
    (stray["2"]!.inputs as Record<string, unknown>).reference_registry = [
      "11",
      0,
    ];
    expect(isCompatibleExistingPrompt({ output: stray, workflow: {} })).toBe(
      false,
    );
  });

  it("keeps distinct large graph identities distinct", () => {
    const first = fingerprint({ payload: "a".repeat(1_000_001) });
    const second = fingerprint({ payload: "b".repeat(1_000_001) });
    expect(first).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(second).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(first).not.toBe(second);
  });

  it("retains queue-wrapper writes in the surroundings digest", () => {
    const graph = { nodes: [{ id: 7, type: "Foreign.Node" }] };
    const withQueueMetadata = {
      ...graph,
      widget_idx_map: { "7": { seed: 0 } },
      seed_widgets: { "7": 0 },
    };
    expect(graphFingerprint(withQueueMetadata)).not.toBe(
      graphFingerprint(graph),
    );
    expect(
      graphFingerprint({ ...graph, private_queue_metadata: { "7": 0 } }),
    ).not.toBe(graphFingerprint(graph));
    expect(
      graphFingerprint({ nodes: [{ id: 8, type: "Foreign.Node" }] }),
    ).not.toBe(graphFingerprint(graph));
  });

  it("qualifies the public graph-load, graph-to-prompt, and queue seam", () => {
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn() },
    );
    expect(qualifyAppMode(app, api)).toEqual({ status: "ready" });
    expect(
      qualifyAppMode(
        { ...app, graph: { serialize: () => ({ nodes: [] }) } },
        api,
        1 as unknown as AppModeDetachedGraphFactory,
      ),
    ).toEqual({
      status: "unavailable",
      reason: "missing_detached_graph_constructor",
    });
  });

  it("never throws from qualification while the host graph is unreadable, and qualifies once it exists", () => {
    // M23-36: frontend 1.51.9 returns `undefined` from `app.graph` until the root graph
    // exists, and extension modules are evaluated before that point. A host getter may
    // also throw. Neither may escape qualification, and neither is a permanent verdict.
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn() },
    );
    const uninitialized = {
      ...app,
      graph: undefined as unknown as typeof app.graph,
    };
    expect(qualifyAppMode(uninitialized, api)).toEqual({
      status: "unavailable",
      reason: "missing_detached_graph_constructor",
    });
    const throwing = { ...app };
    Object.defineProperty(throwing, "graph", {
      get() {
        throw new Error("ComfyApp graph accessed before initialization");
      },
    });
    expect(qualifyAppMode(throwing, api)).toEqual({
      status: "unavailable",
      reason: "missing_detached_graph_constructor",
    });
    expect(qualifyAppMode(app, api)).toEqual({ status: "ready" });
  });

  it("refuses a missing template duration seam before replacing the canvas", async () => {
    const malformed = syntheticTemplate("video_minimax_h3_t2v");
    const anchor = (malformed.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "MiniMaxH3ImageToVideo",
    )!;
    const length = (anchor.inputs as Array<Record<string, unknown>>).find(
      (input) => input.name === "length",
    )!;
    length.link = null;
    loadTemplate.mockImplementationOnce(() => malformed);
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      }).start({
        task_mode: "t2va",
        user_intent: "Reject before replacing the canvas.",
        duration_milliseconds: 5000,
        frame_count: 124,
      }),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it.each(["mutate", "clone"] as const)(
    "loads the qualified base graph and queues exactly once through a %s wrapper",
    async (wrapperMode) => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "A kite crosses the sky.",
        duration_milliseconds: 5167,
        frame_count: 124,
      };
      const compiled = {
        output: createMaterializedPrompt(inputs),
        workflow: {},
      };
      const compiledBefore = structuredClone(compiled);
      const received: AppModeCompiledPrompt[] = [];
      const delegate: QueuePromptDelegate = vi.fn(async (_batch, envelope) => {
        received.push(envelope as AppModeCompiledPrompt);
        return acceptedQueueResponse(5);
      });
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData: vi.fn(),
          graphToPrompt: vi.fn().mockResolvedValue(compiled),
          graph: { serialize: () => ({ nodes: [] }) },
        },
        { queuePrompt: wrapQueuePrompt(delegate, wrapperMode) },
      );
      const controller = createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      });
      await controller.start(inputs);
      expect(loadTemplate).toHaveBeenCalledExactlyOnceWith(
        "video_minimax_h3_t2v",
      );
      expect(app.loadGraphData).toHaveBeenCalledOnce();
      expect(app.graphToPrompt).toHaveBeenCalledOnce();
      expect(delegate).toHaveBeenCalledOnce();
      expect(received[0]?.output).toEqual(compiled.output);
      expect(compiled).toEqual(compiledBefore);
    },
  );

  it("materializes in the active workflow without creating a tab", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A single canvas remains active across explicit runs.",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    const compiled = {
      output: createMaterializedPrompt(inputs),
      workflow: {},
    };
    let visibleGraph: Json = { nodes: [], links: [] };
    const activeWorkflow: object = { path: "workflows/repeated-run.json" };
    const workflowStore = {
      activeWorkflow,
      openWorkflows: [activeWorkflow] as object[],
    };
    const loadGraphData = vi.fn(
      (
        next: unknown,
        _clean = true,
        _restoreView = true,
        workflow: object | null = null,
      ) => {
        visibleGraph = structuredClone(next) as Json;
        if (workflow === null) {
          const temporary = { path: "workflows/unexpected-copy.json" };
          workflowStore.openWorkflows.push(temporary);
          workflowStore.activeWorkflow = temporary;
          return;
        }
        workflowStore.activeWorkflow = workflow;
      },
    );
    let promptNumber = 0;
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(compiled),
        graph: { serialize: () => visibleGraph },
      },
      {
        queuePrompt: vi.fn(async () =>
          acceptedQueueResponse(20 + ++promptNumber),
        ),
      },
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });

    const materialized = await controller.start(inputs);
    expect(materialized.route).toBe("new");
    expect(api.queuePrompt).toHaveBeenCalledOnce();
    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(
      loadGraphData.mock.calls.every((call) => call[3] === activeWorkflow),
    ).toBe(true);
    expect(workflowStore.activeWorkflow).toBe(activeWorkflow);
    expect(workflowStore.openWorkflows).toEqual([activeWorkflow]);
  });

  it("applies and queues twice without accumulating workflow tabs", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A repeated explicit run stays on this canvas.",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    const fixture = existingGraphRouteFixture(inputs);
    let visibleGraph = structuredClone(fixture.workflow);
    const activeWorkflow: object = { path: "workflows/repeated-apply.json" };
    const workflowStore = {
      activeWorkflow,
      openWorkflows: [activeWorkflow] as object[],
    };
    const loadGraphData = vi.fn(
      (
        next: unknown,
        _clean = true,
        _restoreView = true,
        workflow: object | null = null,
      ) => {
        visibleGraph = structuredClone(next) as Json;
        if (workflow === null) {
          const temporary = { path: "workflows/unexpected-copy.json" };
          workflowStore.openWorkflows.push(temporary);
          workflowStore.activeWorkflow = temporary;
          return;
        }
        workflowStore.activeWorkflow = workflow;
      },
    );
    const graph = liveAuthoringGraph(() => visibleGraph);
    let promptNumber = 0;
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      {
        queuePrompt: vi.fn(async () =>
          acceptedQueueResponse(30 + ++promptNumber),
        ),
      },
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });

    const first = await controller.start(inputs, { useExisting: true });
    const noisyHost = createNoisyHostExtension();
    noisyHost.afterConfigureGraph(visibleGraph);
    for (const node of visibleGraph.nodes as Json[])
      noisyHost.nodeCreated(node);
    const second = await controller.start(inputs, { useExisting: true });

    expect(first.route).toBe("existing");
    expect(second.route).toBe("existing");
    expect(second.ownedProjectionFingerprint).toBe(
      first.ownedProjectionFingerprint,
    );
    // D12: the rebuilt candidate remains stable; whole-graph surroundings are
    // retained as evidence but are not copied into the repository-owned graph.
    expect(second.graphFingerprint).toBe(first.graphFingerprint);
    expect(api.queuePrompt).toHaveBeenCalledTimes(2);
    expect(loadGraphData).toHaveBeenCalledTimes(2);
    expect(graph.receipt.change).not.toHaveBeenCalled();
    expect(workflowStore.activeWorkflow).toBe(activeWorkflow);
    expect(workflowStore.openWorkflows).toEqual([activeWorkflow]);
  });

  it("writes the authored candidate once without invoking host-owned widget callbacks", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A second managed run keeps the compiled subgraph live.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs);
    const visibleGraph = structuredClone(fixture.workflow);
    const request = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    const duration = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    request.widgets_values = [
      "t2va",
      "The first managed run left an older authored intent.",
      5.167,
    ];
    duration.widgets_values = [5.167];
    const graph = liveAuthoringGraph(() => visibleGraph);
    const getNodeById = graph.getNodeById.bind(graph);
    graph.getNodeById = (id: string | number) => {
      const node = getNodeById(id);
      return node === undefined ? undefined : { ...node, type: "HostLiveNode" };
    };
    graph.receipt.widgetCallback.mockImplementation(() => {
      throw new Error("host widget callbacks are outside the bounded write");
    });
    graph.receipt.onWidgetChanged.mockImplementation(() => {
      throw new Error("host node callbacks are outside the bounded write");
    });
    const loadGraphData = vi.fn();
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      {
        queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(6)),
      },
    );

    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, { useExisting: true });

    expect(result.route).toBe("existing");
    expect(loadGraphData).toHaveBeenCalledOnce();
    const written = app.graph.serialize?.() as Json;
    const writtenRequest = (written.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    const writtenDuration = (written.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    expect(writtenRequest.widgets_values).toEqual([
      "t2va",
      inputs.user_intent,
      5.167,
    ]);
    expect(writtenDuration.widgets_values).toEqual([8]);
    expect(request.widgets_values).toEqual([
      "t2va",
      "The first managed run left an older authored intent.",
      5.167,
    ]);
    expect(duration.widgets_values).toEqual([5.167]);
    expect(graph.receipt.change).not.toHaveBeenCalled();
    expect(graph.receipt.setDirtyCanvas).not.toHaveBeenCalled();
    expect(graph.receipt.widgetCallback).not.toHaveBeenCalled();
    expect(graph.receipt.onWidgetChanged).not.toHaveBeenCalled();
    expect(api.queuePrompt).toHaveBeenCalledOnce();
  });

  it.each(["mutate_in_place", "forward_copy"] as const)(
    "treats noisy host surroundings as evidence and gives the single managed queue a wrapper-safe %s envelope",
    async (wrapperMode) => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent: "Foreign canvas metadata cannot own this submission.",
        duration_milliseconds: 8_000,
        frame_count: 192,
      };
      const fixture = existingGraphRouteFixture(inputs);
      const visibleGraph = structuredClone(fixture.workflow);
      const graph = liveAuthoringGraph(() => visibleGraph);
      let forwardedPrompt: AppModeCompiledPrompt | undefined;
      const queuePrompt = vi.fn(
        async (_batch: number, prompt: AppModeCompiledPrompt) => {
          const workflow = prompt.workflow as Record<string, unknown>;
          if (wrapperMode === "mutate_in_place") {
            workflow.widget_idx_map = {};
            workflow.seed_widgets = {};
            forwardedPrompt = prompt;
          } else {
            forwardedPrompt = {
              ...prompt,
              workflow: {
                ...workflow,
                widget_idx_map: {},
                seed_widgets: {},
              },
            };
          }
          return acceptedQueueResponse(7);
        },
      );
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData: vi.fn(),
          graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
          graph,
        },
        { queuePrompt },
      );
      let retainedWorkflow: Record<string, unknown> | undefined;
      let retainedManagedIdentity:
        | Readonly<{
            workflowAuthority: object;
            ownedReference: Readonly<{
              nodeIds: readonly string[];
              linkIds: readonly string[];
            }>;
          }>
        | undefined;
      const prepareManaged = vi.fn(async (prepared) => {
        retainedWorkflow = prepared.bootstrap.workflow as Record<
          string,
          unknown
        >;
        const noisyHost = createNoisyHostExtension();
        noisyHost.afterConfigureGraph(visibleGraph);
        for (const node of visibleGraph.nodes as Json[])
          noisyHost.nodeCreated(node);
        return {
          expectedIdentity: {
            // D12: the backend may echo a different surroundings digest; it is evidence only.
            graphFingerprint: `sha256:${"9".repeat(64)}`,
            compiledPromptFingerprint:
              prepared.observation.compiled_prompt_fingerprint,
          },
          bindCanvasIdentity: (identity: ManagedAppModeCanvasIdentity) => {
            retainedManagedIdentity = identity;
          },
          onQueueSubmitted: () => undefined,
          onQueueAccepted: () => undefined,
          onQueueFailed: () => undefined,
        };
      });

      const result = await createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      }).start(inputs, { useExisting: true, prepareManaged });

      expect(result.route).toBe("existing");
      expect(result.ownedProjectionFingerprint).toBe(
        prepareManaged.mock.calls[0]![0].observation
          .owned_projection_fingerprint,
      );
      const activeWorkflow = (
        app as typeof app & {
          extensionManager?: { workflow?: { activeWorkflow?: object } };
        }
      ).extensionManager?.workflow?.activeWorkflow;
      expect(retainedManagedIdentity?.workflowAuthority).toBe(activeWorkflow);
      expect(
        [...(retainedManagedIdentity?.ownedReference.nodeIds ?? [])].sort(),
      ).toEqual([...result.ownedNodeIds].sort());
      expect(
        [...(retainedManagedIdentity?.ownedReference.linkIds ?? [])].sort(),
      ).toEqual([...result.ownedLinkIds].sort());
      expect(queuePrompt).toHaveBeenCalledOnce();
      expect(forwardedPrompt).toBeDefined();
      expect(forwardedPrompt?.workflow).toMatchObject({
        nodes: [],
        links: [],
        widget_idx_map: {},
        seed_widgets: {},
      });
      expect(
        Object.values(forwardedPrompt?.output ?? {}).map(
          (node) => (node as { class_type?: unknown }).class_type,
        ),
      ).toEqual(expect.arrayContaining(["MiniMaxH3ImageToVideo", "SaveVideo"]));
      expect(forwardedPrompt?.workflow).not.toBe(retainedWorkflow);
      expect(Object.hasOwn(retainedWorkflow ?? {}, "widget_idx_map")).toBe(
        false,
      );
      expect(Object.hasOwn(retainedWorkflow ?? {}, "seed_widgets")).toBe(false);
    },
  );

  it("refuses a compiled prompt mutation between validation and queue", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "The validated API prompt is the queued API prompt.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs);
    const visibleGraph = structuredClone(fixture.workflow);
    const graph = liveAuthoringGraph(() => visibleGraph);
    const queuePrompt = vi.fn();
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );
    const onQueueFailed = vi.fn();
    const prepareManaged = vi.fn(async (prepared) => {
      const request = fixture.compiled.output["1"] as {
        inputs: Record<string, unknown>;
      };
      request.inputs.user_intent = "A post-validation mutation.";
      return {
        expectedIdentity: {
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
        },
        onQueueSubmitted: () => undefined,
        onQueueAccepted: () => undefined,
        onQueueFailed,
      };
    });

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        { useExisting: true, prepareManaged },
      ),
    ).rejects.toMatchObject({ code: "stale_graph" });
    expect(onQueueFailed).toHaveBeenCalledExactlyOnceWith("not_invoked");
    expect(queuePrompt).not.toHaveBeenCalled();
  });

  it("fingerprints and queues one identical managed execution projection", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "Queue one validated model-free execution identity.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs, { artifactSink: false });
    const visibleGraph = structuredClone(fixture.workflow);
    const graph = liveAuthoringGraph(() => visibleGraph);
    let forwarded: AppModeCompiledPrompt | undefined;
    const queuePrompt = vi.fn(
      async (_batch: number, compiled: AppModeCompiledPrompt) => {
        forwarded = compiled;
        return acceptedQueueResponse(201);
      },
    );
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );
    const compiledBefore = structuredClone(fixture.compiled);
    const projectManagedExecution = vi.fn(
      (compiled: AppModeCompiledPrompt): AppModeCompiledPrompt => {
        const projected = structuredClone(compiled);
        const native = Object.entries(projected.output).find(([, value]) =>
          String(
            (value as { class_type?: unknown }).class_type ?? "",
          ).startsWith("MiniMaxH3"),
        );
        if (native === undefined) throw new Error("native boundary absent");
        delete projected.output[native[0]];
        return projected;
      },
    );
    const prepareManaged = vi.fn(async (prepared) => ({
      expectedIdentity: {
        graphFingerprint: prepared.observation.graph_fingerprint,
        compiledPromptFingerprint:
          prepared.observation.compiled_prompt_fingerprint,
      },
      onQueueSubmitted: () => undefined,
      onQueueAccepted: () => undefined,
      onQueueFailed: () => undefined,
    }));

    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, {
      useExisting: true,
      prepareManaged,
      projectManagedExecution,
    });

    expect(projectManagedExecution).toHaveBeenCalledTimes(2);
    expect(prepareManaged).toHaveBeenCalledOnce();
    expect(result.compiledPromptFingerprint).toBe(
      prepareManaged.mock.calls[0]![0].observation.compiled_prompt_fingerprint,
    );
    expect(fingerprint(forwarded)).toBe(result.compiledPromptFingerprint);
    expect(
      Object.values(forwarded?.output ?? {}).some((value) =>
        String((value as { class_type?: unknown }).class_type ?? "").startsWith(
          "MiniMaxH3",
        ),
      ),
    ).toBe(false);
    expect(fixture.compiled).toEqual(compiledBefore);
  });

  it("rejects a non-deterministic managed execution projection before transport", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "Reject divergent managed execution identities.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs, { artifactSink: false });
    const visibleGraph = structuredClone(fixture.workflow);
    const graph = liveAuthoringGraph(() => visibleGraph);
    const queuePrompt = vi.fn();
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );
    let projectionCall = 0;
    const projectManagedExecution = vi.fn(
      (compiled: AppModeCompiledPrompt): AppModeCompiledPrompt => {
        projectionCall += 1;
        const projected = structuredClone(compiled);
        projected.workflow = { projectionCall };
        return projected;
      },
    );
    const onQueueFailed = vi.fn();

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        {
          useExisting: true,
          projectManagedExecution,
          prepareManaged: async (prepared) => ({
            expectedIdentity: {
              graphFingerprint: prepared.observation.graph_fingerprint,
              compiledPromptFingerprint:
                prepared.observation.compiled_prompt_fingerprint,
            },
            onQueueSubmitted: () => undefined,
            onQueueAccepted: () => undefined,
            onQueueFailed,
          }),
        },
      ),
    ).rejects.toMatchObject({ code: "compile_failed" });
    expect(projectManagedExecution).toHaveBeenCalledTimes(2);
    expect(onQueueFailed).toHaveBeenCalledExactlyOnceWith("not_invoked");
    expect(queuePrompt).not.toHaveBeenCalled();
  });

  it("rejects a managed execution projection without a transaction owner", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "Do not admit an unowned model-free execution.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs, { artifactSink: false });
    const visibleGraph = structuredClone(fixture.workflow);
    const graph = liveAuthoringGraph(() => visibleGraph);
    const queuePrompt = vi.fn();
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );
    const projectManagedExecution = vi.fn((compiled: AppModeCompiledPrompt) =>
      structuredClone(compiled),
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        { useExisting: true, projectManagedExecution },
      ),
    ).rejects.toMatchObject({ code: "invalid_request" });
    expect(projectManagedExecution).not.toHaveBeenCalled();
    expect(queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses an equal-projection workflow object switch before queue", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent:
        "The captured workflow object owns the final queue boundary.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs);
    const visibleGraph = structuredClone(fixture.workflow);
    const graph = liveAuthoringGraph(() => visibleGraph);
    const queuePrompt = vi.fn();
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );
    const workflowStore = (
      app as typeof app & {
        extensionManager: { workflow: { activeWorkflow: object } };
      }
    ).extensionManager.workflow;
    const capturedWorkflow = workflowStore.activeWorkflow;
    const foreignWorkflow = { path: "workflows/equal-projection-foreign.json" };
    const onQueueFailed = vi.fn();
    const prepareManaged = vi.fn(async (prepared) => {
      return {
        expectedIdentity: {
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
        },
        bindCanvasIdentity: (identity: ManagedAppModeCanvasIdentity) => {
          expect(identity.workflowAuthority).toBe(capturedWorkflow);
          workflowStore.activeWorkflow = foreignWorkflow;
        },
        onQueueSubmitted: () => undefined,
        onQueueAccepted: () => undefined,
        onQueueFailed,
      };
    });

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        { useExisting: true, prepareManaged },
      ),
    ).rejects.toMatchObject({ code: "stale_graph" });
    expect(onQueueFailed).toHaveBeenCalledExactlyOnceWith("not_invoked");
    expect(queuePrompt).not.toHaveBeenCalled();
    expect(workflowStore.activeWorkflow).toBe(foreignWorkflow);
  });

  it("refuses a serialized graph whose bounded authoring seam is unavailable", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A managed graph requires the supported live mutation seam.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs);
    const visibleGraph = structuredClone(fixture.workflow);
    visibleGraph.nodes = (visibleGraph.nodes as Json[]).filter(
      (node) => node.type !== H3_NODE_TYPES.request,
    );
    const loadGraphData = vi.fn();
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn(),
        graph: { serialize: () => visibleGraph },
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        { useExisting: true },
      ),
    ).rejects.toMatchObject({ code: "incompatible_graph" });
    expect(loadGraphData).not.toHaveBeenCalled();
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses duplicate serialized authoring nodes before mutation", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A duplicate authoring widget must not be guessed.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs);
    const visibleGraph = structuredClone(fixture.workflow);
    const graph = liveAuthoringGraph(() => visibleGraph);
    const request = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    (visibleGraph.nodes as Json[]).push({
      ...structuredClone(request),
      id: 10_001,
    });
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph,
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        { useExisting: true },
      ),
    ).rejects.toMatchObject({ code: "incompatible_graph" });
    expect(graph.receipt.change).not.toHaveBeenCalled();
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("does not treat a serialized Request task mode as pipeline admission", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A concurrent live widget change must be detected.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs);
    let visibleGraph = structuredClone(fixture.workflow);
    const graph = liveAuthoringGraph(() => visibleGraph);
    const request = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    request.widgets_values = ["i2va", inputs.user_intent, 8];
    const loadGraphData = vi.fn((next: unknown) => {
      visibleGraph = structuredClone(next) as Json;
    });
    const queuePrompt = vi.fn(async () => acceptedQueueResponse(99));
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );

    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, { useExisting: true });
    expect(result.queuePromptId).toBe(syntheticPromptUuid(99));
    expect(graph.receipt.change).not.toHaveBeenCalled();
    expect(app.graphToPrompt).toHaveBeenCalledOnce();
    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(queuePrompt).toHaveBeenCalledOnce();
    expect(loadProfile).not.toHaveBeenCalled();
  });

  it("does not overwrite foreign graph drift while rolling back live authoring", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A bounded authoring transaction detects foreign drift.",
      duration_milliseconds: 8_000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs);
    const visibleGraph = structuredClone(fixture.workflow);
    const request = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    const duration = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    request.widgets_values = ["t2va", "An older bounded intent.", 5.167];
    duration.widgets_values = [5.167];
    const graph = liveAuthoringGraph(() => visibleGraph);
    const loadGraphData = vi.fn();
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn(() => {
          visibleGraph.foreign_change = { owner: "host" };
          throw new Error("the compiler observed a concurrent graph change");
        }),
        graph,
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        { useExisting: true },
      ),
    ).rejects.toMatchObject({ code: "compile_failed" });
    expect(loadGraphData).not.toHaveBeenCalled();
    expect(visibleGraph.foreign_change).toEqual({ owner: "host" });
    expect(request.widgets_values).toEqual([
      "t2va",
      "An older bounded intent.",
      5.167,
    ]);
    expect(duration.widgets_values).toEqual([5.167]);
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("fails closed on an expected command identity mismatch before queue ownership", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A bounded sequence command.",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    const compiled = {
      output: createQualifiedBasePrompt(inputs),
      workflow: {},
    };
    const initialGraph = { nodes: [] };
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue(compiled),
        graph: { serialize: () => initialGraph },
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      }).start(inputs, {
        expectedIdentity: {
          graphFingerprint: fingerprint(initialGraph),
          compiledPromptFingerprint: `sha256:${"f".repeat(64)}`,
        },
      }),
    ).rejects.toMatchObject({ code: "compile_failed" });
    expect(api.queuePrompt).not.toHaveBeenCalled();
    expect(app.loadGraphData).not.toHaveBeenCalled();
  });

  it.each([
    ["i2va", "mutate", "17", undefined],
    ["i2va", "clone", "17", undefined],
    ["fl2va", "mutate", "17", "18"],
    ["fl2va", "clone", "17", "18"],
    ["l2va", "mutate", undefined, "18"],
    ["l2va", "clone", undefined, "18"],
  ] as const)(
    "qualifies visible host-owned images opaquely and queues %s once through a %s wrapper",
    async (taskMode, wrapperMode, firstSource, lastSource) => {
      const inputs = {
        task_mode: taskMode,
        user_intent: "Animate the selected frame route.",
        duration_milliseconds: 5167,
        frame_count: 124,
        ...(firstSource === undefined
          ? {}
          : { first_frame_source: firstSource }),
        ...(lastSource === undefined ? {} : { last_frame_source: lastSource }),
      };
      const serialized = {
        nodes: [
          {
            id: 17,
            type: "LoadImage",
            widgets_values: ["private-source.png"],
          },
          {
            id: 18,
            type: "LoadImage",
            widgets_values: ["private-last.png"],
          },
        ],
      };
      const firstOpaqueSource = {
        class_type: "LoadImage",
        inputs: { image: "private-source.png" },
      };
      const lastOpaqueSource = {
        class_type: "LoadImage",
        inputs: { image: "private-last.png" },
      };
      const expectedPrompt = createMaterializedPrompt(inputs, {
        ...(firstSource === undefined
          ? {}
          : { first_frame: firstOpaqueSource }),
        ...(lastSource === undefined ? {} : { last_frame: lastOpaqueSource }),
      });
      const delegate = vi.fn().mockResolvedValue(acceptedQueueResponse(8));
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData: vi.fn(),
          graphToPrompt: vi
            .fn()
            .mockResolvedValueOnce({
              output: {
                "17": firstOpaqueSource,
                "18": lastOpaqueSource,
              },
              workflow: {},
            })
            .mockResolvedValueOnce({ output: expectedPrompt, workflow: {} }),
          graph: { serialize: () => serialized },
        },
        { queuePrompt: wrapQueuePrompt(delegate, wrapperMode) },
      );
      const controller = createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      });
      expect(controller.imageSources()).toEqual([
        { node_id: "17", label: "Image node 17" },
        { node_id: "18", label: "Image node 18" },
      ]);

      await controller.start(inputs, { replaceExisting: true });

      expect(app.graphToPrompt).toHaveBeenCalledTimes(2);
      expect(loadTemplate).toHaveBeenCalledWith("video_minimax_h3_i2v");
      expect(
        materializedGraphSummary(app.loadGraphData.mock.calls[0]?.[0]),
      ).toEqual({
        anchorType: "MiniMaxH3ImageToVideo",
        requestWidgets: [taskMode, inputs.user_intent, 5.167],
        promptIsLinked: true,
        sinks: 1,
      });
      expect(delegate).toHaveBeenCalledOnce();
    },
  );

  it("fails closed when the normal queue omits its prompt identity", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A safe queue identity test.",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn().mockResolvedValue({}) },
    );
    await expect(
      createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      }).start(inputs),
    ).rejects.toThrow(/queue_failed|prompt identity/i);
  });

  it("rejects an unqualified replacement envelope before queueing", async () => {
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: {
            evil: {
              class_type: "Foreign.Node",
              inputs: { private_media: "file:///secret" },
            },
          },
          workflow: {},
          private_payload: "stale",
        }),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn() },
    );
    await expect(
      createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      }).start(
        {
          task_mode: "t2va",
          user_intent: "Reject foreign compiled output.",
          duration_milliseconds: 5167,
          frame_count: 124,
        },
        { replaceExisting: true },
      ),
    ).rejects.toMatchObject({ code: "compile_failed" });
    expect(api.queuePrompt).not.toHaveBeenCalled();
    expect(app.loadGraphData).not.toHaveBeenCalled();
  });

  /**
   * Canvas writes are declared, so a host cannot cancel a run for doing them.
   *
   * A real host announces every graph configure to its extensions, and this
   * package's own configure hook abandons the run when it hears one -- correct
   * for a user switching workflows, fatal for the write M17-20 D11 makes the
   * run perform itself. A stubbed `loadGraphData` fires no hook, so what is
   * asserted here is the declaration at each write; the entry rows assert the
   * hook honours it.
   */
  it("declares the materialization write as this run's own", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "Declare the canvas write.",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    let depth = 0;
    const observed: number[] = [];
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(() => {
          observed.push(depth);
        }),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      {
        queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(9)),
      },
    );
    await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
      beginOwnedGraphConfigure: () => {
        depth += 1;
        return () => {
          depth -= 1;
        };
      },
    }).start(inputs, { replaceExisting: true });

    expect(observed).toEqual([1]);
    // The declaration ends with the write. A configure arriving later is the
    // user's, and the run has to be abandoned for it.
    expect(depth).toBe(0);
  });

  it("declares the rollback write as its own as well", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "Unwind under a declared write.",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    let depth = 0;
    const observed: number[] = [];
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(() => {
          observed.push(depth);
        }),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn() },
    );
    await expect(
      createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
        beginOwnedGraphConfigure: () => {
          depth += 1;
          return () => {
            depth -= 1;
          };
        },
      }).start(inputs, {
        replaceExisting: true,
        prepareManaged: async (prepared) => ({
          expectedIdentity: {
            graphFingerprint: prepared.observation.graph_fingerprint,
            compiledPromptFingerprint:
              prepared.observation.compiled_prompt_fingerprint,
          },
          bindCanvasIdentity: () => {
            throw new Error("post-write identity bind refused");
          },
          onQueueSubmitted: () => undefined,
          onQueueAccepted: () => undefined,
          onQueueFailed: () => undefined,
        }),
      }),
    ).rejects.toMatchObject({ code: "compile_failed" });

    // Commit, then restore: an undeclared rollback would cancel the very
    // run that is already failing, and report the wrong reason for it.
    expect(observed).toEqual([1, 1]);
    expect(depth).toBe(0);
  });

  it("removes only the frontend zero-duration widget sentinel", () => {
    const compiled = {
      output: {
        "1": {
          class_type: "comfyui_h3_context.H3Context.Request",
          inputs: {
            task_mode: "t2va",
            user_intent: "safe",
            duration_milliseconds: 5167,
            frame_count: 124,
            duration_seconds: 0,
          },
        },
      },
      workflow: {},
    };
    const normalized = normalizeAppModeCompiledPrompt(compiled);
    expect(normalized.output["1"]).toEqual({
      class_type: "comfyui_h3_context.H3Context.Request",
      inputs: {
        task_mode: "t2va",
        user_intent: "safe",
        duration_milliseconds: 5167,
        frame_count: 124,
      },
    });
  });

  it("refuses to mutate a dirty or foreign graph", async () => {
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: {
          serialize: () => ({ nodes: [{ id: 1, type: "Foreign.Node" }] }),
        },
      },
      { queuePrompt: vi.fn() },
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });
    await expect(
      controller.start({
        task_mode: "t2va",
        user_intent: "safe",
        duration_milliseconds: 5167,
        frame_count: 124,
      }),
    ).rejects.toThrow(/dirty|foreign/i);
    expect(loadTemplate).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("does not overwrite a qualified-looking graph without explicit replacement", async () => {
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: {
          serialize: () => ({
            nodes: [
              { id: 1, type: "comfyui_h3_context.H3Context.Request" },
              { id: 2, type: "comfyui_h3_context.H3Context.Plan" },
              { id: 8, type: "comfyui_h3_context.H3Context.ProductShell" },
              { id: 6, type: "MiniMaxH3ImageToVideo" },
            ],
          }),
        },
      },
      { queuePrompt: vi.fn() },
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "safe",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    await expect(controller.start(inputs)).rejects.toThrow(
      /explicit|overwrite/i,
    );
    expect(loadTemplate).not.toHaveBeenCalled();
    await expect(
      controller.start(inputs, { replaceExisting: true }),
    ).rejects.toThrow(/compile|invalid|incompatible/i);
    expect(loadTemplate).toHaveBeenCalledOnce();
  });

  it("applies visible Sidebar authoring and refuses a split existing-graph authority", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "A synthetic eight-second existing-canvas request.",
      duration_milliseconds: 8000,
      frame_count: 192,
    };
    const originalGraph = {
      nodes: [
        { id: 40, type: "VAELoader" },
        { id: 41, type: "VAEDecode" },
        { id: 42, type: "CreateVideo" },
        { id: 43, type: "SaveVideo" },
        { id: 44, type: "CLIPLoader" },
        {
          id: 45,
          type: "PrimitiveFloat",
          outputs: [{ links: [18, 19], type: "FLOAT" }],
          widgets_values: [5.167],
        },
        {
          id: 46,
          type: "ComfyMathExpression",
          inputs: [{ name: "values.a", link: 19, type: "FLOAT" }],
          outputs: [
            { links: [], type: "FLOAT" },
            { links: [20], type: "INT" },
          ],
          widgets_values: [OFFICIAL_LENGTH_EXPRESSION],
        },
        {
          id: 1,
          type: "comfyui_h3_context.H3Context.Request",
          inputs: [
            { name: "task_mode", link: null, type: "COMBO" },
            { name: "user_intent", link: null, type: "STRING" },
            { name: "duration_seconds", link: 18, type: "FLOAT" },
          ],
          outputs: [{ links: [1], type: "H3_CONTEXT_REQUEST" }],
          widgets_values: [
            "t2va",
            "The canvas still carries an older intent.",
            5.167,
          ],
        },
        {
          id: 2,
          type: "comfyui_h3_context.H3Context.Plan",
          inputs: [{ link: 1, type: "H3_CONTEXT_REQUEST" }],
          outputs: [{ links: [2, 3], type: "H3_CONTEXT_PLAN" }],
        },
        {
          id: 3,
          type: "comfyui_h3_context.H3Context.Compiler",
          inputs: [{ link: 2, type: "H3_CONTEXT_PLAN" }],
          outputs: [
            { links: [], type: "H3_PROMPT_STRING" },
            { links: [], type: "H3_CONTEXT_REPORT" },
            { links: [6], type: "H3_PROMPT_DOCUMENT" },
          ],
        },
        {
          id: 4,
          type: "comfyui_h3_context.H3Context.Validator",
          inputs: [
            { link: 3, type: "H3_CONTEXT_PLAN" },
            { link: 6, type: "H3_PROMPT_DOCUMENT" },
          ],
          outputs: [
            { links: [], type: "H3_VALIDATION_RESULT" },
            { links: [4, 13, 15], type: "H3_CONTEXT_REPORT" },
          ],
        },
        {
          id: 5,
          type: "comfyui_h3_context.H3Context.NativeH3Adapter",
          inputs: [{ link: 4, type: "H3_CONTEXT_REPORT" }],
          outputs: [
            { links: [], type: "H3_PROMPT_STRING" },
            { links: [16], type: "H3_NATIVE_H3_WIRING" },
          ],
        },
        {
          id: 8,
          type: "comfyui_h3_context.H3Context.ProductShell",
          inputs: [
            { link: 15, type: "H3_CONTEXT_REPORT" },
            { link: 16, type: "H3_NATIVE_H3_WIRING" },
          ],
          outputs: [
            { links: [17], type: "STRING" },
            { links: [], type: "H3_PRODUCT_SHELL" },
          ],
        },
        {
          id: 7,
          type: "comfyui_h3_context.H3Context.Preview",
          inputs: [{ link: 13, type: "H3_CONTEXT_REPORT" }],
          outputs: [],
        },
        {
          id: 6,
          type: "MiniMaxH3ImageToVideo",
          inputs: [
            { name: "prompt", link: 17, type: "STRING" },
            { link: null, type: "INT" },
            { link: null, type: "INT" },
            { name: "length", link: 20, type: "INT" },
          ],
          outputs: [],
          widgets_values: [512, 512, 124],
        },
      ],
      links: [
        [1, 1, 0, 2, 0, "H3_CONTEXT_REQUEST"],
        [2, 2, 0, 3, 0, "H3_CONTEXT_PLAN"],
        [3, 2, 0, 4, 0, "H3_CONTEXT_PLAN"],
        [4, 4, 1, 5, 0, "H3_CONTEXT_REPORT"],
        [6, 3, 2, 4, 1, "H3_PROMPT_DOCUMENT"],
        [13, 4, 1, 7, 0, "H3_CONTEXT_REPORT"],
        [15, 4, 1, 8, 0, "H3_CONTEXT_REPORT"],
        [16, 5, 1, 8, 1, "H3_NATIVE_H3_WIRING"],
        [17, 8, 0, 6, 0, "STRING"],
        [18, 45, 0, 1, 2, "FLOAT"],
        [19, 45, 0, 46, 0, "FLOAT"],
        [20, 46, 1, 6, 3, "INT"],
      ],
    };
    let visibleGraph = structuredClone(originalGraph);
    const loadGraphData = vi.fn((next: unknown) => {
      visibleGraph = structuredClone(next) as typeof originalGraph;
    });
    const compiled = {
      output: createMaterializedPrompt(inputs),
      workflow: {},
    };
    const graph = liveAuthoringGraph(() => visibleGraph as Json);
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(compiled),
        graph,
      },
      { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(10)) },
    );

    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, { useExisting: true });

    expect(result.route).toBe("existing");
    expect(loadTemplate).not.toHaveBeenCalled();
    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(app.graphToPrompt).toHaveBeenCalledOnce();
    expect(api.queuePrompt).toHaveBeenCalledOnce();
    const request = visibleGraph.nodes.find((node) => node.id === 1)!;
    const duration = visibleGraph.nodes.find((node) => node.id === 45)!;
    expect(request.widgets_values).toEqual(["t2va", inputs.user_intent, 5.167]);
    expect(duration.widgets_values).toEqual([8]);
    const normalized = structuredClone(visibleGraph);
    normalized.nodes.find((node) => node.id === 1)!.widgets_values = [
      "t2va",
      "The canvas still carries an older intent.",
      5.167,
    ];
    normalized.nodes.find((node) => node.id === 45)!.widgets_values = [5.167];
    expect(normalized).toEqual(originalGraph);

    const splitGraph = structuredClone(originalGraph);
    const sharedSource = splitGraph.nodes.find((node) => node.id === 45)!;
    sharedSource.outputs = [{ links: [19], type: "FLOAT" }];
    splitGraph.nodes.push({
      id: 47,
      type: "PrimitiveFloat",
      outputs: [{ links: [18], type: "FLOAT" }],
      widgets_values: [5.167],
    });
    splitGraph.links.find((link) => link[0] === 18)![1] = 47;
    const splitLoad = vi.fn();
    const splitQueue = vi.fn();
    const splitHost = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: splitLoad,
        graphToPrompt: vi.fn(),
        graph: { serialize: () => splitGraph },
      },
      { queuePrompt: splitQueue },
    );

    await expect(
      createAppModeController(splitHost.app, splitHost.api, {
        loadTemplate,
        loadProfile,
      }).start(inputs, { useExisting: true }),
    ).rejects.toMatchObject({ code: "incompatible_graph" });
    expect(splitLoad).not.toHaveBeenCalled();
    expect(splitHost.app.graphToPrompt).not.toHaveBeenCalled();
    expect(splitQueue).not.toHaveBeenCalled();
    expect(splitHost.app.graph.serialize?.()).toEqual(splitGraph);
  });

  it("admits a canvas without pre-enumerating an artifact sink", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent:
        "A red kite crosses the sky while the camera follows its arc.",
      duration_milliseconds: 5000,
      frame_count: 124,
    };
    const routeFixture = existingGraphRouteFixture(inputs, {
      artifactSink: false,
    });
    let visibleGraph = structuredClone(routeFixture.workflow);
    const request = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    const duration = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    request.widgets_values = ["t2va", "An older visible intent.", 5.167];
    duration.widgets_values = [5.167];
    const originalGraph = structuredClone(visibleGraph);
    const loadGraphData = vi.fn((next: unknown) => {
      visibleGraph = structuredClone(next) as Json;
    });
    const graph = liveAuthoringGraph(() => visibleGraph);
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(routeFixture.compiled),
        graph,
      },
      {
        queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(11)),
      },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        {
          useExisting: true,
        },
      ),
    ).resolves.toMatchObject({ queuePromptId: syntheticPromptUuid(11) });
    expect(loadTemplate).not.toHaveBeenCalled();
    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(graph.receipt.change).not.toHaveBeenCalled();
    expect(visibleGraph).not.toEqual(originalGraph);
    expect(api.queuePrompt).toHaveBeenCalledOnce();
  });

  it("keeps one active workflow and tab while admitting a sink-free candidate", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "Keep this exact workflow tab while validating the graph.",
      duration_milliseconds: 5000,
      frame_count: 124,
    };
    const routeFixture = existingGraphRouteFixture(inputs, {
      artifactSink: false,
    });
    let visibleGraph = structuredClone(routeFixture.workflow);
    const request = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    request.widgets_values = ["t2va", "An older tab-bound intent.", 5.167];
    const duration = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    duration.widgets_values = [5.167];
    const originalGraph = structuredClone(visibleGraph);
    const activeWorkflow: object = { path: "workflows/active.json" };
    const workflowStore = {
      activeWorkflow,
      openWorkflows: [activeWorkflow] as object[],
    };
    // Model the supported ComfyUI workflow service, not only its graph write:
    // null/default workflow authority creates a fresh temporary workflow tab.
    const loadGraphData = vi.fn(
      (
        next: unknown,
        _clean = true,
        _restoreView = true,
        workflow: object | null = null,
      ) => {
        visibleGraph = structuredClone(next) as Json;
        if (workflow === null) {
          const temporary = {
            path: `workflows/temporary-${workflowStore.openWorkflows.length}.json`,
          };
          workflowStore.openWorkflows.push(temporary);
          workflowStore.activeWorkflow = temporary;
          return;
        }
        workflowStore.activeWorkflow = workflow;
        if (!workflowStore.openWorkflows.includes(workflow))
          workflowStore.openWorkflows.push(workflow);
      },
    );
    const graph = liveAuthoringGraph(() => visibleGraph);
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(routeFixture.compiled),
        graph,
      },
      { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(12)) },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        { useExisting: true },
      ),
    ).resolves.toMatchObject({ queuePromptId: syntheticPromptUuid(12) });

    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(graph.receipt.change).not.toHaveBeenCalled();
    expect(visibleGraph).not.toEqual(originalGraph);
    expect(workflowStore.activeWorkflow).toBe(activeWorkflow);
    expect(workflowStore.openWorkflows).toEqual([activeWorkflow]);
    expect(api.queuePrompt).toHaveBeenCalledOnce();
  });

  it("binds composed Subgraph node identities and local link bindings", () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent:
        "A red kite crosses the sky while the camera follows its arc.",
      duration_milliseconds: 5000,
      frame_count: 124,
    };
    const basePrompt = createQualifiedBasePrompt(
      inputs,
      {},
      {
        artifactSink: false,
      },
    );
    const output = composePromptOutput(basePrompt);
    expect(
      compiledPromptMatchesVisibleGraph(baseAssistant, {
        output,
        workflow: {},
      }),
    ).toBe(true);
  });

  it("rejects a stale graph-to-prompt widget projection before queueing", async () => {
    const visibleInputs = {
      task_mode: "t2va" as const,
      user_intent:
        "A red kite crosses the sky while the camera follows its arc.",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    const routeFixture = existingGraphRouteFixture({
      ...visibleInputs,
      user_intent: "an older intent returned by a stale compiler",
    });
    let visibleGraph = structuredClone(
      existingGraphRouteFixture(visibleInputs).workflow,
    );
    const loadGraphData = vi.fn((next: unknown) => {
      visibleGraph = structuredClone(next) as Json;
    });
    const graph = liveAuthoringGraph(() => visibleGraph);
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(routeFixture.compiled),
        graph,
      },
      {
        queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(13)),
      },
    );

    await expect(
      createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      }).start(visibleInputs, {
        useExisting: true,
      }),
    ).rejects.toMatchObject({ code: "incompatible_graph" });
    expect(api.queuePrompt).not.toHaveBeenCalled();
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(graph.receipt.change).not.toHaveBeenCalled();
  });

  it("rejects a strict compiled prompt whose node identity is foreign to the visible graph", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "safe",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    const compiled = {
      output: createQualifiedBasePrompt(inputs, {}, { artifactSink: false }),
      workflow: {},
    };
    const output = compiled.output as Record<string, unknown>;
    const foreignOutput: Record<string, unknown> = {
      ...output,
      "99": output["6"],
    };
    delete foreignOutput["6"];
    const visibleGraph = {
      nodes: [
        {
          id: 1,
          type: requestNodeType,
          widgets_values: [
            "t2va",
            inputs.user_intent,
            inputs.duration_milliseconds / 1000,
          ],
        },
        { id: 2, type: planNodeType },
        { id: 3, type: compilerNodeType },
        { id: 4, type: validatorNodeType },
        { id: 5, type: nativeAdapterNodeType },
        {
          id: 6,
          type: imageGenerationNodeType,
          widgets_values: [512, 512, 124],
        },
        { id: 7, type: previewNodeType },
        { id: 8, type: productShellNodeType },
      ],
    };
    expect(compiledPromptMatchesVisibleGraph(visibleGraph, compiled)).toBe(
      true,
    );
    const unboundVisibleGraph = {
      nodes: visibleGraph.nodes.map((node) => {
        const copy = { ...node };
        delete copy.widgets_values;
        return copy;
      }),
    };
    expect(
      compiledPromptMatchesVisibleGraph(unboundVisibleGraph, compiled),
    ).toBe(false);
    expect(
      compiledPromptMatchesVisibleGraph(visibleGraph, {
        output: foreignOutput,
        workflow: {},
      }),
    ).toBe(false);
    const widgetVisibleGraph = {
      nodes: visibleGraph.nodes.map((node) =>
        node.type === requestNodeType
          ? {
              ...node,
              widgets_values: ["t2va", "visible intent", 5.167],
            }
          : node,
      ),
    };
    const staleCompiled = {
      output: createQualifiedBasePrompt({
        task_mode: "t2va",
        user_intent: "stale intent",
        duration_milliseconds: 5167,
        frame_count: 124,
      }),
      workflow: {},
    };
    expect(
      compiledPromptMatchesVisibleGraph(widgetVisibleGraph, staleCompiled),
    ).toBe(false);
    const shortWidgetGraph = {
      nodes: visibleGraph.nodes.map((node) =>
        node.type === requestNodeType
          ? { ...node, widgets_values: ["t2va", "safe"] }
          : node,
      ),
    };
    expect(compiledPromptMatchesVisibleGraph(shortWidgetGraph, compiled)).toBe(
      false,
    );
    const operatorAnchorWidgetGraph = {
      nodes: visibleGraph.nodes.map((node) =>
        node.type === imageGenerationNodeType
          ? { ...node, widgets_values: [2048, 128, -1, "operator-owned"] }
          : node,
      ),
    };
    expect(
      compiledPromptMatchesVisibleGraph(operatorAnchorWidgetGraph, compiled),
    ).toBe(true);
  });

  it("rejects a ProductShell-only graph before queueing it", async () => {
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: {
            "8": {
              class_type: "comfyui_h3_context.H3Context.ProductShell",
              inputs: {},
            },
          },
          workflow: {},
        }),
        graph: {
          serialize: () => ({
            nodes: [
              { id: 8, type: "comfyui_h3_context.H3Context.ProductShell" },
            ],
          }),
        },
      },
      { queuePrompt: vi.fn() },
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });
    await expect(
      controller.start(
        {
          task_mode: "t2va",
          user_intent: "",
          duration_milliseconds: 5167,
          frame_count: 124,
        },
        { useExisting: true },
      ),
    ).rejects.toThrow(/incompatible_graph/);
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("rejects an incomplete visible graph even when compilation returns a canonical prompt", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "safe",
      duration_milliseconds: 5167,
      frame_count: 124,
    };
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: {
          serialize: () => ({
            nodes: [
              { id: 8, type: "comfyui_h3_context.H3Context.ProductShell" },
            ],
          }),
        },
      },
      { queuePrompt: vi.fn() },
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });

    await expect(
      controller.start(inputs, { useExisting: true }),
    ).rejects.toThrow(/incompatible_graph/);
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("admits the template's own nodes but not a dangling link", () => {
    const output = createQualifiedBasePrompt({
      task_mode: "t2va",
      user_intent: "safe",
      duration_milliseconds: 5167,
      frame_count: 124,
    }) as Record<string, Record<string, unknown>>;
    // M17-20 D11: a materialized graph is the official template plus the H3
    // pipeline, so a node this repository has never heard of is the normal case,
    // not a defect. Enumerating the surrounding graph would fail closed on every
    // correct materialization the moment an official template gained a node.
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          foreign: { class_type: "Foreign.Node", inputs: {} },
        },
        workflow: {},
      }),
    ).toBe(true);
    // What it may not do is depend on a node that is not here: the host reads a
    // bare two-element array as a link and would fail on the missing origin.
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          foreign: {
            class_type: "Foreign.Node",
            inputs: { latent: ["999", 0] },
          },
        },
        workflow: {},
      }),
    ).toBe(false);
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "2": {
            ...output["2"],
            inputs: { request: ["999", 0] },
          },
        },
        workflow: {},
      }),
    ).toBe(false);
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "8": {
            ...output["8"],
            inputs: { report: ["4", 1], native_h3_wiring: ["5", 99] },
          },
        },
        workflow: {},
      }),
    ).toBe(false);
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "7": { ...output["7"], inputs: {} },
        },
        workflow: {},
      }),
    ).toBe(false);
  });

  it("keeps the c44 API envelope closed while allowing _meta.title", () => {
    const output = createQualifiedBasePrompt({
      task_mode: "t2va",
      user_intent: "safe",
      duration_milliseconds: 5167,
      frame_count: 124,
    }) as Record<string, Record<string, unknown>>;
    output["1"] = { ...output["1"], _meta: { title: "Request" } };
    expect(isCompatibleExistingPrompt({ output, workflow: {} })).toBe(true);
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "1": { ...output["1"], private_payload: "unexpected" },
        },
        workflow: {},
      }),
    ).toBe(false);
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "1": { ...output["1"], _meta: { title: "Request", secret: "x" } },
        },
        workflow: {},
      }),
    ).toBe(false);
    const inheritedNode = structuredClone(output);
    const request = inheritedNode["1"]!;
    delete request.class_type;
    Object.setPrototypeOf(request, {
      class_type: "comfyui_h3_context.H3Context.Request",
    });
    expect(
      isCompatibleExistingPrompt({ output: inheritedNode, workflow: {} }),
    ).toBe(false);
  });

  it("ignores native geometry but retains the owned reference sizing contract", () => {
    const output = createQualifiedBasePrompt({
      task_mode: "t2va",
      user_intent: "safe",
      duration_milliseconds: 5167,
      frame_count: 124,
    }) as Record<string, Record<string, unknown>>;
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "6": {
            ...output["6"],
            inputs: {
              ...(output["6"].inputs as Record<string, unknown>),
              width: 31,
            },
          },
        },
        workflow: {},
      }),
    ).toBe(true);
    const reference = createQualifiedReferencePrompt();
    (reference["6"].inputs as Record<string, unknown>).ref_image_size =
      "invalid";
    expect(
      isCompatibleExistingPrompt({ output: reference, workflow: {} }),
    ).toBe(false);
  });

  it("keeps the bounded zero-duration host sentinel bindable", () => {
    const output = createQualifiedBasePrompt({
      task_mode: "t2va",
      user_intent: "safe",
      duration_milliseconds: 5167,
      frame_count: 124,
    }) as Record<string, Record<string, unknown>>;
    output["1"] = {
      ...output["1"],
      inputs: {
        ...(output["1"]?.inputs as Record<string, unknown>),
        duration_seconds: 0,
      },
    };
    expect(isCompatibleExistingPrompt({ output, workflow: {} })).toBe(true);
  });

  it("rejects structured H3 values masquerading as extra compiled links", () => {
    const base = createQualifiedBasePrompt({
      task_mode: "t2va",
      user_intent: "safe",
      duration_milliseconds: 5167,
      frame_count: 124,
    }) as Record<string, Record<string, unknown>>;
    const hardConstraints = structuredClone(base);
    (hardConstraints["1"].inputs as Record<string, unknown>).hard_constraints =
      ["1", 0];
    expect(
      isCompatibleExistingPrompt({ output: hardConstraints, workflow: {} }),
    ).toBe(false);

    const intentGraph = structuredClone(base);
    (intentGraph["2"].inputs as Record<string, unknown>).intent_graph = [
      "1",
      0,
    ];
    expect(
      isCompatibleExistingPrompt({ output: intentGraph, workflow: {} }),
    ).toBe(false);
  });

  it("accepts a canonical existing Reference graph with nested image links", () => {
    const output = createQualifiedReferencePrompt();
    expect(isCompatibleExistingPrompt({ output, workflow: {} })).toBe(true);
    const oneImage = createQualifiedReferencePrompt();
    (oneImage["6"].inputs as Record<string, unknown>).ref_images = [["10", 0]];
    (oneImage["9"].inputs as Record<string, unknown>).images = [["10", 0]];
    expect(isCompatibleExistingPrompt({ output: oneImage, workflow: {} })).toBe(
      true,
    );
    const duplicateImage = createQualifiedReferencePrompt();
    (duplicateImage["6"].inputs as Record<string, unknown>).ref_images = [
      ["10", 0],
      ["10", 0],
    ];
    (duplicateImage["9"].inputs as Record<string, unknown>).images = [
      ["10", 0],
      ["10", 0],
    ];
    expect(
      isCompatibleExistingPrompt({ output: duplicateImage, workflow: {} }),
    ).toBe(false);
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "6": {
            ...output["6"],
            inputs: {
              ...(output["6"].inputs as Record<string, unknown>),
              ref_images: [["10", 99]],
            },
          },
        },
        workflow: {},
      }),
    ).toBe(false);
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "9": {
            ...output["9"],
            inputs: { images: [["foreign", 0]] },
          },
        },
        workflow: {},
      }),
    ).toBe(false);
  });

  it("accepts a qualified external Reference boundary without LoadImage nodes", () => {
    const output = createQualifiedExternalReferencePrompt();
    expect(
      isCompatibleExistingPrompt({
        output,
        workflow: referenceAssistant,
      }),
    ).toBe(true);
    expect(
      isCompatibleExistingPrompt({
        output,
        workflow: {},
      }),
    ).toBe(false);
    expect(
      isCompatibleExistingPrompt({
        output: {
          ...output,
          "12": {
            class_type: "comfyui_h3_context.H3Context.ProductShell",
            inputs: {},
          },
        },
        workflow: referenceAssistant,
      }),
    ).toBe(false);
    const missingExternalImage = structuredClone(output);
    (missingExternalImage["6"].inputs as Record<string, unknown>).ref_images = [
      ["-10", 3],
    ];
    expect(
      isCompatibleExistingPrompt({
        output: missingExternalImage,
        workflow: referenceAssistant,
      }),
    ).toBe(false);
    const missingExternalVideo = structuredClone(output);
    delete (missingExternalVideo["9"].inputs as Record<string, unknown>).videos;
    expect(
      isCompatibleExistingPrompt({
        output: missingExternalVideo,
        workflow: referenceAssistant,
      }),
    ).toBe(false);
    const alteredWorkflow = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const definition = alteredWorkflow.definitions.subgraphs[0];
    const nodes = definition?.nodes as Array<Record<string, unknown>>;
    if (nodes === undefined) throw new Error("fixture drift");
    const generation = nodes.find(
      (node) => node.type === "MiniMaxH3ReferenceToVideo",
    );
    if (generation === undefined) throw new Error("fixture drift");
    generation.type = "Foreign.ReferenceToVideo";
    expect(
      isCompatibleExistingPrompt({ output, workflow: alteredWorkflow }),
    ).toBe(false);
    const unreferencedWorkflow = structuredClone(referenceAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    unreferencedWorkflow.nodes[0]!.type = "wrapper-only";
    expect(
      isCompatibleExistingPrompt({ output, workflow: unreferencedWorkflow }),
    ).toBe(false);
  });

  it("cancels detached compilation without writing the candidate", async () => {
    const initialGraph = { nodes: [] };
    let resolveCompile: ((value: AppModeCompiledPrompt) => void) | undefined;
    const compiled = {
      output: createQualifiedBasePrompt({
        task_mode: "t2va" as const,
        user_intent: "safe",
        duration_milliseconds: 5167,
        frame_count: 124,
      }),
      workflow: {},
    };
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(
          () =>
            new Promise<AppModeCompiledPrompt>((resolve) => {
              resolveCompile = resolve;
            }),
        ),
        graph: { serialize: () => initialGraph },
      },
      { queuePrompt: vi.fn() },
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });
    const abort = new AbortController();
    const pending = controller.start(
      {
        task_mode: "t2va",
        user_intent: "safe",
        duration_milliseconds: 5167,
        frame_count: 124,
      },
      { signal: abort.signal },
    );
    await vi.waitFor(() => expect(app.graphToPrompt).toHaveBeenCalledOnce());
    expect(app.loadGraphData).not.toHaveBeenCalled();
    abort.abort();
    resolveCompile?.(compiled);
    await expect(pending).rejects.toMatchObject({ code: "cancelled" });
    expect(api.queuePrompt).not.toHaveBeenCalled();
    expect(app.loadGraphData).not.toHaveBeenCalled();
  });

  it("leaves the canvas untouched when cancelled before materialization", async () => {
    // Cancelling while the template is still being read must not load anything,
    // and therefore must not need a rollback either.
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn() },
    );
    const abort = new AbortController();
    const slowTemplate = vi.fn(
      (name: string) =>
        new Promise((resolve) => {
          abort.abort();
          resolve(loadSyntheticTemplate(name));
        }),
    );
    await expect(
      createAppModeController(app, api, {
        loadTemplate: slowTemplate,
        loadProfile,
      }).start(
        {
          task_mode: "t2va",
          user_intent: "safe",
          duration_milliseconds: 5167,
          frame_count: 124,
        },
        { signal: abort.signal },
      ),
    ).rejects.toMatchObject({ code: "cancelled" });
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });
});

describe("M23-15 managed queue boundary", () => {
  it.each([
    [
      "a classified host execution terminal",
      new AppModeError(
        "execution_failed",
        "the accepted prompt failed during host execution",
      ),
      "execution_failed",
      undefined,
    ],
    [
      "a classified host interruption terminal",
      new AppModeError(
        "execution_interrupted",
        "the accepted prompt was interrupted during host execution",
      ),
      "execution_interrupted",
      undefined,
    ],
    [
      "an unrelated typed callback failure",
      new AppModeError(
        "compile_failed",
        "synthetic post-acceptance callback failure",
      ),
      "ambiguous_host_ownership",
      "ambiguous",
    ],
    [
      "an unclassified managed callback failure",
      new Error("synthetic coordinator boundary failure"),
      "ambiguous_host_ownership",
      "ambiguous",
    ],
  ] as const)(
    "preserves %s after queue acceptance",
    async (
      _label,
      acceptedFailure,
      expectedCode,
      expectedFailureDisposition,
    ) => {
      const inputs = {
        task_mode: "t2va" as const,
        user_intent:
          "Classify the accepted prompt without losing host ownership.",
        duration_milliseconds: 8000,
        frame_count: 192,
      };
      const fixture = existingGraphRouteFixture(inputs);
      let visibleGraph = structuredClone(fixture.workflow);
      const graph = liveAuthoringGraph(() => visibleGraph);
      const queuePrompt = vi.fn().mockResolvedValue(acceptedQueueResponse(101));
      const onQueueFailed = vi.fn();
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData: vi.fn((next: unknown) => {
            visibleGraph = structuredClone(next) as Json;
          }),
          graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
          graph,
        },
        { queuePrompt },
      );

      await expect(
        createAppModeController(app, api, { loadTemplate, loadProfile }).start(
          inputs,
          {
            useExisting: true,
            prepareManaged: async (prepared) => ({
              expectedIdentity: {
                graphFingerprint: prepared.observation.graph_fingerprint,
                compiledPromptFingerprint:
                  prepared.observation.compiled_prompt_fingerprint,
              },
              onQueueSubmitted: () => undefined,
              onQueueAccepted: () => {
                throw acceptedFailure;
              },
              onQueueFailed,
            }),
          },
        ),
      ).rejects.toMatchObject({ code: expectedCode });
      expect(queuePrompt).toHaveBeenCalledOnce();
      if (expectedFailureDisposition === undefined)
        expect(onQueueFailed).not.toHaveBeenCalled();
      else
        expect(onQueueFailed).toHaveBeenCalledExactlyOnceWith(
          expectedFailureDisposition,
        );
    },
  );

  it("skips source-identity qualification for an existing I2VA route", async () => {
    const inputs = {
      task_mode: "i2va" as const,
      user_intent: "Preserve the portrait source aspect for one managed run.",
      duration_milliseconds: 8000,
      frame_count: 192,
      first_frame_source: "9",
    };
    const fixture = existingGraphRouteFixture(inputs);
    let visibleGraph = structuredClone(fixture.workflow);
    const originalGraph = structuredClone(visibleGraph);
    const order: string[] = [];
    const observeSourceIdentity = vi.fn(async () => {
      order.push("observe");
      return {
        schema: "h3.context.input_geometry.receipt.v2" as const,
        receipt_handle: "ig_" + "r".repeat(40),
        source_fingerprint: "sha256:" + "b".repeat(64),
      };
    });
    const prepareManaged = vi.fn(async (prepared) => {
      order.push("prepare");
      expect(prepared.observation).toMatchObject({
        expected_frames: 192,
        source_identity: null,
      });
      return {
        expectedIdentity: {
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
        },
        onQueueSubmitted: () => undefined,
        onQueueAccepted: () => undefined,
        onQueueFailed: () => undefined,
      };
    });
    const queuePrompt = vi.fn(async () => {
      order.push("queue");
      return acceptedQueueResponse(102);
    });
    const graph = liveAuthoringGraph(() => visibleGraph);
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn((next: unknown) => {
          order.push("write");
          visibleGraph = structuredClone(next) as Json;
        }),
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );

    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, {
      useExisting: true,
      observeSourceIdentity,
      prepareManaged,
    });

    expect(result.queuePromptId).toBe(syntheticPromptUuid(102));
    expect(order).toEqual(["prepare", "write", "queue"]);
    expect(app.loadGraphData).toHaveBeenCalledOnce();
    expect(visibleGraph).toEqual(originalGraph);
    expect(
      (visibleGraph.nodes as Json[]).find(
        (node) => node.type === H3_NODE_TYPES.request,
      )?.widgets_values,
    ).toEqual(["i2va", inputs.user_intent, 8]);
    expect(observeSourceIdentity).not.toHaveBeenCalled();
    expect(loadProfile).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "preserves arbitrary canvas inference controls through the current-I2VA queue when Turbo is %s",
    async (turboEnabled) => {
      const inputs = {
        task_mode: "i2va" as const,
        user_intent: "Queue the user's synthetic inference controls unchanged.",
        duration_milliseconds: 8000,
        frame_count: 192,
        first_frame_source: "9",
      };
      const fixture = existingGraphRouteFixture(inputs);
      let visibleGraph = structuredClone(fixture.workflow);
      const compiled = structuredClone(
        fixture.compiled,
      ) as AppModeCompiledPrompt;
      const baseSteps = 37;
      const turboSteps = 11;
      const modelName = "synthetic-user-video-model.safetensors";
      const loraName = "synthetic-user-turbo-lora.safetensors";
      const loraStrength = 0.37;
      const scheduler = "synthetic_user_scheduler";
      const samplerName = "synthetic_user_sampler";
      const denoise = 0.73;
      const cfg = 4.625;
      const noiseSeed = 9_007_199_254_740_991;
      const inferenceNodeIds = new Set(
        Array.from({ length: 11 }, (_value, index) => 900 + index),
      );
      const inferenceLinkIds = new Set(
        Array.from({ length: 10 }, (_value, index) => 9000 + index),
      );
      const inferenceNodes: Json[] = [
        {
          id: 900,
          type: "UNETLoader",
          inputs: [],
          outputs: [{ name: "MODEL", type: "MODEL", links: [9000, 9001] }],
          widgets_values: [modelName, "default"],
          widgets_values_named: {
            unet_name: modelName,
            weight_dtype: "default",
          },
        },
        {
          id: 901,
          type: "LoraLoaderModelOnly",
          inputs: [{ name: "model", type: "MODEL", link: 9000 }],
          outputs: [{ name: "MODEL", type: "MODEL", links: [9002] }],
          widgets_values: [loraName, loraStrength],
          widgets_values_named: {
            lora_name: loraName,
            strength_model: loraStrength,
          },
        },
        {
          id: 902,
          type: "PrimitiveBoolean",
          inputs: [],
          outputs: [{ name: "BOOLEAN", type: "BOOLEAN", links: [9003, 9004] }],
          widgets_values: [turboEnabled],
          widgets_values_named: { value: turboEnabled },
        },
        {
          id: 903,
          type: "ComfySwitchNode",
          inputs: [
            { name: "on_false", type: "MODEL", link: 9001 },
            { name: "on_true", type: "MODEL", link: 9002 },
            { name: "switch", type: "BOOLEAN", link: 9003 },
          ],
          outputs: [{ name: "output", type: "MODEL", links: [9005, 9006] }],
          widgets_values: [turboEnabled],
        },
        {
          id: 904,
          type: "PrimitiveInt",
          inputs: [],
          outputs: [{ name: "INT", type: "INT", links: [9007] }],
          widgets_values: [baseSteps, "fixed"],
          widgets_values_named: { value: baseSteps, fixed: "fixed" },
        },
        {
          id: 905,
          type: "PrimitiveInt",
          inputs: [],
          outputs: [{ name: "INT", type: "INT", links: [9008] }],
          widgets_values: [turboSteps, "fixed"],
          widgets_values_named: { value: turboSteps, fixed: "fixed" },
        },
        {
          id: 906,
          type: "ComfySwitchNode",
          inputs: [
            { name: "on_false", type: "INT", link: 9007 },
            { name: "on_true", type: "INT", link: 9008 },
            { name: "switch", type: "BOOLEAN", link: 9004 },
          ],
          outputs: [{ name: "output", type: "INT", links: [9009] }],
          widgets_values: [turboEnabled],
        },
        {
          id: 907,
          type: "BasicScheduler",
          inputs: [
            { name: "model", type: "MODEL", link: 9006 },
            { name: "steps", type: "INT", link: 9009 },
          ],
          outputs: [{ name: "SIGMAS", type: "SIGMAS", links: [] }],
          widgets_values: [scheduler, 99, denoise],
          widgets_values_named: { scheduler, steps: 99, denoise },
        },
        {
          id: 908,
          type: "KSamplerSelect",
          inputs: [],
          outputs: [{ name: "SAMPLER", type: "SAMPLER", links: [] }],
          widgets_values: [samplerName],
          widgets_values_named: { sampler_name: samplerName },
        },
        {
          id: 909,
          type: "RandomNoise",
          inputs: [],
          outputs: [{ name: "NOISE", type: "NOISE", links: [] }],
          widgets_values: [noiseSeed, "fixed"],
          widgets_values_named: {
            noise_seed: noiseSeed,
            control_after_generate: "fixed",
          },
        },
        {
          id: 910,
          type: "CFGGuider",
          inputs: [{ name: "model", type: "MODEL", link: 9005 }],
          outputs: [{ name: "GUIDER", type: "GUIDER", links: [] }],
          widgets_values: [cfg],
          widgets_values_named: { cfg },
        },
      ];
      const inferenceLinks: unknown[][] = [
        [9000, 900, 0, 901, 0, "MODEL"],
        [9001, 900, 0, 903, 0, "MODEL"],
        [9002, 901, 0, 903, 1, "MODEL"],
        [9003, 902, 0, 903, 2, "BOOLEAN"],
        [9004, 902, 0, 906, 2, "BOOLEAN"],
        [9005, 903, 0, 910, 0, "MODEL"],
        [9006, 903, 0, 907, 0, "MODEL"],
        [9007, 904, 0, 906, 0, "INT"],
        [9008, 905, 0, 906, 1, "INT"],
        [9009, 906, 0, 907, 1, "INT"],
      ];
      (visibleGraph.nodes as Json[]).push(...inferenceNodes);
      (visibleGraph.links as unknown[][]).push(...inferenceLinks);
      visibleGraph.last_node_id = 910;
      visibleGraph.last_link_id = 9009;

      const output = compiled.output as Record<string, Record<string, unknown>>;
      Object.assign(output, {
        "900": {
          class_type: "UNETLoader",
          inputs: { unet_name: modelName, weight_dtype: "default" },
        },
        "901": {
          class_type: "LoraLoaderModelOnly",
          inputs: {
            model: ["900", 0],
            lora_name: loraName,
            strength_model: loraStrength,
          },
        },
        "902": {
          class_type: "PrimitiveBoolean",
          inputs: { value: turboEnabled },
        },
        "903": {
          class_type: "ComfySwitchNode",
          inputs: {
            on_false: ["900", 0],
            on_true: ["901", 0],
            switch: ["902", 0],
          },
        },
        "904": { class_type: "PrimitiveInt", inputs: { value: baseSteps } },
        "905": { class_type: "PrimitiveInt", inputs: { value: turboSteps } },
        "906": {
          class_type: "ComfySwitchNode",
          inputs: {
            on_false: ["904", 0],
            on_true: ["905", 0],
            switch: ["902", 0],
          },
        },
        "907": {
          class_type: "BasicScheduler",
          inputs: {
            model: ["903", 0],
            scheduler,
            steps: ["906", 0],
            denoise,
          },
        },
        "908": {
          class_type: "KSamplerSelect",
          inputs: { sampler_name: samplerName },
        },
        "909": { class_type: "RandomNoise", inputs: { noise_seed: noiseSeed } },
        "910": {
          class_type: "CFGGuider",
          inputs: { model: ["903", 0], cfg },
        },
      });
      const selectVisibleInference = (workflow: Json) => ({
        nodes: (workflow.nodes as Json[]).filter((node) =>
          inferenceNodeIds.has(Number(node.id)),
        ),
        links: (workflow.links as unknown[][]).filter((link) =>
          inferenceLinkIds.has(Number(link[0])),
        ),
      });
      const selectCompiledInference = (subject: AppModeCompiledPrompt) =>
        Object.fromEntries(
          [...inferenceNodeIds].map((id) => [
            String(id),
            (subject.output as Record<string, unknown>)[String(id)],
          ]),
        );
      const visibleInferenceBefore = structuredClone(
        selectVisibleInference(visibleGraph),
      );
      const compiledInferenceBefore = structuredClone(
        selectCompiledInference(compiled),
      );
      const graph = liveAuthoringGraph(() => visibleGraph);
      const queuePrompt = vi
        .fn()
        .mockResolvedValue(acceptedQueueResponse(turboEnabled ? 105 : 104));
      const { app, api } = fixtureBackedAppModeHost(
        {
          loadApiJson: vi.fn(),
          loadGraphData: vi.fn((next: unknown) => {
            visibleGraph = structuredClone(next) as Json;
          }),
          graphToPrompt: vi.fn().mockResolvedValue(compiled),
          graph,
        },
        { queuePrompt },
      );

      const result = await createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      }).start(inputs, { useExisting: true });

      expect(result.route).toBe("existing");
      expect(loadTemplate).not.toHaveBeenCalled();
      expect(selectVisibleInference(visibleGraph)).toEqual(
        visibleInferenceBefore,
      );
      expect(queuePrompt).toHaveBeenCalledOnce();
      const queued = queuePrompt.mock.calls[0]![1] as AppModeCompiledPrompt;
      expect(selectCompiledInference(queued)).toEqual(compiledInferenceBefore);
      const queuedOutput = queued.output as Record<
        string,
        { inputs: Record<string, unknown> }
      >;
      const selectedStepLink = queuedOutput["906"]!.inputs[
        turboEnabled ? "on_true" : "on_false"
      ] as [string, number];
      expect(queuedOutput[selectedStepLink[0]]!.inputs.value).toBe(
        turboEnabled ? turboSteps : baseSteps,
      );
      expect(queuedOutput["906"]!.inputs.switch).toEqual(
        queuedOutput["903"]!.inputs.switch,
      );
    },
  );

  it("queues an existing I2VA canvas whose native frame passes through a scaler", async () => {
    const inputs = {
      task_mode: "i2va" as const,
      user_intent:
        "Keep the user's scaled first-frame branch and selected weights.",
      duration_milliseconds: 8000,
      frame_count: 192,
      first_frame_source: "9",
    };
    const fixture = existingI2vaScalerRegistryFixture(inputs);
    let visibleGraph = structuredClone(fixture.workflow);
    const originalGraph = structuredClone(visibleGraph);
    const observeSourceIdentity = vi.fn(async () => ({
      schema: "h3.context.input_geometry.receipt.v2" as const,
      receipt_handle: "ig_" + "s".repeat(40),
      source_fingerprint: "sha256:" + "c".repeat(64),
    }));
    const prepareManaged = vi.fn(async (prepared) => ({
      expectedIdentity: {
        graphFingerprint: prepared.observation.graph_fingerprint,
        compiledPromptFingerprint:
          prepared.observation.compiled_prompt_fingerprint,
      },
      onQueueSubmitted: () => undefined,
      onQueueAccepted: () => undefined,
      onQueueFailed: () => undefined,
    }));
    const queuePrompt = vi.fn(async () => acceptedQueueResponse(103));
    const graph = liveAuthoringGraph(() => visibleGraph);
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn((next: unknown) => {
          visibleGraph = structuredClone(next) as Json;
        }),
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );

    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, {
      useExisting: true,
      observeSourceIdentity,
      prepareManaged,
    });

    expect(result.queuePromptId).toBe(syntheticPromptUuid(103));
    expect(queuePrompt).toHaveBeenCalledOnce();
    expect(observeSourceIdentity).not.toHaveBeenCalled();
    // The existing transaction may update only Request/duration; these inputs
    // already match, so equality proves scaler, Registry and weight widgets stay put.
    expect(visibleGraph).toEqual(originalGraph);
  });

  it("keeps existing source locators outside managed qualification", async () => {
    const inputs = {
      task_mode: "i2va" as const,
      user_intent:
        "Author duration without qualifying the user's media branch.",
      duration_milliseconds: 8000,
      frame_count: 192,
      first_frame_source: "9",
    };
    const fixture = existingGraphRouteFixture(inputs);
    let visibleGraph = structuredClone(fixture.workflow);
    const request = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    const duration = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    request.widgets_values = ["i2va", "An older geometry request.", 5.167];
    duration.widgets_values = [5.167];
    const loadGraphData = vi.fn((next: unknown) => {
      visibleGraph = structuredClone(next) as Json;
    });
    const observeSourceIdentity = vi.fn(async () => {
      throw new Error("opaque-media-marker changed");
    });
    const prepareManaged = vi.fn(async (prepared) => {
      expect(prepared.observation.source_identity).toBeNull();
      expect(JSON.stringify(prepared.observation)).not.toContain(
        "template-first.png",
      );
      return {
        expectedIdentity: {
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
        },
        onQueueSubmitted: () => undefined,
        onQueueAccepted: () => undefined,
        onQueueFailed: () => undefined,
      };
    });
    const queuePrompt = vi.fn(async () => acceptedQueueResponse(104));
    const graph = liveAuthoringGraph(() => visibleGraph);
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );

    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs, {
      useExisting: true,
      observeSourceIdentity,
      prepareManaged,
    });

    expect(result.queuePromptId).toBe(syntheticPromptUuid(104));
    expect(observeSourceIdentity).not.toHaveBeenCalled();
    expect(prepareManaged).toHaveBeenCalledOnce();
    expect(queuePrompt).toHaveBeenCalledOnce();
    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(graph.receipt.change).not.toHaveBeenCalled();
    const reboundRequest = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === H3_NODE_TYPES.request,
    )!;
    const reboundDuration = (visibleGraph.nodes as Json[]).find(
      (node) => node.type === "PrimitiveFloat",
    )!;
    expect(reboundRequest.widgets_values).toEqual([
      "i2va",
      inputs.user_intent,
      5.167,
    ]);
    // The linked PrimitiveFloat is authoritative; the replaced Request widget
    // stays an inert fallback and is outside the owned write projection.
    expect(reboundDuration.widgets_values).toEqual([8]);
  });

  it("releases prepared authority when cancellation wins before native submission", async () => {
    const inputs = {
      task_mode: "t2va" as const,
      user_intent: "Cancel one prepared managed run before model submission.",
      duration_milliseconds: 8000,
      frame_count: 192,
    };
    const fixture = existingGraphRouteFixture(inputs);
    let visibleGraph = structuredClone(fixture.workflow);
    const loadGraphData = vi.fn((next: unknown) => {
      visibleGraph = structuredClone(next) as Json;
    });
    const graph = liveAuthoringGraph(() => visibleGraph);
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      {
        queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(103)),
      },
    );
    const abort = new AbortController();
    const onQueueFailed = vi.fn(async (_disposition: string) => undefined);

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        {
          useExisting: true,
          signal: abort.signal,
          prepareManaged: async (prepared) => {
            abort.abort();
            return {
              expectedIdentity: {
                graphFingerprint: prepared.observation.graph_fingerprint,
                compiledPromptFingerprint:
                  prepared.observation.compiled_prompt_fingerprint,
              },
              onQueueSubmitted: () => undefined,
              onQueueAccepted: () => undefined,
              onQueueFailed,
            };
          },
        },
      ),
    ).rejects.toMatchObject({ code: "cancelled" });

    expect(onQueueFailed).toHaveBeenCalledOnce();
    expect(onQueueFailed).toHaveBeenCalledWith("not_invoked");
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });
});

describe("M23-20 hardened queue seam", () => {
  const inputs = {
    task_mode: "t2va" as const,
    user_intent: "Exercise one privacy-safe queue boundary.",
    duration_milliseconds: 8000,
    frame_count: 192,
  };

  function subject(
    queuePrompt: (
      batch: number,
      compiled: AppModeCompiledPrompt,
      options?: unknown,
    ) => unknown,
  ) {
    const fixture = existingGraphRouteFixture(inputs);
    let visibleGraph = structuredClone(fixture.workflow);
    const graph = liveAuthoringGraph(() => visibleGraph);
    const host = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn((next: unknown) => {
          visibleGraph = structuredClone(next) as Json;
        }),
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph,
      },
      { queuePrompt },
    );
    const controller = createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile,
    });
    return { ...host, controller, fixture };
  }

  function managedCallbacks() {
    const onQueueSubmitted = vi.fn();
    const onQueueAccepted = vi.fn();
    const onQueueFailed = vi.fn();
    return {
      onQueueSubmitted,
      onQueueAccepted,
      onQueueFailed,
      prepareManaged: async (prepared: {
        observation: {
          graph_fingerprint: string;
          compiled_prompt_fingerprint: string;
        };
      }) => ({
        expectedIdentity: {
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
        },
        onQueueSubmitted,
        onQueueAccepted,
        onQueueFailed,
      }),
    };
  }

  it.each(["mutate", "clone"] as const)(
    "keeps the validated prompt isolated across consecutive %s wrapper calls",
    async (mode) => {
      const received: AppModeCompiledPrompt[] = [];
      let call = 0;
      let observedThis: unknown;
      const delegate: QueuePromptDelegate = function (
        this: unknown,
        _batch,
        envelope,
      ) {
        observedThis = this;
        received.push(envelope);
        call += 1;
        return Promise.resolve(acceptedQueueResponse(110 + call));
      };
      const queuePrompt = wrapQueuePrompt(delegate, mode);
      const fixture = subject(queuePrompt);
      const compiledBefore = structuredClone(fixture.fixture.compiled);
      const observations: unknown[] = [];

      const first = await fixture.controller.start(inputs, {
        useExisting: true,
        onQueueSeamObserved: (observation) => observations.push(observation),
      });
      const second = await fixture.controller.start(inputs, {
        useExisting: true,
        onQueueSeamObserved: (observation) => observations.push(observation),
      });

      expect(first.queueResult).toEqual({
        prompt_id: syntheticPromptUuid(111),
        number: -111,
        nodeErrorClassTypes: [],
        nodeErrorTypes: [],
      });
      expect(second.queuePromptId).toBe(syntheticPromptUuid(112));
      expect(received).toHaveLength(2);
      expect(received[0]).not.toBe(received[1]);
      expect(received[0]?.output).not.toBe(received[1]?.output);
      expect(received[0]?.workflow).not.toBe(received[1]?.workflow);
      expect(fixture.fixture.compiled).toEqual(compiledBefore);
      expect(observedThis).toBe(fixture.api);
      expect(observations).toEqual([
        {
          functionName:
            mode === "mutate" ? "mutatingQueuePrompt" : "cloningQueuePrompt",
          arity: 3,
          changedSinceControllerCreation: false,
        },
        {
          functionName:
            mode === "mutate" ? "mutatingQueuePrompt" : "cloningQueuePrompt",
          arity: 3,
          changedSinceControllerCreation: false,
        },
      ]);
    },
  );

  it("observes a later replacement without calling it native or third-party", async () => {
    const original = vi.fn().mockResolvedValue(acceptedQueueResponse(120));
    const fixture = subject(original);
    const replacement = wrapQueuePrompt(
      vi.fn().mockResolvedValue(acceptedQueueResponse(121)),
      "clone",
    );
    fixture.api.queuePrompt = replacement;
    const observed = vi.fn();

    await fixture.controller.start(inputs, {
      useExisting: true,
      onQueueSeamObserved: observed,
    });

    expect(original).not.toHaveBeenCalled();
    expect(observed).toHaveBeenCalledExactlyOnceWith({
      functionName: "cloningQueuePrompt",
      arity: 3,
      changedSinceControllerCreation: true,
    });
    expect(Object.keys(observed.mock.calls[0]?.[0] ?? {})).toEqual([
      "functionName",
      "arity",
      "changedSinceControllerCreation",
    ]);
  });

  it("keeps queue authority unchanged when the diagnostic observer fails", async () => {
    const queuePrompt = vi.fn().mockResolvedValue(acceptedQueueResponse(122));
    const fixture = subject(queuePrompt);

    await expect(
      fixture.controller.start(inputs, {
        useExisting: true,
        onQueueSeamObserved: () => {
          throw new Error("synthetic observer failure");
        },
      }),
    ).resolves.toMatchObject({ queuePromptId: syntheticPromptUuid(122) });
    expect(queuePrompt).toHaveBeenCalledOnce();
  });

  it("accepts a complete acknowledgement while retaining only node-error vocabulary", async () => {
    const queuePrompt = vi.fn().mockResolvedValue(
      acceptedQueueResponse(123, {
        "private-node-id": {
          class_type: "UNETLoader",
          errors: [
            {
              type: "value_not_in_list",
              message: "private rejected value",
            },
          ],
        },
      }),
    );
    const fixture = subject(queuePrompt);

    const result = await fixture.controller.start(inputs, {
      useExisting: true,
    });

    expect(result.queueResult).toEqual({
      prompt_id: syntheticPromptUuid(123),
      number: -123,
      nodeErrorClassTypes: ["UNETLoader"],
      nodeErrorTypes: ["value_not_in_list"],
    });
    expect(JSON.stringify(result.queueResult)).not.toContain("private");
  });

  it.each(["async", "sync"] as const)(
    "classifies a pinned HTTP 400 %s throw as a redacted explicit rejection",
    async (throwMode) => {
      const privateMarker = "private-model-value-and-path";
      const queuePrompt =
        throwMode === "async"
          ? vi.fn().mockRejectedValue(rejectedQueueError(privateMarker))
          : vi.fn(() => {
              throw rejectedQueueError(privateMarker);
            });
      const fixture = subject(queuePrompt);
      const callbacks = managedCallbacks();

      const error = await fixture.controller
        .start(inputs, {
          useExisting: true,
          prepareManaged: callbacks.prepareManaged,
        })
        .then(
          () => undefined,
          (failure: unknown) => failure,
        );

      expect(error).toMatchObject({
        code: "queue_failed",
        source: "queue",
        recovery: "inspect",
        reason: {
          kind: "queue_rejected",
          classTypes: ["CLIPLoader"],
          errorTypes: ["prompt_outputs_failed_validation", "value_not_in_list"],
        },
      });
      if (throwMode === "async")
        expect(callbacks.onQueueSubmitted).toHaveBeenCalledOnce();
      else expect(callbacks.onQueueSubmitted).not.toHaveBeenCalled();
      expect(callbacks.onQueueAccepted).not.toHaveBeenCalled();
      expect(callbacks.onQueueFailed).toHaveBeenCalledExactlyOnceWith(
        "rejected",
      );
      expect(JSON.stringify(error)).not.toContain(privateMarker);
      expect(JSON.stringify(error)).not.toContain("private_node_id");
    },
  );

  it("keeps malformed acknowledgement ownership ambiguous and disables retry", async () => {
    const queuePrompt = vi.fn().mockResolvedValue({
      prompt_id: syntheticPromptUuid(130),
    });
    const fixture = subject(queuePrompt);
    const callbacks = managedCallbacks();

    const error = await fixture.controller
      .start(inputs, {
        useExisting: true,
        prepareManaged: callbacks.prepareManaged,
      })
      .then(
        () => undefined,
        (failure: unknown) => failure,
      );

    expect(error).toMatchObject({
      code: "queue_failed",
      source: "queue",
      recovery: "use_native",
      reason: { kind: "queue_response_invalid" },
    });
    expect(callbacks.onQueueSubmitted).toHaveBeenCalledOnce();
    expect(callbacks.onQueueAccepted).not.toHaveBeenCalled();
    expect(callbacks.onQueueFailed).toHaveBeenCalledExactlyOnceWith(
      "ambiguous",
    );
  });
});

describe("M17-20 template materialization route", () => {
  const inputs = {
    task_mode: "t2va" as const,
    user_intent: "A red kite crosses the sky.",
    duration_milliseconds: 5167,
    frame_count: 124,
  };

  const hostStubs = () =>
    fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(14)) },
    );

  it("reports the graph-load seam as a capability, not as a rollback detail", () => {
    // Without it there is no way to materialize a template, and the honest
    // answer is MANUAL_ONLY_SCOPED rather than the truncated graph M17-20
    // replaces.
    const api = fixtureBackedAppModeApi({ queuePrompt: vi.fn() });
    expect(
      qualifyAppMode({ loadApiJson: vi.fn(), graphToPrompt: vi.fn() }, api),
    ).toEqual({ status: "unavailable", reason: "missing_load_graph_data" });
    expect(
      qualifyAppMode(
        {
          loadApiJson: vi.fn(),
          graphToPrompt: vi.fn(),
          loadGraphData: vi.fn(),
          extensionManager: {
            workflow: { activeWorkflow: {} },
          },
        },
        api,
      ),
    ).toEqual({
      status: "unavailable",
      reason: "missing_detached_graph_constructor",
    });
    expect(
      qualifyAppMode(
        {
          loadApiJson: vi.fn(),
          graphToPrompt: vi.fn(),
          loadGraphData: vi.fn(),
          graph: new (class FixtureGraph {
            serialize() {
              return { nodes: [] };
            }
          })(),
          extensionManager: {
            workflow: { activeWorkflow: {} },
          },
        },
        api,
      ),
    ).toEqual({ status: "ready" });
  });

  it("fails before mutation when no active workflow identity is available", async () => {
    const workflowStore: { activeWorkflow: object | null } = {
      activeWorkflow: { path: "workflows/initial.json" },
    };
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn() },
    );
    workflowStore.activeWorkflow = null;

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
      ),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("bootstraps one workflow identity on a legal blank host and reuses it", async () => {
    let visibleGraph: Json = { nodes: [], links: [] };
    const workflowBehaviour =
      createHostWorkflowBehaviourFixture("null_empty_exact");
    const workflowStore = workflowBehaviour.workflowStore;
    let bootstrappedWorkflow: object | undefined;
    const loadGraphData = vi.fn(
      (
        next: unknown,
        _clean = true,
        _restoreView = true,
        workflow: object | null = null,
      ) => {
        visibleGraph = structuredClone(next) as Json;
        if (workflow === null) {
          bootstrappedWorkflow = {
            path: "workflows/blank-startup.json",
          };
          workflowStore.openWorkflows.push(bootstrappedWorkflow);
          workflowStore.activeWorkflow = bootstrappedWorkflow;
          return;
        }
        workflowStore.activeWorkflow = workflow;
      },
    );
    let promptNumber = 0;
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => visibleGraph },
      },
      {
        queuePrompt: vi.fn(async () =>
          acceptedQueueResponse(40 + ++promptNumber),
        ),
      },
      { workflowVariant: workflowBehaviour },
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });

    await controller.start(inputs, { replaceExisting: true });
    await controller.start(inputs, { replaceExisting: true });

    expect(bootstrappedWorkflow).toBeDefined();
    expect(loadGraphData).toHaveBeenCalledTimes(2);
    expect(loadGraphData.mock.calls[0]?.[3]).toBeNull();
    expect(loadGraphData.mock.calls[1]?.[3]).toBe(bootstrappedWorkflow);
    expect(graphFingerprint(loadGraphData.mock.calls[0]?.[0])).toBe(
      graphFingerprint(loadGraphData.mock.calls[1]?.[0]),
    );
    expect(workflowStore.activeWorkflow).toBe(bootstrappedWorkflow);
    expect(workflowStore.openWorkflows).toEqual([bootstrappedWorkflow]);
    expect(api.queuePrompt).toHaveBeenCalledTimes(2);
  });

  it("does not bootstrap a workflow while asking how to handle a dirty canvas", async () => {
    let visibleGraph: Json = {
      nodes: [{ id: 1, type: "ForeignVisibleNode" }],
      links: [],
    };
    const workflowBehaviour =
      createHostWorkflowBehaviourFixture("null_empty_exact");
    const workflowStore = workflowBehaviour.workflowStore;
    const loadGraphData = vi.fn((next: unknown) => {
      visibleGraph = structuredClone(next) as Json;
      const returned = { path: "workflows/should-not-exist.json" };
      workflowStore.openWorkflows.push(returned);
      workflowStore.activeWorkflow = returned;
    });
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn(),
        graph: { serialize: () => visibleGraph },
      },
      { queuePrompt: vi.fn() },
      { workflowVariant: workflowBehaviour },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
      ),
    ).rejects.toMatchObject({ code: "dirty_graph" });
    expect(loadGraphData).not.toHaveBeenCalled();
    expect(workflowStore.activeWorkflow).toBeNull();
    expect(workflowStore.openWorkflows).toEqual([]);
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses a null active identity while an open workflow awaits activation", async () => {
    const workflowBehaviour = createHostWorkflowBehaviourFixture(
      "foreign_open_inconsistent",
    );
    const workflowStore = workflowBehaviour.workflowStore;
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => ({ nodes: [], links: [] }) },
      },
      { queuePrompt: vi.fn() },
      { workflowVariant: workflowBehaviour },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
      ),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses a final tabless write that returns more than one workflow", async () => {
    let visibleGraph: Json = { nodes: [], links: [] };
    const workflowBehaviour = createHostWorkflowBehaviourFixture("count_drift");
    const workflowStore = workflowBehaviour.workflowStore;
    const loadGraphData = vi.fn(
      (
        next: unknown,
        _clean = true,
        _restoreView = true,
        _workflow: object | null = null,
      ) => {
        visibleGraph = structuredClone(next) as Json;
        const returned = { path: "workflows/returned.json" };
        workflowStore.openWorkflows.push(returned, {
          path: "workflows/unexpected.json",
        });
        workflowStore.activeWorkflow = returned;
      },
    );
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => visibleGraph },
      },
      { queuePrompt: vi.fn() },
      { workflowVariant: workflowBehaviour },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
        { replaceExisting: true },
      ),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(loadGraphData.mock.calls[0]?.[3]).toBeNull();
    expect(app.graphToPrompt).toHaveBeenCalledOnce();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses a tabless host rewrite that removes the owned projection", async () => {
    let visibleGraph: Json = { nodes: [], links: [] };
    const workflowBehaviour =
      createHostWorkflowBehaviourFixture("null_empty_exact");
    const workflowStore = workflowBehaviour.workflowStore;
    let bootstrappedWorkflow: object | undefined;
    const loadGraphData = vi.fn(
      (
        next: unknown,
        _clean = true,
        _restoreView = true,
        workflow: object | null = null,
      ) => {
        if (workflow === null) {
          visibleGraph = {
            nodes: [{ id: 999, type: "ForeignNode", pos: [12, 34] }],
            links: [],
            extra: { host_extension: true },
          };
          bootstrappedWorkflow = { path: "workflows/returned.json" };
          workflowStore.openWorkflows.push(bootstrappedWorkflow);
          workflowStore.activeWorkflow = bootstrappedWorkflow;
          return;
        }
        visibleGraph = structuredClone(next) as Json;
        workflowStore.activeWorkflow = workflow;
      },
    );
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => visibleGraph },
      },
      { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(15)) },
      { workflowVariant: workflowBehaviour },
    );

    await expect(
      createAppModeController(app, api, {
        loadTemplate,
        loadProfile,
      }).start(inputs, { replaceExisting: true }),
    ).rejects.toMatchObject({ code: "stale_graph" });

    expect(loadGraphData).toHaveBeenCalledTimes(2);
    expect(loadGraphData.mock.calls[0]?.[3]).toBeNull();
    expect(loadGraphData.mock.calls[1]?.[3]).toBe(bootstrappedWorkflow);
    expect(workflowStore.activeWorkflow).toBe(bootstrappedWorkflow);
    expect(workflowStore.openWorkflows).toEqual([bootstrappedWorkflow]);
    expect(app.graphToPrompt).toHaveBeenCalledOnce();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("never restores into a workflow that became active during an owned write", async () => {
    let visibleGraph: Json = { nodes: [], links: [] };
    const initialWorkflow = { path: "workflows/initial.json" };
    const foreignWorkflow = { path: "workflows/user-selected.json" };
    const workflowStore = { activeWorkflow: initialWorkflow as object };
    const loadGraphData = vi.fn((next: unknown) => {
      visibleGraph = structuredClone(next) as Json;
      workflowStore.activeWorkflow = foreignWorkflow;
    });
    const { app, api } = fixtureBackedAppModeHost(
      {
        extensionManager: { workflow: workflowStore },
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => visibleGraph },
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
      ),
    ).rejects.toMatchObject({ code: "stale_graph" });
    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(workflowStore.activeWorkflow).toBe(foreignWorkflow);
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("reads the pinned template through the host's own asset route", async () => {
    const { app, api } = hostStubs();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => JSON.stringify(loadSyntheticTemplate("t2v")),
    });
    const fileURL = vi.fn((route: string) => `http://host${route}`);
    vi.stubGlobal("fetch", fetchMock);
    try {
      await createAppModeController(
        app,
        { ...api, fileURL },
        {
          loadProfile,
        },
      ).start(inputs);
    } finally {
      vi.unstubAllGlobals();
    }
    expect(fileURL).toHaveBeenCalledWith(
      "/templates/video_minimax_h3_t2v.json",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://host/templates/video_minimax_h3_t2v.json",
      { credentials: "same-origin" },
    );
  });

  it("refuses an unreadable or unbounded template without touching the canvas", async () => {
    for (const response of [
      { ok: false, text: async () => "" },
      { ok: true, text: async () => "x".repeat(1_048_577) },
      { ok: true, text: async () => "{" },
      { ok: true, text: async () => "[]" },
      {
        ok: true,
        text: async () => {
          throw new Error("private host read detail");
        },
      },
    ]) {
      const { app, api } = hostStubs();
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
      try {
        await expect(
          createAppModeController(app, api, { loadProfile }).start(inputs),
        ).rejects.toMatchObject({
          code: "incompatible_seam",
          reason: { kind: "template_unavailable" },
        });
      } finally {
        vi.unstubAllGlobals();
      }
      expect(app.loadGraphData).not.toHaveBeenCalled();
      expect(api.queuePrompt).not.toHaveBeenCalled();
    }
  });

  it("classifies a rejected template request without exposing host detail", async () => {
    const { app, api } = hostStubs();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("private template transport detail")),
    );
    try {
      const pending = createAppModeController(app, api, { loadProfile }).start(
        inputs,
      );
      await expect(pending).rejects.toMatchObject({
        code: "incompatible_seam",
        reason: { kind: "template_unavailable" },
      });
      await expect(pending).rejects.not.toThrow(/private template transport/i);
    } finally {
      vi.unstubAllGlobals();
    }
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses a template that no longer carries the splice point", async () => {
    const { app, api } = hostStubs();
    const anchorless = loadSyntheticTemplate("video_minimax_h3_t2v");
    anchorless.nodes = (anchorless.nodes as Record<string, unknown>[]).filter(
      (node) => node.type !== "MiniMaxH3ImageToVideo",
    );
    await expect(
      createAppModeController(app, api, {
        loadTemplate: () => anchorless,
        loadProfile,
      }).start(inputs),
    ).rejects.toMatchObject({
      code: "incompatible_seam",
      reason: { kind: "template_unavailable" },
    });
    // Nothing was loaded, so nothing needs restoring: a template this
    // repository cannot splice leaves the user's canvas exactly as it was.
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("enforces the request contract before the template is read", async () => {
    const { app, api } = hostStubs();
    const loadTemplate = vi.fn(loadSyntheticTemplate);
    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start({
        ...inputs,
        user_intent: "   ",
      }),
    ).rejects.toMatchObject({ code: "invalid_request" });
    expect(loadTemplate).not.toHaveBeenCalled();
  });

  it("does not compare the Request duration to the native length branch", async () => {
    const { app, api } = hostStubs();
    const output = createMaterializedPrompt(inputs) as Record<
      string,
      Record<string, unknown>
    >;
    output["52"] = {
      class_type: "PrimitiveFloat",
      inputs: { value: inputs.duration_milliseconds / 1000 },
    };
    (output["1"]!.inputs as Record<string, unknown>).duration_seconds = [
      "52",
      0,
    ];
    vi.mocked(app.graphToPrompt).mockResolvedValue({ output, workflow: {} });

    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
      ),
    ).resolves.toMatchObject({ queuePromptId: syntheticPromptUuid(14) });
    expect(api.queuePrompt).toHaveBeenCalledOnce();
    expect(app.loadGraphData).toHaveBeenCalledOnce();
  });

  it("keeps a mode that does not exist a malformed request, not a host refusal", async () => {
    // The profile answers per family; a mode no family claims would otherwise be
    // reported as a host that does not support it, which blames the wrong side.
    const { app, api } = hostStubs();
    const loadTemplate = vi.fn(loadSyntheticTemplate);
    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start({
        ...inputs,
        task_mode: "x2va" as never,
      }),
    ).rejects.toMatchObject({ code: "invalid_request" });
    expect(loadProfile).not.toHaveBeenCalled();
    expect(loadTemplate).not.toHaveBeenCalled();
  });
});

describe("M23-29 native length is not an admission authority", () => {
  const inputs = {
    task_mode: "t2va" as const,
    user_intent: "safe",
    duration_milliseconds: 5167,
    frame_count: 124,
  };
  const derived = (expression: string, authored: number) => {
    const output = createQualifiedBasePrompt(inputs) as Record<
      string,
      Record<string, unknown>
    >;
    output["6"] = {
      ...output["6"],
      inputs: {
        ...(output["6"].inputs as Record<string, unknown>),
        length: ["51", 0],
      },
    };
    output["50"] = {
      class_type: "PrimitiveFloat",
      inputs: { value: authored },
    };
    output["51"] = {
      class_type: "ComfyMathExpression",
      inputs: { expression, "values.a": ["50", 0] },
    };
    return { output, workflow: {} };
  };

  it.each([
    [OFFICIAL_LENGTH_EXPRESSION, 5.167],
    ["round(a * 30)", 5.167],
    ["operator_owned(a)", 1000],
  ])("does not inspect expression %s or operand %s", (expression, authored) => {
    expect(isCompatibleExistingPrompt(derived(expression, authored))).toBe(
      true,
    );
  });
});

describe("M17-20 generation profile admission", () => {
  // M17-20 D1/D2. Materialization sits behind the backend's capability
  // decision: a host that cannot run the graph must cost zero queue
  // submissions, zero template reads and zero canvas writes.
  const inputs = {
    task_mode: "t2va" as const,
    user_intent: "A safe bounded intent for the admission rows.",
    duration_milliseconds: 5167,
    frame_count: 124,
  };

  const hostStubs = () =>
    fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(inputs),
          workflow: {},
        }),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(16)) },
    );

  it.each([
    ["the served template has drifted", TEMPLATE_DRIFT, "template_drift"],
    ["the host itself does not qualify", UNSUPPORTED_HOST, "unsupported_host"],
  ])("refuses materialization when %s", async (_label, override, reason) => {
    const { app, api } = hostStubs();
    loadProfile.mockImplementation(() => generationProfile({ text: override }));
    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
      ),
    ).rejects.toMatchObject({
      code: "incompatible_seam",
      reason: {
        kind: "generation_admission_refused",
        admissionReason: reason,
        unsatisfiedSlots: [],
      },
    });
    expect(loadTemplate).not.toHaveBeenCalled();
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("queues a missing default weight for ComfyUI validation", async () => {
    const { app, api } = hostStubs();
    loadProfile.mockImplementation(() =>
      generationProfile({ text: MISSING_ASSET }),
    );
    await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs);
    expect(loadTemplate).toHaveBeenCalledWith("video_minimax_h3_t2v");
    expect(app.loadGraphData).toHaveBeenCalledOnce();
    expect(api.queuePrompt).toHaveBeenCalledOnce();
  });

  it("still admits the adoption route when only the default weight is absent", async () => {
    // The other half of D2: having fixed the widget, the user queues the visible
    // graph. Refusing that for the same reason would close the only door out.
    const { app, api } = hostStubs();
    loadProfile.mockImplementation(() =>
      generationProfile({ text: MISSING_ASSET }),
    );
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });
    // The canvas is empty here, so adoption fails on its own graph rule rather
    // than on admission -- which is exactly the distinction being asserted.
    await expect(
      controller.start(inputs, { useExisting: true }),
    ).rejects.toMatchObject({ code: "incompatible_graph" });
    expect(await controller.admission("t2va")).toMatchObject({
      reason: "missing_asset",
    });
  });

  it("does not apply host profile admission to the existing route", async () => {
    const fixture = existingGraphRouteFixture(inputs);
    let visible = structuredClone(fixture.workflow);
    const loadGraphData = vi.fn((candidate: unknown) => {
      visible = structuredClone(candidate) as Json;
    });
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData,
        graphToPrompt: vi.fn().mockResolvedValue(fixture.compiled),
        graph: liveAuthoringGraph(() => visible),
      },
      { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(17)) },
    );
    const unsupportedProfile = vi.fn(() =>
      generationProfile({ text: UNSUPPORTED_HOST }),
    );

    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile: unsupportedProfile,
      createDetachedGraph: vi.fn((candidate: unknown) => ({ candidate })),
    }).start(inputs, { useExisting: true });

    expect(result.route).toBe("existing");
    expect(unsupportedProfile).not.toHaveBeenCalled();
    expect(loadGraphData).toHaveBeenCalledOnce();
    expect(api.queuePrompt).toHaveBeenCalledOnce();
  });

  it("fails closed when the capability cannot be read at all", async () => {
    const { app, api } = hostStubs();
    loadProfile.mockRejectedValue(new Error("private host detail"));
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });
    await expect(controller.start(inputs)).rejects.toMatchObject({
      code: "incompatible_seam",
      reason: {
        kind: "generation_admission_refused",
        admissionReason: "profile_unavailable",
        unsatisfiedSlots: [],
      },
    });
    expect(await controller.admission("t2va")).toMatchObject({
      status: "refused",
      reason: "profile_unavailable",
      remediation: "upgrade_host",
    });
    expect(loadTemplate).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("carries unsupported task-mode admission into the refusal reason", async () => {
    const { app, api } = hostStubs();
    loadProfile.mockImplementation(() => {
      const available = loadAvailableProfile();
      return {
        ...available,
        families: available.families.map((family) => ({
          ...family,
          taskModes: family.taskModes.filter((mode) => mode !== "t2va"),
        })),
      };
    });
    await expect(
      createAppModeController(app, api, { loadTemplate, loadProfile }).start(
        inputs,
      ),
    ).rejects.toMatchObject({
      code: "incompatible_seam",
      reason: {
        kind: "generation_admission_refused",
        admissionReason: "unsupported_task_mode",
        unsatisfiedSlots: [],
      },
    });
    expect(loadTemplate).not.toHaveBeenCalled();
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("asks the host once and does not cache a failed read", async () => {
    const { app, api } = hostStubs();
    const controller = createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    });
    loadProfile.mockRejectedValueOnce(new Error("transient"));
    expect(await controller.admission("t2va")).toMatchObject({
      reason: "profile_unavailable",
    });
    expect(await controller.admission("t2va")).toEqual({ status: "admitted" });
    expect(await controller.admission("ref2va")).toEqual({
      status: "admitted",
    });
    // Two calls: the rejected read, then the one that succeeded and was reused.
    expect(loadProfile).toHaveBeenCalledTimes(2);
  });

  it("materializes normally once the backend says the family is available", async () => {
    const { app, api } = hostStubs();
    const result = await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(inputs);
    expect(result.route).toBe("new");
    expect(loadTemplate).toHaveBeenCalledWith("video_minimax_h3_t2v");
  });
});

describe("M17-20 D5 artifact sink location", () => {
  // The pinned templates all ship `video/MiniMax_H3`, so without this every
  // revision of every workspace writes into one place and a regeneration is
  // indistinguishable from the run it replaced.
  const base = {
    task_mode: "t2va" as const,
    user_intent: "A safe bounded intent for the artifact rows.",
    duration_milliseconds: 5167,
    frame_count: 124,
  };

  it("is deterministic in the authored revision and moves when the revision does", () => {
    expect(appModeArtifactPrefix(base)).toBe(appModeArtifactPrefix(base));
    expect(appModeArtifactPrefix(base)).not.toBe(
      appModeArtifactPrefix({ ...base, user_intent: "A different intent." }),
    );
    expect(appModeArtifactPrefix(base)).not.toBe(
      appModeArtifactPrefix({ ...base, duration_milliseconds: 6167 }),
    );
  });

  it("separates workspaces and refuses an unvalidated scope rather than pathing it", () => {
    expect(appModeArtifactPrefix(base, "workspace.1")).not.toBe(
      appModeArtifactPrefix(base, "workspace.2"),
    );
    // A scope that is not a plain identifier never reaches the path; it falls
    // back to the session scope, so no host path component can be injected.
    for (const hostile of ["../../etc", "C:/models", "a/b", ""])
      expect(appModeArtifactPrefix(base, hostile)).toBe(
        appModeArtifactPrefix(base),
      );
  });

  it("carries no intent text, no filename and no host path", () => {
    const prefix = appModeArtifactPrefix(
      { ...base, user_intent: "A rare distinctive phrase about kites." },
      "workspace.1",
    );
    expect(prefix).toContain("video/h3-context/workspace.1/");
    expect(prefix).not.toMatch(/kite|safetensors|[A-Za-z]:|\.\./i);
  });

  it("writes the deterministic prefix into the materialized sink", async () => {
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue({
          output: createMaterializedPrompt(base),
          workflow: {},
        }),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      {
        queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(17)),
      },
    );
    await createAppModeController(app, api, {
      loadTemplate,
      loadProfile,
    }).start(base);
    const loaded = app.loadGraphData.mock.calls[0]?.[0] as {
      nodes: { type?: string; widgets_values?: unknown[] }[];
    };
    const sink = loaded.nodes.find((node) => node.type === "SaveVideo");
    expect(sink?.widgets_values?.[0]).toBe(appModeArtifactPrefix(base));
  });
});
