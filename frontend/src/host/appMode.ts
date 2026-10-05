import { qualifyAppMode } from "./appModeQualification";
export { qualifyAppMode } from "./appModeQualification";

import type { GenerationProfile } from "../contracts/generationProfileCodec";
import { createGenerationProfileClient } from "./generationProfileClient";
import { H3_SHELL_MANIFEST, inspectH3GraphAdmission } from "./graphAdapter";
import { readQueueMetadataCompatibleGraph } from "./graphSerialization";
import type { InputGeometryReceipt } from "./inputGeometry";
import {
  observeOwnedGraph,
  type OwnedGraphObservation,
  type OwnedGraphReference,
} from "./ownedGraphIdentity";
import {
  probeFetchApi,
  probeGraphToPrompt,
  readNodeDefinitionRegistry,
  readVisibleGraph,
  serializeGraph,
} from "./hostSeams";
import {
  cloneQueueEnvelope,
  invokeQueue,
  observeQueueSeam,
  probeQueueCallable,
} from "./queueSeam";
import {
  admitCanvasForConnect,
  type CanvasAdmission,
  MODE_TEMPLATE,
  rebindExistingContextAuthoring,
  spliceContextPipeline,
  type SpliceMediaIds,
  TASK_MODE_FAMILY,
  TemplateSpliceError,
} from "./templateMaterialization";
import {
  ADMISSION_MESSAGES,
  admissionBlocksQueue,
  admissionBlocksRoute,
  admitGenerationProfile,
  ADMITTED,
  candidateRequiresHostLoadBeforeCompile,
  generationAdmissionRefusalReason,
} from "./appModeAdmission";
import {
  compiledPromptMatchesVisibleGraph,
  listAppModeImageSources,
  listAppModeMediaSources,
} from "./appModeCensus";
import {
  anchorMissingReason,
  APP_MODE_TASK_MODES,
  type AppModeAdmission,
  type AppModeApi,
  type AppModeApp,
  appModeArtifactPrefix,
  type AppModeCapability,
  type AppModeCompiledPrompt,
  type AppModeDetachedGraphFactory,
  AppModeError,
  type AppModeImageSource,
  type AppModeInputs,
  type AppModeMediaSource,
  type AppModeProfileLoader,
  type ProductionCanonicalLowering,
  type AppModeStartOptions,
  type AppModeStartResult,
  type AppModeCanvasResult,
  type CompiledPromptRoute,
  contentFreeBootstrapWorkflow,
  type ExistingCompiledAdmission,
  fingerprint,
  graphFingerprint,
  graphNodes,
  isSafeCompiledPromptEnvelope,
  type ManagedAppModeQueueAuthority,
  type ManagedAppModeQueueFailureDisposition,
  MILLISECONDS_PER_SECOND,
  normalizeAppModeCompiledPrompt,
  record,
  recordManagedExecutionIdentity,
  snapshotGraph,
  TEMPLATE_UNAVAILABLE_REASON,
  throwIfCancelled,
  validateConnectInputs,
  validateExistingInputs,
  validateInputs,
  validatePreparedCanonicalStart,
} from "./appModeContract";
import {
  compiledPromptMatchesOfficialAssetResolution,
  compiledPromptMatchesRequestedRoute,
  isCompatibleExistingPrompt,
  resolveOpaqueSources,
} from "./appModeExistingRoute";
import {
  buildManagedAppModePreparation,
  buildManagedSourceIdentityRequest,
  projectManagedExecutionPrompt,
} from "./appModeManagedPreparation";
import {
  type AppModeTemplateLoader,
  createDefaultTemplateLoader,
  resolveSpliceMedia,
} from "./appModeTemplateSources";
import {
  assertGraphWriteAuthority,
  assertPendingGraphWriteAuthority,
  captureGraphWriteAuthority,
  ownedAuthorityAfterPartialBootstrap,
  ownedAuthorityBeforeWrite,
  probeGraphWriter,
  readActiveWorkflow,
  restoreGraph,
  type OwnedGraphConfigure,
  type OwnedGraphWriteAuthority,
  writeValidatedGraph,
} from "./canvasOwnedWrite";
import {
  resolveConnectRoute,
  compileDetachedCandidate,
  compileLoadedCandidate,
  createHostDetachedGraph,
  resolveExistingRoute,
  resolveMaterializeRoute,
} from "./appModeRoutes";

export {
  admissionBlocksQueue,
  admissionBlocksRoute,
  admitGenerationProfile,
  candidateRequiresHostLoadBeforeCompile,
} from "./appModeAdmission";
export {
  compiledPromptMatchesVisibleGraph,
  listAppModeImageSources,
  listAppModeMediaSources,
} from "./appModeCensus";
export {
  APP_MODE_ARTIFACT_PREFIX_ROOT,
  APP_MODE_DEFAULT_LENGTH,
  APP_MODE_MAX_DURATION_MILLISECONDS,
  APP_MODE_MAX_DURATION_SECONDS,
  APP_MODE_MAX_FRAME_COUNT,
  APP_MODE_MIN_FRAME_COUNT,
  APP_MODE_MODE_CAPABILITIES,
  APP_MODE_TASK_MODES,
  APP_MODE_WORKFLOW_ID,
  AppModeError,
  PREPARED_GRAPH_OBSERVATION_SCHEMA,
  PRODUCTION_CANONICAL_LOWERING_SCHEMA,
  appModeArtifactPrefix,
  fingerprint,
  graphFingerprint,
  normalizeAppModeCompiledPrompt,
  referenceVideoSoundtrackOf,
  validateConnectInputs,
  validateExistingInputs,
  validateInputs,
} from "./appModeContract";
export type {
  AppModeAdmission,
  AppModeAdmissionReason,
  AppModeApi,
  AppModeApp,
  AppModeCapability,
  AppModeCompiledPrompt,
  AppModeDetachedGraphFactory,
  AppModeImageSource,
  AppModeInputs,
  AppModeMediaKind,
  AppModeMediaSource,
  AppModeProfileLoader,
  AppModeRefusalReason,
  AppModeStartOptions,
  AppModeStartResult,
  AppModeCanvasResult,
  AppModeTaskMode,
  ExistingCompiledAdmission,
  ManagedAppModeCanvasIdentity,
  ManagedAppModeExecutionProjector,
  ManagedAppModePreflight,
  ManagedAppModePreparation,
  ManagedAppModeQueueAuthority,
  ManagedAppModeQueueFailureDisposition,
  PreparedGraphObservation,
  ProductionCanonicalLowering,
} from "./appModeContract";
export {
  compiledPromptMatchesOfficialAssetResolution,
  compiledPromptMatchesRequestedRoute,
  isCompatibleExistingPrompt,
} from "./appModeExistingRoute";
export {
  buildManagedAppModePreparation,
  buildManagedSourceIdentityRequest,
} from "./appModeManagedPreparation";
export { APP_MODE_MAX_TEMPLATE_BYTES } from "./appModeTemplateSources";
export type { AppModeTemplateLoader } from "./appModeTemplateSources";
export type { OwnedGraphConfigure } from "./canvasOwnedWrite";

export type { ReferenceVideoSoundtrack } from "./templateMaterialization";
export { OFFICIAL_LENGTH_EXPRESSION } from "./templateMaterialization";

export function createAppModeController(
  app: AppModeApp,
  api: AppModeApi,
  options: {
    loadTemplate?: AppModeTemplateLoader;
    loadProfile?: AppModeProfileLoader;
    beginOwnedGraphConfigure?: OwnedGraphConfigure;
    readNodeDefinitions?: () => unknown;
    nodeDefinitionReadinessReached?: () => boolean;
    createDetachedGraph?: AppModeDetachedGraphFactory;
  } = {},
) {
  // The callable captured at construction is the wrapper-detection baseline for
  // `observeQueueSeam`; a non-callable value is recorded as "no baseline".
  const constructionProbe = probeQueueCallable(api);
  const controllerQueueCallable: unknown =
    constructionProbe.status === "ready"
      ? constructionProbe.callable
      : undefined;
  const capability = (): AppModeCapability =>
    qualifyAppMode(app, api, options.createDetachedGraph);
  const beginOwnedConfigure = options.beginOwnedGraphConfigure;
  const createDetachedGraph =
    options.createDetachedGraph ??
    ((serialized: unknown) => createHostDetachedGraph(app, serialized));
  const loadTemplate = options.loadTemplate ?? createDefaultTemplateLoader(api);
  // IMPORTANT: read lazily. The extension module is evaluated before ComfyUI
  // finishes registering every LiteGraph node class on the supported host.
  const readNodeDefinitions =
    options.readNodeDefinitions ?? readNodeDefinitionRegistry;
  // The controller is constructed during module evaluation but a user action
  // can only enter start() after extension setup. Tests may inject the earlier
  // phase so present-but-unready never collapses into an absent seam.
  const nodeDefinitionReadinessReached =
    options.nodeDefinitionReadinessReached ?? (() => true);
  // The probe binds `fetchApi` to the host api object: the route resolves
  // through `this`, so a detached reference reaches the wrong origin or throws.
  const fetchApiSeam = probeFetchApi(api);
  const profileClient = createGenerationProfileClient({
    fetchApi: fetchApiSeam.status === "ready" ? fetchApiSeam.value : undefined,
  });
  const loadProfile =
    options.loadProfile ??
    ((signal?: AbortSignal) => profileClient.load(signal));
  // The projection is a host observation, not a per-run one, so it is read once
  // and reused. A rejected read is not cached: a host that was briefly
  // unreachable must not stay refused for the life of the sidebar.
  let pendingProfile: Promise<GenerationProfile> | undefined;
  // The exact workflow object is the only durable in-session owner. Retaining
  // the write-time IDs here avoids rediscovering ownership from user topology.
  const ownedByWorkflow = new WeakMap<object, OwnedGraphReference>();
  const profile = (signal?: AbortSignal): Promise<GenerationProfile> => {
    if (pendingProfile === undefined)
      pendingProfile = Promise.resolve(loadProfile(signal)).catch(
        (error: unknown) => {
          pendingProfile = undefined;
          throw error;
        },
      );
    return pendingProfile;
  };
  const admission = async (
    taskMode: string,
    settings: { signal?: AbortSignal; refresh?: boolean } = {},
  ): Promise<AppModeAdmission> => {
    if (settings.refresh === true) pendingProfile = undefined;
    try {
      return admitGenerationProfile(await profile(settings.signal), taskMode);
    } catch {
      // CRITICAL: fail closed. A host that cannot say whether generation is
      // possible has not qualified it, and guessing here is how a shell queues a
      // graph the host cannot run.
      return {
        status: "refused",
        reason: "profile_unavailable",
        remediation: "upgrade_host",
        unsatisfiedSlots: [],
      };
    }
  };
  const preparedCanvases = new WeakSet<object>();
  return {
    capability,
    admission,
    hasPreparedCanvas: () => {
      const workflow = readActiveWorkflow(app);
      return (
        workflow !== undefined &&
        workflow !== null &&
        preparedCanvases.has(workflow)
      );
    },
    activeWorkflow: () => readActiveWorkflow(app),
    imageSources: (): AppModeImageSource[] => {
      const visible = serializeGraph(app);
      if (visible.status !== "ready") return [];
      try {
        return listAppModeImageSources(visible.value);
      } catch {
        return [];
      }
    },
    /**
     * Which native H3 anchors the visible canvas offers to connect to.
     *
     * M17-20 D13. This is a census, not a decision: it reports what is there so
     * the surface can offer the route and, when there is more than one anchor,
     * require the user to designate. A canvas that cannot be read at all reports
     * tier C, which is the same answer as a canvas with no anchor -- replace or
     * keep, and nothing invented.
     */
    connectCandidates: (): CanvasAdmission => {
      const empty = { tier: "unavailable" as const, anchors: [] };
      const visible = serializeGraph(app);
      if (visible.status !== "ready") return empty;
      try {
        return admitCanvasForConnect(record(visible.value) ?? {});
      } catch {
        return empty;
      }
    },
    mediaSources: (): AppModeMediaSource[] => {
      const visible = serializeGraph(app);
      if (visible.status !== "ready") return [];
      try {
        return listAppModeMediaSources(visible.value);
      } catch {
        return [];
      }
    },
    start: (inputs: AppModeInputs, options: AppModeStartOptions = {}) =>
      execute(inputs, options, false),
    prepareCanvas: (inputs: AppModeInputs, options: AppModeStartOptions = {}) =>
      execute(inputs, options, true),
  };

  function execute(
    inputs: AppModeInputs,
    options: AppModeStartOptions,
    canvasOnly: false,
  ): Promise<AppModeStartResult>;
  function execute(
    inputs: AppModeInputs,
    options: AppModeStartOptions,
    canvasOnly: true,
  ): Promise<AppModeCanvasResult>;
  async function execute(
    inputs: AppModeInputs,
    options: AppModeStartOptions,
    canvasOnly: boolean,
  ): Promise<AppModeStartResult | AppModeCanvasResult> {
    const signal = options.signal;
    const prepareManaged = canvasOnly ? undefined : options.prepareManaged;
    if (!canvasOnly && options.prepareOnly === true)
      throw new AppModeError(
        "invalid_request",
        "canvas preparation must use prepareCanvas",
      );
    throwIfCancelled(signal);
    const qualified = capability();
    if (qualified.status !== "ready")
      throw new AppModeError("incompatible_seam", qualified.reason);
    const snapshot = serializeGraph(app);
    if (snapshot.status !== "ready")
      throw new AppModeError(
        "incompatible_seam",
        snapshot.reason === "graph_serialize_failed"
          ? "the host graph snapshot seam failed safely"
          : "the host graph snapshot seam is unavailable",
      );
    let serialized: unknown = snapshot.value;
    const connecting = options.connectExisting !== undefined;
    if (connecting || options.useExisting === true) {
      try {
        // CRITICAL: validate every existing-canvas workflow envelope before
        // snapshot/fingerprint cloning can invoke extension accessors.
        serialized = readQueueMetadataCompatibleGraph(serialized).graph;
      } catch {
        throw new AppModeError(
          "incompatible_graph",
          "the current canvas contains unsupported serialized queue metadata",
        );
      }
    }
    const beforeLoad = snapshotGraph(serialized);
    let beforeFingerprint: string;
    try {
      beforeFingerprint = graphFingerprint(serialized);
    } catch {
      throw new AppModeError(
        "incompatible_seam",
        "the host graph snapshot has no stable identity",
      );
    }
    const existingNodeCount = graphNodes(serialized).length;
    // Three routes, and exactly one of them per run. `connecting` and
    // `adopting` differ in what they do to the canvas but agree on what they
    // do *not* do: neither writes a template, so neither is held to the
    // template's admission or its artifact location.
    const chosen = [
      options.useExisting === true,
      options.replaceExisting === true,
      connecting,
    ].filter(Boolean).length;
    if (chosen > 1)
      throw new AppModeError(
        "invalid_request",
        "existing-graph binding, replacement and connect are mutually exclusive",
      );
    const adopting = options.useExisting === true || connecting;
    if (
      canvasOnly &&
      (adopting || options.preparedCanonicalPrompt !== undefined)
    )
      throw new AppModeError(
        "invalid_request",
        "canvas preparation requires a new or explicitly replaced template",
      );
    if (adopting && existingNodeCount === 0)
      throw new AppModeError(
        "incompatible_graph",
        "the current canvas is empty; start the canonical Base graph instead",
        anchorMissingReason(inputs.task_mode),
      );
    // CRITICAL: reject an un-restorable snapshot before inspecting or binding
    // an existing graph; every route needs a safe rollback boundary.
    if (beforeLoad === undefined)
      throw new AppModeError(
        "incompatible_seam",
        "the current canvas could not be snapshotted safely",
      );
    if (options.useExisting === true) {
      let inspection;
      try {
        inspection = inspectH3GraphAdmission(serialized, H3_SHELL_MANIFEST);
      } catch {
        throw new AppModeError(
          "incompatible_graph",
          "the current canvas is not a valid serialized H3 graph",
        );
      }
      // CRITICAL: existing-canvas admission recognizes the minimum native H3
      // core and one ProductShell correlation seam. Complete pipeline validity
      // belongs to the explicit host compilation below, not this census.
      if (
        inspection.status !== "ready" ||
        inspection.existingGraphCompatible !== true
      )
        throw new AppModeError(
          "incompatible_graph",
          "the current canvas has no recognized H3 core and ProductShell correlation seam",
          inspection.status === "missing" &&
            inspection.reason === "missing_native_h3_core"
            ? anchorMissingReason(inputs.task_mode)
            : undefined,
        );
    }
    // CRITICAL: never let a broad node-type check silently replace a user-edited graph.
    // Replacement is only legal after the UI has surfaced and the user has chosen it.
    if (existingNodeCount > 0 && !adopting && options.replaceExisting !== true)
      throw new AppModeError(
        "dirty_graph",
        "the current canvas contains an existing or unsaved graph; choose explicit replace or keep the current canvas",
      );
    // CRITICAL: every route may let host compilation mutate the graph;
    // require a verified snapshot/restore seam before any queue attempt.
    if (probeGraphWriter(app).status !== "ready")
      throw new AppModeError(
        "incompatible_seam",
        "the public graph restore seam is unavailable",
      );
    // M17-20 D1/D2: materialization sits behind profile admission. The gate is
    // here -- after the cheap structural refusals, before the first compile,
    // fetch or canvas write -- so a host that cannot run the graph costs the
    // user nothing and, above all, reaches zero queue submissions.
    //
    // The requested mode is checked first so a malformed request stays a
    // malformed request: without this, asking for a mode that does not exist
    // would be reported as a host that does not support it.
    if (!APP_MODE_TASK_MODES.includes(inputs.task_mode))
      throw new AppModeError(
        "invalid_request",
        "the requested App Mode route is unavailable",
      );
    if (connecting) validateConnectInputs(inputs);
    else if (options.useExisting === true) validateExistingInputs(inputs);
    else validateInputs(inputs);
    validatePreparedCanonicalStart(inputs, options);
    if (connecting && prepareManaged !== undefined)
      throw new AppModeError(
        "invalid_request",
        "the connected-canvas route is not Production-managed",
      );
    if (
      !canvasOnly &&
      options.projectManagedExecution !== undefined &&
      prepareManaged === undefined
    )
      throw new AppModeError(
        "invalid_request",
        "a managed execution projection requires one managed transaction owner",
      );
    let admitted = ADMITTED;
    // IMPORTANT: Connect and useExisting both adopt user-owned generation
    // state. Official asset/default admission applies only to a graph this
    // repository will materialize; consulting it here would restrict user
    // model filenames and settings before ComfyUI can validate their graph.
    if (!connecting && options.useExisting !== true) {
      throwIfCancelled(signal);
      admitted = await admission(inputs.task_mode, { signal });
      throwIfCancelled(signal);
      if (admissionBlocksRoute(admitted, adopting))
        throw new AppModeError(
          "incompatible_seam",
          ADMISSION_MESSAGES[
            (admitted as Extract<AppModeAdmission, { status: "refused" }>)
              .reason
          ],
          generationAdmissionRefusalReason(admitted),
        );
    }
    const pendingConfigure = captureGraphWriteAuthority(
      app,
      beginOwnedConfigure,
    );
    const priorOwnedReference =
      pendingConfigure.workflow === null
        ? undefined
        : ownedByWorkflow.get(pendingConfigure.workflow);
    let ownedReference = priorOwnedReference;
    let expectedOwnedGraph: OwnedGraphObservation | undefined;
    let candidateWorkflow: unknown;
    let existingSubject: ExistingCompiledAdmission | undefined;
    let ownConfigure: OwnedGraphWriteAuthority | undefined;
    let canvasWritten = false;
    const restoreOwnedGraph = async () => {
      if (!canvasWritten || ownConfigure === undefined) return;
      const restoreAuthority = ownConfigure;
      // IMPORTANT: consume the rollback exactly once before awaiting the host.
      // Nested failure classifiers must not apply the original graph twice.
      canvasWritten = false;
      await restoreGraph(
        app,
        beforeLoad,
        restoreAuthority,
        priorOwnedReference,
      );
    };
    throwIfCancelled(signal);
    // The selected media is qualified against the compiled prompt before the
    // canvas is replaced. Its widget values are read from the visible graph
    // (`resolveSpliceMedia`); this pass is what proves the visible node and the
    // compiled node are the same node and that its envelope is safe.
    if (!adopting && inputs.task_mode !== "t2va") {
      let sourceCompiled: AppModeCompiledPrompt;
      try {
        throwIfCancelled(signal);
        const compileSource = probeGraphToPrompt(app);
        if (compileSource.status !== "ready")
          throw new Error(compileSource.reason);
        sourceCompiled = await Promise.resolve(compileSource.value());
        throwIfCancelled(signal);
        if (!isSafeCompiledPromptEnvelope(sourceCompiled))
          throw new Error("source graph changed during qualification");
      } catch {
        await restoreOwnedGraph();
        throw new AppModeError(
          "incompatible_graph",
          "the selected host-owned image source could not be qualified",
        );
      }
      const resolved = resolveOpaqueSources(serialized, sourceCompiled, inputs);
      if (resolved === undefined)
        throw new AppModeError(
          "incompatible_graph",
          "a selected host-owned media source is not a visible supported loader output",
        );
    }
    let expectedGraphFingerprint = beforeFingerprint;
    let materializedMedia: SpliceMediaIds | undefined;
    if (options.useExisting === true) {
      const resolved = resolveExistingRoute(
        serialized,
        inputs,
        signal,
        priorOwnedReference,
      );
      existingSubject = resolved.existingSubject;
      ownedReference = resolved.ownedReference;
      candidateWorkflow = resolved.candidateWorkflow;
      expectedGraphFingerprint = resolved.expectedGraphFingerprint;
      expectedOwnedGraph = resolved.expectedOwnedGraph;
      serialized = candidateWorkflow;
    }
    if (!adopting) {
      const resolved = await resolveMaterializeRoute(serialized, inputs, {
        signal,
        artifactScope: options.artifactScope,
        loadTemplate,
        readNodeDefinitions,
        nodeDefinitionReadinessReached,
        admitted,
        preparedCanonicalPrompt: options.preparedCanonicalPrompt,
      });
      ownedReference = resolved.ownedReference;
      materializedMedia = resolved.materializedMedia;
      candidateWorkflow = resolved.candidateWorkflow;
      expectedGraphFingerprint = resolved.expectedGraphFingerprint;
      expectedOwnedGraph = resolved.expectedOwnedGraph;
    }
    if (connecting) {
      const resolved = resolveConnectRoute(
        serialized,
        inputs,
        signal,
        options.connectExisting?.anchorNodeId,
      );
      ownedReference = resolved.ownedReference;
      candidateWorkflow = resolved.candidateWorkflow;
      expectedGraphFingerprint = resolved.expectedGraphFingerprint;
      expectedOwnedGraph = resolved.expectedOwnedGraph;
    }
    if (
      candidateWorkflow === undefined ||
      ownedReference === undefined ||
      expectedOwnedGraph === undefined
    ) {
      throw new AppModeError(
        "incompatible_seam",
        "the repository-owned graph identity is unavailable",
      );
    }
    // CRITICAL (M24-08, B-M1605-EXIST-01): constructing or configuring an
    // LGraph from a candidate that carries subgraph definitions or the
    // visible workflow's root id publishes candidate widgets through the
    // host's shared graph and widget value stores. Route on those two
    // properties, write on the captured workflow, and root-compile that
    // write. Restoring an adopting/detached exception here
    // reintroduces stale Request intent/duration and can overwrite live edits.
    let hostLoad: OwnedGraphWriteAuthority | undefined;
    if (candidateRequiresHostLoadBeforeCompile(candidateWorkflow)) {
      try {
        throwIfCancelled(signal);
        assertPendingGraphWriteAuthority(app, pendingConfigure);
        const currentBeforeWrite = readVisibleGraph(app);
        const canvasChanged =
          options.useExisting === true
            ? observeOwnedGraph(currentBeforeWrite, ownedReference)
                .fingerprint !==
              observeOwnedGraph(beforeLoad, ownedReference).fingerprint
            : graphFingerprint(currentBeforeWrite) !== beforeFingerprint;
        if (canvasChanged)
          throw new AppModeError(
            "stale_graph",
            "the canvas changed before the validated graph write",
          );
        ownConfigure = ownedAuthorityBeforeWrite(pendingConfigure);
        canvasWritten = ownConfigure !== undefined;
        hostLoad = await writeValidatedGraph(
          app,
          candidateWorkflow,
          pendingConfigure,
        );
        ownConfigure = hostLoad;
        canvasWritten = true;
        options.onHostLoadBeforeCompile?.();
        const written = readQueueMetadataCompatibleGraph(
          readVisibleGraph(app),
        ).graph;
        if (
          observeOwnedGraph(written, ownedReference).fingerprint !==
          expectedOwnedGraph.fingerprint
        )
          throw new AppModeError(
            "stale_graph",
            "the host changed the repository-owned candidate during its one canvas write",
          );
        throwIfCancelled(signal);
      } catch (error) {
        if (ownConfigure === undefined) {
          ownConfigure = ownedAuthorityAfterPartialBootstrap(
            app,
            pendingConfigure,
          );
          canvasWritten = ownConfigure !== undefined;
        }
        await restoreOwnedGraph();
        if (signal?.aborted)
          throw new AppModeError(
            "cancelled",
            "App Mode was cancelled before candidate compilation completed",
          );
        if (error instanceof AppModeError) throw error;
        throw new AppModeError(
          "compile_failed",
          "the host rejected the validated graph transaction",
        );
      }
    }
    let compiled: AppModeCompiledPrompt;
    try {
      throwIfCancelled(signal);
      compiled =
        hostLoad !== undefined
          ? await compileLoadedCandidate(app)
          : await compileDetachedCandidate(
              app,
              createDetachedGraph,
              candidateWorkflow,
            );
      throwIfCancelled(signal);
    } catch (error) {
      await restoreOwnedGraph();
      if (signal?.aborted)
        throw new AppModeError(
          "cancelled",
          "App Mode was cancelled before candidate compilation completed",
        );
      if (error instanceof AppModeError) throw error;
      throw new AppModeError(
        "compile_failed",
        "the host could not compile the H3 graph candidate",
      );
    }
    const compiledRoute: CompiledPromptRoute = connecting
      ? "connect"
      : options.useExisting === true
        ? "existing"
        : options.preparedCanonicalPrompt === undefined
          ? "materialize"
          : "prepared";
    if (
      !isSafeCompiledPromptEnvelope(compiled) ||
      !isCompatibleExistingPrompt(compiled, compiledRoute, existingSubject) ||
      !compiledPromptMatchesRequestedRoute(
        compiled,
        inputs,
        compiledRoute,
        materializedMedia,
        existingSubject,
        options.preparedCanonicalPrompt,
      )
    ) {
      await restoreOwnedGraph();
      throw new AppModeError(
        adopting ? "incompatible_graph" : "compile_failed",
        adopting
          ? "the current graph is not a complete H3 flow with one Product Shell and native generation anchor"
          : "host compilation returned an unqualified H3 prompt envelope",
      );
    }
    if (
      options.useExisting === true &&
      !compiledPromptMatchesVisibleGraph(
        candidateWorkflow,
        compiled,
        existingSubject,
      )
    ) {
      await restoreOwnedGraph();
      throw new AppModeError(
        "incompatible_graph",
        "the compiled prompt is not bound to the visible H3 graph",
      );
    }
    compiled = normalizeAppModeCompiledPrompt(compiled);
    let completeManagedCompiled: AppModeCompiledPrompt | undefined;
    if (prepareManaged !== undefined) {
      // CRITICAL: the one managed model submission is also the ProductShell
      // bootstrap. Keep only its workflow metadata content-free, fresh and
      // mutable so co-installed queue wrappers cannot see the native canvas
      // or contaminate a retained preparation.
      completeManagedCompiled = {
        ...compiled,
        workflow: contentFreeBootstrapWorkflow(),
      };
      compiled = projectManagedExecutionPrompt(
        completeManagedCompiled,
        options.projectManagedExecution,
      );
    }
    let compiledPromptFingerprint: string;
    try {
      compiledPromptFingerprint = fingerprint(compiled);
      if (completeManagedCompiled !== undefined)
        recordManagedExecutionIdentity(
          "final_projection",
          compiledPromptFingerprint,
        );
    } catch {
      await restoreOwnedGraph();
      throw new AppModeError(
        "compile_failed",
        "host compilation returned no stable prompt identity",
      );
    }
    // CRITICAL: compiled loader names are host-owned. Keep graph/prompt binding
    // checks above, but leave model-name validity to ComfyUI's queue response.
    const expectedIdentity = options.expectedIdentity;
    if (
      expectedIdentity !== undefined &&
      expectedIdentity.compiledPromptFingerprint !== compiledPromptFingerprint
    ) {
      await restoreOwnedGraph();
      throw new AppModeError("compile_failed", "identity mismatch");
    }
    let managedAuthority: ManagedAppModeQueueAuthority | undefined;
    if (prepareManaged !== undefined) {
      try {
        throwIfCancelled(signal);
        const managedRoute = options.useExisting
          ? "existing"
          : options.replaceExisting
            ? "replace"
            : "new";
        let sourceIdentity: InputGeometryReceipt | undefined;
        if (inputs.task_mode === "i2va" && managedRoute !== "existing") {
          try {
            if (
              options.observeSourceIdentity === undefined ||
              inputs.first_frame_source === undefined
            )
              throw new AppModeError(
                "incompatible_seam",
                "source identity observation is unavailable; keep the canvas and use native nodes",
              );
            sourceIdentity = await options.observeSourceIdentity(
              buildManagedSourceIdentityRequest(
                serialized,
                inputs.first_frame_source,
              ),
              signal,
            );
            throwIfCancelled(signal);
          } catch (error) {
            if (signal?.aborted)
              throw new AppModeError(
                "cancelled",
                "App Mode was cancelled before source identity was accepted",
              );
            if (error instanceof AppModeError) throw error;
            throw new AppModeError(
              "incompatible_seam",
              "source identity could not be observed safely; reselect the image or use native nodes",
            );
          }
        }
        const preparation = buildManagedAppModePreparation(
          completeManagedCompiled ?? compiled,
          inputs,
          managedRoute,
          expectedGraphFingerprint,
          expectedOwnedGraph,
          sourceIdentity,
          existingSubject,
          options.projectManagedExecution,
        );
        recordManagedExecutionIdentity(
          "preflight_projection",
          preparation.observation.compiled_prompt_fingerprint,
        );
        managedAuthority = await prepareManaged(preparation);
        throwIfCancelled(signal);
      } catch (error) {
        try {
          await managedAuthority?.onQueueFailed("not_invoked");
        } catch {
          // Preserve the preparation failure as the authoritative error.
        }
        await restoreOwnedGraph();
        if (signal?.aborted)
          throw new AppModeError(
            "cancelled",
            "App Mode was cancelled before managed preparation completed",
          );
        if (error instanceof AppModeError) throw error;
        throw new AppModeError(
          "queue_failed",
          "the managed Context preparation could not be completed",
        );
      }
      if (
        managedAuthority.expectedIdentity.compiledPromptFingerprint !==
        compiledPromptFingerprint
      ) {
        await managedAuthority.onQueueFailed("not_invoked");
        await restoreOwnedGraph();
        throw new AppModeError(
          "compile_failed",
          "the prepared sequence authority did not match the compiled prompt",
        );
      }
    }
    let committedAuthority: OwnedGraphWriteAuthority;
    try {
      throwIfCancelled(signal);
      if (hostLoad !== undefined) {
        // M23-37: the candidate is already on the canvas; the same workflow
        // object must still own it.
        assertGraphWriteAuthority(app, hostLoad);
        committedAuthority = hostLoad;
      } else {
        // CRITICAL: detached compilation is asynchronous. Do not let its final
        // write overwrite a user/host canvas edit made after this run captured
        // replacement consent and workflow authority.
        assertPendingGraphWriteAuthority(app, pendingConfigure);
        const currentBeforeWrite = readVisibleGraph(app);
        const canvasChanged =
          options.useExisting === true
            ? observeOwnedGraph(currentBeforeWrite, ownedReference)
                .fingerprint !==
              observeOwnedGraph(beforeLoad, ownedReference).fingerprint
            : graphFingerprint(currentBeforeWrite) !== beforeFingerprint;
        if (canvasChanged)
          throw new AppModeError(
            "stale_graph",
            "the canvas changed before the validated graph write",
          );
        committedAuthority = await writeValidatedGraph(
          app,
          candidateWorkflow,
          pendingConfigure,
        );
        ownConfigure = committedAuthority;
        canvasWritten = true;
      }
      const written = readQueueMetadataCompatibleGraph(
        readVisibleGraph(app),
      ).graph;
      const writtenOwned = observeOwnedGraph(written, ownedReference);
      if (writtenOwned.fingerprint !== expectedOwnedGraph.fingerprint)
        throw new AppModeError(
          "stale_graph",
          "the host changed the repository-owned candidate during its one canvas write",
        );
      // D12: the candidate compile identity is the execution gate. The host's
      // whole-graph serialization remains surroundings evidence only.
      expectedGraphFingerprint = graphFingerprint(candidateWorkflow);
      managedAuthority?.bindCanvasIdentity?.(
        Object.freeze({
          workflowAuthority: committedAuthority.workflow,
          ownedReference,
        }),
      );
      throwIfCancelled(signal);
    } catch (error) {
      try {
        await managedAuthority?.onQueueFailed("not_invoked");
      } catch {
        // Preserve the canvas transaction failure as the authoritative error.
      }
      await restoreOwnedGraph();
      if (signal?.aborted)
        throw new AppModeError(
          "cancelled",
          "App Mode was cancelled before queue submission",
        );
      if (error instanceof AppModeError) throw error;
      throw new AppModeError(
        "compile_failed",
        "the host rejected the validated graph transaction",
      );
    }
    // CRITICAL: preparing a canvas must finish before acquiring any host queue
    // ownership. Never install a bootstrap waiter or invent a prompt ID here.
    if (canvasOnly) {
      ownedByWorkflow.set(committedAuthority.workflow, ownedReference);
      preparedCanvases.add(committedAuthority.workflow);
      return {
        preparedOnly: true,
        transactionId: options.transactionId,
        graphFingerprint: expectedGraphFingerprint,
        compiledPromptFingerprint,
        ownedProjectionFingerprint: expectedOwnedGraph.fingerprint,
        ownedNodeIds: expectedOwnedGraph.nodeIds,
        ownedLinkIds: expectedOwnedGraph.linkIds,
        route: options.replaceExisting === true ? "replace" : "new",
      };
    }
    let queueState: "not_invoked" | "invoked" | "accepted" = "not_invoked";
    let knownFailure:
      | Extract<ManagedAppModeQueueFailureDisposition, "rejected" | "ambiguous">
      | undefined;
    try {
      throwIfCancelled(signal);
      const beforeQueueCompiled = fingerprint(compiled);
      if (beforeQueueCompiled !== compiledPromptFingerprint) {
        throw new AppModeError(
          "stale_graph",
          "the compiled prompt changed before queue submission",
        );
      }
      const beforeQueueOwned = observeOwnedGraph(
        readVisibleGraph(app),
        ownedReference,
      );
      if (beforeQueueOwned.fingerprint !== expectedOwnedGraph.fingerprint)
        throw new AppModeError(
          "stale_graph",
          "the repository-owned graph changed before queue submission",
        );
      // CRITICAL: an equal owned projection cannot transfer queue authority to another tab.
      assertGraphWriteAuthority(app, committedAuthority);
      const queueProbe = probeQueueCallable(api);
      if (queueProbe.status !== "ready")
        throw new AppModeError(
          "incompatible_seam",
          queueProbe.reason === "queue_callable_unreadable"
            ? "the host queue callable could not be read safely"
            : "the host queue callable is unavailable",
        );
      const queueObservation = observeQueueSeam(
        queueProbe.callable,
        controllerQueueCallable,
      );
      const queueEnvelope = cloneQueueEnvelope(compiled);
      if (
        queueEnvelope === undefined ||
        fingerprint(queueEnvelope) !== compiledPromptFingerprint
      )
        throw new AppModeError(
          "incompatible_seam",
          "the validated prompt could not be isolated for the host queue",
        );
      recordManagedExecutionIdentity(
        "queue_envelope",
        fingerprint(queueEnvelope),
      );
      try {
        options.onQueueSeamObserved?.(queueObservation);
      } catch {
        // Diagnostics are best-effort and cannot change queue authority.
      }
      // IMPORTANT: once the seam has been invoked, ComfyUI may already own host
      // work. A later local abort cannot truthfully cancel that submission or
      // restore the canvas as though it never happened; `onInvoked` records the
      // boundary before the call and the classification below honours it.
      const invocation = await invokeQueue(
        api,
        queueProbe.callable,
        queueEnvelope,
        {
          onInvoked: () => {
            queueState = "invoked";
          },
          onSubmitted: () => {
            ownedByWorkflow.set(committedAuthority.workflow, ownedReference);
            managedAuthority?.onQueueSubmitted();
            options.onQueueSubmitted?.();
          },
        },
      );
      if (invocation.status === "rejected") {
        knownFailure = "rejected";
        throw new AppModeError(
          "queue_failed",
          "ComfyUI rejected the graph before queue acceptance",
          {
            kind: "queue_rejected",
            classTypes: invocation.rejection.classTypes,
            errorTypes: invocation.rejection.errorTypes,
          },
        );
      }
      if (invocation.status === "failed") throw invocation.error;
      if (invocation.status === "invalid_response") {
        knownFailure = "ambiguous";
        throw new AppModeError(
          "queue_failed",
          "ComfyUI returned an invalid queue acknowledgement",
          { kind: "queue_response_invalid" },
        );
      }
      const queued = invocation.receipt;
      queueState = "accepted";
      preparedCanvases.delete(committedAuthority.workflow);
      const result: AppModeStartResult = {
        queueResult: queued,
        transactionId: options.transactionId,
        graphFingerprint: expectedGraphFingerprint,
        compiledPromptFingerprint,
        ownedProjectionFingerprint: expectedOwnedGraph.fingerprint,
        ownedNodeIds: expectedOwnedGraph.nodeIds,
        ownedLinkIds: expectedOwnedGraph.linkIds,
        queuePromptId: queued.prompt_id,
        route: connecting
          ? "connect"
          : options.useExisting === true
            ? "existing"
            : options.replaceExisting === true
              ? "replace"
              : "new",
      };
      await managedAuthority?.onQueueAccepted(result);
      return result;
    } catch (error) {
      // IMPORTANT: only M23-39 execution terminals remain authoritative here.
      // Preserving any other typed callback error would hide ambiguous ownership.
      if (
        queueState === "accepted" &&
        error instanceof AppModeError &&
        (error.code === "execution_failed" ||
          error.code === "execution_interrupted")
      )
        throw error;
      const failureDisposition: ManagedAppModeQueueFailureDisposition =
        knownFailure ??
        (queueState === "not_invoked" ? "not_invoked" : "ambiguous");
      try {
        await managedAuthority?.onQueueFailed(failureDisposition);
      } catch {
        // The original queue boundary remains the authoritative failure.
      }
      if (signal?.aborted && queueState === "not_invoked") {
        await restoreOwnedGraph();
        throw new AppModeError(
          "cancelled",
          "App Mode was cancelled before queue submission",
        );
      }
      // CRITICAL: after ComfyUI returns a prompt id, the managed authority may
      // already have classified its terminal. Rewrapping that typed failure as
      // ownership ambiguity hides a real execution_failed/interrupted result.
      if (failureDisposition === "rejected") {
        // CRITICAL: a native model-name rejection leaves a newly generated
        // template on the canvas for user remediation. Restoring the prior
        // canvas would erase the loader the user must correct and retry.
        const modelNameRejected =
          !adopting &&
          error instanceof AppModeError &&
          error.reason?.kind === "queue_rejected" &&
          error.reason.errorTypes.includes("value_not_in_list") &&
          error.reason.classTypes.some((type) =>
            [
              "UNETLoader",
              "CLIPLoader",
              "VAELoader",
              "LoraLoaderModelOnly",
            ].includes(type),
          );
        if (!modelNameRejected) await restoreOwnedGraph();
        if (error instanceof AppModeError) throw error;
        throw new AppModeError(
          "queue_failed",
          "ComfyUI rejected the graph before queue acceptance",
        );
      }
      if (
        queueState !== "not_invoked" &&
        error instanceof AppModeError &&
        error.reason?.kind === "queue_response_invalid"
      )
        throw error;
      if (queueState !== "not_invoked")
        throw new AppModeError(
          "ambiguous_host_ownership",
          "ComfyUI may own the submitted generation; automatic retry is disabled",
        );
      if (readActiveWorkflow(app) !== ownConfigure?.workflow) {
        // A user/host workflow switch is authoritative; never restore into the foreign object.
        if (error instanceof AppModeError && error.code === "stale_graph")
          throw error;
        throw new AppModeError(
          "stale_graph",
          "the active host workflow changed before queue submission",
        );
      }
      await restoreOwnedGraph();
      if (error instanceof AppModeError) {
        if (error.code === "rollback_failed") throw error;
        if (error.code === "stale_graph") throw error;
      }
      throw new AppModeError(
        "queue_failed",
        "the normal ComfyUI queue rejected the graph",
      );
    }
  }
}
