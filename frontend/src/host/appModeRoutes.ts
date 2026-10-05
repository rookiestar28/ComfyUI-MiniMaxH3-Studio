// App Mode route resolution: the three ways a run obtains one validated candidate workflow
// and its owned reference -- rebinding the existing canvas, materializing the pinned template,
// or connecting the context pipeline into a user canvas. Nothing here calls the host or writes
// the canvas; the controller owns the transaction around these resolutions (M23-28 split).
import { classifyHostMappingReadiness } from "./hostSeamContract";
import {
  OfficialAssetResolutionError,
  readOfficialAssetInventoryFromNodeDefinitions,
  resolveOfficialAssets,
  type OfficialAssetResolution,
} from "./officialAssetResolution";
import {
  observeOwnedGraph,
  type OwnedGraphObservation,
  type OwnedGraphReference,
} from "./ownedGraphIdentity";
import {
  MODE_TEMPLATE,
  rebindExistingContextAuthoring,
  spliceContextPipeline,
  TASK_MODE_FAMILY,
  TemplateSpliceError,
  type SpliceMediaIds,
} from "./templateMaterialization";
import {
  anchorMissingReason,
  AppModeError,
  appModeArtifactPrefix,
  graphFingerprint,
  MILLISECONDS_PER_SECOND,
  record,
  snapshotGraph,
  TEMPLATE_UNAVAILABLE_REASON,
  throwIfCancelled,
  type AppModeAdmission,
  type AppModeApp,
  type AppModeCompiledPrompt,
  type AppModeDetachedGraphFactory,
  type AppModeInputs,
  type AppModeStartOptions,
  type ExistingCompiledAdmission,
} from "./appModeContract";
import {
  resolveSpliceMedia,
  type AppModeTemplateLoader,
} from "./appModeTemplateSources";
import { existingOwnedReference } from "./canvasOwnedWrite";
import { probeGraphConstructor, probeGraphToPrompt } from "./hostSeams";

export type ResolvedRouteCandidate = Readonly<{
  candidateWorkflow: unknown;
  ownedReference: OwnedGraphReference;
  expectedOwnedGraph: OwnedGraphObservation;
  expectedGraphFingerprint: string;
}>;

export type ResolvedExistingRoute = ResolvedRouteCandidate &
  Readonly<{ existingSubject: ExistingCompiledAdmission }>;

export type ResolvedMaterializeRoute = ResolvedRouteCandidate &
  Readonly<{
    materializedMedia: SpliceMediaIds;
    assetResolution: OfficialAssetResolution | undefined;
  }>;

export type MaterializeRouteOptions = Readonly<{
  signal: AbortSignal | undefined;
  artifactScope: AppModeStartOptions["artifactScope"];
  loadTemplate: AppModeTemplateLoader;
  readNodeDefinitions: () => unknown;
  nodeDefinitionReadinessReached: () => boolean;
  admitted: AppModeAdmission;
  preparedCanonicalPrompt: AppModeStartOptions["preparedCanonicalPrompt"];
}>;

export function createHostDetachedGraph(
  app: AppModeApp,
  serialized: unknown,
): unknown {
  const constructor = probeGraphConstructor(app);
  if (constructor.status !== "ready")
    throw new AppModeError(
      "incompatible_seam",
      "the detached host graph constructor is unavailable",
    );
  try {
    // CRITICAL: construct from the live host graph class. Importing a second LiteGraph runtime
    // splits node registries and Pinia-scoped graph stores from the graph ComfyUI compiles.
    return new constructor.value(serialized);
  } catch {
    throw new AppModeError(
      "incompatible_seam",
      "the detached host graph constructor rejected the candidate safely",
    );
  }
}

// CRITICAL (M23-37, frontend 1.51.9): a definitions-carrying candidate must be compiled from
// the loaded root canvas. A detached compile loses the anchor prompt binding on that host.
export async function compileLoadedCandidate(
  app: AppModeApp,
): Promise<AppModeCompiledPrompt> {
  try {
    const compile = probeGraphToPrompt(app);
    if (compile.status !== "ready") throw new Error(compile.reason);
    return await Promise.resolve(compile.value());
  } catch {
    throw new AppModeError(
      "compile_failed",
      "the host could not compile the loaded H3 graph",
    );
  }
}

// IMPORTANT (M24-08, B-M1605-EXIST-01): only candidates with neither subgraph definitions nor a
// root graph id use this execution identity; see candidateRequiresHostLoadBeforeCompile.
export async function compileDetachedCandidate(
  app: AppModeApp,
  createDetachedGraph: AppModeDetachedGraphFactory,
  serialized: unknown,
): Promise<AppModeCompiledPrompt> {
  let detached: unknown;
  try {
    detached = createDetachedGraph(snapshotGraph(serialized));
  } catch (error) {
    if (error instanceof AppModeError) throw error;
    throw new AppModeError(
      "incompatible_seam",
      "the detached host graph constructor rejected the candidate safely",
    );
  }
  try {
    const compile = probeGraphToPrompt(app);
    if (compile.status !== "ready") throw new Error(compile.reason);
    return await Promise.resolve(compile.value(detached));
  } catch {
    throw new AppModeError(
      "compile_failed",
      "the host could not compile the detached H3 graph",
    );
  }
}

/**
 * Existing route: bind the Sidebar request to the one shared Context authoring seam of the
 * visible H3 graph. The rebound workflow is the candidate; its owned reference is the
 * previously written one when this workflow was materialized by this session.
 */
export function resolveExistingRoute(
  serialized: unknown,
  inputs: AppModeInputs,
  signal: AbortSignal | undefined,
  priorOwnedReference: OwnedGraphReference | undefined,
): ResolvedExistingRoute {
  let rebound;
  try {
    throwIfCancelled(signal);
    rebound = rebindExistingContextAuthoring(record(serialized) ?? {}, {
      taskMode: inputs.task_mode,
      userIntent: inputs.user_intent,
      durationSeconds: inputs.duration_milliseconds / MILLISECONDS_PER_SECOND,
    });
    throwIfCancelled(signal);
  } catch (error) {
    if (signal?.aborted)
      throw new AppModeError(
        "cancelled",
        "App Mode was cancelled before the canvas was changed",
      );
    throw new AppModeError(
      "incompatible_graph",
      "the Sidebar request could not be bound to one shared Context authoring seam",
    );
  }
  const existingSubject: ExistingCompiledAdmission = Object.freeze({
    requestNodeId: rebound.requestNodeId,
    durationSourceNodeId: rebound.durationSourceNodeId,
    visibleAnchorNodeId: rebound.anchorNodeId,
    anchorNodeId: rebound.compiledAnchorNodeId,
    anchorNodeType: rebound.anchorNodeType,
    productShellNodeId: rebound.productShellNodeId,
  });
  const ownedReference = priorOwnedReference ?? existingOwnedReference(rebound);
  let candidateWorkflow: unknown;
  let expectedGraphFingerprint: string;
  let expectedOwnedGraph: OwnedGraphObservation;
  try {
    throwIfCancelled(signal);
    candidateWorkflow = rebound.workflow;
    expectedGraphFingerprint = graphFingerprint(candidateWorkflow);
    expectedOwnedGraph = observeOwnedGraph(candidateWorkflow, ownedReference);
    throwIfCancelled(signal);
  } catch (error) {
    if (error instanceof AppModeError) {
      if (error.code === "stale_graph") throw error;
      if (error.code === "incompatible_seam") throw error;
    }
    if (signal?.aborted)
      throw new AppModeError(
        "cancelled",
        "App Mode was cancelled without accepting the Sidebar update",
      );
    throw new AppModeError(
      "incompatible_graph",
      "the bounded existing-graph candidate could not be validated safely",
    );
  }
  return Object.freeze({
    candidateWorkflow,
    ownedReference,
    expectedOwnedGraph,
    expectedGraphFingerprint,
    existingSubject,
  });
}

/**
 * Materialize route: the pinned official template is the workflow. Official assets are
 * resolved against the host inventory and the context pipeline is spliced in before any
 * canvas write, so every refusal here leaves the canvas untouched.
 */
export async function resolveMaterializeRoute(
  serialized: unknown,
  inputs: AppModeInputs,
  options: MaterializeRouteOptions,
): Promise<ResolvedMaterializeRoute> {
  const signal = options.signal;
  // M17-20 D11 option (b): the pinned official template *is* the workflow.
  // The H3 context pipeline is spliced into its prompt input; the sampler,
  // decode and sink chain arrive from the template unmodified, which is
  // what makes the materialized graph directly generation-capable instead
  // of the conditioning-only graph this item replaces.
  // The request contract used to be enforced by the API-prompt builder.
  // The builder is gone, so the route enforces it directly -- before the
  // template is fetched, and therefore before anything is loaded.
  const media = resolveSpliceMedia(serialized, inputs);
  const templateName = MODE_TEMPLATE[inputs.task_mode];
  if (media === undefined || templateName === undefined)
    throw new AppModeError(
      "incompatible_graph",
      "a selected host-owned media source is not a visible supported loader output",
    );
  let spliced;
  let ownedReference: OwnedGraphReference;
  let assetResolution: OfficialAssetResolution | undefined;
  try {
    throwIfCancelled(signal);
    const template = await Promise.resolve(options.loadTemplate(templateName));
    throwIfCancelled(signal);
    let materializationTemplate = record(template) ?? {};
    // M17-29. Resolve against the host's already-loaded combo arrays and
    // finish every serialized mutation before the first canvas write.
    // Connect/adopt never enter this block, so a later user choice cannot
    // be overwritten by the materialization ladder.
    const nodeDefinitions = options.readNodeDefinitions();
    const nodeDefinitionReadiness = classifyHostMappingReadiness(
      nodeDefinitions,
      options.nodeDefinitionReadinessReached(),
    );
    if (nodeDefinitionReadiness === "ready") {
      const family = TASK_MODE_FAMILY[inputs.task_mode];
      if (family === undefined)
        throw new OfficialAssetResolutionError("missing_loader_seam");
      try {
        assetResolution = resolveOfficialAssets(
          materializationTemplate,
          family,
          readOfficialAssetInventoryFromNodeDefinitions(nodeDefinitions),
        );
        materializationTemplate = assetResolution.workflow;
      } catch (error) {
        // CRITICAL: model inventory is advisory. An unrecognized filename or
        // COMBO shape must leave the template available for native queue-time
        // validation; only the actual splice and host-readiness checks gate it.
        if (
          !(error instanceof OfficialAssetResolutionError) ||
          error.code === "invalid_manifest"
        )
          throw error;
      }
    } else if (nodeDefinitionReadiness === "present_not_ready") {
      // IMPORTANT: a registry observed before the host readiness point is
      // not evidence that the public collection is empty or unavailable.
      throw new OfficialAssetResolutionError("invalid_host_inventory");
    }
    spliced = spliceContextPipeline(materializationTemplate, {
      taskMode: inputs.task_mode,
      userIntent: inputs.user_intent,
      durationSeconds: inputs.duration_milliseconds / MILLISECONDS_PER_SECOND,
      media,
      artifactPrefix: appModeArtifactPrefix(inputs, options.artifactScope),
      canonicalLowering:
        options.preparedCanonicalPrompt === undefined
          ? undefined
          : {
              baseReportFingerprint:
                options.preparedCanonicalPrompt.baseReportFingerprint,
              baseReportRevision:
                options.preparedCanonicalPrompt.baseReportRevision,
              overrideRevision:
                options.preparedCanonicalPrompt.overrideRevision,
              reason: options.preparedCanonicalPrompt.reason,
            },
    });
    ownedReference = Object.freeze({
      nodeIds: Object.freeze(spliced.ownedNodeIds.map(String)),
      linkIds: Object.freeze(spliced.ownedLinkIds.map(String)),
      anchorNodeId: String(spliced.anchorNodeId),
      authoredWidgetNodeIds: Object.freeze(
        spliced.authoredWidgetNodeIds.map(String),
      ),
    });
  } catch (error) {
    if (signal?.aborted)
      throw new AppModeError(
        "cancelled",
        "App Mode was cancelled before the canvas was replaced",
      );
    if (error instanceof AppModeError) throw error;
    // Nothing has been loaded yet, so there is nothing to roll back: a
    // template that cannot be read or spliced leaves the canvas untouched.
    throw new AppModeError(
      "incompatible_seam",
      error instanceof TemplateSpliceError ||
        error instanceof OfficialAssetResolutionError
        ? "the pinned generation template no longer carries the expected splice point"
        : "the pinned generation template could not be read from the host",
      error instanceof OfficialAssetResolutionError
        ? undefined
        : TEMPLATE_UNAVAILABLE_REASON,
    );
  }
  // D5: a template whose sink prefix could not be written is a template
  // this build cannot attribute an artifact from, and it is refused rather
  // than queued under the shared default location.
  if (!spliced.artifactPrefixApplied)
    throw new AppModeError(
      "incompatible_seam",
      "the pinned generation template no longer carries the expected splice point",
      TEMPLATE_UNAVAILABLE_REASON,
    );
  // CRITICAL: replacement cannot be the probe for a malformed duration
  // seam. Refuse while the user's current canvas is still untouched.
  if (!spliced.durationApplied)
    throw new AppModeError(
      "incompatible_seam",
      "the pinned generation template no longer carries the expected duration source",
      TEMPLATE_UNAVAILABLE_REASON,
    );
  const materializedMedia = spliced.mediaNodeIds;
  let candidateWorkflow: unknown;
  let expectedGraphFingerprint: string;
  let expectedOwnedGraph: OwnedGraphObservation;
  try {
    throwIfCancelled(signal);
    candidateWorkflow = spliced.workflow;
    expectedGraphFingerprint = graphFingerprint(candidateWorkflow);
    expectedOwnedGraph = observeOwnedGraph(candidateWorkflow, ownedReference);
    throwIfCancelled(signal);
  } catch (error) {
    if (signal?.aborted)
      throw new AppModeError(
        "cancelled",
        "App Mode was cancelled before the replacement graph was written",
      );
    throw new AppModeError(
      "compile_failed",
      "the qualified Base graph candidate has no stable identity",
    );
  }
  return Object.freeze({
    candidateWorkflow,
    ownedReference,
    expectedOwnedGraph,
    expectedGraphFingerprint,
    materializedMedia,
    assetResolution,
  });
}

/**
 * Connect route: the user canvas is the graph; the context pipeline is inserted and exactly
 * one native H3 anchor's `prompt` is rewired. Nothing else on the canvas is read or rewritten.
 */
export function resolveConnectRoute(
  serialized: unknown,
  inputs: AppModeInputs,
  signal: AbortSignal | undefined,
  designatedAnchorId: number | undefined,
): ResolvedRouteCandidate {
  // M17-20 D13. The canvas is the graph; this route inserts the context
  // pipeline into it and rewires exactly one native H3 anchor's `prompt`.
  // Everything else the user assembled is left alone, which is why the
  // splice runs in connect mode and why nothing here reads or rewrites a
  // media input, a loader or the artifact location.
  //
  // The splice is visible on purpose: substituting the prompt at compile
  // time would hide the effective prompt from the canvas, which section
  // 2.4 rejects outright.
  let spliced;
  let ownedReference: OwnedGraphReference;
  try {
    throwIfCancelled(signal);
    spliced = spliceContextPipeline(record(serialized) ?? {}, {
      taskMode: inputs.task_mode,
      userIntent: inputs.user_intent,
      durationSeconds: inputs.duration_milliseconds / MILLISECONDS_PER_SECOND,
      connect: true,
      designatedAnchorId,
    });
    ownedReference = Object.freeze({
      nodeIds: Object.freeze(spliced.ownedNodeIds.map(String)),
      linkIds: Object.freeze(spliced.ownedLinkIds.map(String)),
      anchorNodeId: String(spliced.anchorNodeId),
      authoredWidgetNodeIds: Object.freeze(
        spliced.authoredWidgetNodeIds.map(String),
      ),
    });
    throwIfCancelled(signal);
  } catch (error) {
    if (signal?.aborted)
      throw new AppModeError(
        "cancelled",
        "App Mode was cancelled before the canvas was changed",
      );
    if (error instanceof AppModeError) throw error;
    // Nothing was written, so there is nothing to roll back. Each refusal
    // names what the user has to change rather than what this module
    // wanted, because the graph is theirs.
    const code =
      error instanceof TemplateSpliceError ? error.code : "malformed";
    throw new AppModeError(
      "incompatible_graph",
      code === "task_mode_mismatch"
        ? "the designated node targets a different task mode than the one selected"
        : code === "ambiguous_anchor"
          ? "the canvas carries several H3 generation nodes; designate the one to connect"
          : code === "unknown_anchor"
            ? "the designated node is not an H3 generation node on this canvas"
            : code === "already_connected"
              ? "this canvas already contains an H3 Context pipeline; queue the current H3 graph instead"
              : code === "no_anchor"
                ? "the canvas carries no H3 generation node the prompt could reach"
                : "the current canvas could not be connected safely",
      code === "no_anchor" || code === "unknown_anchor"
        ? anchorMissingReason(inputs.task_mode)
        : code === "connect_missing_first_frame"
          ? { kind: "connect_missing_first_frame" }
          : code === "connect_missing_last_frame"
            ? { kind: "connect_missing_last_frame" }
            : undefined,
    );
  }
  let candidateWorkflow: unknown;
  let expectedGraphFingerprint: string;
  let expectedOwnedGraph: OwnedGraphObservation;
  try {
    throwIfCancelled(signal);
    candidateWorkflow = spliced.workflow;
    expectedGraphFingerprint = graphFingerprint(candidateWorkflow);
    expectedOwnedGraph = observeOwnedGraph(candidateWorkflow, ownedReference);
    throwIfCancelled(signal);
  } catch (error) {
    if (signal?.aborted)
      throw new AppModeError(
        "cancelled",
        "App Mode was cancelled before the connected graph was written",
      );
    throw new AppModeError(
      "compile_failed",
      "the connected graph candidate has no stable identity",
    );
  }
  return Object.freeze({
    candidateWorkflow,
    ownedReference,
    expectedOwnedGraph,
    expectedGraphFingerprint,
  });
}
