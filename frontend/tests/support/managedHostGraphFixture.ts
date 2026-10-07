import type {
  AppModeCompiledPrompt,
  AppModeInputs,
} from "../../src/host/appMode";
import { H3_NODE_TYPES } from "../../src/host/graphAdapter";
import {
  I2VA_SCALE_NODE_TYPE,
  I2VA_SCALE_WIDGET_VALUES,
  I2VA_SIZE_NODE_TYPE,
  MODE_TEMPLATE,
  spliceContextPipeline,
  type Json,
} from "../../src/host/templateMaterialization";
import { createQualifiedBasePrompt } from "./qualifiedBasePrompt";
import { syntheticTemplate } from "./templateFixture";

function remapCompiledOutput(
  output: Record<string, unknown>,
  ids: Readonly<Record<string, string>>,
): Record<string, unknown> {
  const remapValue = (value: unknown): unknown => {
    if (
      Array.isArray(value) &&
      value.length === 2 &&
      (typeof value[0] === "string" || typeof value[0] === "number") &&
      typeof value[1] === "number"
    )
      return [ids[String(value[0])] ?? String(value[0]), value[1]];
    if (Array.isArray(value)) return value.map(remapValue);
    return value;
  };
  return Object.fromEntries(
    Object.entries(output).map(([id, value]) => {
      const node = value as Record<string, unknown>;
      const inputs = (node.inputs ?? {}) as Record<string, unknown>;
      return [
        ids[id] ?? id,
        {
          ...node,
          inputs: Object.fromEntries(
            Object.entries(inputs).map(([name, input]) => [
              name,
              remapValue(input),
            ]),
          ),
        },
      ];
    }),
  );
}

export function existingManagedHostGraphFixture(
  inputs: AppModeInputs,
  options: Parameters<typeof createQualifiedBasePrompt>[2] = {},
): Readonly<{ workflow: Json; compiled: AppModeCompiledPrompt }> {
  const result = spliceContextPipeline(
    syntheticTemplate(MODE_TEMPLATE[inputs.task_mode]!),
    {
      taskMode: inputs.task_mode,
      userIntent: inputs.user_intent,
      durationSeconds: inputs.duration_milliseconds / 1000,
    },
  );
  const nodes = result.workflow.nodes as Json[];
  const math = nodes.find((node) => node.type === "ComfyMathExpression")!;
  const anchorNode = nodes.find(
    (node) => node.type === "MiniMaxH3ImageToVideo",
  )!;
  const nativeLength = (result.workflow.links as unknown[][]).find(
    (link) =>
      String(link[1]) === String(math.id) &&
      String(link[3]) === String(result.anchorNodeId),
  )!;
  nativeLength[2] = 1;
  math.outputs = [
    { name: "FLOAT", type: "FLOAT", links: [] },
    { name: "INT", type: "INT", links: [nativeLength[0]] },
  ];
  const workflowLinks = result.workflow.links as unknown[][];
  let resolutionNodeId: number | undefined;
  if (inputs.task_mode !== "i2va") {
    resolutionNodeId = Math.max(
      47,
      Math.max(...nodes.map((node) => Number(node.id))) + 1,
    );
    const nextLinkId =
      Math.max(...workflowLinks.map((link) => Number(link[0]))) + 1;
    const anchorInputs = anchorNode.inputs as Json[];
    const widthSlot = anchorInputs.findIndex((input) => input.name === "width");
    const heightSlot = anchorInputs.findIndex(
      (input) => input.name === "height",
    );
    if (widthSlot < 0 || heightSlot < 0)
      throw new Error("managed fixture anchor geometry inputs are unavailable");
    anchorInputs[widthSlot]!.link = nextLinkId;
    anchorInputs[heightSlot]!.link = nextLinkId + 1;
    nodes.push({
      id: resolutionNodeId,
      type: "ResolutionSelector",
      inputs: [],
      outputs: [
        { name: "width", type: "INT", links: [nextLinkId] },
        { name: "height", type: "INT", links: [nextLinkId + 1] },
      ],
      widgets_values: ["16:9 (Widescreen)", 0.4, 32],
    });
    workflowLinks.push(
      [nextLinkId, resolutionNodeId, 0, anchorNode.id, widthSlot, "INT"],
      [nextLinkId + 1, resolutionNodeId, 1, anchorNode.id, heightSlot, "INT"],
    );
    result.workflow.last_link_id = nextLinkId + 1;
  }
  result.workflow.last_node_id = Math.max(
    Number(result.workflow.last_node_id),
    resolutionNodeId ?? 0,
    44,
  );
  nodes.push({ id: 40, type: "VAELoader" });
  nodes.push({ id: 44, type: "CLIPLoader" });
  const idOf = (type: string): string =>
    String(nodes.find((node) => node.type === type)!.id);
  const ids: Record<string, string> = {
    "1": idOf(H3_NODE_TYPES.request),
    "2": idOf(H3_NODE_TYPES.plan),
    "3": idOf(H3_NODE_TYPES.compiler),
    "4": idOf(H3_NODE_TYPES.validator),
    "5": idOf(H3_NODE_TYPES.nativeAdapter),
    "6": idOf(H3_NODE_TYPES.imageGeneration),
    "7": idOf(H3_NODE_TYPES.preview),
    "8": idOf(H3_NODE_TYPES.productShell),
    "40": "40",
    "41": idOf("VAEDecode"),
    "42": idOf("CreateVideo"),
    "43": idOf("SaveVideo"),
    "44": "44",
    "45": idOf("PrimitiveFloat"),
    "46": idOf("ComfyMathExpression"),
  };
  if (inputs.task_mode === "i2va") {
    ids["9"] = idOf("LoadImage");
    ids["11"] = idOf(H3_NODE_TYPES.referenceRegistry);
    ids["47"] = idOf(I2VA_SCALE_NODE_TYPE);
    ids["48"] = idOf(I2VA_SIZE_NODE_TYPE);
  } else {
    ids["47"] = String(resolutionNodeId);
  }
  const qualifiedOutput = createQualifiedBasePrompt(
    inputs,
    inputs.task_mode === "i2va"
      ? {
          first_frame: {
            class_type: "LoadImage",
            inputs: { image: "template-first.png" },
          },
        }
      : {},
    {
      ...options,
      sharedDurationSource: true,
    },
  );
  const qualifiedAnchor = qualifiedOutput["6"] as {
    inputs: Record<string, unknown>;
  };
  if (inputs.task_mode === "i2va") {
    qualifiedAnchor.inputs.width = ["48", 0];
    qualifiedAnchor.inputs.height = ["48", 1];
    qualifiedOutput["47"] = {
      class_type: I2VA_SCALE_NODE_TYPE,
      inputs: {
        image: ["9", 0],
        upscale_method: I2VA_SCALE_WIDGET_VALUES[0],
        megapixels: I2VA_SCALE_WIDGET_VALUES[1],
        resolution_steps: I2VA_SCALE_WIDGET_VALUES[2],
      },
    };
    qualifiedOutput["48"] = {
      class_type: I2VA_SIZE_NODE_TYPE,
      inputs: { image: ["47", 0] },
    };
  } else {
    qualifiedAnchor.inputs.width = ["47", 0];
    qualifiedAnchor.inputs.height = ["47", 1];
    qualifiedOutput["47"] = {
      class_type: "ResolutionSelector",
      inputs: {
        aspect_ratio: "16:9 (Widescreen)",
        megapixels: 0.4,
        multiple: 32,
      },
    };
  }
  const output = remapCompiledOutput(qualifiedOutput, ids);
  const anchor = output[ids["6"]] as Record<string, unknown>;
  (anchor.inputs as Record<string, unknown>).length = [ids["46"], 1];
  return { workflow: result.workflow, compiled: { output, workflow: {} } };
}
