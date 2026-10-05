/**
 * The visible-graph inspection entry point, and the module every consumer imports.
 *
 * This file was a single 3382-line module holding serialization primitives, port
 * shape reading, node contracts, subgraph links, reference qualification and topology
 * validation alongside the one function that ties them together. Those are six
 * separate questions and they now have six owners. The public surface is unchanged:
 * every name that could be imported from here before still can.
 */

import {
  H3_NODE_TYPES,
  canonicalTypes,
  contextChainTypes,
  definitionEdges,
  maxDepth,
  maxIdentifierLength,
  maxNodes,
  nodeIdentifier,
  readQueueMetadataCompatibleGraph,
  record,
  requiredTypes,
  serializedNodes,
  shellFieldId,
  shellNodeId,
  tupleEdges,
  validateSerializedLinks,
} from "./graphSerialization";
import type {
  GraphAnchor,
  GraphInspection,
  SerializedEdge,
} from "./graphSerialization";
import {
  validateDirectExecutableContracts,
  validateSerializedNamedPorts,
  validateSerializedWrapperPorts,
} from "./graphNodeContracts";
import { validateSubgraphDefinitionLinks } from "./graphSubgraphLinks";
import { isQualifiedExternalReferenceBoundary } from "./graphReferenceQualification";
import {
  anchorNodeTypes,
  definitionContainsShell,
  validateCanonicalTopology,
  validateOpenWorldPromptPath,
  validateReferenceMediaTopology,
  validateShellBoundaryDefinition,
} from "./graphTopology";

export function inspectVisibleH3Graph(
  graphValue: unknown,
  manifestValue: unknown,
): GraphInspection {
  const { graph, rootNodes } = readQueueMetadataCompatibleGraph(graphValue);
  const nodeCount = rootNodes.length;
  const manifest = record(manifestValue, "manifest");
  if (!Array.isArray(manifest.fields))
    return {
      status: "incompatible",
      anchors: [],
      nodeCount,
      reason: "missing_manifest_fields",
    };
  const hasAuthority = manifest.fields.some((value) => {
    const field = record(value, "manifest field");
    return (
      field.field_id === shellFieldId &&
      field.node_id === shellNodeId &&
      field.port_name === "product_shell"
    );
  });
  if (!hasAuthority)
    return {
      status: "incompatible",
      anchors: [],
      nodeCount,
      reason: "missing_shell_authority",
    };

  const definitions = new Map<string, Record<string, unknown>>();
  if (graph.definitions !== undefined) {
    const definitionRoot = record(graph.definitions, "graph.definitions");
    if (Object.keys(definitionRoot).some((key) => key !== "subgraphs"))
      throw new Error("graph definitions contain an unknown serialized member");
    if (definitionRoot.subgraphs !== undefined) {
      const allowedDefinitionKeys = new Set([
        "category",
        "config",
        "description",
        "extra",
        "groups",
        "id",
        "inputNode",
        "inputs",
        "links",
        "name",
        "nodes",
        "outputNode",
        "outputs",
        "reroutes",
        "revision",
        "state",
        "version",
        "widgets",
      ]);
      for (const definition of serializedNodes(
        definitionRoot.subgraphs,
        "graph.definitions.subgraphs",
      )) {
        if (
          Object.keys(definition).some((key) => !allowedDefinitionKeys.has(key))
        )
          throw new Error(
            "subgraph definitions contain an unknown serialized member",
          );
        if (
          definition.reroutes !== undefined &&
          (!Array.isArray(definition.reroutes) ||
            definition.reroutes.length !== 0)
        )
          throw new Error("subgraph definition contains unsupported reroutes");
        if (Object.hasOwn(definition, "_nodes"))
          throw new Error(
            "subgraph definition must use serialized public data",
          );
        const id = nodeIdentifier(definition.id, "subgraph definition");
        if (definitions.has(id))
          throw new Error("serialized subgraph definitions contain duplicates");
        definitions.set(id, definition);
      }
    }
  }

  const anchors: GraphAnchor[] = [];
  const typeCounts = new Map<string, number>();
  const reachableDefinitionIds = new Set<string>();
  let untypedNode = false;
  let openWorld = false;
  let seen = 0;
  const active = new Set<string>();
  const visit = (
    nodes: Record<string, unknown>[],
    prefix: string,
    depth: number,
  ): void => {
    if (depth > maxDepth)
      throw new Error("serialized graph exceeds recursion bound");
    const namespaceIds = new Set<string>();
    for (const node of nodes) {
      seen += 1;
      if (seen > maxNodes)
        throw new Error("serialized graph exceeds node bound");
      if (Object.hasOwn(node, "_nodes"))
        throw new Error("node must use serialized public data");
      const id = nodeIdentifier(node.id, "node");
      if (namespaceIds.has(id))
        throw new Error("serialized node namespace contains duplicate IDs");
      namespaceIds.add(id);
      const executionId = prefix.length === 0 ? id : `${prefix}:${id}`;
      if (executionId.length > maxIdentifierLength)
        throw new Error("composed execution ID exceeds its bound");
      const nodeType = node.type;
      if (typeof nodeType !== "string") {
        untypedNode = true;
      } else if (canonicalTypes.has(nodeType)) {
        typeCounts.set(nodeType, (typeCounts.get(nodeType) ?? 0) + 1);
      }
      if (node.type === shellNodeId)
        anchors.push({ executionId, nodeId: shellNodeId });
      const subgraphIdentity =
        node.subgraph_id !== undefined
          ? node.subgraph_id
          : typeof node.type === "string" && definitions.has(node.type)
            ? node.type
            : undefined;
      // M17-20 D11/D13: a node this repository does not own is the normal case
      // now, not a defect -- the official templates ship loaders, samplers,
      // decode and a sink. Its presence switches this inspection to the
      // open-world rules rather than rejecting the canvas.
      if (
        typeof nodeType === "string" &&
        !canonicalTypes.has(nodeType) &&
        subgraphIdentity === undefined
      )
        openWorld = true;
      if (subgraphIdentity !== undefined) {
        const subgraphId = nodeIdentifier(subgraphIdentity, "node.subgraph_id");
        const definition = definitions.get(subgraphId);
        if (definition === undefined)
          throw new Error("serialized graph references a missing subgraph");
        reachableDefinitionIds.add(subgraphId);
        if (active.has(subgraphId))
          throw new Error("serialized graph contains a subgraph cycle");
        active.add(subgraphId);
        visit(
          serializedNodes(definition.nodes, "subgraph.nodes"),
          executionId,
          depth + 1,
        );
        active.delete(subgraphId);
      }
    }
  };
  visit(rootNodes, "", 0);
  const wrapperPortFailure = validateSerializedWrapperPorts(
    rootNodes,
    definitions,
  );
  if (wrapperPortFailure !== undefined)
    return {
      status: "incompatible",
      anchors,
      nodeCount,
      reason: wrapperPortFailure,
    };
  const namedPortFailure = validateSerializedNamedPorts(rootNodes);
  if (namedPortFailure !== undefined)
    return {
      status: "incompatible",
      anchors,
      nodeCount,
      reason: namedPortFailure,
    };
  if (anchors.length === 0) return { status: "missing", anchors, nodeCount };
  if (anchors.length > 1) return { status: "ambiguous", anchors, nodeCount };
  // A node with no serialized type is still a defect: it cannot be reasoned
  // about at all, in either world.
  if (untypedNode)
    return {
      status: "incompatible",
      anchors,
      nodeCount,
      reason: "untyped_node",
    };
  const imageGenerationCount = typeCounts.get("MiniMaxH3ImageToVideo") ?? 0;
  const referenceGenerationCount =
    typeCounts.get("MiniMaxH3ReferenceToVideo") ?? 0;
  const generationCount = imageGenerationCount + referenceGenerationCount;
  if (
    requiredTypes.some((type) => typeCounts.get(type) !== 1) ||
    generationCount !== 1
  )
    return {
      status: "incompatible",
      anchors,
      nodeCount,
      reason: "incomplete_h3_flow",
    };
  const isReference = referenceGenerationCount === 1;
  const referenceRegistryCount =
    typeCounts.get("comfyui_h3_context.H3Context.ReferenceRegistry") ?? 0;
  const videoComponentsCount = typeCounts.get("GetVideoComponents") ?? 0;
  const referenceSourceCount = ["LoadImage", "LoadVideo", "LoadAudio"].reduce(
    (total, type) => total + (typeCounts.get(type) ?? 0),
    0,
  );
  const qualifiedExternalReferenceBoundary =
    isReference &&
    referenceSourceCount === 0 &&
    [...reachableDefinitionIds]
      .map((id) => definitions.get(id))
      .some(
        (definition) =>
          definition !== undefined &&
          isQualifiedExternalReferenceBoundary(definition),
      );
  // The registry pairing is a contract in both worlds: a reference plan without
  // the registry is not the same plan. The remaining rules say "a graph this
  // repository built carries no media node it did not put there", which stops
  // being true the moment the canvas is an official template -- an i2v template
  // ships a LoadImage for the frame it binds.
  if (
    generationCount === 1 &&
    ((isReference && referenceRegistryCount !== 1) ||
      (!isReference && referenceRegistryCount > (openWorld ? 1 : 0)) ||
      (!openWorld && !isReference && videoComponentsCount !== 0) ||
      (!openWorld && !isReference && referenceSourceCount !== 0) ||
      (!openWorld &&
        isReference &&
        referenceSourceCount === 0 &&
        !qualifiedExternalReferenceBoundary))
  )
    return {
      status: "incompatible",
      anchors,
      nodeCount,
      reason: "mixed_h3_flow",
    };
  const linkFailure = validateSerializedLinks(graph, rootNodes);
  if (linkFailure !== undefined)
    return {
      status: "incompatible",
      anchors,
      nodeCount,
      reason: linkFailure,
    };
  const directContractFailure =
    definitions.size === 0
      ? validateDirectExecutableContracts(rootNodes, graph.links)
      : undefined;
  // IMPORTANT: an unreferenced definition is still executable serialized data;
  // do not let hidden workflow ownership lend compatibility to a direct graph.
  const definitionsClosed = definitions.size === reachableDefinitionIds.size;
  const hasSerializedLinks =
    graph.links !== undefined ||
    rootNodes.some(
      (node) => node.inputs !== undefined || node.outputs !== undefined,
    );
  const rootTypeOverrides = new Map<string, string>();
  for (const node of rootNodes) {
    const subgraphIdentity =
      node.subgraph_id !== undefined
        ? node.subgraph_id
        : typeof node.type === "string" && definitions.has(node.type)
          ? node.type
          : undefined;
    if (subgraphIdentity === undefined) continue;
    const definition = definitions.get(String(subgraphIdentity));
    if (definition !== undefined && definitionContainsShell(definition))
      rootTypeOverrides.set(String(node.id), shellNodeId);
  }
  const rootHasAnchor = rootNodes.some((node) =>
    anchorNodeTypes.has(String(node.type)),
  );
  const rootTopology = validateCanonicalTopology(
    rootNodes,
    tupleEdges(graph.links),
    rootTypeOverrides,
    {
      openWorld,
      anchorLocal: !openWorld || rootHasAnchor,
      isReference,
      skipPromptEdge: openWorld,
    },
  );
  // In an open-world graph the prompt edge is followed by socket name, and
  // across the subgraph boundary when the anchor lives inside one.
  const openWorldPromptPath =
    !openWorld ||
    validateOpenWorldPromptPath(
      rootNodes,
      tupleEdges(graph.links),
      definitions,
    );
  let malformedSubgraphDefinition = false;
  const definitionTopologies = [...definitions.values()].map((definition) => {
    const nodes = serializedNodes(definition.nodes, "subgraph.nodes");
    // A Product Shell Boundary is intentionally a one-node wrapper. Its
    // canonical producer chain remains in the parent graph, so do not force
    // the wrapper definition itself to contain Request/Plan/Native stages.
    // A definition that carries none of the context chain is template or user
    // territory in an open-world graph: this repository spliced nothing into it
    // and warrants nothing about it.
    const carriesContext = nodes.some((node) =>
      contextChainTypes.has(String(node.type)),
    );
    const valid =
      openWorld && !carriesContext
        ? true
        : nodes.length === 1 && nodes[0]?.type === shellNodeId
          ? validateShellBoundaryDefinition(definition)
          : validateSubgraphDefinitionLinks(definition, nodes) &&
            validateCanonicalTopology(
              nodes,
              definitionEdges(definition.links),
              new Map(),
              { openWorld, isReference, skipPromptEdge: openWorld },
            );
    if (!valid) malformedSubgraphDefinition = true;
    return valid;
  });
  const canonicalTopology =
    rootTopology && definitionTopologies.every((value) => value);
  const topologyContainers: Array<{
    nodes: Record<string, unknown>[];
    edges: SerializedEdge[] | undefined;
  }> = [{ nodes: rootNodes, edges: tupleEdges(graph.links) }];
  for (const definitionId of reachableDefinitionIds) {
    const definition = definitions.get(definitionId);
    if (definition === undefined) continue;
    topologyContainers.push({
      nodes: serializedNodes(definition.nodes, "subgraph.nodes"),
      edges: definitionEdges(definition.links),
    });
  }
  const activeReferenceContainers = topologyContainers.filter(
    ({ nodes }) =>
      nodes.filter((node) => node.type === "MiniMaxH3ReferenceToVideo")
        .length === 1,
  );
  const activeReferenceSourceCount =
    activeReferenceContainers.length === 1
      ? activeReferenceContainers[0]!.nodes.filter((node) =>
          ["LoadImage", "LoadVideo", "LoadAudio"].includes(String(node.type)),
        ).length
      : -1;
  const referenceMediaTopologyValid =
    openWorld ||
    !isReference ||
    referenceSourceCount === 0 ||
    (activeReferenceContainers.length === 1 &&
      activeReferenceSourceCount === referenceSourceCount &&
      validateReferenceMediaTopology(
        activeReferenceContainers[0]!.nodes,
        activeReferenceContainers[0]!.edges,
      ));
  const externalReferenceRootCompatible =
    isReference &&
    qualifiedExternalReferenceBoundary &&
    rootNodes.length === 1 &&
    Array.isArray(graph.links) &&
    graph.links.length === 0 &&
    reachableDefinitionIds.size === 1 &&
    [...reachableDefinitionIds].every((id) => {
      const definition = definitions.get(id);
      return (
        definition !== undefined &&
        isQualifiedExternalReferenceBoundary(definition)
      );
    }) &&
    definitionTopologies.every((value) => value);
  const baseSubgraphRootCompatible =
    !isReference &&
    rootNodes.length === 1 &&
    Array.isArray(graph.links) &&
    graph.links.length === 0 &&
    reachableDefinitionIds.size === 1 &&
    definitions.size === reachableDefinitionIds.size &&
    definitionTopologies.every((value) => value) &&
    [...reachableDefinitionIds].some((id) => {
      const definition = definitions.get(id);
      if (definition === undefined) return false;
      const definitionNodes = serializedNodes(
        definition.nodes,
        "subgraph.nodes",
      );
      const counts = new Map<string, number>();
      for (const node of definitionNodes) {
        if (typeof node.type !== "string") continue;
        counts.set(node.type, (counts.get(node.type) ?? 0) + 1);
      }
      return (
        requiredTypes.every((type) => counts.get(type) === 1) &&
        counts.get(H3_NODE_TYPES.imageGeneration) === 1
      );
    });
  return {
    status: "ready",
    anchors,
    nodeCount,
    existingGraphCompatible:
      hasSerializedLinks &&
      Array.isArray(graph.links) &&
      (graph.links.length > 0 ||
        externalReferenceRootCompatible ||
        baseSubgraphRootCompatible) &&
      (canonicalTopology ||
        externalReferenceRootCompatible ||
        baseSubgraphRootCompatible) &&
      openWorldPromptPath &&
      definitionsClosed &&
      referenceMediaTopologyValid &&
      directContractFailure === undefined,
    reason:
      directContractFailure ??
      (!canonicalTopology && malformedSubgraphDefinition
        ? "malformed_subgraph_definition"
        : undefined),
  };
}

/** Complete-pipeline diagnostics retained for explicit audit callers only. */
export const auditVisibleH3Pipeline = inspectVisibleH3Graph;

export {
  inspectH3GraphAdmission,
  recognizeH3InferenceCanvas,
} from "./graphRecognition";

export type {
  H3InferenceAnchor,
  H3InferenceRecognition,
} from "./graphRecognition";

export { H3_NODE_TYPES, H3_SHELL_MANIFEST } from "./graphSerialization";

export type { GraphAnchor, GraphInspection } from "./graphSerialization";

export {
  isQualifiedExternalReferenceBoundary,
  isQualifiedExternalReferenceGraph,
} from "./graphReferenceQualification";
