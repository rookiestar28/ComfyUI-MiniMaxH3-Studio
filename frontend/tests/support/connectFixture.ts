export type Json = Record<string, unknown>;

/**
 * A canvas this repository did not build.
 *
 * M17-20 D13 tier B is about arbitrary community graphs, so the fixture is
 * deliberately not a template: it carries node types this repository has never
 * heard of, wires them to each other, and puts one or more official native H3
 * conditioning anchors somewhere in the middle. The only thing the splice is
 * allowed to recognise is the anchor.
 */

type AnchorSpec = {
  readonly id: number;
  readonly type?: string;
  /** Frame roles that arrive as links, which is what decides the task mode. */
  readonly frames?: readonly ("first_frame" | "last_frame")[];
  /**
   * Drive `length` from the official duration chain, as every pinned template
   * does: `PrimitiveFloat -> ComfyMathExpression -> length`. Without this the
   * anchor carries a literal frame count and nothing on the canvas holds a
   * duration the splice could overwrite.
   */
  readonly durationSeconds?: number;
};

export const OFFICIAL_LENGTH_EXPRESSION =
  "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17";

const IMAGE_ANCHOR = "MiniMaxH3ImageToVideo";

function anchorNode(spec: AnchorSpec, links: unknown[][]): Json {
  const type = spec.type ?? IMAGE_ANCHOR;
  const reference = type === "MiniMaxH3ReferenceToVideo";
  const frames = spec.frames ?? [];
  // Every anchor gets its own link ids so two of them on one canvas never
  // collide, which is the case the designation rows are about.
  const base = spec.id * 10;
  const inputs: Json[] = [
    { name: "clip", type: "CLIP", link: base },
    { name: "vae", type: "VAE", link: base + 1 },
  ];
  links.push(
    [base, 1, 0, spec.id, 0, "CLIP"],
    [base + 1, 1, 1, spec.id, 1, "VAE"],
  );
  if (reference) inputs.push({ name: "audio_vae", type: "VAE", link: null });
  const durationLink = spec.durationSeconds === undefined ? null : base + 5;
  inputs.push(
    { name: "width", type: "INT", link: null, widget: { name: "width" } },
    { name: "height", type: "INT", link: null, widget: { name: "height" } },
    {
      name: "length",
      type: "INT",
      link: durationLink,
      widget: { name: "length" },
    },
  );
  if (!reference)
    for (const role of ["first_frame", "last_frame"] as const) {
      const linked = frames.includes(role);
      const linkId = base + (role === "first_frame" ? 2 : 3);
      if (linked) links.push([linkId, 2, 0, spec.id, inputs.length, "IMAGE"]);
      inputs.push({
        name: role,
        type: "IMAGE",
        shape: 7,
        link: linked ? linkId : null,
      });
    }
  return {
    id: spec.id,
    type,
    pos: [400, spec.id * 200],
    size: [320, 200],
    flags: {},
    order: spec.id,
    mode: 0,
    inputs,
    outputs: [
      { name: "positive", type: "CONDITIONING", links: [] },
      { name: "LATENT", type: "LATENT", links: [] },
    ],
    properties: {},
    // `prompt` is a widget here, exactly as a community workflow ships it. The
    // splice's whole job is to turn it into a link.
    widgets_values: [
      "a prompt the user typed on their own canvas",
      1344,
      768,
      124,
    ],
  };
}

/**
 * @param anchors the native anchors to place; foreign nodes surround them.
 */
export function communityCanvas(
  anchors: readonly AnchorSpec[] = [{ id: 20 }],
): Json {
  const nodes: Json[] = [
    {
      id: 1,
      type: "SomeoneElsesLoader",
      pos: [0, 0],
      size: [280, 100],
      flags: {},
      order: 0,
      mode: 0,
      inputs: [],
      outputs: [
        { name: "CLIP", type: "CLIP", links: [] },
        { name: "VAE", type: "VAE", links: [] },
      ],
      properties: { note: "not ours" },
      widgets_values: ["whatever.safetensors"],
    },
    {
      id: 2,
      type: "SomeoneElsesImageSource",
      pos: [0, 200],
      size: [280, 100],
      flags: {},
      order: 1,
      mode: 0,
      inputs: [],
      outputs: [{ name: "IMAGE", type: "IMAGE", links: [] }],
      properties: {},
      widgets_values: ["their-frame.png"],
    },
    {
      id: 3,
      type: "SomeoneElsesSampler",
      pos: [800, 0],
      size: [280, 140],
      flags: {},
      order: 5,
      mode: 0,
      inputs: [{ name: "latent", type: "LATENT", link: null }],
      outputs: [{ name: "IMAGE", type: "IMAGE", links: [10] }],
      properties: {},
      widgets_values: [42, "their_sampler"],
    },
    {
      id: 4,
      type: "CreateVideo",
      pos: [1100, 0],
      size: [240, 80],
      flags: {},
      order: 6,
      mode: 0,
      inputs: [{ name: "images", type: "IMAGE", link: 10 }],
      outputs: [{ name: "VIDEO", type: "VIDEO", links: [11] }],
      properties: {},
      widgets_values: [24, 8],
    },
    {
      id: 5,
      type: "SaveVideo",
      pos: [1400, 0],
      size: [240, 80],
      flags: {},
      order: 7,
      mode: 0,
      inputs: [{ name: "video", type: "VIDEO", link: 11 }],
      outputs: [],
      properties: {},
      widgets_values: ["their-project/take-1", "auto", "auto"],
    },
  ];
  const links: unknown[][] = [
    [10, 3, 0, 4, 0, "IMAGE"],
    [11, 4, 0, 5, 0, "VIDEO"],
  ];
  for (const spec of anchors) nodes.push(anchorNode(spec, links));
  for (const spec of anchors) {
    if (spec.durationSeconds === undefined) continue;
    const base = spec.id * 10;
    const primitiveId = spec.id * 100;
    const mathId = primitiveId + 1;
    nodes.push(
      {
        id: primitiveId,
        type: "PrimitiveFloat",
        pos: [0, 600],
        size: [240, 60],
        flags: {},
        order: 2,
        mode: 0,
        inputs: [],
        outputs: [{ name: "FLOAT", type: "FLOAT", links: [base + 4] }],
        properties: {},
        widgets_values: [spec.durationSeconds],
      },
      {
        id: mathId,
        type: "ComfyMathExpression",
        pos: [300, 600],
        size: [240, 60],
        flags: {},
        order: 3,
        mode: 0,
        inputs: [
          { name: "values.a", type: "FLOAT,INT,BOOLEAN", link: base + 4 },
        ],
        outputs: [{ name: "INT", type: "INT", links: [base + 5] }],
        properties: {},
        widgets_values: [OFFICIAL_LENGTH_EXPRESSION],
      },
    );
    const anchor = nodes.find((node) => node.id === spec.id)!;
    const lengthSlot = ((anchor.inputs as Json[]) ?? []).findIndex(
      (input) => input.name === "length",
    );
    links.push(
      [base + 4, primitiveId, 0, mathId, 0, "FLOAT"],
      [base + 5, mathId, 0, spec.id, lengthSlot, "INT"],
    );
  }
  const loaderLinks = links
    .filter((entry) => entry[1] === 1)
    .map((entry) => entry[0] as number);
  (nodes[0]!.outputs as Json[])[0]!.links = loaderLinks.filter(
    (id) => id % 10 === 0,
  );
  (nodes[0]!.outputs as Json[])[1]!.links = loaderLinks.filter(
    (id) => id % 10 === 1,
  );
  (nodes[1]!.outputs as Json[])[0]!.links = links
    .filter((entry) => entry[1] === 2)
    .map((entry) => entry[0] as number);
  return {
    id: "community-workflow",
    revision: 0,
    last_node_id: Math.max(20, ...anchors.map((spec) => spec.id)) + 1,
    last_link_id: Math.max(40, ...links.map((entry) => Number(entry[0]))) + 1,
    nodes,
    links,
    groups: [],
    config: {},
    extra: {},
    version: 0.4,
  };
}

/** The foreign nodes, deep-copied, so a mutation anywhere in them is visible. */
export function foreignNodes(workflow: Json): Json[] {
  const nodes = (workflow.nodes as Json[]) ?? [];
  return structuredClone(
    nodes.filter(
      (node) =>
        !String(node.type ?? "").startsWith("MiniMaxH3") &&
        !String(node.type ?? "").startsWith("comfyui_h3_context."),
    ),
  );
}
