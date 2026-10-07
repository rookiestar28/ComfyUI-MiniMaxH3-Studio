import type {
  AppModeCompiledPrompt,
  AppModeInputs,
  ExistingCompiledAdmission,
} from "../../src/host/appMode";
import { H3_NODE_TYPES } from "../../src/host/graphAdapter";
import {
  I2VA_SCALE_NODE_TYPE,
  type Json,
} from "../../src/host/templateMaterialization";
import { existingManagedHostGraphFixture } from "./managedHostGraphFixture";
import { createQualifiedBasePrompt } from "./qualifiedBasePrompt";

type CompiledNode = {
  class_type: string;
  inputs: Record<string, unknown>;
};

function compiledNode(
  output: Record<string, unknown>,
  classType: string,
): [string, CompiledNode] {
  const match = Object.entries(output).find(
    ([, value]) => (value as CompiledNode).class_type === classType,
  );
  if (match === undefined)
    throw new Error(`fixture node is unavailable: ${classType}`);
  return match as [string, CompiledNode];
}

function opaqueSources(inputs: AppModeInputs) {
  const image = (name: string) => ({
    class_type: H3_NODE_TYPES.loadImage,
    inputs: { image: name },
  });
  if (inputs.task_mode === "i2va") return { first_frame: image("first.png") };
  if (inputs.task_mode === "l2va") return { last_frame: image("last.png") };
  if (inputs.task_mode === "fl2va")
    return {
      first_frame: image("first.png"),
      last_frame: image("last.png"),
    };
  return {};
}

/**
 * A user-authored existing prompt deliberately unlike the repository template.
 *
 * Native frame inputs pass through an arbitrary IMAGE producer while the typed
 * Registry retains the raw loader declaration. FL2VA uses two slots of one
 * producer. Extra media/Registry/H3 nodes prove the existing route is open-world.
 */
export function existingCompiledUserVariant(inputs: AppModeInputs): Readonly<{
  compiled: AppModeCompiledPrompt;
  subject: ExistingCompiledAdmission;
}> {
  const output = createQualifiedBasePrompt(inputs, opaqueSources(inputs), {
    sharedDurationSource: true,
  });
  const [requestNodeId] = compiledNode(output, H3_NODE_TYPES.request);
  const [durationSourceNodeId] = compiledNode(output, "PrimitiveFloat");
  const [anchorNodeId, anchor] = compiledNode(
    output,
    H3_NODE_TYPES.imageGeneration,
  );
  const [productShellNodeId] = compiledNode(output, H3_NODE_TYPES.productShell);
  const registry = Object.values(output).find(
    (node) =>
      (node as CompiledNode).class_type === H3_NODE_TYPES.referenceRegistry,
  ) as CompiledNode | undefined;

  output["119"] = {
    class_type: "UserImageTransform",
    inputs: {
      first: inputs.first_frame_source
        ? [inputs.first_frame_source, 0]
        : undefined,
      last: inputs.last_frame_source
        ? [inputs.last_frame_source, 0]
        : undefined,
    },
  };
  if (inputs.task_mode === "i2va") anchor.inputs.first_frame = ["119", 0];
  if (inputs.task_mode === "l2va") anchor.inputs.last_frame = ["119", 1];
  if (inputs.task_mode === "fl2va") {
    anchor.inputs.first_frame = ["119", 0];
    anchor.inputs.last_frame = ["119", 1];
  }

  output["120"] = { class_type: H3_NODE_TYPES.loadImage, inputs: {} };
  output["121"] = { class_type: H3_NODE_TYPES.loadVideo, inputs: {} };
  output["122"] = { class_type: H3_NODE_TYPES.loadAudio, inputs: {} };
  if (registry === undefined)
    output["123"] = {
      class_type: H3_NODE_TYPES.referenceRegistry,
      inputs: {},
    };
  output["124"] = {
    class_type: H3_NODE_TYPES.referenceGeneration,
    inputs: { prompt: [productShellNodeId, 0] },
  };

  return {
    compiled: { output, workflow: {} },
    subject: {
      requestNodeId,
      durationSourceNodeId,
      visibleAnchorNodeId: anchorNodeId,
      anchorNodeId,
      anchorNodeType: H3_NODE_TYPES.imageGeneration,
      productShellNodeId,
    },
  };
}

/** Report-equivalent i2va workflow: scaler -> native, raw loader -> Registry. */
export function existingI2vaScalerRegistryFixture(
  inputs: AppModeInputs & { task_mode: "i2va"; first_frame_source: string },
): Readonly<{ workflow: Json; compiled: AppModeCompiledPrompt }> {
  const base = existingManagedHostGraphFixture(inputs);
  const workflow = structuredClone(base.workflow);
  const compiled = structuredClone(base.compiled);
  const nodes = workflow.nodes as Json[];
  const links = workflow.links as unknown[][];
  const anchor = nodes.find(
    (node) => node.type === H3_NODE_TYPES.imageGeneration,
  )!;
  const scaler = nodes.find((node) => node.type === I2VA_SCALE_NODE_TYPE)!;
  const firstInput = (anchor.inputs as Json[]).find(
    (input) => input.name === "first_frame",
  )!;
  const link = links.find((row) => String(row[0]) === String(firstInput.link))!;
  const oldOrigin = nodes.find((node) => String(node.id) === String(link[1]))!;
  const imageOutput = (scaler.outputs as Json[]).findIndex(
    (output) => output.type === "IMAGE",
  );
  if (imageOutput < 0)
    throw new Error("fixture scaler IMAGE output is unavailable");
  for (const output of oldOrigin.outputs as Json[]) {
    if (Array.isArray(output.links))
      output.links = output.links.filter(
        (value) => String(value) !== String(firstInput.link),
      );
  }
  const scalerOutput = (scaler.outputs as Json[])[imageOutput]!;
  scalerOutput.links = [
    ...(Array.isArray(scalerOutput.links) ? scalerOutput.links : []),
    firstInput.link,
  ];
  link[1] = scaler.id;
  link[2] = imageOutput;

  const output = compiled.output;
  const [compiledAnchorId, compiledAnchor] = compiledNode(
    output,
    H3_NODE_TYPES.imageGeneration,
  );
  const [compiledScalerId] = compiledNode(output, I2VA_SCALE_NODE_TYPE);
  compiledAnchor.inputs.first_frame = [compiledScalerId, imageOutput];
  if (output[compiledAnchorId] === undefined)
    throw new Error("fixture compiled anchor is unavailable");
  let unrelatedId = Math.max(...nodes.map((node) => Number(node.id))) + 1;
  while (output[String(unrelatedId)] !== undefined) unrelatedId += 1;
  nodes.push({
    id: unrelatedId,
    type: H3_NODE_TYPES.referenceGeneration,
    inputs: [],
    outputs: [],
    widgets_values: [],
  });
  workflow.last_node_id = Math.max(Number(workflow.last_node_id), unrelatedId);
  output[String(unrelatedId)] = {
    class_type: H3_NODE_TYPES.referenceGeneration,
    inputs: {},
  };
  return { workflow, compiled };
}
