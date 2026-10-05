/**
 * M17-20 phase 2, decision D11 option (b): App Mode materializes by loading the
 * pinned official template workflow and splicing the H3 context pipeline into
 * it, rather than by expanding the template into an API prompt.
 *
 * The whole module is pure. It takes a template workflow and returns a new one;
 * it never touches the host, never fetches, and never mutates its input. That is
 * what lets the splice be tested against the real pinned template bytes without
 * a ComfyUI running, which is the only way the anchor-resolution rules below can
 * be trusted -- they are rules about a specific serialized shape, and reasoning
 * about that shape without exercising it is how the previous truncated graph
 * survived as long as it did.
 *
 * Two shapes matter and they are not the same:
 *
 *  - a **top-level** litegraph link is the array `[id, originId, originSlot,
 *    targetId, targetSlot, type]`;
 *  - a link inside a **subgraph definition** is an object with those fields
 *    named.
 *
 * Writing one where the other belongs produces a workflow the host loads without
 * complaint and then cannot execute, so the two are kept apart deliberately.
 */

import {
  definitionEdges,
  nodeIdentifier,
  readQueueMetadataCompatibleGraph,
  tupleEdges,
  validateSerializedLinks,
} from "./graphSerialization";
import {
  FRAME_BINDINGS,
  FAMILY_ANCHOR,
  I2VA_SCALE_NODE_TYPE,
  I2VA_SCALE_WIDGET_VALUES,
  I2VA_SIZE_NODE_TYPE,
  MAX_REFERENCE_AUDIOS,
  MAX_REFERENCE_IMAGES,
  MAX_REFERENCE_VIDEOS,
  PROMPT_SOURCE_KEY,
  PROMPT_SOURCE_SLOT,
  TASK_MODE_FAMILY,
  TemplateSpliceError,
  anchorTaskMode,
  detachInput,
  hasContextPipelineOwnership,
  highestId,
  isRecord,
  linkList,
  nodeList,
  resolveAnchor,
  subgraphDefinitions,
  widgetOrder,
  type Json,
  type LoaderSource,
  type ModeFamily,
  type ReferenceVideoSoundtrack,
  type SpliceMedia,
  type SpliceOptions,
  type SpliceResult,
} from "./templateGraphTopology";

export {
  FAMILY_ANCHOR,
  FRAME_BINDINGS,
  I2VA_SCALE_NODE_TYPE,
  I2VA_SCALE_WIDGET_VALUES,
  I2VA_SIZE_NODE_TYPE,
  MAX_REFERENCE_AUDIOS,
  MAX_REFERENCE_IMAGES,
  MAX_REFERENCE_VIDEOS,
  MODE_TEMPLATE,
  OFFICIAL_LENGTH_EXPRESSION,
  TASK_MODE_FAMILY,
  TemplateSpliceError,
  admitCanvasForConnect,
  anchorTaskMode,
  hasContextPipelineOwnership,
  listNativeAnchors,
  rebindExistingContextAuthoring,
  resolveAnchor,
} from "./templateGraphTopology";
export type {
  CanvasAdmission,
  ExistingContextAuthoringOptions,
  ExistingContextAuthoringResult,
  Json,
  LoaderSource,
  ModeFamily,
  NativeAnchor,
  ReferenceVideoSoundtrack,
  SpliceMedia,
  SpliceMediaIds,
  SpliceOptions,
  SpliceResult,
} from "./templateGraphTopology";

/**
 * The context pipeline, minus the conditioning node.
 *
 * The shipped `H3 Context Assistant - Base` subgraph carries its own
 * `MiniMaxH3ImageToVideo`, which is the truncated historical graph M17-20 exists
 * to replace. Splicing that package into a template would leave two anchors on
 * one canvas and no way to say which one the prompt belongs to, so the pipeline
 * is declared here without it: the template already owns the conditioning node,
 * and this contributes only the prompt that feeds it.
 */
type PipelineNode = {
  readonly key: string;
  readonly type: string;
  readonly inputs: readonly {
    readonly name: string;
    readonly type: string;
    readonly from?: readonly [string, number];
    readonly widget?: boolean;
    readonly optional?: boolean;
  }[];
  readonly outputs: readonly { readonly name: string; readonly type: string }[];
};

export const CONTEXT_PIPELINE: readonly PipelineNode[] = Object.freeze([
  {
    key: "request",
    type: "comfyui_h3_context.H3Context.Request",
    inputs: [
      { name: "task_mode", type: "COMBO", widget: true },
      { name: "user_intent", type: "STRING", widget: true },
      { name: "duration_seconds", type: "FLOAT", widget: true },
      // Declared and unbound. The host reconciles a node's sockets against its
      // own definition on load, but a workflow this repository hands over should
      // already be the shape the host publishes rather than rely on that.
      { name: "hard_constraints", type: "H3_HARD_CONSTRAINTS", optional: true },
    ],
    outputs: [{ name: "request", type: "H3_CONTEXT_REQUEST" }],
  },
  {
    key: "plan",
    type: "comfyui_h3_context.H3Context.Plan",
    inputs: [
      { name: "request", type: "H3_CONTEXT_REQUEST", from: ["request", 0] },
      {
        name: "reference_registry",
        type: "H3_REFERENCE_REGISTRY",
        optional: true,
      },
      { name: "intent_graph", type: "H3_INTENT_GRAPH", optional: true },
    ],
    outputs: [
      { name: "plan", type: "H3_CONTEXT_PLAN" },
      { name: "report", type: "H3_CONTEXT_REPORT" },
    ],
  },
  {
    key: "compiler",
    type: "comfyui_h3_context.H3Context.Compiler",
    inputs: [{ name: "plan", type: "H3_CONTEXT_PLAN", from: ["plan", 0] }],
    outputs: [
      { name: "prompt", type: "H3_PROMPT_STRING" },
      { name: "report", type: "H3_CONTEXT_REPORT" },
      { name: "prompt_document", type: "H3_PROMPT_DOCUMENT" },
    ],
  },
  {
    key: "validator",
    type: "comfyui_h3_context.H3Context.Validator",
    inputs: [
      { name: "plan", type: "H3_CONTEXT_PLAN", from: ["plan", 0] },
      {
        name: "prompt_document",
        type: "H3_PROMPT_DOCUMENT",
        from: ["compiler", 2],
      },
    ],
    outputs: [
      { name: "validation", type: "H3_VALIDATION_RESULT" },
      { name: "validated_report", type: "H3_CONTEXT_REPORT" },
    ],
  },
  {
    key: "native",
    type: "comfyui_h3_context.H3Context.NativeH3Adapter",
    inputs: [
      { name: "report", type: "H3_CONTEXT_REPORT", from: ["validator", 1] },
    ],
    outputs: [
      { name: "prompt", type: "H3_PROMPT_STRING" },
      { name: "native_h3_wiring", type: "H3_NATIVE_H3_WIRING" },
    ],
  },
  {
    key: "preview",
    type: "comfyui_h3_context.H3Context.Preview",
    inputs: [
      { name: "report", type: "H3_CONTEXT_REPORT", from: ["validator", 1] },
    ],
    outputs: [
      { name: "prompt", type: "H3_PROMPT_STRING" },
      { name: "preview", type: "H3_CONTEXT_PREVIEW" },
    ],
  },
  {
    key: "shell",
    type: "comfyui_h3_context.H3Context.ProductShell",
    inputs: [
      { name: "report", type: "H3_CONTEXT_REPORT", from: ["validator", 1] },
      {
        name: "native_h3_wiring",
        type: "H3_NATIVE_H3_WIRING",
        from: ["native", 1],
      },
    ],
    outputs: [
      { name: "prompt", type: "STRING" },
      { name: "product_shell", type: "H3_PRODUCT_SHELL" },
    ],
  },
]);

export const LOADER_TYPES: Readonly<
  Record<"image" | "video" | "audio", string>
> = Object.freeze({
  image: "LoadImage",
  video: "LoadVideo",
  audio: "LoadAudio",
});

const VIDEO_COMPONENTS_TYPE = "GetVideoComponents";
const REGISTRY_KEY = "registry";
const REGISTRY_TYPE = "comfyui_h3_context.H3Context.ReferenceRegistry";
/**
 * The registry node, sized to the reference set it will carry.
 *
 * The socket names are the autogrow ones the shipped Reference subgraph
 * serializes (`images.image0`, `videos.video0`, `audios.audio0`), not the group
 * names the API prompt uses. The host derives the API name from the serialized
 * socket, so writing the API spelling here would produce a graph that loads and
 * then compiles to a registry with no references in it.
 */
function referenceRegistrySpec(counts: ReferenceCounts): PipelineNode {
  const inputs: PipelineNode["inputs"] = [
    ...(counts.firstFrame
      ? [{ name: "first_frame", type: "IMAGE" } as const]
      : []),
    ...(counts.lastFrame
      ? [{ name: "last_frame", type: "IMAGE" } as const]
      : []),
    ...Array.from({ length: counts.images }, (_unused, index) => ({
      name: `images.image${index}`,
      type: "IMAGE",
    })),
    ...Array.from({ length: counts.videos }, (_unused, index) => ({
      name: `videos.video${index}`,
      type: "VIDEO",
    })),
    ...Array.from({ length: counts.pairedAudios }, (_unused, index) => ({
      name: `paired_audios.paired_audio${index}`,
      type: "AUDIO",
    })),
    ...Array.from({ length: counts.audios }, (_unused, index) => ({
      name: `audios.audio${index}`,
      type: "AUDIO",
    })),
  ];
  return {
    key: REGISTRY_KEY,
    type: REGISTRY_TYPE,
    inputs,
    outputs: [{ name: "reference_registry", type: "H3_REFERENCE_REGISTRY" }],
  };
}

type ReferenceCounts = {
  readonly firstFrame: boolean;
  readonly lastFrame: boolean;
  readonly images: number;
  readonly videos: number;
  /**
   * How many reference videos submit their own soundtrack.
   *
   * M17-17: this is what makes the Reference Registry the owner of the pairing.
   * `paired_audios[i]` belongs to `videos[i]`, so the count is either every video
   * or none -- a partially paired set is sparse multi-video pairing, which this
   * item explicitly does not open.
   */
  readonly pairedAudios: number;
  readonly audios: number;
};

/**
 * The pipeline for a task mode.
 *
 * Reference modes carry one extra node: the Reference Registry owns canonical
 * reference identity, and a `ref2va` plan without it is not the same plan. The
 * registry is emitted before the Plan it feeds so the wiring pass can resolve it
 * by key.
 */
export function contextPipeline(
  taskMode: string,
  counts: ReferenceCounts = {
    firstFrame: false,
    lastFrame: false,
    images: 0,
    videos: 0,
    pairedAudios: 0,
    audios: 0,
  },
  canonicalLowering?: SpliceOptions["canonicalLowering"],
): readonly PipelineNode[] {
  const base = canonicalLowering
    ? [
        ...CONTEXT_PIPELINE.flatMap((spec) => {
          if (spec.key === "validator")
            return [
              {
                ...spec,
                inputs: spec.inputs.map((input) =>
                  input.name === "prompt_document"
                    ? {
                        ...input,
                        from: ["audit_override", 3] as readonly [
                          string,
                          number,
                        ],
                      }
                    : input,
                ),
              },
            ];
          if (spec.key !== "compiler") return [spec];
          return [
            spec,
            {
              key: "audit_override",
              type: "comfyui_h3_context.H3Context.AuditOverride",
              inputs: [
                {
                  name: "report",
                  type: "H3_CONTEXT_REPORT",
                  from: ["compiler", 1] as readonly [string, number],
                },
                {
                  name: "base_report_fingerprint",
                  type: "STRING",
                  widget: true,
                },
                { name: "revision", type: "INT", widget: true },
                { name: "reason", type: "STRING", widget: true },
                { name: "prompt_text", type: "STRING", widget: true },
              ],
              outputs: [
                { name: "edited_prompt", type: "H3_PROMPT_STRING" },
                { name: "updated_report", type: "H3_CONTEXT_REPORT" },
                { name: "override", type: "H3_AUDIT_OVERRIDE" },
                { name: "prompt_document", type: "H3_PROMPT_DOCUMENT" },
              ],
            },
          ];
        }),
      ]
    : CONTEXT_PIPELINE;
  if (taskMode !== "ref2va" && !counts.firstFrame && !counts.lastFrame)
    return base;
  return [
    referenceRegistrySpec(counts),
    ...base.map((spec) =>
      spec.key !== "plan"
        ? spec
        : {
            ...spec,
            inputs: spec.inputs.map((input) =>
              input.name === "reference_registry"
                ? {
                    ...input,
                    from: [REGISTRY_KEY, 0] as readonly [string, number],
                  }
                : input,
            ),
          },
    ),
  ];
}

function ensureInputs(node: Json): Json[] {
  const inputs = Array.isArray(node.inputs)
    ? (node.inputs.filter(isRecord) as Json[])
    : [];
  node.inputs = inputs;
  return inputs;
}

function ensureOutputs(node: Json): Json[] {
  const outputs = Array.isArray(node.outputs)
    ? (node.outputs.filter(isRecord) as Json[])
    : [];
  node.outputs = outputs;
  return outputs;
}

/** The anchor inputs this splice owns, in the order it will rebind them. */
function ownedMediaInputs(anchor: Json, family: ModeFamily): string[] {
  if (family === "image_to_video") return ["first_frame", "last_frame"];
  const groups = [
    "ref_images.",
    "ref_videos.",
    "ref_video_audios.",
    "ref_audios.",
  ];
  return ensureInputs(anchor)
    .map((input) => String(input.name ?? ""))
    .filter((name) => groups.some((group) => name.startsWith(group)));
}

/** The output sockets each supported loader class publishes. */
function loaderOutputs(
  type: string,
): readonly { readonly name: string; readonly type: string }[] {
  if (type === LOADER_TYPES.video) return [{ name: "VIDEO", type: "VIDEO" }];
  if (type === LOADER_TYPES.audio) return [{ name: "AUDIO", type: "AUDIO" }];
  return [
    { name: "IMAGE", type: "IMAGE" },
    { name: "MASK", type: "MASK" },
  ];
}

/**
 * Splice the context pipeline into a pinned template and return a new workflow.
 *
 * The authored duration is written into the template's own duration control
 * rather than into the anchor's `length`. That is deliberate and is what keeps
 * one alignment authority: the template drives `length` through a
 * `ComfyMathExpression` carrying the official expression, and M17-25 proved that
 * expression equals `core/length.py` over every integer millisecond in the
 * tested range. Writing an aligned frame count here would put the lattice rule
 * in a second place, which is the defect M17-25 removed.
 *
 * Media is bound the same way round. The caller supplies the loader class and
 * the widget values the user already chose on their own canvas; this rebuilds
 * the edges. Where the caller supplies nothing, the template's own published
 * default survives untouched, which is the D2 affordance: the user resolves it
 * on the visible canvas widget rather than through a second sidebar control.
 */
export function spliceContextPipeline(
  template: Json,
  options: SpliceOptions,
): SpliceResult {
  const connecting = options.connect === true;
  let admittedTemplate = template;
  if (connecting) {
    try {
      // CRITICAL: the pure Connect boundary must recheck the untrusted canvas
      // before clone/accessor execution even when UI admission already ran.
      admittedTemplate = readQueueMetadataCompatibleGraph(template).graph;
    } catch {
      throw new TemplateSpliceError(
        "malformed_template",
        "the canvas contains unsupported serialized queue metadata",
      );
    }
  }
  const family = TASK_MODE_FAMILY[options.taskMode];
  if (family === undefined)
    throw new TemplateSpliceError(
      "unsupported_task_mode",
      `${options.taskMode} is not a supported H3 task mode`,
    );
  if (!Number.isFinite(options.durationSeconds) || options.durationSeconds <= 0)
    throw new TemplateSpliceError(
      "invalid_duration",
      "the authored duration must be a positive number of seconds",
    );
  const media = options.media ?? {};
  const requested = {
    images: media.referenceImages?.length,
    videos: media.referenceVideos?.length,
    audios: media.referenceAudios?.length,
  };
  const soundtrack: ReferenceVideoSoundtrack =
    media.referenceVideoSoundtrack ?? "included";
  if (
    (requested.images ?? 0) > MAX_REFERENCE_IMAGES ||
    (requested.videos ?? 0) > MAX_REFERENCE_VIDEOS ||
    (requested.audios ?? 0) > MAX_REFERENCE_AUDIOS
  )
    throw new TemplateSpliceError(
      "too_many_references",
      "the selected reference set is larger than the registry admits",
    );

  // CRITICAL: Connect may add this repository's pipeline only once. Keep the
  // pure boundary independent of UI admission and refuse before cloning so no
  // caller can accidentally create partial or duplicate canvas ownership.
  if (connecting && hasContextPipelineOwnership(admittedTemplate))
    throw new TemplateSpliceError(
      "already_connected",
      "the canvas already contains an H3 Context pipeline",
    );
  const workflow = structuredClone(admittedTemplate) as Json;
  const { node: anchor, nested } = resolveAnchor(
    workflow,
    family,
    options.designatedAnchorId,
  );

  // Take the loaders the template already wired into the inputs this splice is
  // about to own, so a rebind reuses the node the user can see rather than
  // leaving a sample image behind and adding a second one next to it.
  const ownedInputs = connecting ? [] : ownedMediaInputs(anchor, family);
  const byIdBefore = new Map(
    nodeList(workflow).map((node) => [Number(node.id), node] as const),
  );
  const originOfInput = (
    name: string,
  ): Readonly<{ source: Json; sourceSlot: number }> | undefined => {
    const inputs = ensureInputs(anchor);
    const slot = inputs.findIndex((candidate) => candidate.name === name);
    const input = inputs[slot];
    if (typeof input?.link !== "number") return undefined;
    const link = linkList(workflow).find(
      (entry) => Array.isArray(entry) && entry[0] === input.link,
    );
    if (
      !Array.isArray(link) ||
      Number(link[3]) !== Number(anchor.id) ||
      Number(link[4]) !== slot
    )
      return undefined;
    const source = byIdBefore.get(Number(link[1]));
    const sourceSlot = Number(link[2]);
    const outputValue = Array.isArray(source?.outputs)
      ? source.outputs[sourceSlot]
      : undefined;
    const output = isRecord(outputValue) ? outputValue : undefined;
    if (
      source === undefined ||
      !Number.isSafeInteger(sourceSlot) ||
      sourceSlot < 0 ||
      !Array.isArray(output?.links) ||
      !output.links.includes(input.link)
    )
      return undefined;
    return { source, sourceSlot };
  };
  const connectedFrameOrigins = new Map<
    "first_frame" | "last_frame",
    Readonly<{ source: Json; sourceSlot: number }>
  >();
  if (connecting) {
    // CRITICAL: connect media authority comes only from the designated anchor's
    // exact existing edge. Never accept a sidebar/caller source id here: that
    // creates hidden ownership; omitting this Registry declaration makes Plan
    // fail later with missing_first_frame/missing_last_frame after queueing.
    for (const role of FRAME_BINDINGS[options.taskMode] ?? []) {
      const origin = originOfInput(role);
      if (origin === undefined)
        throw new TemplateSpliceError(
          role === "first_frame"
            ? "connect_missing_first_frame"
            : "connect_missing_last_frame",
          `the designated anchor has no consistent ${role} edge`,
        );
      connectedFrameOrigins.set(role, origin);
    }
    // D13 computes the task mode from the anchor and requires the sidebar to
    // agree. A mismatch is refused rather than reconciled: the anchor's linked
    // frame roles decide what the host actually receives, and overriding them
    // from the sidebar would queue a graph that does not do what was asked.
    const declared = anchorTaskMode(FAMILY_ANCHOR[family], anchor);
    if (declared !== options.taskMode)
      throw new TemplateSpliceError(
        "task_mode_mismatch",
        `the designated anchor is a ${declared} target, not ${options.taskMode}`,
      );
  }
  const reusable: Json[] = [];
  for (const name of ownedInputs) {
    const origin = originOfInput(name)?.source;
    if (
      origin !== undefined &&
      Object.values(LOADER_TYPES).includes(String(origin.type)) &&
      !reusable.includes(origin)
    )
      reusable.push(origin);
  }
  const templateDefaults = new Map<string, readonly unknown[]>();
  for (const node of reusable)
    if (!templateDefaults.has(String(node.type)))
      templateDefaults.set(
        String(node.type),
        Array.isArray(node.widgets_values) ? [...node.widgets_values] : [],
      );
  const templateImageCount = reusable.filter(
    (node) => node.type === LOADER_TYPES.image,
  ).length;

  // Detach every input this splice owns before the link list is captured. Doing
  // it afterwards silently loses the removal: `detachInput` replaces
  // `workflow.links`, so a list captured earlier and written back at the end
  // resurrects the link it just removed, leaving an edge to a node nothing
  // reaches. The test for a dangling link is what caught that ordering.
  detachInput(workflow, anchor, "prompt");
  for (const name of ownedInputs) detachInput(workflow, anchor, name);
  if (!connecting && nested) detachInput(workflow, anchor, "value_1");

  const nodes = nodeList(workflow);
  const links = linkList(workflow);
  let nextNodeId =
    highestId(
      nodes.map((node) => (typeof node.id === "number" ? node.id : 0)),
      workflow.last_node_id,
    ) + 1;
  let nextLinkId =
    highestId(
      links.map((link) =>
        Array.isArray(link) && typeof link[0] === "number" ? link[0] : 0,
      ),
      workflow.last_link_id,
    ) + 1;

  const newNode = (
    type: string,
    spec: {
      readonly widgetValues?: readonly unknown[];
      readonly inputs?: readonly {
        readonly name: string;
        readonly type: string;
        readonly widget?: boolean;
        readonly optional?: boolean;
      }[];
      readonly outputs?: readonly {
        readonly name: string;
        readonly type: string;
      }[];
      readonly pos?: readonly [number, number];
    },
  ): Json => {
    const node: Json = {
      id: nextNodeId++,
      type,
      pos: [...(spec.pos ?? [-2400, -1600])],
      size: [420, 120],
      flags: {},
      order: 0,
      mode: 0,
      inputs: (spec.inputs ?? []).map((input) => {
        const entry: Json = { name: input.name, type: input.type, link: null };
        if (input.optional === true) entry.shape = 7;
        if (input.widget === true) entry.widget = { name: input.name };
        return entry;
      }),
      outputs: (spec.outputs ?? []).map((output) => ({
        name: output.name,
        type: output.type,
        links: [] as number[],
      })),
      properties: {},
      widgets_values: [...(spec.widgetValues ?? [])],
    };
    nodes.push(node);
    return node;
  };

  const connect = (
    source: Json,
    sourceSlot: number,
    target: Json,
    inputName: string,
    type: string,
  ): void => {
    const inputs = ensureInputs(target);
    let slot = inputs.findIndex((input) => input.name === inputName);
    if (slot < 0) {
      // An autogrow group grows by declaring the next socket. `shape: 7` is the
      // optional-input shape the pinned templates use for those sockets.
      inputs.push({ name: inputName, type, shape: 7, link: null });
      slot = inputs.length - 1;
    }
    const linkId = nextLinkId++;
    inputs[slot]!.link = linkId;
    const outputs = ensureOutputs(source);
    const output = outputs[sourceSlot];
    if (output === undefined)
      throw new TemplateSpliceError(
        "malformed_pipeline",
        `slot ${sourceSlot} does not exist on ${String(source.type)}`,
      );
    const existing = Array.isArray(output.links) ? output.links : [];
    output.links = [...existing, linkId];
    links.push([
      linkId,
      Number(source.id),
      sourceSlot,
      Number(target.id),
      slot,
      type,
    ]);
  };

  const disconnect = (target: Json, inputName: string): void => {
    const input = ensureInputs(target).find(
      (candidate) => candidate.name === inputName,
    );
    const linkId = input?.link;
    if (typeof linkId !== "number") return;
    const index = links.findIndex(
      (entry) => Array.isArray(entry) && entry[0] === linkId,
    );
    const edge = index < 0 ? undefined : links[index];
    if (Array.isArray(edge)) {
      const source = nodes.find(
        (candidate) => Number(candidate.id) === Number(edge[1]),
      );
      const output = ensureOutputs(source ?? {})[Number(edge[2])];
      if (output !== undefined && Array.isArray(output.links))
        output.links = output.links.filter((value) => value !== linkId);
      links.splice(index, 1);
    }
    input!.link = null;
  };

  const inputOrigin = (
    target: Json,
    inputName: string,
  ): readonly [Json, number] | undefined => {
    const input = ensureInputs(target).find(
      (candidate) => candidate.name === inputName,
    );
    const edge = links.find(
      (entry) => Array.isArray(entry) && entry[0] === input?.link,
    );
    if (!Array.isArray(edge)) return undefined;
    const source = nodes.find(
      (candidate) => Number(candidate.id) === Number(edge[1]),
    );
    return source === undefined ? undefined : [source, Number(edge[2])];
  };

  const bindI2vaSourceGeometry = (source: Json): void => {
    const scales = nodes.filter((node) => node.type === I2VA_SCALE_NODE_TYPE);
    const sizes = nodes.filter((node) => node.type === I2VA_SIZE_NODE_TYPE);
    if (scales.length > 1 || sizes.length > 1)
      throw new TemplateSpliceError(
        "ambiguous_geometry_seam",
        "the generation template exposes ambiguous source-size nodes",
      );
    const scale =
      scales[0] ??
      newNode(I2VA_SCALE_NODE_TYPE, {
        inputs: [{ name: "image", type: "IMAGE" }],
        outputs: [{ name: "IMAGE", type: "IMAGE" }],
        widgetValues: I2VA_SCALE_WIDGET_VALUES,
        pos: [-2350, -900],
      });
    const size =
      sizes[0] ??
      newNode(I2VA_SIZE_NODE_TYPE, {
        inputs: [{ name: "image", type: "IMAGE" }],
        outputs: [
          { name: "width", type: "INT" },
          { name: "height", type: "INT" },
          { name: "batch_size", type: "INT" },
        ],
        pos: [-1950, -900],
      });

    // CRITICAL: new/replace owns the pinned scale settings and these four edges as one
    // authority. Retaining a template drift or an old edge makes the visible branch and
    // queued graph disagree.
    scale.widgets_values = [...I2VA_SCALE_WIDGET_VALUES];
    disconnect(scale, "image");
    connect(source, 0, scale, "image", "IMAGE");
    const sizeOrigin = inputOrigin(size, "image");
    if (sizeOrigin?.[0] !== scale || sizeOrigin[1] !== 0) {
      disconnect(size, "image");
      connect(scale, 0, size, "image", "IMAGE");
    }
    disconnect(anchor, "width");
    disconnect(anchor, "height");
    connect(size, 0, anchor, "width", "INT");
    connect(size, 1, anchor, "height", "INT");
  };

  const videos = requested.videos ?? 0;
  const counts: ReferenceCounts = {
    // Connect adds a repository-owned declaration consumer from the same output
    // the native anchor already uses. It does not rewrite the anchor or claim
    // the source branch, but Plan still needs the typed role to normalize it.
    firstFrame: (FRAME_BINDINGS[options.taskMode] ?? []).includes(
      "first_frame",
    ),
    lastFrame: (FRAME_BINDINGS[options.taskMode] ?? []).includes("last_frame"),
    images:
      requested.images ??
      (family === "reference_to_video" ? templateImageCount : 0),
    videos,
    pairedAudios: soundtrack === "included" ? videos : 0,
    audios: requested.audios ?? 0,
  };
  const pipeline = contextPipeline(
    options.taskMode,
    counts,
    options.canonicalLowering,
  );

  const byKey = new Map<string, Json>();
  pipeline.forEach((spec, index) => {
    const node = newNode(spec.type, {
      inputs: spec.inputs,
      outputs: spec.outputs,
      widgetValues:
        spec.key === "request"
          ? [options.taskMode, options.userIntent, options.durationSeconds]
          : spec.key === "audit_override" &&
              options.canonicalLowering !== undefined
            ? [
                options.canonicalLowering.baseReportFingerprint,
                options.canonicalLowering.overrideRevision,
                options.canonicalLowering.reason,
                options.userIntent,
              ]
            : [],
      pos: [-2400, -1200 + index * 180],
    });
    byKey.set(spec.key, node);
  });
  for (const spec of pipeline) {
    for (const input of spec.inputs) {
      if (input.from === undefined) continue;
      const [sourceKey, sourceSlot] = input.from;
      const source = byKey.get(sourceKey);
      const target = byKey.get(spec.key);
      if (source === undefined || target === undefined)
        throw new TemplateSpliceError(
          "malformed_pipeline",
          `pipeline node ${sourceKey} is not declared before it is used`,
        );
      connect(source, sourceSlot, target, input.name, input.type);
    }
  }

  let durationApplied = false;
  let durationSourceNodeId: number | undefined;
  if (!connecting) {
    const application = applyDuration(
      workflow,
      anchor,
      nested,
      options.durationSeconds,
    );
    if (!application.applied)
      throw new TemplateSpliceError(
        "missing_duration_seam",
        "the generation template no longer exposes its native duration source",
      );
    const durationSource =
      application.source ??
      newNode("PrimitiveFloat", {
        outputs: [{ name: "FLOAT", type: "FLOAT" }],
        widgetValues: [options.durationSeconds],
        pos: [-2900, -1650],
      });
    durationSourceNodeId = Number(durationSource.id);
    if (nested) {
      durationSource.title = "H3 App Mode duration (Sidebar)";
      connect(durationSource, 0, anchor, "value_1", "FLOAT");
      const promoted = ensureInputs(anchor).find(
        (input) => input.name === "value_1",
      );
      if (promoted !== undefined && promoted.widget === undefined)
        promoted.widget = { name: "value_1" };
    }
    const request = byKey.get("request");
    if (request === undefined)
      throw new TemplateSpliceError(
        "malformed_pipeline",
        "the Context Request node is unavailable",
      );
    connect(durationSource, 0, request, "duration_seconds", "FLOAT");
    durationApplied = true;
  }

  // The prompt edge: the template's conditioning node stops reading its widget
  // and reads the shell instead.
  const shell = byKey.get(PROMPT_SOURCE_KEY)!;
  connect(shell, PROMPT_SOURCE_SLOT, anchor, "prompt", "STRING");
  const promptInput = ensureInputs(anchor).find(
    (input) => input.name === "prompt",
  )!;
  const promptLinkId = Number(promptInput.link);
  if (promptInput.widget === undefined) promptInput.widget = { name: "prompt" };
  if (!nested) clearPromptWidgetValue(anchor);

  // Media. A reused loader keeps its identity and only its widget values move;
  // a loader the template did not ship is created next to the pipeline.
  const spent = new Set<Json>();
  const loader = (
    kind: keyof typeof LOADER_TYPES,
    source?: LoaderSource,
  ): Json => {
    const type = source?.type ?? LOADER_TYPES[kind];
    const reused = reusable.find(
      (node) => !spent.has(node) && String(node.type) === type,
    );
    if (reused !== undefined) {
      spent.add(reused);
      if (source !== undefined)
        reused.widgets_values = [...source.widgetValues];
      return reused;
    }
    return newNode(type, {
      widgetValues: source?.widgetValues ?? templateDefaults.get(type) ?? [],
      outputs: loaderOutputs(type),
      pos: [-2900, -1200 + spent.size * 160],
    });
  };

  const mediaNodeIds: {
    firstFrame?: number;
    lastFrame?: number;
    referenceImages: number[];
    referenceVideos: number[];
    referenceAudios: number[];
  } = { referenceImages: [], referenceVideos: [], referenceAudios: [] };
  let i2vaSourceNode: Json | undefined;

  if (connecting && family === "image_to_video") {
    const registry = byKey.get(REGISTRY_KEY);
    if (
      (FRAME_BINDINGS[options.taskMode] ?? []).length > 0 &&
      registry === undefined
    )
      throw new TemplateSpliceError(
        "malformed_pipeline",
        "the connected frame Registry is unavailable",
      );
    for (const role of FRAME_BINDINGS[options.taskMode] ?? []) {
      const origin = connectedFrameOrigins.get(role);
      if (origin === undefined || registry === undefined)
        throw new TemplateSpliceError(
          "malformed_pipeline",
          `the connected ${role} declaration is unavailable`,
        );
      connect(origin.source, origin.sourceSlot, registry, role, "IMAGE");
    }
    // Existing anchor links, user widgets, artifact sink and every unrelated
    // edge remain exactly as assembled; only this owned declaration consumer is
    // added to the already-selected frame output.
  } else if (connecting) {
    // Ref2VA Connect remains outside M23-38. Nothing else is touched here.
  } else if (family === "image_to_video") {
    const registry = byKey.get(REGISTRY_KEY);
    for (const role of FRAME_BINDINGS[options.taskMode] ?? []) {
      const source =
        role === "first_frame" ? media.firstFrame : media.lastFrame;
      const node = loader("image", source);
      connect(node, 0, anchor, role, "IMAGE");
      if (registry === undefined)
        throw new TemplateSpliceError(
          "malformed_pipeline",
          "the typed frame registry is unavailable",
        );
      // CRITICAL: typed ownership and native execution must dereference the
      // same loader output. Copying a widget value would recreate the host
      // failure while leaving the canvas superficially plausible.
      connect(node, 0, registry, role, "IMAGE");
      if (role === "first_frame") {
        mediaNodeIds.firstFrame = Number(node.id);
        if (options.taskMode === "i2va") i2vaSourceNode = node;
      } else mediaNodeIds.lastFrame = Number(node.id);
    }
  } else {
    const registry = byKey.get(REGISTRY_KEY)!;
    for (let index = 0; index < counts.images; index += 1) {
      const node = loader("image", media.referenceImages?.[index]);
      connect(node, 0, anchor, `ref_images.ref_image_${index}`, "IMAGE");
      connect(node, 0, registry, `images.image${index}`, "IMAGE");
      mediaNodeIds.referenceImages.push(Number(node.id));
    }
    for (let index = 0; index < counts.videos; index += 1) {
      const node = loader("video", media.referenceVideos?.[index]);
      connect(node, 0, registry, `videos.video${index}`, "VIDEO");
      const components = newNode(VIDEO_COMPONENTS_TYPE, {
        inputs: [{ name: "video", type: "VIDEO" }],
        outputs: [
          { name: "images", type: "IMAGE" },
          { name: "audio", type: "AUDIO" },
        ],
        pos: [-2700, -600 + index * 160],
      });
      connect(node, 0, components, "video", "VIDEO");
      connect(components, 0, anchor, `ref_videos.ref_video_${index}`, "IMAGE");
      // M17-17: the soundtrack reaches the anchor only by way of the registry
      // that owns it. Binding the anchor without binding the registry is what
      // made the typed report describe a different request from the queued one.
      if (soundtrack === "included") {
        connect(
          components,
          1,
          registry,
          `paired_audios.paired_audio${index}`,
          "AUDIO",
        );
        connect(
          components,
          1,
          anchor,
          `ref_video_audios.ref_video_audio_${index}`,
          "AUDIO",
        );
      }
      mediaNodeIds.referenceVideos.push(Number(node.id));
    }
    for (let index = 0; index < counts.audios; index += 1) {
      const node = loader("audio", media.referenceAudios?.[index]);
      connect(node, 0, anchor, `ref_audios.ref_audio_${index}`, "AUDIO");
      connect(node, 0, registry, `audios.audio${index}`, "AUDIO");
      mediaNodeIds.referenceAudios.push(Number(node.id));
    }
  }

  if (!connecting && options.taskMode === "i2va") {
    if (i2vaSourceNode === undefined)
      throw new TemplateSpliceError(
        "missing_geometry_source",
        "the i2va source image is unavailable",
      );
    bindI2vaSourceGeometry(i2vaSourceNode);
  }

  // A template loader this mode does not need is removed rather than left on the
  // canvas: it is sample content whose only purpose was the edge just detached,
  // and an orphan `LoadImage` holding a photograph the user never chose reads as
  // part of their request.
  const discarded = new Set(
    reusable.filter((node) => !spent.has(node)).map((node) => Number(node.id)),
  );
  workflow.nodes = nodes.filter((node) => !discarded.has(Number(node.id)));
  workflow.links = links.filter(
    (link) =>
      !Array.isArray(link) ||
      (!discarded.has(Number(link[1])) && !discarded.has(Number(link[3]))),
  );
  workflow.last_node_id = nextNodeId - 1;
  workflow.last_link_id = nextLinkId - 1;

  // D11/D13: on the connect route the length control belongs to the graph the
  // user assembled. Writing the authored duration into it would mutate a foreign
  // node the consent copy promises to leave alone; managed admission therefore
  // leaves that branch to ComfyUI and verifies only the authored frame count on
  // the observed artifact.
  const artifactPrefixApplied =
    options.artifactPrefix === undefined || connecting
      ? false
      : applyArtifactPrefix(workflow, options.artifactPrefix);
  const ownedNodeIds = new Set(
    [...byKey.values()].map((node) => Number(node.id)),
  );
  if (durationSourceNodeId !== undefined)
    ownedNodeIds.add(durationSourceNodeId);
  const ownedLinkIds = (workflow.links as unknown[])
    .filter(Array.isArray)
    .filter(
      (link) =>
        Number(link[0]) === promptLinkId ||
        // A foreign frame output feeding our Registry is also an owned link:
        // this repository created that consumer edge and must detect if the host
        // drops or rewrites it, without hashing any foreign node content.
        ownedNodeIds.has(Number(link[3])),
    )
    .map((link) => Number(link[0]));

  return {
    workflow,
    anchorNodeId: Number(anchor.id),
    promptNodeId: Number(shell.id),
    ownedNodeIds: Object.freeze(
      [...ownedNodeIds].sort((left, right) => left - right),
    ),
    ownedLinkIds: Object.freeze(
      [...new Set(ownedLinkIds)].sort((left, right) => left - right),
    ),
    authoredWidgetNodeIds: Object.freeze(
      [
        durationSourceNodeId,
        options.canonicalLowering === undefined
          ? undefined
          : Number(byKey.get("request")?.id),
        options.canonicalLowering === undefined
          ? undefined
          : Number(byKey.get("audit_override")?.id),
      ].filter((value): value is number => value !== undefined),
    ),
    nestedAnchor: nested,
    durationApplied,
    artifactPrefixApplied,
    mediaNodeIds,
  };
}

/**
 * Point the template's artifact sink at this run's own output location.
 *
 * The prefix is the sink's first widget on every pinned template
 * (`video/MiniMax_H3` by default), and there is exactly one sink -- which App
 * Mode independently requires of the compiled prompt. Writing it by widget
 * position rather than by name is unavoidable here: a serialized litegraph node
 * carries widget *values* without their names, and the name only exists in the
 * host's node definition. The single-sink requirement is what keeps that
 * positional write from landing on some other node's first widget.
 */
/**
 * Blank the prompt widget the link has just replaced.
 *
 * Once `prompt` arrives as a link the stored widget value drives nothing, and
 * the host's own conversion blanks it. Leaving it behind has two costs: the
 * serialized graph keeps text that no longer describes what will be rendered,
 * and on the connect route that text is the user's previous prompt, sitting in a
 * graph they are told now carries the Context's.
 *
 * The prompt is the leading widget on both native anchors, and every other
 * widget they declare is numeric, so a leading string is the prompt slot and a
 * leading number means there is no prompt slot to clear. Nested anchors are left
 * alone: the widgets on a subgraph instance are its promoted ones, and the
 * leading value there belongs to whatever the definition promoted first.
 */
function clearPromptWidgetValue(anchor: Json): void {
  const widgets = anchor.widgets_values;
  if (!Array.isArray(widgets) || typeof widgets[0] !== "string") return;
  anchor.widgets_values = ["", ...widgets.slice(1)];
}

function applyArtifactPrefix(workflow: Json, prefix: string): boolean {
  const sinks = nodeList(workflow).filter((node) => node.type === "SaveVideo");
  if (sinks.length !== 1) return false;
  const sink = sinks[0]!;
  const widgets = Array.isArray(sink.widgets_values)
    ? [...sink.widgets_values]
    : [];
  if (widgets.length === 0 || typeof widgets[0] !== "string") return false;
  widgets[0] = prefix;
  sink.widgets_values = widgets;
  return true;
}

/**
 * Write the authored duration into the control the template already drives
 * `length` from.
 *
 * For a subgraph anchor that control is a promoted widget on the instance. For a
 * flat graph it is found by walking the edge the template actually built --
 * `length` back to the math node, and its first operand back to the primitive --
 * rather than by trusting a node id, because a node id is a property of one file
 * and the edge is a property of the design.
 */
function applyDuration(
  workflow: Json,
  anchor: Json,
  nested: boolean,
  durationSeconds: number,
): Readonly<{ applied: boolean; source?: Json }> {
  if (nested) {
    const definition = subgraphDefinitions(workflow).find(
      (candidate) => String(candidate.id ?? "") === String(anchor.type ?? ""),
    );
    if (definition === undefined) return { applied: false };
    const order = widgetOrder(definition);
    const index = order.indexOf("value_1");
    const widgets = Array.isArray(anchor.widgets_values)
      ? [...anchor.widgets_values]
      : [];
    if (index < 0 || index >= widgets.length) return { applied: false };
    widgets[index] = durationSeconds;
    anchor.widgets_values = widgets;
    return { applied: true };
  }

  const links = linkList(workflow).filter(Array.isArray) as unknown[][];
  const byId = new Map(
    nodeList(workflow).map((node) => [Number(node.id), node] as const),
  );
  const originOf = (linkId: unknown): Json | undefined => {
    const link = links.find((entry) => entry[0] === linkId);
    return link === undefined ? undefined : byId.get(Number(link[1]));
  };
  const inputsOf = (node: Json): Json[] =>
    Array.isArray(node.inputs) ? (node.inputs.filter(isRecord) as Json[]) : [];

  const lengthInput = inputsOf(anchor).find((input) => input.name === "length");
  const math = originOf(lengthInput?.link);
  if (math === undefined || math.type !== "ComfyMathExpression")
    return { applied: false };
  const operand = inputsOf(math).find((input) =>
    String(input.name ?? "").endsWith("a"),
  );
  const primitive = originOf(operand?.link);
  if (primitive === undefined || primitive.type !== "PrimitiveFloat")
    return { applied: false };
  primitive.widgets_values = [durationSeconds];
  return { applied: true, source: primitive };
}
