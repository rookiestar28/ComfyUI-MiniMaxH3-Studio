import {
  executionCorrelationKeys as correlationKeys,
  semanticProposalClarificationKeys as clarificationKeys,
  semanticProposalReviewGroupKeys as groupKeys,
  semanticProposalReviewHandleKeys as handleKeys,
  semanticProposalReviewItemKeys as itemKeys,
  semanticProposalReviewProjectionKeys as projectionKeys,
} from "./generatedSurface";

export const SEMANTIC_PROPOSAL_REVIEW_HANDLE_SCHEMA =
  "h3.context.semantic_proposal_review_handle.v1" as const;
export const SEMANTIC_PROPOSAL_REVIEW_SCHEMA =
  "h3.context.semantic_proposal_review.v1" as const;
export const SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA =
  "h3.context.semantic_proposal.action_result.v1" as const;

const MAX_WIRE_BYTES = 65_536;
const MAX_ITEMS = 64;
const MAX_TEXT = 4_096;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/;
const reviewId = /^review_[A-Za-z0-9_-]{32,96}$/;
const sensitive =
  /https?:\/\/|file:\/\/|authorization|bearer\s|api[_-]?key|password|secret|token\s*=|sig\s*=|x-amz-|(?:[A-Za-z]:[\\/]|\/(?:home|mnt|tmp|var|Users|private)\/)/i;

export type SemanticProposalReviewHandle = {
  schema: typeof SEMANTIC_PROPOSAL_REVIEW_HANDLE_SCHEMA;
  review_id: string;
  transaction_fingerprint: string;
  workspace_fingerprint: string;
  report_fingerprint: string;
  correlation: { prompt_id: string; execution_node_id: string };
  available: true;
  reason: "review_available";
};

export type SemanticProposalReviewItem = {
  target_id: string;
  summary: string;
  change_kind: "added" | "modified" | "removed";
  reference_labels: string[];
  constraint_labels: string[];
  uncertainty_codes: string[];
  reason_code: string;
};

export type SemanticProposalClarification = {
  clarification_id: string;
  label: string;
  reason_code: "resolution_required";
};

export type SemanticProposalReviewGroup = {
  collection:
    "subjects" | "scenes" | "actions" | "cameras" | "styles" | "audios";
  items: SemanticProposalReviewItem[];
};

export type SemanticProposalReviewProjection = {
  schema: typeof SEMANTIC_PROPOSAL_REVIEW_SCHEMA;
  review_id: string;
  transaction_fingerprint: string;
  workspace_id: string;
  workspace_revision: number;
  workspace_fingerprint: string;
  report_fingerprint: string;
  attempt: number;
  revision: number;
  state:
    | "clarification_required"
    | "ready_for_review"
    | "accepted"
    | "rejected"
    | "cancelled"
    | "failed";
  segment_id: string;
  correlation: { prompt_id: string; execution_node_id: string };
  changed_collections: SemanticProposalReviewGroup["collection"][];
  groups: SemanticProposalReviewGroup[];
  clarifications: SemanticProposalClarification[];
  uncertainty_codes: string[];
  reason_code: string;
  actions: {
    proposal_read: boolean;
    proposal_resolve: boolean;
    proposal_accept: boolean;
    proposal_reject: boolean;
    proposal_cancel: boolean;
    edit: false;
    regenerate: false;
  };
  action_reasons: {
    edit: "source_owner_unavailable";
    regenerate: "source_owner_unavailable";
  };
  terminal: "accepted" | "rejected" | "cancelled" | "failed" | null;
};

export type SemanticProposalActionResult = {
  schema: typeof SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA;
  outcome: "read" | "resolved" | "accepted" | "rejected" | "cancelled";
  reason: string;
  review: SemanticProposalReviewProjection;
};

const resultKeys = ["schema", "outcome", "reason", "review"] as const;
const actionKeys = [
  "proposal_read",
  "proposal_resolve",
  "proposal_accept",
  "proposal_reject",
  "proposal_cancel",
  "edit",
  "regenerate",
] as const;
const actionReasonKeys = ["edit", "regenerate"] as const;
const collections = [
  "subjects",
  "scenes",
  "actions",
  "cameras",
  "styles",
  "audios",
] as const;

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
    throw new Error(`${field} is not closed`);
}

function text(
  value: unknown,
  field: string,
  pattern: RegExp = identifier,
): string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > MAX_TEXT ||
    !pattern.test(value) ||
    sensitive.test(value)
  )
    throw new Error(`${field} is invalid`);
  return value;
}

function bounded(value: unknown): void {
  if (
    new TextEncoder().encode(JSON.stringify(value)).byteLength > MAX_WIRE_BYTES
  )
    throw new Error("semantic proposal review exceeds the byte limit");
}

function correlation(
  value: unknown,
): SemanticProposalReviewHandle["correlation"] {
  const wire = object(value, "review correlation");
  closed(wire, correlationKeys, "review correlation");
  return {
    prompt_id: text(wire.prompt_id, "correlation.prompt_id"),
    execution_node_id: text(
      wire.execution_node_id,
      "correlation.execution_node_id",
    ),
  };
}

export function decodeSemanticProposalReviewHandle(
  value: unknown,
): SemanticProposalReviewHandle {
  const wire = object(value, "semantic proposal review handle");
  closed(wire, handleKeys, "semantic proposal review handle");
  if (
    wire.schema !== SEMANTIC_PROPOSAL_REVIEW_HANDLE_SCHEMA ||
    wire.available !== true ||
    wire.reason !== "review_available"
  )
    throw new Error("semantic proposal review handle is incompatible");
  const result: SemanticProposalReviewHandle = {
    schema: SEMANTIC_PROPOSAL_REVIEW_HANDLE_SCHEMA,
    review_id: text(wire.review_id, "review_id", reviewId),
    transaction_fingerprint: text(
      wire.transaction_fingerprint,
      "transaction_fingerprint",
      fingerprint,
    ),
    workspace_fingerprint: text(
      wire.workspace_fingerprint,
      "workspace_fingerprint",
      fingerprint,
    ),
    report_fingerprint: text(
      wire.report_fingerprint,
      "report_fingerprint",
      fingerprint,
    ),
    correlation: correlation(wire.correlation),
    available: true,
    reason: "review_available",
  };
  bounded(result);
  return result;
}

function stringList(
  value: unknown,
  field: string,
  maximum: number,
  pattern: RegExp = identifier,
): string[] {
  if (!Array.isArray(value) || value.length > maximum)
    throw new Error(`${field} is invalid`);
  const result = value.map((item, index) =>
    text(item, `${field}[${index}]`, pattern),
  );
  if (new Set(result).size !== result.length)
    throw new Error(`${field} contains duplicates`);
  return result;
}

export function decodeSemanticProposalReviewProjection(
  value: unknown,
): SemanticProposalReviewProjection {
  const wire = object(value, "semantic proposal review");
  closed(wire, projectionKeys, "semantic proposal review");
  if (wire.schema !== SEMANTIC_PROPOSAL_REVIEW_SCHEMA)
    throw new Error("semantic proposal review schema is incompatible");
  const changed = stringList(
    wire.changed_collections,
    "changed_collections",
    MAX_ITEMS,
  );
  if (
    changed.some((item) => !(collections as readonly string[]).includes(item))
  )
    throw new Error("changed_collections is unsupported");
  if (!Array.isArray(wire.groups) || wire.groups.length !== changed.length)
    throw new Error("review groups are invalid");
  let itemCount = 0;
  const groups = wire.groups.map((value, groupIndex) => {
    const group = object(value, `groups[${groupIndex}]`);
    closed(group, groupKeys, `groups[${groupIndex}]`);
    if (
      group.collection !== changed[groupIndex] ||
      !(collections as readonly unknown[]).includes(group.collection) ||
      !Array.isArray(group.items) ||
      group.items.length < 1 ||
      group.items.length > MAX_ITEMS
    )
      throw new Error(`groups[${groupIndex}] is invalid`);
    const items = group.items.map((value, itemIndex) => {
      const item = object(value, `groups[${groupIndex}].items[${itemIndex}]`);
      closed(item, itemKeys, `groups[${groupIndex}].items[${itemIndex}]`);
      itemCount += 1;
      return {
        target_id: text(item.target_id, "review target_id"),
        summary: text(item.summary, "review summary", /^[\s\S]{1,4096}$/),
        change_kind: (() => {
          if (
            !["added", "modified", "removed"].includes(String(item.change_kind))
          )
            throw new Error("review change_kind is invalid");
          return item.change_kind as SemanticProposalReviewItem["change_kind"];
        })(),
        reference_labels: stringList(
          item.reference_labels,
          "review reference_labels",
          MAX_ITEMS,
          /^[\s\S]{1,4096}$/,
        ),
        constraint_labels: stringList(
          item.constraint_labels,
          "review constraint_labels",
          MAX_ITEMS,
          /^[\s\S]{1,4096}$/,
        ),
        uncertainty_codes: stringList(
          item.uncertainty_codes,
          "review item uncertainty_codes",
          32,
        ),
        reason_code: text(item.reason_code, "review item reason_code"),
      };
    });
    if (
      new Set(items.map((item) => item.target_id)).size !== items.length ||
      itemCount > MAX_ITEMS
    )
      throw new Error("review items exceed the closed bound");
    return {
      collection: group.collection as SemanticProposalReviewGroup["collection"],
      items,
    };
  });
  const actions = object(wire.actions, "review actions");
  closed(actions, actionKeys, "review actions");
  if (
    Object.values(actions).some((enabled) => typeof enabled !== "boolean") ||
    actions.proposal_read !== true ||
    actions.edit !== false ||
    actions.regenerate !== false
  )
    throw new Error("review actions are invalid");
  const reasons = object(wire.action_reasons, "review action reasons");
  closed(reasons, actionReasonKeys, "review action reasons");
  if (
    reasons.edit !== "source_owner_unavailable" ||
    reasons.regenerate !== "source_owner_unavailable"
  )
    throw new Error("review action reasons are invalid");
  const states = [
    "clarification_required",
    "ready_for_review",
    "accepted",
    "rejected",
    "cancelled",
    "failed",
  ] as const;
  const terminals = ["accepted", "rejected", "cancelled", "failed"] as const;
  if (!(states as readonly unknown[]).includes(wire.state))
    throw new Error("review state is invalid");
  if (
    wire.terminal !== null &&
    !(terminals as readonly unknown[]).includes(wire.terminal)
  )
    throw new Error("review terminal state is invalid");
  if (
    !Number.isInteger(wire.workspace_revision) ||
    (wire.workspace_revision as number) < 1 ||
    (wire.workspace_revision as number) > 1_000_000 ||
    !Number.isInteger(wire.attempt) ||
    (wire.attempt as number) < 1 ||
    (wire.attempt as number) > 8 ||
    !Number.isInteger(wire.revision) ||
    (wire.revision as number) < 1 ||
    (wire.revision as number) > 32
  )
    throw new Error("review revision is invalid");
  const result: SemanticProposalReviewProjection = {
    schema: SEMANTIC_PROPOSAL_REVIEW_SCHEMA,
    review_id: text(wire.review_id, "review_id", reviewId),
    transaction_fingerprint: text(
      wire.transaction_fingerprint,
      "transaction_fingerprint",
      fingerprint,
    ),
    workspace_id: text(wire.workspace_id, "workspace_id"),
    workspace_revision: wire.workspace_revision as number,
    workspace_fingerprint: text(
      wire.workspace_fingerprint,
      "workspace_fingerprint",
      fingerprint,
    ),
    report_fingerprint: text(
      wire.report_fingerprint,
      "report_fingerprint",
      fingerprint,
    ),
    attempt: wire.attempt as number,
    revision: wire.revision as number,
    state: wire.state as SemanticProposalReviewProjection["state"],
    segment_id: text(wire.segment_id, "segment_id"),
    correlation: correlation(wire.correlation),
    changed_collections: changed as SemanticProposalReviewGroup["collection"][],
    groups,
    clarifications: (() => {
      if (
        !Array.isArray(wire.clarifications) ||
        wire.clarifications.length > 32
      )
        throw new Error("review clarifications are invalid");
      const values = wire.clarifications.map((value, index) => {
        const clarification = object(value, `clarifications[${index}]`);
        closed(clarification, clarificationKeys, `clarifications[${index}]`);
        if (clarification.reason_code !== "resolution_required")
          throw new Error("review clarification reason is invalid");
        return {
          clarification_id: text(
            clarification.clarification_id,
            "review clarification_id",
          ),
          label: text(
            clarification.label,
            "review clarification label",
            /^[\s\S]{1,4096}$/,
          ),
          reason_code: "resolution_required" as const,
        };
      });
      if (
        new Set(values.map((value) => value.clarification_id)).size !==
        values.length
      )
        throw new Error("review clarifications contain duplicates");
      return values;
    })(),
    uncertainty_codes: stringList(
      wire.uncertainty_codes,
      "review uncertainty_codes",
      32,
    ),
    reason_code: text(wire.reason_code, "review reason_code"),
    actions: actions as SemanticProposalReviewProjection["actions"],
    action_reasons:
      reasons as SemanticProposalReviewProjection["action_reasons"],
    terminal: wire.terminal as SemanticProposalReviewProjection["terminal"],
  };
  bounded(result);
  return result;
}

export function decodeSemanticProposalActionResult(
  value: unknown,
): SemanticProposalActionResult {
  const wire = object(value, "semantic proposal action result");
  closed(wire, resultKeys, "semantic proposal action result");
  const outcomes = ["read", "resolved", "accepted", "rejected", "cancelled"];
  if (
    wire.schema !== SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA ||
    !outcomes.includes(wire.outcome as string)
  )
    throw new Error("semantic proposal action result is incompatible");
  const result: SemanticProposalActionResult = {
    schema: SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA,
    outcome: wire.outcome as SemanticProposalActionResult["outcome"],
    reason: text(wire.reason, "review reason"),
    review: decodeSemanticProposalReviewProjection(wire.review),
  };
  bounded(result);
  return result;
}
