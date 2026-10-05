import { sha256Text } from "../contracts/canonicalFingerprint";

export const MANAGED_SEQUENCE_ACTION_SCHEMA =
  "h3.context.managed_sequence_action.v1" as const;
export const MANAGED_SEQUENCE_RESULT_SCHEMA =
  "h3.context.managed_sequence_result.v1" as const;
export const MANAGED_SEQUENCE_PROJECTION_SCHEMA =
  "h3.context.managed_sequence_projection.v1" as const;
export const MANAGED_SEQUENCE_REATTACH_POINTER_SCHEMA =
  "h3.context.managed_sequence_reattach_pointer.v1" as const;
export const MANAGED_SEQUENCE_CHILD_AUTHORITY_SCHEMA =
  "h3.context.managed_sequence_child_authority.v1" as const;
export const MANAGED_SEQUENCE_ROUTE =
  "/h3-context/v1/managed-sequences" as const;
export const MANAGED_SEQUENCE_REATTACH_STORAGE_KEY =
  "h3-context.managed-sequence-reattach.v1" as const;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const contextHandle = /^ws_[A-Za-z0-9_-]{32,96}$/;
const runHandle = /^mc_[A-Za-z0-9_-]{32,96}$/;
const binary64 = /^[0-9a-f]{16}$/;
const states = new Set([
  "authorized",
  "active",
  "paused_client_absent",
  "paused_unknown_ownership",
  "paused_canvas_drift",
  "paused_failure",
  "cancelled",
  "succeeded",
]);
const slotStates = new Set([
  "pending",
  "eligible",
  "prepared",
  "bound",
  "submitted",
  "running",
  "artifact_verified",
  "succeeded",
  "reused",
  "failed",
  "interrupted",
  "cancelled",
  "unknown_ownership",
]);

export type ManagedSequenceAction =
  | "authorize_sequence"
  | "start_sequence"
  | "prepare_sequence_child"
  | "bind_prepared_child"
  | "fail_prepared_child"
  | "fail_bound_child"
  | "mark_invocation_unknown"
  | "record_submission"
  | "record_running"
  | "record_artifact"
  | "record_terminal"
  | "record_reuse"
  | "detach_client"
  | "resume_sequence"
  | "record_canvas_rollback"
  | "mark_unknown_ownership"
  | "retry_segment"
  | "cancel_sequence";

export type ManagedSequenceState =
  | "authorized"
  | "active"
  | "paused_client_absent"
  | "paused_unknown_ownership"
  | "paused_canvas_drift"
  | "paused_failure"
  | "cancelled"
  | "succeeded";

export type ManagedSequenceSlot = Readonly<{
  segmentId: string;
  ordinal: number;
  state: string;
  attemptEpoch: number;
  eligibleExecutionFingerprint: string | null;
  childRunHandle: string | null;
  childStateFingerprint: string | null;
  graphFingerprint: string | null;
  compiledPromptFingerprint: string | null;
  canvasWriteFingerprint: string | null;
  queuePromptId: string | null;
  artifactReceiptFingerprint: string | null;
  terminalFingerprint: string | null;
}>;

export type ManagedSequenceProjection = Readonly<{
  schema: typeof MANAGED_SEQUENCE_PROJECTION_SCHEMA;
  parentSequenceId: string;
  authorizationFingerprint: string;
  state: ManagedSequenceState;
  revision: number;
  etag: string;
  lease: Readonly<{
    createdAt: string;
    idleDeadline: string;
    activeDeadline: string | null;
    absoluteDeadline: string;
    idleExpiryHandled: boolean;
  }>;
  activeSegmentId: string | null;
  slots: readonly ManagedSequenceSlot[];
}>;

export type EligibleSegmentExecution = Readonly<{
  parentSequenceId: string;
  parentAuthorizationFingerprint: string;
  segmentId: string;
  slotRevision: number;
  attemptEpoch: number;
  materializationReceiptFingerprint: string;
  predecessorTerminalFingerprint: string | null;
  requestId: string;
  jobCount: 1;
  fingerprint: string;
}>;

export type ManagedSequenceMutationResult = Readonly<{
  schema: typeof MANAGED_SEQUENCE_RESULT_SCHEMA;
  parentSequenceId: string;
  authorizationFingerprint: string;
  readAuthorityFingerprint: string;
  state: ManagedSequenceState;
  revision: number;
  execution: EligibleSegmentExecution | null;
  childAuthority: ManagedSequenceChildAuthority | null;
  contextWorkspaceHandle: string | null;
  parentExpiresAtEpochMs: number | null;
  replayed: boolean;
}>;

export type ManagedSequenceChildAuthority = Readonly<{
  schema: typeof MANAGED_SEQUENCE_CHILD_AUTHORITY_SCHEMA;
  runHandle: string;
  stateFingerprint: string;
  jobId: string;
  transactionId: string;
}>;

export type ManagedSequenceReattachPointerV1 = Readonly<{
  schema: typeof MANAGED_SEQUENCE_REATTACH_POINTER_SCHEMA;
  parentSequenceId: string;
  readAuthorityFingerprint: string;
  expiresAtEpochMs: number;
}>;

type FetchResponse = Readonly<{
  ok: boolean;
  status: number;
  headers: Readonly<{ get(name: string): string | null }>;
  json(): Promise<unknown>;
}>;

export class ManagedSequenceClientError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(code);
    this.name = "ManagedSequenceClientError";
    this.code = code;
    this.status = status;
  }
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new ManagedSequenceClientError("invalid_response", 500);
  return value as Record<string, unknown>;
}

function closed(wire: Record<string, unknown>, keys: readonly string[]): void {
  if (Object.keys(wire).sort().join() !== [...keys].sort().join())
    throw new ManagedSequenceClientError("invalid_response", 500);
}

function requiredIdentifier(value: unknown): string {
  if (typeof value !== "string" || !identifier.test(value))
    throw new ManagedSequenceClientError("invalid_response", 500);
  return value;
}

function requiredFingerprint(value: unknown): string {
  if (typeof value !== "string" || !fingerprint.test(value))
    throw new ManagedSequenceClientError("invalid_response", 500);
  return value;
}

function nullableFingerprint(value: unknown): string | null {
  return value === null ? null : requiredFingerprint(value);
}

function boundedRevision(value: unknown): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < 0 ||
    (value as number) > 1_000_000
  )
    throw new ManagedSequenceClientError("invalid_response", 500);
  return value as number;
}

function decodeState(value: unknown): ManagedSequenceState {
  if (typeof value !== "string" || !states.has(value))
    throw new ManagedSequenceClientError("invalid_response", 500);
  return value as ManagedSequenceState;
}

function decodeSlot(value: unknown): ManagedSequenceSlot {
  const wire = object(value);
  closed(wire, [
    "schema",
    "segment_id",
    "ordinal",
    "state",
    "attempt_epoch",
    "eligible_execution_fingerprint",
    "child_run_handle",
    "child_state_fingerprint",
    "graph_fingerprint",
    "compiled_prompt_fingerprint",
    "canvas_write_fingerprint",
    "queue_prompt_id",
    "artifact_receipt_fingerprint",
    "terminal_fingerprint",
  ]);
  if (
    wire.schema !== "h3.context.managed_sequence_slot.v1" ||
    !Number.isSafeInteger(wire.ordinal) ||
    (wire.ordinal as number) < 1 ||
    (wire.ordinal as number) > 15 ||
    typeof wire.state !== "string" ||
    !slotStates.has(wire.state) ||
    (wire.attempt_epoch !== 1 && wire.attempt_epoch !== 2) ||
    (wire.child_run_handle !== null &&
      (typeof wire.child_run_handle !== "string" ||
        !runHandle.test(wire.child_run_handle))) ||
    (wire.queue_prompt_id !== null &&
      (typeof wire.queue_prompt_id !== "string" ||
        !identifier.test(wire.queue_prompt_id)))
  )
    throw new ManagedSequenceClientError("invalid_response", 500);
  return Object.freeze({
    segmentId: requiredIdentifier(wire.segment_id),
    ordinal: wire.ordinal as number,
    state: wire.state,
    attemptEpoch: wire.attempt_epoch,
    eligibleExecutionFingerprint: nullableFingerprint(
      wire.eligible_execution_fingerprint,
    ),
    childRunHandle: wire.child_run_handle as string | null,
    childStateFingerprint: nullableFingerprint(wire.child_state_fingerprint),
    graphFingerprint: nullableFingerprint(wire.graph_fingerprint),
    compiledPromptFingerprint: nullableFingerprint(
      wire.compiled_prompt_fingerprint,
    ),
    canvasWriteFingerprint: nullableFingerprint(wire.canvas_write_fingerprint),
    queuePromptId: wire.queue_prompt_id as string | null,
    artifactReceiptFingerprint: nullableFingerprint(
      wire.artifact_receipt_fingerprint,
    ),
    terminalFingerprint: nullableFingerprint(wire.terminal_fingerprint),
  });
}

function decodeExecution(
  value: unknown,
  executionFingerprint: unknown,
): EligibleSegmentExecution | null {
  if (value === null) {
    if (executionFingerprint !== null)
      throw new ManagedSequenceClientError("invalid_response", 500);
    return null;
  }
  const wire = object(value);
  closed(wire, [
    "schema",
    "parent_sequence_id",
    "parent_authorization_fingerprint",
    "segment_id",
    "slot_revision",
    "attempt_epoch",
    "materialization_receipt_fingerprint",
    "predecessor_terminal_fingerprint",
    "request_id",
    "job_count",
  ]);
  if (
    wire.schema !== "h3.context.eligible_segment_execution.v1" ||
    (wire.attempt_epoch !== 1 && wire.attempt_epoch !== 2) ||
    wire.job_count !== 1
  )
    throw new ManagedSequenceClientError("invalid_response", 500);
  const normalized = {
    parentSequenceId: requiredIdentifier(wire.parent_sequence_id),
    parentAuthorizationFingerprint: requiredFingerprint(
      wire.parent_authorization_fingerprint,
    ),
    segmentId: requiredIdentifier(wire.segment_id),
    slotRevision: boundedRevision(wire.slot_revision),
    attemptEpoch: wire.attempt_epoch,
    materializationReceiptFingerprint: requiredFingerprint(
      wire.materialization_receipt_fingerprint,
    ),
    predecessorTerminalFingerprint: nullableFingerprint(
      wire.predecessor_terminal_fingerprint,
    ),
    requestId: requiredIdentifier(wire.request_id),
    jobCount: 1 as const,
  };
  return Object.freeze({
    ...normalized,
    fingerprint: requiredFingerprint(executionFingerprint),
  });
}

function decodeChildAuthority(
  value: unknown,
): ManagedSequenceChildAuthority | null {
  if (value === null) return null;
  const wire = object(value);
  closed(wire, [
    "schema",
    "run_handle",
    "state_fingerprint",
    "job_id",
    "transaction_id",
  ]);
  if (
    wire.schema !== MANAGED_SEQUENCE_CHILD_AUTHORITY_SCHEMA ||
    typeof wire.run_handle !== "string" ||
    !runHandle.test(wire.run_handle)
  )
    throw new ManagedSequenceClientError("invalid_response", 500);
  return Object.freeze({
    schema: MANAGED_SEQUENCE_CHILD_AUTHORITY_SCHEMA,
    runHandle: wire.run_handle,
    stateFingerprint: requiredFingerprint(wire.state_fingerprint),
    jobId: requiredIdentifier(wire.job_id),
    transactionId: requiredIdentifier(wire.transaction_id),
  });
}

export function decodeManagedSequenceMutationResult(
  value: unknown,
): ManagedSequenceMutationResult {
  const wire = object(value);
  closed(wire, [
    "schema",
    "parent_sequence_id",
    "authorization_fingerprint",
    "read_authority_fingerprint",
    "state",
    "revision",
    "execution",
    "execution_fingerprint",
    "child_authority",
    "context_workspace_handle",
    "parent_expires_at_epoch_ms",
    "replayed",
  ]);
  if (
    wire.schema !== MANAGED_SEQUENCE_RESULT_SCHEMA ||
    typeof wire.replayed !== "boolean" ||
    (wire.context_workspace_handle !== null &&
      (typeof wire.context_workspace_handle !== "string" ||
        !contextHandle.test(wire.context_workspace_handle))) ||
    (wire.parent_expires_at_epoch_ms !== null &&
      (!Number.isSafeInteger(wire.parent_expires_at_epoch_ms) ||
        (wire.parent_expires_at_epoch_ms as number) < 0))
  )
    throw new ManagedSequenceClientError("invalid_response", 500);
  return Object.freeze({
    schema: MANAGED_SEQUENCE_RESULT_SCHEMA,
    parentSequenceId: requiredIdentifier(wire.parent_sequence_id),
    authorizationFingerprint: requiredFingerprint(
      wire.authorization_fingerprint,
    ),
    readAuthorityFingerprint: requiredFingerprint(
      wire.read_authority_fingerprint,
    ),
    state: decodeState(wire.state),
    revision: boundedRevision(wire.revision),
    execution: decodeExecution(wire.execution, wire.execution_fingerprint),
    childAuthority: decodeChildAuthority(wire.child_authority),
    contextWorkspaceHandle: wire.context_workspace_handle as string | null,
    parentExpiresAtEpochMs: wire.parent_expires_at_epoch_ms as number | null,
    replayed: wire.replayed,
  });
}

export function decodeManagedSequenceProjection(
  value: unknown,
): ManagedSequenceProjection {
  const wire = object(value);
  closed(wire, [
    "schema",
    "parent_sequence_id",
    "authorization_fingerprint",
    "state",
    "revision",
    "etag",
    "lease",
    "active_segment_id",
    "slots",
  ]);
  const lease = object(wire.lease);
  closed(lease, [
    "schema",
    "created_at",
    "idle_deadline",
    "active_deadline",
    "absolute_deadline",
    "idle_expiry_handled",
  ]);
  if (
    wire.schema !== MANAGED_SEQUENCE_PROJECTION_SCHEMA ||
    lease.schema !== "h3.context.managed_sequence_lease.v1" ||
    typeof lease.created_at !== "string" ||
    !binary64.test(lease.created_at) ||
    typeof lease.idle_deadline !== "string" ||
    !binary64.test(lease.idle_deadline) ||
    (lease.active_deadline !== null &&
      (typeof lease.active_deadline !== "string" ||
        !binary64.test(lease.active_deadline))) ||
    typeof lease.absolute_deadline !== "string" ||
    !binary64.test(lease.absolute_deadline) ||
    typeof lease.idle_expiry_handled !== "boolean" ||
    (wire.active_segment_id !== null &&
      (typeof wire.active_segment_id !== "string" ||
        !identifier.test(wire.active_segment_id))) ||
    !Array.isArray(wire.slots) ||
    wire.slots.length < 1 ||
    wire.slots.length > 15
  )
    throw new ManagedSequenceClientError("invalid_response", 500);
  const slots = wire.slots.map(decodeSlot);
  if (
    new Set(slots.map((row) => row.segmentId)).size !== slots.length ||
    slots.some((row, index) => row.ordinal !== index + 1) ||
    (wire.active_segment_id !== null &&
      !slots.some((row) => row.segmentId === wire.active_segment_id))
  )
    throw new ManagedSequenceClientError("invalid_response", 500);
  return Object.freeze({
    schema: MANAGED_SEQUENCE_PROJECTION_SCHEMA,
    parentSequenceId: requiredIdentifier(wire.parent_sequence_id),
    authorizationFingerprint: requiredFingerprint(
      wire.authorization_fingerprint,
    ),
    state: decodeState(wire.state),
    revision: boundedRevision(wire.revision),
    etag: requiredFingerprint(wire.etag),
    lease: Object.freeze({
      createdAt: lease.created_at,
      idleDeadline: lease.idle_deadline,
      activeDeadline: lease.active_deadline as string | null,
      absoluteDeadline: lease.absolute_deadline,
      idleExpiryHandled: lease.idle_expiry_handled,
    }),
    activeSegmentId: wire.active_segment_id as string | null,
    slots: Object.freeze(slots),
  });
}

export function createManagedSequenceClient({
  fetchApi,
}: {
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
}) {
  if (typeof fetchApi !== "function")
    throw new Error("supported same-origin managed sequence seam is absent");
  return Object.freeze({
    async send(
      requestId: string,
      action: ManagedSequenceAction,
      payload: Record<string, unknown>,
      signal?: AbortSignal,
    ): Promise<ManagedSequenceMutationResult> {
      if (!identifier.test(requestId))
        throw new ManagedSequenceClientError("invalid_request_id", 400);
      const body = JSON.stringify({
        schema: MANAGED_SEQUENCE_ACTION_SCHEMA,
        request_id: requestId,
        action,
        payload,
      });
      if (body.length > 32_768)
        throw new ManagedSequenceClientError("request_too_large", 413);
      const response = await fetchApi(MANAGED_SEQUENCE_ROUTE, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body,
        signal,
      });
      if (!response.ok)
        throw new ManagedSequenceClientError(
          "managed_sequence_rejected",
          response.status,
        );
      if (response.status !== 200)
        throw new ManagedSequenceClientError(
          "unexpected_status",
          response.status,
        );
      return decodeManagedSequenceMutationResult(await response.json());
    },

    async read(
      requestedParentSequenceId: string,
      readAuthorityFingerprint: string,
      ifNoneMatch?: string,
      signal?: AbortSignal,
    ): Promise<
      Readonly<{
        status: 200 | 304;
        etag: string;
        projection: ManagedSequenceProjection | null;
      }>
    > {
      if (
        !identifier.test(requestedParentSequenceId) ||
        !fingerprint.test(readAuthorityFingerprint) ||
        (ifNoneMatch !== undefined && !fingerprint.test(ifNoneMatch))
      )
        throw new ManagedSequenceClientError("invalid_read_authority", 400);
      const headers: Record<string, string> = { accept: "application/json" };
      if (ifNoneMatch !== undefined) headers["if-none-match"] = ifNoneMatch;
      const path = `${MANAGED_SEQUENCE_ROUTE}/${encodeURIComponent(
        requestedParentSequenceId,
      )}?read_authority_fingerprint=${encodeURIComponent(readAuthorityFingerprint)}`;
      const response = await fetchApi(path, {
        method: "GET",
        credentials: "same-origin",
        headers,
        signal,
      });
      const etag = response.headers.get("etag");
      if (typeof etag !== "string" || !fingerprint.test(etag))
        throw new ManagedSequenceClientError(
          "invalid_response",
          response.status,
        );
      if (response.status === 304)
        return Object.freeze({ status: 304, etag, projection: null });
      if (!response.ok || response.status !== 200)
        throw new ManagedSequenceClientError(
          "managed_sequence_read_rejected",
          response.status,
        );
      const projection = decodeManagedSequenceProjection(await response.json());
      if (
        projection.parentSequenceId !== requestedParentSequenceId ||
        projection.etag !== etag
      )
        throw new ManagedSequenceClientError(
          "cross_parent_authority_response",
          500,
        );
      return Object.freeze({ status: 200, etag, projection });
    },
  });
}

function decodePointer(value: unknown): ManagedSequenceReattachPointerV1 {
  const wire = object(value);
  closed(wire, [
    "schema",
    "parent_sequence_id",
    "read_authority_fingerprint",
    "expires_at_epoch_ms",
  ]);
  if (
    wire.schema !== MANAGED_SEQUENCE_REATTACH_POINTER_SCHEMA ||
    !Number.isSafeInteger(wire.expires_at_epoch_ms) ||
    (wire.expires_at_epoch_ms as number) < 0
  )
    throw new ManagedSequenceClientError("invalid_reattach_pointer", 400);
  return Object.freeze({
    schema: MANAGED_SEQUENCE_REATTACH_POINTER_SCHEMA,
    parentSequenceId: requiredIdentifier(wire.parent_sequence_id),
    readAuthorityFingerprint: requiredFingerprint(
      wire.read_authority_fingerprint,
    ),
    expiresAtEpochMs: wire.expires_at_epoch_ms as number,
  });
}

export function createManagedSequenceReattachStore(
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">,
  nowEpochMs: () => number = Date.now,
) {
  if (
    storage === null ||
    typeof storage !== "object" ||
    typeof nowEpochMs !== "function"
  )
    throw new Error("managed sequence pointer storage is invalid");
  const remove = (): void => {
    try {
      storage.removeItem(MANAGED_SEQUENCE_REATTACH_STORAGE_KEY);
    } catch {
      // Storage availability is optional. Backend read authority remains the source of truth.
    }
  };
  return Object.freeze({
    replace(value: Omit<ManagedSequenceReattachPointerV1, "schema">): boolean {
      let pointer: ManagedSequenceReattachPointerV1;
      try {
        pointer = decodePointer({
          schema: MANAGED_SEQUENCE_REATTACH_POINTER_SCHEMA,
          parent_sequence_id: value.parentSequenceId,
          read_authority_fingerprint: value.readAuthorityFingerprint,
          expires_at_epoch_ms: value.expiresAtEpochMs,
        });
        // CRITICAL: an already-expired pointer is no recovery authority. Reporting persistence
        // success here would let the runner reach its first queue with nothing valid to reattach.
        if (pointer.expiresAtEpochMs <= nowEpochMs()) {
          remove();
          return false;
        }
        storage.setItem(
          MANAGED_SEQUENCE_REATTACH_STORAGE_KEY,
          JSON.stringify({
            schema: pointer.schema,
            parent_sequence_id: pointer.parentSequenceId,
            read_authority_fingerprint: pointer.readAuthorityFingerprint,
            expires_at_epoch_ms: pointer.expiresAtEpochMs,
          }),
        );
        return true;
      } catch {
        remove();
        return false;
      }
    },
    read(): ManagedSequenceReattachPointerV1 | undefined {
      try {
        const raw = storage.getItem(MANAGED_SEQUENCE_REATTACH_STORAGE_KEY);
        if (raw === null || raw.length > 1024) {
          if (raw !== null) remove();
          return undefined;
        }
        const pointer = decodePointer(JSON.parse(raw) as unknown);
        if (pointer.expiresAtEpochMs <= nowEpochMs()) {
          remove();
          return undefined;
        }
        return pointer;
      } catch {
        remove();
        return undefined;
      }
    },
    clear: remove,
  });
}

export function createBrowserManagedSequenceReattachStore(
  nowEpochMs: () => number = Date.now,
) {
  // CRITICAL: keep raw browser storage inside this host adapter. Moving these calls into the
  // entry shell breaks the composition boundary, and eager access lets disabled-origin errors
  // escape before the bounded store can fail closed and prevent queue authority.
  const browserStorage: Pick<Storage, "getItem" | "setItem" | "removeItem"> = {
    getItem: (key: string) => globalThis.localStorage.getItem(key),
    setItem: (key: string, value: string) =>
      globalThis.localStorage.setItem(key, value),
    removeItem: (key: string) => globalThis.localStorage.removeItem(key),
  };
  return createManagedSequenceReattachStore(browserStorage, nowEpochMs);
}
