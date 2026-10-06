const fingerprintPattern = /^sha256:[0-9a-f]{64}$/;
const proposalIdPattern = /^assist_[A-Za-z0-9_-]{32,96}$/;
const actionIdPattern = /^action_[A-Za-z0-9_-]{32}$/;

const proposalStates = ["active", "accepted", "rejected", "cancelled"] as const;
const sidebarStates = ["proposal", "failed", "cancelled", "idle"] as const;
const lengthBands = [
  "severely_short",
  "below_target",
  "within_target",
  "above_target",
] as const;
const severities = ["info", "warning", "error"] as const;
const evidenceLevels = [
  "official",
  "framework_reference",
  "community_recommended",
  "experimental",
  "modified",
] as const;
const repairShapes = [
  "narrow_text_correction",
  "evidence_continuation",
] as const;
const adoptionConditions = [
  "reaudit_passed",
  "reference_inventory_unchanged",
  "user_text_preserved",
  "output_within_limit",
] as const;
const remediations = [
  "none",
  "retry_later",
  "reduce_request",
  "review_credential",
  "grant_consent",
  "install_backend",
  "select_model",
  "correct_endpoint",
  "change_media",
] as const;

export const ASSISTED_OUTCOME_IDS = [
  "prompt_model.ok",
  "prompt_model.cancelled",
  "prompt_model.timeout",
  "prompt_model.authentication",
  "prompt_model.quota",
  "prompt_model.moderated",
  "prompt_model.unsupported_media",
  "prompt_model.context_exceeded",
  "prompt_model.request_too_large",
  "prompt_model.insufficient_memory",
  "prompt_model.provider_managed_setting",
  "prompt_model.truncated_reasoning",
  "prompt_model.estimate_unavailable",
  "prompt_model.backend_absent",
  "prompt_model.model_missing",
  "prompt_model.digest_mismatch",
  "prompt_model.capability_mismatch",
  "prompt_model.profile_not_qualified",
  "prompt_model.consent_required",
  "prompt_model.egress_refused",
  "prompt_model.transport",
  "prompt_model.malformed_response",
  "prompt_model.provider_error",
  "prompt_model.draft_empty",
  "prompt_model.draft_truncated",
  "prompt_model.draft_instruction_refused",
  "prompt_model.repair_reaudit_failed",
  "prompt_model.repair_reference_drift",
  "prompt_model.repair_user_text_altered",
  "prompt_model.repair_output_truncated",
  "prompt_model.consent_revoked",
  "prompt_model.network_not_permitted",
  "prompt_model.upload_not_consented",
  "prompt_model.payment_required",
  "prompt_model.permission_denied",
  "prompt_model.rate_limited",
  "prompt_model.redirect_refused",
  "prompt_model.destination_unresolved",
] as const;

export type AssistedOutcomeId = (typeof ASSISTED_OUTCOME_IDS)[number];
export type AssistedFailureId =
  AssistedOutcomeId | "assisted.client_request_failed";
export type AssistedProposalState = (typeof proposalStates)[number];

export type AssistedDraftReceipt = Readonly<{
  schema: "h3.context.assisted_draft.receipt.v2";
  action_id: string;
  profile_id: string;
  provider_family: string;
  model_id: string;
  attempts: number;
  outcome_id: AssistedOutcomeId;
  requests: number;
  request_bytes: number;
  response_bytes: number;
  prompt_tokens: number;
  completion_tokens: number;
  duration_ms: number;
  evidence_fingerprint: string;
  provider_revision: number;
  downgraded: boolean;
  observed_model_id: string;
}>;

export type AssistedPromptAudit = Readonly<{
  schema: "h3.context.prompt_fidelity.v2";
  length_band: (typeof lengthBands)[number];
  description_characters: number;
  diagnostics: readonly Readonly<{
    diagnostic_id: string;
    severity: (typeof severities)[number];
    evidence_level: (typeof evidenceLevels)[number];
    location: string;
    message: string;
    remediation: string;
    parameters: Readonly<Record<string, string | number>>;
  }>[];
}>;

export type AssistedPromptProposalProjection = Readonly<{
  schema: "h3.context.assisted_prompt_proposal.v1";
  proposal_id: string;
  proposal_revision: number;
  state: AssistedProposalState;
  workspace_id: string;
  report_revision: number;
  report_fingerprint: string;
  prompt_fingerprint: string;
  candidate_text: string;
  audit: AssistedPromptAudit;
  receipt: AssistedDraftReceipt;
}>;

export type AssistedSidebarResult = Readonly<{
  schema: "h3.context.assisted_sidebar_result.v1";
  state: (typeof sidebarStates)[number];
  outcomeId?: AssistedOutcomeId;
  proposal: AssistedPromptProposalProjection | null;
}>;

export type AssistedActionRequest =
  | Readonly<{ action: "optimize_prompt"; payload: Record<string, never> }>
  | Readonly<{ action: "refine_prompt"; payload: { instruction: string } }>
  | Readonly<{
      action: "edit_assisted_proposal";
      payload: {
        proposal_id: string;
        expected_proposal_revision: number;
        prompt_text: string;
      };
    }>
  | Readonly<{
      action: "accept_assisted_proposal" | "reject_assisted_proposal";
      payload: { proposal_id: string; expected_proposal_revision: number };
    }>
  | Readonly<{
      action: "cancel_assisted_execution";
      payload: Record<string, never>;
    }>;

export const MAX_REVISION_INSTRUCTION_SCALARS = 2_048;
export const MAX_REVISION_INSTRUCTION_BYTES = 8_192;

export function refinementInstructionMetrics(value: string): Readonly<{
  scalars: number;
  bytes: number;
  valid: boolean;
}> {
  const scalars = Array.from(value).length;
  const bytes = new TextEncoder().encode(value).length;
  const malformed =
    /\u0000|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(
      value,
    );
  return {
    scalars,
    bytes,
    valid:
      !/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u.test(
        value,
      ) &&
      !malformed &&
      scalars <= MAX_REVISION_INSTRUCTION_SCALARS &&
      bytes <= MAX_REVISION_INSTRUCTION_BYTES,
  };
}

export function validateAssistedActionRequest(
  value: unknown,
): AssistedActionRequest {
  const wire = object(value);
  exact(wire, ["action", "payload"]);
  const payload = object(wire.payload);
  if (wire.action === "refine_prompt") {
    exact(payload, ["instruction"]);
    if (
      typeof payload.instruction !== "string" ||
      !refinementInstructionMetrics(payload.instruction).valid
    )
      incompatible();
  } else if (
    wire.action === "optimize_prompt" ||
    wire.action === "cancel_assisted_execution"
  ) {
    exact(payload, []);
  } else if (
    wire.action === "edit_assisted_proposal" ||
    wire.action === "accept_assisted_proposal" ||
    wire.action === "reject_assisted_proposal"
  ) {
    exact(
      payload,
      wire.action === "edit_assisted_proposal"
        ? ["proposal_id", "expected_proposal_revision", "prompt_text"]
        : ["proposal_id", "expected_proposal_revision"],
    );
    if (
      typeof payload.proposal_id !== "string" ||
      !proposalIdPattern.test(payload.proposal_id) ||
      !Number.isSafeInteger(payload.expected_proposal_revision) ||
      (payload.expected_proposal_revision as number) < 1 ||
      (payload.expected_proposal_revision as number) > 1_000_000
    )
      incompatible();
    if (
      wire.action === "edit_assisted_proposal" &&
      typeof payload.prompt_text !== "string"
    )
      incompatible();
  } else incompatible();
  return value as AssistedActionRequest;
}

function incompatible(): never {
  throw new Error("assisted prompt response is incompatible");
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    incompatible();
  return value as Record<string, unknown>;
}

function exact(wire: Record<string, unknown>, keys: readonly string[]): void {
  const actual = Object.keys(wire);
  if (
    actual.length !== keys.length ||
    actual.some((key) => !keys.includes(key)) ||
    keys.some((key) => !Object.hasOwn(wire, key))
  )
    incompatible();
}

function member<T extends string>(value: unknown, values: readonly T[]): T {
  if (typeof value !== "string" || !values.includes(value as T)) incompatible();
  return value as T;
}

function boundedString(value: unknown, maximum = 512): string {
  if (typeof value !== "string" || value.length === 0 || value.length > maximum)
    incompatible();
  return value;
}

function count(value: unknown, maximum = Number.MAX_SAFE_INTEGER): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < 0 ||
    (value as number) > maximum
  )
    incompatible();
  return value as number;
}

function fingerprint(value: unknown): string {
  if (typeof value !== "string" || !fingerprintPattern.test(value))
    incompatible();
  return value;
}

function decodeParameters(
  value: unknown,
): Readonly<Record<string, string | number>> {
  const wire = object(value);
  if (Object.keys(wire).length > 16) incompatible();
  for (const [key, parameter] of Object.entries(wire)) {
    if (!key || key.length > 128) incompatible();
    if (
      (typeof parameter !== "string" || parameter.length > 512) &&
      (!Number.isSafeInteger(parameter) ||
        Math.abs(parameter as number) > 1_000_000) &&
      typeof parameter !== "boolean"
    )
      incompatible();
  }
  return wire as Record<string, string | number>;
}

function decodeOutcome(value: unknown): AssistedOutcomeId {
  const wire = object(value);
  exact(wire, [
    "schema",
    "outcome_id",
    "severity",
    "remediation",
    "retryable",
    "parameters",
    "untrusted_provider_detail",
  ]);
  if (
    wire.schema !== "h3.prompt_model.provider.v1" ||
    typeof wire.retryable !== "boolean" ||
    wire.untrusted_provider_detail !== null
  )
    incompatible();
  member(wire.severity, severities);
  member(wire.remediation, remediations);
  decodeParameters(wire.parameters);
  return member(wire.outcome_id, ASSISTED_OUTCOME_IDS);
}

function decodeAudit(value: unknown): AssistedPromptAudit {
  const wire = object(value);
  exact(wire, [
    "schema",
    "length_band",
    "description_characters",
    "diagnostics",
  ]);
  if (
    wire.schema !== "h3.context.prompt_fidelity.v2" ||
    !Array.isArray(wire.diagnostics)
  )
    incompatible();
  if (wire.diagnostics.length > 256) incompatible();
  const diagnostics = wire.diagnostics.map((value) => {
    const item = object(value);
    exact(item, [
      "diagnostic_id",
      "severity",
      "evidence_level",
      "location",
      "message",
      "remediation",
      "parameters",
    ]);
    return Object.freeze({
      diagnostic_id: boundedString(item.diagnostic_id, 128),
      severity: member(item.severity, severities),
      evidence_level: member(item.evidence_level, evidenceLevels),
      location: boundedString(item.location),
      message: boundedString(item.message),
      remediation: boundedString(item.remediation),
      parameters: decodeParameters(item.parameters),
    });
  });
  return Object.freeze({
    schema: "h3.context.prompt_fidelity.v2",
    length_band: member(wire.length_band, lengthBands),
    description_characters: count(wire.description_characters, 65_536),
    diagnostics: Object.freeze(diagnostics),
  });
}

function decodeReceipt(value: unknown): AssistedDraftReceipt {
  const wire = object(value);
  const legacy = wire.schema === "h3.context.assisted_draft.receipt.v1";
  if (!legacy && wire.schema !== "h3.context.assisted_draft.receipt.v2")
    incompatible();
  exact(wire, [
    "schema",
    "action_id",
    "profile_id",
    "provider_family",
    "model_id",
    "attempts",
    "outcome_id",
    "requests",
    "request_bytes",
    "response_bytes",
    "prompt_tokens",
    "completion_tokens",
    "duration_ms",
    "evidence_fingerprint",
    "provider_revision",
    ...(legacy ? [] : ["downgraded", "observed_model_id"]),
  ]);
  const actionId = boundedString(wire.action_id, 39);
  if (!actionIdPattern.test(actionId)) incompatible();
  const attempts = count(wire.attempts, 2);
  if (attempts < 1) incompatible();
  if (!legacy && typeof wire.downgraded !== "boolean") incompatible();
  const observedModelId =
    legacy || wire.observed_model_id === ""
      ? ""
      : boundedString(wire.observed_model_id, 128);
  if (
    observedModelId &&
    !/^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$/.test(observedModelId)
  )
    incompatible();
  return Object.freeze({
    schema: "h3.context.assisted_draft.receipt.v2",
    action_id: actionId,
    profile_id: boundedString(wire.profile_id, 256),
    provider_family: boundedString(wire.provider_family, 256),
    model_id: boundedString(wire.model_id, 256),
    attempts,
    outcome_id: member(wire.outcome_id, ASSISTED_OUTCOME_IDS),
    requests: count(wire.requests),
    request_bytes: count(wire.request_bytes),
    response_bytes: count(wire.response_bytes),
    prompt_tokens: count(wire.prompt_tokens),
    completion_tokens: count(wire.completion_tokens),
    duration_ms: count(wire.duration_ms),
    evidence_fingerprint: fingerprint(wire.evidence_fingerprint),
    provider_revision: count(wire.provider_revision, 1_000_000),
    downgraded: legacy ? false : (wire.downgraded as boolean),
    observed_model_id: observedModelId,
  });
}

export function decodeAssistedPromptProposal(
  value: unknown,
): AssistedPromptProposalProjection {
  const wire = object(value);
  exact(wire, [
    "schema",
    "proposal_id",
    "proposal_revision",
    "state",
    "workspace_id",
    "report_revision",
    "report_fingerprint",
    "prompt_fingerprint",
    "candidate_text",
    "audit",
    "receipt",
  ]);
  if (wire.schema !== "h3.context.assisted_prompt_proposal.v1") incompatible();
  const proposalId = boundedString(wire.proposal_id, 103);
  if (!proposalIdPattern.test(proposalId)) incompatible();
  const candidate = boundedString(wire.candidate_text, 65_536);
  const proposalRevision = count(wire.proposal_revision, 1_000_000);
  if (proposalRevision < 1) incompatible();
  return Object.freeze({
    schema: "h3.context.assisted_prompt_proposal.v1",
    proposal_id: proposalId,
    proposal_revision: proposalRevision,
    state: member(wire.state, proposalStates),
    workspace_id: boundedString(wire.workspace_id, 131),
    report_revision: count(wire.report_revision, 1_000_000),
    report_fingerprint: fingerprint(wire.report_fingerprint),
    prompt_fingerprint: fingerprint(wire.prompt_fingerprint),
    candidate_text: candidate,
    audit: decodeAudit(wire.audit),
    receipt: decodeReceipt(wire.receipt),
  });
}

function decodeExecution(value: unknown): AssistedOutcomeId {
  const wire = object(value);
  exact(wire, ["schema", "outcome", "draft", "receipt"]);
  if (wire.schema !== "h3.context.assisted_draft.execution.v1") incompatible();
  const outcome = decodeOutcome(wire.outcome);
  if (wire.receipt !== null) decodeReceipt(wire.receipt);
  if (wire.draft !== null) {
    const draft = object(wire.draft);
    exact(draft, [
      "schema",
      "characters",
      "attempts",
      "repair_shape",
      "adoption",
      "audit",
      "outcome",
    ]);
    if (draft.schema !== "h3.context.assisted_draft.v1") incompatible();
    count(draft.characters, 65_536);
    count(draft.attempts, 2);
    if (draft.repair_shape !== null) member(draft.repair_shape, repairShapes);
    decodeAudit(draft.audit);
    decodeOutcome(draft.outcome);
    if (draft.adoption !== null) {
      const adoption = object(draft.adoption);
      exact(adoption, ["schema", "adopted", "failed_condition", "outcome"]);
      if (
        adoption.schema !== "h3.context.assisted_draft.v1" ||
        typeof adoption.adopted !== "boolean"
      )
        incompatible();
      const failedCondition =
        adoption.failed_condition === null
          ? null
          : member(adoption.failed_condition, adoptionConditions);
      if (adoption.adopted === (failedCondition !== null)) incompatible();
      decodeOutcome(adoption.outcome);
    }
  }
  return outcome;
}

export function decodeAssistedSidebarResult(
  value: unknown,
): AssistedSidebarResult {
  const wire = object(value);
  const state = member(wire.state, sidebarStates);
  const keys =
    state === "proposal" || state === "failed"
      ? ["schema", "state", "execution", "proposal"]
      : ["schema", "state", "proposal"];
  exact(wire, keys);
  if (wire.schema !== "h3.context.assisted_sidebar_result.v1") incompatible();
  const proposal =
    wire.proposal === null ? null : decodeAssistedPromptProposal(wire.proposal);
  if (state === "proposal" && proposal === null) incompatible();
  if (state !== "proposal" && proposal !== null) incompatible();
  const outcomeId = Object.hasOwn(wire, "execution")
    ? decodeExecution(wire.execution)
    : undefined;
  return Object.freeze({
    schema: "h3.context.assisted_sidebar_result.v1",
    state,
    ...(outcomeId === undefined ? {} : { outcomeId }),
    proposal,
  });
}
