import {
  H3_NODE_TYPES,
  maxDepth,
  maxIdentifierLength,
  maxNodes,
  nodeIdentifier,
  shellFieldId,
  shellNodeId,
} from "./graphSerialization";
import type { GraphAnchor, GraphInspection } from "./graphSerialization";

export type H3InferenceAnchor = Readonly<{
  executionId: string;
  nodeId:
    | typeof H3_NODE_TYPES.imageGeneration
    | typeof H3_NODE_TYPES.referenceGeneration;
}>;

export type H3InferenceRecognition = Readonly<{
  status: "recognized" | "missing";
  anchors: readonly H3InferenceAnchor[];
  productShellAnchors: readonly GraphAnchor[];
  /** Top-level node count; nested nodes are bounded separately. */
  nodeCount: number;
  reason?: "missing_native_h3_core";
}>;

function serializedObject(
  value: unknown,
  label: string,
): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${label} must be serialized object data`);
  return value as Record<string, unknown>;
}

function ownData(
  owner: Record<string, unknown>,
  key: string,
  label: string,
  required = false,
): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(owner, key);
  if (descriptor === undefined) {
    if (required) throw new Error(`${label} is missing`);
    return undefined;
  }
  // CRITICAL: recognition reads only public data descriptors. Accessors from
  // co-installed extensions must never execute merely because the Sidebar scans.
  if (descriptor.enumerable !== true || !("value" in descriptor))
    throw new Error(`${label} must be an enumerable data member`);
  return descriptor.value;
}

function serializedArrayData(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype)
    throw new Error(`${label} must be a serialized node array`);
  const lengthDescriptor = Object.getOwnPropertyDescriptor(value, "length");
  if (
    lengthDescriptor === undefined ||
    !("value" in lengthDescriptor) ||
    !Number.isSafeInteger(lengthDescriptor.value) ||
    lengthDescriptor.value < 0
  )
    throw new Error(`${label} must be a serialized node array`);
  const length = lengthDescriptor.value as number;
  if (length > maxNodes) throw new Error(`${label} exceeds the node bound`);
  if (
    Object.getOwnPropertySymbols(value).length !== 0 ||
    Object.getOwnPropertyNames(value).length !== length + 1
  )
    throw new Error(`${label} must be a serialized node array`);

  const result = new Array<unknown>(length);
  for (let index = 0; index < length; index += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
    // CRITICAL: do not call array methods or iterators here. A co-installed
    // extension can replace them and fabricate an H3 anchor during census.
    if (
      descriptor === undefined ||
      descriptor.enumerable !== true ||
      !("value" in descriptor)
    )
      throw new Error(`${label} must be a serialized node array`);
    result[index] = descriptor.value;
  }
  return result;
}

function nodeArray(value: unknown, label: string): Record<string, unknown>[] {
  const values = serializedArrayData(value, label);
  const result = new Array<Record<string, unknown>>(values.length);
  for (let index = 0; index < values.length; index += 1)
    result[index] = serializedObject(values[index], `${label}[${index}]`);
  return result;
}

function boundedNodeType(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  if (value.length === 0 || value.length > maxIdentifierLength)
    return undefined;
  return value;
}

function optionalNodeIdentifier(
  value: unknown,
  label: string,
): string | undefined {
  if (value === undefined) return undefined;
  try {
    return nodeIdentifier(value, label);
  } catch {
    return undefined;
  }
}

/**
 * Recognize only the minimum native identity that makes this an H3 inference canvas.
 *
 * This function intentionally does not inspect ports, widgets, links, model choices or
 * topology. ProductShell anchors are returned as separate repository-integration state.
 */
export function recognizeH3InferenceCanvas(
  graphValue: unknown,
): H3InferenceRecognition {
  const graph = serializedObject(graphValue, "graph");
  const rootNodes = nodeArray(
    ownData(graph, "nodes", "graph.nodes", true),
    "graph.nodes",
  );
  const definitions = new Map<string, Record<string, unknown>>();
  const definitionsValue = ownData(graph, "definitions", "graph.definitions");
  if (definitionsValue !== undefined) {
    const definitionRoot = serializedObject(
      definitionsValue,
      "graph.definitions",
    );
    const subgraphsValue = ownData(
      definitionRoot,
      "subgraphs",
      "graph.definitions.subgraphs",
    );
    if (subgraphsValue !== undefined) {
      const definitionValues = nodeArray(
        subgraphsValue,
        "graph.definitions.subgraphs",
      );
      for (let index = 0; index < definitionValues.length; index += 1) {
        const definition = definitionValues[index]!;
        const id = optionalNodeIdentifier(
          ownData(definition, "id", "subgraph definition id"),
          "subgraph definition",
        );
        // Recognition is not a pipeline validator. An unrelated malformed or
        // duplicate definition cannot invalidate an otherwise visible H3 core.
        if (id !== undefined && !definitions.has(id))
          definitions.set(id, definition);
      }
    }
  }

  const anchors: H3InferenceAnchor[] = [];
  const productShellAnchors: GraphAnchor[] = [];
  let visitedNodes = 0;
  const activeDefinitions = new Set<string>();
  const visit = (
    nodes: Record<string, unknown>[],
    prefix: string,
    depth: number,
  ): void => {
    if (depth > maxDepth) return;
    for (let index = 0; index < nodes.length; index += 1) {
      const node = nodes[index]!;
      visitedNodes += 1;
      if (visitedNodes > maxNodes)
        throw new Error("serialized graph exceeds node bound");
      const nodeType = boundedNodeType(ownData(node, "type", "node.type"));
      const native =
        nodeType === H3_NODE_TYPES.imageGeneration ||
        nodeType === H3_NODE_TYPES.referenceGeneration;
      const productShell = nodeType === shellNodeId;
      const explicitSubgraph = optionalNodeIdentifier(
        ownData(node, "subgraph_id", "node.subgraph_id"),
        "node.subgraph_id",
      );
      const subgraphId =
        explicitSubgraph ??
        (nodeType !== undefined && definitions.has(nodeType)
          ? nodeType
          : undefined);
      if (!native && !productShell && subgraphId === undefined) continue;

      const id = optionalNodeIdentifier(ownData(node, "id", "node.id"), "node");
      if (id === undefined) continue;
      const executionId = prefix.length === 0 ? id : `${prefix}:${id}`;
      if (executionId.length > maxIdentifierLength)
        throw new Error("composed execution ID exceeds its bound");

      if (native)
        anchors.push({
          executionId,
          nodeId: nodeType,
        });
      if (productShell)
        productShellAnchors.push({ executionId, nodeId: shellNodeId });

      if (subgraphId === undefined || activeDefinitions.has(subgraphId))
        continue;
      const definition = definitions.get(subgraphId);
      if (definition === undefined) continue;
      const nestedValue = ownData(definition, "nodes", "subgraph.nodes");
      if (nestedValue === undefined) continue;
      activeDefinitions.add(subgraphId);
      visit(nodeArray(nestedValue, "subgraph.nodes"), executionId, depth + 1);
      activeDefinitions.delete(subgraphId);
    }
  };
  visit(rootNodes, "", 0);

  if (anchors.length === 0)
    return {
      status: "missing",
      anchors,
      productShellAnchors,
      nodeCount: rootNodes.length,
      reason: "missing_native_h3_core",
    };
  return {
    status: "recognized",
    anchors,
    productShellAnchors,
    nodeCount: rootNodes.length,
  };
}

function hasProductShellAuthority(manifestValue: unknown): boolean {
  const manifest = serializedObject(manifestValue, "manifest");
  const fields = ownData(manifest, "fields", "manifest.fields");
  if (!Array.isArray(fields)) return false;
  const values = serializedArrayData(fields, "manifest.fields");
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    const field = serializedObject(value, `manifest.fields[${index}]`);
    if (
      ownData(field, "field_id", "manifest field.field_id") === shellFieldId &&
      ownData(field, "node_id", "manifest field.node_id") === shellNodeId &&
      ownData(field, "port_name", "manifest field.port_name") ===
        "product_shell"
    )
      return true;
  }
  return false;
}

/**
 * Compose minimum H3 recognition with the ProductShell seam App Mode needs to
 * correlate one verified Context result. No pipeline correctness is inferred.
 */
export function inspectH3GraphAdmission(
  graphValue: unknown,
  manifestValue: unknown,
): GraphInspection {
  const recognition = recognizeH3InferenceCanvas(graphValue);
  if (!hasProductShellAuthority(manifestValue))
    return {
      status: "incompatible",
      anchors: [],
      nodeCount: recognition.nodeCount,
      reason: "missing_shell_authority",
    };
  // CRITICAL: copy by bounded index. Array.prototype iteration is shared with
  // co-installed extensions and cannot be trusted at this recognition seam.
  const anchors = new Array<GraphAnchor>(
    recognition.productShellAnchors.length,
  );
  for (let index = 0; index < anchors.length; index += 1)
    anchors[index] = recognition.productShellAnchors[index]!;
  if (recognition.status === "missing")
    return {
      status: "missing",
      anchors,
      nodeCount: recognition.nodeCount,
      reason: recognition.reason,
    };
  if (anchors.length === 0)
    return {
      status: "missing",
      anchors,
      nodeCount: recognition.nodeCount,
      reason: "missing_product_shell",
    };
  if (anchors.length > 1)
    return {
      status: "ambiguous",
      anchors,
      nodeCount: recognition.nodeCount,
      reason: "ambiguous_product_shell",
    };
  return {
    status: "ready",
    anchors,
    nodeCount: recognition.nodeCount,
    existingGraphCompatible: true,
  };
}
