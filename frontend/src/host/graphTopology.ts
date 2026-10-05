/**
 * Whether a whole graph is canonical: the prompt path, the reference-media topology and the shell
 * boundary.
 *
 * The owner of "is this graph one we can act on". Nothing above this layer is allowed to reach a
 * different answer.
 */

import {
  H3_NODE_TYPES,
  contextChainTypes,
  definitionEdges,
  requiredTypes,
  serializedNodes,
  shellNodeId,
} from "./graphSerialization";
import type { SerializedEdge } from "./graphSerialization";
import { serializedPorts } from "./graphPortShapes";
import { validateSubgraphDefinitionLinks } from "./graphSubgraphLinks";

type TopologyOptions = {
  /** The canvas carries nodes this repository does not own. */
  readonly openWorld?: boolean;
  /** The native anchor lives in this container. Default true. */
  readonly anchorLocal?: boolean;
  /** Used when the anchor is elsewhere and cannot be read from this container. */
  readonly isReference?: boolean;
  /** The prompt edge is validated by socket name elsewhere. */
  readonly skipPromptEdge?: boolean;
};

export function validateCanonicalTopology(
  nodes: Record<string, unknown>[],
  edges: SerializedEdge[] | undefined,
  typeOverrides: ReadonlyMap<string, string> = new Map(),
  options: TopologyOptions = {},
): boolean {
  const openWorld = options.openWorld === true;
  if (edges === undefined) return false;
  const byType = new Map<string, string[]>();
  for (const node of nodes) {
    const id = String(node.id);
    const type = typeOverrides.get(id) ?? node.type;
    if (typeof type !== "string") continue;
    const values = byType.get(type) ?? [];
    values.push(id);
    byType.set(type, values);
  }
  const typeById = new Map<string, string>();
  for (const node of nodes) {
    const id = String(node.id);
    const type = typeOverrides.get(id) ?? node.type;
    if (typeof type === "string") typeById.set(id, type);
  }
  const referenceRegistry = H3_NODE_TYPES.referenceRegistry;
  const edgeType = (id: string): string =>
    id === "-10"
      ? "__H3_EXTERNAL_INPUT__"
      : id === "-20"
        ? "__H3_EXTERNAL_OUTPUT__"
        : (typeById.get(id) ?? "__H3_UNKNOWN__");
  const edgeKey = (edge: SerializedEdge): string =>
    `${edge.originId}:${edge.originSlot}:${edge.targetId}:${edge.targetSlot}:${edge.type}`;
  const allowedEdge = (edge: SerializedEdge): boolean => {
    const originType = edgeType(edge.originId);
    const targetType = edgeType(edge.targetId);
    if (originType === "__H3_EXTERNAL_INPUT__") {
      if (targetType === "comfyui_h3_context.H3Context.Request")
        return (
          edge.originSlot === edge.targetSlot &&
          edge.targetSlot >= 0 &&
          edge.targetSlot <= 2 &&
          ["COMBO", "STRING", "FLOAT"][edge.targetSlot] === edge.type
        );
      if (targetType === referenceRegistry)
        return (
          edge.originSlot === edge.targetSlot + 3 &&
          edge.targetSlot >= 0 &&
          edge.targetSlot <= 3 &&
          ["IMAGE", "IMAGE", "VIDEO", "AUDIO"][edge.targetSlot] === edge.type
        );
      if (targetType === "GetVideoComponents")
        return (
          edge.originSlot === 5 &&
          edge.targetSlot === 0 &&
          edge.type === "VIDEO"
        );
      if (targetType === "MiniMaxH3ReferenceToVideo") {
        const expectedReferenceInput = new Map([
          [8, { originSlot: 3, type: "IMAGE" }],
          [9, { originSlot: 4, type: "IMAGE" }],
          [12, { originSlot: 6, type: "AUDIO" }],
        ]).get(edge.targetSlot);
        return (
          expectedReferenceInput !== undefined &&
          edge.originSlot === expectedReferenceInput.originSlot &&
          edge.type === expectedReferenceInput.type
        );
      }
      return false;
    }
    if (targetType === "__H3_EXTERNAL_OUTPUT__") {
      if (originType === "comfyui_h3_context.H3Context.ProductShell")
        return (
          edge.originSlot === 0 &&
          ((edge.targetSlot === 0 &&
            (edge.type === "STRING" || edge.type === "H3_PROMPT_STRING")) ||
            (edge.targetSlot === 1 && edge.type === "H3_PRODUCT_SHELL"))
        );
      if (originType === "comfyui_h3_context.H3Context.NativeH3Adapter")
        return (
          edge.originSlot === 1 &&
          edge.targetSlot === 2 &&
          edge.type === "H3_NATIVE_H3_WIRING"
        );
      if (originType === "comfyui_h3_context.H3Context.Validator")
        return (
          edge.originSlot === 1 &&
          edge.targetSlot === 1 &&
          edge.type === "H3_CONTEXT_REPORT"
        );
      if (originType === "comfyui_h3_context.H3Context.Preview")
        return (
          edge.originSlot === 1 &&
          edge.targetSlot === 3 &&
          edge.type === "H3_CONTEXT_PREVIEW"
        );
      return false;
    }
    if (
      originType === "comfyui_h3_context.H3Context.Request" &&
      targetType === "comfyui_h3_context.H3Context.Plan"
    )
      return (
        edge.originSlot === 0 &&
        edge.targetSlot === 0 &&
        edge.type === "H3_CONTEXT_REQUEST"
      );
    if (
      originType === "PrimitiveFloat" &&
      targetType === "comfyui_h3_context.H3Context.Request"
    )
      // IMPORTANT: M23-04 materialization exposes one visible duration source.
      // Admission recognizes only its exact Request socket; compiled-route
      // qualification still proves the value and shared native derivation.
      return (
        edge.originSlot === 0 && edge.targetSlot === 2 && edge.type === "FLOAT"
      );
    if (
      originType === "comfyui_h3_context.H3Context.Plan" &&
      (targetType === "comfyui_h3_context.H3Context.Compiler" ||
        targetType === "comfyui_h3_context.H3Context.Validator")
    )
      return (
        edge.originSlot === 0 &&
        edge.targetSlot === 0 &&
        edge.type === "H3_CONTEXT_PLAN"
      );
    if (
      originType === "comfyui_h3_context.H3Context.ReferenceRegistry" &&
      targetType === "comfyui_h3_context.H3Context.Plan"
    )
      return (
        edge.originSlot === 0 &&
        edge.targetSlot === 1 &&
        edge.type === "H3_REFERENCE_REGISTRY"
      );
    if (
      originType === "comfyui_h3_context.H3Context.Compiler" &&
      targetType === "comfyui_h3_context.H3Context.Validator"
    )
      return (
        edge.originSlot === 2 &&
        edge.targetSlot === 1 &&
        edge.type === "H3_PROMPT_DOCUMENT"
      );
    if (
      originType === "comfyui_h3_context.H3Context.Validator" &&
      (targetType === "comfyui_h3_context.H3Context.NativeH3Adapter" ||
        targetType === "comfyui_h3_context.H3Context.Preview" ||
        targetType === "comfyui_h3_context.H3Context.ProductShell")
    )
      return (
        edge.originSlot === 1 &&
        ((targetType === "comfyui_h3_context.H3Context.NativeH3Adapter" &&
          edge.targetSlot === 0) ||
          (targetType === "comfyui_h3_context.H3Context.Preview" &&
            edge.targetSlot === 0) ||
          (targetType === "comfyui_h3_context.H3Context.ProductShell" &&
            edge.targetSlot === 0)) &&
        edge.type === "H3_CONTEXT_REPORT"
      );
    if (
      originType === "comfyui_h3_context.H3Context.NativeH3Adapter" &&
      targetType === "comfyui_h3_context.H3Context.ProductShell"
    )
      return (
        edge.originSlot === 1 &&
        edge.targetSlot === 1 &&
        edge.type === "H3_NATIVE_H3_WIRING"
      );
    if (
      originType === "comfyui_h3_context.H3Context.ProductShell" &&
      (targetType === "MiniMaxH3ImageToVideo" ||
        targetType === "MiniMaxH3ReferenceToVideo")
    )
      return (
        edge.originSlot === 0 &&
        [0, 4].includes(edge.targetSlot) &&
        edge.type === "STRING"
      );
    if (originType === "LoadImage")
      return (
        ((targetType === referenceRegistry &&
          edge.targetSlot >= 0 &&
          edge.targetSlot <= 1) ||
          (targetType === "MiniMaxH3ReferenceToVideo" &&
            edge.targetSlot >= 8 &&
            edge.targetSlot <= 9) ||
          false) &&
        edge.type === "IMAGE"
      );
    if (originType === "LoadVideo")
      return (
        ((targetType === referenceRegistry && edge.targetSlot === 2) ||
          (targetType === "GetVideoComponents" && edge.targetSlot === 0)) &&
        edge.type === "VIDEO"
      );
    if (originType === "LoadAudio")
      return (
        ((targetType === referenceRegistry && edge.targetSlot === 3) ||
          (targetType === "MiniMaxH3ReferenceToVideo" &&
            edge.targetSlot === 12)) &&
        edge.type === "AUDIO"
      );
    if (originType === "GetVideoComponents")
      return (
        targetType === "MiniMaxH3ReferenceToVideo" &&
        ((edge.targetSlot === 10 && edge.type === "IMAGE") ||
          (edge.targetSlot === 11 && edge.type === "AUDIO"))
      );
    return false;
  };
  const edgeKeys = new Set<string>();
  // In an open-world graph only the edges that feed the context chain are held
  // to the allowlist. The rest belong to the template or to the user's own
  // design: this repository does not warrant how a sampler is wired, and an
  // allowlist that pretended to would reject correct graphs while proving
  // nothing about the part that matters.
  const enforced = (edge: SerializedEdge): boolean => {
    if (!openWorld) return true;
    const target = edgeType(edge.targetId);
    // The registry is the one context node whose inputs are the user's media
    // selection rather than the chain, and an open-world graph sizes those
    // sockets to that selection. What reaches the registry is verified against
    // the compiled prompt, where the socket names are available.
    return contextChainTypes.has(target) && target !== referenceRegistry;
  };
  if (
    edges.some(
      (edge) =>
        (enforced(edge) && !allowedEdge(edge)) || !edgeKeys.add(edgeKey(edge)),
    )
  )
    return false;
  const generationTypes = [
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
  ];
  const generation = generationTypes.filter(
    (type) => byType.get(type)?.length === 1,
  );
  const anchorLocal = options.anchorLocal !== false;
  if (requiredTypes.some((type) => byType.get(type)?.length !== 1))
    return false;
  // When the anchor sits inside a subgraph, this container cannot count it. The
  // count is taken once across the whole visible graph instead, and the family
  // is passed in rather than guessed.
  if (anchorLocal && generation.length !== 1) return false;
  const isReference = anchorLocal
    ? generation[0] === "MiniMaxH3ReferenceToVideo"
    : options.isReference === true;
  const referenceRegistryCount = byType.get(referenceRegistry)?.length ?? 0;
  const hasReferenceRegistry = referenceRegistryCount === 1;
  if (referenceRegistryCount > 1 || (isReference && !hasReferenceRegistry))
    return false;
  // An image-family registry is valid only in an open-world materialized graph;
  // the compiled-prompt qualifier then proves its exact frame roles and source
  // identities. Closed context-only packages retain their original shape.
  if (!isReference && hasReferenceRegistry && !openWorld) return false;
  const hasEdge = (
    originType: string,
    originSlot: number,
    targetType: string,
    targetSlot: number,
    edgeType: string,
  ): boolean => {
    const origins = new Set(byType.get(originType) ?? []);
    const targets = new Set(byType.get(targetType) ?? []);
    return edges.some(
      (edge) =>
        origins.has(edge.originId) &&
        edge.originSlot === originSlot &&
        targets.has(edge.targetId) &&
        edge.targetSlot === targetSlot &&
        edge.type === edgeType,
    );
  };
  const requiredEdges: Array<[string, number, string, number, string]> = [
    [
      "comfyui_h3_context.H3Context.Request",
      0,
      "comfyui_h3_context.H3Context.Plan",
      0,
      "H3_CONTEXT_REQUEST",
    ],
    [
      "comfyui_h3_context.H3Context.Plan",
      0,
      "comfyui_h3_context.H3Context.Compiler",
      0,
      "H3_CONTEXT_PLAN",
    ],
    [
      "comfyui_h3_context.H3Context.Plan",
      0,
      "comfyui_h3_context.H3Context.Validator",
      0,
      "H3_CONTEXT_PLAN",
    ],
    [
      "comfyui_h3_context.H3Context.Compiler",
      2,
      "comfyui_h3_context.H3Context.Validator",
      1,
      "H3_PROMPT_DOCUMENT",
    ],
    [
      "comfyui_h3_context.H3Context.Validator",
      1,
      "comfyui_h3_context.H3Context.NativeH3Adapter",
      0,
      "H3_CONTEXT_REPORT",
    ],
    [
      "comfyui_h3_context.H3Context.Validator",
      1,
      "comfyui_h3_context.H3Context.Preview",
      0,
      "H3_CONTEXT_REPORT",
    ],
    [
      "comfyui_h3_context.H3Context.Validator",
      1,
      "comfyui_h3_context.H3Context.ProductShell",
      0,
      "H3_CONTEXT_REPORT",
    ],
    [
      "comfyui_h3_context.H3Context.NativeH3Adapter",
      1,
      "comfyui_h3_context.H3Context.ProductShell",
      1,
      "H3_NATIVE_H3_WIRING",
    ],
  ];
  if (hasReferenceRegistry)
    requiredEdges.push([
      referenceRegistry,
      0,
      "comfyui_h3_context.H3Context.Plan",
      1,
      "H3_REFERENCE_REGISTRY",
    ]);
  // ComfyUI's serialized native node keeps the prompt socket at slot 4,
  // while the API/subgraph contract represents the same socket as slot 0.
  // Accept only those two pinned public layouts; arbitrary target slots remain
  // incompatible rather than becoming a type-count-only queue target.
  const nativePromptEdge =
    options.skipPromptEdge === true ||
    (generation[0] !== undefined &&
      [0, 4].some((targetSlot) =>
        hasEdge(
          "comfyui_h3_context.H3Context.ProductShell",
          0,
          generation[0]!,
          targetSlot,
          "STRING",
        ),
      ));
  return requiredEdges.every((edge) => hasEdge(...edge)) && nativePromptEdge;
}

export const anchorNodeTypes = new Set<string>([
  H3_NODE_TYPES.imageGeneration,
  H3_NODE_TYPES.referenceGeneration,
]);

function inputNameAt(
  node: Record<string, unknown>,
  slot: number,
): string | undefined {
  const ports = serializedPorts(node.inputs);
  const port = ports?.find((entry) => entry.slot === slot);
  const name = port?.value.name;
  return typeof name === "string" ? name : undefined;
}

function subgraphIdentityOf(
  node: Record<string, unknown>,
  definitions: ReadonlyMap<string, Record<string, unknown>>,
): string | undefined {
  const identity =
    node.subgraph_id !== undefined
      ? node.subgraph_id
      : typeof node.type === "string" && definitions.has(node.type)
        ? node.type
        : undefined;
  return identity === undefined ? undefined : String(identity);
}

/**
 * Verify that the compiled prompt reaches the native anchor, by socket name.
 *
 * In a graph this repository built, the prompt edge landed on a slot index it
 * chose, so an index was enough. In an open-world graph the anchor's slot layout
 * belongs to the host and to whoever assembled the canvas: the pinned reference
 * template puts `prompt` at slot 9, and a subgraph instance puts the promoted
 * `prompt` wherever the promotion order happened to place it. The socket name is
 * the stable thing, so that is what this follows.
 *
 * The anchor may also sit inside a subgraph, in which case the edge lands on the
 * instance's promoted `prompt` input and the path continues from the definition
 * boundary to the anchor itself. A check that stopped at the boundary would
 * accept a graph whose promoted input goes somewhere else entirely.
 */

export function validateOpenWorldPromptPath(
  rootNodes: Record<string, unknown>[],
  rootEdges: SerializedEdge[] | undefined,
  definitions: ReadonlyMap<string, Record<string, unknown>>,
): boolean {
  if (rootEdges === undefined) return false;
  const shell = rootNodes.find((node) => node.type === shellNodeId);
  if (shell === undefined) return false;

  const localAnchor = rootNodes.find((node) =>
    anchorNodeTypes.has(String(node.type)),
  );
  const nestedEntry = rootNodes.find((node) => {
    const identity = subgraphIdentityOf(node, definitions);
    const definition =
      identity === undefined ? undefined : definitions.get(identity);
    if (definition === undefined) return false;
    return serializedNodes(definition.nodes, "subgraph.nodes").some((inner) =>
      anchorNodeTypes.has(String(inner.type)),
    );
  });
  const target = localAnchor ?? nestedEntry;
  if (target === undefined) return false;

  // A legacy positional save has no socket names, and the pinned public layouts
  // put the prompt at slot 0 or 4. A named save is followed by name.
  const promptSlot = (node: Record<string, unknown>, slot: number): boolean => {
    const name = inputNameAt(node, slot);
    return name === undefined ? [0, 4].includes(slot) : name === "prompt";
  };
  const edge = rootEdges.find(
    (candidate) =>
      candidate.originId === String(shell.id) &&
      candidate.originSlot === 0 &&
      candidate.targetId === String(target.id) &&
      candidate.type === "STRING" &&
      promptSlot(target, candidate.targetSlot),
  );
  if (edge === undefined) return false;
  if (localAnchor !== undefined) return true;

  const identity = subgraphIdentityOf(target, definitions);
  const definition =
    identity === undefined ? undefined : definitions.get(identity);
  if (definition === undefined) return false;
  const promoted = Array.isArray(definition.inputs) ? definition.inputs : [];
  const promotedSlot = promoted.findIndex(
    (input) =>
      input !== null &&
      typeof input === "object" &&
      !Array.isArray(input) &&
      (input as Record<string, unknown>).name === "prompt",
  );
  if (promotedSlot < 0) return false;
  const innerNodes = serializedNodes(definition.nodes, "subgraph.nodes");
  const anchor = innerNodes.find((node) =>
    anchorNodeTypes.has(String(node.type)),
  );
  const innerEdges = definitionEdges(definition.links);
  if (anchor === undefined || innerEdges === undefined) return false;
  return innerEdges.some(
    (candidate) =>
      candidate.originId === "-10" &&
      candidate.originSlot === promotedSlot &&
      candidate.targetId === String(anchor.id) &&
      promptSlot(anchor, candidate.targetSlot),
  );
}

export function validateReferenceMediaTopology(
  nodes: Record<string, unknown>[],
  edges: SerializedEdge[] | undefined,
): boolean {
  if (edges === undefined) return false;
  const byType = new Map<string, Record<string, unknown>[]>();
  for (const node of nodes) {
    if (typeof node.type !== "string") continue;
    const bucket = byType.get(node.type) ?? [];
    bucket.push(node);
    byType.set(node.type, bucket);
  }
  const registry =
    byType.get("comfyui_h3_context.H3Context.ReferenceRegistry") ?? [];
  const generation = byType.get("MiniMaxH3ReferenceToVideo") ?? [];
  const video = byType.get("GetVideoComponents") ?? [];
  if (registry.length !== 1 || generation.length !== 1 || video.length > 1)
    return false;
  const registryId = String(registry[0]!.id);
  const generationId = String(generation[0]!.id);
  const videoId = video[0] === undefined ? undefined : String(video[0].id);
  const outgoing = (originId: string): SerializedEdge[] =>
    edges.filter((edge) => edge.originId === originId);
  const sources = nodes.filter((node) =>
    ["LoadImage", "LoadVideo", "LoadAudio"].includes(String(node.type)),
  );
  if (sources.length === 0) return false;
  if (!sources.some((node) => node.type === "LoadImage")) return false;
  const hasVideoSource = sources.some((node) => node.type === "LoadVideo");
  if ((videoId !== undefined) !== hasVideoSource) return false;
  if (
    sources.filter((node) => node.type === "LoadVideo").length > 1 ||
    sources.filter((node) => node.type === "LoadAudio").length > 1
  )
    return false;
  const usedRegistryImageSlots = new Set<number>();
  const usedGenerationImageSlots = new Set<number>();
  const imageSlots = new Set([8, 9]);
  for (const source of sources) {
    const id = String(source.id);
    if (source.type === "LoadImage") {
      const registryImageEdges = outgoing(id).filter(
        (edge) =>
          edge.targetId === registryId &&
          edge.targetSlot >= 0 &&
          edge.targetSlot <= 1 &&
          edge.type === "IMAGE",
      );
      const generationImageEdges = outgoing(id).filter(
        (edge) =>
          edge.targetId === generationId &&
          imageSlots.has(edge.targetSlot) &&
          edge.type === "IMAGE",
      );
      if (
        registryImageEdges.length !== 1 ||
        generationImageEdges.length !== 1 ||
        outgoing(id).filter((edge) => edge.type === "IMAGE").length !== 2 ||
        generationImageEdges[0]!.targetSlot !==
          registryImageEdges[0]!.targetSlot + 8
      )
        return false;
      if (
        usedRegistryImageSlots.has(registryImageEdges[0]!.targetSlot) ||
        usedGenerationImageSlots.has(generationImageEdges[0]!.targetSlot)
      )
        return false;
      usedRegistryImageSlots.add(registryImageEdges[0]!.targetSlot);
      usedGenerationImageSlots.add(generationImageEdges[0]!.targetSlot);
    } else if (source.type === "LoadVideo") {
      const registryVideoEdges = outgoing(id).filter(
        (edge) =>
          edge.targetId === registryId &&
          edge.targetSlot === 2 &&
          edge.type === "VIDEO",
      );
      const videoInputEdges =
        videoId === undefined
          ? []
          : outgoing(id).filter(
              (edge) =>
                edge.targetId === videoId &&
                edge.targetSlot === 0 &&
                edge.type === "VIDEO",
            );
      if (
        registryVideoEdges.length !== 1 ||
        videoId === undefined ||
        videoInputEdges.length !== 1 ||
        outgoing(id).filter((edge) => edge.type === "VIDEO").length !== 2
      )
        return false;
    } else if (
      outgoing(id).filter(
        (edge) =>
          edge.targetId === registryId &&
          edge.targetSlot === 3 &&
          edge.type === "AUDIO",
      ).length !== 1 ||
      outgoing(id).filter(
        (edge) =>
          edge.targetId === generationId &&
          edge.targetSlot === 12 &&
          edge.type === "AUDIO",
      ).length !== 1 ||
      outgoing(id).filter((edge) => edge.type === "AUDIO").length !== 2
    )
      return false;
  }
  if (
    usedRegistryImageSlots.size !==
      sources.filter((node) => node.type === "LoadImage").length ||
    usedGenerationImageSlots.size !== usedRegistryImageSlots.size ||
    [...usedRegistryImageSlots].some((slot) => ![0, 1].includes(slot))
  )
    return false;
  if (videoId !== undefined) {
    const videoInput = outgoing(videoId).some(
      (edge) =>
        edge.targetId === generationId &&
        edge.targetSlot === 10 &&
        edge.type === "IMAGE",
    );
    if (!videoInput) return false;
  }
  return true;
}

export function definitionContainsShell(
  definition: Record<string, unknown>,
): boolean {
  return Array.isArray(definition.nodes)
    ? definition.nodes.some(
        (node) =>
          node !== null &&
          typeof node === "object" &&
          !Array.isArray(node) &&
          (node as Record<string, unknown>).type === shellNodeId,
      )
    : false;
}

export function validateShellBoundaryDefinition(
  definition: Record<string, unknown>,
): boolean {
  const nodes = serializedNodes(definition.nodes, "subgraph.nodes");
  if (
    nodes.length !== 1 ||
    nodes[0]?.type !== shellNodeId ||
    !validateSubgraphDefinitionLinks(definition, nodes, false)
  )
    return false;
  const inputs = definition.inputs as Array<Record<string, unknown>>;
  const outputs = definition.outputs as Array<Record<string, unknown>>;
  if (
    inputs.length !== 2 ||
    outputs.length !== 2 ||
    inputs[0]?.name !== "report" ||
    inputs[0]?.type !== "H3_CONTEXT_REPORT" ||
    inputs[1]?.name !== "native_h3_wiring" ||
    inputs[1]?.type !== "H3_NATIVE_H3_WIRING" ||
    outputs[0]?.name !== "prompt" ||
    outputs[0]?.type !== "STRING" ||
    outputs[1]?.name !== "product_shell" ||
    outputs[1]?.type !== "H3_PRODUCT_SHELL"
  )
    return false;
  const shellId = String(nodes[0].id);
  const edges = definitionEdges(definition.links);
  if (edges === undefined || edges.length !== 4) return false;
  const requiredEdges: Array<SerializedEdge> = [
    {
      originId: "-10",
      originSlot: 0,
      targetId: shellId,
      targetSlot: 0,
      type: "H3_CONTEXT_REPORT",
    },
    {
      originId: "-10",
      originSlot: 1,
      targetId: shellId,
      targetSlot: 1,
      type: "H3_NATIVE_H3_WIRING",
    },
    {
      originId: shellId,
      originSlot: 0,
      targetId: "-20",
      targetSlot: 0,
      type: "STRING",
    },
    {
      originId: shellId,
      originSlot: 1,
      targetId: "-20",
      targetSlot: 1,
      type: "H3_PRODUCT_SHELL",
    },
  ];
  return requiredEdges.every((required) =>
    edges.some(
      (edge) =>
        edge.originId === required.originId &&
        edge.originSlot === required.originSlot &&
        edge.targetId === required.targetId &&
        edge.targetSlot === required.targetSlot &&
        edge.type === required.type,
    ),
  );
}
