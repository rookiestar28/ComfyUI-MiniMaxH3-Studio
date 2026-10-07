import { describe, expect, it } from "vitest";

import {
  inspectH3GraphAdmission,
  inspectVisibleH3Graph,
  isQualifiedExternalReferenceGraph,
  recognizeH3InferenceCanvas,
} from "../src/host/graphAdapter";
import referenceAssistant from "../../subgraphs/H3 Context Assistant - Reference.json";
import baseAssistant from "../../subgraphs/H3 Context Assistant - Base.json";
import { observedH3CoreCanvas } from "./support/h3CoreCensusFixture";

const manifest = {
  fields: [
    {
      field_id:
        "h3.comfyui_h3_context_h3context_productshell.output.product_shell",
      node_id: "comfyui_h3_context.H3Context.ProductShell",
      port_name: "product_shell",
    },
  ],
};

const canonicalTypes = [
  "comfyui_h3_context.H3Context.Request",
  "comfyui_h3_context.H3Context.Plan",
  "comfyui_h3_context.H3Context.Compiler",
  "comfyui_h3_context.H3Context.Validator",
  "comfyui_h3_context.H3Context.NativeH3Adapter",
  "comfyui_h3_context.H3Context.Preview",
  "MiniMaxH3ImageToVideo",
];

function canonicalGraph(shellId = 8) {
  return {
    nodes: [
      ...canonicalTypes.map((type, index) => ({ id: index + 10, type })),
      { id: shellId, type: "comfyui_h3_context.H3Context.ProductShell" },
    ],
  };
}

function directBaseGraph() {
  const node = (
    id: number,
    type: string,
    inputs: Array<number | null>,
    outputs: Array<number[]>,
  ) => ({
    id,
    type,
    inputs: inputs.map((link) => ({ link })),
    outputs: outputs.map((links) => ({ links })),
  });
  const graph = {
    nodes: [
      node(1, canonicalTypes[0], [null, null, null, null, null], [[10]]),
      node(2, canonicalTypes[1], [10, null, null], [[11, 12], []]),
      node(3, canonicalTypes[2], [11], [[], [], [13]]),
      node(4, canonicalTypes[3], [12, 13], [[], [14, 15, 18]]),
      node(5, canonicalTypes[4], [14], [[], [16]]),
      node(
        6,
        "comfyui_h3_context.H3Context.ProductShell",
        [15, 16],
        [[17], []],
      ),
      node(
        7,
        canonicalTypes[6],
        [null, null, null, null, 17, null, null, null],
        [[], []],
      ),
      node(8, canonicalTypes[5], [18], [[], []]),
    ],
    links: [
      [10, 1, 0, 2, 0, "H3_CONTEXT_REQUEST"],
      [11, 2, 0, 3, 0, "H3_CONTEXT_PLAN"],
      [12, 2, 0, 4, 0, "H3_CONTEXT_PLAN"],
      [13, 3, 2, 4, 1, "H3_PROMPT_DOCUMENT"],
      [14, 4, 1, 5, 0, "H3_CONTEXT_REPORT"],
      [15, 4, 1, 6, 0, "H3_CONTEXT_REPORT"],
      [16, 5, 1, 6, 1, "H3_NATIVE_H3_WIRING"],
      [17, 6, 0, 7, 4, "STRING"],
      [18, 4, 1, 8, 0, "H3_CONTEXT_REPORT"],
    ],
  };
  const directRequest = graph.nodes.find(
    (candidate) => candidate.type === canonicalTypes[0],
  );
  const directGeneration = graph.nodes.find(
    (candidate) => candidate.type === canonicalTypes[6],
  );
  if (directRequest === undefined || directGeneration === undefined)
    throw new Error("fixture drift");
  (directRequest as Record<string, unknown>).widgets_values = [
    "t2va",
    "safe",
    5.167,
  ];
  (directGeneration as Record<string, unknown>).widgets_values = [
    512, 512, 124,
  ];
  return graph;
}

describe("inspectVisibleH3Graph", () => {
  it("finds one visible shell recursively without private graph fields", () => {
    const graph = {
      nodes: [
        ...canonicalTypes.map((type, index) => ({ id: index + 10, type })),
        { id: 1, type: "Comfy.Subgraph", subgraph_id: "child-1" },
      ],
      definitions: {
        subgraphs: [
          {
            id: "child-1",
            nodes: [
              { id: 7, type: "comfyui_h3_context.H3Context.ProductShell" },
            ],
          },
        ],
      },
    };
    expect(inspectVisibleH3Graph(graph, manifest)).toMatchObject({
      status: "ready",
      anchors: [
        {
          executionId: "1:7",
          nodeId: "comfyui_h3_context.H3Context.ProductShell",
        },
      ],
    });
  });

  it("fails closed for missing, duplicate, malformed, or private-only graphs", () => {
    expect(inspectVisibleH3Graph({ nodes: [] }, manifest).status).toBe(
      "missing",
    );
    expect(
      inspectVisibleH3Graph(
        {
          nodes: [
            { id: 1, type: "comfyui_h3_context.H3Context.ProductShell" },
            { id: 2, type: "comfyui_h3_context.H3Context.ProductShell" },
          ],
        },
        manifest,
      ).status,
    ).toBe("ambiguous");
    expect(() => inspectVisibleH3Graph({ _nodes: [] }, manifest)).toThrow(
      /serialized/,
    );
    const duplicateNamedPort = canonicalGraph();
    const duplicateRequest = duplicateNamedPort.nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (duplicateRequest === undefined) throw new Error("fixture drift");
    duplicateRequest.inputs = [
      { name: "task_mode", type: "COMBO", link: null },
      { name: "task_mode", type: "COMBO", link: null },
      { name: "duration_seconds", type: "FLOAT", link: null },
    ];
    expect(inspectVisibleH3Graph(duplicateNamedPort, manifest)).toMatchObject({
      status: "incompatible",
    });
    const hiddenRootMember = canonicalGraph();
    const hiddenRequest = hiddenRootMember.nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (hiddenRequest === undefined) throw new Error("fixture drift");
    hiddenRequest.private_media = "file:///secret";
    expect(inspectVisibleH3Graph(hiddenRootMember, manifest)).toMatchObject({
      status: "incompatible",
    });
  });

  it("does not mutate the graph while inspecting", () => {
    const graph = Object.freeze({
      nodes: Object.freeze([
        Object.freeze({
          id: "shell",
          type: "comfyui_h3_context.H3Context.ProductShell",
        }),
      ]),
    });
    expect(inspectVisibleH3Graph(graph, manifest).status).toBe("incompatible");
  });

  it("uses serialized save/load identity for paste ambiguity and undo recovery", () => {
    const shell = {
      id: 6,
      type: "comfyui_h3_context.H3Context.ProductShell",
    };
    const saved = JSON.parse(JSON.stringify(canonicalGraph(6))) as unknown;
    expect(inspectVisibleH3Graph(saved, manifest)).toMatchObject({
      status: "ready",
      anchors: [{ executionId: "6" }],
    });
    const pasted = {
      ...canonicalGraph(6),
      nodes: [...canonicalGraph(6).nodes, { ...shell, id: 9 }],
    };
    expect(inspectVisibleH3Graph(pasted, manifest).status).toBe("ambiguous");
    expect(inspectVisibleH3Graph(saved, manifest).status).toBe("ready");
  });

  it("accepts a Reference graph with image sources and no video component node", () => {
    const graph = {
      nodes: [
        ...canonicalTypes.filter((type) => type !== "MiniMaxH3ImageToVideo"),
        { id: 20, type: "MiniMaxH3ReferenceToVideo" },
        { id: 21, type: "comfyui_h3_context.H3Context.ReferenceRegistry" },
        { id: 22, type: "LoadImage" },
        { id: 23, type: "LoadImage" },
        { id: 24, type: "comfyui_h3_context.H3Context.ProductShell" },
      ].map((type, index) =>
        typeof type === "string" ? { id: index + 10, type } : type,
      ),
    };
    expect(inspectVisibleH3Graph(graph, manifest).status).toBe("ready");
  });

  it("accepts the qualified Reference Assistant boundary without local source nodes", () => {
    expect(inspectVisibleH3Graph(referenceAssistant, manifest)).toMatchObject({
      status: "ready",
      anchors: [{ executionId: "10:12" }],
      existingGraphCompatible: true,
    });
    const missingMedia = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const definition = missingMedia.definitions.subgraphs[0];
    const inputs = definition?.inputs as
      Array<Record<string, unknown>> | undefined;
    if (inputs === undefined) throw new Error("fixture drift");
    inputs[3]!.linkIds = [];
    expect(inspectVisibleH3Graph(missingMedia, manifest)).toMatchObject({
      status: "incompatible",
      reason: "mixed_h3_flow",
    });
    const wrongOutputType = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const wrongOutputNode = wrongOutputType.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(wrongOutputNode)) throw new Error("fixture drift");
    const shellNode = wrongOutputNode.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.ProductShell",
    ) as Record<string, unknown> | undefined;
    if (shellNode === undefined) throw new Error("fixture drift");
    (shellNode.outputs as Array<Record<string, unknown>>)[0]!.type = "FOREIGN";
    expect(inspectVisibleH3Graph(wrongOutputType, manifest)).toMatchObject({
      status: "incompatible",
    });
    const wrongReferenceMode = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const wrongReferenceNodes =
      wrongReferenceMode.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(wrongReferenceNodes)) throw new Error("fixture drift");
    const referenceRequest = wrongReferenceNodes.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (referenceRequest === undefined) throw new Error("fixture drift");
    referenceRequest.widgets_values = [
      "t2va",
      "Preserve the supplied references while the subject walks forward.",
      124,
    ];
    expect(inspectVisibleH3Graph(wrongReferenceMode, manifest)).toMatchObject({
      status: "incompatible",
    });
    const directWithoutSources = {
      nodes: [
        ...canonicalTypes.filter((type) => type !== "MiniMaxH3ImageToVideo"),
        { id: 20, type: "MiniMaxH3ReferenceToVideo" },
        { id: 21, type: "comfyui_h3_context.H3Context.ReferenceRegistry" },
        { id: 24, type: "comfyui_h3_context.H3Context.ProductShell" },
      ].map((type, index) =>
        typeof type === "string" ? { id: index + 30, type } : type,
      ),
      definitions: referenceAssistant.definitions,
    };
    expect(inspectVisibleH3Graph(directWithoutSources, manifest)).toMatchObject(
      {
        status: "incompatible",
        reason: "mixed_h3_flow",
      },
    );
    const duplicateImageSlot = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const links = duplicateImageSlot.definitions.subgraphs[0]!.links as Array<
      Record<string, unknown>
    >;
    const duplicate = links.find((link) => link.id === 20);
    if (duplicate === undefined) throw new Error("fixture drift");
    duplicate.target_slot = 0;
    expect(inspectVisibleH3Graph(duplicateImageSlot, manifest)).toMatchObject({
      status: "incompatible",
    });
    const extraDefinition = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const qualified = extraDefinition.definitions.subgraphs[0];
    if (qualified === undefined) throw new Error("fixture drift");
    extraDefinition.definitions.subgraphs.push({
      ...structuredClone(qualified),
      id: "unreferenced-qualified-definition",
    });
    expect(isQualifiedExternalReferenceGraph(extraDefinition)).toBe(false);
    const privateDefinition = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    privateDefinition.definitions.subgraphs[0]!.private_media =
      "file:///secret";
    expect(() => inspectVisibleH3Graph(privateDefinition, manifest)).toThrow(
      /unknown serialized member/,
    );
    const privateDefinitionsRoot = structuredClone(referenceAssistant) as {
      definitions: Record<string, unknown>;
    };
    privateDefinitionsRoot.definitions.private_payload = "unexpected";
    expect(() =>
      inspectVisibleH3Graph(privateDefinitionsRoot, manifest),
    ).toThrow(/unknown serialized member/);
    const extraRoot = structuredClone(referenceAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    extraRoot.nodes.push({ id: 99, type: "Foreign.Node" });
    expect(isQualifiedExternalReferenceGraph(extraRoot)).toBe(false);
    const hiddenWrapperPort = structuredClone(referenceAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    (hiddenWrapperPort.nodes[0]!.inputs as Array<Record<string, unknown>>).push(
      { name: "secret", type: "STRING", link: null },
    );
    expect(inspectVisibleH3Graph(hiddenWrapperPort, manifest)).toMatchObject({
      status: "incompatible",
    });
    const privateWorkflow = structuredClone(referenceAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    privateWorkflow.nodes[0]!._nodes = [];
    expect(isQualifiedExternalReferenceGraph(privateWorkflow)).toBe(false);
    const linkedWorkflow = structuredClone(referenceAssistant) as {
      links?: unknown;
    };
    linkedWorkflow.links = [[1, 10, 0, 10, 0, "FOREIGN"]];
    expect(isQualifiedExternalReferenceGraph(linkedWorkflow)).toBe(false);
    const unnamedNestedPort = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const nestedRequest = unnamedNestedPort.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(nestedRequest)) throw new Error("fixture drift");
    const requestNode = nestedRequest.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (requestNode === undefined) throw new Error("fixture drift");
    (requestNode.inputs as Array<Record<string, unknown>>).push({
      type: "STRING",
      link: null,
    });
    expect(inspectVisibleH3Graph(unnamedNestedPort, manifest)).toMatchObject({
      status: "incompatible",
    });
    const unnamedWrapperPort = structuredClone(referenceAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    (
      unnamedWrapperPort.nodes[0]!.inputs as Array<Record<string, unknown>>
    ).push({ type: "STRING", link: null });
    expect(inspectVisibleH3Graph(unnamedWrapperPort, manifest)).toMatchObject({
      status: "incompatible",
    });
  });

  it("accepts the qualified Base Assistant subgraph as a bindable graph", () => {
    expect(inspectVisibleH3Graph(baseAssistant, manifest)).toMatchObject({
      status: "ready",
      anchors: [{ executionId: "6:8" }],
      existingGraphCompatible: true,
    });
    const namedWidgetMirror = structuredClone(baseAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    const namedWrapper = namedWidgetMirror.nodes[0];
    if (
      namedWrapper === undefined ||
      !Array.isArray(namedWrapper.widgets_values)
    )
      throw new Error("fixture drift");
    namedWrapper.widgets_values_named = {
      task_mode: namedWrapper.widgets_values[0],
      user_intent: namedWrapper.widgets_values[1],
      duration_seconds: namedWrapper.widgets_values[2],
    };
    const namedDefinitions = (
      namedWidgetMirror as unknown as {
        definitions: { subgraphs: Array<Record<string, unknown>> };
      }
    ).definitions.subgraphs;
    const namedDefinitionNodes = namedDefinitions[0]?.nodes;
    if (!Array.isArray(namedDefinitionNodes)) throw new Error("fixture drift");
    const namedRequest = namedDefinitionNodes.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (
      namedRequest === undefined ||
      !Array.isArray(namedRequest.widgets_values)
    )
      throw new Error("fixture drift");
    namedRequest.widgets_values_named = {
      task_mode: namedRequest.widgets_values[0],
      user_intent: namedRequest.widgets_values[1],
      duration_seconds: namedRequest.widgets_values[2],
    };
    expect(inspectVisibleH3Graph(namedWidgetMirror, manifest)).toMatchObject({
      status: "ready",
      anchors: [{ executionId: "6:8" }],
      existingGraphCompatible: true,
    });
    const reorderedNamedWrapper = structuredClone(namedWidgetMirror) as {
      nodes: Array<Record<string, unknown>>;
    };
    const reorderedWrapper = reorderedNamedWrapper.nodes[0];
    if (
      reorderedWrapper === undefined ||
      !Array.isArray(reorderedWrapper.widgets_values)
    )
      throw new Error("fixture drift");
    reorderedWrapper.widgets_values_named = {
      user_intent: reorderedWrapper.widgets_values[1],
      task_mode: reorderedWrapper.widgets_values[0],
      duration_seconds: reorderedWrapper.widgets_values[2],
    };
    expect(
      inspectVisibleH3Graph(reorderedNamedWrapper, manifest),
    ).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_wrapper_widgets_named_0",
    });
    const wrongNamedNestedRequest = structuredClone(
      namedWidgetMirror,
    ) as unknown as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const wrongNamedNestedNodes =
      wrongNamedNestedRequest.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(wrongNamedNestedNodes)) throw new Error("fixture drift");
    const wrongNamedRequest = wrongNamedNestedNodes.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (
      wrongNamedRequest === undefined ||
      !Array.isArray(wrongNamedRequest.widgets_values)
    )
      throw new Error("fixture drift");
    wrongNamedRequest.widgets_values_named = {
      mode: wrongNamedRequest.widgets_values[0],
      intent: wrongNamedRequest.widgets_values[1],
      duration: wrongNamedRequest.widgets_values[2],
    };
    expect(
      inspectVisibleH3Graph(wrongNamedNestedRequest, manifest),
    ).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
      reason: "malformed_subgraph_definition",
    });
    const inconsistentNamedWidgetMirror = structuredClone(
      namedWidgetMirror,
    ) as { nodes: Array<Record<string, unknown>> };
    const inconsistentNamedWrapper = inconsistentNamedWidgetMirror.nodes[0];
    if (
      inconsistentNamedWrapper === undefined ||
      inconsistentNamedWrapper.widgets_values_named === null ||
      typeof inconsistentNamedWrapper.widgets_values_named !== "object" ||
      Array.isArray(inconsistentNamedWrapper.widgets_values_named)
    )
      throw new Error("fixture drift");
    (
      inconsistentNamedWrapper.widgets_values_named as Record<string, unknown>
    ).duration_seconds = 6;
    expect(
      inspectVisibleH3Graph(inconsistentNamedWidgetMirror, manifest),
    ).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_wrapper_widgets_named_0",
    });
    const extraBaseDefinition = structuredClone(baseAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const baseDefinition = extraBaseDefinition.definitions.subgraphs[0];
    if (baseDefinition === undefined) throw new Error("fixture drift");
    extraBaseDefinition.definitions.subgraphs.push({
      ...structuredClone(baseDefinition),
      id: "unreferenced-base-definition",
    });
    expect(inspectVisibleH3Graph(extraBaseDefinition, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const wrongBoundaryEdge = structuredClone(baseAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const baseLinks = wrongBoundaryEdge.definitions.subgraphs[0]?.links;
    if (!Array.isArray(baseLinks)) throw new Error("fixture drift");
    const promptBoundary = baseLinks.find(
      (link) =>
        (link as Record<string, unknown>).target_id === -20 &&
        (link as Record<string, unknown>).target_slot === 0,
    ) as Record<string, unknown> | undefined;
    if (promptBoundary === undefined) throw new Error("fixture drift");
    promptBoundary.type = "FOREIGN";
    expect(inspectVisibleH3Graph(wrongBoundaryEdge, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const unnamedWrapperPorts = structuredClone(baseAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    const wrapper = unnamedWrapperPorts.nodes[0];
    if (wrapper === undefined) throw new Error("fixture drift");
    const wrapperInputs = wrapper.inputs;
    const wrapperOutputs = wrapper.outputs;
    if (!Array.isArray(wrapperInputs) || !Array.isArray(wrapperOutputs))
      throw new Error("fixture drift");
    wrapper.inputs = wrapperInputs.map(() => ({ link: null }));
    wrapper.outputs = wrapperOutputs.map(() => ({ links: [] }));
    expect(inspectVisibleH3Graph(unnamedWrapperPorts, manifest)).toMatchObject({
      status: "incompatible",
    });
    const wrongWrapperWidget = structuredClone(baseAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    const wrongWrapper = wrongWrapperWidget.nodes[0];
    if (wrongWrapper === undefined || !Array.isArray(wrongWrapper.inputs))
      throw new Error("fixture drift");
    const firstWrapperInput = wrongWrapper.inputs[0] as Record<string, unknown>;
    firstWrapperInput.widget = { name: "unowned_secret" };
    expect(inspectVisibleH3Graph(wrongWrapperWidget, manifest)).toMatchObject({
      status: "incompatible",
    });
    const extraWrapperWidget = structuredClone(baseAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    const extraWrapper = extraWrapperWidget.nodes[0];
    if (extraWrapper === undefined || !Array.isArray(extraWrapper.inputs))
      throw new Error("fixture drift");
    const extraWrapperInput = extraWrapper.inputs[0] as Record<string, unknown>;
    extraWrapperInput.widget = { name: "task_mode", private: "hidden" };
    expect(inspectVisibleH3Graph(extraWrapperWidget, manifest)).toMatchObject({
      status: "incompatible",
    });
    const privateWrapperMember = structuredClone(baseAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    const privateWrapper = privateWrapperMember.nodes[0];
    if (privateWrapper === undefined) throw new Error("fixture drift");
    privateWrapper.private_media = "file:///secret";
    expect(inspectVisibleH3Graph(privateWrapperMember, manifest)).toMatchObject(
      {
        status: "incompatible",
      },
    );
    const foreignWrapperType = structuredClone(referenceAssistant) as {
      nodes: Array<Record<string, unknown>>;
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const referenceWrapper = foreignWrapperType.nodes[0];
    if (referenceWrapper === undefined) throw new Error("fixture drift");
    referenceWrapper.type = "Foreign.Wrapper";
    referenceWrapper.subgraph_id =
      foreignWrapperType.definitions.subgraphs[0]?.id;
    expect(inspectVisibleH3Graph(foreignWrapperType, manifest)).toMatchObject({
      status: "incompatible",
    });
    const wrongWrapperMode = structuredClone(baseAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    const wrongWrapperModeNode = wrongWrapperMode.nodes[0];
    if (wrongWrapperModeNode === undefined) throw new Error("fixture drift");
    wrongWrapperModeNode.widgets_values = [
      "ref2va",
      "A red kite crosses the sky while the camera follows its arc.",
      124,
    ];
    expect(inspectVisibleH3Graph(wrongWrapperMode, manifest)).toMatchObject({
      status: "incompatible",
    });
    const extraWrapperValue = structuredClone(baseAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    const extraWrapperValueNode = extraWrapperValue.nodes[0];
    if (extraWrapperValueNode === undefined) throw new Error("fixture drift");
    extraWrapperValueNode.widgets_values = [
      "t2va",
      "A red kite crosses the sky while the camera follows its arc.",
      124,
      "hidden",
    ];
    expect(inspectVisibleH3Graph(extraWrapperValue, manifest)).toMatchObject({
      status: "incompatible",
    });
    const invalidWrapperWidgetValues = structuredClone(baseAssistant) as {
      nodes: Array<Record<string, unknown>>;
    };
    const invalidWrapperNode = invalidWrapperWidgetValues.nodes[0];
    if (invalidWrapperNode === undefined) throw new Error("fixture drift");
    invalidWrapperNode.widgets_values = ["t2va", "", 1];
    expect(
      inspectVisibleH3Graph(invalidWrapperWidgetValues, manifest),
    ).toMatchObject({
      status: "incompatible",
    });
    const invalidNestedWidgetValues = structuredClone(baseAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const invalidNestedNodes =
      invalidNestedWidgetValues.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(invalidNestedNodes)) throw new Error("fixture drift");
    const invalidNestedRequest = invalidNestedNodes.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    const invalidNestedImage = invalidNestedNodes.find(
      (node) =>
        (node as Record<string, unknown>).type === "MiniMaxH3ImageToVideo",
    ) as Record<string, unknown> | undefined;
    if (invalidNestedRequest === undefined || invalidNestedImage === undefined)
      throw new Error("fixture drift");
    invalidNestedRequest.widgets_values = ["t2va", "", 1];
    expect(
      inspectVisibleH3Graph(invalidNestedWidgetValues, manifest),
    ).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    invalidNestedRequest.widgets_values = [
      "t2va",
      "A red kite crosses the sky while the camera follows its arc.",
      124,
    ];
    invalidNestedImage.widgets_values = [1, 512, 124];
    expect(
      inspectVisibleH3Graph(invalidNestedWidgetValues, manifest),
    ).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const wrongWidgetName = structuredClone(baseAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const baseNodes = wrongWidgetName.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(baseNodes)) throw new Error("fixture drift");
    const requestNode = baseNodes.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (requestNode === undefined) throw new Error("fixture drift");
    const requestInputs = requestNode.inputs as Array<Record<string, unknown>>;
    const widget = requestInputs[0]?.widget as Record<string, unknown>;
    widget.name = "unowned_secret";
    expect(inspectVisibleH3Graph(wrongWidgetName, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const extraWidgetValue = structuredClone(baseAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const extraNodes = extraWidgetValue.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(extraNodes)) throw new Error("fixture drift");
    const extraRequest = extraNodes.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (extraRequest === undefined) throw new Error("fixture drift");
    extraRequest.widgets_values = [
      ...(extraRequest.widgets_values as unknown[]),
      "hidden",
    ];
    expect(inspectVisibleH3Graph(extraWidgetValue, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const privateNodeMember = structuredClone(baseAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const privateNodes = privateNodeMember.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(privateNodes)) throw new Error("fixture drift");
    const privateRequest = privateNodes.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (privateRequest === undefined) throw new Error("fixture drift");
    privateRequest.private_media = "file:///secret";
    expect(inspectVisibleH3Graph(privateNodeMember, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const wrongTaskMode = structuredClone(baseAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const wrongModeNodes = wrongTaskMode.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(wrongModeNodes)) throw new Error("fixture drift");
    const wrongModeRequest = wrongModeNodes.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Request",
    ) as Record<string, unknown> | undefined;
    if (wrongModeRequest === undefined) throw new Error("fixture drift");
    wrongModeRequest.widgets_values = [
      "ref2va",
      "A red kite crosses the sky while the camera follows its arc.",
      124,
    ];
    expect(inspectVisibleH3Graph(wrongTaskMode, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const extraBasePlanInput = structuredClone(baseAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const basePlan = extraBasePlanInput.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(basePlan)) throw new Error("fixture drift");
    const basePlanNode = basePlan.find(
      (node) =>
        (node as Record<string, unknown>).type ===
        "comfyui_h3_context.H3Context.Plan",
    ) as Record<string, unknown> | undefined;
    if (basePlanNode === undefined) throw new Error("fixture drift");
    const basePlanInput = basePlanNode.inputs;
    basePlanNode.inputs = [
      basePlanInput,
      { name: "reference_registry", type: "H3_REFERENCE_REGISTRY", link: null },
    ];
    expect(inspectVisibleH3Graph(extraBasePlanInput, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const extraReferenceInput = structuredClone(referenceAssistant) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const referenceNodes = extraReferenceInput.definitions.subgraphs[0]?.nodes;
    if (!Array.isArray(referenceNodes)) throw new Error("fixture drift");
    const referenceGeneration = referenceNodes.find(
      (node) =>
        (node as Record<string, unknown>).type === "MiniMaxH3ReferenceToVideo",
    ) as Record<string, unknown> | undefined;
    if (referenceGeneration === undefined) throw new Error("fixture drift");
    const referenceInputs = referenceGeneration.inputs;
    if (!Array.isArray(referenceInputs)) throw new Error("fixture drift");
    referenceInputs.push({
      name: "ref_video_audios.video_audio0",
      type: "AUDIO",
      link: null,
    });
    expect(inspectVisibleH3Graph(extraReferenceInput, manifest)).toMatchObject({
      status: "incompatible",
    });
  });

  it("enforces the exact ProductShell socket schema in nested assistants", () => {
    const optionalInputs = [
      { name: "recompute_plan", type: "H3_RECOMPUTE_PLAN", link: null },
      {
        name: "pipeline_transaction",
        type: "H3_PIPELINE_TRANSACTION",
        link: null,
      },
      {
        name: "generation_sequence_state",
        type: "H3_GENERATION_SEQUENCE_STATE",
        link: null,
      },
      {
        name: "semantic_proposal_review_authority",
        type: "H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY",
        link: null,
      },
    ];
    const nestedShell = (fixture: unknown): Record<string, unknown> => {
      const definition = (
        fixture as {
          definitions: { subgraphs: Array<{ nodes?: unknown }> };
        }
      ).definitions.subgraphs[0];
      if (definition === undefined || !Array.isArray(definition.nodes))
        throw new Error("fixture drift");
      const shell = definition.nodes.find(
        (node) =>
          (node as Record<string, unknown>).type ===
          "comfyui_h3_context.H3Context.ProductShell",
      ) as Record<string, unknown> | undefined;
      if (shell === undefined || !Array.isArray(shell.inputs))
        throw new Error("fixture drift");
      return shell;
    };
    const withOptionalInputs = (fixture: unknown) => {
      const candidate = structuredClone(fixture);
      const shell = nestedShell(candidate);
      shell.inputs = [
        ...(shell.inputs as Array<Record<string, unknown>>),
        ...structuredClone(optionalInputs),
      ];
      return candidate;
    };
    const expectRejected = (candidate: unknown) => {
      expect(inspectVisibleH3Graph(candidate, manifest)).not.toMatchObject({
        status: "ready",
        existingGraphCompatible: true,
      });
    };

    for (const fixture of [baseAssistant, referenceAssistant]) {
      const orderedSubset = structuredClone(fixture);
      const subsetShell = nestedShell(orderedSubset);
      subsetShell.inputs = [
        ...(subsetShell.inputs as Array<Record<string, unknown>>),
        structuredClone(optionalInputs[1]),
        structuredClone(optionalInputs[3]),
      ];
      expect(inspectVisibleH3Graph(orderedSubset, manifest)).toMatchObject({
        status: "ready",
        existingGraphCompatible: true,
      });

      const exact = withOptionalInputs(fixture);
      expect(inspectVisibleH3Graph(exact, manifest)).toMatchObject({
        status: "ready",
        existingGraphCompatible: true,
      });

      const seventh = withOptionalInputs(fixture);
      (nestedShell(seventh).inputs as Array<Record<string, unknown>>).push({
        name: "unknown_authority",
        type: "H3_UNKNOWN_AUTHORITY",
        link: null,
      });
      expectRejected(seventh);

      const wrongType = withOptionalInputs(fixture);
      const wrongTypeInputs = nestedShell(wrongType).inputs as Array<
        Record<string, unknown>
      >;
      wrongTypeInputs[5]!.type = "STRING";
      expectRejected(wrongType);

      const duplicate = withOptionalInputs(fixture);
      const duplicateInputs = nestedShell(duplicate).inputs as Array<
        Record<string, unknown>
      >;
      duplicateInputs[5] = structuredClone(duplicateInputs[4]);
      expectRejected(duplicate);

      const reordered = withOptionalInputs(fixture);
      const reorderedInputs = nestedShell(reordered).inputs as Array<
        Record<string, unknown>
      >;
      [reorderedInputs[2], reorderedInputs[3]] = [
        reorderedInputs[3],
        reorderedInputs[2],
      ];
      expectRejected(reordered);
    }
  });

  it("accepts the serialized ComfyUI direct Base graph with prompt slot 4", () => {
    const graph = directBaseGraph();
    expect(inspectVisibleH3Graph(graph, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });
    const wrongNamedDirectRequest = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const wrongDirectRequest = wrongNamedDirectRequest.nodes[0];
    if (
      wrongDirectRequest === undefined ||
      !Array.isArray(wrongDirectRequest.widgets_values)
    )
      throw new Error("fixture drift");
    wrongDirectRequest.widgets_values_named = {
      mode: wrongDirectRequest.widgets_values[0],
      intent: wrongDirectRequest.widgets_values[1],
      duration: wrongDirectRequest.widgets_values[2],
    };
    expect(
      inspectVisibleH3Graph(wrongNamedDirectRequest, manifest),
    ).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_widgets_named_0",
    });
    const expectHiddenNamedRequestValueRejected = (
      mutate: (subject: {
        request: Record<string, unknown>;
        named: Record<PropertyKey, unknown>;
      }) => void,
    ): void => {
      const subject = structuredClone(graph) as {
        nodes: Array<Record<string, unknown>>;
      };
      const request = subject.nodes[0];
      if (request === undefined || !Array.isArray(request.widgets_values))
        throw new Error("fixture drift");
      const named: Record<PropertyKey, unknown> = {
        task_mode: request.widgets_values[0],
        user_intent: request.widgets_values[1],
        duration_seconds: request.widgets_values[2],
      };
      request.widgets_values_named = named;
      mutate({ request, named });
      expect(inspectVisibleH3Graph(subject, manifest)).toMatchObject({
        status: "incompatible",
        reason: "invalid_serialized_widgets_named_0",
      });
    };
    expectHiddenNamedRequestValueRejected(({ named }) => {
      Object.defineProperty(named, Symbol("hidden"), {
        value: Number.NaN,
        enumerable: true,
      });
    });
    expectHiddenNamedRequestValueRejected(({ named }) => {
      Object.defineProperty(named, "hidden_function", {
        value: () => undefined,
        enumerable: false,
      });
    });
    expectHiddenNamedRequestValueRejected(({ named }) => {
      const cycle: Record<string, unknown> = {};
      cycle.self = cycle;
      Object.defineProperty(named, "hidden_cycle", {
        value: cycle,
        enumerable: false,
      });
    });
    expectHiddenNamedRequestValueRejected(({ request, named }) => {
      const hostileArray = ["safe"];
      Object.setPrototypeOf(hostileArray, { hidden: true });
      (request.widgets_values as unknown[])[1] = hostileArray;
      named.user_intent = hostileArray;
    });
    const oversizedNamedDirectRequest = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const oversizedDirectRequest = oversizedNamedDirectRequest.nodes[0];
    if (oversizedDirectRequest === undefined) throw new Error("fixture drift");
    const oversizedIntent = "x".repeat(65_537);
    oversizedDirectRequest.widgets_values = ["t2va", oversizedIntent, 5.167];
    oversizedDirectRequest.widgets_values_named = {
      task_mode: "t2va",
      user_intent: oversizedIntent,
      duration_seconds: 5.167,
    };
    expect(
      inspectVisibleH3Graph(oversizedNamedDirectRequest, manifest),
    ).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_widgets_named_0",
    });
    const tooManyNamedGenerationWidgets = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const tooManyGeneration = tooManyNamedGenerationWidgets.nodes[6];
    if (tooManyGeneration === undefined) throw new Error("fixture drift");
    tooManyGeneration.widgets_values = Array.from({ length: 65 }, () => 0);
    tooManyGeneration.widgets_values_named = Object.fromEntries(
      Array.from({ length: 65 }, (_, index) => [`widget_${index}`, 0]),
    );
    expect(
      inspectVisibleH3Graph(tooManyNamedGenerationWidgets, manifest),
    ).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_widgets_named_6",
    });
    const privateGraph = structuredClone(graph) as Record<string, unknown>;
    privateGraph.private_media = "file:///secret";
    expect(() => inspectVisibleH3Graph(privateGraph, manifest)).toThrow(
      /unknown serialized member/,
    );
    const supportedHostGraph: Record<string, unknown> = {
      ...structuredClone(graph),
      id: "7dd805d5-53f8-41ec-8d8c-0f3f4fced8fe",
      floatingLinks: undefined,
    };
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });
    const supportedNodes = supportedHostGraph.nodes as Array<
      Record<string, unknown>
    >;
    supportedNodes[0]!.showAdvanced = false;
    const requestWidgets = supportedNodes[0]!.widgets_values as unknown[];
    // M17-25: the Request node carries exactly one optional scalar widget, the
    // authored duration in seconds. Zero remains the host loader sentinel for an
    // unset optional widget, so it stays bindable; a value outside the accepted
    // authored range does not, and a fourth widget value is no longer a shape the
    // node can produce at all.
    requestWidgets[2] = 0;
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });
    requestWidgets[2] = 1000;
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_widgets_0",
    });
    requestWidgets[2] = 5.167;
    requestWidgets.push(124);
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_widgets_0",
    });
    requestWidgets.pop();
    const generation = supportedNodes.find(
      (node) => node.type === "MiniMaxH3ImageToVideo",
    );
    if (generation === undefined) throw new Error("fixture drift");
    const generationWidgets = generation.widgets_values as unknown[];
    generationWidgets.unshift("");
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });
    generationWidgets[0] = "hidden prompt";
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_widgets_6",
    });
    generationWidgets.shift();
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });
    supportedNodes[0]!.showAdvanced = "false";
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_node_0",
    });
    supportedNodes[0]!.showAdvanced = false;
    supportedHostGraph.floatingLinks = [];
    expect(inspectVisibleH3Graph(supportedHostGraph, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });
    expect(() =>
      inspectVisibleH3Graph(
        {
          ...supportedHostGraph,
          floatingLinks: [
            {
              id: 1,
              origin_id: 8,
              origin_slot: 0,
              target_id: -1,
              target_slot: -1,
              type: "STRING",
            },
          ],
        },
        manifest,
      ),
    ).toThrow(/unsupported floating links/);
    const unreferencedDefinition = structuredClone(graph) as Record<
      string,
      unknown
    >;
    unreferencedDefinition.definitions = structuredClone(
      (baseAssistant as { definitions: unknown }).definitions,
    );
    expect(
      inspectVisibleH3Graph(unreferencedDefinition, manifest),
    ).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const optionalSockets = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const optionalRequest = optionalSockets.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    const optionalPlan = optionalSockets.nodes.find(
      (node) => node.type === canonicalTypes[1],
    );
    const optionalShell = optionalSockets.nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.ProductShell",
    );
    if (
      optionalRequest === undefined ||
      optionalPlan === undefined ||
      optionalShell === undefined
    )
      throw new Error("fixture drift");
    (optionalRequest.inputs as Array<Record<string, unknown>>).push(
      { name: "duration_seconds", type: "FLOAT", link: null },
      { name: "hard_constraints", type: "H3_HARD_CONSTRAINTS", link: null },
    );
    (optionalPlan.inputs as Array<Record<string, unknown>>).push({
      name: "intent_graph",
      type: "H3_INTENT_GRAPH",
      link: null,
    });
    (optionalShell.inputs as Array<Record<string, unknown>>).push(
      { name: "recompute_plan", type: "H3_RECOMPUTE_PLAN", link: null },
      {
        name: "pipeline_transaction",
        type: "H3_PIPELINE_TRANSACTION",
        link: null,
      },
      {
        name: "generation_sequence_state",
        type: "H3_GENERATION_SEQUENCE_STATE",
        link: null,
      },
      {
        name: "semantic_proposal_review_authority",
        type: "H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY",
        link: null,
      },
    );
    expect(inspectVisibleH3Graph(optionalSockets, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });
    const extraProductShellSocket = structuredClone(optionalSockets) as {
      nodes: Array<Record<string, unknown>>;
    };
    const extraProductShell = extraProductShellSocket.nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.ProductShell",
    );
    if (extraProductShell === undefined) throw new Error("fixture drift");
    (extraProductShell.inputs as Array<Record<string, unknown>>).push({
      name: "unknown_authority",
      type: "H3_UNKNOWN_AUTHORITY",
      link: null,
    });
    expect(
      inspectVisibleH3Graph(extraProductShellSocket, manifest),
    ).toMatchObject({
      status: "incompatible",
      reason: expect.stringMatching(/^extra_serialized_input_\d+$/),
    });
    const unnamedExtraSocket = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const unnamedRequest = unnamedExtraSocket.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    if (unnamedRequest === undefined) throw new Error("fixture drift");
    (unnamedRequest.inputs as Array<Record<string, unknown>>).push({
      link: null,
    });
    expect(inspectVisibleH3Graph(unnamedExtraSocket, manifest)).toMatchObject({
      status: "incompatible",
      reason: "malformed_serialized_input_0",
    });
    const malformedWidgets = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const malformedRequest = malformedWidgets.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    if (malformedRequest === undefined) throw new Error("fixture drift");
    malformedRequest.widgets_values = ["t2va", "safe"];
    expect(inspectVisibleH3Graph(malformedWidgets, manifest)).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_widgets_0",
    });
    const malformedGeneration = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const malformedImage = malformedGeneration.nodes.find(
      (node) => node.type === canonicalTypes[6],
    );
    if (malformedImage === undefined) throw new Error("fixture drift");
    malformedImage.widgets_values = [1, 1, 1];
    expect(inspectVisibleH3Graph(malformedGeneration, manifest)).toMatchObject({
      status: "incompatible",
      reason: "invalid_serialized_widgets_6",
    });
    const missingDirectRequestInputs = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const missingRequest = missingDirectRequestInputs.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    if (missingRequest === undefined) throw new Error("fixture drift");
    delete missingRequest.inputs;
    expect(
      inspectVisibleH3Graph(missingDirectRequestInputs, manifest),
    ).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
      reason: "malformed_serialized_request_contract",
    });
    const mismatchedDirectMode = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const mismatchedRequest = mismatchedDirectMode.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    if (mismatchedRequest === undefined) throw new Error("fixture drift");
    mismatchedRequest.widgets_values = ["ref2va", "safe", 124];
    expect(inspectVisibleH3Graph(mismatchedDirectMode, manifest)).toMatchObject(
      {
        status: "ready",
        existingGraphCompatible: false,
        reason: "mismatched_serialized_task_mode",
      },
    );
    const extraCanonicalEdge = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
      links: Array<unknown[]>;
    };
    const extraRequest = extraCanonicalEdge.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    const extraPlan = extraCanonicalEdge.nodes.find(
      (node) => node.type === canonicalTypes[1],
    );
    if (extraRequest === undefined || extraPlan === undefined)
      throw new Error("fixture drift");
    (extraRequest.outputs as Array<Record<string, unknown>>)[0]!.links = [
      ...((extraRequest.outputs as Array<Record<string, unknown>>)[0]!
        .links as number[]),
      19,
    ];
    (extraPlan.inputs as Array<Record<string, unknown>>)[1]!.link = 19;
    extraCanonicalEdge.links.push([
      19,
      extraRequest.id,
      0,
      extraPlan.id,
      1,
      "H3_CONTEXT_REQUEST",
    ]);
    expect(inspectVisibleH3Graph(extraCanonicalEdge, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    const wrongPositionalOutput = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const requestOutput = wrongPositionalOutput.nodes.find(
      (node) => node.type === canonicalTypes[0],
    )?.outputs as Array<Record<string, unknown>> | undefined;
    if (requestOutput === undefined) throw new Error("fixture drift");
    requestOutput[0]!.type = "FOREIGN";
    expect(
      inspectVisibleH3Graph(wrongPositionalOutput, manifest),
    ).toMatchObject({ status: "incompatible" });
    const hiddenPort = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const hiddenRequest = hiddenPort.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    if (hiddenRequest === undefined) throw new Error("fixture drift");
    (hiddenRequest.inputs as Array<Record<string, unknown>>).push({
      name: "unowned_secret",
      type: "STRING",
      link: null,
    });
    expect(inspectVisibleH3Graph(hiddenPort, manifest)).toMatchObject({
      status: "incompatible",
    });
    const typedPositionalPort = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const typedRequest = typedPositionalPort.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    if (typedRequest === undefined) throw new Error("fixture drift");
    typedRequest.inputs = [{ type: "FOREIGN", link: null }];
    expect(inspectVisibleH3Graph(typedPositionalPort, manifest)).toMatchObject({
      status: "incompatible",
    });
    const hiddenPortMember = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const hiddenMemberRequest = hiddenPortMember.nodes.find(
      (node) => node.type === canonicalTypes[0],
    );
    if (hiddenMemberRequest === undefined) throw new Error("fixture drift");
    const hiddenMemberInput = (
      hiddenMemberRequest.inputs as Array<Record<string, unknown>>
    )[0];
    if (hiddenMemberInput === undefined) throw new Error("fixture drift");
    hiddenMemberInput.private_media = "file:///secret";
    expect(inspectVisibleH3Graph(hiddenPortMember, manifest)).toMatchObject({
      status: "incompatible",
    });
    const hiddenOutputMember = structuredClone(graph) as {
      nodes: Array<Record<string, unknown>>;
    };
    const hiddenMemberPlan = hiddenOutputMember.nodes.find(
      (node) => node.type === canonicalTypes[1],
    );
    if (hiddenMemberPlan === undefined) throw new Error("fixture drift");
    const hiddenMemberOutput = (
      hiddenMemberPlan.outputs as Array<Record<string, unknown>>
    )[0];
    if (hiddenMemberOutput === undefined) throw new Error("fixture drift");
    hiddenMemberOutput.private_payload = "provider payload";
    expect(inspectVisibleH3Graph(hiddenOutputMember, manifest)).toMatchObject({
      status: "incompatible",
    });
    const hiddenLinkMember = structuredClone(graph) as {
      links: Array<unknown[]>;
    };
    hiddenLinkMember.links[0]!.push({ private_payload: "provider payload" });
    expect(inspectVisibleH3Graph(hiddenLinkMember, manifest)).toMatchObject({
      status: "incompatible",
    });
  });

  it("requires exactly one native generation anchor and strict serialized links for binding", () => {
    const noGeneration = canonicalGraph().nodes.filter(
      (node) => node.type !== "MiniMaxH3ImageToVideo",
    );
    expect(
      inspectVisibleH3Graph({ nodes: noGeneration }, manifest),
    ).toMatchObject({
      status: "incompatible",
      reason: "incomplete_h3_flow",
    });
    const withEmptyLinks = inspectVisibleH3Graph(
      { ...canonicalGraph(), links: [] },
      manifest,
    );
    expect(withEmptyLinks).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });
    expect(
      inspectVisibleH3Graph(
        {
          ...canonicalGraph(),
          links: [[1, 10, 0, 11, 0, "H3"]],
        },
        manifest,
      ),
    ).toMatchObject({ status: "incompatible" });
  });

  it("does not mark a disconnected canonical node set as queue-compatible", () => {
    const nodes = canonicalGraph().nodes.map((node) => ({
      ...node,
    })) as Array<Record<string, unknown>>;
    const request = nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Request",
    );
    const plan = nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Plan",
    );
    if (request === undefined || plan === undefined)
      throw new Error("fixture drift");
    request.outputs = [{ links: [1] }];
    plan.inputs = [{ link: 1 }];
    expect(
      inspectVisibleH3Graph(
        {
          nodes,
          links: [[1, request.id, 0, plan.id, 0, "H3_CONTEXT_REQUEST"]],
        },
        manifest,
      ),
    ).toMatchObject({ status: "ready", existingGraphCompatible: false });
  });

  it("accepts a canonical parent graph wrapped by the one-node Product Shell Boundary", () => {
    const parentTypes = canonicalTypes.map((type, index) => ({
      id: index + 10,
      type,
      inputs: [] as Array<Record<string, unknown>>,
      outputs: [] as Array<Record<string, unknown>>,
    }));
    const shell = {
      id: 17,
      type: "boundary-definition",
      inputs: [
        { name: "report", type: "H3_CONTEXT_REPORT", link: null },
        {
          name: "native_h3_wiring",
          type: "H3_NATIVE_H3_WIRING",
          link: null,
        },
      ] as Array<Record<string, unknown>>,
      outputs: [
        { name: "prompt", type: "STRING", links: [] },
        { name: "product_shell", type: "H3_PRODUCT_SHELL", links: [] },
      ] as Array<Record<string, unknown>>,
    };
    const nodes = [...parentTypes, shell];
    const byType = new Map(nodes.map((node) => [node.type, node.id]));
    const links = [
      [
        1,
        byType.get(canonicalTypes[0]),
        0,
        byType.get(canonicalTypes[1]),
        0,
        "H3_CONTEXT_REQUEST",
      ],
      [
        2,
        byType.get(canonicalTypes[1]),
        0,
        byType.get(canonicalTypes[2]),
        0,
        "H3_CONTEXT_PLAN",
      ],
      [
        3,
        byType.get(canonicalTypes[1]),
        0,
        byType.get(canonicalTypes[3]),
        0,
        "H3_CONTEXT_PLAN",
      ],
      [
        4,
        byType.get(canonicalTypes[2]),
        2,
        byType.get(canonicalTypes[3]),
        1,
        "H3_PROMPT_DOCUMENT",
      ],
      [
        5,
        byType.get(canonicalTypes[3]),
        1,
        byType.get(canonicalTypes[4]),
        0,
        "H3_CONTEXT_REPORT",
      ],
      [
        6,
        byType.get(canonicalTypes[3]),
        1,
        byType.get(canonicalTypes[5]),
        0,
        "H3_CONTEXT_REPORT",
      ],
      [7, byType.get(canonicalTypes[3]), 1, shell.id, 0, "H3_CONTEXT_REPORT"],
      [8, byType.get(canonicalTypes[4]), 1, shell.id, 1, "H3_NATIVE_H3_WIRING"],
      // Serialized ComfyUI native nodes expose the prompt socket at slot 4.
      [9, shell.id, 0, byType.get(canonicalTypes[6]), 4, "STRING"],
    ] as Array<
      [number, number | undefined, number, number | undefined, number, string]
    >;
    for (const [id, originId, originSlot, targetId, targetSlot] of links) {
      const origin = nodes.find((node) => node.id === originId);
      const target = nodes.find((node) => node.id === targetId);
      if (origin === undefined || target === undefined)
        throw new Error("fixture drift");
      while (origin.outputs.length <= originSlot)
        origin.outputs.push({ links: [] });
      while (target.inputs.length <= targetSlot)
        target.inputs.push({ link: null });
      (origin.outputs[originSlot] ??= {}).links = [
        ...(((origin.outputs[originSlot] ?? {}).links as
          number[] | undefined) ?? []),
        id,
      ];
      (target.inputs[targetSlot] ??= {}).link = id;
    }
    const boundaryDefinition = {
      id: "boundary-definition",
      inputNode: { id: -10, bounding: [0, 0, 180, 100] },
      outputNode: { id: -20, bounding: [900, 0, 140, 100] },
      inputs: [
        {
          id: "report-port",
          name: "report",
          type: "H3_CONTEXT_REPORT",
          linkIds: [1],
          color_off: "#000000",
          color_on: "#ffffff",
          dir: 3,
          shape: 1,
          pos: [0, 0],
        },
        {
          name: "native_h3_wiring",
          type: "H3_NATIVE_H3_WIRING",
          linkIds: [2],
        },
      ],
      outputs: [
        { name: "prompt", type: "STRING", linkIds: [3] },
        { name: "product_shell", type: "H3_PRODUCT_SHELL", linkIds: [4] },
      ],
      reroutes: [],
      nodes: [
        {
          id: 9,
          type: "comfyui_h3_context.H3Context.ProductShell",
          inputs: [
            { name: "report", type: "H3_CONTEXT_REPORT", link: 1 },
            {
              name: "native_h3_wiring",
              type: "H3_NATIVE_H3_WIRING",
              link: 2,
            },
          ],
          outputs: [
            { name: "prompt", type: "STRING", links: [3] },
            {
              name: "product_shell",
              type: "H3_PRODUCT_SHELL",
              links: [4],
            },
          ],
        },
      ],
      links: [
        {
          id: 1,
          origin_id: -10,
          origin_slot: 0,
          target_id: 9,
          target_slot: 0,
          type: "H3_CONTEXT_REPORT",
        },
        {
          id: 2,
          origin_id: -10,
          origin_slot: 1,
          target_id: 9,
          target_slot: 1,
          type: "H3_NATIVE_H3_WIRING",
        },
        {
          id: 3,
          origin_id: 9,
          origin_slot: 0,
          target_id: -20,
          target_slot: 0,
          type: "STRING",
        },
        {
          id: 4,
          origin_id: 9,
          origin_slot: 1,
          target_id: -20,
          target_slot: 1,
          type: "H3_PRODUCT_SHELL",
        },
      ],
    };
    const inspected = inspectVisibleH3Graph(
      { nodes, links, definitions: { subgraphs: [boundaryDefinition] } },
      manifest,
    );
    expect(inspected).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });

    const invalidPresentation = structuredClone({
      nodes,
      links,
      definitions: { subgraphs: [boundaryDefinition] },
    }) as { definitions: { subgraphs: Array<Record<string, unknown>> } };
    const invalidInterface = invalidPresentation.definitions.subgraphs[0]
      ?.inputs as Array<Record<string, unknown>> | undefined;
    if (invalidInterface === undefined) throw new Error("fixture drift");
    invalidInterface[0]!.shape = "private";
    expect(inspectVisibleH3Graph(invalidPresentation, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
      reason: "malformed_subgraph_definition",
    });

    const reroutedBoundary = structuredClone({
      nodes,
      links,
      definitions: { subgraphs: [boundaryDefinition] },
    });
    const reroutedDefinition = reroutedBoundary.definitions.subgraphs[0] as
      Record<string, unknown> | undefined;
    if (reroutedDefinition === undefined) throw new Error("fixture drift");
    reroutedDefinition.reroutes = [{ id: 1 }];
    expect(() => inspectVisibleH3Graph(reroutedBoundary, manifest)).toThrow(
      "subgraph definition contains unsupported reroutes",
    );

    const privateInterfaceMember = structuredClone({
      nodes,
      links,
      definitions: { subgraphs: [boundaryDefinition] },
    }) as {
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const interfaceDefinition = privateInterfaceMember.definitions.subgraphs[0];
    const interfaceInputs = interfaceDefinition?.inputs as
      Array<Record<string, unknown>> | undefined;
    if (interfaceInputs === undefined) throw new Error("fixture drift");
    interfaceInputs[0]!.private_media = "file:///secret";
    expect(
      inspectVisibleH3Graph(privateInterfaceMember, manifest),
    ).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
      reason: "malformed_subgraph_definition",
    });

    const invalidSlotGraph = structuredClone({
      nodes,
      links,
      definitions: { subgraphs: [boundaryDefinition] },
    }) as {
      nodes: Array<Record<string, unknown>>;
      links: Array<unknown[]>;
      definitions: Record<string, unknown>;
    };
    const generationNode = invalidSlotGraph.nodes.find(
      (node) => node.id === byType.get(canonicalTypes[6]),
    );
    const generationInputs = generationNode?.inputs as
      Array<Record<string, unknown>> | undefined;
    const promptLink = invalidSlotGraph.links.find((link) => link[0] === 9);
    if (generationInputs === undefined || promptLink === undefined)
      throw new Error("fixture drift");
    promptLink[4] = 3;
    generationInputs[3] = { link: 9 };
    generationInputs[4] = { link: null };
    expect(inspectVisibleH3Graph(invalidSlotGraph, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });

    const invalidTypeGraph = structuredClone(invalidSlotGraph) as {
      nodes: Array<Record<string, unknown>>;
      links: Array<unknown[]>;
    };
    const invalidTypeLink = invalidTypeGraph.links.find(
      (link) => link[0] === 9,
    );
    if (invalidTypeLink === undefined) throw new Error("fixture drift");
    invalidTypeLink[4] = 4;
    invalidTypeLink[5] = "H3_CONTEXT_REPORT";
    const invalidTypeGeneration = invalidTypeGraph.nodes.find(
      (node) => node.id === byType.get(canonicalTypes[6]),
    );
    const invalidTypeInputs = invalidTypeGeneration?.inputs as
      Array<Record<string, unknown>> | undefined;
    if (invalidTypeInputs === undefined) throw new Error("fixture drift");
    invalidTypeInputs[3] = { link: null };
    invalidTypeInputs[4] = { link: 9 };
    expect(inspectVisibleH3Graph(invalidTypeGraph, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
    });

    const malformedBoundary = structuredClone({
      nodes,
      links,
      definitions: { subgraphs: [boundaryDefinition] },
    }) as {
      nodes: Array<Record<string, unknown>>;
      links: Array<unknown[]>;
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const malformedDefinition = malformedBoundary.definitions.subgraphs[0];
    if (malformedDefinition === undefined) throw new Error("fixture drift");
    malformedDefinition.inputNode = { id: -11 };
    expect(inspectVisibleH3Graph(malformedBoundary, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
      reason: "malformed_subgraph_definition",
    });

    const danglingBoundary = structuredClone({
      nodes,
      links,
      definitions: { subgraphs: [boundaryDefinition] },
    }) as {
      nodes: Array<Record<string, unknown>>;
      links: Array<unknown[]>;
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const danglingDefinition = danglingBoundary.definitions.subgraphs[0];
    const danglingNodes = danglingDefinition?.nodes as
      Array<Record<string, unknown>> | undefined;
    const danglingShell = danglingNodes?.[0];
    const danglingInputs = danglingShell?.inputs as
      Array<Record<string, unknown>> | undefined;
    if (danglingInputs === undefined) throw new Error("fixture drift");
    danglingInputs[0]!.link = 99;
    expect(inspectVisibleH3Graph(danglingBoundary, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
      reason: "malformed_subgraph_definition",
    });

    const duplicateBoundary = structuredClone({
      nodes,
      links,
      definitions: { subgraphs: [boundaryDefinition] },
    }) as {
      nodes: Array<Record<string, unknown>>;
      links: Array<unknown[]>;
      definitions: { subgraphs: Array<Record<string, unknown>> };
    };
    const duplicateDefinition = duplicateBoundary.definitions.subgraphs[0];
    const duplicateOutputs = duplicateDefinition?.outputs as
      Array<Record<string, unknown>> | undefined;
    if (duplicateOutputs === undefined) throw new Error("fixture drift");
    duplicateOutputs[0]!.linkIds = [3, 3];
    expect(inspectVisibleH3Graph(duplicateBoundary, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: false,
      reason: "malformed_subgraph_definition",
    });
  });

  it("follows the public definition type when the host omits subgraph_id", () => {
    const graph = {
      nodes: [
        ...canonicalTypes.map((type, index) => ({ id: index + 10, type })),
        { id: 4, type: "definition-1" },
      ],
      definitions: {
        subgraphs: [
          {
            id: "definition-1",
            nodes: [
              { id: 2, type: "comfyui_h3_context.H3Context.ProductShell" },
            ],
          },
        ],
      },
    };
    expect(inspectVisibleH3Graph(graph, manifest)).toMatchObject({
      status: "ready",
      anchors: [{ executionId: "4:2" }],
    });
  });

  it("rejects duplicate serialized IDs and bounded-identity violations", () => {
    expect(() =>
      inspectVisibleH3Graph(
        {
          nodes: [
            { id: 7, type: "comfyui_h3_context.H3Context.ProductShell" },
            { id: 7, type: "foreign.Node" },
          ],
        },
        manifest,
      ),
    ).toThrow(/duplicate/);
    expect(() =>
      inspectVisibleH3Graph(
        {
          nodes: [
            {
              id: "x".repeat(100_000),
              type: "comfyui_h3_context.H3Context.ProductShell",
            },
          ],
        },
        manifest,
      ),
    ).toThrow(/invalid|bound/);
    expect(
      inspectVisibleH3Graph(
        {
          nodes: [{ id: 7, type: "comfyui_h3_context.H3Context.ProductShell" }],
        },
        manifest,
      ).status,
    ).toBe("incompatible");
  });

  it("rejects prototype-pollution keys in nested serialized graph data", () => {
    const graph = canonicalGraph();
    Object.defineProperty(graph.nodes[0], "constructor", {
      value: { polluted: true },
      enumerable: true,
    });
    expect(() => inspectVisibleH3Graph(graph, manifest)).toThrow(
      /unsafe serialized key/,
    );
  });

  it("admits only the two exact installed queue metadata shapes without changing inspection", () => {
    const baseline = directBaseGraph();
    const expected = inspectVisibleH3Graph(baseline, manifest);
    expect(expected).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
    });

    const inspire = structuredClone(baseline) as Record<string, unknown>;
    inspire.widget_idx_map = {
      "7": { seed: 0, noise_seed: 1, sampler_name: 1, scheduler: 2 },
    };
    expect(inspectVisibleH3Graph(inspire, manifest)).toEqual(expected);

    const easyUse = structuredClone(baseline) as Record<string, unknown>;
    easyUse.seed_widgets = { "7": 0 };
    expect(inspectVisibleH3Graph(easyUse, manifest)).toEqual(expected);

    const combined = structuredClone(inspire) as Record<string, unknown>;
    combined.seed_widgets = { "7": 2 };
    expect(inspectVisibleH3Graph(combined, manifest)).toEqual(expected);
  });

  it("rejects malformed or unrelated installed-metadata lookalikes", () => {
    const reject = (mutate: (graph: Record<string, unknown>) => void): void => {
      const graph = structuredClone(directBaseGraph()) as Record<
        string,
        unknown
      >;
      mutate(graph);
      expect(() => inspectVisibleH3Graph(graph, manifest)).toThrow();
    };

    reject((graph) => {
      graph.private_queue_metadata = { "7": 0 };
    });
    reject((graph) => {
      graph.widget_idx_map = undefined;
    });
    reject((graph) => {
      graph.widget_idx_map = { "7": { cfg: 0 } };
    });
    reject((graph) => {
      graph.widget_idx_map = { missing: { seed: 0 } };
    });
    reject((graph) => {
      graph.definitions = {
        subgraphs: [
          {
            id: "nested",
            nodes: [{ id: "nested-only", type: "Foreign.Node" }],
          },
        ],
      };
      graph.seed_widgets = { "nested-only": 0 };
    });
    for (const index of [-1, 0.5, 3])
      reject((graph) => {
        graph.seed_widgets = { "7": index };
      });
    reject((graph) => {
      graph.widget_idx_map = Object.assign(Object.create({ inherited: true }), {
        "7": { seed: 0 },
      });
    });

    let accessorRead = false;
    const accessorGraph = structuredClone(directBaseGraph()) as Record<
      string,
      unknown
    >;
    const accessorMap: Record<string, unknown> = {};
    Object.defineProperty(accessorMap, "7", {
      enumerable: true,
      get() {
        accessorRead = true;
        return 0;
      },
    });
    accessorGraph.seed_widgets = accessorMap;
    expect(() => inspectVisibleH3Graph(accessorGraph, manifest)).toThrow();
    expect(accessorRead).toBe(false);

    let rootAccessorRead = false;
    const rootAccessorGraph = structuredClone(directBaseGraph()) as Record<
      string,
      unknown
    >;
    Object.defineProperty(rootAccessorGraph, "widget_idx_map", {
      enumerable: false,
      get() {
        rootAccessorRead = true;
        return { "7": { seed: 0 } };
      },
    });
    expect(() => inspectVisibleH3Graph(rootAccessorGraph, manifest)).toThrow();
    expect(rootAccessorRead).toBe(false);
  });

  it("bounds the aggregate installed queue metadata entries", () => {
    const nodes = Array.from({ length: 4097 }, (_unused, index) => ({
      id: index + 1,
      type: "Foreign.Node",
      widgets_values: [0],
    }));
    const seed_widgets = Object.fromEntries(
      nodes.map((node) => [String(node.id), 0]),
    );
    expect(() =>
      inspectVisibleH3Graph({ nodes, seed_widgets }, manifest),
    ).toThrow(/metadata.*bound/i);
  });

  it("recognizes the minimum native H3 inference core without certifying the pipeline", () => {
    const observed = observedH3CoreCanvas();
    expect(recognizeH3InferenceCanvas(observed)).toMatchObject({
      status: "recognized",
      anchors: [{ nodeId: "MiniMaxH3ImageToVideo" }],
    });
    expect(inspectH3GraphAdmission(observed, manifest)).toMatchObject({
      status: "ready",
      existingGraphCompatible: true,
      anchors: [
        {
          executionId: "134",
          nodeId: "comfyui_h3_context.H3Context.ProductShell",
        },
      ],
    });
    // The complete-pipeline audit is intentionally independent and may still
    // diagnose this content-free projection; that result cannot gate admission.
    expect(inspectVisibleH3Graph(observed, manifest).status).not.toBe("ready");

    const nativeOnly = {
      nodes: [{ id: 1, type: "MiniMaxH3ReferenceToVideo" }],
    };
    expect(recognizeH3InferenceCanvas(nativeOnly)).toMatchObject({
      status: "recognized",
      anchors: [{ nodeId: "MiniMaxH3ReferenceToVideo" }],
    });
    expect(inspectH3GraphAdmission(nativeOnly, manifest)).toMatchObject({
      status: "missing",
      reason: "missing_product_shell",
    });

    const generic = {
      nodes: [
        { id: 1, type: "SamplerCustomAdvanced" },
        { id: 2, type: "VAEDecode" },
        { id: 3, type: "CreateVideo" },
      ],
    };
    expect(recognizeH3InferenceCanvas(generic)).toMatchObject({
      status: "missing",
      anchors: [],
    });
  });

  it("keeps a legacy selector-linked I2VA canvas inside minimum-H3 recognition", () => {
    const legacy = {
      nodes: [
        {
          id: 1,
          type: "LoadImage",
          inputs: [],
          outputs: [{ links: [10] }],
        },
        {
          id: 2,
          type: "ResolutionSelector",
          inputs: [],
          outputs: [{ links: [11] }, { links: [12] }],
        },
        {
          id: 3,
          type: "MiniMaxH3ImageToVideo",
          inputs: [
            { name: "width", link: 11 },
            { name: "height", link: 12 },
            { name: "first_frame", link: 10 },
          ],
          outputs: [],
        },
      ],
      links: [
        [10, 1, 0, 3, 2, "IMAGE"],
        [11, 2, 0, 3, 0, "INT"],
        [12, 2, 1, 3, 1, "INT"],
      ],
    };

    expect(recognizeH3InferenceCanvas(legacy)).toMatchObject({
      status: "recognized",
      anchors: [{ nodeId: "MiniMaxH3ImageToVideo" }],
    });
  });

  it("admits only descriptor-safe arrays and reachable native subgraphs", () => {
    const forgedMapNodes = [{ id: 1, type: "Foreign.Node" }];
    Object.defineProperty(forgedMapNodes, "map", {
      enumerable: false,
      value: () => [{ id: 2, type: "MiniMaxH3ImageToVideo" }],
    });
    expect(() => recognizeH3InferenceCanvas({ nodes: forgedMapNodes })).toThrow(
      /serialized node array/i,
    );

    let accessorReads = 0;
    const accessorNodes = new Array(1);
    Object.defineProperty(accessorNodes, "0", {
      configurable: true,
      enumerable: true,
      get() {
        accessorReads += 1;
        return { id: 3, type: "MiniMaxH3ReferenceToVideo" };
      },
    });
    expect(() => recognizeH3InferenceCanvas({ nodes: accessorNodes })).toThrow(
      /serialized node array/i,
    );
    expect(accessorReads).toBe(0);

    const iteratorNodes = [{ id: 4, type: "Foreign.Node" }];
    Object.defineProperty(iteratorNodes, Symbol.iterator, {
      enumerable: false,
      value: function* () {
        yield { id: 5, type: "MiniMaxH3ImageToVideo" };
      },
    });
    expect(() => recognizeH3InferenceCanvas({ nodes: iteratorNodes })).toThrow(
      /serialized node array/i,
    );

    const customPrototypeNodes = [{ id: 6, type: "Foreign.Node" }];
    Object.setPrototypeOf(customPrototypeNodes, null);
    expect(() =>
      recognizeH3InferenceCanvas({ nodes: customPrototypeNodes }),
    ).toThrow(/serialized node array/i);

    const subgraphId = "5dcb4ae0-2bda-4ee9-b889-b8c9b9012035";
    const definitions = {
      subgraphs: [
        {
          id: subgraphId,
          nodes: [{ id: 9, type: "MiniMaxH3ImageToVideo" }],
        },
      ],
    };
    expect(
      recognizeH3InferenceCanvas({
        nodes: [{ id: 7, type: "Foreign.Node" }],
        definitions,
      }),
    ).toMatchObject({ status: "missing", anchors: [] });
    expect(
      recognizeH3InferenceCanvas({
        nodes: [{ id: 8, type: subgraphId, subgraph_id: subgraphId }],
        definitions,
      }),
    ).toMatchObject({
      status: "recognized",
      anchors: [{ nodeId: "MiniMaxH3ImageToVideo" }],
    });

    const cycleA = "8598ac03-88d7-4b75-8ec8-ed13793c13dc";
    const cycleB = "c6d580db-c1b5-4255-8660-661b75c9dc91";
    expect(
      recognizeH3InferenceCanvas({
        nodes: [{ id: 10, type: cycleA, subgraph_id: cycleA }],
        definitions: {
          subgraphs: [
            {
              id: cycleA,
              nodes: [{ id: 11, type: cycleB, subgraph_id: cycleB }],
            },
            {
              id: cycleB,
              nodes: [{ id: 12, type: cycleA, subgraph_id: cycleA }],
            },
          ],
        },
      }),
    ).toMatchObject({ status: "missing", anchors: [] });
    expect(() =>
      recognizeH3InferenceCanvas({
        nodes: Array.from({ length: 4097 }, (_unused, index) => ({
          id: index + 1,
          type: "Foreign.Node",
        })),
      }),
    ).toThrow(/node bound/i);
  });

  it("does not trust the inherited global array iterator during recognition", () => {
    const originalIterator = Array.prototype[Symbol.iterator];
    const fabricated = {
      id: "fabricated-subgraph",
      nodes: [{ id: 92, type: "MiniMaxH3ImageToVideo" }],
      executionId: "fabricated-shell",
      nodeId: "comfyui_h3_context.H3Context.ProductShell",
    };
    let iteratorReads = 0;
    let recognition: ReturnType<typeof recognizeH3InferenceCanvas> | undefined;
    let admission: ReturnType<typeof inspectH3GraphAdmission> | undefined;
    Object.defineProperty(Array.prototype, Symbol.iterator, {
      configurable: true,
      writable: true,
      value: function () {
        let emitted = false;
        return {
          next() {
            iteratorReads += 1;
            if (emitted) return { done: true, value: undefined };
            emitted = true;
            return { done: false, value: fabricated };
          },
        };
      },
    });
    try {
      recognition = recognizeH3InferenceCanvas({
        nodes: [
          {
            id: 91,
            type: "fabricated-subgraph",
            subgraph_id: "fabricated-subgraph",
          },
        ],
        definitions: { subgraphs: [] },
      });
      admission = inspectH3GraphAdmission(
        { nodes: [{ id: 93, type: "MiniMaxH3ReferenceToVideo" }] },
        manifest,
      );
    } finally {
      Object.defineProperty(Array.prototype, Symbol.iterator, {
        configurable: true,
        writable: true,
        value: originalIterator,
      });
    }

    expect(iteratorReads).toBe(0);
    expect(recognition).toMatchObject({ status: "missing", anchors: [] });
    expect(admission).toMatchObject({
      status: "missing",
      reason: "missing_product_shell",
    });
  });
});
