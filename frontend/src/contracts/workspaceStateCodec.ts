export const WORKSPACE_STATE_RESPONSE_SCHEMA =
  "h3.context.workspace_state_response.v1" as const;
export const WORKSPACE_STATE_STATUS_SCHEMA =
  "h3.context.workspace_state_status.v1" as const;
export const MANAGED_STATE_NAMES = [
  "created",
  "context_ready",
  "production_ready",
  "sequence_prepared",
  "submitted",
  "running",
  "artifact_recorded",
  "terminal_succeeded",
  "terminal_failed",
  "terminal_cancelled",
  "terminal_unknown_ownership",
  "expired",
] as const;
const TRIGGERS = [
  "stage_context",
  "create_production",
  "release_production",
  "prepare_sequence",
  "record_submission",
  "record_running",
  "record_artifact",
  "record_succeeded",
  "record_failed",
  "cancel_before_host",
  "cancel_after_host",
  "release_sequence",
] as const;
export const WORKSPACE_STATE_CODES = [
  "owner_invalid",
  "record_invalid",
  "integer_invalid",
  "shape_invalid",
  "duplicate_key",
  "number_invalid",
  "json_invalid",
  "size_invalid",
  "shape_bound",
  "kind_invalid",
  "transition_invalid",
  "state_invalid",
  "version_unsupported",
  "owner_mismatch",
  "record_bound",
  "record_duplicate",
  "timestamp_invalid",
  "limits_invalid",
  "revision_invalid",
  "manifest_invalid",
  "storage_unsafe",
  "storage_unavailable",
  "quota_directory",
  "quota_owners",
  "quota_records",
  "quota_bytes",
  "owner_busy",
  "catalog_busy",
  "recovery_disabled",
  "revision_conflict",
  "revision_exhausted",
  "snapshot_corrupt",
  "snapshot_unavailable",
  "storage_unverified",
  "intent_invalid",
  "host_unqualified",
  "filesystem_unqualified",
  "owner_changed",
  "service_closed",
  "record_unavailable",
  "media_type_rejected",
  "origin_rejected",
  "request_too_large",
  "invalid_request",
  "internal_failure",
  "route_worker_capacity",
] as const;
export type WorkspaceStateCode = (typeof WORKSPACE_STATE_CODES)[number];
export type WorkspaceRecordKind = "production" | "authoring" | "managed_run";
export type WorkspaceRecordState =
  | (typeof MANAGED_STATE_NAMES)[number]
  | "workspace_active"
  | "workspace_closed";
type Revisions = Readonly<{
  workspace: number | null;
  reference: number | null;
  timeline: number | null;
  context: number | null;
  transition: number;
}>;
export type WorkspaceStateRecord = Readonly<{
  record_id: string;
  kind: WorkspaceRecordKind;
  created_at_ms: number;
  closed_at_ms: number | null;
  segment_count: number;
  state: WorkspaceRecordState;
  revisions: Revisions;
  last_transition: Readonly<{
    sequence: number;
    trigger: (typeof TRIGGERS)[number];
    source: (typeof MANAGED_STATE_NAMES)[number];
    target: (typeof MANAGED_STATE_NAMES)[number];
    guard: "single_segment_sequence" | "no_live_sequence" | null;
  }> | null;
}>;
export type WorkspaceStateProjection = Readonly<{
  schema: typeof WORKSPACE_STATE_STATUS_SCHEMA;
  supported: boolean;
  enabled: boolean;
  revision: number;
  count: number;
  saved_at_ms: number | null;
  current: boolean;
  save_state: "disabled" | "dirty" | "saved" | "blocked";
  sampler_active: boolean;
  records: readonly WorkspaceStateRecord[];
}>;
export type RecoveredWorkspaceState = Readonly<{
  recovery_handle: string;
  record_id: string;
  kind: WorkspaceRecordKind;
  state: WorkspaceRecordState;
  source_status: "source_reauthorization_required";
  executable: false;
  segment_count: number;
  revisions: Revisions;
}>;
export type WorkspaceStateResponse = Readonly<{
  schema: typeof WORKSPACE_STATE_RESPONSE_SCHEMA;
  projection: WorkspaceStateProjection;
  error: WorkspaceStateCode | null;
  recovered: RecoveredWorkspaceState | null;
}>;
export type WorkspaceStateAction =
  | Readonly<{ intent: "status" | "list" }>
  | Readonly<{
      intent: "set_enabled";
      enabled: boolean;
      expected_revision: number;
    }>
  | Readonly<{ intent: "save" | "reset"; expected_revision: number }>
  | Readonly<{
      intent: "restore";
      record_id: string;
      expected_revision: number;
    }>;

const REVISION_KEYS = [
  "workspace",
  "reference",
  "timeline",
  "context",
  "transition",
] as const;
const RECORD_ID = /^record_[a-f0-9]{32}$/;
const MAX_TIME = 4102444800000;
function reject(): never {
  throw new Error("workspace_state_wire_invalid");
}
function object(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype ||
    Object.keys(value).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(value, key))
  )
    return reject();
  return value as Record<string, unknown>;
}
function integer(
  value: unknown,
  max = Number.MAX_SAFE_INTEGER,
  min = 0,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < min ||
    value > max
  )
    return reject();
  return value;
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") return reject();
  return value;
}
function oneOf<const T extends readonly string[]>(
  value: unknown,
  values: T,
): T[number] {
  if (typeof value !== "string" || !values.includes(value)) return reject();
  return value as T[number];
}
function recordId(value: unknown): string {
  if (typeof value !== "string" || !RECORD_ID.test(value)) return reject();
  return value;
}
function revisions(value: unknown): Revisions {
  const row = object(value, REVISION_KEYS);
  return Object.freeze({
    workspace:
      row.workspace === null ? null : integer(row.workspace, 1_000_000),
    reference:
      row.reference === null ? null : integer(row.reference, 1_000_000),
    timeline: row.timeline === null ? null : integer(row.timeline, 1_000_000),
    context: row.context === null ? null : integer(row.context, 1_000_000),
    transition: integer(row.transition, 64),
  });
}
function record(value: unknown): WorkspaceStateRecord {
  const row = object(value, [
    "record_id",
    "kind",
    "created_at_ms",
    "closed_at_ms",
    "segment_count",
    "state",
    "revisions",
    "last_transition",
  ]);
  const kind = oneOf(row.kind, [
    "production",
    "authoring",
    "managed_run",
  ] as const);
  const state = oneOf(
    row.state,
    kind === "managed_run"
      ? MANAGED_STATE_NAMES
      : (["workspace_active", "workspace_closed"] as const),
  );
  const created = integer(row.created_at_ms, MAX_TIME, 1);
  const closed =
    row.closed_at_ms === null
      ? null
      : integer(row.closed_at_ms, MAX_TIME, created);
  const count = integer(row.segment_count, 64),
    revision = revisions(row.revisions);
  let fact: WorkspaceStateRecord["last_transition"] = null;
  if (row.last_transition !== null) {
    const last = object(row.last_transition, [
      "sequence",
      "trigger",
      "source",
      "target",
      "guard",
    ]);
    fact = Object.freeze({
      sequence: integer(last.sequence, 64),
      trigger: oneOf(last.trigger, TRIGGERS),
      source: oneOf(last.source, MANAGED_STATE_NAMES),
      target: oneOf(last.target, MANAGED_STATE_NAMES),
      guard:
        last.guard === null
          ? null
          : oneOf(last.guard, [
              "single_segment_sequence",
              "no_live_sequence",
            ] as const),
    });
    if (fact.sequence !== revision.transition || fact.target !== state)
      return reject();
  }
  if (kind === "managed_run") {
    if (
      revision.workspace !== null ||
      revision.reference !== null ||
      revision.timeline !== null ||
      (state === "created"
        ? fact !== null || revision.transition !== 0
        : fact === null)
    )
      return reject();
  } else if (
    fact !== null ||
    revision.transition !== 0 ||
    revision.context !== null ||
    revision.workspace === null ||
    (kind === "production"
      ? revision.workspace < 1 ||
        revision.reference !== null ||
        revision.timeline !== null
      : count !== 0 ||
        revision.reference === null ||
        revision.timeline === null) ||
    (state === "workspace_closed") !== (closed !== null)
  )
    return reject();
  return Object.freeze({
    record_id: recordId(row.record_id),
    kind,
    state,
    created_at_ms: created,
    closed_at_ms: closed,
    segment_count: count,
    revisions: revision,
    last_transition: fact,
  });
}

export function decodeWorkspaceStateResponse(
  value: unknown,
): WorkspaceStateResponse {
  const row = object(value, ["schema", "projection", "error", "recovered"]);
  if (row.schema !== WORKSPACE_STATE_RESPONSE_SCHEMA) return reject();
  const raw = object(row.projection, [
    "schema",
    "supported",
    "enabled",
    "revision",
    "count",
    "saved_at_ms",
    "current",
    "save_state",
    "sampler_active",
    "records",
  ]);
  if (
    raw.schema !== WORKSPACE_STATE_STATUS_SCHEMA ||
    !Array.isArray(raw.records) ||
    raw.records.length > 64
  )
    return reject();
  const records = Object.freeze(raw.records.map(record));
  const projection: WorkspaceStateProjection = Object.freeze({
    schema: WORKSPACE_STATE_STATUS_SCHEMA,
    supported: boolean(raw.supported),
    enabled: boolean(raw.enabled),
    revision: integer(raw.revision),
    count: integer(raw.count, 64),
    saved_at_ms:
      raw.saved_at_ms === null ? null : integer(raw.saved_at_ms, MAX_TIME, 1),
    current: boolean(raw.current),
    save_state: oneOf(raw.save_state, [
      "disabled",
      "dirty",
      "saved",
      "blocked",
    ] as const),
    sampler_active: boolean(raw.sampler_active),
    records,
  });
  const error =
    row.error === null ? null : oneOf(row.error, WORKSPACE_STATE_CODES);
  if (
    projection.count !== records.length ||
    new Set(records.map((r) => r.record_id)).size !== records.length ||
    (projection.save_state === "saved" &&
      (!projection.current ||
        !projection.enabled ||
        projection.saved_at_ms === null)) ||
    (projection.current &&
      (!projection.enabled ||
        !projection.supported ||
        projection.saved_at_ms === null)) ||
    (projection.sampler_active && !projection.enabled) ||
    (!projection.enabled &&
      projection.save_state !== "disabled" &&
      projection.supported) ||
    (!projection.supported &&
      (projection.enabled ||
        projection.revision !== 0 ||
        records.length !== 0 ||
        projection.current ||
        projection.sampler_active ||
        projection.saved_at_ms !== null ||
        projection.save_state !== "blocked")) ||
    records.some(
      (r) =>
        projection.saved_at_ms === null ||
        r.created_at_ms > projection.saved_at_ms ||
        (r.closed_at_ms !== null && r.closed_at_ms > projection.saved_at_ms),
    )
  )
    return reject();
  let recovered: RecoveredWorkspaceState | null = null;
  if (row.recovered !== null) {
    const rawRecovery = object(row.recovered, [
      "recovery_handle",
      "record_id",
      "kind",
      "state",
      "source_status",
      "executable",
      "segment_count",
      "revisions",
    ]);
    const id = recordId(rawRecovery.record_id),
      source = records.find((r) => r.record_id === id);
    if (
      !source ||
      !projection.enabled ||
      rawRecovery.executable !== false ||
      rawRecovery.source_status !== "source_reauthorization_required" ||
      typeof rawRecovery.recovery_handle !== "string" ||
      !/^recovery_[A-Za-z0-9_-]{43}$/.test(rawRecovery.recovery_handle) ||
      rawRecovery.kind !== source.kind ||
      rawRecovery.segment_count !== source.segment_count
    )
      return reject();
    // CRITICAL: persisted inflight state is not execution ownership, even in a valid response.
    const safeState = ["submitted", "running", "artifact_recorded"].includes(
      source.state,
    )
      ? "terminal_unknown_ownership"
      : source.state;
    const restoredRevisions = revisions(rawRecovery.revisions);
    if (
      rawRecovery.state !== safeState ||
      REVISION_KEYS.some(
        (key) => restoredRevisions[key] !== source.revisions[key],
      )
    )
      return reject();
    recovered = Object.freeze({
      recovery_handle: rawRecovery.recovery_handle,
      record_id: id,
      kind: source.kind,
      state: safeState,
      source_status: "source_reauthorization_required",
      executable: false,
      segment_count: source.segment_count,
      revisions: restoredRevisions,
    });
  }
  return Object.freeze({
    schema: WORKSPACE_STATE_RESPONSE_SCHEMA,
    projection,
    error,
    recovered,
  });
}

export function encodeWorkspaceStateAction(
  value: WorkspaceStateAction,
): string {
  const intent = oneOf(value?.intent, [
    "status",
    "list",
    "set_enabled",
    "save",
    "restore",
    "reset",
  ] as const);
  const keys =
    intent === "status" || intent === "list"
      ? ["intent"]
      : intent === "set_enabled"
        ? ["intent", "enabled", "expected_revision"]
        : intent === "restore"
          ? ["intent", "record_id", "expected_revision"]
          : ["intent", "expected_revision"];
  const action = object(value, keys);
  if ("expected_revision" in action) integer(action.expected_revision);
  if (intent === "set_enabled") boolean(action.enabled);
  if (intent === "restore") recordId(action.record_id);
  return JSON.stringify(action);
}
