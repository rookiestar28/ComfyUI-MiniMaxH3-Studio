import { describe, expect, it, vi } from "vitest";

import {
  candidateRequiresHostLoadBeforeCompile,
  createAppModeController,
  type AppModeCompiledPrompt,
} from "../src/host/appMode";
import { selectOfficialAsset } from "../src/host/officialAssetResolution";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import {
  generationProfile,
  loadAvailableProfile,
  MISSING_ASSET,
} from "./support/generationProfileFixture";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";
import { acceptedQueueResponse } from "./support/queuePromptTestDouble";
import {
  familySpecs,
  installedInventory,
  nodeDefinitions,
  templateWithDirectLoaders,
} from "./support/officialAssetInventoryFixture";
import {
  definitionIds,
  misalignedDetachedEnvelope,
  normalizeOwnedSlotsOnWrite,
  placeholderDetachedEnvelope,
  subgraphInstanceRegistry,
} from "./support/hostBehaviourCatalogue";

type Json = Record<string, any>;

/**
 * M23-37. Two host behaviours from the D13 catalogue, observed on the supplied
 * host (ComfyUI 0.34.0 / frontend 1.51.9) by the first supported-host lane run
 * after M23-25:
 *
 * 1. a subgraph instance type is registered, and its compact serialized
 *    inputs reconciled, only by the host's own `loadGraphData`: a detached
 *    compile of a template candidate returns a placeholder envelope before
 *    that load and a link-misaligned envelope after it, while the root compile
 *    of the loaded canvas is correct;
 * 2. the host re-serializes repository-owned nodes from their node definitions
 *    after the one canvas write (appended optional inputs, reordered inputs,
 *    dropped empty widget values).
 *
 * The host double carries a real loader inventory in the same public registry,
 * exactly as the supplied host does, so official-asset resolution runs on every
 * row here rather than being skipped by an empty registry.
 */

const SUBGRAPH_ID = "79dd8a95-fixture-subgraph-instance";

const inputs = {
  task_mode: "t2va" as const,
  user_intent: "A quiet landscape with a gentle camera drift.",
  duration_milliseconds: 5167,
  frame_count: 124,
};

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

/** The official templates carry their generation block as one subgraph definition. */
function templateWithSubgraphDefinition(): Json {
  const template = templateWithDirectLoaders();
  template.definitions = {
    subgraphs: [
      { id: SUBGRAPH_ID, nodes: [], links: [], inputs: [], outputs: [] },
    ],
  };
  return template;
}

function registeringHost(options: {
  registered: boolean;
  compileAfterRegistration?: "qualified" | "placeholder";
  normalizeOnWrite?: boolean;
  /** Official roles absent from the host inventory. */
  omitRoles?: readonly string[];
}) {
  const { inventory } = installedInventory(
    "image_to_video",
    options.omitRoles ?? [],
  );
  const registry = subgraphInstanceRegistry({
    ...nodeDefinitions(inventory),
    ...(options.registered
      ? { [SUBGRAPH_ID]: { nodeData: { name: SUBGRAPH_ID, subgraph: true } } }
      : {}),
  });
  const materializedPrompt = (): Record<string, unknown> => {
    const output = createQualifiedBasePrompt(
      inputs,
      {},
      {
        sharedDurationSource: true,
      },
    ) as Record<string, Json>;
    for (const spec of familySpecs("image_to_video")) {
      const selected = selectOfficialAsset(
        spec,
        inventory[spec.folderCategory] ?? [],
      );
      if (selected === undefined) continue;
      output[`asset-${spec.slot}`] = {
        class_type: spec.loaderType,
        inputs: { [spec.widgetName]: selected.value },
      };
    }
    return output;
  };
  let visible = emptyCanvas();
  const loadGraphData = vi.fn((candidate: unknown) => {
    registry.registerFromLoad(candidate);
    visible = (
      options.normalizeOnWrite === true
        ? normalizeOwnedSlotsOnWrite(structuredClone(candidate))
        : structuredClone(candidate)
    ) as Json;
  });
  const createDetachedGraph = vi.fn((candidate: unknown) => ({
    candidate: structuredClone(candidate),
  }));
  const graphToPrompt = vi.fn((graph?: unknown): AppModeCompiledPrompt => {
    const detached = graph !== undefined;
    const candidate = detached
      ? (graph as { candidate?: unknown }).candidate
      : visible;
    const qualified = (): AppModeCompiledPrompt =>
      ({
        output: materializedPrompt(),
        workflow: candidate,
      }) as AppModeCompiledPrompt;
    // A candidate without subgraph definitions compiles the same detached or
    // loaded; the catalogue behaviours apply only to definitions-carrying ones.
    if (definitionIds(candidate).length === 0) return qualified();
    if (
      !registry.isRegistered(SUBGRAPH_ID) ||
      options.compileAfterRegistration === "placeholder"
    )
      return placeholderDetachedEnvelope(candidate) as AppModeCompiledPrompt;
    if (detached)
      return misalignedDetachedEnvelope(
        candidate,
        materializedPrompt(),
      ) as AppModeCompiledPrompt;
    return qualified();
  });
  const { app, api } = fixtureBackedAppModeHost(
    {
      loadApiJson: vi.fn(),
      loadGraphData,
      graphToPrompt,
      graph: { serialize: () => visible },
    },
    { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(1)) },
  );
  return {
    app,
    api,
    registry,
    loadGraphData,
    createDetachedGraph,
    graphToPrompt,
    visible: () => visible,
  };
}

describe("M23-37 candidate host load before compile", () => {
  it("requires the host load for a candidate with subgraph definitions or a root graph id", () => {
    expect(candidateRequiresHostLoadBeforeCompile({ nodes: [] })).toBe(false);
    // B-M1605-EXIST-01: the host keys widget state by graph id, so a detached graph carrying the
    // visible workflow's id writes into the live canvas even without definitions.
    expect(
      candidateRequiresHostLoadBeforeCompile({
        id: "4f0d2c2e-2a4b-4c8e-9e36-5d0b8e1a7c11",
        nodes: [],
      }),
    ).toBe(true);
    expect(
      candidateRequiresHostLoadBeforeCompile({
        definitions: { subgraphs: [] },
      }),
    ).toBe(false);
    expect(candidateRequiresHostLoadBeforeCompile("not a graph")).toBe(false);
    expect(
      candidateRequiresHostLoadBeforeCompile({
        definitions: { subgraphs: [{ id: SUBGRAPH_ID }] },
      }),
    ).toBe(true);
  });

  it("writes a definitions-carrying candidate before the compile, validates the root compile of the written canvas, and queues once", async () => {
    const host = registeringHost({ registered: false });
    const onHostLoadBeforeCompile = vi.fn();

    await createAppModeController(host.app, host.api, {
      loadTemplate: templateWithSubgraphDefinition,
      loadProfile: loadAvailableProfile,
      createDetachedGraph: host.createDetachedGraph,
      readNodeDefinitions: () => host.registry.registry,
    }).start(inputs, { onHostLoadBeforeCompile });

    expect(host.loadGraphData).toHaveBeenCalledTimes(1);
    expect(host.graphToPrompt).toHaveBeenCalledTimes(1);
    expect(host.loadGraphData.mock.invocationCallOrder[0]).toBeLessThan(
      host.graphToPrompt.mock.invocationCallOrder[0]!,
    );
    // The compile is the root compile: no detached construction at all.
    expect(host.createDetachedGraph).not.toHaveBeenCalled();
    expect(host.graphToPrompt.mock.calls[0]?.[0]).toBeUndefined();
    expect(onHostLoadBeforeCompile).toHaveBeenCalledTimes(1);
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
    expect(host.registry.isRegistered(SUBGRAPH_ID)).toBe(true);
    expect((host.visible().definitions as Json).subgraphs[0].id).toBe(
      SUBGRAPH_ID,
    );
  });

  it("writes first even on a host that has already registered the instance type, because a detached construction misaligns the instance links", async () => {
    const host = registeringHost({ registered: true });
    const onHostLoadBeforeCompile = vi.fn();

    await createAppModeController(host.app, host.api, {
      loadTemplate: templateWithSubgraphDefinition,
      loadProfile: loadAvailableProfile,
      createDetachedGraph: host.createDetachedGraph,
      readNodeDefinitions: () => host.registry.registry,
    }).start(inputs, { onHostLoadBeforeCompile });

    expect(host.loadGraphData.mock.invocationCallOrder[0]).toBeLessThan(
      host.graphToPrompt.mock.invocationCallOrder[0]!,
    );
    expect(host.createDetachedGraph).not.toHaveBeenCalled();
    expect(host.loadGraphData).toHaveBeenCalledTimes(1);
    expect(onHostLoadBeforeCompile).toHaveBeenCalledTimes(1);
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
  });

  it("keeps the detached compile ahead of the one write for a template without subgraph definitions", async () => {
    const host = registeringHost({ registered: false });
    const onHostLoadBeforeCompile = vi.fn();

    await createAppModeController(host.app, host.api, {
      loadTemplate: templateWithDirectLoaders,
      loadProfile: loadAvailableProfile,
      createDetachedGraph: host.createDetachedGraph,
      readNodeDefinitions: () => host.registry.registry,
    }).start(inputs, { onHostLoadBeforeCompile });

    expect(host.createDetachedGraph).toHaveBeenCalledTimes(1);
    expect(host.graphToPrompt.mock.invocationCallOrder[0]).toBeLessThan(
      host.loadGraphData.mock.invocationCallOrder[0]!,
    );
    expect(host.loadGraphData).toHaveBeenCalledTimes(1);
    expect(onHostLoadBeforeCompile).not.toHaveBeenCalled();
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
  });

  it("loads and queues unresolved model names through host registration", async () => {
    const host = registeringHost({
      registered: false,
      omitRoles: ["video_unet"],
    });
    await createAppModeController(host.app, host.api, {
      loadTemplate: templateWithSubgraphDefinition,
      loadProfile: () => generationProfile({ text: MISSING_ASSET }),
      createDetachedGraph: host.createDetachedGraph,
      readNodeDefinitions: () => host.registry.registry,
    }).start(inputs);
    expect(host.loadGraphData).toHaveBeenCalledOnce();
    expect(host.graphToPrompt).toHaveBeenCalledOnce();
    expect(host.api.queuePrompt).toHaveBeenCalledOnce();
  });

  it("restores the prior canvas exactly once when the root compile refuses after the host load", async () => {
    const host = registeringHost({
      registered: false,
      compileAfterRegistration: "placeholder",
    });

    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate: templateWithSubgraphDefinition,
        loadProfile: loadAvailableProfile,
        createDetachedGraph: host.createDetachedGraph,
        readNodeDefinitions: () => host.registry.registry,
      }).start(inputs),
    ).rejects.toMatchObject({ code: "compile_failed" });

    // One host load, one restore of the empty canvas, no queue.
    expect(host.loadGraphData).toHaveBeenCalledTimes(2);
    expect(host.loadGraphData.mock.calls[1]?.[0]).toEqual(emptyCanvas());
    expect(host.visible()).toEqual(emptyCanvas());
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it("completes a run on a host that normalizes the owned slots during the one write", async () => {
    const host = registeringHost({ registered: true, normalizeOnWrite: true });

    await createAppModeController(host.app, host.api, {
      loadTemplate: templateWithSubgraphDefinition,
      loadProfile: loadAvailableProfile,
      createDetachedGraph: host.createDetachedGraph,
      readNodeDefinitions: () => host.registry.registry,
    }).start(inputs);

    expect(host.loadGraphData).toHaveBeenCalledTimes(1);
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
    const productShell = (host.visible().nodes as Json[]).find(
      (node) => node.type === "comfyui_h3_context.H3Context.ProductShell",
    );
    expect(productShell?.inputs.at(-1)?.name).toBe("host_optional_b");
  });

  it("completes a first-load run on a host that also normalizes the owned slots", async () => {
    const host = registeringHost({ registered: false, normalizeOnWrite: true });

    await createAppModeController(host.app, host.api, {
      loadTemplate: templateWithSubgraphDefinition,
      loadProfile: loadAvailableProfile,
      createDetachedGraph: host.createDetachedGraph,
      readNodeDefinitions: () => host.registry.registry,
    }).start(inputs);

    expect(host.loadGraphData).toHaveBeenCalledTimes(1);
    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
  });
});
