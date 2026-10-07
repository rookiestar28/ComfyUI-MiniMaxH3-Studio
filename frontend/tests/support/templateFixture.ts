/**
 * A synthetic stand-in for a pinned official generation template.
 *
 * The real templates are vendored read-only under `reference/`, which is not
 * part of a clean checkout, so the splice is exercised against the real bytes in
 * `templateMaterialization.test.ts` (guarded on the corpus being present) and
 * against this fixture everywhere a host seam has to be driven end to end.
 *
 * It reproduces the parts of the template shape the App Mode route depends on:
 * a native H3 anchor whose `prompt` is a widget, a `PrimitiveFloat ->
 * ComfyMathExpression -> length` chain carrying the official expression, the
 * frame loaders the anchor reads, and a `CreateVideo -> SaveVideo` sink at 24
 * fps. The loader ids are 9 and 10 because that is what a materialized graph's
 * compiled prompt reports for the first and last frame roles, and several route
 * assertions are about exactly that identity.
 *
 * It carries neither subgraph definitions nor a root graph `id`, which is the
 * only candidate shape the detached compile still serves (B-M1605-EXIST-01): a
 * root id sends the candidate through the host load before its root compile.
 * Tests of that route add the id explicitly.
 */

export type Json = Record<string, unknown>;

export const SYNTHETIC_LENGTH_EXPRESSION =
  "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17";

function link(
  id: number,
  originId: number,
  originSlot: number,
  targetId: number,
  targetSlot: number,
  type: string,
): unknown[] {
  return [id, originId, originSlot, targetId, targetSlot, type];
}

export function syntheticTemplate(name: string): Json {
  const reference = name.endsWith("_r2v");
  const anchorType = reference
    ? "MiniMaxH3ReferenceToVideo"
    : "MiniMaxH3ImageToVideo";
  // The sockets the pinned host publishes for each anchor, in its own order:
  // the fixed ones, then the autogrow group members a reference graph binds.
  const anchorInputs = reference
    ? [
        { name: "clip", type: "CLIP", link: null },
        { name: "vae", type: "VAE", link: null },
        { name: "audio_vae", type: "VAE", link: null },
        { name: "ref_images.ref_image_0", type: "IMAGE", shape: 7, link: 20 },
        { name: "ref_images.ref_image_1", type: "IMAGE", shape: 7, link: 21 },
        { name: "ref_videos.ref_video_0", type: "IMAGE", shape: 7, link: null },
        {
          name: "ref_video_audios.ref_video_audio_0",
          type: "AUDIO",
          shape: 7,
          link: null,
        },
        { name: "ref_audios.ref_audio_0", type: "AUDIO", shape: 7, link: null },
        { name: "prompt", type: "STRING", link: null },
        { name: "width", type: "INT", widget: { name: "width" }, link: null },
        { name: "height", type: "INT", widget: { name: "height" }, link: null },
        { name: "length", type: "INT", widget: { name: "length" }, link: 11 },
        {
          name: "ref_image_size",
          type: "COMBO",
          widget: { name: "ref_image_size" },
          link: null,
        },
      ]
    : [
        { name: "clip", type: "CLIP", link: null },
        { name: "vae", type: "VAE", link: null },
        { name: "prompt", type: "STRING", link: null },
        { name: "width", type: "INT", widget: { name: "width" }, link: null },
        { name: "height", type: "INT", widget: { name: "height" }, link: null },
        { name: "length", type: "INT", widget: { name: "length" }, link: 11 },
        { name: "first_frame", type: "IMAGE", shape: 7, link: 20 },
        { name: "last_frame", type: "IMAGE", shape: 7, link: 21 },
      ];

  return {
    last_node_id: 10,
    last_link_id: 21,
    nodes: [
      {
        id: 1,
        type: "PrimitiveFloat",
        pos: [0, 0],
        mode: 0,
        inputs: [],
        outputs: [{ name: "FLOAT", type: "FLOAT", links: [10] }],
        widgets_values: [5],
      },
      {
        id: 2,
        type: "ComfyMathExpression",
        pos: [0, 100],
        mode: 0,
        inputs: [
          { name: "values.a", type: "FLOAT,INT,BOOLEAN", link: 10 },
          { name: "values.b", type: "FLOAT,INT,BOOLEAN", shape: 7, link: null },
        ],
        outputs: [{ name: "INT", type: "INT", links: [11] }],
        widgets_values: [SYNTHETIC_LENGTH_EXPRESSION],
      },
      {
        id: 3,
        type: anchorType,
        pos: [200, 0],
        mode: 0,
        inputs: anchorInputs,
        // The pinned host names these sockets `positive` and `LATENT`; the
        // fixture has to agree with the host, not with the type names.
        outputs: [
          { name: "positive", type: "CONDITIONING", links: [] },
          { name: "LATENT", type: "LATENT", links: [12] },
        ],
        widgets_values: reference ? [512, 512, 124, "match"] : [512, 512, 124],
      },
      {
        id: 4,
        type: "VAEDecode",
        pos: [400, 0],
        mode: 0,
        inputs: [{ name: "samples", type: "LATENT", link: 12 }],
        outputs: [{ name: "IMAGE", type: "IMAGE", links: [13] }],
        widgets_values: [],
      },
      {
        id: 5,
        type: "CreateVideo",
        pos: [600, 0],
        mode: 0,
        inputs: [{ name: "images", type: "IMAGE", link: 13 }],
        outputs: [{ name: "VIDEO", type: "VIDEO", links: [14] }],
        widgets_values: [24, 8],
      },
      {
        id: 6,
        type: "SaveVideo",
        pos: [800, 0],
        mode: 0,
        inputs: [{ name: "video", type: "VIDEO", link: 14 }],
        outputs: [],
        widgets_values: ["video/MiniMax_H3", "auto", "auto"],
      },
      {
        id: 9,
        type: "LoadImage",
        pos: [-200, 0],
        mode: 0,
        inputs: [],
        outputs: [
          { name: "IMAGE", type: "IMAGE", links: [20] },
          { name: "MASK", type: "MASK", links: [] },
        ],
        widgets_values: ["template-first.png", "image"],
      },
      {
        id: 10,
        type: "LoadImage",
        pos: [-200, 160],
        mode: 0,
        inputs: [],
        outputs: [
          { name: "IMAGE", type: "IMAGE", links: [21] },
          { name: "MASK", type: "MASK", links: [] },
        ],
        widgets_values: ["template-last.png", "image"],
      },
    ],
    links: [
      link(10, 1, 0, 2, 0, "FLOAT"),
      link(11, 2, 0, 3, reference ? 11 : 5, "INT"),
      link(12, 3, 1, 4, 0, "LATENT"),
      link(13, 4, 0, 5, 0, "IMAGE"),
      link(14, 5, 0, 6, 0, "VIDEO"),
      link(20, 9, 0, 3, reference ? 3 : 6, "IMAGE"),
      link(21, 10, 0, 3, reference ? 4 : 7, "IMAGE"),
    ],
    definitions: { subgraphs: [] },
  };
}

export function loadSyntheticTemplate(name: string): Json {
  return syntheticTemplate(name);
}

/**
 * A small, stable description of a materialized graph.
 *
 * Route tests care about four things: which anchor the mode landed on, what the
 * Request node was authored with, that the anchor reads its prompt from a link
 * rather than a widget, and that the template's sink survived. Asserting the
 * whole workflow instead would make every test a change detector for template
 * layout.
 */
export function materializedGraphSummary(graph: unknown): {
  anchorType: string | undefined;
  requestWidgets: unknown[] | undefined;
  promptIsLinked: boolean;
  sinks: number;
} {
  const nodes = ((graph as Json | undefined)?.nodes ?? []) as Json[];
  const anchor = nodes.find(
    (node) =>
      node.type === "MiniMaxH3ImageToVideo" ||
      node.type === "MiniMaxH3ReferenceToVideo",
  );
  const request = nodes.find(
    (node) => node.type === "comfyui_h3_context.H3Context.Request",
  );
  const prompt = (
    ((anchor?.inputs ?? []) as Json[]).find(
      (input) => input.name === "prompt",
    ) ?? {}
  ).link;
  return {
    anchorType: anchor?.type as string | undefined,
    requestWidgets: request?.widgets_values as unknown[] | undefined,
    promptIsLinked: typeof prompt === "number",
    sinks: nodes.filter((node) => node.type === "SaveVideo").length,
  };
}
