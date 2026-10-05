import {
  canonicalStringFingerprint,
  sha256Text,
} from "../contracts/canonicalFingerprint";
import {
  type AppModeApp,
  type AppModeInputs,
  PRODUCTION_CANONICAL_LOWERING_SCHEMA,
  type ProductionCanonicalLowering,
  graphFingerprint,
} from "./appMode";
import { readActiveWorkflow } from "./canvasOwnedWrite";
import { serializeGraph } from "./hostSeams";
import {
  MANAGED_SEQUENCE_ROUTE,
  ManagedSequenceClientError,
  type EligibleSegmentExecution,
} from "./managedSequenceClient";
import type {
  ManagedSerialChildPlan,
  ManagedSerialResolveChild,
} from "./managedSequenceRunnerContract";
import {
  observeOwnedGraph,
  type OwnedGraphReference,
} from "./ownedGraphIdentity";

export const MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA =
  "h3.context.managed_prepared_context_action.v1" as const;
export const MANAGED_PREPARED_CONTEXT_SCHEMA =
  "h3.context.managed_prepared_context.v1" as const;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const contextHandle = /^ws_[A-Za-z0-9_-]{32,96}$/;
const decimal = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const modes = new Set(["t2va", "i2va", "fl2va", "l2va", "ref2va"]);
const kinds = new Set(["image", "video", "audio"]);
const roles = new Set([
  "primary",
  "first_frame",
  "last_frame",
  "reference",
  "subject_reference",
  "style_reference",
  "motion_reference",
  "camera_reference",
  "editing_source",
  "continuation_source",
  "audio_source",
]);

type PreparedTaskMode = AppModeInputs["task_mode"];
type PreparedReferenceKind = "image" | "video" | "audio";
type PreparedReferenceRole =
  | "primary"
  | "first_frame"
  | "last_frame"
  | "reference"
  | "subject_reference"
  | "style_reference"
  | "motion_reference"
  | "camera_reference"
  | "editing_source"
  | "continuation_source"
  | "audio_source";

export type ProductionPreparedReference = Readonly<{
  asset_id: string;
  kind: PreparedReferenceKind;
  role: PreparedReferenceRole;
  connection_order: number;
  metadata: Readonly<{
    duration_seconds: string | null;
    width: number | null;
    height: number | null;
    frame_count: number | null;
    sample_rate: number | null;
    channels: number | null;
  }> | null;
  paired_video_id: string | null;
}>;

export type ProductionManagedPreparedContext = Readonly<{
  schema: typeof MANAGED_PREPARED_CONTEXT_SCHEMA;
  parent_sequence_id: string;
  parent_revision: number;
  authorization_fingerprint: string;
  segment_id: string;
  eligible_execution_fingerprint: string;
  materialization_receipt_fingerprint: string;
  context_workspace_handle: string;
  context_report_id: string;
  context_report_revision: number;
  context_report_fingerprint: string;
  canonical_prompt: string;
  canonical_prompt_fingerprint: string;
  task_mode: PreparedTaskMode;
  duration_milliseconds: number;
  frame_count: number;
  profile: Readonly<{ name: "h3_base" | "h3_full_reference"; version: "1.0" }>;
  profile_fingerprint: string;
  ordered_references: readonly ProductionPreparedReference[];
  reference_registry_fingerprint: string;
  native_binding_fingerprint: string;
  canonical_lowering: Omit<
    ProductionCanonicalLowering,
    "canonicalPrompt"
  > | null;
}>;

type FetchResponse = Readonly<{
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}>;

type ResolverDependencies = Readonly<{
  app: AppModeApp;
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
  currentOwnedGraphReference(): OwnedGraphReference | undefined;
}>;

function reject(code = "invalid_response", status = 500): never {
  throw new ManagedSequenceClientError(code, status);
}

function object(value: unknown): Record<string, unknown> {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  )
    return reject();
  return value as Record<string, unknown>;
}

function closed(value: Record<string, unknown>, keys: readonly string[]): void {
  if (
    Object.keys(value).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(value, key))
  )
    reject();
}

function requiredText(value: unknown, pattern: RegExp = identifier): string {
  if (typeof value !== "string" || !pattern.test(value)) return reject();
  return value;
}

function integer(value: unknown, minimum: number, maximum: number): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  )
    return reject();
  return value;
}

function optionalInteger(value: unknown, maximum: number): number | null {
  return value === null ? null : integer(value, 1, maximum);
}

function validDurationDecimal(value: string): boolean {
  if (!decimal.test(value) || !/[1-9]/.test(value)) return false;
  const [whole, fraction = ""] = value.split(".");
  if (whole.length < 5) return true;
  if (whole.length > 5 || whole > "86400") return false;
  return whole < "86400" || !/[1-9]/.test(fraction);
}

function metadata(value: unknown): ProductionPreparedReference["metadata"] {
  if (value === null) return null;
  const wire = object(value);
  closed(wire, [
    "duration_seconds",
    "width",
    "height",
    "frame_count",
    "sample_rate",
    "channels",
  ]);
  const duration = wire.duration_seconds;
  if (
    duration !== null &&
    (typeof duration !== "string" || !validDurationDecimal(duration))
  )
    reject();
  return Object.freeze({
    duration_seconds: duration as string | null,
    width: optionalInteger(wire.width, 1_048_576),
    height: optionalInteger(wire.height, 1_048_576),
    frame_count: optionalInteger(wire.frame_count, 10_000_000),
    sample_rate: optionalInteger(wire.sample_rate, 768_000),
    channels: optionalInteger(wire.channels, 256),
  });
}

function reference(value: unknown): ProductionPreparedReference {
  const wire = object(value);
  closed(wire, [
    "asset_id",
    "kind",
    "role",
    "connection_order",
    "metadata",
    "paired_video_id",
  ]);
  if (!kinds.has(String(wire.kind)) || !roles.has(String(wire.role))) reject();
  if (
    wire.paired_video_id !== null &&
    (wire.kind !== "audio" ||
      typeof wire.paired_video_id !== "string" ||
      !identifier.test(wire.paired_video_id))
  )
    reject();
  if (
    (wire.role === "first_frame" || wire.role === "last_frame") &&
    wire.kind !== "image"
  )
    reject();
  return Object.freeze({
    asset_id: requiredText(wire.asset_id),
    kind: wire.kind as PreparedReferenceKind,
    role: wire.role as PreparedReferenceRole,
    connection_order: integer(wire.connection_order, 1, 1_000),
    metadata: metadata(wire.metadata),
    paired_video_id: wire.paired_video_id as string | null,
  });
}

function orderedReferences(
  value: unknown,
): readonly ProductionPreparedReference[] {
  if (!Array.isArray(value) || value.length > 12) return reject();
  const rows = value.map(reference);
  const byId = new Map(rows.map((row) => [row.asset_id, row] as const));
  if (
    byId.size !== rows.length ||
    rows.some((row, index) => row.connection_order !== index + 1)
  )
    reject();
  const pairedVideos = new Set<string>();
  let phase: "images" | "videos" | "audio" = "images";
  let imageCount = 0;
  let videoCount = 0;
  let pairedCount = 0;
  let standaloneAudioCount = 0;
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index]!;
    if (row.kind === "image") {
      imageCount += 1;
      if (phase !== "images") reject();
      continue;
    }
    if (row.kind === "video") {
      videoCount += 1;
      if (phase === "audio") reject();
      phase = "videos";
      continue;
    }
    if (row.paired_video_id === null) {
      standaloneAudioCount += 1;
      phase = "audio";
      continue;
    }
    pairedCount += 1;
    if (phase === "audio" || pairedVideos.has(row.paired_video_id)) reject();
    const target = byId.get(row.paired_video_id);
    if (
      target?.kind !== "video" ||
      rows[index + 1] !== target ||
      target.connection_order !== row.connection_order + 1
    )
      reject();
    pairedVideos.add(row.paired_video_id);
    phase = "videos";
  }
  if (
    imageCount > 9 ||
    videoCount > 3 ||
    pairedCount > 3 ||
    standaloneAudioCount > 3
  )
    reject();
  return Object.freeze(rows);
}

function canonicalLowering(
  value: unknown,
): ProductionManagedPreparedContext["canonical_lowering"] {
  if (value === null) return null;
  const wire = object(value);
  closed(wire, [
    "schema",
    "base_report_fingerprint",
    "base_report_revision",
    "override_revision",
    "reason",
  ]);
  if (
    wire.schema !== PRODUCTION_CANONICAL_LOWERING_SCHEMA ||
    wire.reason !== "Materialize approved Production segment prompt"
  )
    reject();
  return Object.freeze({
    schema: PRODUCTION_CANONICAL_LOWERING_SCHEMA,
    baseReportFingerprint: requiredText(
      wire.base_report_fingerprint,
      fingerprint,
    ),
    baseReportRevision: integer(wire.base_report_revision, 0, 1_000_000),
    overrideRevision: integer(wire.override_revision, 1, 1_000_000),
    reason: "Materialize approved Production segment prompt",
  });
}

export function decodeProductionManagedPreparedContext(
  value: unknown,
): ProductionManagedPreparedContext {
  let serialized: string | undefined;
  try {
    serialized = JSON.stringify(value);
  } catch {
    return reject();
  }
  if (serialized === undefined || serialized.length > 262_144) return reject();
  const wire = object(value);
  closed(wire, [
    "schema",
    "parent_sequence_id",
    "parent_revision",
    "authorization_fingerprint",
    "segment_id",
    "eligible_execution_fingerprint",
    "materialization_receipt_fingerprint",
    "context_workspace_handle",
    "context_report_id",
    "context_report_revision",
    "context_report_fingerprint",
    "canonical_prompt",
    "canonical_prompt_fingerprint",
    "task_mode",
    "duration_milliseconds",
    "frame_count",
    "profile",
    "profile_fingerprint",
    "ordered_references",
    "reference_registry_fingerprint",
    "native_binding_fingerprint",
    "canonical_lowering",
  ]);
  if (wire.schema !== MANAGED_PREPARED_CONTEXT_SCHEMA) reject();
  const prompt = wire.canonical_prompt;
  if (
    typeof prompt !== "string" ||
    prompt.length === 0 ||
    prompt.length > 65_536 ||
    prompt.includes("\0")
  )
    reject();
  const promptFingerprint = requiredText(
    wire.canonical_prompt_fingerprint,
    fingerprint,
  );
  try {
    if (canonicalStringFingerprint(prompt) !== promptFingerprint) reject();
  } catch (error) {
    if (error instanceof ManagedSequenceClientError) throw error;
    return reject();
  }
  const mode = requiredText(wire.task_mode);
  if (!modes.has(mode)) reject();
  const profileWire = object(wire.profile);
  closed(profileWire, ["name", "version"]);
  if (
    (profileWire.name !== "h3_base" &&
      profileWire.name !== "h3_full_reference") ||
    profileWire.version !== "1.0"
  )
    reject();
  const profile = Object.freeze({
    name: profileWire.name,
    version: profileWire.version,
  }) as ProductionManagedPreparedContext["profile"];
  const profileFingerprint = requiredText(
    wire.profile_fingerprint,
    fingerprint,
  );
  if (sha256Text(JSON.stringify(profile)) !== profileFingerprint) reject();
  return Object.freeze({
    schema: MANAGED_PREPARED_CONTEXT_SCHEMA,
    parent_sequence_id: requiredText(wire.parent_sequence_id),
    parent_revision: integer(wire.parent_revision, 0, 1_000_000),
    authorization_fingerprint: requiredText(
      wire.authorization_fingerprint,
      fingerprint,
    ),
    segment_id: requiredText(wire.segment_id),
    eligible_execution_fingerprint: requiredText(
      wire.eligible_execution_fingerprint,
      fingerprint,
    ),
    materialization_receipt_fingerprint: requiredText(
      wire.materialization_receipt_fingerprint,
      fingerprint,
    ),
    context_workspace_handle: requiredText(
      wire.context_workspace_handle,
      contextHandle,
    ),
    context_report_id: requiredText(wire.context_report_id),
    context_report_revision: integer(
      wire.context_report_revision,
      0,
      1_000_000,
    ),
    context_report_fingerprint: requiredText(
      wire.context_report_fingerprint,
      fingerprint,
    ),
    canonical_prompt: prompt,
    canonical_prompt_fingerprint: promptFingerprint,
    task_mode: mode as PreparedTaskMode,
    duration_milliseconds: integer(wire.duration_milliseconds, 4_000, 15_000),
    frame_count: integer(wire.frame_count, 1, 512),
    profile,
    profile_fingerprint: profileFingerprint,
    ordered_references: orderedReferences(wire.ordered_references),
    reference_registry_fingerprint: requiredText(
      wire.reference_registry_fingerprint,
      fingerprint,
    ),
    native_binding_fingerprint: requiredText(
      wire.native_binding_fingerprint,
      fingerprint,
    ),
    canonical_lowering: canonicalLowering(wire.canonical_lowering),
  });
}

function inputsFromPreparedContext(
  prepared: ProductionManagedPreparedContext,
): AppModeInputs {
  if (
    prepared.canonical_prompt.trim().length === 0 ||
    prepared.canonical_prompt.length > 4_096 ||
    prepared.frame_count < 5
  )
    return reject("unsupported_prepared_context", 422);
  const base = {
    task_mode: prepared.task_mode,
    user_intent: prepared.canonical_prompt,
    duration_milliseconds: prepared.duration_milliseconds,
    frame_count: prepared.frame_count,
  };
  // ReferenceRegistry asset IDs are semantic identities, not ComfyUI loader
  // node IDs. Media modes stay closed until the retained M26-04 binding maps
  // each exact asset to a repository-owned host source.
  if (
    prepared.task_mode !== "t2va" ||
    prepared.profile.name !== "h3_base" ||
    prepared.ordered_references.length !== 0 ||
    prepared.canonical_lowering === null ||
    prepared.canonical_lowering.baseReportRevision !== 0 ||
    prepared.canonical_lowering.overrideRevision !== 1
  )
    return reject("unsupported_prepared_context", 422);
  return Object.freeze(base);
}

type CapturedWorkflowIdentity = Readonly<{
  workflowAuthority: object;
  workflowStore: object;
  openWorkflowCount: number;
  reference: OwnedGraphReference;
  previousOwnedProjectionFingerprint: string;
}>;

function captureWorkflowIdentity(
  app: AppModeApp,
  currentOwnedGraphReference: () => OwnedGraphReference | undefined,
): CapturedWorkflowIdentity {
  try {
    const workflowStore = app.extensionManager?.workflow;
    const workflowAuthority = readActiveWorkflow(app);
    const openWorkflows = workflowStore?.openWorkflows;
    const serialized = serializeGraph(app);
    const reference = currentOwnedGraphReference();
    if (
      workflowStore === undefined ||
      workflowAuthority === undefined ||
      !Array.isArray(openWorkflows) ||
      !openWorkflows.includes(workflowAuthority) ||
      serialized.status !== "ready" ||
      reference === undefined
    )
      return reject("workflow_identity_unavailable", 409);
    return Object.freeze({
      workflowAuthority,
      workflowStore,
      openWorkflowCount: openWorkflows.length,
      reference,
      previousOwnedProjectionFingerprint: observeOwnedGraph(
        serialized.value,
        reference,
      ).fingerprint,
    });
  } catch (error) {
    if (error instanceof ManagedSequenceClientError) throw error;
    return reject("workflow_identity_unavailable", 409);
  }
}

function sameStrings(
  left: readonly string[],
  right: readonly string[],
): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function sameOwnedReference(
  left: OwnedGraphReference,
  right: OwnedGraphReference,
): boolean {
  return (
    left.anchorNodeId === right.anchorNodeId &&
    sameStrings(left.nodeIds, right.nodeIds) &&
    sameStrings(left.linkIds, right.linkIds) &&
    sameStrings(left.authoredWidgetNodeIds, right.authoredWidgetNodeIds)
  );
}

function assertWorkflowIdentityUnchanged(
  app: AppModeApp,
  currentOwnedGraphReference: () => OwnedGraphReference | undefined,
  captured: CapturedWorkflowIdentity,
): string {
  try {
    const workflowStore = app.extensionManager?.workflow;
    const currentWorkflow = readActiveWorkflow(app);
    const openWorkflows = workflowStore?.openWorkflows;
    const currentSerialized = serializeGraph(app);
    const currentReference = currentOwnedGraphReference();
    if (
      workflowStore !== captured.workflowStore ||
      currentWorkflow !== captured.workflowAuthority ||
      !Array.isArray(openWorkflows) ||
      openWorkflows.length !== captured.openWorkflowCount ||
      !openWorkflows.includes(currentWorkflow) ||
      currentSerialized.status !== "ready" ||
      currentReference === undefined ||
      !sameOwnedReference(currentReference, captured.reference) ||
      observeOwnedGraph(currentSerialized.value, captured.reference)
        .fingerprint !== captured.previousOwnedProjectionFingerprint
    )
      reject("workflow_identity_changed", 409);
    // The complete graph is surroundings evidence and the App Mode compile
    // baseline. Foreign host changes are legal, so capture its latest identity
    // only after the owned currentness checks have passed.
    return graphFingerprint(currentSerialized.value);
  } catch (error) {
    if (error instanceof ManagedSequenceClientError) throw error;
    reject("workflow_identity_changed", 409);
  }
}

function assertExecution(
  execution: EligibleSegmentExecution,
  context: string,
  index: number,
): void {
  if (
    !identifier.test(execution.parentSequenceId) ||
    !fingerprint.test(execution.parentAuthorizationFingerprint) ||
    !identifier.test(execution.segmentId) ||
    !Number.isSafeInteger(execution.slotRevision) ||
    execution.slotRevision < 0 ||
    !fingerprint.test(execution.materializationReceiptFingerprint) ||
    !fingerprint.test(execution.fingerprint) ||
    !contextHandle.test(context) ||
    !Number.isSafeInteger(index) ||
    index < 0 ||
    index > 14
  )
    reject("invalid_prepared_context_request", 400);
}

function assertResponseJoin(
  response: ProductionManagedPreparedContext,
  execution: EligibleSegmentExecution,
  context: string,
): void {
  if (
    response.parent_sequence_id !== execution.parentSequenceId ||
    response.parent_revision !== execution.slotRevision ||
    response.authorization_fingerprint !==
      execution.parentAuthorizationFingerprint ||
    response.segment_id !== execution.segmentId ||
    response.eligible_execution_fingerprint !== execution.fingerprint ||
    response.materialization_receipt_fingerprint !==
      execution.materializationReceiptFingerprint ||
    response.context_workspace_handle !== context
  )
    reject("cross_prepared_context_authority", 500);
}

export function createProductionManagedChildResolver({
  app,
  fetchApi,
  currentOwnedGraphReference,
}: ResolverDependencies): ManagedSerialResolveChild {
  if (
    app === null ||
    typeof app !== "object" ||
    typeof fetchApi !== "function" ||
    typeof currentOwnedGraphReference !== "function"
  )
    throw new Error(
      "production managed child resolver dependencies are unavailable",
    );

  let parentBinding:
    | Readonly<{
        parentSequenceId: string;
        authorizationFingerprint: string;
        workflowAuthority: object;
        workflowStore: object;
        fingerprint: string;
      }>
    | undefined;

  return async (
    execution,
    contextWorkspaceHandle,
    index,
  ): Promise<ManagedSerialChildPlan> => {
    assertExecution(execution, contextWorkspaceHandle, index);
    const captured = captureWorkflowIdentity(app, currentOwnedGraphReference);
    const retained =
      parentBinding?.parentSequenceId === execution.parentSequenceId
        ? parentBinding
        : undefined;
    if (
      retained !== undefined &&
      (retained.authorizationFingerprint !==
        execution.parentAuthorizationFingerprint ||
        retained.workflowAuthority !== captured.workflowAuthority ||
        retained.workflowStore !== captured.workflowStore)
    )
      return reject("workflow_identity_changed", 409);
    const payload = {
      parent_sequence_id: execution.parentSequenceId,
      expected_revision: execution.slotRevision,
      authorization_fingerprint: execution.parentAuthorizationFingerprint,
      segment_id: execution.segmentId,
      eligible_execution_fingerprint: execution.fingerprint,
      materialization_receipt_fingerprint:
        execution.materializationReceiptFingerprint,
      context_workspace_handle: contextWorkspaceHandle,
    };
    const request = {
      schema: MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA,
      request_id: `read.prepared.${execution.fingerprint.slice(-40)}`,
      action: "read_prepared_child_context",
      payload,
    };
    const response = await fetchApi(MANAGED_SEQUENCE_ROUTE, {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
    });
    if (!response.ok) reject("prepared_context_rejected", response.status);
    if (response.status !== 200) reject("unexpected_status", response.status);
    let responseValue: unknown;
    try {
      responseValue = await response.json();
    } catch {
      return reject("invalid_response", 500);
    }
    const prepared = decodeProductionManagedPreparedContext(responseValue);
    assertResponseJoin(prepared, execution, contextWorkspaceHandle);

    // IMPORTANT: recheck the workflow/tab identity, declared ownership and owned
    // projection after the HTTP await; otherwise a prepared child could overwrite
    // repository-owned canvas state edited during the read.
    const observedGraphFingerprint = assertWorkflowIdentityUnchanged(
      app,
      currentOwnedGraphReference,
      captured,
    );
    const inputs = inputsFromPreparedContext(prepared);
    const lowering = prepared.canonical_lowering;
    if (lowering === null) return reject("unsupported_prepared_context", 422);

    // IMPORTANT: keep one workflow label for the parent; hashing each child canvas
    // rejects the next legitimate owned write as backend canvas_drift. Actual
    // workflow objects fence replacement; fresh owned projections still fence edits.
    // Reused predecessors do not bind a canvas, so the first dirty index may be nonzero.
    const activeWorkflowFingerprint =
      retained?.fingerprint ?? observedGraphFingerprint;
    parentBinding = Object.freeze({
      parentSequenceId: execution.parentSequenceId,
      authorizationFingerprint: execution.parentAuthorizationFingerprint,
      workflowAuthority: captured.workflowAuthority,
      workflowStore: captured.workflowStore,
      fingerprint: activeWorkflowFingerprint,
    });

    return Object.freeze({
      inputs,
      canonicalLowering: Object.freeze({
        ...lowering,
        canonicalPrompt: prepared.canonical_prompt,
      }),
      options: Object.freeze({
        replaceExisting: true,
        artifactScope: `managed.${execution.fingerprint.slice(-32)}`,
      }),
      workflowAuthority: captured.workflowAuthority,
      activeWorkflowFingerprint,
      previousOwnedProjectionFingerprint:
        captured.previousOwnedProjectionFingerprint,
    });
  };
}
