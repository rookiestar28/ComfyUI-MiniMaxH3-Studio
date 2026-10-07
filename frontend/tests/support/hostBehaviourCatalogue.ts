/**
 * Host behaviour catalogue (D13, `tests/E2E_TESTING_SOP.md` section 2).
 *
 * Every entry is a seam behaviour observed on a supplied host, implemented so the
 * hermetic double carries it. The catalogue is a fixture: it changes only when a
 * supported frontend is observed to behave differently, and each entry names the
 * observation it came from. Item M23-26 owns its growth; M23-37 opened it.
 *
 * Observed on ComfyUI 0.34.0 / frontend 1.51.9 (2026-08-28):
 *
 * 1. Subgraph instance types resolve only after the host's own `loadGraphData`
 *    has run for a workflow carrying their definitions, and only that load
 *    reconciles an instance node's compact serialized inputs (linked inputs
 *    only) against the definition by name. `LGraph.configure()` creates the
 *    instance through the global node-type registry
 *    (`LiteGraph.registered_node_types`): a detached `new LGraph(candidate)`
 *    before registration holds a placeholder node whose compiled output
 *    carries no `class_type` and no flattened inner nodes; after registration
 *    it holds a resolved instance whose serialized slot indices are applied to
 *    the expanded input list, so the candidate's links land on the wrong
 *    inputs (the anchor's `prompt` stays the template literal). The root
 *    compile of the loaded canvas is correct in both cases.
 * 2. After the one candidate write the host re-serializes repository-owned
 *    nodes from their node definitions: optional inputs the splice never
 *    carried are appended unlinked, link inputs are ordered before widget
 *    inputs (renumbering link target slots), empty `widgets_values` arrays
 *    disappear and `widgets_values_named` appears.
 *
 * Observed on the supplied pinned host (2026-08-30):
 *
 * 3. Configuring a definitions-carrying `LGraph` as a purported detached
 *    candidate publishes its graph-store-backed widget state to the live
 *    canvas. Constructing empty and calling `configure()` has the same effect.
 *
 * Observed on the supplied pinned host, frontend 1.51.9 (2026-09-14, M16-05):
 *
 * 4. Entry 3 is not limited to subgraph definitions. The frontend's widget value
 *    store keys widget state by graph id, node id and widget name, and
 *    registering a widget whose key already exists returns the existing state.
 *    A detached `LGraph` configured from a candidate that carries the visible
 *    workflow's own root `id` therefore binds to the visible nodes' widget
 *    states and writes the candidate's widget values into them, with no
 *    definitions involved.
 */

type Json = Record<string, any>;

export const HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS = Object.freeze([
  "null_empty_exact",
  "existing_active",
  "foreign_open_inconsistent",
  "identity_drift",
  "count_drift",
] as const);

export type HostWorkflowBehaviourVariantId =
  (typeof HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS)[number];

export type HostWorkflowBehaviourFixture = Readonly<{
  id: HostWorkflowBehaviourVariantId;
  workflowStore: {
    activeWorkflow: object | null;
    openWorkflows: object[];
    createNewTemporary(path?: string, initialState?: unknown): object;
    openWorkflow(workflow: object): Promise<object>;
  };
  authority: object;
  createdWorkflow: object;
  openedAuthority: object;
  foreignWorkflow: object;
  trace: readonly string[];
}>;

/**
 * Executable workflow-store rows for the supported-host lifecycle catalogue.
 * The caller selects one row before the hermetic host binds its public seams.
 */
export function createHostWorkflowBehaviourFixture(
  id: HostWorkflowBehaviourVariantId,
): HostWorkflowBehaviourFixture {
  const authority = Object.freeze({ path: "workflows/catalogue-active.json" });
  const createdWorkflow = Object.freeze({
    path: "workflows/m25-08-authorized-video-acceptance.json",
  });
  const openedAuthority = Object.freeze({
    path: "workflows/m25-08-authorized-video-acceptance.json",
  });
  const foreignWorkflow = Object.freeze({
    path: "workflows/catalogue-foreign.json",
  });
  const trace: string[] = [];
  const workflowStore = {
    activeWorkflow: (id === "existing_active" ? authority : null) as
      object | null,
    openWorkflows:
      id === "existing_active"
        ? [authority]
        : id === "foreign_open_inconsistent"
          ? [foreignWorkflow]
          : ([] as object[]),
    createNewTemporary(path?: string, initialState?: unknown): object {
      if (path !== "m25-08-authorized-video-acceptance.json")
        throw new Error("catalogue temporary workflow name changed");
      if (
        initialState === null ||
        typeof initialState !== "object" ||
        Array.isArray(initialState)
      )
        throw new Error("catalogue temporary workflow initial state changed");
      trace.push("createNewTemporary");
      return createdWorkflow;
    },
    async openWorkflow(workflow: object): Promise<object> {
      if (workflow !== createdWorkflow)
        throw new Error("catalogue temporary workflow identity changed");
      trace.push("openWorkflow");
      const nextActive =
        id === "identity_drift" ? foreignWorkflow : openedAuthority;
      workflowStore.activeWorkflow = nextActive;
      trace.push("activeWorkflow");
      workflowStore.openWorkflows.push(openedAuthority);
      if (id === "count_drift")
        workflowStore.openWorkflows.push(foreignWorkflow);
      trace.push("openMembership");
      return workflow;
    },
  };
  return Object.freeze({
    id,
    workflowStore,
    authority,
    createdWorkflow,
    openedAuthority,
    foreignWorkflow,
    trace,
  });
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

export function definitionIds(workflow: unknown): string[] {
  const definitions = (workflow as Json | undefined)?.definitions?.subgraphs;
  return Array.isArray(definitions)
    ? definitions
        .map((definition: Json) => definition?.id)
        .filter((id: unknown): id is string => typeof id === "string")
    : [];
}

/**
 * Catalogue entry 3. Model the host's shared graph-store publication when a
 * definitions-carrying candidate is configured through the live LGraph class.
 */
export function configureDefinitionsCandidateWithLiveStorePublication<T>(
  candidate: T,
  publishLive: (configured: T) => void,
): T {
  const configured = clone(candidate);
  if (definitionIds(configured).length > 0) publishLive(clone(configured));
  return configured;
}

/**
 * Catalogue entry 1. A public node-type registry that gains a subgraph instance
 * type only when a root load carries its definition.
 */
export function subgraphInstanceRegistry(
  initial: Record<string, unknown> = {},
): Readonly<{
  registry: Record<string, unknown>;
  registerFromLoad(workflow: unknown): void;
  isRegistered(id: string): boolean;
}> {
  const registry: Record<string, unknown> = { ...initial };
  return Object.freeze({
    registry,
    registerFromLoad(workflow: unknown) {
      for (const id of definitionIds(workflow))
        registry[id] = { nodeData: { name: id, subgraph: true } };
    },
    isRegistered: (id: string) =>
      Object.prototype.hasOwnProperty.call(registry, id),
  });
}

/**
 * Catalogue entry 1, compiled side. What the host's `graphToPrompt` returns for a
 * detached graph whose subgraph instance type is unregistered: the instance is a
 * placeholder without `class_type`, and none of its inner nodes appear.
 */
export function placeholderDetachedEnvelope(candidate: unknown): {
  output: Record<string, unknown>;
  workflow: unknown;
} {
  const nodes = Array.isArray((candidate as Json | undefined)?.nodes)
    ? ((candidate as Json).nodes as Json[])
    : [];
  const ids = new Set(definitionIds(candidate));
  const output: Record<string, unknown> = {};
  for (const node of nodes) {
    if (typeof node?.type !== "string") continue;
    if (ids.has(node.type)) {
      output[String(node.id)] = {
        inputs: { UNKNOWN: "", UNKNOWN_1: 0, prompt: ["0", 0] },
        _meta: {},
      };
    } else if (
      node.type === "SaveVideo" ||
      node.type === "ResolutionSelector"
    ) {
      output[String(node.id)] = {
        class_type: node.type,
        inputs: {},
        _meta: { title: node.type },
      };
    }
  }
  return { output, workflow: clone(candidate) };
}

/**
 * Catalogue entry 1, compiled side after registration. What the host's
 * `graphToPrompt` returns for a detached construction of a definitions-carrying
 * candidate once the instance type is registered: every inner node resolves,
 * but the serialized slot indices were applied to the expanded input list, so
 * the anchor's `prompt` is the template's literal text instead of the shell
 * binding. `qualified` is the correctly reconciled compile of the same
 * candidate.
 */
export function misalignedDetachedEnvelope(
  candidate: unknown,
  qualified: Record<string, unknown>,
): { output: Record<string, unknown>; workflow: unknown } {
  const output = clone(qualified) as Record<string, Json>;
  for (const node of Object.values(output)) {
    if (
      node?.class_type === "MiniMaxH3ImageToVideo" ||
      node?.class_type === "MiniMaxH3ReferenceToVideo"
    ) {
      node.inputs = {
        ...(node.inputs as Json),
        prompt: "template literal prompt, not the shell binding",
      };
    }
  }
  return { output, workflow: clone(candidate) };
}

/**
 * Catalogue entry 2. The host's re-serialization of repository-owned nodes after
 * the one canvas write.
 */
export function normalizeOwnedSlotsOnWrite<T>(workflow: T): T {
  const written = clone(workflow) as Json;
  const nodes = Array.isArray(written?.nodes) ? (written.nodes as Json[]) : [];
  for (const node of nodes) {
    if (!String(node?.type ?? "").startsWith("comfyui_h3_context.")) continue;
    const inputs = Array.isArray(node.inputs) ? (node.inputs as Json[]) : [];
    const linked = inputs.filter((input) => !input?.widget);
    const widgets = inputs.filter((input) => input?.widget);
    node.inputs = [
      ...linked,
      ...widgets,
      { name: "host_optional_a", type: "H3_HOST_OPTIONAL", link: null },
      { name: "host_optional_b", type: "H3_HOST_OPTIONAL", link: null },
    ];
    if (Array.isArray(node.widgets_values) && node.widgets_values.length === 0)
      delete node.widgets_values;
    if (Array.isArray(node.widgets_values) && node.widgets_values.length > 0)
      node.widgets_values_named = Object.fromEntries(
        node.widgets_values.map((value: unknown, index: number) => [
          `widget_${index}`,
          value,
        ]),
      );
    const outputs = Array.isArray(node.outputs) ? (node.outputs as Json[]) : [];
    node.outputs = [...outputs, { name: "host_optional_out", links: [] }];
  }
  // Link target slots move with the reordered inputs.
  const links = Array.isArray(written?.links)
    ? (written.links as unknown[][])
    : [];
  for (const link of links) {
    const target = nodes.find((node) => String(node?.id) === String(link[3]));
    if (target === undefined) continue;
    const index = (target.inputs as Json[]).findIndex(
      (input) => String(input?.link) === String(link[0]),
    );
    if (index >= 0) link[4] = index;
  }
  return written as T;
}

/**
 * Catalogue entry 4. Model the widget value store publication when a candidate carrying the live
 * workflow's root graph id is configured as a detached graph.
 */
export function configureCandidateWithGraphIdWidgetStorePublication<T>(
  candidate: T,
  liveGraphId: string,
  publishLive: (configured: T) => void,
): T {
  const configured = clone(candidate);
  if ((configured as Json | undefined)?.id === liveGraphId)
    publishLive(clone(configured));
  return configured;
}
