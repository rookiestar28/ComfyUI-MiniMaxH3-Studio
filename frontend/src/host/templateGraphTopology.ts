// Pure graph-topology, anchor-resolution and splice-contract support for template materialization.
// Kept separate from the splice implementation so both cohesive modules remain under the M23-28
// 1,200-line ceiling. This module never reads or writes a live ComfyUI host.

import {
  H3_NODE_TYPES,
  definitionEdges,
  nodeIdentifier,
  readQueueMetadataCompatibleGraph,
  tupleEdges,
  validateSerializedLinks,
} from "./graphSerialization";
import { recognizeH3InferenceCanvas } from "./graphRecognition";

export type Json = Record<string, unknown>;

export type ModeFamily = "image_to_video" | "reference_to_video";

export const TASK_MODE_FAMILY: Readonly<Record<string, ModeFamily>> =
  Object.freeze({
    t2va: "image_to_video",
    i2va: "image_to_video",
    fl2va: "image_to_video",
    l2va: "image_to_video",
    ref2va: "reference_to_video",
  });

export const FAMILY_ANCHOR: Readonly<Record<ModeFamily, string>> =
  Object.freeze({
    image_to_video: "MiniMaxH3ImageToVideo",
    reference_to_video: "MiniMaxH3ReferenceToVideo",
  });

/**
 * The template each task mode materializes from (plan section 8.3).
 *
 * This is keyed by task mode, not by family, because `t2va` and `i2va` share an
 * anchor family and a subgraph definition but not a basis: `video_minimax_h3_t2v`
 * is the same subgraph with no `LoadImage` chain reaching the promoted
 * `first_frame`. Materializing `t2va` from the `i2v` bytes would ship a
 * text-to-video request with a sample photograph wired into it.
 */
export const MODE_TEMPLATE: Readonly<Record<string, string>> = Object.freeze({
  t2va: "video_minimax_h3_t2v",
  i2va: "video_minimax_h3_i2v",
  fl2va: "video_minimax_h3_i2v",
  l2va: "video_minimax_h3_i2v",
  ref2va: "video_minimax_h3_r2v",
});

/** The task modes that bind a frame image, and which promoted input each uses. */
export const FRAME_BINDINGS: Readonly<
  Record<string, readonly ("first_frame" | "last_frame")[]>
> = Object.freeze({
  t2va: [],
  i2va: ["first_frame"],
  fl2va: ["first_frame", "last_frame"],
  l2va: ["last_frame"],
  ref2va: [],
});

/**
 * The reference cardinality the pure core admits, restated here only as an outer
 * bound on how many sockets the splice will build. The registry remains the
 * authority on what an accepted reference set is; this stops a selection the
 * backend is going to reject from silently growing the anchor first.
 */
export const MAX_REFERENCE_IMAGES = 9;
export const MAX_REFERENCE_VIDEOS = 3;
export const MAX_REFERENCE_AUDIOS = 3;

export const OFFICIAL_LENGTH_EXPRESSION =
  "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17";

export class TemplateSpliceError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
    this.name = "TemplateSpliceError";
  }
}

/**
 * A media loader the caller wants the materialized graph to read from.
 *
 * Only the node class and its serialized widget values cross this boundary. The
 * widget value is a host-owned filename the user already chose on their own
 * canvas; it is copied into the new graph and never read, resolved or reported.
 */
export type LoaderSource = {
  readonly type: string;
  readonly widgetValues: readonly unknown[];
};

/**
 * Whether a reference video's own soundtrack is part of the request.
 *
 * M17-17. `excluded` and `unavailable` produce the same graph on purpose and are
 * kept apart everywhere else: "the user did not want it" and "the host cannot
 * supply it" are different facts, and collapsing them would let an unavailable
 * soundtrack be reported as a declined one.
 */
export type ReferenceVideoSoundtrack = "included" | "excluded" | "unavailable";

export type SpliceMedia = {
  readonly firstFrame?: LoaderSource;
  readonly lastFrame?: LoaderSource;
  readonly referenceImages?: readonly LoaderSource[];
  readonly referenceVideos?: readonly LoaderSource[];
  readonly referenceAudios?: readonly LoaderSource[];
  /** Defaults to `included`, which is the graph every accepted route produced. */
  readonly referenceVideoSoundtrack?: ReferenceVideoSoundtrack;
};

export const I2VA_SCALE_NODE_TYPE = "ImageScaleToTotalPixels";
export const I2VA_SIZE_NODE_TYPE = "GetImageSize";
export const I2VA_SCALE_WIDGET_VALUES = Object.freeze([
  "nearest-exact",
  0.8,
  32,
] as const);

/** The pipeline node whose STRING output becomes the anchor's prompt. */
export const PROMPT_SOURCE_KEY = "shell";
export const PROMPT_SOURCE_SLOT = 0;

export function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function nodeList(workflow: Json): Json[] {
  const nodes = workflow.nodes;
  if (!Array.isArray(nodes))
    throw new TemplateSpliceError(
      "malformed_template",
      "the template has no node list",
    );
  return nodes.filter(isRecord);
}

export function linkList(workflow: Json): unknown[] {
  const links = workflow.links;
  if (!Array.isArray(links))
    throw new TemplateSpliceError(
      "malformed_template",
      "the template has no link list",
    );
  return links;
}

export function subgraphDefinitions(workflow: Json): Json[] {
  const definitions = workflow.definitions;
  if (!isRecord(definitions)) return [];
  const subgraphs = definitions.subgraphs;
  return Array.isArray(subgraphs) ? subgraphs.filter(isRecord) : [];
}

/**
 * Resolve the one node whose `prompt` the splice may rewire.
 *
 * The anchor may be a top-level native node, or it may be a subgraph instance
 * whose definition contains one. Only the second case is what the pinned
 * frame-driven templates ship, and a resolver that walked only top-level nodes
 * would report "no anchor" on the very template this item materializes -- which
 * is why the subgraph case is handled first-class rather than as a fallback.
 */
/**
 * One native H3 conditioning node a prompt could be spliced into.
 *
 * M17-20 D13 tier B. The census is anchored on the official node classes rather
 * than on anything this repository owns, because in an arbitrary community graph
 * that is the only thing whose meaning is fixed: every prompt-consuming native
 * H3 node has exactly one `prompt` String input, and nothing else about the
 * surrounding graph is knowable.
 */
export type NativeAnchor = {
  readonly nodeId: number;
  readonly anchorType: string;
  readonly nested: boolean;
  readonly taskMode: string;
};

const NATIVE_ANCHOR_TYPES: readonly string[] = Object.values(FAMILY_ANCHOR);
const CONTEXT_NODE_PREFIX = "comfyui_h3_context.H3Context.";

/** Whether this repository already owns a context pipeline anywhere serialized. */
export function hasContextPipelineOwnership(workflow: Json): boolean {
  const ownsNode = (node: Json): boolean =>
    typeof node.type === "string" && node.type.startsWith(CONTEXT_NODE_PREFIX);
  if (nodeList(workflow).some(ownsNode)) return true;
  return subgraphDefinitions(workflow).some((definition) =>
    (Array.isArray(definition.nodes) ? definition.nodes : [])
      .filter(isRecord)
      .some(ownsNode),
  );
}

function linkedInputNames(node: Json): Set<string> {
  const inputs = Array.isArray(node.inputs) ? node.inputs.filter(isRecord) : [];
  return new Set(
    inputs
      .filter((input) => typeof input.link === "number")
      .map((input) => String(input.name ?? "")),
  );
}

/**
 * Which task mode a designated anchor is asking for.
 *
 * D13 derives this from the anchor rather than trusting the sidebar, and the
 * sidebar's mode must then match. A reference anchor is `ref2va` outright; an
 * image anchor is read from which frame roles are actually connected, because
 * that is what decides whether the host receives a first frame, a last frame,
 * both, or neither.
 */
export function anchorTaskMode(anchorType: string, node: Json): string {
  if (anchorType === FAMILY_ANCHOR.reference_to_video) return "ref2va";
  const linked = linkedInputNames(node);
  const first = linked.has("first_frame");
  const last = linked.has("last_frame");
  if (first && last) return "fl2va";
  if (first) return "i2va";
  if (last) return "l2va";
  return "t2va";
}

/**
 * Every native H3 anchor on a graph, top level or one subgraph deep.
 *
 * A nested anchor is reported by its *instance* id, because that is the node the
 * user can see and designate, and because the promoted `prompt` input the splice
 * rewires lives on the instance.
 */
export function listNativeAnchors(workflow: Json): NativeAnchor[] {
  const definitionAnchor = new Map<string, string>();
  for (const definition of subgraphDefinitions(workflow)) {
    const inner = (Array.isArray(definition.nodes) ? definition.nodes : [])
      .filter(isRecord)
      .find((node) => NATIVE_ANCHOR_TYPES.includes(String(node.type)));
    if (inner !== undefined)
      definitionAnchor.set(String(definition.id ?? ""), String(inner.type));
  }
  const anchors: NativeAnchor[] = [];
  for (const node of nodeList(workflow)) {
    const type = String(node.type ?? "");
    const nestedType = definitionAnchor.get(type);
    const anchorType = NATIVE_ANCHOR_TYPES.includes(type) ? type : nestedType;
    if (anchorType === undefined) continue;
    anchors.push({
      nodeId: Number(node.id),
      anchorType,
      nested: nestedType !== undefined,
      taskMode: anchorTaskMode(anchorType, node),
    });
  }
  return anchors;
}

/**
 * The admission tier a canvas falls into.
 *
 * `connect` means at least one native anchor is present and the connect route is
 * offered; `designate` means more than one is, so D13 requires the user to say
 * which; `unavailable` is tier C, where the only choices remain replace or keep.
 */
export type CanvasAdmission =
  | Readonly<{ tier: "connect"; anchors: readonly NativeAnchor[] }>
  | Readonly<{ tier: "designate"; anchors: readonly NativeAnchor[] }>
  | Readonly<{ tier: "unavailable"; anchors: readonly NativeAnchor[] }>;

export function admitCanvasForConnect(workflow: Json): CanvasAdmission {
  let admitted: Json;
  try {
    admitted = readQueueMetadataCompatibleGraph(workflow).graph;
  } catch {
    return Object.freeze({ tier: "unavailable" as const, anchors: [] });
  }
  if (hasContextPipelineOwnership(admitted))
    return Object.freeze({ tier: "unavailable" as const, anchors: [] });
  const anchors = listNativeAnchors(admitted);
  if (anchors.length === 0)
    return Object.freeze({ tier: "unavailable" as const, anchors: [] });
  return Object.freeze({
    tier: anchors.length === 1 ? ("connect" as const) : ("designate" as const),
    anchors: Object.freeze(anchors),
  });
}

export function resolveAnchor(
  workflow: Json,
  family: ModeFamily,
  designatedNodeId?: number,
): { node: Json; nested: boolean } {
  const anchorType = FAMILY_ANCHOR[family];
  const byId = new Map(
    nodeList(workflow).map((node) => [Number(node.id), node] as const),
  );
  const candidates = listNativeAnchors(workflow).filter(
    (candidate) => candidate.anchorType === anchorType,
  );
  if (designatedNodeId !== undefined) {
    // D13: a designation names one node, and it is either a candidate of the
    // requested family or it is refused. Falling back to "the only other anchor"
    // would splice into a node the user did not choose.
    const chosen = candidates.find(
      (candidate) => candidate.nodeId === designatedNodeId,
    );
    if (chosen === undefined)
      throw new TemplateSpliceError(
        "unknown_anchor",
        `node ${designatedNodeId} is not a ${anchorType} on this graph`,
      );
    return { node: byId.get(chosen.nodeId)!, nested: chosen.nested };
  }
  if (candidates.length === 0)
    throw new TemplateSpliceError(
      "no_anchor",
      `the graph carries no ${anchorType} the prompt could be spliced into`,
    );
  if (candidates.length > 1)
    throw new TemplateSpliceError(
      "ambiguous_anchor",
      `the graph carries ${candidates.length} candidate anchors and none was designated`,
    );
  const chosen = candidates[0]!;
  return { node: byId.get(chosen.nodeId)!, nested: chosen.nested };
}

export function highestId(
  values: readonly number[],
  declared: unknown,
): number {
  const start =
    typeof declared === "number" && Number.isFinite(declared) ? declared : 0;
  return values.reduce((carry, value) => Math.max(carry, value), start);
}

/** Widget-backed promoted inputs, in the order `widgets_values` uses. */
export function widgetOrder(definition: Json): string[] {
  const inputs = Array.isArray(definition.inputs) ? definition.inputs : [];
  return inputs
    .filter(isRecord)
    .filter((input) => input.type !== "IMAGE")
    .map((input) => String(input.name ?? ""));
}

export type ExistingContextAuthoringOptions = Readonly<{
  taskMode: string;
  userIntent: string;
  durationSeconds: number;
}>;

export type ExistingContextAuthoringResult = Readonly<{
  workflow: Json;
  requestNodeId: string;
  durationSourceNodeId: string;
  anchorNodeId: string;
  compiledAnchorNodeId: string;
  anchorNodeType: string;
  productShellNodeId: string;
  nestedAnchor: boolean;
}>;

/**
 * Apply Sidebar-owned authoring to a graph the user explicitly chose to queue.
 *
 * Recognition deliberately stays permissive. This stricter boundary runs only
 * after submit and proves the two fields this repository may write: one Context
 * Request Intent widget and one PrimitiveFloat already shared by Request and
 * the native anchor's official length derivation.
 */
export function rebindExistingContextAuthoring(
  value: Json,
  options: ExistingContextAuthoringOptions,
): ExistingContextAuthoringResult {
  if (
    TASK_MODE_FAMILY[options.taskMode] === undefined ||
    typeof options.userIntent !== "string" ||
    options.userIntent.trim().length === 0 ||
    options.userIntent.length > 4096 ||
    !Number.isFinite(options.durationSeconds) ||
    options.durationSeconds <= 0
  )
    throw new TemplateSpliceError(
      "invalid_authoring_request",
      "the Sidebar authoring request is outside its bounded contract",
    );

  let admitted: Json;
  try {
    // CRITICAL: inspect accessors before structuredClone or any array method.
    // Existing-canvas submit is an explicit mutation boundary, not permission
    // to execute data supplied by another extension.
    admitted = readQueueMetadataCompatibleGraph(value).graph;
  } catch {
    throw new TemplateSpliceError(
      "unsafe_authoring_seam",
      "the current graph is not safe serialized workflow data",
    );
  }
  const workflow = structuredClone(admitted) as Json;
  const roots = nodeList(workflow);
  if (validateSerializedLinks(workflow, roots) !== undefined)
    throw new TemplateSpliceError(
      "unsafe_authoring_seam",
      "the current graph link metadata is inconsistent",
    );
  const tupleLinks = tupleEdges(workflow.links);
  if (tupleLinks === undefined)
    throw new TemplateSpliceError(
      "missing_authoring_seam",
      "the current graph has no serialized authoring links",
    );
  const linksById = new Map<number, (typeof tupleLinks)[number]>();
  for (const edge of tupleLinks) {
    if (edge.id === undefined || linksById.has(edge.id))
      throw new TemplateSpliceError(
        "unsafe_authoring_seam",
        "the current graph has ambiguous link identities",
      );
    linksById.set(edge.id, edge);
  }

  const rootsById = new Map<string, Json>();
  for (const node of roots) {
    let id: string;
    try {
      id = nodeIdentifier(node.id, "existing graph node");
    } catch {
      throw new TemplateSpliceError(
        "unsafe_authoring_seam",
        "the current graph has an invalid node identity",
      );
    }
    if (rootsById.has(id))
      throw new TemplateSpliceError(
        "ambiguous_authoring_seam",
        "the current graph has duplicate node identities",
      );
    rootsById.set(id, node);
  }

  const linkedInput = (
    node: Json,
    acceptedNames: readonly string[],
    missingCode: string,
  ): Readonly<{
    edge: (typeof tupleLinks)[number];
    input: Json;
    slot: number;
  }> => {
    const inputs = Array.isArray(node.inputs)
      ? (node.inputs.filter(isRecord) as Json[])
      : [];
    const matches = inputs
      .map((input, slot) => ({ input, slot }))
      .filter(({ input }) => acceptedNames.includes(String(input.name ?? "")));
    if (matches.length === 0)
      throw new TemplateSpliceError(
        missingCode,
        "the current graph is missing a required authoring input",
      );
    if (matches.length > 1)
      throw new TemplateSpliceError(
        "ambiguous_authoring_seam",
        "the current graph has duplicate authoring inputs",
      );
    const match = matches[0]!;
    if (!Number.isSafeInteger(match.input.link))
      throw new TemplateSpliceError(
        missingCode,
        "the current graph authoring input is not linked",
      );
    const edge = linksById.get(match.input.link as number);
    let targetId: string;
    try {
      targetId = nodeIdentifier(node.id, "authoring target");
    } catch {
      throw new TemplateSpliceError(
        "unsafe_authoring_seam",
        "the current graph has an invalid authoring target",
      );
    }
    if (
      edge === undefined ||
      edge.targetId !== targetId ||
      edge.targetSlot !== match.slot
    )
      throw new TemplateSpliceError(
        "unsafe_authoring_seam",
        "the current graph authoring edge does not match its socket",
      );
    return { edge, input: match.input, slot: match.slot };
  };

  const requests = roots.filter((node) => node.type === H3_NODE_TYPES.request);
  if (requests.length === 0)
    throw new TemplateSpliceError(
      "missing_authoring_seam",
      "the current graph has no Context Request authoring seam",
    );
  if (requests.length > 1)
    throw new TemplateSpliceError(
      "ambiguous_authoring_seam",
      "the current graph has more than one Context Request authoring seam",
    );
  const request = requests[0]!;
  const requestWidgets = Array.isArray(request.widgets_values)
    ? [...request.widgets_values]
    : [];
  if (requestWidgets.length !== 3 || typeof requestWidgets[1] !== "string")
    throw new TemplateSpliceError(
      "unsafe_authoring_seam",
      "the Context Request widget layout is not the owned layout",
    );
  const requestNamedWidgets = request.widgets_values_named;
  if (
    Object.hasOwn(request, "widgets_values_named") &&
    (!isRecord(requestNamedWidgets) ||
      requestNamedWidgets.user_intent !== requestWidgets[1])
  )
    throw new TemplateSpliceError(
      "unsafe_authoring_seam",
      "the Context Request named widget state disagrees with its owned layout",
    );
  const requestDuration = linkedInput(
    request,
    ["duration_seconds"],
    "non_shared_duration_seam",
  );
  const sharedSource = rootsById.get(requestDuration.edge.originId);
  const sharedWidgets = Array.isArray(sharedSource?.widgets_values)
    ? [...sharedSource.widgets_values]
    : [];
  const sharedInputs = Array.isArray(sharedSource?.inputs)
    ? sharedSource.inputs.filter(isRecord)
    : [];
  const sharedNamedWidgets = sharedSource?.widgets_values_named;
  if (
    sharedSource?.type !== "PrimitiveFloat" ||
    requestDuration.edge.originSlot !== 0 ||
    sharedWidgets.length !== 1 ||
    typeof sharedWidgets[0] !== "number" ||
    !Number.isFinite(sharedWidgets[0]) ||
    sharedInputs.some(
      (input) => input.link !== null && input.link !== undefined,
    )
  )
    throw new TemplateSpliceError(
      "non_shared_duration_seam",
      "the Context Request duration has no bounded shared source",
    );
  if (
    Object.hasOwn(sharedSource, "widgets_values_named") &&
    (!isRecord(sharedNamedWidgets) ||
      sharedNamedWidgets.value !== sharedWidgets[0])
  )
    throw new TemplateSpliceError(
      "unsafe_authoring_seam",
      "the shared duration named widget state disagrees with its owned layout",
    );

  const definitions = new Map<string, Json>();
  for (const definition of subgraphDefinitions(workflow)) {
    if (typeof definition.id !== "string" || definitions.has(definition.id))
      throw new TemplateSpliceError(
        "ambiguous_authoring_seam",
        "the current graph has ambiguous subgraph definitions",
      );
    definitions.set(definition.id, definition);
  }
  type ExistingAnchor = Readonly<{
    node: Json;
    nested: boolean;
    definition?: Json;
    inner?: Json;
  }>;
  const anchors: ExistingAnchor[] = [];
  for (const node of roots) {
    if (NATIVE_ANCHOR_TYPES.includes(String(node.type ?? "")))
      anchors.push({ node, nested: false });
    const definitionId =
      typeof node.subgraph_id === "string"
        ? node.subgraph_id
        : typeof node.type === "string" && definitions.has(node.type)
          ? node.type
          : undefined;
    if (definitionId === undefined) continue;
    const definition = definitions.get(definitionId);
    if (definition === undefined) continue;
    const innerNodes = Array.isArray(definition.nodes)
      ? (definition.nodes.filter(isRecord) as Json[])
      : [];
    for (const inner of innerNodes)
      if (NATIVE_ANCHOR_TYPES.includes(String(inner.type ?? "")))
        anchors.push({ node, nested: true, definition, inner });
  }
  if (anchors.length === 0)
    throw new TemplateSpliceError(
      "missing_authoring_seam",
      "the current graph has no native H3 authoring target",
    );

  const incomingRoot = new Map<string, (typeof tupleLinks)[number][]>();
  for (const edge of tupleLinks) {
    const rows = incomingRoot.get(edge.targetId) ?? [];
    rows.push(edge);
    incomingRoot.set(edge.targetId, rows);
  }
  const sharedSourceId = requestDuration.edge.originId;
  const sharedSourceSlot = requestDuration.edge.originSlot;
  const rootUpstreamContainsSharedSource = (
    start: (typeof tupleLinks)[number],
  ): boolean => {
    const pending = [start];
    const visited = new Set<string>();
    while (pending.length > 0) {
      const edge = pending.pop()!;
      if (
        edge.originId === sharedSourceId &&
        edge.originSlot === sharedSourceSlot
      )
        return true;
      if (visited.has(edge.originId)) continue;
      visited.add(edge.originId);
      if (visited.size > rootsById.size) return false;
      for (const upstream of incomingRoot.get(edge.originId) ?? [])
        pending.push(upstream);
    }
    return false;
  };
  const rootInputEdge = (
    node: Json,
    name: string,
  ): (typeof tupleLinks)[number] | undefined => {
    const inputs = Array.isArray(node.inputs)
      ? (node.inputs.filter(isRecord) as Json[])
      : [];
    const matches = inputs
      .map((input, slot) => ({ input, slot }))
      .filter(({ input }) => input.name === name);
    if (matches.length !== 1 || !Number.isSafeInteger(matches[0]!.input.link))
      return undefined;
    const match = matches[0]!;
    const edge = linksById.get(match.input.link as number);
    return edge !== undefined &&
      edge.targetId === String(node.id) &&
      edge.targetSlot === match.slot
      ? edge
      : undefined;
  };
  const nestedBoundarySlots = (candidate: ExistingAnchor): Set<number> => {
    const definition = candidate.definition;
    const inner = candidate.inner;
    const edges = definitionEdges(definition?.links);
    if (definition === undefined || inner === undefined || edges === undefined)
      return new Set();
    const innerByLink = new Map<number, (typeof edges)[number]>();
    for (const edge of edges) {
      if (edge.id === undefined || innerByLink.has(edge.id)) return new Set();
      innerByLink.set(edge.id, edge);
    }
    const inputs = Array.isArray(inner.inputs)
      ? (inner.inputs.filter(isRecord) as Json[])
      : [];
    const matches = inputs
      .map((input, slot) => ({ input, slot }))
      .filter(({ input }) => input.name === "length");
    if (matches.length !== 1 || !Number.isSafeInteger(matches[0]!.input.link))
      return new Set();
    const start = innerByLink.get(matches[0]!.input.link as number);
    if (
      start === undefined ||
      start.targetId !== String(inner.id) ||
      start.targetSlot !== matches[0]!.slot
    )
      return new Set();
    const incoming = new Map<string, (typeof edges)[number][]>();
    for (const edge of edges) {
      const rows = incoming.get(edge.targetId) ?? [];
      rows.push(edge);
      incoming.set(edge.targetId, rows);
    }
    const boundarySlots = new Set<number>();
    const pending = [start];
    const visited = new Set<string>();
    while (pending.length > 0) {
      const edge = pending.pop()!;
      if (edge.originId === "-10") {
        boundarySlots.add(edge.originSlot);
        continue;
      }
      if (visited.has(edge.originId)) continue;
      visited.add(edge.originId);
      if (visited.size > edges.length + 1) return new Set();
      for (const upstream of incoming.get(edge.originId) ?? [])
        pending.push(upstream);
    }
    return boundarySlots;
  };
  const usesSharedDuration = (candidate: ExistingAnchor): boolean => {
    if (!candidate.nested) {
      const length = rootInputEdge(candidate.node, "length");
      return length !== undefined && rootUpstreamContainsSharedSource(length);
    }
    const definitionInputs = Array.isArray(candidate.definition?.inputs)
      ? candidate.definition.inputs
      : [];
    const wrapperInputs = Array.isArray(candidate.node.inputs)
      ? candidate.node.inputs
      : [];
    for (const boundarySlot of nestedBoundarySlots(candidate)) {
      const boundaryInput = definitionInputs[boundarySlot];
      if (!isRecord(boundaryInput) || typeof boundaryInput.name !== "string")
        continue;
      const matches = wrapperInputs
        .map((input, slot) => ({ input, slot }))
        .filter(
          (entry): entry is { input: Json; slot: number } =>
            isRecord(entry.input) && entry.input.name === boundaryInput.name,
        );
      if (matches.length !== 1 || !Number.isSafeInteger(matches[0]!.input.link))
        continue;
      const { input, slot } = matches[0]!;
      const edge = linksById.get(input.link as number);
      if (
        edge !== undefined &&
        edge.targetId === String(candidate.node.id) &&
        edge.targetSlot === slot &&
        rootUpstreamContainsSharedSource(edge)
      )
        return true;
    }
    return false;
  };

  // CRITICAL: duration reachability is the sole existing-canvas inference
  // invariant. Do not replace this with task-mode, prompt, expression, model or
  // fixed-hop checks; host subgraph expansion inserts PrimitiveFloat bridges and
  // those checks reject user-owned pipelines that the shared source still drives.
  const anchor = anchors.find(usesSharedDuration);
  if (anchor === undefined)
    throw new TemplateSpliceError(
      "non_shared_duration_seam",
      "Context Request and native length do not share one duration source",
    );
  const actualAnchorType = String(
    (anchor.nested ? anchor.inner?.type : anchor.node.type) ?? "",
  );
  const compiledAnchorNodeId = anchor.nested
    ? `${String(anchor.node.id)}:${nodeIdentifier(
        anchor.inner!.id,
        "nested native H3 target",
      )}`
    : String(anchor.node.id);
  let recognition;
  try {
    recognition = recognizeH3InferenceCanvas(workflow);
  } catch {
    throw new TemplateSpliceError(
      "unsafe_authoring_seam",
      "the current graph has no bounded H3 identity",
    );
  }
  if (recognition.productShellAnchors.length !== 1)
    throw new TemplateSpliceError(
      "ambiguous_authoring_seam",
      "the current graph has no single Product Shell correlation seam",
    );

  // CRITICAL: inference controls belong to the visible canvas. Duration is the
  // sole generation scalar the Sidebar may replace; user intent is context
  // authoring, not inference tuning. Adding model, sampler, scheduler,
  // step/Turbo, LoRA, guidance, denoise, seed or geometry defaults here silently
  // changes the user's inference cost and output. Every other node, link and
  // media field survives from the serialized graph byte-for-byte.
  request.widgets_values = [
    requestWidgets[0],
    options.userIntent,
    requestWidgets[2],
  ];
  sharedSource.widgets_values = [options.durationSeconds];
  if (isRecord(requestNamedWidgets))
    request.widgets_values_named = {
      ...requestNamedWidgets,
      user_intent: options.userIntent,
    };
  if (isRecord(sharedNamedWidgets))
    sharedSource.widgets_values_named = {
      ...sharedNamedWidgets,
      value: options.durationSeconds,
    };
  return {
    workflow,
    requestNodeId: String(request.id),
    durationSourceNodeId: requestDuration.edge.originId,
    anchorNodeId: String(anchor.node.id),
    compiledAnchorNodeId,
    anchorNodeType: actualAnchorType,
    productShellNodeId: recognition.productShellAnchors[0]!.executionId,
    nestedAnchor: anchor.nested,
  };
}

export function detachInput(
  workflow: Json,
  node: Json,
  inputName: string,
): void {
  const inputs = Array.isArray(node.inputs) ? node.inputs.filter(isRecord) : [];
  const target = inputs.find((input) => input.name === inputName);
  const existing = target?.link;
  if (typeof existing !== "number") return;
  // Clear the socket as well as the edge. Leaving the id behind on an input the
  // splice does not rebind -- `first_frame` on an l2va graph, say -- points a
  // live socket at a link that no longer exists, which the host reads as a bound
  // input and then cannot resolve.
  target!.link = null;
  workflow.links = linkList(workflow).filter(
    (link) => !(Array.isArray(link) && link[0] === existing),
  );
  for (const other of nodeList(workflow)) {
    const outputs = Array.isArray(other.outputs)
      ? other.outputs.filter(isRecord)
      : [];
    for (const output of outputs) {
      if (Array.isArray(output.links))
        output.links = output.links.filter((value) => value !== existing);
    }
  }
}

export type SpliceOptions = {
  readonly taskMode: string;
  readonly userIntent: string;
  readonly durationSeconds: number;
  readonly media?: SpliceMedia;
  /**
   * M17-20 D5: where this run's artifact is written, as a deterministic
   * content-free path prefix. The template ships `video/MiniMax_H3` for every
   * workflow, so two revisions of the same segment would write into the same
   * place and the second would be indistinguishable from the first. The caller
   * computes the value; this module only writes it.
   */
  readonly artifactPrefix?: string;
  /**
   * M17-20 D13: splice into a graph the user assembled rather than into a
   * template this repository materializes.
   *
   * In connect mode the repository owns exactly the splice seam — inserting the
   * context pipeline and rewiring the designated anchor's `prompt` widget to a
   * link. It touches no media input, discards no node and rewrites no artifact
   * location, because every one of those belongs to a graph it does not own and
   * cannot warrant.
   */
  readonly connect?: boolean;
  /** The anchor the user designated; mandatory when the graph has more than one. */
  readonly designatedAnchorId?: number;
  readonly canonicalLowering?: Readonly<{
    readonly baseReportFingerprint: string;
    readonly baseReportRevision: number;
    readonly overrideRevision: number;
    readonly reason: "Materialize approved Production segment prompt";
  }>;
};

export type SpliceMediaIds = {
  readonly firstFrame?: number;
  readonly lastFrame?: number;
  readonly referenceImages: readonly number[];
  readonly referenceVideos: readonly number[];
  readonly referenceAudios: readonly number[];
};

export type SpliceResult = {
  readonly workflow: Json;
  readonly anchorNodeId: number;
  readonly promptNodeId: number;
  readonly ownedNodeIds: readonly number[];
  readonly ownedLinkIds: readonly number[];
  readonly authoredWidgetNodeIds: readonly number[];
  readonly nestedAnchor: boolean;
  readonly durationApplied: boolean;
  readonly artifactPrefixApplied: boolean;
  readonly mediaNodeIds: SpliceMediaIds;
};
