// App Mode contract: constants, exported types, the refusal/error vocabulary and the
// pure validation and identity helpers every App Mode module shares (M23-28 split).

import { sha256Text } from "../contracts/canonicalFingerprint";
import type {
  GenerationAssetSlot,
  GenerationFamilyDisposition,
  GenerationProfile,
  GenerationRemediation,
} from "../contracts/generationProfileCodec";
import { H3_NODE_TYPES } from "./graphAdapter";
import type {
  InputGeometryReceipt,
  InputGeometryRequest,
} from "./inputGeometry";
import type { MaterializationAssetRole } from "./officialAssetResolution";
import type { OwnedGraphReference } from "./ownedGraphIdentity";
import type {
  QueuePromptAcceptedReceipt,
  QueueSeamObservation,
} from "./queueSeam";
import type { ReferenceVideoSoundtrack } from "./templateMaterialization";

export const APP_MODE_WORKFLOW_ID = "h3-context-app-mode-base-v2";

export const APP_MODE_TASK_MODES = [
  "t2va",
  "i2va",
  "fl2va",
  "l2va",
  "ref2va",
] as const;

export const APP_MODE_MIN_FRAME_COUNT = 5;

// The largest producible length, not the host's stated 3600-frame ceiling: a
// derived frame count above this cannot be delivered, so accepting one would
// admit a graph that is going to fail.
export const APP_MODE_MAX_FRAME_COUNT = 3592;

// Bounds on the authored duration, expressed in the units the surface uses. They
// are outer sanity bounds for a host payload, not a restatement of the frame
// lattice: the producible set is decided by the backend alignment authority.
//
// M17-25 distinct review: these were 150 s, taken from the host's 3600-frame
// ceiling divided by 24. That ceiling is not the longest producible video --
// 3593..3600 frames all align to 3609, above it -- so the old bound offered
// about 313 ms of durations that could never resolve. The backend publishes the
// duration domain that does resolve, and these carry it. They are still outer
// bounds and still not the lattice; they are simply no longer wider than the
// thing they bound.
export const APP_MODE_MAX_DURATION_SECONDS = 149.687;

export const MILLISECONDS_PER_SECOND = 1000;

export const APP_MODE_MAX_DURATION_MILLISECONDS = 149_687;

// The pairing the backend derives for the default request, carried so a shell
// with no projection yet can still offer a coherent starting point. It is a
// carried value, not a computation: the duration and the length it produces are
// both decided by `core.length`, and this constant is asserted against it.
export const APP_MODE_DEFAULT_LENGTH = Object.freeze({
  durationMilliseconds: 5167,
  frameCount: 124,
});

export type AppModeTaskMode = (typeof APP_MODE_TASK_MODES)[number];

export const PRODUCTION_CANONICAL_LOWERING_SCHEMA =
  "h3.context.production_canonical_lowering.v1" as const;

export type ProductionCanonicalLowering = Readonly<{
  schema: typeof PRODUCTION_CANONICAL_LOWERING_SCHEMA;
  canonicalPrompt: string;
  baseReportFingerprint: string;
  baseReportRevision: number;
  overrideRevision: number;
  reason: "Materialize approved Production segment prompt";
}>;

export function validateProductionCanonicalLowering(
  value: ProductionCanonicalLowering,
  inputs: AppModeInputs,
): void {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  )
    throw new AppModeError(
      "invalid_request",
      "the prepared canonical prompt authority is invalid",
    );
  const keys = Object.keys(value).sort();
  const expected = [
    "baseReportFingerprint",
    "baseReportRevision",
    "canonicalPrompt",
    "overrideRevision",
    "reason",
    "schema",
  ].sort();
  if (
    keys.length !== expected.length ||
    keys.some((key, index) => key !== expected[index]) ||
    value.schema !== PRODUCTION_CANONICAL_LOWERING_SCHEMA ||
    value.canonicalPrompt !== inputs.user_intent ||
    value.canonicalPrompt.length === 0 ||
    value.canonicalPrompt.length > 4096 ||
    !/^sha256:[0-9a-f]{64}$/.test(value.baseReportFingerprint) ||
    value.baseReportRevision !== 0 ||
    value.overrideRevision !== 1 ||
    value.reason !== "Materialize approved Production segment prompt" ||
    inputs.task_mode !== "t2va"
  )
    throw new AppModeError(
      "invalid_request",
      "the prepared canonical prompt authority is invalid",
    );
}

export const APP_MODE_MODE_CAPABILITIES = [
  {
    schema: "h3.app_mode_capability.v1",
    task_mode: "t2va",
    label: "T2VA",
    required_asset_roles: [],
    asset_cardinality: 0,
    enabled: true,
    disabled_reason: null,
    graph_profile: "h3.non_reference.direct.v1",
    graph_version: 1,
    native_contract: {
      node_type: "MiniMaxH3ImageToVideo",
      media_inputs: [],
    },
    qualified_surfaces: {
      direct_node: "QUALIFIED",
      sidebar_normal_queue: "QUALIFIED",
      headless_api: "QUALIFIED",
      fixed_app_mode: "QUALIFIED",
      fixed_subgraph: "QUALIFIED",
      saved_workflow: "QUALIFIED",
    },
    evidence_revision: "m15-15.direct-use.v1",
    surface_qualified: true,
  },
  {
    schema: "h3.app_mode_capability.v1",
    task_mode: "i2va",
    label: "I2VA",
    required_asset_roles: ["first_frame"],
    asset_cardinality: 1,
    enabled: true,
    disabled_reason: null,
    graph_profile: "h3.non_reference.direct.v1",
    graph_version: 1,
    native_contract: {
      node_type: "MiniMaxH3ImageToVideo",
      media_inputs: ["first_frame"],
    },
    qualified_surfaces: {
      direct_node: "QUALIFIED",
      sidebar_normal_queue: "QUALIFIED",
      headless_api: "QUALIFIED",
      fixed_app_mode: "RETAINED_UNAVAILABLE",
      fixed_subgraph: "RETAINED_UNAVAILABLE",
      saved_workflow: "RETAINED_UNAVAILABLE",
    },
    evidence_revision: "m15-15.direct-use.v1",
    surface_qualified: true,
  },
  {
    schema: "h3.app_mode_capability.v1",
    task_mode: "fl2va",
    label: "FL2VA",
    required_asset_roles: ["first_frame", "last_frame"],
    asset_cardinality: 2,
    enabled: true,
    disabled_reason: null,
    graph_profile: "h3.non_reference.direct.v1",
    graph_version: 1,
    native_contract: {
      node_type: "MiniMaxH3ImageToVideo",
      media_inputs: ["first_frame", "last_frame"],
    },
    qualified_surfaces: {
      direct_node: "QUALIFIED",
      sidebar_normal_queue: "QUALIFIED",
      headless_api: "QUALIFIED",
      fixed_app_mode: "RETAINED_UNAVAILABLE",
      fixed_subgraph: "RETAINED_UNAVAILABLE",
      saved_workflow: "RETAINED_UNAVAILABLE",
    },
    evidence_revision: "m15-15.direct-use.v1",
    surface_qualified: true,
  },
  {
    schema: "h3.app_mode_capability.v1",
    task_mode: "l2va",
    label: "L2VA",
    required_asset_roles: ["last_frame"],
    asset_cardinality: 1,
    enabled: true,
    disabled_reason: null,
    graph_profile: "h3.non_reference.direct.v1",
    graph_version: 1,
    native_contract: {
      node_type: "MiniMaxH3ImageToVideo",
      media_inputs: ["last_frame"],
    },
    qualified_surfaces: {
      direct_node: "QUALIFIED",
      sidebar_normal_queue: "QUALIFIED",
      headless_api: "QUALIFIED",
      fixed_app_mode: "RETAINED_UNAVAILABLE",
      fixed_subgraph: "RETAINED_UNAVAILABLE",
      saved_workflow: "RETAINED_UNAVAILABLE",
    },
    evidence_revision: "m15-15.direct-use.v1",
    surface_qualified: true,
  },
  {
    schema: "h3.app_mode_capability.v1",
    task_mode: "ref2va",
    label: "Ref2VA",
    required_asset_roles: ["reference"],
    asset_cardinality: "1..3",
    enabled: true,
    disabled_reason: null,
    graph_profile: "h3.reference.direct.v1",
    graph_version: 1,
    native_contract: {
      node_type: "MiniMaxH3ReferenceToVideo",
      media_inputs: [
        "ref_images",
        "ref_videos",
        "ref_video_audios",
        "ref_audios",
      ],
    },
    qualified_surfaces: {
      direct_node: "QUALIFIED",
      sidebar_normal_queue: "QUALIFIED",
      headless_api: "QUALIFIED",
      fixed_app_mode: "PARENT_GRAPH_VISIBLE",
      fixed_subgraph: "RETAINED_BOUNDED",
      saved_workflow: "RETAINED_UNAVAILABLE",
    },
    evidence_revision: "m15-16.reference-direct.v1",
    surface_qualified: true,
  },
] as const;

export const {
  request: requestNodeType,
  plan: planNodeType,
  compiler: compilerNodeType,
  auditOverride: auditOverrideNodeType,
  validator: validatorNodeType,
  nativeAdapter: nativeAdapterNodeType,
  productShell: productShellNodeType,
  preview: previewNodeType,
  referenceRegistry: referenceRegistryNodeType,
  imageGeneration: imageGenerationNodeType,
  referenceGeneration: referenceGenerationNodeType,
  getVideoComponents: getVideoComponentsNodeType,
  loadImage: loadImageNodeType,
  loadVideo: loadVideoNodeType,
  loadAudio: loadAudioNodeType,
} = H3_NODE_TYPES;

export const REFERENCE_SOURCE_TYPES = new Set<string>([
  loadImageNodeType,
  loadVideoNodeType,
  loadAudioNodeType,
]);

export type AppModeInputs = {
  task_mode: AppModeTaskMode;
  user_intent: string;
  // M23-04: `duration_milliseconds` is the Sidebar-authored request and is what
  // the shared materialized duration source receives. `frame_count` is the
  // backend-resolved value used for qualification and exact UI disclosure; the
  // native node derives the same value from that source through the pinned
  // official expression. Neither value is computed in frontend code.
  duration_milliseconds: number;
  frame_count: number;
  first_frame_source?: string;
  last_frame_source?: string;
  reference_image_sources?: string[];
  reference_video_sources?: string[];
  reference_audio_sources?: string[];
  /**
   * M17-17: whether the reference videos submit their own soundtrack.
   *
   * The Reference Registry owns the pairing, and this is what tells it. Omitted
   * means `included`, which is the graph every accepted ref2va route produced
   * before this field existed. Admitted only for `ref2va`, and only when at
   * least one reference video is selected -- a soundtrack state with no video to
   * own it is a claim about nothing.
   */
  reference_video_soundtrack?: ReferenceVideoSoundtrack;
};

export type AppModeImageSource = { node_id: string; label: string };

export type AppModeMediaKind = "image" | "video" | "audio";

export type AppModeMediaSource = AppModeImageSource & {
  kind: AppModeMediaKind;
  output_slot: 0;
};

export type AppModeOpaqueSources = {
  first_frame?: Record<string, unknown>;
  last_frame?: Record<string, unknown>;
  reference_images?: Record<string, unknown>[];
  reference_videos?: Record<string, unknown>[];
  reference_audios?: Record<string, unknown>[];
};

export type AppModeStartOptions = {
  /** Explicitly replace an existing canvas after the user chose that action. */
  replaceExisting?: boolean;
  /** Sidebar intent: prepare canvas without generation; entry dispatches prepareCanvas. */
  prepareOnly?: boolean;
  /** Queue the currently visible, compatible H3 graph without replacing it. */
  useExisting?: boolean;
  /**
   * M17-20 D13: splice the context pipeline into the graph already on the
   * canvas, rewiring only the designated native H3 anchor's `prompt`.
   *
   * Choosing this route is the consent: like `replaceExisting`, it mutates a
   * user-assembled canvas and is legal only after the UI has surfaced it and the
   * user has picked it. `anchorNodeId` is mandatory when the graph carries more
   * than one anchor.
   */
  connectExisting?: { anchorNodeId?: number };
  /** Abort local work before the host queue operation has been submitted. */
  signal?: AbortSignal;
  /** Monotonic UI transaction identity used to correlate a projection. */
  transactionId?: number;
  /** Marks the irreversible boundary where host queue ownership begins. */
  onQueueSubmitted?: () => void;
  /** Bounded metadata for the exact queue callable this run invokes. */
  onQueueSeamObserved?: (observation: QueueSeamObservation) => void;
  /**
   * M23-37 (D13): invoked when the one candidate write had to happen before
   * the compile because the candidate carries subgraph definitions that only
   * the host's own load reconciles; the compile that follows is the root
   * compile of the written canvas.
   */
  onHostLoadBeforeCompile?: () => void;
  /** Runner-reserved authority for the exact reproducible Production lowering. */
  preparedCanonicalPrompt?: ProductionCanonicalLowering;
  /** Optional backend-issued identities that must match before host ownership begins. */
  expectedIdentity?: {
    graphFingerprint: string;
    compiledPromptFingerprint: string;
  };
  /**
   * M17-20 D5: the content-free workspace identifier this run's artifact belongs
   * to. It becomes one path segment of the sink prefix, so two workspaces never
   * write into each other's output. Absent, the run is scoped to the session.
   */
  artifactScope?: string;
  /**
   * Establish backend sequence authority from the ProductShell projection of
   * the same prompt after this controller crosses the one native queue boundary.
   */
  prepareManaged?: (
    prepared: ManagedAppModePreflight,
  ) => Promise<ManagedAppModeQueueAuthority>;
  /**
   * Optional, fail-closed execution projection for a managed transaction.
   * The controller derives it independently at final compile and preflight,
   * then queues only the byte-equivalent validated clone.
   */
  projectManagedExecution?: ManagedAppModeExecutionProjector;
  /**
   * M23-18: observe the exact operator-selected I2VA source immediately before
   * managed preparation. The transient locator exists only in this request and
   * never enters the prepared observation or a diagnostic.
   */
  observeSourceIdentity?: (
    request: InputGeometryRequest,
    signal?: AbortSignal,
  ) => Promise<InputGeometryReceipt>;
};

export function validatePreparedCanonicalStart(
  inputs: AppModeInputs,
  options: AppModeStartOptions,
): void {
  const authority = options.preparedCanonicalPrompt;
  if (authority === undefined) return;
  // IMPORTANT: this authority is runner-reserved; accepting it on an adopted
  // canvas or without managed ownership would let ordinary starts bypass the prepared claim.
  if (
    options.prepareManaged === undefined ||
    options.useExisting === true ||
    options.connectExisting !== undefined
  )
    throw new AppModeError(
      "invalid_request",
      "prepared canonical prompts require one managed materialization owner",
    );
  validateProductionCanonicalLowering(authority, inputs);
}

export type AppModeCompiledPrompt = {
  output: Record<string, unknown>;
  workflow: unknown;
};

export type ManagedAppModeExecutionProjector = (
  compiled: AppModeCompiledPrompt,
) => AppModeCompiledPrompt;

export function contentFreeBootstrapWorkflow() {
  // CRITICAL: keep this fresh and mutable; co-installed queue wrappers attach
  // workflow metadata before POST. It must still remain structurally valid and content-free.
  return {
    last_node_id: 0,
    last_link_id: 0,
    nodes: [],
    links: [],
    groups: [],
    config: {},
    extra: {},
    version: 0.4,
  };
}

export const PREPARED_GRAPH_OBSERVATION_SCHEMA =
  "h3.context.prepared_graph_observation.v4" as const;

export type PreparedGraphObservation = Readonly<{
  schema: typeof PREPARED_GRAPH_OBSERVATION_SCHEMA;
  route: "new" | "replace" | "existing";
  graph_fingerprint: string;
  compiled_prompt_fingerprint: string;
  owned_projection_fingerprint: string;
  owned_node_ids: readonly string[];
  owned_link_ids: readonly string[];
  model_fingerprint: string;
  runtime_fingerprint: string;
  fingerprint_domain: "output_producing_graph";
  expected_frames: number;
  source_identity: InputGeometryReceipt | null;
  timeout_ms: number;
  native_anchor_node_id: string;
}>;

export type ManagedAppModePreflight = Readonly<{
  bootstrap: AppModeCompiledPrompt;
  observation: PreparedGraphObservation;
  productShellNodeId: string;
  nativeAnchorNodeId: string;
}>;

export type ManagedAppModeCanvasIdentity = Readonly<{
  workflowAuthority: object;
  ownedReference: OwnedGraphReference;
}>;

export type ManagedAppModePreparation = Readonly<
  ManagedAppModePreflight & { managedIdentity: ManagedAppModeCanvasIdentity }
>;

export type ManagedAppModeQueueFailureDisposition =
  "not_invoked" | "rejected" | "ambiguous";

export type ManagedAppModeQueueAuthority = Readonly<{
  expectedIdentity: Readonly<{
    graphFingerprint: string;
    compiledPromptFingerprint: string;
  }>;
  bindCanvasIdentity?(identity: ManagedAppModeCanvasIdentity): void;
  onQueueSubmitted(): void;
  onQueueAccepted(result: AppModeStartResult): Promise<void> | void;
  onQueueFailed(
    disposition: ManagedAppModeQueueFailureDisposition,
  ): Promise<void> | void;
}>;

export type AppModeCanvasResult = Omit<
  AppModeStartResult,
  "queueResult" | "queuePromptId" | "preparedOnly"
> & { preparedOnly: true };

export type AppModeStartResult = {
  preparedOnly?: false;
  queueResult: QueuePromptAcceptedReceipt;
  transactionId?: number;
  graphFingerprint: string;
  compiledPromptFingerprint: string;
  ownedProjectionFingerprint: string;
  ownedNodeIds: readonly string[];
  ownedLinkIds: readonly string[];
  queuePromptId: string;
  route: "new" | "replace" | "existing" | "connect";
};

/** Remove the pinned frontend's zero-valued optional widget sentinel only. */
export function normalizeAppModeCompiledPrompt(
  compiled: AppModeCompiledPrompt,
): AppModeCompiledPrompt {
  const request = record(compiled.output["1"]);
  const inputs = request === undefined ? undefined : record(request.inputs);
  if (inputs?.duration_seconds !== 0) return compiled;
  const output = { ...compiled.output };
  const normalizedInputs = { ...inputs };
  output["1"] = { ...request, inputs: normalizedInputs };
  delete normalizedInputs.duration_seconds;
  return { ...compiled, output };
}

export function recordManagedExecutionIdentity(
  stage: "final_projection" | "preflight_projection" | "queue_envelope",
  compiledPromptFingerprint: string,
): void {
  const trace = (
    globalThis as typeof globalThis & {
      __h3M2508ExecutionIdentityTrace?: Array<Record<string, unknown>>;
    }
  ).__h3M2508ExecutionIdentityTrace;
  if (!Array.isArray(trace)) return;
  trace.push(Object.freeze({ stage, fingerprint: compiledPromptFingerprint }));
}

type AppModeGraph = {
  serialize?: () => unknown;
  getNodeById?: (id: string | number) => unknown;
  change?: () => unknown;
  setDirtyCanvas?: (foreground: boolean, background?: boolean) => unknown;
};

export type AppModeApp = {
  loadApiJson?: (prompt: Record<string, unknown>, name?: string) => unknown;
  graphToPrompt?: (
    graph?: unknown,
  ) => Promise<AppModeCompiledPrompt> | AppModeCompiledPrompt;
  graph?: AppModeGraph;
  extensionManager?: {
    workflow?: {
      activeWorkflow?: unknown;
      openWorkflows?: unknown;
    };
  };
  loadGraphData?: (
    graph: unknown,
    clean?: boolean,
    restoreView?: boolean,
    workflow?: object | null,
  ) => Promise<unknown> | unknown;
};

export type AppModeDetachedGraphFactory = (serialized: unknown) => unknown;

export type AppModeApi = {
  queuePrompt?: (
    batch: number,
    compiled: AppModeCompiledPrompt,
    options?: unknown,
  ) => Promise<unknown> | unknown;
  /** The host's own asset URL builder, used to reach the served templates. */
  fileURL?: (route: string) => string;
  /** The host's same-origin API seam, used to reach this repository's routes. */
  fetchApi?: (
    path: string,
    init: RequestInit,
  ) => Promise<{ ok: boolean; status: number; text(): Promise<string> }>;
};

export type AppModeCapability =
  | { status: "ready" }
  | {
      status: "unavailable";
      reason:
        | "missing_load_api_json"
        | "missing_graph_to_prompt"
        | "missing_detached_graph_constructor"
        | "missing_queue_prompt"
        | "missing_load_graph_data"
        | "missing_workflow_store";
    };

export type AppModeRefusalReason = Readonly<
  | {
      kind: "anchor_missing";
      requiredNode: "MiniMaxH3ImageToVideo" | "MiniMaxH3ReferenceToVideo";
    }
  | {
      kind: "generation_admission_refused";
      admissionReason: AppModeAdmissionReason;
      unsatisfiedSlots: readonly MaterializationAssetRole[];
    }
  | { kind: "connect_missing_first_frame" }
  | { kind: "connect_missing_last_frame" }
  | { kind: "source_image_changed" }
  | { kind: "template_unavailable" }
  | {
      kind: "queue_rejected";
      classTypes: readonly string[];
      errorTypes: readonly string[];
    }
  | { kind: "queue_response_invalid" }
>;

export const TEMPLATE_UNAVAILABLE_REASON = Object.freeze({
  kind: "template_unavailable",
}) satisfies AppModeRefusalReason;

export const SOURCE_IMAGE_CHANGED_REASON = Object.freeze({
  kind: "source_image_changed",
}) satisfies AppModeRefusalReason;

export function anchorMissingReason(
  taskMode: AppModeTaskMode,
): AppModeRefusalReason {
  return Object.freeze({
    kind: "anchor_missing",
    requiredNode:
      taskMode === "ref2va"
        ? "MiniMaxH3ReferenceToVideo"
        : "MiniMaxH3ImageToVideo",
  });
}

function freezeRefusalReason(
  reason: AppModeRefusalReason,
): AppModeRefusalReason {
  // CRITICAL: only this closed vocabulary may cross into UI state. Never retain
  // host exception text, paths, model names or media identities as refusal data.
  if (reason.kind === "generation_admission_refused")
    return Object.freeze({
      kind: reason.kind,
      admissionReason: reason.admissionReason,
      unsatisfiedSlots: Object.freeze([...reason.unsatisfiedSlots]),
    });
  if (reason.kind === "anchor_missing")
    return Object.freeze({
      kind: reason.kind,
      requiredNode: reason.requiredNode,
    });
  if (reason.kind === "queue_rejected")
    return Object.freeze({
      kind: reason.kind,
      classTypes: Object.freeze([...reason.classTypes]),
      errorTypes: Object.freeze([...reason.errorTypes]),
    });
  return Object.freeze({ kind: reason.kind });
}

export class AppModeError extends Error {
  readonly code:
    | "incompatible_seam"
    | "incompatible_graph"
    | "dirty_graph"
    | "invalid_request"
    | "compile_failed"
    | "queue_failed"
    | "execution_failed"
    | "execution_interrupted"
    | "ambiguous_host_ownership"
    | "cancelled"
    | "stale_graph"
    | "rollback_failed";
  readonly source:
    "request" | "seam" | "graph" | "compile" | "queue" | "transaction";
  readonly recovery: "retry" | "inspect" | "use_native";
  readonly reason?: AppModeRefusalReason;

  constructor(
    code: AppModeError["code"],
    message: string,
    reason?: AppModeRefusalReason,
  ) {
    // Keep provider/graph errors out of the UI and logs; only stable codes and
    // bounded, user-safe messages cross the sidebar seam.
    super(`${code}: ${message}`);
    this.name = "AppModeError";
    this.code = code;
    this.reason =
      reason === undefined ? undefined : freezeRefusalReason(reason);
    this.source =
      code === "incompatible_seam"
        ? "seam"
        : code === "incompatible_graph" || code === "dirty_graph"
          ? "graph"
          : code === "compile_failed"
            ? "compile"
            : code === "queue_failed"
              ? "queue"
              : code === "execution_failed" ||
                  code === "execution_interrupted" ||
                  code === "stale_graph" ||
                  code === "rollback_failed" ||
                  code === "ambiguous_host_ownership"
                ? "transaction"
                : "request";
    this.recovery =
      reason?.kind === "queue_rejected"
        ? "inspect"
        : reason?.kind === "queue_response_invalid"
          ? "use_native"
          : code === "dirty_graph" || code === "incompatible_graph"
            ? "inspect"
            : code === "incompatible_seam" ||
                code === "ambiguous_host_ownership"
              ? "use_native"
              : "retry";
  }
}

export function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

export function graphNodes(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  const graph = record(value);
  return Array.isArray(graph?.nodes) ? graph.nodes : [];
}

export function qualifiedGraphId(
  prefix: string,
  value: string | number,
): string {
  const id = String(value);
  return prefix.length === 0 ? id : `${prefix}:${id}`;
}

export function qualifiedBindingId(
  prefix: string,
  value: string | number,
): string {
  if (String(value) === "-10" || String(value) === "-20") return String(value);
  return qualifiedGraphId(prefix, value);
}

export const serializedIdentifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/;

export function isNodeIdentifier(value: unknown): value is string | number {
  if (typeof value === "number")
    return Number.isSafeInteger(value) && value >= 0;
  return typeof value === "string" && serializedIdentifier.test(value);
}

export function isLink(value: unknown): value is [string | number, number] {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    isNodeIdentifier(value[0]) &&
    Number.isSafeInteger(value[1]) &&
    value[1] >= 0
  );
}

export function isExternalBoundaryLink(
  value: unknown,
  slots: ReadonlySet<number>,
): value is [string, number] {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    (value[0] === "-10" || value[0] === -10) &&
    Number.isSafeInteger(value[1]) &&
    slots.has(value[1] as number)
  );
}

const COMPILED_NODE_KEYS = new Set(["class_type", "inputs", "_meta"]);

const COMPILED_PROMPT_KEYS = new Set(["output", "workflow"]);

export function isSafeCompiledPromptEnvelope(
  value: unknown,
): value is AppModeCompiledPrompt {
  const compiled = record(value);
  return (
    compiled !== undefined &&
    Object.keys(compiled).every((key) => COMPILED_PROMPT_KEYS.has(key)) &&
    Object.prototype.hasOwnProperty.call(compiled, "output") &&
    record(compiled.output) !== undefined &&
    Object.prototype.hasOwnProperty.call(compiled, "workflow")
  );
}

export function isSafeCompiledNodeEnvelope(
  node: Record<string, unknown>,
): boolean {
  // IMPORTANT: ComfyUI's public API envelope includes only class_type, inputs,
  // and the presentation-only _meta.title. Rejecting other members keeps
  // private/unknown payloads out of the queue-boundary contract.
  if (
    !Object.prototype.hasOwnProperty.call(node, "class_type") ||
    !Object.prototype.hasOwnProperty.call(node, "inputs")
  )
    return false;
  if (
    Object.keys(node).some(
      (key) =>
        !COMPILED_NODE_KEYS.has(key) ||
        key === "__proto__" ||
        key === "prototype" ||
        key === "constructor",
    )
  )
    return false;
  if (typeof node.class_type !== "string" || record(node.inputs) === undefined)
    return false;
  if (node._meta !== undefined) {
    const meta = record(node._meta);
    if (
      meta === undefined ||
      Object.keys(meta).some((key) => key !== "title") ||
      (meta.title !== undefined && typeof meta.title !== "string")
    )
      return false;
  }
  return true;
}

export function compiledOutputNodes(
  compiled: AppModeCompiledPrompt,
): Array<{ id: string; node: Record<string, unknown> }> {
  const compiledRecord = record(compiled);
  const output = record(compiledRecord?.output);
  if (output === undefined) return [];
  const nodes: Array<{ id: string; node: Record<string, unknown> }> = [];
  for (const [id, value] of Object.entries(output)) {
    if (!serializedIdentifier.test(id)) return [];
    const node = record(value);
    if (node === undefined || !isSafeCompiledNodeEnvelope(node)) return [];
    nodes.push({ id, node });
  }
  return nodes;
}

export type CompiledPromptRoute =
  "materialize" | "prepared" | "existing" | "connect";

/** The exact existing-canvas projection resolved before detached compilation. */
export type ExistingCompiledAdmission = Readonly<{
  requestNodeId: string;
  durationSourceNodeId: string;
  visibleAnchorNodeId: string;
  anchorNodeId: string;
  anchorNodeType: string;
  productShellNodeId: string;
}>;

/**
 * Validate the compiled public graph before binding App Mode to an existing canvas.
 * A single ProductShell node is not an H3 flow; require the canonical producer chain
 * and a native generation anchor so malformed or boundary-only graphs fail closed.
 */
export function isBindableDurationSeconds(value: unknown): boolean {
  return (
    value === undefined ||
    isLink(value) ||
    (typeof value === "number" &&
      Number.isFinite(value) &&
      value >= 0 &&
      value <= APP_MODE_MAX_DURATION_SECONDS)
  );
}

function validateCommonInputs(inputs: AppModeInputs): void {
  if (!APP_MODE_TASK_MODES.includes(inputs.task_mode))
    throw new AppModeError(
      "invalid_request",
      "the requested App Mode route is unavailable",
    );
  if (
    typeof inputs.user_intent !== "string" ||
    inputs.user_intent.trim().length === 0 ||
    inputs.user_intent.length > 4096
  )
    throw new AppModeError(
      "invalid_request",
      "intent must be non-empty and at most 4096 characters",
    );
  if (
    !Number.isInteger(inputs.duration_milliseconds) ||
    inputs.duration_milliseconds <= 0 ||
    inputs.duration_milliseconds > APP_MODE_MAX_DURATION_MILLISECONDS
  )
    throw new AppModeError(
      "invalid_request",
      "duration is outside the canonical bounded range",
    );
  // The frame count is not authored here; it is the value the backend derived
  // from the same duration, and materialization only checks that it is a length
  // the host can accept before writing it into the native node.
  if (
    !Number.isInteger(inputs.frame_count) ||
    inputs.frame_count < APP_MODE_MIN_FRAME_COUNT ||
    inputs.frame_count > APP_MODE_MAX_FRAME_COUNT
  )
    throw new AppModeError(
      "invalid_request",
      "derived frame count is outside the canonical bounded range",
    );
}

/** Validate a request that will materialize repository-owned media bindings. */
export function validateInputs(inputs: AppModeInputs): void {
  validateCommonInputs(inputs);
  const first = inputs.first_frame_source;
  const last = inputs.last_frame_source;
  const validSource = (value: unknown): value is string =>
    typeof value === "string" && serializedIdentifier.test(value);
  const validSources = (value: unknown, maximum: number): value is string[] =>
    value === undefined ||
    (Array.isArray(value) &&
      value.length <= maximum &&
      value.every(validSource) &&
      new Set(value).size === value.length);
  const referenceImages = inputs.reference_image_sources;
  const referenceVideos = inputs.reference_video_sources;
  const referenceAudios = inputs.reference_audio_sources;
  const referenceCount =
    (referenceImages?.length ?? 0) +
    (referenceVideos?.length ?? 0) +
    (referenceAudios?.length ?? 0);
  const hasReferenceFields =
    referenceImages !== undefined ||
    referenceVideos !== undefined ||
    referenceAudios !== undefined;
  if (
    (inputs.task_mode === "i2va" &&
      (!validSource(first) || last !== undefined)) ||
    (inputs.task_mode === "l2va" &&
      (!validSource(last) || first !== undefined)) ||
    (inputs.task_mode === "fl2va" &&
      (!validSource(first) || !validSource(last) || first === last)) ||
    (inputs.task_mode === "t2va" &&
      (first !== undefined || last !== undefined)) ||
    (inputs.task_mode === "ref2va" &&
      (first !== undefined ||
        last !== undefined ||
        !validSources(referenceImages, 1) ||
        !validSources(referenceVideos, 1) ||
        !validSources(referenceAudios, 1) ||
        referenceCount < 1)) ||
    (inputs.task_mode !== "ref2va" && hasReferenceFields) ||
    !validSoundtrack(inputs)
  )
    throw new AppModeError(
      "invalid_request",
      "the selected mode requires exact visible source roles and bounded cardinality",
    );
}

/**
 * Validate the connect route's closed request shape.
 *
 * IMPORTANT: media belongs to the foreign canvas on this route. Accepting a
 * caller-supplied source id here would create a second, hidden media authority
 * even though the splice deliberately leaves the user's media wiring intact.
 */
export function validateConnectInputs(inputs: AppModeInputs): void {
  validateCommonInputs(inputs);
  if (
    inputs.first_frame_source !== undefined ||
    inputs.last_frame_source !== undefined ||
    inputs.reference_image_sources !== undefined ||
    inputs.reference_video_sources !== undefined ||
    inputs.reference_audio_sources !== undefined ||
    inputs.reference_video_soundtrack !== undefined
  )
    throw new AppModeError(
      "invalid_request",
      "connect inputs must leave media ownership with the current canvas",
    );
}

/** Validate sidebar fields without claiming ownership of existing media wiring. */
export function validateExistingInputs(inputs: AppModeInputs): void {
  validateCommonInputs(inputs);
}

/**
 * The declared soundtrack state, or the default that preserves accepted routes.
 *
 * M17-17. `included` is the default because it is what every accepted ref2va
 * route already produced; the correction declares that state rather than
 * changing it.
 */
export function referenceVideoSoundtrackOf(
  inputs: AppModeInputs,
): ReferenceVideoSoundtrack {
  return inputs.reference_video_soundtrack ?? "included";
}

/** How many `ref_video_audio_*` bindings the declared state admits. */
export function declaredSoundtrackCount(inputs: AppModeInputs): number {
  return referenceVideoSoundtrackOf(inputs) === "included"
    ? (inputs.reference_video_sources?.length ?? 0)
    : 0;
}

function validSoundtrack(inputs: AppModeInputs): boolean {
  const declared = inputs.reference_video_soundtrack;
  if (declared === undefined) return true;
  return (
    (declared === "included" ||
      declared === "excluded" ||
      declared === "unavailable") &&
    inputs.task_mode === "ref2va" &&
    (inputs.reference_video_sources?.length ?? 0) > 0
  );
}

export function throwIfCancelled(signal: AbortSignal | undefined): void {
  if (signal?.aborted)
    throw new AppModeError(
      "cancelled",
      "App Mode was cancelled before queueing",
    );
}

export function snapshotGraph(value: unknown): unknown {
  if (value === undefined) return undefined;
  try {
    return structuredClone(value);
  } catch {
    // IMPORTANT: JSON fallback can silently drop functions, cycles, or host
    // object identity and would make rollback appear successful while
    // restoring a different graph. Fail closed until the host supplies a
    // cloneable public snapshot.
    return undefined;
  }
}

export function fingerprint(value: unknown): string {
  let serialized = "";
  try {
    serialized = JSON.stringify(value) ?? "";
  } catch {
    // CRITICAL: a shared cycle/error sentinel would make distinct graph
    // revisions compare equal and could admit stale work at the queue seam.
    throw new Error("graph identity is not canonically serializable");
  }
  if (serialized.length === 0 && value !== "")
    throw new Error("graph identity is not canonically serializable");
  // Fingerprints are correlation metadata only; never retain or display the
  // serialized graph/prompt itself. Hash the complete payload so two large
  // values cannot collapse to one sentinel identity.
  return sha256Text(serialized);
}

/** Whole serialized-canvas digest retained only as D12 surroundings evidence. */
export function graphFingerprint(value: unknown): string {
  return fingerprint(value);
}

/**
 * The official duration-to-length expression, pinned by content.
 *
 * M17-25 proved this expression equals `core/length.py` over every integer
 * millisecond of the accepted domain. Recognising it here is what lets App Mode
 * admit a `length` that arrives as a link instead of a number without restating
 * the lattice: the canvas is running the same rule, and this asserts that it is
 * still that rule and not something a later template edit replaced it with.
 */
/**
 * The root every materialized artifact is written under.
 *
 * M17-20 D5. The pinned templates all ship `video/MiniMax_H3`, so two revisions
 * of the same segment would write into one place and a regeneration would be
 * indistinguishable from the run it replaced. This repository writes its own
 * root instead, and the prefix below adds the scope and the revision identity.
 */
export const APP_MODE_ARTIFACT_PREFIX_ROOT = "video/h3-context";

const artifactScopePattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

const APP_MODE_ARTIFACT_REVISION_LENGTH = 16;

/**
 * The deterministic, content-free output location for one authored revision.
 *
 * Every component is either a fixed literal, a validated identifier, or a
 * digest: no intent text, no filename and no host path can reach the sink
 * widget through here. Determinism is the point -- the same authored revision
 * resolves to the same location, and any edit to it resolves elsewhere, which is
 * what stops a regeneration from overwriting the artifact of the revision it
 * replaced.
 */
export function appModeArtifactPrefix(
  inputs: AppModeInputs,
  scope?: string,
): string {
  const scoped =
    typeof scope === "string" && artifactScopePattern.test(scope)
      ? scope
      : "session";
  const revision = fingerprint({
    task_mode: inputs.task_mode,
    user_intent: inputs.user_intent,
    duration_milliseconds: inputs.duration_milliseconds,
    frame_count: inputs.frame_count,
    first_frame_source: inputs.first_frame_source ?? null,
    last_frame_source: inputs.last_frame_source ?? null,
    reference_image_sources: inputs.reference_image_sources ?? [],
    reference_video_sources: inputs.reference_video_sources ?? [],
    reference_audio_sources: inputs.reference_audio_sources ?? [],
    // M17-17: excluding a reference video's soundtrack is a different request,
    // so it must not resolve to the location the included revision wrote to.
    reference_video_soundtrack: referenceVideoSoundtrackOf(inputs),
  }).slice(-APP_MODE_ARTIFACT_REVISION_LENGTH);
  return `${APP_MODE_ARTIFACT_PREFIX_ROOT}/${scoped}/${revision}`;
}

/**
 * Accept a `length` the template drives through the official expression.
 *
 * The pinned templates do not put a frame count on the anchor: they compute it
 * from an authored duration with the official rounding expression. So the check
 * is not "is this integer producible" -- there is no integer -- but "is this
 * still the official derivation, fed by a duration inside the accepted domain".
 */
/**
 * How App Mode reaches the backend's capability projection.
 *
 * Injectable for the same reason the template loader is: the seam belongs to the
 * host, and a test that had to stand up a route to exercise a refusal would be
 * testing the route rather than the refusal.
 */
export type AppModeProfileLoader = (
  signal?: AbortSignal,
) => Promise<GenerationProfile> | GenerationProfile;

/**
 * Why generation was refused, in the projection's own vocabulary where it has
 * one. `profile_unavailable` and `unsupported_task_mode` are the two answers the
 * projection cannot give: the first is a host that could not be asked, the
 * second a host whose answer does not cover the requested mode. Both mean this
 * shell has not qualified this host, which is what `upgrade_host` says.
 */
export type AppModeAdmissionReason =
  GenerationFamilyDisposition | "profile_unavailable" | "unsupported_task_mode";

export type AppModeAdmission =
  | Readonly<{ status: "admitted" }>
  | Readonly<{
      status: "refused";
      reason: AppModeAdmissionReason;
      remediation: GenerationRemediation;
      unsatisfiedSlots: readonly GenerationAssetSlot[];
    }>;
