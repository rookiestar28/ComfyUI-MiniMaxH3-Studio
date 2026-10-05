import {
  executionCorrelationKeys as correlationKeys,
  transactionTransparencyProjectionKeys as rootKeys,
  transactionTransparencySnapshotKeys as transactionKeys,
} from "./generatedSurface";

export type TransactionState =
  | "noop_clean"
  | "blocked"
  | "prepared"
  | "submitted"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "unknown_ownership";

export type TransactionIntent =
  | "confirm_native_queue"
  | "inspect_queue_history"
  | "return_to_native"
  | "rerun";

export type TransactionTransparencyProjection = {
  schema: "h3.context.transaction_transparency.v1";
  workspace_id: string;
  workspace_revision: number;
  workspace_fingerprint: string;
  recompute_plan_fingerprint: string;
  correlation: { prompt_id: string; execution_node_id: string };
  selection_safe: boolean;
  requires_full_recompute: boolean;
  mandatory_segment_ids: string[];
  requested_segment_ids: string[] | null;
  missing_required_segment_ids: string[];
  decisions: Array<{
    segment_id: string;
    disposition:
      | "clean"
      | "dirty_self"
      | "dirty_upstream"
      | "blocked_missing_predecessor"
      | "requires_full_recompute";
    reason_codes: string[];
    triggering_segment_ids: string[];
  }>;
  transaction: {
    transaction_id: string;
    transaction_fingerprint: string;
    attempt: number;
    state: TransactionState;
    graph_fingerprint: string | null;
    compiled_prompt_fingerprint: string | null;
    queue_prompt_id: string | null;
    host_owner_id: string | null;
    result_fingerprint: string | null;
    cancellation_requested: boolean;
  };
  actions: Record<TransactionIntent, boolean>;
  guidance:
    | "no_work_required"
    | "resolve_blocker"
    | "review_before_native_queue"
    | "inspect_queue_history"
    | "ownership_unknown"
    | "generation_complete"
    | "rerun_available";
};

const actionKeys: TransactionIntent[] = [
  "confirm_native_queue",
  "inspect_queue_history",
  "return_to_native",
  "rerun",
];
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const sensitive =
  /https?:\/\/|file:\/\/|(?:[A-Za-z]:[\\/]|\/(?:home|mnt|tmp|var|Users|private|workspace)\/)|\b(?:authorization|api[_-]?key|password|secret|token)\s*[:=]/i;

function object(value: unknown, field: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${field} must be an object`);
  return value as Record<string, unknown>;
}

function closed(
  value: Record<string, unknown>,
  keys: readonly string[],
  field: string,
): void {
  if (
    Object.keys(value).some((key) => !keys.includes(key)) ||
    keys.some((key) => !Object.hasOwn(value, key))
  )
    throw new Error(`${field} must be closed`);
}

function text(value: unknown, field: string, pattern = identifier): string {
  if (
    typeof value !== "string" ||
    !pattern.test(value) ||
    sensitive.test(value)
  )
    throw new Error(`${field} is invalid`);
  return value;
}

function optionalFingerprint(value: unknown, field: string): string | null {
  return value === null ? null : text(value, field, fingerprint);
}

function identifiers(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || value.length > 64)
    throw new Error(`${field} is invalid`);
  const result = value.map((item, index) => text(item, `${field}[${index}]`));
  if (new Set(result).size !== result.length)
    throw new Error(`${field} contains duplicates`);
  return result;
}

const states: TransactionState[] = [
  "noop_clean",
  "blocked",
  "prepared",
  "submitted",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "unknown_ownership",
];
const dispositions = [
  "clean",
  "dirty_self",
  "dirty_upstream",
  "blocked_missing_predecessor",
  "requires_full_recompute",
] as const;
const guidanceByState: Record<
  TransactionState,
  TransactionTransparencyProjection["guidance"]
> = {
  noop_clean: "no_work_required",
  blocked: "resolve_blocker",
  prepared: "review_before_native_queue",
  submitted: "inspect_queue_history",
  running: "inspect_queue_history",
  succeeded: "generation_complete",
  failed: "rerun_available",
  cancelled: "rerun_available",
  unknown_ownership: "ownership_unknown",
};

function expectedActions(
  state: TransactionState,
): Record<TransactionIntent, boolean> {
  return {
    confirm_native_queue: state === "prepared",
    inspect_queue_history: [
      "submitted",
      "running",
      "succeeded",
      "failed",
      "unknown_ownership",
    ].includes(state),
    return_to_native: true,
    rerun: state === "failed" || state === "cancelled",
  };
}

export function decodeTransactionTransparencyProjection(
  value: unknown,
): TransactionTransparencyProjection {
  if (JSON.stringify(value).length > 65_536)
    throw new Error("transaction transparency exceeds its byte bound");
  const wire = object(value, "transaction transparency");
  closed(wire, rootKeys, "transaction transparency");
  if (wire.schema !== "h3.context.transaction_transparency.v1")
    throw new Error("transaction transparency schema is unsupported");
  const workspaceId = text(wire.workspace_id, "workspace_id");
  if (
    !Number.isInteger(wire.workspace_revision) ||
    (wire.workspace_revision as number) < 1 ||
    (wire.workspace_revision as number) > 1_000_000
  )
    throw new Error("workspace_revision is invalid");
  const workspaceFingerprint = text(
    wire.workspace_fingerprint,
    "workspace_fingerprint",
    fingerprint,
  );
  const planFingerprint = text(
    wire.recompute_plan_fingerprint,
    "recompute_plan_fingerprint",
    fingerprint,
  );
  const correlation = object(wire.correlation, "correlation");
  closed(correlation, correlationKeys, "correlation");
  const decodedCorrelation = {
    prompt_id: text(correlation.prompt_id, "correlation.prompt_id"),
    execution_node_id: text(
      correlation.execution_node_id,
      "correlation.execution_node_id",
    ),
  };
  if (typeof wire.selection_safe !== "boolean")
    throw new Error("selection_safe is invalid");
  if (typeof wire.requires_full_recompute !== "boolean")
    throw new Error("requires_full_recompute is invalid");
  const mandatory = identifiers(
    wire.mandatory_segment_ids,
    "mandatory_segment_ids",
  );
  const requested =
    wire.requested_segment_ids === null
      ? null
      : identifiers(wire.requested_segment_ids, "requested_segment_ids");
  const missing = identifiers(
    wire.missing_required_segment_ids,
    "missing_required_segment_ids",
  );
  if (!Array.isArray(wire.decisions) || wire.decisions.length > 64)
    throw new Error("decisions are invalid");
  const decisions = wire.decisions.map((item, index) => {
    const decision = object(item, `decisions[${index}]`);
    closed(
      decision,
      ["segment_id", "disposition", "reason_codes", "triggering_segment_ids"],
      `decisions[${index}]`,
    );
    if (!(dispositions as readonly unknown[]).includes(decision.disposition))
      throw new Error(`decisions[${index}].disposition is invalid`);
    return {
      segment_id: text(decision.segment_id, `decisions[${index}].segment_id`),
      disposition: decision.disposition as (typeof dispositions)[number],
      reason_codes: identifiers(
        decision.reason_codes,
        `decisions[${index}].reason_codes`,
      ),
      triggering_segment_ids: identifiers(
        decision.triggering_segment_ids,
        `decisions[${index}].triggering_segment_ids`,
      ),
    };
  });
  if (
    new Set(decisions.map((item) => item.segment_id)).size !== decisions.length
  )
    throw new Error("decisions contain duplicate segments");
  const transaction = object(wire.transaction, "transaction");
  closed(transaction, transactionKeys, "transaction");
  if (!(states as unknown[]).includes(transaction.state))
    throw new Error("transaction.state is invalid");
  const state = transaction.state as TransactionState;
  if (
    !Number.isInteger(transaction.attempt) ||
    (transaction.attempt as number) < 1 ||
    (transaction.attempt as number) > 8
  )
    throw new Error("transaction.attempt is invalid");
  if (typeof transaction.cancellation_requested !== "boolean")
    throw new Error("transaction.cancellation_requested is invalid");
  const decodedTransaction = {
    transaction_id: text(
      transaction.transaction_id,
      "transaction.transaction_id",
    ),
    transaction_fingerprint: text(
      transaction.transaction_fingerprint,
      "transaction.transaction_fingerprint",
      fingerprint,
    ),
    attempt: transaction.attempt as number,
    state,
    graph_fingerprint: optionalFingerprint(
      transaction.graph_fingerprint,
      "transaction.graph_fingerprint",
    ),
    compiled_prompt_fingerprint: optionalFingerprint(
      transaction.compiled_prompt_fingerprint,
      "transaction.compiled_prompt_fingerprint",
    ),
    queue_prompt_id:
      transaction.queue_prompt_id === null
        ? null
        : text(transaction.queue_prompt_id, "transaction.queue_prompt_id"),
    host_owner_id:
      transaction.host_owner_id === null
        ? null
        : text(transaction.host_owner_id, "transaction.host_owner_id"),
    result_fingerprint: optionalFingerprint(
      transaction.result_fingerprint,
      "transaction.result_fingerprint",
    ),
    cancellation_requested: transaction.cancellation_requested,
  };
  const actions = object(wire.actions, "actions");
  closed(actions, actionKeys, "actions");
  const expected = expectedActions(state);
  for (const key of actionKeys) {
    if (actions[key] !== expected[key])
      throw new Error(`actions.${key} conflicts with transaction state`);
  }
  if (wire.guidance !== guidanceByState[state])
    throw new Error("guidance conflicts with transaction state");
  return {
    schema: "h3.context.transaction_transparency.v1",
    workspace_id: workspaceId,
    workspace_revision: wire.workspace_revision as number,
    workspace_fingerprint: workspaceFingerprint,
    recompute_plan_fingerprint: planFingerprint,
    correlation: decodedCorrelation,
    selection_safe: wire.selection_safe,
    requires_full_recompute: wire.requires_full_recompute,
    mandatory_segment_ids: mandatory,
    requested_segment_ids: requested,
    missing_required_segment_ids: missing,
    decisions,
    transaction: decodedTransaction,
    actions: expected,
    guidance: guidanceByState[state],
  };
}
