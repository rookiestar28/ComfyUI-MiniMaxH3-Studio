import {
  executionCorrelationKeys as correlationKeys,
  segmentDurationKeys as durationKeys,
} from "./generatedSurface";

export const PRODUCTION_ACTION_SCHEMA =
  "h3.context.production_workbench.action.v1" as const;
export const PRODUCTION_PROJECTION_SCHEMA =
  "h3.context.production_workbench.projection.v1" as const;
export const PRODUCTION_ACCUMULATION_ACTION_VERSION =
  "h3.context.production_accumulation.v1" as const;

export type ProductionAction =
  | "create_workspace_from_context"
  | "add_segment_from_context"
  | "replace_segment_from_context"
  | "set_segment_relation"
  | "delete_segment"
  | "reorder_segments"
  | "set_selection"
  | "read_projection"
  | "release_workspace"
  | "assemble_sequence"
  | "cancel_assembly"
  | "retry_assembly";
export type ProductionAllowedAction =
  | ProductionAction
  | "submit_generation_job"
  | "preview_output"
  | "import_production_outputs_to_authoring";
export type SegmentRelation =
  "independent" | "predecessor" | "adjacent_pair" | "cut" | "reset";

export type ProductionSegment = Readonly<{
  segmentId: string;
  ordinal: number;
  taskMode: "t2va" | "i2va" | "fl2va" | "l2va" | "ref2va";
  // M17-25: the authored value is a duration in integer milliseconds; the frame
  // count and the delivered duration are derived by the backend from the single
  // alignment authority. The frontend renders them and never recomputes them.
  duration: Readonly<{
    requestedMilliseconds: number;
    deliveredMilliseconds: number;
    frameCount: number;
    snapped: boolean;
  }>;
  relation: SegmentRelation;
  predecessorSegmentId: string | null;
  boundaryKind: "independent" | "native_handoff" | "adjacent" | "cut" | "reset";
  closureState:
    | "unavailable"
    | "clean"
    | "dirty_self"
    | "dirty_upstream"
    | "blocked_missing_predecessor"
    | "requires_full_recompute";
  jobState:
    | "unavailable"
    | "clean"
    | "planned"
    | "projected"
    | "submitted"
    | "running"
    | "output_verification_failed"
    | "succeeded"
    | "failed"
    | "timed_out"
    | "cancelled"
    | "unknown_ownership";
  artifactState: "unavailable" | "partial" | "complete" | "failed";
  continuityState: "unavailable" | "cut" | "restart" | "native_frame_handoff";
  deliveredGeometry: Readonly<{
    format: "mp4" | "mkv" | "webm";
    frameCount: number;
    width: number;
    height: number;
  }> | null;
}>;

export type ProductionOutput = Readonly<{
  outputHandle: string;
  ordinal: number;
  state: "pending" | "ready" | "failed" | "unavailable";
  segmentId: string | null;
  preview: boolean;
}>;

export type ProductionGenerationSequenceSummary = Readonly<{
  schema: "h3.context.generation_sequence_projection.v1";
  sequenceId: string;
  sequenceFingerprint: string;
  stateFingerprint: string;
  workspaceId: string;
  workspaceRevision: number;
  workspaceFingerprint: string;
  correlation: Readonly<{ promptId: string; executionNodeId: string }>;
}>;

export type ProductionAssemblyProjection = Readonly<{
  schema: "h3.context.production_assembly.projection.v1";
  state:
    | "unavailable"
    | "planned"
    | "running"
    | "cancelling"
    | "succeeded"
    | "failed";
  progress: Readonly<{ completed: number; total: number }>;
  capabilityFingerprint: string | null;
  managedSequenceFingerprint: string | null;
  artifactReceiptFingerprints: readonly string[];
  cutBoundaryReceiptFingerprints: readonly string[];
  outputProfileId: "legacy_av_30fps_48khz_stereo";
  assemblyJobId: string | null;
  authorizationFingerprint: string | null;
  receiptFingerprint: string | null;
  failureCode: string | null;
  projectionFingerprint: string;
}>;

export type ProductionWorkbenchProjection = Readonly<{
  schema: typeof PRODUCTION_PROJECTION_SCHEMA;
  workspaceHandle: string;
  workspaceId: string;
  workspaceRevision: number;
  workspaceFingerprint: string;
  segments: readonly ProductionSegment[];
  selectedSegmentIds: readonly string[];
  runState:
    "unavailable" | "ready" | "running" | "succeeded" | "failed" | "cancelled";
  runProgress: Readonly<{ completed: number; total: number }>;
  generationSequence: ProductionGenerationSequenceSummary | null;
  reconstructionState: "unavailable" | "complete";
  assembly: ProductionAssemblyProjection;
  authorityVersions: readonly (
    | "h3.context.generation_sequence_projection.v1"
    | "h3.context.segment_artifact_receipt.v1"
    | "h3.context.continuity_boundary_receipt.v1"
    | "h3.context.av_reconstruction_receipt.v1"
    | "h3.context.m26.production_assembly_receipt.v1"
  )[];
  outputs: readonly ProductionOutput[];
  allowedActions: readonly ProductionAllowedAction[];
  blockerCodes: readonly string[];
}>;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const workspaceHandle = /^pw_[A-Za-z0-9_-]{32,96}$/;
const outputHandle = /^out_[A-Za-z0-9_-]{16,96}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const taskModes = new Set(["t2va", "i2va", "fl2va", "l2va", "ref2va"]);
const relations = new Set<SegmentRelation>([
  "independent",
  "predecessor",
  "adjacent_pair",
  "cut",
  "reset",
]);
const boundaries = new Set([
  "independent",
  "native_handoff",
  "adjacent",
  "cut",
  "reset",
]);
const boundaryByRelation: Readonly<
  Record<SegmentRelation, ProductionSegment["boundaryKind"]>
> = {
  independent: "independent",
  predecessor: "native_handoff",
  adjacent_pair: "adjacent",
  cut: "cut",
  reset: "reset",
};
const runStates = new Set([
  "unavailable",
  "ready",
  "running",
  "succeeded",
  "failed",
  "cancelled",
]);
const outputStates = new Set(["pending", "ready", "failed", "unavailable"]);
const closureStates = new Set([
  "unavailable",
  "clean",
  "dirty_self",
  "dirty_upstream",
  "blocked_missing_predecessor",
  "requires_full_recompute",
]);
const jobStates = new Set([
  "unavailable",
  "clean",
  "planned",
  "projected",
  "submitted",
  "running",
  "output_verification_failed",
  "succeeded",
  "failed",
  "timed_out",
  "cancelled",
  "unknown_ownership",
]);
const artifactStates = new Set([
  "unavailable",
  "partial",
  "complete",
  "failed",
]);
const continuityStates = new Set([
  "unavailable",
  "cut",
  "restart",
  "native_frame_handoff",
]);
const reconstructionStates = new Set(["unavailable", "complete"]);
const assemblyStates = new Set([
  "unavailable",
  "planned",
  "running",
  "cancelling",
  "succeeded",
  "failed",
]);
const authorityVersions = new Set([
  "h3.context.generation_sequence_projection.v1",
  "h3.context.segment_artifact_receipt.v1",
  "h3.context.continuity_boundary_receipt.v1",
  "h3.context.av_reconstruction_receipt.v1",
  "h3.context.m26.production_assembly_receipt.v1",
]);
const allowedActions = new Set<ProductionAllowedAction>([
  "add_segment_from_context",
  "replace_segment_from_context",
  "set_segment_relation",
  "delete_segment",
  "reorder_segments",
  "set_selection",
  "read_projection",
  "release_workspace",
  "submit_generation_job",
  "preview_output",
  "assemble_sequence",
  "cancel_assembly",
  "retry_assembly",
  "import_production_outputs_to_authoring",
]);

function object(value: unknown, keys: readonly string[], name: string) {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} must be an object`);
  const wire = value as Record<string, unknown>;
  const actual = Object.keys(wire).sort();
  const expected = [...keys].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected))
    throw new Error(`${name} must be closed`);
  return wire;
}

function exactString(value: unknown, pattern: RegExp, name: string): string {
  if (typeof value !== "string" || !pattern.test(value))
    throw new Error(`${name} is invalid`);
  return value;
}

function exactInteger(value: unknown, min: number, max: number, name: string) {
  if (
    !Number.isInteger(value) ||
    (value as number) < min ||
    (value as number) > max
  )
    throw new Error(`${name} is invalid`);
  return value as number;
}

// M17-25. The frontend validates structure and internal consistency, never the
// lattice: the accepted range and the 17k+5 rule live in `core.length` alone, and
// restating either here would recreate the second authority this item removed.
// What is checkable without them is that the snapped flag agrees with the two
// durations, which is enough to reject a payload that claims a segment moved when
// it did not, or that it did not when it moved.
function decodeDuration(duration: Record<string, unknown>) {
  const requestedMilliseconds = exactInteger(
    duration.duration_milliseconds,
    1,
    86_400_000,
    "duration requested milliseconds",
  );
  const deliveredMilliseconds = exactInteger(
    duration.delivered_milliseconds,
    1,
    86_400_000,
    "duration delivered milliseconds",
  );
  const frameCount = exactInteger(
    duration.frame_count,
    1,
    // The host's stated frame ceiling, matching `generationSequenceCodec`. It is
    // an outer sanity bound rather than a producibility test -- that is the
    // backend's -- but it must not be a day of milliseconds, which is what this
    // used to be and what made the two sibling codecs disagree about the same
    // field.
    3_600,
    "duration frame count",
  );
  if (duration.snapped !== (requestedMilliseconds !== deliveredMilliseconds))
    throw new Error(
      "duration snapped flag does not match the delivered duration",
    );
  return {
    requestedMilliseconds,
    deliveredMilliseconds,
    frameCount,
    snapped: duration.snapped as boolean,
  };
}

function uniqueStrings(
  value: unknown,
  maximum: number,
  name: string,
): readonly string[] {
  if (!Array.isArray(value) || value.length > maximum)
    throw new Error(`${name} exceeds limit`);
  const result = value.map((item, index) =>
    exactString(item, identifier, `${name}[${index}]`),
  );
  if (new Set(result).size !== result.length)
    throw new Error(`${name} contains duplicate values`);
  return Object.freeze(result);
}

function uniqueFingerprints(
  value: unknown,
  maximum: number,
  name: string,
): readonly string[] {
  if (!Array.isArray(value) || value.length > maximum)
    throw new Error(`${name} exceeds limit`);
  const result = value.map((item, index) =>
    exactString(item, fingerprint, `${name}[${index}]`),
  );
  if (new Set(result).size !== result.length)
    throw new Error(`${name} contains duplicate values`);
  return Object.freeze(result);
}

function nullableString(
  value: unknown,
  pattern: RegExp,
  name: string,
): string | null {
  return value === null ? null : exactString(value, pattern, name);
}

function decodeSegment(
  value: unknown,
  expectedOrdinal: number,
): ProductionSegment {
  const wire = object(
    value,
    [
      "segment_id",
      "ordinal",
      "task_mode",
      "duration",
      "relation",
      "predecessor_segment_id",
      "boundary_kind",
      "closure_state",
      "job_state",
      "artifact_state",
      "continuity_state",
      "delivered_geometry",
    ],
    "production segment",
  );
  const duration = object(wire.duration, durationKeys, "segment duration");
  if (typeof duration.snapped !== "boolean")
    throw new Error("segment duration snapped flag is invalid");
  const relation = wire.relation;
  const taskMode = wire.task_mode;
  const boundary = wire.boundary_kind;
  if (
    typeof relation !== "string" ||
    !relations.has(relation as SegmentRelation)
  )
    throw new Error("segment relation is invalid");
  if (typeof taskMode !== "string" || !taskModes.has(taskMode))
    throw new Error("segment task mode is invalid");
  if (typeof boundary !== "string" || !boundaries.has(boundary))
    throw new Error("segment boundary is invalid");
  const predecessor = wire.predecessor_segment_id;
  if (predecessor !== null)
    exactString(predecessor, identifier, "segment predecessor");
  const requiresPredecessor =
    relation === "predecessor" || relation === "adjacent_pair";
  if (requiresPredecessor !== (predecessor !== null))
    throw new Error("segment predecessor does not match relation");
  if (boundary !== boundaryByRelation[relation as SegmentRelation])
    throw new Error("segment boundary does not match relation");
  if (
    typeof wire.closure_state !== "string" ||
    !closureStates.has(wire.closure_state)
  )
    throw new Error("segment closure state is invalid");
  if (typeof wire.job_state !== "string" || !jobStates.has(wire.job_state))
    throw new Error("segment job state is invalid");
  if (
    typeof wire.artifact_state !== "string" ||
    !artifactStates.has(wire.artifact_state)
  )
    throw new Error("segment artifact state is invalid");
  if (
    typeof wire.continuity_state !== "string" ||
    !continuityStates.has(wire.continuity_state)
  )
    throw new Error("segment continuity state is invalid");
  const decodedDuration = Object.freeze(decodeDuration(duration));
  let deliveredGeometry: ProductionSegment["deliveredGeometry"] = null;
  if (wire.delivered_geometry !== null) {
    const delivered = object(
      wire.delivered_geometry,
      ["format", "frame_count", "width", "height"],
      "segment delivered geometry",
    );
    if (
      typeof delivered.format !== "string" ||
      !new Set(["mp4", "mkv", "webm"]).has(delivered.format) ||
      wire.artifact_state !== "complete"
    )
      throw new Error("segment delivered geometry is invalid");
    const frameCount = exactInteger(
      delivered.frame_count,
      1,
      512,
      "segment delivered frame count",
    );
    if (frameCount !== decodedDuration.frameCount)
      throw new Error("segment delivered frame count is inconsistent");
    deliveredGeometry = Object.freeze({
      format: delivered.format as "mp4" | "mkv" | "webm",
      frameCount,
      width: exactInteger(
        delivered.width,
        64,
        8_192,
        "segment delivered width",
      ),
      height: exactInteger(
        delivered.height,
        64,
        8_192,
        "segment delivered height",
      ),
    });
  }
  return Object.freeze({
    segmentId: exactString(wire.segment_id, identifier, "segment id"),
    ordinal: exactInteger(
      wire.ordinal,
      expectedOrdinal,
      expectedOrdinal,
      "segment ordinal",
    ),
    taskMode: taskMode as ProductionSegment["taskMode"],
    duration: decodedDuration,
    relation: relation as SegmentRelation,
    predecessorSegmentId: predecessor as string | null,
    boundaryKind: boundary as ProductionSegment["boundaryKind"],
    closureState: wire.closure_state as ProductionSegment["closureState"],
    jobState: wire.job_state as ProductionSegment["jobState"],
    artifactState: wire.artifact_state as ProductionSegment["artifactState"],
    continuityState:
      wire.continuity_state as ProductionSegment["continuityState"],
    deliveredGeometry,
  });
}

function decodeAssembly(value: unknown): ProductionAssemblyProjection {
  const wire = object(
    value,
    [
      "schema",
      "state",
      "progress",
      "capability_fingerprint",
      "managed_sequence_fingerprint",
      "artifact_receipt_fingerprints",
      "cut_boundary_receipt_fingerprints",
      "output_profile_id",
      "assembly_job_id",
      "authorization_fingerprint",
      "receipt_fingerprint",
      "failure_code",
      "projection_fingerprint",
    ],
    "production assembly",
  );
  if (wire.schema !== "h3.context.production_assembly.projection.v1")
    throw new Error("production assembly schema is invalid");
  if (typeof wire.state !== "string" || !assemblyStates.has(wire.state))
    throw new Error("production assembly state is invalid");
  const progress = object(
    wire.progress,
    ["completed", "total"],
    "production assembly progress",
  );
  const total = exactInteger(progress.total, 0, 64, "assembly total");
  const completed = exactInteger(
    progress.completed,
    0,
    total,
    "assembly completed",
  );
  const capabilityFingerprint = nullableString(
    wire.capability_fingerprint,
    fingerprint,
    "assembly capability fingerprint",
  );
  const managedSequenceFingerprint = nullableString(
    wire.managed_sequence_fingerprint,
    fingerprint,
    "assembly managed sequence fingerprint",
  );
  const artifactReceiptFingerprints = uniqueFingerprints(
    wire.artifact_receipt_fingerprints,
    64,
    "assembly artifact receipt fingerprints",
  );
  const cutBoundaryReceiptFingerprints = uniqueFingerprints(
    wire.cut_boundary_receipt_fingerprints,
    63,
    "assembly cut boundary receipt fingerprints",
  );
  if (wire.output_profile_id !== "legacy_av_30fps_48khz_stereo")
    throw new Error("production assembly output profile is invalid");
  const assemblyJobId = nullableString(
    wire.assembly_job_id,
    identifier,
    "assembly job id",
  );
  const authorizationFingerprint = nullableString(
    wire.authorization_fingerprint,
    fingerprint,
    "assembly authorization fingerprint",
  );
  const receiptFingerprint = nullableString(
    wire.receipt_fingerprint,
    fingerprint,
    "assembly receipt fingerprint",
  );
  const failureCode = nullableString(
    wire.failure_code,
    identifier,
    "assembly failure code",
  );
  const state = wire.state as ProductionAssemblyProjection["state"];
  if (capabilityFingerprint === null) {
    if (
      state !== "unavailable" ||
      managedSequenceFingerprint !== null ||
      artifactReceiptFingerprints.length !== 0 ||
      cutBoundaryReceiptFingerprints.length !== 0 ||
      assemblyJobId !== null ||
      authorizationFingerprint !== null ||
      receiptFingerprint !== null ||
      failureCode === null ||
      completed !== 0 ||
      total !== 0
    )
      throw new Error("production assembly unavailable state is invalid");
  } else {
    if (
      managedSequenceFingerprint === null ||
      artifactReceiptFingerprints.length === 0 ||
      total !== artifactReceiptFingerprints.length ||
      cutBoundaryReceiptFingerprints.length !==
        artifactReceiptFingerprints.length - 1
    )
      throw new Error("production assembly predecessors are invalid");
    if (state === "unavailable") {
      if (
        assemblyJobId !== null ||
        authorizationFingerprint !== null ||
        receiptFingerprint !== null ||
        failureCode !== null ||
        completed !== 0
      )
        throw new Error("production assembly ready state is invalid");
    } else if (
      assemblyJobId === null ||
      authorizationFingerprint === null ||
      total === 0
    ) {
      throw new Error("production assembly active state is invalid");
    }
  }
  if (
    state === "succeeded" &&
    (receiptFingerprint === null || failureCode !== null || completed !== total)
  )
    throw new Error("production assembly success is invalid");
  if (
    state === "failed" &&
    (failureCode === null || receiptFingerprint !== null)
  )
    throw new Error("production assembly failure is invalid");
  if (
    new Set(["planned", "running", "cancelling"]).has(state) &&
    (failureCode !== null || receiptFingerprint !== null)
  )
    throw new Error("production assembly active state is invalid");
  return Object.freeze({
    schema: "h3.context.production_assembly.projection.v1",
    state,
    progress: Object.freeze({ completed, total }),
    capabilityFingerprint,
    managedSequenceFingerprint,
    artifactReceiptFingerprints,
    cutBoundaryReceiptFingerprints,
    outputProfileId: "legacy_av_30fps_48khz_stereo",
    assemblyJobId,
    authorizationFingerprint,
    receiptFingerprint,
    failureCode,
    projectionFingerprint: exactString(
      wire.projection_fingerprint,
      fingerprint,
      "assembly projection fingerprint",
    ),
  });
}

export function decodeProductionWorkbenchProjection(
  value: unknown,
): ProductionWorkbenchProjection {
  const wire = object(
    value,
    [
      "schema",
      "workspace_handle",
      "workspace_id",
      "workspace_revision",
      "workspace_fingerprint",
      "segments",
      "selected_segment_ids",
      "run",
      "generation_sequence",
      "reconstruction",
      "assembly",
      "authority_versions",
      "outputs",
      "allowed_actions",
      "blocker_codes",
      "limits",
    ],
    "production projection",
  );
  if (wire.schema !== PRODUCTION_PROJECTION_SCHEMA)
    throw new Error("production projection schema is unsupported");
  const decodedWorkspaceId = exactString(
    wire.workspace_id,
    identifier,
    "workspace id",
  );
  const decodedWorkspaceRevision = exactInteger(
    wire.workspace_revision,
    1,
    1_000_000,
    "workspace revision",
  );
  const decodedWorkspaceFingerprint = exactString(
    wire.workspace_fingerprint,
    fingerprint,
    "workspace fingerprint",
  );
  if (
    !Array.isArray(wire.segments) ||
    wire.segments.length < 1 ||
    wire.segments.length > 64
  )
    throw new Error("production segment limit is invalid");
  const segments = Object.freeze(
    wire.segments.map((segment, index) => decodeSegment(segment, index + 1)),
  );
  if (
    new Set(segments.map((segment) => segment.segmentId)).size !==
    segments.length
  )
    throw new Error("production segments contain duplicate ids");
  for (const segment of segments) {
    if (
      segment.predecessorSegmentId !== null &&
      (!segments.some(
        (candidate) => candidate.segmentId === segment.predecessorSegmentId,
      ) ||
        segment.predecessorSegmentId === segment.segmentId)
    )
      throw new Error("segment predecessor is unknown or self-referential");
  }
  const selected = uniqueStrings(
    wire.selected_segment_ids,
    64,
    "selected segment ids",
  );
  if (
    !selected.every((id) =>
      segments.some((segment) => segment.segmentId === id),
    )
  )
    throw new Error("selected segment id is unknown");
  const run = object(
    wire.run,
    ["state", "completed", "total"],
    "production run",
  );
  if (typeof run.state !== "string" || !runStates.has(run.state))
    throw new Error("production run state is invalid");
  const total = exactInteger(run.total, 0, 64, "production run total");
  const completed = exactInteger(
    run.completed,
    0,
    total,
    "production run completed",
  );
  let generationSequence: ProductionGenerationSequenceSummary | null = null;
  if (wire.generation_sequence !== null) {
    const summary = object(
      wire.generation_sequence,
      [
        "schema",
        "sequence_id",
        "sequence_fingerprint",
        "state_fingerprint",
        "workspace_id",
        "workspace_revision",
        "workspace_fingerprint",
        "correlation",
      ],
      "generation sequence summary",
    );
    if (summary.schema !== "h3.context.generation_sequence_projection.v1")
      throw new Error("generation sequence schema is invalid");
    const correlation = object(
      summary.correlation,
      correlationKeys,
      "generation sequence correlation",
    );
    const summaryWorkspaceId = exactString(
      summary.workspace_id,
      identifier,
      "generation sequence workspace id",
    );
    const summaryWorkspaceRevision = exactInteger(
      summary.workspace_revision,
      1,
      1_000_000,
      "generation sequence workspace revision",
    );
    const summaryWorkspaceFingerprint = exactString(
      summary.workspace_fingerprint,
      fingerprint,
      "generation sequence workspace fingerprint",
    );
    // IMPORTANT: selection advances the live CAS revision while the accepted run summary keeps
    // its source identity. Same-revision forks and future summaries must still fail closed.
    if (
      summaryWorkspaceId !== decodedWorkspaceId ||
      summaryWorkspaceRevision > decodedWorkspaceRevision ||
      (summaryWorkspaceRevision === decodedWorkspaceRevision &&
        summaryWorkspaceFingerprint !== decodedWorkspaceFingerprint)
    )
      throw new Error("generation sequence workspace does not match");
    generationSequence = Object.freeze({
      schema: "h3.context.generation_sequence_projection.v1",
      sequenceId: exactString(
        summary.sequence_id,
        identifier,
        "generation sequence id",
      ),
      sequenceFingerprint: exactString(
        summary.sequence_fingerprint,
        fingerprint,
        "generation sequence fingerprint",
      ),
      stateFingerprint: exactString(
        summary.state_fingerprint,
        fingerprint,
        "generation sequence state fingerprint",
      ),
      workspaceId: summaryWorkspaceId,
      workspaceRevision: summaryWorkspaceRevision,
      workspaceFingerprint: summaryWorkspaceFingerprint,
      correlation: Object.freeze({
        promptId: exactString(
          correlation.prompt_id,
          identifier,
          "generation sequence prompt id",
        ),
        executionNodeId: exactString(
          correlation.execution_node_id,
          identifier,
          "generation sequence execution node id",
        ),
      }),
    });
  }
  const reconstruction = object(
    wire.reconstruction,
    ["state"],
    "production reconstruction",
  );
  if (
    typeof reconstruction.state !== "string" ||
    !reconstructionStates.has(reconstruction.state)
  )
    throw new Error("production reconstruction state is invalid");
  const assembly = decodeAssembly(wire.assembly);
  if (assembly.state === "succeeded" && reconstruction.state !== "complete")
    throw new Error("production assembly result is inconsistent");
  if (
    !Array.isArray(wire.authority_versions) ||
    wire.authority_versions.length > 5
  )
    throw new Error("authority versions are invalid");
  const versions = wire.authority_versions.map((version) => {
    if (typeof version !== "string" || !authorityVersions.has(version))
      throw new Error("authority version is invalid");
    return version as ProductionWorkbenchProjection["authorityVersions"][number];
  });
  if (new Set(versions).size !== versions.length)
    throw new Error("authority versions contain duplicate values");
  if (!Array.isArray(wire.outputs) || wire.outputs.length > 65)
    throw new Error("production output limit is invalid");
  const outputs = Object.freeze(
    wire.outputs.map((value, index): ProductionOutput => {
      const output = object(
        value,
        ["output_handle", "ordinal", "state", "segment_id", "preview"],
        "production output",
      );
      if (typeof output.state !== "string" || !outputStates.has(output.state))
        throw new Error("production output state is invalid");
      if (
        output.segment_id !== null &&
        (typeof output.segment_id !== "string" ||
          !identifier.test(output.segment_id) ||
          !segments.some((segment) => segment.segmentId === output.segment_id))
      )
        throw new Error("production output segment is invalid");
      if (
        typeof output.preview !== "boolean" ||
        (output.preview && output.state !== "ready")
      )
        throw new Error("production output preview is invalid");
      return Object.freeze({
        outputHandle: exactString(
          output.output_handle,
          outputHandle,
          "output handle",
        ),
        ordinal: exactInteger(
          output.ordinal,
          index + 1,
          index + 1,
          "output ordinal",
        ),
        state: output.state as ProductionOutput["state"],
        segmentId: output.segment_id as string | null,
        preview: output.preview,
      });
    }),
  );
  if (
    new Set(outputs.map((output) => output.outputHandle)).size !==
    outputs.length
  )
    throw new Error("production outputs contain duplicate handles");
  const aggregateOutputs = outputs.filter(
    (output) => output.segmentId === null,
  ).length;
  const expectedAggregateOutputs = reconstruction.state === "complete" ? 1 : 0;
  // IMPORTANT: a verified generated clip is segment-only until reconstruction publishes an
  // aggregate. Requiring a synthetic aggregate here makes the real managed preview undecodable.
  if (outputs.length > 0 && aggregateOutputs !== expectedAggregateOutputs)
    throw new Error("production aggregate output is invalid");
  const outputSegmentIds = outputs.flatMap((output) =>
    output.segmentId === null ? [] : [output.segmentId],
  );
  if (new Set(outputSegmentIds).size !== outputSegmentIds.length)
    throw new Error("production output segments contain duplicates");
  if (!Array.isArray(wire.allowed_actions))
    throw new Error("allowed actions are invalid");
  const actions = wire.allowed_actions.map((action) => {
    if (
      typeof action !== "string" ||
      !allowedActions.has(action as ProductionAllowedAction)
    )
      throw new Error("allowed action is invalid");
    return action as ProductionAllowedAction;
  });
  if (new Set(actions).size !== actions.length)
    throw new Error("allowed actions contain duplicate values");
  const expectedAssemblyActions: ProductionAllowedAction[] = [];
  if (assembly.capabilityFingerprint !== null) {
    if (assembly.state === "unavailable")
      expectedAssemblyActions.push("assemble_sequence");
    else if (assembly.state === "planned" || assembly.state === "running")
      expectedAssemblyActions.push("cancel_assembly");
    else if (assembly.state === "failed")
      expectedAssemblyActions.push("retry_assembly");
  }
  const actualAssemblyActions = actions.filter((action) =>
    new Set(["assemble_sequence", "cancel_assembly", "retry_assembly"]).has(
      action,
    ),
  );
  if (
    JSON.stringify(actualAssemblyActions) !==
    JSON.stringify(expectedAssemblyActions)
  )
    throw new Error("production assembly action is inconsistent");
  if (
    actions.includes("preview_output") !==
    outputs.some((output) => output.preview)
  )
    throw new Error("production preview capability is inconsistent");
  if (
    actions.includes("import_production_outputs_to_authoring") &&
    // IMPORTANT: succeeded automatic assembly preserves its original rows. Legacy reconstruction
    // and aggregate-only outputs never advertise original import, even with a ready media row.
    (!(
      reconstruction.state === "unavailable" ||
      (reconstruction.state === "complete" &&
        assembly.state === "succeeded" &&
        versions.includes("h3.context.m26.production_assembly_receipt.v1"))
    ) ||
      !outputs.some(
        (output) => output.state === "ready" && output.segmentId !== null,
      ))
  )
    throw new Error("production authoring import capability is inconsistent");
  if (segments.length === 64 && actions.includes("add_segment_from_context"))
    throw new Error("add action exceeds the segment limit");
  if (
    segments.length === 1 &&
    (actions.includes("delete_segment") || actions.includes("reorder_segments"))
  )
    throw new Error("multi-segment action is unavailable");
  if (!Array.isArray(wire.blocker_codes) || wire.blocker_codes.length > 64)
    throw new Error("blocker code limit is invalid");
  const blockerCodes = wire.blocker_codes.map((code) =>
    exactString(code, identifier, "blocker code"),
  );
  if (new Set(blockerCodes).size !== blockerCodes.length)
    throw new Error("blocker codes contain duplicate values");
  const limits = object(
    wire.limits,
    ["max_segments", "max_outputs"],
    "production limits",
  );
  if (limits.max_segments !== 64 || limits.max_outputs !== 65)
    throw new Error("production limit facts are invalid");
  return Object.freeze({
    schema: PRODUCTION_PROJECTION_SCHEMA,
    workspaceHandle: exactString(
      wire.workspace_handle,
      workspaceHandle,
      "workspace handle",
    ),
    workspaceId: decodedWorkspaceId,
    workspaceRevision: decodedWorkspaceRevision,
    workspaceFingerprint: decodedWorkspaceFingerprint,
    segments,
    selectedSegmentIds: selected,
    runState: run.state as ProductionWorkbenchProjection["runState"],
    runProgress: Object.freeze({ completed, total }),
    generationSequence,
    reconstructionState:
      reconstruction.state as ProductionWorkbenchProjection["reconstructionState"],
    assembly,
    authorityVersions: Object.freeze(versions),
    outputs,
    allowedActions: Object.freeze(actions),
    blockerCodes: Object.freeze(blockerCodes),
  });
}

/**
 * Destination admission for one managed Start. These are not projection allowed actions: the
 * backend answers them before any project changes, so they never appear in `allowed_actions`.
 */
export type ProductionDestinationAction =
  | "admit_generation_destination"
  | "release_generation_destination"
  | "settle_generation_destination";

/** `null` admits a new project; a segment id regenerates that member in place. */
export type ProductionDestinationTarget = Readonly<{
  workspaceHandle: string;
  workspaceId: string;
  segmentId: string | null;
}> | null;

export type ProductionDestinationInput =
  | Readonly<{ target: ProductionDestinationTarget }>
  | Readonly<{ admissionRequestId: string }>
  | Readonly<{
      admissionRequestId: string;
      terminal: "failed" | "cancelled" | "unknown_ownership";
    }>;

export function encodeProductionDestinationAction(
  requestId: string,
  action: ProductionDestinationAction,
  input: ProductionDestinationInput,
): Record<string, unknown> {
  exactString(requestId, identifier, "request id");
  let payload: Record<string, unknown>;
  if (action === "admit_generation_destination") {
    object(input, ["target"], "destination admission input");
    const target = (input as { target: ProductionDestinationTarget }).target;
    if (target === null)
      payload = {
        version: PRODUCTION_ACCUMULATION_ACTION_VERSION,
        workspace_handle: null,
        workspace_id: null,
        segment_id: null,
      };
    else {
      object(
        target,
        ["workspaceHandle", "workspaceId", "segmentId"],
        "destination target",
      );
      payload = {
        version: PRODUCTION_ACCUMULATION_ACTION_VERSION,
        workspace_handle: exactString(
          target.workspaceHandle,
          workspaceHandle,
          "workspace handle",
        ),
        workspace_id: exactString(
          target.workspaceId,
          identifier,
          "workspace id",
        ),
        segment_id:
          target.segmentId === null
            ? null
            : exactString(target.segmentId, identifier, "segment id"),
      };
    }
  } else if (action === "release_generation_destination") {
    object(input, ["admissionRequestId"], "destination release input");
    payload = {
      version: PRODUCTION_ACCUMULATION_ACTION_VERSION,
      admission_request_id: exactString(
        (input as { admissionRequestId: string }).admissionRequestId,
        identifier,
        "admission request id",
      ),
    };
  } else if (action === "settle_generation_destination") {
    object(
      input,
      ["admissionRequestId", "terminal"],
      "destination settlement input",
    );
    const settlement = input as {
      admissionRequestId: string;
      terminal: "failed" | "cancelled" | "unknown_ownership";
    };
    if (
      !new Set(["failed", "cancelled", "unknown_ownership"]).has(
        settlement.terminal,
      )
    )
      throw new Error("destination terminal is unsupported");
    payload = {
      version: PRODUCTION_ACCUMULATION_ACTION_VERSION,
      admission_request_id: exactString(
        settlement.admissionRequestId,
        identifier,
        "admission request id",
      ),
      terminal: settlement.terminal,
    };
  } else throw new Error("destination action is unsupported");
  return {
    schema: PRODUCTION_ACTION_SCHEMA,
    request_id: requestId,
    action:
      action === "admit_generation_destination"
        ? "admit_generation_destination_v2"
        : action === "release_generation_destination"
          ? "release_generation_destination_v2"
          : "settle_generation_destination_v2",
    payload,
  };
}

export type ProductionActionInput = Readonly<{
  projection?: ProductionWorkbenchProjection;
  workspaceHandle?: string;
  contextWorkspaceHandle?: string;
  segmentId?: string;
  relation?: SegmentRelation;
  predecessorSegmentId?: string | null;
  segmentIds?: readonly string[];
}>;

export function encodeProductionAction(
  requestId: string,
  action: ProductionAction,
  input: ProductionActionInput,
): Record<string, unknown> {
  exactString(requestId, identifier, "request id");
  const admitInput = (keys: readonly string[]) =>
    object(input, keys, "production action input");
  const projectionAllows = (requested: ProductionAction): void => {
    if (
      input.projection !== undefined &&
      !input.projection.allowedActions.includes(requested)
    )
      throw new Error("production action is not allowed");
  };
  const knownSegment = (value: unknown, name: string): string => {
    const segmentId = exactString(value, identifier, name);
    if (
      !input.projection?.segments.some(
        (segment) => segment.segmentId === segmentId,
      )
    )
      throw new Error(`${name} is unknown`);
    return segmentId;
  };
  const relationPredecessor = (): string | null => {
    if (input.relation === undefined || !relations.has(input.relation))
      throw new Error("segment relation is required");
    const requiresPredecessor =
      input.relation === "predecessor" || input.relation === "adjacent_pair";
    if (!requiresPredecessor) {
      if (input.predecessorSegmentId !== null)
        throw new Error("segment predecessor does not match relation");
      return null;
    }
    return knownSegment(input.predecessorSegmentId, "segment predecessor");
  };
  const expected = (extra: Record<string, unknown>) => {
    if (input.projection === undefined)
      throw new Error("production projection is required");
    return {
      workspace_handle: input.projection.workspaceHandle,
      expected_workspace_revision: input.projection.workspaceRevision,
      expected_workspace_fingerprint: input.projection.workspaceFingerprint,
      ...extra,
    };
  };
  let payload: Record<string, unknown>;
  switch (action) {
    case "create_workspace_from_context":
      admitInput(["contextWorkspaceHandle"]);
      payload = {
        context_workspace_handle: exactString(
          input.contextWorkspaceHandle,
          /^ws_[A-Za-z0-9_-]{32,96}$/,
          "context workspace handle",
        ),
      };
      break;
    case "read_projection":
      if (input.projection !== undefined) {
        admitInput(["projection"]);
        projectionAllows(action);
        payload = { workspace_handle: input.projection.workspaceHandle };
      } else {
        admitInput(["workspaceHandle"]);
        payload = {
          workspace_handle: exactString(
            input.workspaceHandle,
            workspaceHandle,
            "workspace handle",
          ),
        };
      }
      break;
    case "release_workspace":
      admitInput(["projection"]);
      projectionAllows(action);
      payload = expected({});
      break;
    case "assemble_sequence":
      admitInput(["projection"]);
      projectionAllows(action);
      if (
        input.projection === undefined ||
        input.projection.assembly.capabilityFingerprint === null ||
        input.projection.assembly.managedSequenceFingerprint === null ||
        input.projection.assembly.state !== "unavailable"
      )
        throw new Error("production assembly is unavailable");
      payload = expected({
        workspace_id: input.projection.workspaceId,
        managed_sequence_fingerprint:
          input.projection.assembly.managedSequenceFingerprint,
        artifact_receipt_fingerprints: [
          ...input.projection.assembly.artifactReceiptFingerprints,
        ],
        cut_boundary_receipt_fingerprints: [
          ...input.projection.assembly.cutBoundaryReceiptFingerprints,
        ],
        assembly_capability_fingerprint:
          input.projection.assembly.capabilityFingerprint,
        output_profile_id: input.projection.assembly.outputProfileId,
      });
      break;
    case "cancel_assembly":
    case "retry_assembly":
      admitInput(["projection"]);
      projectionAllows(action);
      if (input.projection === undefined)
        throw new Error("production projection is required");
      payload = expected({
        workspace_id: input.projection.workspaceId,
        expected_assembly_fingerprint:
          input.projection.assembly.projectionFingerprint,
      });
      break;
    case "set_selection":
      admitInput(["projection", "segmentIds"]);
      projectionAllows(action);
      {
        const segmentIds = uniqueStrings(input.segmentIds, 64, "segment ids");
        if (
          !segmentIds.every((segmentId) =>
            input.projection?.segments.some(
              (segment) => segment.segmentId === segmentId,
            ),
          )
        )
          throw new Error("selected segment id is unknown");
        payload = expected({ segment_ids: [...segmentIds] });
      }
      break;
    case "reorder_segments":
      admitInput(["projection", "segmentIds"]);
      projectionAllows(action);
      {
        const segmentIds = uniqueStrings(input.segmentIds, 64, "segment ids");
        const currentIds = input.projection?.segments.map(
          (segment) => segment.segmentId,
        );
        if (
          currentIds === undefined ||
          segmentIds.length !== currentIds.length ||
          !segmentIds.every((segmentId) => currentIds.includes(segmentId))
        )
          throw new Error("segment reorder is not a permutation");
        payload = expected({ segment_ids: [...segmentIds] });
      }
      break;
    case "delete_segment":
      admitInput(["projection", "segmentId"]);
      projectionAllows(action);
      payload = expected({
        segment_id: knownSegment(input.segmentId, "segment id"),
      });
      break;
    case "replace_segment_from_context":
      admitInput(["projection", "contextWorkspaceHandle", "segmentId"]);
      projectionAllows(action);
      payload = expected({
        segment_id: knownSegment(input.segmentId, "segment id"),
        context_workspace_handle: exactString(
          input.contextWorkspaceHandle,
          /^ws_[A-Za-z0-9_-]{32,96}$/,
          "context workspace handle",
        ),
      });
      break;
    case "add_segment_from_context":
      admitInput([
        "projection",
        "contextWorkspaceHandle",
        "relation",
        "predecessorSegmentId",
      ]);
      projectionAllows(action);
      payload = expected({
        context_workspace_handle: exactString(
          input.contextWorkspaceHandle,
          /^ws_[A-Za-z0-9_-]{32,96}$/,
          "context workspace handle",
        ),
        relation: input.relation,
        predecessor_segment_id: relationPredecessor(),
      });
      break;
    case "set_segment_relation":
      admitInput([
        "projection",
        "segmentId",
        "relation",
        "predecessorSegmentId",
      ]);
      projectionAllows(action);
      {
        const segmentId = knownSegment(input.segmentId, "segment id");
        const predecessor = relationPredecessor();
        if (predecessor === segmentId)
          throw new Error("segment cannot depend on itself");
        payload = expected({
          segment_id: segmentId,
          relation: input.relation,
          predecessor_segment_id: predecessor,
        });
      }
      break;
  }
  return {
    schema: PRODUCTION_ACTION_SCHEMA,
    request_id: requestId,
    action,
    payload,
  };
}
