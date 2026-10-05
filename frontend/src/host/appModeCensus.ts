// App Mode census: read-only observation of the visible canvas and of a compiled prompt
// against it. No host call, no write (M23-28 split).

import { H3_NODE_TYPES } from "./graphAdapter";
import { rebindExistingContextAuthoring } from "./templateMaterialization";
import {
  type AppModeCompiledPrompt,
  type AppModeImageSource,
  type AppModeMediaKind,
  type AppModeMediaSource,
  compiledOutputNodes,
  type ExistingCompiledAdmission,
  graphNodes,
  imageGenerationNodeType,
  isLink,
  isNodeIdentifier,
  loadAudioNodeType,
  loadImageNodeType,
  loadVideoNodeType,
  productShellNodeType,
  qualifiedBindingId,
  qualifiedGraphId,
  record,
  referenceGenerationNodeType,
  requestNodeType,
  serializedIdentifier,
} from "./appModeContract";

/** Return content-free identities for visible top-level host-owned image nodes. */
export function listAppModeImageSources(
  graphValue: unknown,
): AppModeImageSource[] {
  const sources: AppModeImageSource[] = [];
  const seen = new Set<string>();
  for (const value of graphNodes(graphValue)) {
    const node = record(value);
    const id = node?.id;
    if (
      node?.type !== loadImageNodeType ||
      !isNodeIdentifier(id) ||
      seen.has(String(id))
    )
      continue;
    const nodeId = String(id);
    seen.add(nodeId);
    sources.push({ node_id: nodeId, label: `Image node ${nodeId}` });
  }
  return sources.sort((left, right) =>
    left.node_id.localeCompare(right.node_id),
  );
}

/** Enumerate content-free identities only; loader inputs remain opaque. */
export function listAppModeMediaSources(
  graphValue: unknown,
): AppModeMediaSource[] {
  const kinds = new Map<string, AppModeMediaKind>([
    [loadImageNodeType, "image"],
    [loadVideoNodeType, "video"],
    [loadAudioNodeType, "audio"],
  ]);
  const sources: AppModeMediaSource[] = [];
  const seen = new Set<string>();
  for (const value of graphNodes(graphValue)) {
    const node = record(value);
    const kind = kinds.get(String(node?.type));
    const id = node?.id;
    if (kind === undefined || !isNodeIdentifier(id)) continue;
    const nodeId = String(id);
    const key = `${kind}:${nodeId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    sources.push({
      node_id: nodeId,
      label: `${kind[0]?.toUpperCase()}${kind.slice(1)} node ${nodeId}`,
      kind,
      output_slot: 0,
    });
  }
  return sources.sort(
    (left, right) =>
      left.kind.localeCompare(right.kind) ||
      left.node_id.localeCompare(right.node_id),
  );
}

type VisibleCanonicalNode = {
  type: string;
  node: Record<string, unknown>;
};

type VisibleGraphCensus = {
  canonical: Map<string, VisibleCanonicalNode>;
  ids: Set<string>;
};

function visibleCanonicalNodes(
  value: unknown,
): Map<string, VisibleCanonicalNode> | undefined {
  return visibleGraphCensus(value)?.canonical;
}

/**
 * Walk the visible graph once and report both the canonical H3 nodes and every
 * node identity it contains, subgraph contents included.
 *
 * The second half exists because a materialized graph is mostly foreign: the
 * binding check can no longer say "the compiled prompt has exactly the canonical
 * nodes", so it says "every compiled node came from somewhere visible" instead.
 * Walking twice would risk the two answers disagreeing about a nested node.
 */
function visibleGraphCensus(value: unknown): VisibleGraphCensus | undefined {
  try {
    const graph = record(value);
    if (graph === undefined) return undefined;
    const definitionsRoot = record(graph.definitions);
    const definitions = new Map<string, Record<string, unknown>>();
    for (const definition of graphNodes(definitionsRoot?.subgraphs)) {
      const definitionRecord = record(definition);
      const id = definitionRecord?.id;
      if (definitionRecord === undefined || !isNodeIdentifier(id))
        return undefined;
      const key = String(id);
      if (definitions.has(key)) return undefined;
      definitions.set(key, definitionRecord);
    }
    const canonical = new Set<string>(Object.values(H3_NODE_TYPES));
    const result = new Map<string, VisibleCanonicalNode>();
    const ids = new Set<string>();
    const active = new Set<string>();
    const visit = (nodes: unknown[], prefix = ""): boolean => {
      for (const value of nodes) {
        const node = record(value);
        if (node === undefined || !isNodeIdentifier(node.id)) return false;
        const id = qualifiedGraphId(prefix, node.id);
        ids.add(id);
        if (typeof node.type === "string" && canonical.has(node.type)) {
          if (result.has(id)) return false;
          result.set(id, { type: node.type, node });
        }
        const identity =
          node.subgraph_id !== undefined
            ? node.subgraph_id
            : typeof node.type === "string" && definitions.has(node.type)
              ? node.type
              : undefined;
        if (identity === undefined) continue;
        if (!isNodeIdentifier(identity)) return false;
        const definitionId = String(identity);
        const definition = definitions.get(definitionId);
        if (definition === undefined || active.has(definitionId)) return false;
        active.add(definitionId);
        if (
          !visit(
            graphNodes(definition.nodes),
            qualifiedGraphId(prefix, node.id),
          )
        )
          return false;
        active.delete(definitionId);
      }
      return true;
    };
    return visit(graphNodes(graph.nodes))
      ? { canonical: result, ids }
      : undefined;
  } catch {
    return undefined;
  }
}

type SerializedBindingEdge = {
  originId: string | number;
  originSlot: number;
  targetId: string | number;
  targetSlot: number;
};

function serializedBindingEdges(
  value: unknown,
): Map<number, SerializedBindingEdge> | undefined {
  if (value === undefined) return new Map();
  if (!Array.isArray(value)) return undefined;
  const edges = new Map<number, SerializedBindingEdge>();
  for (const item of value) {
    let id: unknown;
    let originId: unknown;
    let originSlot: unknown;
    let targetId: unknown;
    let targetSlot: unknown;
    if (Array.isArray(item)) {
      [id, originId, originSlot, targetId, targetSlot] = item;
    } else {
      const edge = record(item);
      if (edge === undefined) return undefined;
      ({
        id,
        origin_id: originId,
        origin_slot: originSlot,
        target_id: targetId,
        target_slot: targetSlot,
      } = edge);
    }
    if (
      !Number.isSafeInteger(id) ||
      !(typeof originId === "string" || typeof originId === "number") ||
      !Number.isSafeInteger(originSlot) ||
      !(typeof targetId === "string" || typeof targetId === "number") ||
      !Number.isSafeInteger(targetSlot) ||
      edges.has(id as number)
    )
      return undefined;
    edges.set(id as number, {
      originId,
      originSlot: originSlot as number,
      targetId,
      targetSlot: targetSlot as number,
    });
  }
  return edges;
}

function visibleCanonicalInputBindings(
  value: unknown,
): Map<string, Map<string, [string | number, number]>> | undefined {
  try {
    const graph = record(value);
    if (graph === undefined) return undefined;
    const definitionRoot = record(graph.definitions);
    const definitions = new Map<string, Record<string, unknown>>();
    for (const definition of graphNodes(definitionRoot?.subgraphs)) {
      const item = record(definition);
      if (item === undefined || !isNodeIdentifier(item.id)) return undefined;
      const id = String(item.id);
      if (definitions.has(id)) return undefined;
      definitions.set(id, item);
    }
    const canonical = new Set<string>(Object.values(H3_NODE_TYPES));
    const result = new Map<string, Map<string, [string | number, number]>>();
    const active = new Set<string>();
    const visit = (
      nodes: unknown[],
      linksValue: unknown,
      prefix = "",
    ): boolean => {
      const edges = serializedBindingEdges(linksValue);
      if (edges === undefined) return false;
      for (const value of nodes) {
        const node = record(value);
        if (node === undefined || !isNodeIdentifier(node.id)) return false;
        const id = qualifiedGraphId(prefix, node.id);
        if (typeof node.type === "string" && canonical.has(node.type)) {
          if (result.has(id)) return false;
          const bindings = new Map<string, [string | number, number]>();
          const ports = Array.isArray(node.inputs)
            ? node.inputs
            : node.inputs === undefined
              ? []
              : [node.inputs];
          for (const portValue of ports) {
            const port = record(portValue);
            if (port === undefined || typeof port.name !== "string") continue;
            const linkId = port.link;
            if (linkId === null || linkId === undefined) continue;
            if (!Number.isSafeInteger(linkId)) return false;
            const edge = edges.get(linkId as number);
            if (
              edge === undefined ||
              qualifiedBindingId(prefix, edge.targetId) !== id ||
              bindings.has(port.name)
            )
              return false;
            bindings.set(port.name, [
              qualifiedBindingId(prefix, edge.originId),
              edge.originSlot,
            ]);
          }
          result.set(id, bindings);
        }
        const identity =
          node.subgraph_id !== undefined
            ? node.subgraph_id
            : typeof node.type === "string" && definitions.has(node.type)
              ? node.type
              : undefined;
        if (identity === undefined) continue;
        if (!isNodeIdentifier(identity)) return false;
        const definitionId = String(identity);
        const definition = definitions.get(definitionId);
        if (definition === undefined || active.has(definitionId)) return false;
        active.add(definitionId);
        if (
          !visit(
            graphNodes(definition.nodes),
            definition.links,
            qualifiedGraphId(prefix, node.id),
          )
        )
          return false;
        active.delete(definitionId);
      }
      return true;
    };
    return visit(graphNodes(graph.nodes), graph.links) ? result : undefined;
  } catch {
    return undefined;
  }
}

function visibleCanonicalNodeTypes(
  value: unknown,
): Map<string, string> | undefined {
  const nodes = visibleCanonicalNodes(value);
  if (nodes === undefined) return undefined;
  return new Map(
    [...nodes].map(([id, visible]) => [id, visible.type] as const),
  );
}

function widgetValuesMatchCompiledNode(
  visible: VisibleCanonicalNode,
  compiled: Record<string, unknown>,
): boolean {
  const widgets = visible.node.widgets_values;
  if (!Array.isArray(widgets)) return true;
  const inputs = record(compiled.inputs);
  if (inputs === undefined) return false;
  // A connected input ignores its widget, so its stored value is not what the
  // host will read. Comparing it anyway is how the adoption route came to reject
  // the graphs this build materializes: the pinned templates drive the anchor's
  // geometry and length through nodes and leave the template's own defaults
  // sitting in `widgets_values`, in an order that starts with the prompt.
  const scalarMatches = (name: string, index: number): boolean =>
    isLink(inputs[name]) || inputs[name] === widgets[index];
  if (visible.type === requestNodeType) {
    return (
      widgets.length === 3 &&
      scalarMatches("task_mode", 0) &&
      scalarMatches("user_intent", 1) &&
      scalarMatches("duration_seconds", 2)
    );
  }
  // M23-29: native anchor geometry, length and reference-size widgets belong to
  // the operator graph. Admission owns only the compiled prompt binding below.
  return true;
}

function exactCompiledLink(
  value: unknown,
  originId: string,
  originSlot = 0,
): boolean {
  return (
    isLink(value) && String(value[0]) === originId && value[1] === originSlot
  );
}

function compiledValueDependsOn(
  byId: ReadonlyMap<string, Record<string, unknown>>,
  value: unknown,
  sourceId: string,
): boolean {
  if (!isLink(value)) return false;
  const pending: unknown[] = [value];
  const visited = new Set<string>();
  while (pending.length > 0) {
    const candidate = pending.pop();
    if (!isLink(candidate)) continue;
    const nodeId = String(candidate[0]);
    if (nodeId === sourceId && candidate[1] === 0) return true;
    if (visited.has(nodeId)) continue;
    visited.add(nodeId);
    if (visited.size > byId.size) return false;
    const inputs = record(byId.get(nodeId)?.inputs);
    if (inputs === undefined) continue;
    for (const input of Object.values(inputs)) {
      if (isLink(input)) pending.push(input);
      else if (Array.isArray(input))
        for (const entry of input) if (isLink(entry)) pending.push(entry);
    }
  }
  return false;
}

export function existingCompiledProjection(
  nodes: Array<{ id: string; node: Record<string, unknown> }>,
  subject: ExistingCompiledAdmission,
):
  | Readonly<{
      request: Record<string, unknown>;
      durationSource: Record<string, unknown>;
      anchor: Record<string, unknown>;
      productShell: Record<string, unknown>;
    }>
  | undefined {
  if (
    ![
      subject.requestNodeId,
      subject.durationSourceNodeId,
      subject.visibleAnchorNodeId,
      subject.anchorNodeId,
      subject.productShellNodeId,
    ].every((id) => serializedIdentifier.test(id)) ||
    (subject.anchorNodeType !== imageGenerationNodeType &&
      subject.anchorNodeType !== referenceGenerationNodeType)
  )
    return undefined;
  const byId = new Map(nodes.map(({ id, node }) => [id, node] as const));
  const request = byId.get(subject.requestNodeId);
  const durationSource = byId.get(subject.durationSourceNodeId);
  const anchor = byId.get(subject.anchorNodeId);
  const productShell = byId.get(subject.productShellNodeId);
  const requestInputs = record(request?.inputs);
  const durationInputs = record(durationSource?.inputs);
  const anchorInputs = record(anchor?.inputs);
  if (
    request?.class_type !== requestNodeType ||
    durationSource?.class_type !== "PrimitiveFloat" ||
    anchor?.class_type !== subject.anchorNodeType ||
    productShell?.class_type !== productShellNodeType ||
    requestInputs === undefined ||
    durationInputs === undefined ||
    anchorInputs === undefined ||
    typeof requestInputs.user_intent !== "string" ||
    requestInputs.user_intent.trim().length === 0 ||
    requestInputs.user_intent.length > 4096 ||
    !exactCompiledLink(
      requestInputs.duration_seconds,
      subject.durationSourceNodeId,
    ) ||
    !compiledValueDependsOn(
      byId,
      anchorInputs.length,
      subject.durationSourceNodeId,
    )
  )
    return undefined;
  return { request, durationSource, anchor, productShell };
}

function visibleInputBinding(
  graph: Record<string, unknown>,
  node: Record<string, unknown>,
  inputName: string,
): [string, number] | undefined {
  const edges = serializedBindingEdges(graph.links);
  const inputs = Array.isArray(node.inputs) ? node.inputs : [];
  const matches = inputs
    .map((value, slot) => ({ input: record(value), slot }))
    .filter(
      (entry): entry is { input: Record<string, unknown>; slot: number } =>
        entry.input?.name === inputName,
    );
  if (edges === undefined || matches.length !== 1) return undefined;
  const match = matches[0]!;
  if (!Number.isSafeInteger(match.input.link)) return undefined;
  const edge = edges.get(match.input.link as number);
  if (
    edge === undefined ||
    String(edge.targetId) !== String(node.id) ||
    edge.targetSlot !== match.slot
  )
    return undefined;
  return [String(edge.originId), edge.originSlot];
}

function existingProjectionMatchesVisibleGraph(
  visibleGraph: unknown,
  compiled: AppModeCompiledPrompt,
  subject: ExistingCompiledAdmission,
): boolean {
  try {
    const graph = record(visibleGraph);
    if (graph === undefined) return false;
    const roots = graphNodes(graph.nodes).map(record);
    if (roots.some((node) => node === undefined)) return false;
    const exactRoot = (id: string): Record<string, unknown> | undefined => {
      const matches = roots.filter(
        (node) => node !== undefined && String(node.id) === id,
      );
      return matches.length === 1 ? matches[0] : undefined;
    };
    const request = exactRoot(subject.requestNodeId);
    const durationSource = exactRoot(subject.durationSourceNodeId);
    const visibleAnchor = exactRoot(subject.visibleAnchorNodeId);
    const productShell = exactRoot(subject.productShellNodeId);
    if (
      request?.type !== requestNodeType ||
      durationSource?.type !== "PrimitiveFloat" ||
      visibleAnchor === undefined ||
      productShell?.type !== productShellNodeType ||
      !(
        subject.anchorNodeId === subject.visibleAnchorNodeId ||
        subject.anchorNodeId.startsWith(`${subject.visibleAnchorNodeId}:`)
      )
    )
      return false;
    const durationBinding = visibleInputBinding(
      graph,
      request,
      "duration_seconds",
    );
    if (
      durationBinding?.[0] !== subject.durationSourceNodeId ||
      durationBinding[1] !== 0
    )
      return false;
    const requestWidgets = request.widgets_values;
    const visibleDuration = durationSource.widgets_values;
    if (
      !Array.isArray(requestWidgets) ||
      typeof requestWidgets[1] !== "string" ||
      !Array.isArray(visibleDuration) ||
      visibleDuration.length !== 1 ||
      typeof visibleDuration[0] !== "number"
    )
      return false;
    const rebound = rebindExistingContextAuthoring(graph, {
      // Existing task mode is user-owned; this value only satisfies the bounded
      // Sidebar request codec and is never compared with or written into it.
      taskMode: "t2va",
      userIntent: requestWidgets[1],
      durationSeconds: visibleDuration[0],
    });
    if (
      rebound.requestNodeId !== subject.requestNodeId ||
      rebound.durationSourceNodeId !== subject.durationSourceNodeId ||
      rebound.anchorNodeId !== subject.visibleAnchorNodeId ||
      rebound.compiledAnchorNodeId !== subject.anchorNodeId ||
      rebound.productShellNodeId !== subject.productShellNodeId
    )
      return false;
    const projection = existingCompiledProjection(
      compiledOutputNodes(compiled),
      subject,
    );
    if (projection === undefined) return false;
    const compiledRequestInputs = record(projection.request.inputs);
    const compiledDuration = record(projection.durationSource.inputs)?.value;
    return (
      compiledRequestInputs?.user_intent === requestWidgets[1] &&
      visibleDuration[0] === compiledDuration
    );
  } catch {
    return false;
  }
}

/**
 * `graphToPrompt` is a public seam, but its result still belongs to the visible
 * graph transaction. Require the same canonical node IDs/classes before an
 * existing graph can reach the queue; a strict foreign prompt is not a bind.
 */
export function compiledPromptMatchesVisibleGraph(
  visibleGraph: unknown,
  compiled: AppModeCompiledPrompt,
  existingSubject?: ExistingCompiledAdmission,
): boolean {
  if (existingSubject !== undefined)
    return existingProjectionMatchesVisibleGraph(
      visibleGraph,
      compiled,
      existingSubject,
    );
  const census = visibleGraphCensus(visibleGraph);
  const visible = census?.canonical;
  const visibleBindings = visibleCanonicalInputBindings(visibleGraph);
  const output = compiledOutputNodes(compiled);
  if (
    census === undefined ||
    visible === undefined ||
    visibleBindings === undefined
  )
    return false;
  const compiledTypes = new Map<string, string>();
  for (const { id, node } of output) {
    const classType = node.class_type;
    if (typeof classType !== "string" || compiledTypes.has(id)) return false;
    // M17-20 D11: the compiled prompt now contains the template's own nodes as
    // well as the H3 chain, so it can be larger than the canonical census. What
    // must still hold is that nothing was invented: every compiled node has to
    // correspond to a node the user can actually see. The host prunes visible
    // nodes it cannot execute (notes, muted nodes), so the containment runs one
    // way only.
    if (!census.ids.has(id)) return false;
    compiledTypes.set(id, classType);
  }
  if (compiledTypes.size < visible.size) return false;
  const compiledById = new Map(
    output.map(({ id, node }) => [id, node] as const),
  );
  for (const [id, visibleNode] of visible) {
    if (compiledTypes.get(id) !== visibleNode.type) return false;
    const compiledNode = compiledById.get(id);
    if (
      (visibleNode.type === requestNodeType ||
        visibleNode.type === imageGenerationNodeType ||
        visibleNode.type === referenceGenerationNodeType) &&
      !Array.isArray(visibleNode.node.widgets_values)
    )
      return false;
    if (
      compiledNode === undefined ||
      !widgetValuesMatchCompiledNode(visibleNode, compiledNode)
    )
      return false;
    const compiledInputs = record(compiledNode.inputs);
    const bindings = visibleBindings.get(id);
    if (compiledInputs === undefined || bindings === undefined) return false;
    for (const [name, [originId, originSlot]] of bindings) {
      // M23-29: the native anchor's side-branch wiring is operator-owned.
      // Admission compares only the Product Shell prompt join written by this
      // repository; geometry, length and media branches execute under ComfyUI.
      if (
        (visibleNode.type === imageGenerationNodeType ||
          visibleNode.type === referenceGenerationNodeType) &&
        name !== "prompt"
      )
        continue;
      const link = compiledInputs[name];
      // An input fed by the subgraph's own boundary has no origin this graph can
      // name: what it resolves to is whatever is connected to the instance from
      // the outside, and the definition cannot see that. The host resolves it
      // either to a scalar or to a node outside the definition, and both are
      // admitted here rather than guessed at.
      //
      // Nothing is given up by that. Which node feeds the anchor's prompt, and
      // whether the sink depends on the anchor at all, are decided on the
      // compiled prompt itself -- `isCompatibleExistingPrompt` requires the
      // prompt to come from the single Product Shell; artifact authority is
      // established later from the first bounded video event for this queue.
      if (String(originId) === "-10") continue;
      if (
        !Array.isArray(link) ||
        link.length !== 2 ||
        String(link[0]) !== String(originId) ||
        link[1] !== originSlot
      )
        return false;
    }
  }
  return true;
}
