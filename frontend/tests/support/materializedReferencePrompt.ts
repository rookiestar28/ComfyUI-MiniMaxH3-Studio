/** A host-seam test double whose media ids belong to the serialized synthetic template. */
import {
  spliceContextPipeline,
  type Json,
  type LoaderSource,
} from "../../src/host/templateMaterialization";
import { createQualifiedBasePrompt } from "./qualifiedBasePrompt";
import { syntheticTemplate } from "./templateFixture";

export function createMaterializedReferencePrompt(
  inputs: Parameters<typeof createQualifiedBasePrompt>[0],
  opaque: Parameters<typeof createQualifiedBasePrompt>[1] = {},
  options: Parameters<typeof createQualifiedBasePrompt>[2] = {},
) {
  const sources = (
    kind: "reference_images" | "reference_videos" | "reference_audios",
  ): LoaderSource[] =>
    (opaque[kind] ?? []).map((node) => ({
      type: String(node.class_type),
      widgetValues: Object.values(node.inputs as Record<string, unknown>),
    }));
  const spliced = spliceContextPipeline(
    syntheticTemplate("video_minimax_h3_r2v"),
    {
      taskMode: "ref2va",
      userIntent: inputs.user_intent,
      durationSeconds: inputs.duration_milliseconds / 1000,
      media: {
        referenceImages: sources("reference_images"),
        referenceVideos: sources("reference_videos"),
        referenceAudios: sources("reference_audios"),
        referenceVideoSoundtrack:
          inputs.reference_video_soundtrack ?? "included",
      },
    },
  );
  const prompt = createQualifiedBasePrompt(inputs, opaque, {
    ...options,
    sharedDurationSource: true,
  }) as Record<string, { class_type: string; inputs: Record<string, unknown> }>;
  const nodes = spliced.workflow.nodes as Json[];
  const mapping = new Map<string, string>();
  const spent = new Set<string>();
  let fallback = 9000;
  for (const [id, node] of Object.entries(prompt)) {
    const actual = nodes.find(
      (value) => value.type === node.class_type && !spent.has(String(value.id)),
    );
    const next = actual === undefined ? String(fallback++) : String(actual.id);
    mapping.set(id, next);
    spent.add(next);
  }
  const result: typeof prompt = {};
  for (const [id, node] of Object.entries(prompt)) {
    const inputs = Object.fromEntries(
      Object.entries(node.inputs).map(([name, value]) => [
        name,
        Array.isArray(value) &&
        value.length === 2 &&
        mapping.has(String(value[0]))
          ? [mapping.get(String(value[0])), value[1]]
          : value,
      ]),
    );
    result[mapping.get(id)!] = { class_type: node.class_type, inputs };
  }
  return result;
}
