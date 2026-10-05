import { sha256Text } from "../contracts/canonicalFingerprint";

type JsonRecord = Record<string, unknown>;

export type OwnedGraphReference = Readonly<{
  nodeIds: readonly string[];
  linkIds: readonly string[];
  anchorNodeId: string;
  authoredWidgetNodeIds: readonly string[];
}>;

export type OwnedGraphObservation = Readonly<{
  nodeIds: readonly string[];
  linkIds: readonly string[];
  anchorNodeId: string;
  fingerprint: string;
}>;

function record(value: unknown): JsonRecord | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : undefined;
}

function identifier(value: unknown): string | undefined {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isSafeInteger(value))
    return String(value);
  return undefined;
}

function normalizedIds(values: readonly string[]): readonly string[] {
  return Object.freeze(
    [...new Set(values)].sort((left, right) => left.localeCompare(right)),
  );
}

function nodeRows(value: unknown): JsonRecord[] {
  const nodes = record(value)?.nodes;
  return Array.isArray(nodes)
    ? nodes.map(record).filter((node): node is JsonRecord => node !== undefined)
    : [];
}

function linkRows(value: unknown): unknown[][] {
  const links = record(value)?.links;
  return Array.isArray(links)
    ? links.filter((link): link is unknown[] => Array.isArray(link))
    : [];
}

function slotName(
  node: JsonRecord | undefined,
  direction: "inputs" | "outputs",
  index: unknown,
): string | null {
  const slots = Array.isArray(node?.[direction]) ? node[direction] : [];
  if (typeof index !== "number" || !Number.isSafeInteger(index) || index < 0)
    return null;
  const name = record(slots[index])?.name;
  return typeof name === "string" && name.length > 0 ? name : null;
}

/**
 * Project one repository-owned node.
 *
 * M23-37 (D13, observed on ComfyUI frontend 1.51.9): after the one candidate
 * write the host re-serializes owned nodes from their node definitions --
 * optional inputs the splice never carried are appended unlinked, link inputs
 * are ordered before widget inputs, empty `widgets_values` arrays disappear and
 * `widgets_values_named` appears. Slot arrays are therefore host presentation.
 * The projection keeps only what the repository wrote: type, mode, authored
 * widget values, and the owned links (projected by slot name below).
 */
// CRITICAL (M23-37 F-8, D12): the owned projection must never include
// positional slot indices, unlinked or host-appended optional inputs, input
// order, or the presence of an empty `widgets_values` array. The host rewrites
// all of these from its node definitions on every load, so hashing them turns
// every successful write into a `stale_graph` refusal. Project slot names,
// authored widget values, node type/mode and the anchor prompt binding only.
function projectedOwnedNode(
  node: JsonRecord | undefined,
  id: string,
  authoredWidgetNodes: ReadonlySet<string>,
): JsonRecord {
  const widgets = Array.isArray(node?.widgets_values)
    ? node.widgets_values
    : [];
  return {
    id,
    type: typeof node?.type === "string" ? node.type : null,
    mode: typeof node?.mode === "number" ? node.mode : null,
    authored_widgets:
      authoredWidgetNodes.has(id) && widgets.length > 0 ? widgets : null,
  };
}

/**
 * Observe only graph state this repository wrote.
 *
 * IMPORTANT: layout, metadata, wrapper fields, foreign widgets, host-normalized
 * slot arrays and every non-owned link stay outside this projection. The
 * returned object contains identifiers and a digest only; prompt/widget values
 * never leave the hash input.
 */
export function observeOwnedGraph(
  value: unknown,
  reference: OwnedGraphReference,
): OwnedGraphObservation {
  const nodeIds = normalizedIds(reference.nodeIds);
  const linkIds = normalizedIds(reference.linkIds);
  const authoredWidgetNodes = new Set(
    normalizedIds(reference.authoredWidgetNodeIds),
  );
  const nodesById = new Map<string, JsonRecord>();
  for (const node of nodeRows(value)) {
    const id = identifier(node.id);
    if (id !== undefined && !nodesById.has(id)) nodesById.set(id, node);
  }
  const linksById = new Map<string, unknown[]>();
  for (const link of linkRows(value)) {
    const id = identifier(link[0]);
    if (id !== undefined && !linksById.has(id)) linksById.set(id, link);
  }
  const anchor = nodesById.get(reference.anchorNodeId);
  const promptInput = (Array.isArray(anchor?.inputs) ? anchor.inputs : [])
    .map(record)
    .find((input) => input?.name === "prompt");
  const promptLink = identifier(promptInput?.link);
  const ownedLinks = new Set(linkIds);
  const projection = {
    schema: "h3.context.owned_graph_projection.v2",
    nodes: nodeIds.map((id) =>
      projectedOwnedNode(nodesById.get(id), id, authoredWidgetNodes),
    ),
    // Owned links are the wiring this repository wrote: origin node and its
    // output name, target node and its input name, link type. Numeric slot
    // indices are host presentation and are resolved to names from the same
    // snapshot; an unresolvable slot projects as null.
    links: linkIds.map((id) => {
      const link = linksById.get(id);
      if (link === undefined) return [id, null, null, null, null, null];
      const origin = identifier(link[1]) ?? null;
      const target = identifier(link[3]) ?? null;
      return [
        id,
        origin,
        origin === null
          ? null
          : slotName(nodesById.get(origin), "outputs", link[2]),
        target,
        target === null
          ? null
          : slotName(nodesById.get(target), "inputs", link[4]),
        typeof link[5] === "string" ? link[5] : null,
      ];
    }),
    anchor_prompt_binding: {
      node_id: reference.anchorNodeId,
      link_id:
        promptLink !== undefined && ownedLinks.has(promptLink)
          ? promptLink
          : null,
    },
  };
  const serialized = JSON.stringify(projection);
  return Object.freeze({
    nodeIds,
    linkIds,
    anchorNodeId: reference.anchorNodeId,
    fingerprint: sha256Text(serialized),
  });
}
