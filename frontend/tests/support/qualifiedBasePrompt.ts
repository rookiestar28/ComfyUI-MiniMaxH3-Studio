/**
 * A hand-built compiled prompt describing a complete H3 flow.
 *
 * Production materialization does not build a prompt at all any more: M17-20 D11
 * option (b) loads the pinned official template and splices the context pipeline
 * into it, so the executable topology comes from the template rather than from
 * this repository. This builder survives as the test fixture, because a
 * hand-written prompt is still the clearest way to state "here is a graph with
 * exactly this defect" and vary one thing at a time.
 *
 * It emits the artifact sink chain as well as the context chain. A prompt that
 * conditions and writes nothing is no longer a qualified flow, so a fixture
 * without a sink would only ever be testing the rejection path.
 */

import {
  AppModeError,
  OFFICIAL_LENGTH_EXPRESSION,
  appModeArtifactPrefix,
  validateInputs,
  type AppModeInputs,
} from "../../src/host/appMode";
import { H3_NODE_TYPES } from "../../src/host/graphAdapter";

type OpaqueNode = Record<string, unknown>;

export type QualifiedOpaqueSources = {
  first_frame?: OpaqueNode;
  last_frame?: OpaqueNode;
  reference_images?: OpaqueNode[];
  reference_videos?: OpaqueNode[];
  reference_audios?: OpaqueNode[];
};

const {
  request: requestNodeType,
  plan: planNodeType,
  compiler: compilerNodeType,
  validator: validatorNodeType,
  nativeAdapter: nativeAdapterNodeType,
  productShell: productShellNodeType,
  preview: previewNodeType,
  referenceRegistry: referenceRegistryNodeType,
  referenceGeneration: referenceGenerationNodeType,
  getVideoComponents: getVideoComponentsNodeType,
  loadImage: loadImageNodeType,
  loadVideo: loadVideoNodeType,
  loadAudio: loadAudioNodeType,
} = H3_NODE_TYPES;

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

/**
 * The decode/container/sink chain the pinned templates contribute.
 *
 * Ids start at 40 so they never collide with the context chain or with the
 * opaque media sources the reference route allocates from 10.
 */
export const ARTIFACT_SINK_IDS = Object.freeze({
  vaeLoader: "40",
  decode: "41",
  container: "42",
  sink: "43",
  // The native anchors encode with a CLIP and a VAE, so a prompt that omits
  // them is not the graph a host compiles. Keeping the loader here means every
  // row is measured against the real anchor contract.
  clipLoader: "44",
});

function artifactSinkChain(
  anchorId: string,
  filenamePrefix: string,
): Record<string, unknown> {
  return {
    [ARTIFACT_SINK_IDS.vaeLoader]: {
      class_type: "VAELoader",
      inputs: { vae_name: "minimax_h3_video_vae_fp16.safetensors" },
    },
    [ARTIFACT_SINK_IDS.clipLoader]: {
      class_type: "CLIPLoader",
      inputs: {
        clip_name: "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        type: "minimax_h3",
      },
    },
    [ARTIFACT_SINK_IDS.decode]: {
      class_type: "VAEDecode",
      inputs: {
        samples: [anchorId, 1],
        vae: [ARTIFACT_SINK_IDS.vaeLoader, 0],
      },
    },
    [ARTIFACT_SINK_IDS.container]: {
      class_type: "CreateVideo",
      inputs: { images: [ARTIFACT_SINK_IDS.decode, 0], fps: 24 },
    },
    [ARTIFACT_SINK_IDS.sink]: {
      class_type: "SaveVideo",
      inputs: {
        video: [ARTIFACT_SINK_IDS.container, 0],
        filename_prefix: filenamePrefix,
        format: "auto",
        codec: "auto",
      },
    },
  };
}

export function createQualifiedBasePrompt(
  inputs: AppModeInputs,
  opaqueSources: QualifiedOpaqueSources = {},
  options: {
    artifactSink?: boolean;
    filenamePrefix?: string;
    sharedDurationSource?: boolean;
  } = {},
): Record<string, unknown> {
  // The adoption route still binds context-only canvases, so its tests need the
  // sink-less shape. The materialized route never produces one.
  //
  // M17-20 D5: the materialized route writes its own deterministic prefix into
  // the sink and then requires the compiled prompt to carry exactly that value,
  // so the fixture derives it the same way rather than restating a literal.
  const prefix = options.filenamePrefix ?? appModeArtifactPrefix(inputs);
  const sink = (anchorId: string): Record<string, unknown> =>
    options.artifactSink === false ? {} : artifactSinkChain(anchorId, prefix);
  // The native anchors encode with a CLIP and a VAE, and the pinned templates
  // wire them from the loaders that also feed the decode. A sink-less canvas has
  // no such loaders, so it has no model links either -- which is the shape of
  // the context-only package the adoption rows bind.
  const modelInputs: Record<string, [string, number]> =
    options.artifactSink === false
      ? {}
      : {
          clip: [ARTIFACT_SINK_IDS.clipLoader, 0],
          vae: [ARTIFACT_SINK_IDS.vaeLoader, 0],
        };
  validateInputs(inputs);
  const durationSourceId = "45";
  const durationMathId = "46";
  const durationSeconds = inputs.duration_milliseconds / 1000;
  const sharedDurationSource = options.sharedDurationSource === true;
  const durationNodes = sharedDurationSource
    ? {
        [durationSourceId]: {
          class_type: "PrimitiveFloat",
          inputs: { value: durationSeconds },
        },
        [durationMathId]: {
          class_type: "ComfyMathExpression",
          inputs: {
            expression: OFFICIAL_LENGTH_EXPRESSION,
            "values.a": [durationSourceId, 0],
          },
        },
      }
    : {};
  const durationInput: number | [string, number] = sharedDurationSource
    ? [durationSourceId, 0]
    : durationSeconds;
  const lengthInput: number | [string, number] = sharedDurationSource
    ? [durationMathId, 1]
    : inputs.frame_count;
  if (inputs.task_mode === "ref2va") {
    const sourceNodes: Record<string, Record<string, unknown>> = {};
    const registryInputs: Record<string, unknown> = {};
    const nativeInputs: Record<string, unknown> = {
      ...modelInputs,
      prompt: ["8", 0],
      width: 512,
      height: 512,
      length: lengthInput,
      ref_image_size: "match",
    };
    let nextId = 10;
    const copySources = (
      sources: readonly Record<string, unknown>[] | undefined,
      expectedType: string,
    ): string[] => {
      const ids: string[] = [];
      for (const source of sources ?? []) {
        if (source.class_type !== expectedType)
          throw new AppModeError(
            "incompatible_graph",
            "a selected host-owned reference source is unavailable",
          );
        const id = String(nextId++);
        sourceNodes[id] = {
          class_type: source.class_type,
          inputs: { ...(record(source.inputs) ?? {}) },
        };
        ids.push(id);
      }
      return ids;
    };
    const imageIds = copySources(
      opaqueSources.reference_images,
      loadImageNodeType,
    );
    const videoIds = copySources(
      opaqueSources.reference_videos,
      loadVideoNodeType,
    );
    const audioIds = copySources(
      opaqueSources.reference_audios,
      loadAudioNodeType,
    );
    const imageLinks = imageIds.map((id) => [id, 0] as [string, number]);
    const videoLinks = videoIds.map((id) => [id, 0] as [string, number]);
    const audioLinks = audioIds.map((id) => [id, 0] as [string, number]);
    const videoComponentIds = videoIds.map((videoId) => {
      const id = String(nextId++);
      sourceNodes[id] = {
        class_type: getVideoComponentsNodeType,
        inputs: { video: [videoId, 0] },
      };
      return id;
    });
    if (imageLinks.length > 0) {
      imageLinks.forEach((link, index) => {
        registryInputs.images = link;
        nativeInputs[`ref_images.ref_image_${index}`] = link;
      });
    }
    const soundtrack = inputs.reference_video_soundtrack ?? "included";
    if (videoLinks.length > 0) {
      videoLinks.forEach((link, index) => {
        registryInputs.videos = link;
        nativeInputs[`ref_videos.ref_video_${index}`] = [
          videoComponentIds[index],
          0,
        ];
        // M17-17: the soundtrack is declared to the registry that owns it and
        // then bound on the anchor. The fixture mirrors the splice, so a fixture
        // that bound only the anchor would qualify a graph the product no longer
        // materializes.
        if (soundtrack === "included") {
          registryInputs[`paired_audios.paired_audio${index}`] = [
            videoComponentIds[index],
            1,
          ];
          nativeInputs[`ref_video_audios.ref_video_audio_${index}`] = [
            videoComponentIds[index],
            1,
          ];
        }
      });
    }
    if (audioLinks.length > 0) {
      audioLinks.forEach((link, index) => {
        registryInputs.audios = link;
        nativeInputs[`ref_audios.ref_audio_${index}`] = link;
      });
    }
    return {
      "1": {
        class_type: requestNodeType,
        inputs: {
          task_mode: inputs.task_mode,
          user_intent: inputs.user_intent,
          duration_seconds: durationInput,
        },
      },
      "2": {
        class_type: planNodeType,
        inputs: { request: ["1", 0], reference_registry: ["9", 0] },
      },
      "3": { class_type: compilerNodeType, inputs: { plan: ["2", 0] } },
      "4": {
        class_type: validatorNodeType,
        inputs: { plan: ["2", 0], prompt_document: ["3", 2] },
      },
      "5": {
        class_type: nativeAdapterNodeType,
        inputs: { report: ["4", 1] },
      },
      "8": {
        class_type: productShellNodeType,
        inputs: { report: ["4", 1], native_h3_wiring: ["5", 1] },
      },
      "6": { class_type: referenceGenerationNodeType, inputs: nativeInputs },
      "7": { class_type: previewNodeType, inputs: { report: ["4", 1] } },
      "9": {
        class_type: referenceRegistryNodeType,
        inputs: registryInputs,
      },
      ...sourceNodes,
      ...durationNodes,
      ...sink("6"),
    };
  }
  const requiredRoles =
    inputs.task_mode === "i2va"
      ? (["first_frame"] as const)
      : inputs.task_mode === "l2va"
        ? (["last_frame"] as const)
        : inputs.task_mode === "fl2va"
          ? (["first_frame", "last_frame"] as const)
          : ([] as const);
  const sourceNodes: Record<string, Record<string, unknown>> = {};
  const sourceLinks: Record<string, [string, number]> = {};
  for (const [index, role] of requiredRoles.entries()) {
    const source = opaqueSources[role];
    if (source === undefined || source.class_type !== loadImageNodeType)
      throw new AppModeError(
        "incompatible_graph",
        "the selected host-owned image source is unavailable",
      );
    const id = String(9 + index);
    // IMPORTANT: copy only the already-qualified compiled node envelope. The
    // UI receives its content-free node identity, never the private widget value.
    sourceNodes[id] = {
      class_type: source.class_type,
      inputs: { ...(record(source.inputs) ?? {}) },
    };
    sourceLinks[role] = [id, 0];
  }
  const frameRegistryId = "11";
  const frameRegistryInputs = Object.fromEntries(
    requiredRoles.map((role) => [role, sourceLinks[role]]),
  );
  const frameRegistry =
    requiredRoles.length === 0
      ? {}
      : {
          [frameRegistryId]: {
            class_type: referenceRegistryNodeType,
            inputs: frameRegistryInputs,
          },
        };
  return {
    "1": {
      class_type: "comfyui_h3_context.H3Context.Request",
      inputs: {
        task_mode: inputs.task_mode,
        user_intent: inputs.user_intent,
        duration_seconds: durationInput,
      },
    },
    "2": {
      class_type: "comfyui_h3_context.H3Context.Plan",
      inputs: {
        request: ["1", 0],
        ...(requiredRoles.length === 0
          ? {}
          : { reference_registry: [frameRegistryId, 0] }),
      },
    },
    "3": {
      class_type: "comfyui_h3_context.H3Context.Compiler",
      inputs: { plan: ["2", 0] },
    },
    "4": {
      class_type: "comfyui_h3_context.H3Context.Validator",
      inputs: { plan: ["2", 0], prompt_document: ["3", 2] },
    },
    "5": {
      class_type: "comfyui_h3_context.H3Context.NativeH3Adapter",
      inputs: { report: ["4", 1] },
    },
    "8": {
      class_type: "comfyui_h3_context.H3Context.ProductShell",
      inputs: { report: ["4", 1], native_h3_wiring: ["5", 1] },
    },
    "6": {
      class_type: "MiniMaxH3ImageToVideo",
      inputs: {
        ...modelInputs,
        prompt: ["8", 0],
        ...sourceLinks,
        width: 512,
        height: 512,
        length: lengthInput,
      },
    },
    "7": {
      class_type: "comfyui_h3_context.H3Context.Preview",
      inputs: { report: ["4", 1] },
    },
    ...sourceNodes,
    ...frameRegistry,
    ...durationNodes,
    ...sink("6"),
  };
}
