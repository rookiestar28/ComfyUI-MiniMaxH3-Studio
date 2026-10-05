/**
 * Whether an external reference boundary qualifies.
 *
 * This is the owner of that question. A later UI must ask here rather than deciding for itself
 * whether a reference is usable -- fabricated availability is exactly what this boundary exists to
 * make impossible.
 */

import {
  assertSafeSerializedKeys,
  definitionEdges,
  isLinkId,
  nodeIdentifier,
  record,
  serializedNodes,
} from "./graphSerialization";
import type { SerializedEdge } from "./graphSerialization";
import { serializedPorts } from "./graphPortShapes";
import { validateSerializedWrapperPorts } from "./graphNodeContracts";
import { validateSubgraphDefinitionLinks } from "./graphSubgraphLinks";

export function isQualifiedExternalReferenceBoundary(
  definition: Record<string, unknown>,
): boolean {
  let nodes: Record<string, unknown>[];
  let edges: SerializedEdge[] | undefined;
  try {
    nodes = serializedNodes(definition.nodes, "subgraph.nodes");
    edges = definitionEdges(definition.links);
  } catch {
    return false;
  }
  if (
    !validateSubgraphDefinitionLinks(definition, nodes) ||
    edges === undefined
  ) {
    return false;
  }
  const registryNodes = nodes.filter(
    (node) => node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
  );
  const generationNodes = nodes.filter(
    (node) => node.type === "MiniMaxH3ReferenceToVideo",
  );
  const videoNodes = nodes.filter((node) => node.type === "GetVideoComponents");
  if (
    registryNodes.length !== 1 ||
    generationNodes.length !== 1 ||
    videoNodes.length !== 1
  ) {
    return false;
  }
  const expectedNodeTypes = new Set([
    "comfyui_h3_context.H3Context.Request",
    "GetVideoComponents",
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    "comfyui_h3_context.H3Context.Plan",
    "comfyui_h3_context.H3Context.Compiler",
    "comfyui_h3_context.H3Context.Validator",
    "comfyui_h3_context.H3Context.NativeH3Adapter",
    "comfyui_h3_context.H3Context.ProductShell",
    "MiniMaxH3ReferenceToVideo",
    "comfyui_h3_context.H3Context.Preview",
  ]);
  if (
    nodes.length !== expectedNodeTypes.size ||
    nodes.some(
      (node) =>
        typeof node.type !== "string" || !expectedNodeTypes.has(node.type),
    ) ||
    [...expectedNodeTypes].some(
      (type) => nodes.filter((node) => node.type === type).length !== 1,
    )
  ) {
    return false;
  }
  const registry = registryNodes[0]!;
  const generation = generationNodes[0]!;
  const video = videoNodes[0]!;
  const request = nodes.find(
    (node) => node.type === "comfyui_h3_context.H3Context.Request",
  );
  if (request === undefined) return false;

  const expectedInputs = [
    ["task_mode", "COMBO"],
    ["user_intent", "STRING"],
    ["duration_seconds", "FLOAT"],
    ["image_1", "IMAGE"],
    ["image_2", "IMAGE"],
    ["video_1", "VIDEO"],
    ["audio_1", "AUDIO"],
  ] as const;
  const expectedOutputs = [
    ["prompt", "H3_PROMPT_STRING"],
    ["report", "H3_CONTEXT_REPORT"],
    ["native_h3_wiring", "H3_NATIVE_H3_WIRING"],
    ["preview", "H3_CONTEXT_PREVIEW"],
  ] as const;
  const interfaceMatches = (
    value: unknown,
    expected: ReadonlyArray<readonly [string, string]>,
  ): boolean => {
    if (!Array.isArray(value) || value.length !== expected.length) return false;
    return expected.every(([name, type], index) => {
      const port = value[index];
      return (
        port !== null &&
        typeof port === "object" &&
        !Array.isArray(port) &&
        (port as Record<string, unknown>).name === name &&
        (port as Record<string, unknown>).type === type
      );
    });
  };
  if (
    !interfaceMatches(definition.inputs, expectedInputs) ||
    !interfaceMatches(definition.outputs, expectedOutputs)
  ) {
    return false;
  }
  const linkedInputSlots = (
    node: Record<string, unknown>,
    predicate: (value: Record<string, unknown>) => boolean,
  ): Set<number> => {
    const inputs = Array.isArray(node.inputs) ? node.inputs : [];
    return new Set(
      inputs
        .filter(
          (value): value is Record<string, unknown> =>
            value !== null &&
            typeof value === "object" &&
            !Array.isArray(value) &&
            predicate(value) &&
            isLinkId(value.link),
        )
        .map(
          (value) => edges.find((edge) => edge.id === value.link)?.targetSlot,
        )
        .filter((slot): slot is number => slot !== undefined),
    );
  };
  const registryImageSlots = linkedInputSlots(
    registry,
    (value) =>
      value.type === "IMAGE" && String(value.name).startsWith("images."),
  );
  const generationImageSlots = linkedInputSlots(
    generation,
    (value) =>
      value.type === "IMAGE" && String(value.name).startsWith("ref_images."),
  );
  const generationVideoSlots = linkedInputSlots(generation, (value) =>
    String(value.name).startsWith("ref_videos."),
  );
  const videoInputSlot =
    [
      ...linkedInputSlots(
        video,
        (value) => value.name === "video" && value.type === "VIDEO",
      ),
    ][0] ?? -1;
  if (
    registryImageSlots.size === 0 ||
    generationImageSlots.size === 0 ||
    generationVideoSlots.size === 0 ||
    videoInputSlot < 0
  ) {
    return false;
  }
  const imageBoundarySlots = new Set(
    (Array.isArray(definition.inputs) ? definition.inputs : [])
      .map((value, index) => ({ value, index }))
      .filter(
        ({ value }) =>
          value !== null &&
          typeof value === "object" &&
          !Array.isArray(value) &&
          (value as Record<string, unknown>).type === "IMAGE",
      )
      .map(({ index }) => index),
  );
  const imageRegistryEdges = edges.filter(
    (edge) =>
      edge.originId === "-10" &&
      edge.type === "IMAGE" &&
      edge.targetId === String(registry.id) &&
      registryImageSlots.has(edge.targetSlot) &&
      imageBoundarySlots.has(edge.originSlot),
  );
  const imageGenerationEdges = edges.filter(
    (edge) =>
      edge.originId === "-10" &&
      edge.type === "IMAGE" &&
      edge.targetId === String(generation.id) &&
      generationImageSlots.has(edge.targetSlot) &&
      imageBoundarySlots.has(edge.originSlot),
  );
  const registryImageSources = new Set(
    imageRegistryEdges.map((edge) => edge.originSlot),
  );
  const generationImageSources = new Set(
    imageGenerationEdges.map((edge) => edge.originSlot),
  );
  if (
    imageRegistryEdges.length === 0 ||
    imageGenerationEdges.length === 0 ||
    [...registryImageSources].some((slot) => !generationImageSources.has(slot))
  ) {
    return false;
  }
  const inputEdge = (
    node: Record<string, unknown>,
    name: string,
  ): SerializedEdge | undefined => {
    const ports = serializedPorts(node.inputs);
    const port = ports?.find(({ value }) => value.name === name)?.value;
    if (port === undefined || !isLinkId(port.link)) return undefined;
    return edges.find((edge) => edge.id === port.link);
  };
  const expectedInputEdge = (
    node: Record<string, unknown>,
    name: string,
    originId: string,
    originSlot: number,
    targetSlot: number,
    type: string,
  ): boolean => {
    const edge = inputEdge(node, name);
    return (
      edge !== undefined &&
      edge.originId === originId &&
      edge.originSlot === originSlot &&
      edge.targetId === String(node.id) &&
      edge.targetSlot === targetSlot &&
      edge.type === type
    );
  };
  const requiredExternalEdge = (
    originSlot: number,
    targetId: string,
    targetSlot: number,
    type: string,
  ): boolean =>
    edges.some(
      (edge) =>
        edge.originId === "-10" &&
        edge.originSlot === originSlot &&
        edge.targetId === targetId &&
        edge.targetSlot === targetSlot &&
        edge.type === type,
    );
  if (
    !expectedInputEdge(request, "task_mode", "-10", 0, 0, "COMBO") ||
    !expectedInputEdge(request, "user_intent", "-10", 1, 1, "STRING") ||
    !expectedInputEdge(request, "duration_seconds", "-10", 2, 2, "FLOAT") ||
    !expectedInputEdge(registry, "images.image0", "-10", 3, 0, "IMAGE") ||
    !expectedInputEdge(registry, "images.image1", "-10", 4, 1, "IMAGE") ||
    !expectedInputEdge(registry, "videos.video0", "-10", 5, 2, "VIDEO") ||
    !expectedInputEdge(registry, "audios.audio0", "-10", 6, 3, "AUDIO") ||
    !expectedInputEdge(generation, "ref_images.image0", "-10", 3, 8, "IMAGE") ||
    !expectedInputEdge(generation, "ref_images.image1", "-10", 4, 9, "IMAGE") ||
    !expectedInputEdge(
      generation,
      "ref_videos.video0",
      String(video.id),
      0,
      10,
      "IMAGE",
    ) ||
    !expectedInputEdge(
      generation,
      "ref_audios.audio0",
      "-10",
      6,
      12,
      "AUDIO",
    ) ||
    !expectedInputEdge(video, "video", "-10", 5, 0, "VIDEO")
  ) {
    return false;
  }
  const optionalVideoAudio = inputEdge(
    generation,
    "ref_video_audios.video_audio0",
  );
  if (
    optionalVideoAudio !== undefined &&
    !(
      optionalVideoAudio.originId === String(video.id) &&
      optionalVideoAudio.originSlot === 1 &&
      optionalVideoAudio.targetSlot === 11 &&
      optionalVideoAudio.type === "AUDIO"
    )
  ) {
    return false;
  }
  if (
    !requiredExternalEdge(3, String(registry.id), 0, "IMAGE") ||
    !requiredExternalEdge(3, String(generation.id), 8, "IMAGE") ||
    !requiredExternalEdge(4, String(registry.id), 1, "IMAGE") ||
    !requiredExternalEdge(4, String(generation.id), 9, "IMAGE") ||
    !requiredExternalEdge(5, String(registry.id), 2, "VIDEO") ||
    !requiredExternalEdge(5, String(video.id), 0, "VIDEO") ||
    !requiredExternalEdge(6, String(registry.id), 3, "AUDIO") ||
    !requiredExternalEdge(6, String(generation.id), 12, "AUDIO")
  )
    return false;
  const allowedExternalEdges = new Set([
    `0:${String(request.id)}:0:COMBO`,
    `1:${String(request.id)}:1:STRING`,
    `2:${String(request.id)}:2:FLOAT`,
    `3:${String(registry.id)}:0:IMAGE`,
    `3:${String(generation.id)}:8:IMAGE`,
    `4:${String(registry.id)}:1:IMAGE`,
    `4:${String(generation.id)}:9:IMAGE`,
    `5:${String(registry.id)}:2:VIDEO`,
    `5:${String(video.id)}:0:VIDEO`,
    `6:${String(registry.id)}:3:AUDIO`,
    `6:${String(generation.id)}:12:AUDIO`,
  ]);
  if (
    edges.some(
      (edge) =>
        edge.originId === "-10" &&
        !allowedExternalEdges.has(
          `${edge.originSlot}:${edge.targetId}:${edge.targetSlot}:${edge.type}`,
        ),
    )
  )
    return false;
  const videoBoundary = edges.find(
    (edge) =>
      edge.originId === "-10" &&
      edge.type === "VIDEO" &&
      edge.targetId === String(video.id) &&
      edge.targetSlot === videoInputSlot,
  );
  if (videoBoundary === undefined) {
    return false;
  }
  const videoOutput = edges.some(
    (edge) =>
      edge.originId === String(video.id) &&
      edge.targetId === String(generation.id) &&
      edge.type === "IMAGE" &&
      generationVideoSlots.has(edge.targetSlot),
  );
  return videoOutput;
}

/**
 * Prove that a serialized workflow's reachable Subgraph is the qualified
 * external-media Reference Assistant boundary. This is intentionally narrow:
 * an unreferenced or wrapper-only definition must not make an API prompt
 * queueable when its media sources are absent from the compiled output.
 */

export function isQualifiedExternalReferenceGraph(value: unknown): boolean {
  try {
    assertSafeSerializedKeys(value);
    const graph = record(value, "graph");
    if (Object.hasOwn(graph, "_nodes")) return false;
    const rootNodes = serializedNodes(graph.nodes, "graph.nodes");
    if (!Array.isArray(graph.links) || graph.links.length !== 0) return false;
    const definitionRoot = record(graph.definitions, "graph.definitions");
    const definitions = serializedNodes(
      definitionRoot.subgraphs,
      "graph.definitions.subgraphs",
    );
    if (definitions.length === 0) return false;
    const byId = new Map<string, Record<string, unknown>>();
    for (const definition of definitions) {
      if (Object.hasOwn(definition, "_nodes")) return false;
      const id = nodeIdentifier(definition.id, "subgraph definition");
      if (byId.has(id)) return false;
      for (const node of serializedNodes(definition.nodes, "subgraph.nodes")) {
        if (Object.hasOwn(node, "_nodes")) return false;
      }
      byId.set(id, definition);
    }
    if (validateSerializedWrapperPorts(rootNodes, byId) !== undefined)
      return false;
    const reachable = new Set<string>();
    const active = new Set<string>();
    const visit = (nodes: Record<string, unknown>[]): boolean => {
      for (const node of nodes) {
        if (Object.hasOwn(node, "_nodes")) return false;
        const identity =
          node.subgraph_id !== undefined
            ? node.subgraph_id
            : typeof node.type === "string" && byId.has(node.type)
              ? node.type
              : undefined;
        if (identity === undefined) continue;
        const id = nodeIdentifier(identity, "node.subgraph_id");
        const definition = byId.get(id);
        if (definition === undefined || active.has(id)) return false;
        reachable.add(id);
        active.add(id);
        const nested = serializedNodes(definition.nodes, "subgraph.nodes");
        if (!visit(nested)) return false;
        active.delete(id);
      }
      return true;
    };
    if (!visit(rootNodes)) return false;
    // A qualified external workflow is one visible wrapper around one reachable
    // definition. Extra root nodes would make an unrelated graph lend its
    // external-media authority to the active canvas.
    if (
      rootNodes.length !== 1 ||
      reachable.size !== 1 ||
      (() => {
        const root = rootNodes[0];
        if (root === undefined) return false;
        const identity =
          root.subgraph_id !== undefined
            ? root.subgraph_id
            : typeof root.type === "string" && byId.has(root.type)
              ? root.type
              : undefined;
        return (
          identity !== undefined &&
          byId.has(nodeIdentifier(identity, "root.subgraph_id"))
        );
      })() === false
    )
      return false;
    const qualified = [...reachable]
      .map((id) => byId.get(id))
      .filter(
        (definition): definition is Record<string, unknown> =>
          definition !== undefined &&
          isQualifiedExternalReferenceBoundary(definition),
      );
    // A serialized workflow may carry definitions that are not reachable from
    // the visible wrapper. Reject them rather than allowing an unrelated
    // qualified definition to lend queue authority to the active graph.
    return (
      qualified.length === 1 &&
      reachable.size === 1 &&
      definitions.length === reachable.size
    );
  } catch {
    return false;
  }
}
