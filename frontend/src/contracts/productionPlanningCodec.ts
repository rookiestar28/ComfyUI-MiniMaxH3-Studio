export const PRODUCTION_PLANNING_ACTION_SCHEMA =
  "h3.context.production_planning.action.v1" as const;
export const PRODUCTION_PLANNING_PROJECTION_SCHEMA =
  "h3.context.production_planning.projection.v1" as const;
export const PRODUCTION_AUTOMATIC_PLAN_SCHEMA =
  "h3.context.production_automatic_plan_projection.v1" as const;

export type PlanningAction =
  | "prepare_context"
  | "admit_storyboard"
  | "propose"
  | "import_plan"
  | "read_plan";
export type SegmentationPolicy =
  "auto_storyboard" | "fixed_5" | "fixed_10" | "fixed_12" | "fixed_15";
export type PlanningSegment = Readonly<{
  segment_id: string;
  ordinal: number;
  task_mode: string;
  duration_seconds: number;
  local_prompt: string;
}>;
export type PlanningProposal = Readonly<{
  proposal_id: string;
  revision: number;
  fingerprint: string;
  importable: boolean;
  blocker_codes: readonly string[];
  start_hold_codes: readonly string[];
  segments: readonly PlanningSegment[];
}>;
export type PlanningProjection = Readonly<{
  schema: typeof PRODUCTION_PLANNING_PROJECTION_SCHEMA;
  request_id: string;
  workspace_handle: string;
  workspace_id: string;
  workspace_revision: number;
  workspace_fingerprint: string;
  planning_context_id: string;
  planning_revision: number;
  source_duration_seconds: number;
  target_seconds: number;
  policy: SegmentationPolicy;
  admission_id: string | null;
  proposal: PlanningProposal | null;
}>;
export type AutomaticPlanProjection = Readonly<{
  schema: typeof PRODUCTION_AUTOMATIC_PLAN_SCHEMA;
  request_id: string;
  workspace_id: string;
  workspace_revision: number;
  workspace_fingerprint: string;
  proposal_id: string;
  proposal_revision: number;
  proposal_fingerprint: string;
  plan_fingerprint: string;
  segment_ids: readonly string[];
  materialization_receipt_fingerprints: readonly string[];
  cut_boundary_receipts: readonly Readonly<{
    schema: string;
    predecessor_segment_id: string;
    successor_segment_id: string;
    boundary_milliseconds: number;
    join_policy: "cut";
    predecessor_artifact_required: false;
  }>[];
  manifest_fingerprints: readonly string[];
  reconstruction_order: readonly string[];
  start_hold_codes: readonly string[];
  startable: boolean;
}>;
export type PlanningResponse = PlanningProjection | AutomaticPlanProjection;

const identifier = /^[A-Za-z][A-Za-z0-9_.-]{0,127}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const workspace = /^pw_[A-Za-z0-9_-]{32,96}$/;
const context = /^ws_[A-Za-z0-9_-]{32,96}$/;
const policies = new Set([
  "auto_storyboard",
  "fixed_5",
  "fixed_10",
  "fixed_12",
  "fixed_15",
]);
const modes = new Set(["t2va", "i2va", "fl2va", "l2va", "ref2va"]);
const codes = new Set([
  "hard_content_crosses_boundary",
  "native_mapping_unavailable",
  "required_asset_missing",
  "managed_execution_qualification_pending",
  "managed_execution_unsupported",
  "local_reference_unavailable",
]);
const cas = [
  "workspace_handle",
  "expected_workspace_revision",
  "expected_workspace_fingerprint",
];
const plan = [...cas, "planning_context_id", "expected_planning_revision"];
const payloadKeys: Record<PlanningAction, readonly string[]> = {
  prepare_context: [
    ...cas,
    "context_workspace_handle",
    "expected_report_revision",
    "expected_report_fingerprint",
    "expected_planning_revision",
    "target_seconds",
    "policy",
  ],
  admit_storyboard: [...plan, "source_kind", "typed_rows", "user_reviewed"],
  propose: [...plan, "admission_id"],
  import_plan: [...plan, "proposal_id"],
  read_plan: plan,
};
function reject(): never {
  throw new Error("invalid production planning contract");
}
function object(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  )
    return reject();
  const wire = value as Record<string, unknown>;
  if (
    Object.keys(wire).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(wire, key))
  )
    return reject();
  return wire;
}
function text(value: unknown, pattern = identifier): string {
  if (typeof value !== "string" || !pattern.test(value)) return reject();
  return value;
}
function integer(value: unknown, min = 1, max = 1_000_000): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < min ||
    value > max
  )
    return reject();
  return value;
}
function array(value: unknown, max: number): unknown[] {
  if (!Array.isArray(value) || value.length > max) return reject();
  return value;
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") return reject();
  return value;
}
function strings(
  value: unknown,
  max: number,
  pattern = identifier,
): readonly string[] {
  return Object.freeze(array(value, max).map((row) => text(row, pattern)));
}
function enumCodes(value: unknown): readonly string[] {
  const rows = strings(value, 6);
  if (rows.some((row) => !codes.has(row)) || new Set(rows).size !== rows.length)
    return reject();
  return rows;
}
function content(value: unknown, max = 8192): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > max ||
    value.includes("\0")
  )
    return reject();
  return value;
}
function freeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

export function encodeProductionPlanningAction(
  requestId: string,
  action: PlanningAction,
  payload: Record<string, unknown>,
) {
  text(requestId);
  if (!Object.hasOwn(payloadKeys, action)) return reject();
  const wire = object(payload, payloadKeys[action]);
  text(wire.workspace_handle, workspace);
  integer(wire.expected_workspace_revision);
  text(wire.expected_workspace_fingerprint, fingerprint);
  integer(
    wire.expected_planning_revision,
    action === "prepare_context" ? 0 : 1,
  );
  if (action === "prepare_context") {
    text(wire.context_workspace_handle, context);
    integer(wire.expected_report_revision, 0);
    text(wire.expected_report_fingerprint, fingerprint);
    integer(wire.target_seconds, 4, 60);
    if (!policies.has(text(wire.policy))) return reject();
  } else text(wire.planning_context_id);
  if (action === "propose") text(wire.admission_id);
  if (action === "import_plan") text(wire.proposal_id);
  if (action === "admit_storyboard") {
    boolean(wire.user_reviewed);
    const rows = array(wire.typed_rows, 64);
    if (wire.source_kind === "canonical_optimized_prompt") {
      if (rows.length || wire.user_reviewed) return reject();
    } else if (
      wire.source_kind !== "user_reviewed_typed_rows" ||
      wire.user_reviewed !== true ||
      !rows.length
    )
      return reject();
    rows.forEach((row) => {
      const shot = object(row, [
        "schema",
        "shot_id",
        "ordinal",
        "start_milliseconds",
        "end_milliseconds",
        "text",
        "subject_ids",
        "asset_ids",
        "exact_dialogue",
        "visible_text",
        "hard_boundary",
        "source_span",
      ]);
      if (shot.schema !== "h3.context.storyboard_shot.v1") return reject();
      text(shot.shot_id);
      integer(shot.ordinal, 1, 64);
      integer(shot.start_milliseconds, 0, 59999);
      integer(shot.end_milliseconds, 1, 60000);
      if (
        (shot.end_milliseconds as number) <= (shot.start_milliseconds as number)
      )
        return reject();
      content(shot.text);
      strings(shot.subject_ids, 64);
      strings(shot.asset_ids, 64);
      boolean(shot.hard_boundary);
      array(shot.exact_dialogue, 64).forEach((row) => content(row));
      array(shot.visible_text, 64).forEach((row) => content(row));
      const span = array(shot.source_span, 2);
      if (span.length !== 2) return reject();
      span.forEach((row) => integer(row, 0, 65536));
    });
  }
  const result = {
    schema: PRODUCTION_PLANNING_ACTION_SCHEMA,
    request_id: requestId,
    action,
    payload,
  };
  const serialized = JSON.stringify(result);
  if (new TextEncoder().encode(serialized).byteLength > 262144) return reject();
  return freeze(JSON.parse(serialized) as typeof result);
}

export function planningSelectors(
  projection: PlanningProjection,
): Record<string, unknown> {
  return {
    workspace_handle: projection.workspace_handle,
    expected_workspace_revision: projection.workspace_revision,
    expected_workspace_fingerprint: projection.workspace_fingerprint,
    planning_context_id: projection.planning_context_id,
    expected_planning_revision: projection.planning_revision,
  };
}

export function decodeProductionPlanningResponse(
  value: unknown,
): PlanningResponse {
  if (JSON.stringify(value)?.length > 1_048_576) return reject();
  const schema = (value as Record<string, unknown> | null)?.schema;
  if (schema === PRODUCTION_PLANNING_PROJECTION_SCHEMA) {
    const wire = object(value, [
      "schema",
      "request_id",
      "workspace_handle",
      "workspace_id",
      "workspace_revision",
      "workspace_fingerprint",
      "planning_context_id",
      "planning_revision",
      "source_duration_seconds",
      "target_seconds",
      "policy",
      "admission_id",
      "proposal",
    ]);
    text(wire.request_id);
    text(wire.workspace_handle, workspace);
    text(wire.workspace_id);
    integer(wire.workspace_revision);
    text(wire.workspace_fingerprint, fingerprint);
    text(wire.planning_context_id);
    integer(wire.planning_revision);
    integer(wire.source_duration_seconds, 4, 15);
    integer(wire.target_seconds, 4, 60);
    if (!policies.has(text(wire.policy))) return reject();
    if (wire.admission_id !== null) text(wire.admission_id);
    if (wire.proposal !== null) {
      const proposal = object(wire.proposal, [
        "proposal_id",
        "revision",
        "fingerprint",
        "importable",
        "blocker_codes",
        "start_hold_codes",
        "segments",
      ]);
      text(proposal.proposal_id);
      integer(proposal.revision);
      text(proposal.fingerprint, fingerprint);
      boolean(proposal.importable);
      const blockers = enumCodes(proposal.blocker_codes);
      enumCodes(proposal.start_hold_codes);
      if (
        proposal.importable !== (blockers.length === 0) ||
        wire.admission_id === null
      )
        return reject();
      const segments = array(proposal.segments, 15);
      if (!segments.length) return reject();
      const ids = new Set<string>();
      let duration = 0;
      segments.forEach((row, index) => {
        const segment = object(row, [
          "segment_id",
          "ordinal",
          "task_mode",
          "duration_seconds",
          "local_prompt",
        ]);
        const id = text(segment.segment_id);
        if (
          ids.has(id) ||
          integer(segment.ordinal, 1, 15) !== index + 1 ||
          !modes.has(text(segment.task_mode))
        )
          return reject();
        ids.add(id);
        duration += integer(segment.duration_seconds, 4, 15);
        content(segment.local_prompt, 65536);
      });
      if (duration !== wire.target_seconds) return reject();
    }
    return freeze(structuredClone(wire)) as PlanningProjection;
  }
  if (schema !== PRODUCTION_AUTOMATIC_PLAN_SCHEMA) return reject();
  const wire = object(value, [
    "schema",
    "request_id",
    "workspace_id",
    "workspace_revision",
    "workspace_fingerprint",
    "proposal_id",
    "proposal_revision",
    "proposal_fingerprint",
    "plan_fingerprint",
    "segment_ids",
    "materialization_receipt_fingerprints",
    "cut_boundary_receipts",
    "manifest_fingerprints",
    "reconstruction_order",
    "start_hold_codes",
    "startable",
  ]);
  text(wire.request_id);
  text(wire.workspace_id);
  integer(wire.workspace_revision);
  text(wire.workspace_fingerprint, fingerprint);
  text(wire.proposal_id);
  integer(wire.proposal_revision);
  text(wire.proposal_fingerprint, fingerprint);
  text(wire.plan_fingerprint, fingerprint);
  const ids = strings(wire.segment_ids, 15);
  if (!ids.length || new Set(ids).size !== ids.length) return reject();
  const receipts = strings(
    wire.materialization_receipt_fingerprints,
    15,
    fingerprint,
  );
  const manifests = strings(wire.manifest_fingerprints, 15, fingerprint);
  const order = strings(wire.reconstruction_order, 15);
  const cuts = array(wire.cut_boundary_receipts, 14);
  if (
    receipts.length !== ids.length ||
    manifests.length !== ids.length ||
    order.length !== ids.length ||
    order.some((id, i) => id !== ids[i]) ||
    cuts.length !== ids.length - 1
  )
    return reject();
  let boundary = 0;
  cuts.forEach((row, index) => {
    const cut = object(row, [
      "schema",
      "predecessor_segment_id",
      "successor_segment_id",
      "boundary_milliseconds",
      "join_policy",
      "predecessor_artifact_required",
    ]);
    const next = integer(cut.boundary_milliseconds, 1, 59999);
    if (
      cut.schema !== "h3.context.production_cut_boundary_receipt.v1" ||
      cut.predecessor_segment_id !== ids[index] ||
      cut.successor_segment_id !== ids[index + 1] ||
      cut.join_policy !== "cut" ||
      cut.predecessor_artifact_required !== false ||
      next <= boundary
    )
      return reject();
    boundary = next;
  });
  const holds = enumCodes(wire.start_hold_codes);
  if (boolean(wire.startable) !== (holds.length === 0)) return reject();
  return freeze(structuredClone(wire)) as AutomaticPlanProjection;
}
