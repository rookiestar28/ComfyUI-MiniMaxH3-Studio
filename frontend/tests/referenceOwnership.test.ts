import { describe, expect, it } from "vitest";
import { compiledPromptMatchesRequestedRoute } from "../src/host/appModeExistingRoute";
import {
  type AppModeInputs,
  type ProductionCanonicalLowering,
} from "../src/host/appModeContract";
import {
  spliceContextPipeline,
  TemplateSpliceError,
  type Json,
} from "../src/host/templateMaterialization";
import { syntheticTemplate } from "./support/templateFixture";

const inputs: AppModeInputs = {
  task_mode: "ref2va",
  user_intent: "Preserve selected order.",
  duration_milliseconds: 5167,
  frame_count: 124,
  reference_image_sources: ["70", "71"],
  reference_video_sources: ["80", "81"],
  reference_audio_sources: ["90"],
  reference_video_soundtrack: "included",
};
const ids = {
  referenceImages: [10, 11],
  referenceVideos: [20, 21],
  referenceAudios: [30],
};
const lowering: ProductionCanonicalLowering = {
  schema: "h3.context.production_canonical_lowering.v1",
  canonicalPrompt: "Approved prompt",
  baseReportFingerprint: "a".repeat(64),
  baseReportRevision: 1,
  overrideRevision: 1,
  reason: "Materialize approved Production segment prompt",
};
type Node = { class_type: string; inputs: Record<string, unknown> };
function output(): Record<string, Node> {
  return {
    "1": {
      class_type: "comfyui_h3_context.H3Context.Request",
      inputs: {
        task_mode: inputs.task_mode,
        user_intent: inputs.user_intent,
        duration_seconds: ["2", 0],
      },
    },
    "2": { class_type: "PrimitiveFloat", inputs: { value: 5.167 } },
    "3": {
      class_type: "MiniMaxH3ReferenceToVideo",
      inputs: {
        "ref_images.ref_image_0": ["10", 0],
        "ref_images.ref_image_1": ["11", 0],
        "ref_videos.ref_video_0": ["40", 0],
        "ref_videos.ref_video_1": ["41", 0],
        "ref_video_audios.ref_video_audio_0": ["40", 1],
        "ref_video_audios.ref_video_audio_1": ["41", 1],
        "ref_audios.ref_audio_0": ["30", 0],
      },
    },
    "4": {
      class_type: "comfyui_h3_context.H3Context.ReferenceRegistry",
      inputs: {
        "images.image0": ["10", 0],
        "images.image1": ["11", 0],
        "videos.video0": ["20", 0],
        "videos.video1": ["21", 0],
        "paired_audios.paired_audio0": ["40", 1],
        "paired_audios.paired_audio1": ["41", 1],
        "audios.audio0": ["30", 0],
      },
    },
    "5": {
      class_type: "comfyui_h3_context.H3Context.AuditOverride",
      inputs: {
        base_report_fingerprint: lowering.baseReportFingerprint,
        revision: lowering.overrideRevision,
        reason: lowering.reason,
        prompt_text: lowering.canonicalPrompt,
      },
    },
    "40": { class_type: "GetVideoComponents", inputs: { video: ["20", 0] } },
    "41": { class_type: "GetVideoComponents", inputs: { video: ["21", 0] } },
  };
}
describe("reference ownership regressions", () => {
  it.each(["materialize", "prepared"] as const)(
    "checks ordered source and component identity on %s",
    (route) => {
      const matches = (value: Record<string, Node>) =>
        compiledPromptMatchesRequestedRoute(
          { output: value, workflow: {} },
          inputs,
          route,
          ids,
          undefined,
          lowering,
        );
      expect(matches(output())).toBe(true);
      const mutations: ((value: Record<string, Node>) => void)[] = [
        (value) => {
          value["3"]!.inputs["ref_images.ref_image_0"] = ["11", 0];
          value["4"]!.inputs["images.image0"] = ["11", 0];
          value["3"]!.inputs["ref_images.ref_image_1"] = ["10", 0];
          value["4"]!.inputs["images.image1"] = ["10", 0];
        },
        (value) => {
          value["3"]!.inputs["ref_videos.ref_video_0"] = ["41", 0];
          value["3"]!.inputs["ref_videos.ref_video_1"] = ["40", 0];
          value["4"]!.inputs["videos.video0"] = ["21", 0];
          value["4"]!.inputs["videos.video1"] = ["20", 0];
        },
        (value) => {
          value["3"]!.inputs["ref_audios.ref_audio_0"] = ["31", 0];
          value["4"]!.inputs["audios.audio0"] = ["31", 0];
        },
        (value) => {
          value["4"]!.inputs["paired_audios.paired_audio0"] = ["41", 1];
        },
        (value) => {
          value["40"]!.inputs.video = ["21", 0];
        },
        (value) => {
          value["3"]!.inputs["ref_videos.ref_video_0"] = [["40"], 0];
        },
        (value) => {
          value["3"]!.inputs["ref_video_audios.ref_video_audio_0"] = ["40", 0];
        },
        (value) => {
          value["3"]!.inputs.ref_images = [
            ["10", 0],
            ["11", 0],
          ];
          delete value["3"]!.inputs["ref_images.ref_image_0"];
          delete value["3"]!.inputs["ref_images.ref_image_1"];
        },
      ];
      for (const mutate of mutations) {
        const value = output();
        mutate(value);
        expect(matches(value)).toBe(false);
      }
    },
  );
  it("preserves graph-owned connect semantics", () => {
    const value = output();
    value["3"]!.inputs["ref_images.ref_image_0"] = ["99", 0];
    expect(
      compiledPromptMatchesRequestedRoute(
        { output: value, workflow: {} },
        inputs,
        "connect",
      ),
    ).toBe(true);
  });
  it("gives every created loader a distinct canvas row", () => {
    const result = spliceContextPipeline(
      syntheticTemplate("video_minimax_h3_r2v"),
      {
        taskMode: "ref2va",
        userIntent: "Keep order.",
        durationSeconds: 5.167,
        media: {
          referenceImages: Array.from({ length: 6 }, () => ({
            type: "LoadImage",
            widgetValues: ["opaque.png"],
          })),
          referenceVideos: Array.from({ length: 2 }, () => ({
            type: "LoadVideo",
            widgetValues: ["opaque.mp4"],
          })),
          referenceAudios: Array.from({ length: 2 }, () => ({
            type: "LoadAudio",
            widgetValues: ["opaque.wav"],
          })),
          referenceVideoSoundtrack: "excluded",
        },
      },
    );
    const created = (result.workflow.nodes as Json[]).filter(
      (node) =>
        Array.isArray(node.pos) &&
        node.pos[0] === -2900 &&
        ["LoadImage", "LoadVideo", "LoadAudio"].includes(String(node.type)),
    );
    expect(created.length).toBeGreaterThan(2);
    expect(new Set(created.map((node) => JSON.stringify(node.pos))).size).toBe(
      created.length,
    );
  });
  it("refuses aggregate over-capacity before changing a template", () => {
    const template = syntheticTemplate("video_minimax_h3_r2v");
    const before = JSON.stringify(template);
    expect(() =>
      spliceContextPipeline(template, {
        taskMode: "ref2va",
        userIntent: "Bound files.",
        durationSeconds: 5.167,
        media: {
          referenceImages: Array.from({ length: 9 }, () => ({
            type: "LoadImage",
            widgetValues: [],
          })),
          referenceVideos: Array.from({ length: 3 }, () => ({
            type: "LoadVideo",
            widgetValues: [],
          })),
          referenceVideoSoundtrack: "included",
        },
      }),
    ).toThrow(TemplateSpliceError);
    expect(JSON.stringify(template)).toBe(before);
  });
});
