export {
  configureDefinitionsCandidateWithLiveStorePublication,
  createHostWorkflowBehaviourFixture,
  definitionIds,
  HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS,
  misalignedDetachedEnvelope,
  normalizeOwnedSlotsOnWrite,
  placeholderDetachedEnvelope,
  subgraphInstanceRegistry,
} from "../support/hostBehaviourCatalogue";

export type {
  HostWorkflowBehaviourFixture,
  HostWorkflowBehaviourVariantId,
} from "../support/hostBehaviourCatalogue";

export type HostBehaviourCatalogueEntry = Readonly<{
  id:
    | "workflow.empty_store_bootstrap"
    | "workflow.null_capture_creates_temporary"
    | "queue.envelope_metadata"
    | "callbacks.foreign_graph_metadata"
    | "widgets.seed_rerandomization";
  observedFrontend: "1.48.7" | "1.51.9";
  effect:
    | "activeWorkflow=null and openWorkflows=[] before first attach"
    | "loadGraphData null capture creates a temporary workflow"
    | "queue handlers may add properties and extra metadata"
    | "foreign callbacks may write graph metadata and move nodes"
    | "host callbacks may rerandomize seed widgets";
}>;

const entry = <T extends HostBehaviourCatalogueEntry>(value: T): T =>
  Object.freeze(value);

/**
 * Content-free D13 fixture. This is observation metadata, not a supported-host
 * claim and not authority to make the hermetic double quieter than a real host.
 */
export const HOST_BEHAVIOUR_CATALOGUE = Object.freeze([
  entry({
    id: "workflow.empty_store_bootstrap",
    observedFrontend: "1.48.7",
    effect: "activeWorkflow=null and openWorkflows=[] before first attach",
  }),
  entry({
    id: "workflow.null_capture_creates_temporary",
    observedFrontend: "1.51.9",
    effect: "loadGraphData null capture creates a temporary workflow",
  }),
  entry({
    id: "queue.envelope_metadata",
    observedFrontend: "1.51.9",
    effect: "queue handlers may add properties and extra metadata",
  }),
  entry({
    id: "callbacks.foreign_graph_metadata",
    observedFrontend: "1.51.9",
    effect: "foreign callbacks may write graph metadata and move nodes",
  }),
  entry({
    id: "widgets.seed_rerandomization",
    observedFrontend: "1.51.9",
    effect: "host callbacks may rerandomize seed widgets",
  }),
] as const satisfies readonly HostBehaviourCatalogueEntry[]);

export function assertHostBehaviourCatalogue(
  catalogue: readonly HostBehaviourCatalogueEntry[],
): void {
  const ids = new Set<string>();
  for (const observed of catalogue) {
    if (ids.has(observed.id))
      throw new Error(`duplicate host behaviour id: ${observed.id}`);
    ids.add(observed.id);
    if (!/^[a-z]+(?:[._][a-z]+)+$/.test(observed.id))
      throw new Error(`invalid host behaviour id: ${observed.id}`);
    if (!/^\d+\.\d+\.\d+$/.test(observed.observedFrontend))
      throw new Error(
        `invalid observed frontend: ${observed.observedFrontend}`,
      );
    if (observed.effect.trim() === "")
      throw new Error(`empty host behaviour effect: ${observed.id}`);
  }
}
