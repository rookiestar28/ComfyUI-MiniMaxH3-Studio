/**
 * Serialized-graph primitives: what the canvas types are called, and how to read a serialized
 * graph safely enough to say anything about it.
 *
 * Everything here is imported by the layers above and imports none of them back. Four of these --
 * `record`, `serializedNodes`, `nodeIdentifier` and the port-type reader -- used to be declared at
 * the very bottom of the single 3382-line module while being used near the top, which is the one
 * thing that would have made a split circular. They live at the base now.
 */

export const H3_NODE_TYPES = {
  request: "comfyui_h3_context.H3Context.Request",
  plan: "comfyui_h3_context.H3Context.Plan",
  compiler: "comfyui_h3_context.H3Context.Compiler",
  auditOverride: "comfyui_h3_context.H3Context.AuditOverride",
  validator: "comfyui_h3_context.H3Context.Validator",
  nativeAdapter: "comfyui_h3_context.H3Context.NativeH3Adapter",
  productShell: "comfyui_h3_context.H3Context.ProductShell",
  preview: "comfyui_h3_context.H3Context.Preview",
  referenceRegistry: "comfyui_h3_context.H3Context.ReferenceRegistry",
  imageGeneration: "MiniMaxH3ImageToVideo",
  referenceGeneration: "MiniMaxH3ReferenceToVideo",
  getVideoComponents: "GetVideoComponents",
  loadImage: "LoadImage",
  loadVideo: "LoadVideo",
  loadAudio: "LoadAudio",
} as const;

export const shellNodeId = H3_NODE_TYPES.productShell;

export const shellFieldId =
  "h3.comfyui_h3_context_h3context_productshell.output.product_shell";

export const maxNodes = 4096;

export const maxDepth = 16;

export const maxIdentifierLength = 192;

const maxSerializedObjects = maxNodes * 64;

export const minFrameCount = 5;

export const maxFrameCount = 3600;
// M17-25: the Request node authors a duration in seconds, so the serialized
// widget in slot 2 is a float, not an integer frame count. Zero remains the
// host loader sentinel for an unset optional widget. These are outer bounds on a
// serialized value; the producible set is decided by the backend alignment
// authority and is never restated here. It stays at the host's 3600-frame
// ceiling rather than at the tighter authoring bound in `appMode.ts`, on
// purpose: this value parses a graph somebody else wrote. Rejecting 149.7..150 s
// here would make the whole splice unavailable, while admitting it lets the
// backend answer with the precise reason the length cannot be produced.

const maxDurationSeconds = 150;

export const isSerializedDurationWidget = (value: unknown): boolean =>
  typeof value === "number" &&
  Number.isFinite(value) &&
  value >= 0 &&
  value <= maxDurationSeconds;

export const minGenerationDimension = 32;

export const maxGenerationDimension = 16384;

export const serializedIdentifier = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$/;

const forbiddenSerializedKeys = new Set([
  "__proto__",
  "prototype",
  "constructor",
]);

export const canonicalTypes = new Set<string>([
  H3_NODE_TYPES.request,
  H3_NODE_TYPES.plan,
  H3_NODE_TYPES.compiler,
  H3_NODE_TYPES.validator,
  H3_NODE_TYPES.nativeAdapter,
  H3_NODE_TYPES.productShell,
  H3_NODE_TYPES.preview,
  H3_NODE_TYPES.referenceRegistry,
  H3_NODE_TYPES.imageGeneration,
  H3_NODE_TYPES.referenceGeneration,
  H3_NODE_TYPES.getVideoComponents,
  H3_NODE_TYPES.loadImage,
  H3_NODE_TYPES.loadVideo,
  H3_NODE_TYPES.loadAudio,
]);
/**
 * The task modes each native anchor family serves.
 *
 * The anchor decides the family, not the exact mode: every frame-driven mode
 * uses the same image anchor and differs only in which promoted frame input is
 * bound. Reading `t2va` as the only image-anchor mode made an `i2va` canvas
 * unbindable for a reason that was never about the canvas.
 */

export const imageAnchorTaskModes = ["t2va", "i2va", "fl2va", "l2va"];

export const referenceAnchorTaskModes = ["ref2va"];

export const anchorTaskModes = [
  ...imageAnchorTaskModes,
  ...referenceAnchorTaskModes,
];

export const requiredTypes = [
  H3_NODE_TYPES.request,
  H3_NODE_TYPES.plan,
  H3_NODE_TYPES.compiler,
  H3_NODE_TYPES.validator,
  H3_NODE_TYPES.nativeAdapter,
  H3_NODE_TYPES.productShell,
  H3_NODE_TYPES.preview,
];

/**
 * The nodes whose inputs this repository is answerable for.
 *
 * M17-20 D11/D13: a canvas may now be an official generation template, or a
 * graph the user built themselves, with the H3 context pipeline spliced into it.
 * In that world the surrounding graph is not this repository's to validate, but
 * what feeds the context chain still is -- and so is the one edge that carries
 * the compiled prompt into the native anchor. Everything on this list keeps its
 * exact edge contract in an open-world graph; nothing else is enumerated.
 */

export const contextChainTypes = new Set<string>([
  ...requiredTypes,
  H3_NODE_TYPES.referenceRegistry,
]);

export type GraphAnchor = { executionId: string; nodeId: typeof shellNodeId };

export type GraphInspection = {
  status: "ready" | "missing" | "ambiguous" | "incompatible";
  anchors: GraphAnchor[];
  /** Top-level serialized node count, used to keep App Mode off dirty canvases. */
  nodeCount?: number;
  /** True only after serialized link/input ownership data was present and valid. */
  existingGraphCompatible?: boolean;
  reason?: string;
};

export const H3_SHELL_MANIFEST = {
  fields: [
    {
      field_id:
        "h3.comfyui_h3_context_h3context_productshell.output.product_shell",
      node_id: shellNodeId,
      port_name: "product_shell",
    },
  ],
} as const;

export function assertSafeSerializedKeys(
  value: unknown,
  seen = new Set<object>(),
  depth = 0,
): void {
  if (value === null || typeof value !== "object") return;
  if (depth > maxDepth * 4)
    throw new Error("serialized graph exceeds recursion bound");
  if (seen.has(value)) return;
  seen.add(value);
  if (seen.size > maxSerializedObjects)
    throw new Error("serialized graph exceeds object bound");
  for (const key of Object.keys(value)) {
    if (forbiddenSerializedKeys.has(key))
      throw new Error(`unsafe serialized key: ${key}`);
    // CRITICAL: never traverse host workflow accessors; reading value[key]
    // would execute untrusted extension code before the shape can be refused.
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor === undefined || !("value" in descriptor))
      throw new Error("serialized graph contains an accessor member");
    assertSafeSerializedKeys(descriptor.value, seen, depth + 1);
  }
}

export function validateSerializedLinks(
  graph: Record<string, unknown>,
  nodes: Record<string, unknown>[],
): string | undefined {
  const linksValue = graph.links;
  const hasLinkMetadata =
    linksValue !== undefined ||
    nodes.some(
      (node) => node.inputs !== undefined || node.outputs !== undefined,
    );
  if (!hasLinkMetadata) return undefined;
  const nodeIds = new Set(nodes.map((node) => String(node.id)));
  const linkRecords = new Map<
    number,
    {
      originId: string;
      originSlot: number;
      targetId: string;
      targetSlot: number;
    }
  >();
  const inputRefs = new Map<number, string>();
  const outputRefs = new Map<number, string>();
  if (linksValue !== undefined) {
    if (!Array.isArray(linksValue)) return "malformed_graph_links";
    for (const [index, value] of linksValue.entries()) {
      if (!Array.isArray(value) || value.length !== 6)
        return "malformed_graph_links";
      const linkId = value[0];
      const originId = value[1];
      const originSlot = value[2];
      const targetId = value[3];
      const targetSlot = value[4];
      if (
        !Number.isSafeInteger(linkId) ||
        linkRecords.has(linkId as number) ||
        !Number.isSafeInteger(originSlot) ||
        (originSlot as number) < 0 ||
        !Number.isSafeInteger(targetSlot) ||
        (targetSlot as number) < 0 ||
        !nodeIds.has(String(originId)) ||
        !nodeIds.has(String(targetId)) ||
        typeof value[5] !== "string"
      )
        return `malformed_graph_link_${index}`;
      linkRecords.set(linkId as number, {
        originId: String(originId),
        originSlot: originSlot as number,
        targetId: String(targetId),
        targetSlot: targetSlot as number,
      });
    }
  }
  for (const [index, node] of nodes.entries()) {
    if (node.inputs !== undefined) {
      if (!Array.isArray(node.inputs)) return `malformed_graph_inputs_${index}`;
      for (const [slot, input] of node.inputs.entries()) {
        const inputRecord =
          input !== null && typeof input === "object" && !Array.isArray(input)
            ? (input as Record<string, unknown>)
            : undefined;
        if (inputRecord === undefined) return `malformed_graph_inputs_${index}`;
        const link = inputRecord.link;
        if (link === null || link === undefined) continue;
        if (
          !Number.isSafeInteger(link) ||
          linksValue === undefined ||
          !linkRecords.has(link as number)
        )
          return `malformed_graph_input_link_${index}`;
        const key = `${String(node.id)}:${slot}`;
        if (inputRefs.has(link as number))
          return `duplicate_graph_input_link_${index}`;
        inputRefs.set(link as number, key);
      }
    }
    if (node.outputs !== undefined) {
      if (!Array.isArray(node.outputs))
        return `malformed_graph_outputs_${index}`;
      for (const [slot, output] of node.outputs.entries()) {
        const outputRecord =
          output !== null &&
          typeof output === "object" &&
          !Array.isArray(output)
            ? (output as Record<string, unknown>)
            : undefined;
        if (outputRecord === undefined)
          return `malformed_graph_outputs_${index}`;
        const links = outputRecord.links;
        if (links === null || links === undefined) continue;
        if (!Array.isArray(links))
          return `malformed_graph_output_links_${index}`;
        for (const link of links) {
          if (
            !Number.isSafeInteger(link) ||
            linksValue === undefined ||
            !linkRecords.has(link as number)
          )
            return `malformed_graph_output_link_${index}`;
          const key = `${String(node.id)}:${slot}`;
          if (outputRefs.has(link as number))
            return `duplicate_graph_output_link_${index}`;
          outputRefs.set(link as number, key);
        }
      }
    }
  }
  if (linkRecords.size > 0) {
    for (const [linkId, link] of linkRecords) {
      if (
        inputRefs.get(linkId) !== `${link.targetId}:${link.targetSlot}` ||
        outputRefs.get(linkId) !== `${link.originId}:${link.originSlot}`
      )
        return `unbound_graph_link_${linkId}`;
    }
  }
  return undefined;
}

export type SerializedEdge = {
  id?: number;
  originId: string;
  originSlot: number;
  targetId: string;
  targetSlot: number;
  type: string;
};

export function tupleEdges(value: unknown): SerializedEdge[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const edges: SerializedEdge[] = [];
  for (const item of value) {
    if (
      !Array.isArray(item) ||
      item.length !== 6 ||
      !Number.isSafeInteger(item[0]) ||
      !Number.isSafeInteger(item[2]) ||
      (item[2] as number) < 0 ||
      !Number.isSafeInteger(item[4]) ||
      (item[4] as number) < 0 ||
      typeof item[5] !== "string"
    )
      return undefined;
    edges.push({
      id: item[0] as number,
      originId: String(item[1]),
      originSlot: item[2] as number,
      targetId: String(item[3]),
      targetSlot: item[4] as number,
      type: item[5],
    });
  }
  return edges;
}

export function definitionEdges(value: unknown): SerializedEdge[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const edges: SerializedEdge[] = [];
  for (const item of value) {
    const link = record(item, "subgraph link");
    if (
      Object.keys(link).some(
        (key) =>
          ![
            "id",
            "origin_id",
            "origin_slot",
            "target_id",
            "target_slot",
            "type",
          ].includes(key),
      )
    )
      return undefined;
    if (
      !Number.isSafeInteger(link.id) ||
      !Number.isSafeInteger(link.origin_slot) ||
      (link.origin_slot as number) < 0 ||
      !Number.isSafeInteger(link.target_slot) ||
      (link.target_slot as number) < 0 ||
      typeof link.type !== "string"
    )
      return undefined;
    edges.push({
      id: link.id as number,
      originId: String(link.origin_id),
      originSlot: link.origin_slot as number,
      targetId: String(link.target_id),
      targetSlot: link.target_slot as number,
      type: link.type,
    });
  }
  return edges;
}

export function isBoundaryPortNode(
  value: unknown,
  expectedId: number,
): boolean {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    return false;
  const node = value as Record<string, unknown>;
  const bounding = node.bounding;
  return (
    node.id === expectedId &&
    Array.isArray(bounding) &&
    bounding.length === 4 &&
    bounding.every(
      (value) => typeof value === "number" && Number.isFinite(value),
    )
  );
}

export function isLinkId(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

export function record(value: unknown, field: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${field} must be serialized object data`);
  return value as Record<string, unknown>;
}

export function serializedNodes(
  value: unknown,
  field: string,
): Record<string, unknown>[] {
  if (!Array.isArray(value))
    throw new Error(`${field} must be a serialized node array`);
  return value.map((node, index) => record(node, `${field}[${index}]`));
}

export function nodeIdentifier(value: unknown, field: string): string {
  if (typeof value === "number" && (!Number.isSafeInteger(value) || value < 0))
    throw new Error(`${field} has an invalid serialized ID`);
  if (!(typeof value === "string" || typeof value === "number"))
    throw new Error(`${field} has an invalid serialized ID`);
  const result = String(value);
  if (
    result.length === 0 ||
    result.length > maxIdentifierLength ||
    !serializedIdentifier.test(result)
  )
    throw new Error(`${field} has an invalid or unbounded serialized ID`);
  return result;
}

const queueWidgetNames = new Set([
  "seed",
  "noise_seed",
  "sampler_name",
  "scheduler",
]);

const serializedWorkflowRootKeys = new Set([
  "revision",
  "last_node_id",
  "last_link_id",
  "nodes",
  "links",
  "groups",
  "config",
  "extra",
  "version",
  "definitions",
  "h3_context_fixture",
  "id",
  "floatingLinks",
  "widget_idx_map",
  "seed_widgets",
]);

function plainMetadataEntries(
  value: unknown,
  label: string,
): Array<readonly [string, unknown]> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${label} metadata must be a plain object`);
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null)
    throw new Error(`${label} metadata must be a plain object`);
  const entries: Array<readonly [string, unknown]> = [];
  const keys = Reflect.ownKeys(value);
  if (keys.length > maxNodes)
    throw new Error(`${label} metadata exceeds its entry bound`);
  for (const key of keys) {
    if (typeof key !== "string")
      throw new Error(`${label} metadata contains a non-string member`);
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (
      descriptor === undefined ||
      descriptor.enumerable !== true ||
      !("value" in descriptor)
    )
      throw new Error(`${label} metadata contains a hidden or accessor member`);
    entries.push([key, descriptor.value]);
  }
  return entries;
}

function enumerableDataValue(
  owner: Record<string, unknown>,
  key: string,
  label: string,
): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(owner, key);
  if (
    descriptor === undefined ||
    descriptor.enumerable !== true ||
    !("value" in descriptor)
  )
    throw new Error(`${label} must be an enumerable data member`);
  return descriptor.value;
}

function validateQueueMetadata(
  graph: Record<string, unknown>,
  rootNodes: Record<string, unknown>[],
): void {
  const metadataValue = (
    key: "widget_idx_map" | "seed_widgets",
  ):
    | Readonly<{ present: false }>
    | Readonly<{ present: true; value: unknown }> => {
    const descriptor = Object.getOwnPropertyDescriptor(graph, key);
    if (descriptor === undefined) return { present: false };
    if (descriptor.enumerable !== true || !("value" in descriptor))
      throw new Error(`${key} metadata must be an enumerable data member`);
    return { present: true, value: descriptor.value };
  };
  const widgetIndexMap = metadataValue("widget_idx_map");
  const seedWidgets = metadataValue("seed_widgets");
  if (!widgetIndexMap.present && !seedWidgets.present) return;
  const rootById = new Map(
    rootNodes.map((node) => [
      nodeIdentifier(
        enumerableDataValue(node, "id", "root node id"),
        "root node",
      ),
      node,
    ]),
  );
  let entryCount = 0;
  const countEntry = (): void => {
    entryCount += 1;
    if (entryCount > maxNodes)
      throw new Error("queue metadata exceeds its aggregate entry bound");
  };
  const validateIndex = (
    nodeId: string,
    value: unknown,
    label: string,
  ): void => {
    const node = rootById.get(nodeId);
    if (node === undefined)
      throw new Error(`${label} metadata names no serialized root node`);
    const widgets = enumerableDataValue(
      node,
      "widgets_values",
      `${label} node widgets_values`,
    );
    if (
      !Number.isInteger(value) ||
      (value as number) < 0 ||
      !Array.isArray(widgets) ||
      (value as number) >= widgets.length
    )
      throw new Error(`${label} metadata contains an invalid widget index`);
  };

  if (widgetIndexMap.present)
    for (const [nodeId, mapping] of plainMetadataEntries(
      widgetIndexMap.value,
      "widget_idx_map",
    )) {
      countEntry();
      for (const [name, index] of plainMetadataEntries(
        mapping,
        "widget_idx_map entry",
      )) {
        countEntry();
        if (!queueWidgetNames.has(name))
          throw new Error("widget_idx_map metadata contains an unknown widget");
        validateIndex(nodeId, index, "widget_idx_map");
      }
    }

  if (seedWidgets.present)
    for (const [nodeId, index] of plainMetadataEntries(
      seedWidgets.value,
      "seed_widgets",
    )) {
      countEntry();
      validateIndex(nodeId, index, "seed_widgets");
    }
}

export type QueueMetadataCompatibleGraph = Readonly<{
  graph: Record<string, unknown>;
  rootNodes: Record<string, unknown>[];
}>;

/**
 * Admit the serialized workflow envelope shared by graph inspection and Connect.
 *
 * The two optional queue-wrapper members are allowed only after exact bounded
 * validation. Callers receive the admitted workflow and root nodes; the exact
 * metadata stays opaque and must not become H3 topology, identity, or mutation
 * authority.
 */
export function readQueueMetadataCompatibleGraph(
  graphValue: unknown,
): QueueMetadataCompatibleGraph {
  // CRITICAL: Connect must execute this before fingerprinting or cloning; both
  // operations may otherwise invoke accessors embedded by an untrusted extension.
  assertSafeSerializedKeys(graphValue);
  const graph = record(graphValue, "graph");
  for (const key of Reflect.ownKeys(graph)) {
    if (typeof key !== "string")
      throw new Error("graph contains a non-string serialized member");
    const descriptor = Object.getOwnPropertyDescriptor(graph, key);
    if (
      descriptor === undefined ||
      descriptor.enumerable !== true ||
      !("value" in descriptor)
    )
      throw new Error("graph contains a hidden or accessor serialized member");
    if (!serializedWorkflowRootKeys.has(key))
      throw new Error("graph contains an unknown serialized member");
  }
  if (
    graph.id !== undefined &&
    (typeof graph.id !== "string" || !serializedIdentifier.test(graph.id))
  )
    throw new Error("graph contains an invalid public graph ID");
  if (
    graph.floatingLinks !== undefined &&
    (!Array.isArray(graph.floatingLinks) || graph.floatingLinks.length !== 0)
  )
    throw new Error("graph contains unsupported floating links");
  if (Object.hasOwn(graph, "_nodes"))
    throw new Error("graph must use serialized public data");
  const rootNodes = serializedNodes(graph.nodes, "graph.nodes");
  validateQueueMetadata(graph, rootNodes);
  if (rootNodes.length > maxNodes)
    throw new Error("serialized graph exceeds its root node bound");
  return Object.freeze({ graph, rootNodes });
}
