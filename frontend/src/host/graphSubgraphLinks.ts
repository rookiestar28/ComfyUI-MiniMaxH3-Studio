/**
 * Link validation inside a subgraph definition.
 *
 * Separate from the node contracts above because a subgraph's internal wiring is the one place a
 * graph can be self-consistent node by node and still be wrong as a whole.
 */

import {
  definitionEdges,
  isBoundaryPortNode,
  isLinkId,
  nodeIdentifier,
  serializedIdentifier,
} from "./graphSerialization";
import type { SerializedEdge } from "./graphSerialization";
import {
  isAllowedSerializedPortName,
  serializedPortSlot,
  serializedPortType,
  serializedPorts,
} from "./graphPortShapes";
import { validateSerializedNamedPorts } from "./graphNodeContracts";

export function validateSubgraphDefinitionLinks(
  definition: Record<string, unknown>,
  nodes: Record<string, unknown>[],
  strictNodeContract = true,
): boolean {
  const inputNode = definition.inputNode;
  const outputNode = definition.outputNode;
  const edges = definitionEdges(definition.links);
  if (
    !isBoundaryPortNode(inputNode, -10) ||
    !isBoundaryPortNode(outputNode, -20) ||
    edges === undefined
  )
    return false;
  const expectedTaskMode = nodes.some(
    (node) => node.type === "MiniMaxH3ReferenceToVideo",
  )
    ? "ref2va"
    : nodes.some((node) => node.type === "MiniMaxH3ImageToVideo")
      ? "t2va"
      : undefined;
  if (
    validateSerializedNamedPorts(
      nodes,
      strictNodeContract,
      expectedTaskMode,
    ) !== undefined
  )
    return false;
  const inputId = "-10";
  const outputId = "-20";
  const byId = new Map<string, Record<string, unknown>>();
  for (const node of nodes) {
    const id = nodeIdentifier(node.id, "subgraph node");
    if (id === inputId || id === outputId || byId.has(id)) return false;
    byId.set(id, node);
  }
  const knownIds = new Set([inputId, outputId, ...byId.keys()]);
  const byLinkId = new Map<number, SerializedEdge>();
  for (const edge of edges) {
    const edgeId = edge.id;
    if (
      !isLinkId(edgeId) ||
      byLinkId.has(edgeId) ||
      !knownIds.has(edge.originId) ||
      !knownIds.has(edge.targetId)
    )
      return false;
    byLinkId.set(edgeId, edge);
  }

  const nodeOutputBackref = (
    node: Record<string, unknown>,
    linkId: number,
    expectedSlot: number,
  ): boolean => {
    const outputs = serializedPorts(node.outputs);
    if (outputs === undefined) return false;
    return outputs.some(({ slot, value: output }) => {
      const links = output.links;
      return (
        Array.isArray(links) &&
        links.every(isLinkId) &&
        new Set(links).size === links.length &&
        links.includes(linkId) &&
        serializedPortSlot(node, output, slot) === expectedSlot
      );
    });
  };
  const nodeInputBackref = (
    node: Record<string, unknown>,
    linkId: number,
    expectedSlot: number,
  ): boolean => {
    const inputs = serializedPorts(node.inputs);
    if (inputs === undefined) return false;
    return inputs.some(
      ({ slot, value: input }) =>
        input.link === linkId &&
        // The dynamic Reference node uses pinned ComfyUI slot IDs rather than
        // the array index used by its serialized input list.
        isLinkId(linkId) &&
        serializedPortSlot(node, input, slot) === expectedSlot,
    );
  };

  for (const edge of edges) {
    if (!isLinkId(edge.id)) return false;
    const edgeId = edge.id;
    const origin = byId.get(edge.originId);
    if (
      origin !== undefined &&
      !nodeOutputBackref(origin, edgeId, edge.originSlot)
    )
      return false;
    if (origin === undefined && edge.originId !== inputId) return false;
    const target = byId.get(edge.targetId);
    if (
      target !== undefined &&
      !nodeInputBackref(target, edgeId, edge.targetSlot)
    )
      return false;
    if (target === undefined && edge.targetId !== outputId) return false;
  }

  for (const node of nodes) {
    const inputs =
      node.inputs === undefined ? [] : serializedPorts(node.inputs);
    if (inputs === undefined) return false;
    for (const { slot, value } of inputs) {
      if (
        typeof value.name === "string" &&
        !isAllowedSerializedPortName(node.type, value.name, "input")
      )
        return false;
      const expectedInputType =
        typeof value.name === "string"
          ? serializedPortType(node.type, value.name, "input")
          : undefined;
      if (
        expectedInputType !== undefined &&
        value.type !== undefined &&
        value.type !== expectedInputType
      )
        return false;
      const link = value.link;
      if (link === null || link === undefined) continue;
      if (!isLinkId(link)) return false;
      const edge = byLinkId.get(link);
      if (
        edge === undefined ||
        edge.targetId !== String(node.id) ||
        edge.targetSlot !== serializedPortSlot(node, value, slot)
      )
        return false;
    }
    const outputs =
      node.outputs === undefined ? [] : serializedPorts(node.outputs);
    if (outputs === undefined) return false;
    for (const { slot, value } of outputs) {
      if (
        typeof value.name === "string" &&
        !isAllowedSerializedPortName(node.type, value.name, "output")
      )
        return false;
      const links = value.links;
      if (links === null || links === undefined) continue;
      if (
        !Array.isArray(links) ||
        !links.every(isLinkId) ||
        new Set(links).size !== links.length
      )
        return false;
      for (const link of links) {
        const edge = byLinkId.get(link);
        if (
          edge === undefined ||
          edge.originId !== String(node.id) ||
          edge.originSlot !== serializedPortSlot(node, value, slot)
        )
          return false;
      }
    }
  }

  const validateInterface = (
    value: unknown,
    boundaryId: string,
    direction: "input" | "output",
  ): boolean => {
    const allowedInterfaceKeys = new Set([
      "id",
      "name",
      "type",
      "linkIds",
      "localized_name",
      "label",
      "pos",
      "color_off",
      "color_on",
      "dir",
      "shape",
    ]);
    if (!Array.isArray(value)) return false;
    for (const [slot, item] of value.entries()) {
      if (item === null || typeof item !== "object" || Array.isArray(item))
        return false;
      const port = item as Record<string, unknown>;
      if (Object.keys(port).some((key) => !allowedInterfaceKeys.has(key)))
        return false;
      if (typeof port.name !== "string" || typeof port.type !== "string")
        return false;
      if (
        port.id !== undefined &&
        (typeof port.id !== "string" || !serializedIdentifier.test(port.id))
      )
        return false;
      if (
        port.localized_name !== undefined &&
        typeof port.localized_name !== "string"
      )
        return false;
      if (port.label !== undefined && typeof port.label !== "string")
        return false;
      if (
        (port.color_off !== undefined && typeof port.color_off !== "string") ||
        (port.color_on !== undefined && typeof port.color_on !== "string") ||
        (port.dir !== undefined && !Number.isSafeInteger(port.dir)) ||
        (port.shape !== undefined && !Number.isSafeInteger(port.shape))
      )
        return false;
      if (
        port.pos !== undefined &&
        (!Array.isArray(port.pos) ||
          port.pos.length !== 2 ||
          port.pos.some(
            (coordinate) =>
              typeof coordinate !== "number" || !Number.isFinite(coordinate),
          ))
      )
        return false;
      const linkIds = port.linkIds;
      if (
        !Array.isArray(linkIds) ||
        !linkIds.every(isLinkId) ||
        new Set(linkIds).size !== linkIds.length
      )
        return false;
      for (const linkId of linkIds) {
        const edge = byLinkId.get(linkId);
        if (edge === undefined) return false;
        if (
          direction === "input"
            ? edge.originId !== boundaryId || edge.originSlot !== slot
            : edge.targetId !== boundaryId || edge.targetSlot !== slot
        )
          return false;
      }
    }
    return true;
  };
  if (
    !validateInterface(definition.inputs, inputId, "input") ||
    !validateInterface(definition.outputs, outputId, "output")
  )
    return false;

  for (const edge of edges) {
    const inputPort =
      edge.originId === inputId
        ? (definition.inputs as unknown[])[edge.originSlot]
        : undefined;
    if (edge.originId === inputId) {
      const links =
        inputPort !== null &&
        typeof inputPort === "object" &&
        !Array.isArray(inputPort)
          ? (inputPort as Record<string, unknown>).linkIds
          : undefined;
      if (!Array.isArray(links) || !links.includes(edge.id)) return false;
    }
    const outputPort =
      edge.targetId === outputId
        ? (definition.outputs as unknown[])[edge.targetSlot]
        : undefined;
    if (edge.targetId === outputId) {
      const links =
        outputPort !== null &&
        typeof outputPort === "object" &&
        !Array.isArray(outputPort)
          ? (outputPort as Record<string, unknown>).linkIds
          : undefined;
      if (!Array.isArray(links) || !links.includes(edge.id)) return false;
    }
  }
  return true;
}
