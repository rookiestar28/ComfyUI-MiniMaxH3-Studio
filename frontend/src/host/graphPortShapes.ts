/**
 * Reading the shape of a serialized node's ports: which ports exist, what type each slot carries,
 * how many are allowed, and which names a node is permitted to declare.
 *
 * Shape only. Whether a shape satisfies a contract is the next layer's question.
 */

import { H3_NODE_TYPES } from "./graphSerialization";

export function serializedPorts(
  value: unknown,
): Array<{ slot: number; value: Record<string, unknown> }> | undefined {
  if (Array.isArray(value)) {
    const ports: Array<{ slot: number; value: Record<string, unknown> }> = [];
    for (const [slot, item] of value.entries()) {
      if (item === null || typeof item !== "object" || Array.isArray(item))
        return undefined;
      ports.push({ slot, value: item as Record<string, unknown> });
    }
    return ports;
  }
  if (value !== null && typeof value === "object")
    return [{ slot: 0, value: value as Record<string, unknown> }];
  return undefined;
}

export function serializedPortSlot(
  node: Record<string, unknown>,
  port: Record<string, unknown>,
  fallback: number,
): number {
  const type = node.type;
  const name = port.name;
  if (type === "MiniMaxH3ReferenceToVideo" && typeof name === "string") {
    // IMPORTANT: c44's dynamic Reference node serializes target slots as
    // 8/9/10/11/12, not input-array indexes; a host revision must requalify
    // this pinned map instead of broadening it to arbitrary slot guesses.
    const pinned: Record<string, number> = {
      "ref_images.image0": 8,
      "ref_images.image1": 9,
      "ref_videos.video0": 10,
      "ref_video_audios.video_audio0": 11,
      "ref_audios.audio0": 12,
    };
    return pinned[name] ?? fallback;
  }
  return fallback;
}

// IMPORTANT: direct and nested ComfyUI serialization share this exact ProductShell
// socket authority; divergent copies can make a valid workflow fail host admission.

const productShellInputSchema = [
  { name: "report", type: "H3_CONTEXT_REPORT", required: true },
  {
    name: "native_h3_wiring",
    type: "H3_NATIVE_H3_WIRING",
    required: true,
  },
  { name: "recompute_plan", type: "H3_RECOMPUTE_PLAN", required: false },
  {
    name: "pipeline_transaction",
    type: "H3_PIPELINE_TRANSACTION",
    required: false,
  },
  {
    name: "generation_sequence_state",
    type: "H3_GENERATION_SEQUENCE_STATE",
    required: false,
  },
  {
    name: "semantic_proposal_review_authority",
    type: "H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY",
    required: false,
  },
] as const;

const productShellRequiredInputCount = productShellInputSchema.filter(
  ({ required }) => required,
).length;

function productShellInputType(name: string): string | undefined {
  return productShellInputSchema.find((port) => port.name === name)?.type;
}

export function hasValidNestedProductShellInputShape(
  ports: ReadonlyArray<{ value: Record<string, unknown> }>,
): boolean {
  if (
    ports.length < productShellRequiredInputCount ||
    ports.length > productShellInputSchema.length
  )
    return false;
  for (let slot = 0; slot < productShellRequiredInputCount; slot += 1) {
    if (ports[slot]?.value.name !== productShellInputSchema[slot]?.name)
      return false;
  }
  const optionalSchema = productShellInputSchema.slice(
    productShellRequiredInputCount,
  );
  let previousOptionalIndex = -1;
  for (const { value } of ports.slice(productShellRequiredInputCount)) {
    const optionalIndex = optionalSchema.findIndex(
      ({ name }) => name === value.name,
    );
    if (optionalIndex <= previousOptionalIndex) return false;
    previousOptionalIndex = optionalIndex;
  }
  return true;
}

export function isAllowedSerializedPortName(
  nodeType: unknown,
  name: string,
  direction: "input" | "output",
): boolean {
  if (typeof nodeType !== "string") return true;
  return serializedPortType(nodeType, name, direction) !== undefined;
}

/**
 * The autogrow input groups, by node type and group name.
 *
 * A `COMFY_AUTOGROW_V3` group serializes one socket per bound item, named
 * `<group>.<member><index>`. The member spelling is the host's, not this
 * repository's, and it has already moved: the shipped Reference subgraph was
 * authored against a revision that wrote `ref_images.image0`, while a later
 * observed template writes `ref_images.ref_image_0`. Enumerating the
 * members would therefore reject a correct graph the next time the host renames
 * one, so the group is matched and the member only has to look like a member.
 */

const autogrowInputGroups: Record<string, Record<string, string>> = {
  "comfyui_h3_context.H3Context.ReferenceRegistry": {
    images: "IMAGE",
    videos: "VIDEO",
    paired_audios: "AUDIO",
    audios: "AUDIO",
  },
  MiniMaxH3ReferenceToVideo: {
    ref_images: "IMAGE",
    ref_videos: "IMAGE",
    ref_video_audios: "AUDIO",
    ref_audios: "AUDIO",
  },
};

function autogrowInputType(nodeType: string, name: string): string | undefined {
  const groups = autogrowInputGroups[nodeType];
  const separator = name.indexOf(".");
  if (groups === undefined || separator <= 0) return undefined;
  const member = name.slice(separator + 1);
  return /^[a-z][a-z_]*_?\d+$/.test(member)
    ? groups[name.slice(0, separator)]
    : undefined;
}

export function serializedPortType(
  nodeType: unknown,
  name: string,
  direction: "input" | "output",
): string | undefined {
  if (typeof nodeType !== "string") return undefined;
  if (direction === "input" && nodeType === H3_NODE_TYPES.productShell)
    return productShellInputType(name);
  if (direction === "input") {
    const autogrow = autogrowInputType(nodeType, name);
    if (autogrow !== undefined) return autogrow;
  }
  const inputTypes: Record<string, Record<string, string>> = {
    "comfyui_h3_context.H3Context.Request": {
      task_mode: "COMBO",
      user_intent: "STRING",
      duration_seconds: "FLOAT",
      hard_constraints: "H3_HARD_CONSTRAINTS",
    },
    "comfyui_h3_context.H3Context.Plan": {
      request: "H3_CONTEXT_REQUEST",
      reference_registry: "H3_REFERENCE_REGISTRY",
      intent_graph: "H3_INTENT_GRAPH",
    },
    "comfyui_h3_context.H3Context.Compiler": {
      plan: "H3_CONTEXT_PLAN",
    },
    "comfyui_h3_context.H3Context.Validator": {
      plan: "H3_CONTEXT_PLAN",
      prompt_document: "H3_PROMPT_DOCUMENT",
    },
    "comfyui_h3_context.H3Context.NativeH3Adapter": {
      report: "H3_CONTEXT_REPORT",
    },
    "comfyui_h3_context.H3Context.ProductShell": {
      report: "H3_CONTEXT_REPORT",
      native_h3_wiring: "H3_NATIVE_H3_WIRING",
    },
    "comfyui_h3_context.H3Context.ReferenceRegistry": {
      first_frame: "IMAGE",
      last_frame: "IMAGE",
    },
    "comfyui_h3_context.H3Context.Preview": {
      report: "H3_CONTEXT_REPORT",
    },
    MiniMaxH3ImageToVideo: {
      clip: "CLIP",
      vae: "VAE",
      first_frame: "IMAGE",
      last_frame: "IMAGE",
      prompt: "STRING",
      width: "INT",
      height: "INT",
      length: "INT",
    },
    MiniMaxH3ReferenceToVideo: {
      clip: "CLIP",
      vae: "VAE",
      audio_vae: "VAE",
      prompt: "STRING",
      width: "INT",
      height: "INT",
      length: "INT",
      ref_image_size: "COMBO",
    },
    GetVideoComponents: { video: "VIDEO" },
    LoadImage: { image: "IMAGE" },
    LoadVideo: { file: "VIDEO" },
    LoadAudio: { audio: "AUDIO" },
  };
  const outputTypes: Record<string, Record<string, string>> = {
    "comfyui_h3_context.H3Context.Request": {
      request: "H3_CONTEXT_REQUEST",
    },
    "comfyui_h3_context.H3Context.Plan": {
      plan: "H3_CONTEXT_PLAN",
      report: "H3_CONTEXT_REPORT",
    },
    "comfyui_h3_context.H3Context.Compiler": {
      prompt: "H3_PROMPT_STRING",
      report: "H3_CONTEXT_REPORT",
      prompt_document: "H3_PROMPT_DOCUMENT",
    },
    "comfyui_h3_context.H3Context.Validator": {
      validation: "H3_VALIDATION_RESULT",
      validated_report: "H3_CONTEXT_REPORT",
    },
    "comfyui_h3_context.H3Context.NativeH3Adapter": {
      prompt: "H3_PROMPT_STRING",
      native_h3_wiring: "H3_NATIVE_H3_WIRING",
    },
    "comfyui_h3_context.H3Context.ProductShell": {
      prompt: "STRING",
      product_shell: "H3_PRODUCT_SHELL",
    },
    "comfyui_h3_context.H3Context.Preview": {
      prompt: "H3_PROMPT_STRING",
      preview: "H3_CONTEXT_PREVIEW",
    },
    "comfyui_h3_context.H3Context.ReferenceRegistry": {
      reference_registry: "H3_REFERENCE_REGISTRY",
    },
    GetVideoComponents: { images: "IMAGE", audio: "AUDIO" },
    LoadImage: { IMAGE: "IMAGE", MASK: "MASK" },
    LoadVideo: { VIDEO: "VIDEO" },
    LoadAudio: { AUDIO: "AUDIO" },
    MiniMaxH3ImageToVideo: {
      positive: "CONDITIONING",
      LATENT: "LATENT",
    },
    MiniMaxH3ReferenceToVideo: {
      positive: "CONDITIONING",
      LATENT: "LATENT",
    },
  };
  return (direction === "input" ? inputTypes : outputTypes)[nodeType]?.[name];
}

export function serializedPortTypeAtSlot(
  nodeType: unknown,
  slot: number,
  direction: "input" | "output",
): string | undefined {
  if (typeof nodeType !== "string") return undefined;
  if (direction === "input") {
    const inputSlots: Record<string, string[]> = {
      "comfyui_h3_context.H3Context.Request": [
        "COMBO",
        "STRING",
        "INT",
        "FLOAT",
        "H3_HARD_CONSTRAINTS",
      ],
      "comfyui_h3_context.H3Context.Plan": [
        "H3_CONTEXT_REQUEST",
        "H3_REFERENCE_REGISTRY",
        "H3_INTENT_GRAPH",
      ],
      "comfyui_h3_context.H3Context.Compiler": ["H3_CONTEXT_PLAN"],
      "comfyui_h3_context.H3Context.Validator": [
        "H3_CONTEXT_PLAN",
        "H3_PROMPT_DOCUMENT",
      ],
      "comfyui_h3_context.H3Context.NativeH3Adapter": ["H3_CONTEXT_REPORT"],
      "comfyui_h3_context.H3Context.ProductShell": productShellInputSchema
        .slice(0, productShellRequiredInputCount)
        .map(({ type }) => type),
      "comfyui_h3_context.H3Context.Preview": ["H3_CONTEXT_REPORT"],
      MiniMaxH3ImageToVideo: ["STRING", "INT", "INT", "INT"],
      MiniMaxH3ReferenceToVideo: [
        "STRING",
        "INT",
        "INT",
        "INT",
        "COMBO",
        "IMAGE",
        "IMAGE",
        "IMAGE",
        "AUDIO",
        "AUDIO",
        "AUDIO",
        "AUDIO",
      ],
      GetVideoComponents: ["VIDEO"],
      LoadImage: ["IMAGE", "MASK"],
      LoadVideo: ["VIDEO"],
      LoadAudio: ["AUDIO"],
    };
    return inputSlots[nodeType]?.[slot];
  }
  const outputSlots: Record<string, string[]> = {
    "comfyui_h3_context.H3Context.Request": ["H3_CONTEXT_REQUEST"],
    "comfyui_h3_context.H3Context.Plan": [
      "H3_CONTEXT_PLAN",
      "H3_CONTEXT_REPORT",
    ],
    "comfyui_h3_context.H3Context.Compiler": [
      "H3_PROMPT_STRING",
      "H3_CONTEXT_REPORT",
      "H3_PROMPT_DOCUMENT",
    ],
    "comfyui_h3_context.H3Context.Validator": [
      "H3_VALIDATION_RESULT",
      "H3_CONTEXT_REPORT",
    ],
    "comfyui_h3_context.H3Context.NativeH3Adapter": [
      "H3_PROMPT_STRING",
      "H3_NATIVE_H3_WIRING",
    ],
    "comfyui_h3_context.H3Context.ProductShell": ["STRING", "H3_PRODUCT_SHELL"],
    "comfyui_h3_context.H3Context.Preview": [
      "H3_PROMPT_STRING",
      "H3_CONTEXT_PREVIEW",
    ],
    "comfyui_h3_context.H3Context.ReferenceRegistry": ["H3_REFERENCE_REGISTRY"],
    GetVideoComponents: ["IMAGE", "AUDIO"],
    MiniMaxH3ImageToVideo: ["CONDITIONING", "LATENT"],
    MiniMaxH3ReferenceToVideo: ["CONDITIONING", "LATENT"],
  };
  return outputSlots[nodeType]?.[slot];
}

export function serializedPortLimit(
  nodeType: unknown,
  direction: "input" | "output",
  nested: boolean,
): number | undefined {
  if (typeof nodeType !== "string") return undefined;
  const limits: Record<
    string,
    { input: number; output: number; nestedInput?: number }
  > = {
    "comfyui_h3_context.H3Context.Request": {
      input: 7,
      nestedInput: 3,
      output: 1,
    },
    "comfyui_h3_context.H3Context.Plan": {
      input: 4,
      nestedInput: 2,
      output: 2,
    },
    "comfyui_h3_context.H3Context.Compiler": { input: 1, output: 3 },
    "comfyui_h3_context.H3Context.Validator": { input: 2, output: 2 },
    "comfyui_h3_context.H3Context.NativeH3Adapter": {
      input: 1,
      output: 2,
    },
    "comfyui_h3_context.H3Context.ProductShell": {
      // IMPORTANT: keep the exact required+optional count synchronized with ProductShell INPUT_TYPES.
      input: productShellInputSchema.length,
      output: 2,
    },
    "comfyui_h3_context.H3Context.Preview": { input: 1, output: 2 },
    "comfyui_h3_context.H3Context.ReferenceRegistry": {
      // first_frame, last_frame, then nine images, three videos, three paired
      // audios and three standalone audios -- the pure core's own maxima.
      input: 20,
      output: 1,
    },
    MiniMaxH3ImageToVideo: { input: 8, output: 2 },
    // Eight fixed sockets plus the same autogrow ceiling.
    MiniMaxH3ReferenceToVideo: { input: 26, output: 2 },
    GetVideoComponents: { input: 1, output: 2 },
    // The host's loaders publish their outputs; a fixture that omitted them is
    // not evidence that a real save does.
    LoadImage: { input: 1, output: 2 },
    LoadVideo: { input: 1, output: 1 },
    LoadAudio: { input: 1, output: 1 },
  };
  const limit = limits[nodeType];
  if (limit === undefined) return undefined;
  return direction === "input" && nested && limit.nestedInput !== undefined
    ? limit.nestedInput
    : limit[direction];
}

export function serializedPositionalLength(
  nodeType: unknown,
  direction: "input" | "output",
): number | undefined {
  if (typeof nodeType !== "string") return undefined;
  const lengths: Record<string, { input: number; output: number }> = {
    "comfyui_h3_context.H3Context.Request": { input: 5, output: 1 },
    "comfyui_h3_context.H3Context.Plan": { input: 3, output: 2 },
    "comfyui_h3_context.H3Context.Compiler": { input: 1, output: 3 },
    "comfyui_h3_context.H3Context.Validator": { input: 2, output: 2 },
    "comfyui_h3_context.H3Context.NativeH3Adapter": { input: 1, output: 2 },
    "comfyui_h3_context.H3Context.ProductShell": {
      input: productShellRequiredInputCount,
      output: 2,
    },
    "comfyui_h3_context.H3Context.Preview": { input: 1, output: 2 },
    "comfyui_h3_context.H3Context.ReferenceRegistry": { input: 4, output: 1 },
    MiniMaxH3ImageToVideo: { input: 8, output: 2 },
    MiniMaxH3ReferenceToVideo: { input: 12, output: 0 },
    GetVideoComponents: { input: 1, output: 2 },
    LoadImage: { input: 1, output: 0 },
    LoadVideo: { input: 1, output: 0 },
    LoadAudio: { input: 1, output: 0 },
  };
  return lengths[nodeType]?.[direction];
}

// Direct LiteGraph saves carry presentation metadata on each slot. Keep the
// c44 public slot vocabulary, but reject arbitrary members that could hide
// private media, widget, or link payloads in an otherwise queueable graph.

export const serializedInputMembers = new Set([
  "color_off",
  "color_on",
  "dir",
  "label",
  "localized_name",
  "locked",
  "name",
  "nameLocked",
  "removable",
  "shape",
  "type",
  "widget",
  "pos",
  "link",
]);

export const serializedOutputMembers = new Set([
  "color_off",
  "color_on",
  "dir",
  "label",
  "localized_name",
  "locked",
  "name",
  "nameLocked",
  "removable",
  "shape",
  "type",
  "widget",
  "pos",
  "slot_index",
  "links",
]);
