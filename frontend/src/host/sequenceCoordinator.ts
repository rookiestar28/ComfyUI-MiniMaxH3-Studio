import {
  decodeGenerationSequenceProjection,
  type GenerationSequenceProjection,
} from "../contracts/generationSequenceCodec";
import {
  decodeProductionWorkbenchProjection,
  type ProductionWorkbenchProjection,
} from "../contracts/productionWorkbenchCodec";
import type { PreparedGraphObservation } from "./appMode";
import type { SaveVideoArtifactEvent } from "./sidebarHost";

export const SEQUENCE_COORDINATOR_ACTION_SCHEMA =
  "h3.context.generation_coordinator.action.v1" as const;
export const SEQUENCE_COORDINATOR_RESPONSE_SCHEMA =
  "h3.context.generation_coordinator.response.v1" as const;
export const MANAGED_CHILD_COORDINATOR_RESPONSE_SCHEMA =
  "h3.context.generation_coordinator.managed_child_response.v1" as const;
export const MANAGED_MEMBER_COORDINATOR_RESPONSE_SCHEMA =
  "h3.context.generation_coordinator.managed_member_response.v1" as const;
export const COORDINATOR_PRODUCTION_AUTHORITY_SCHEMA =
  "h3.context.generation_coordinator.production_authority.v1" as const;
export const COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA =
  "h3.context.generation_coordinator.production_member_authority.v1" as const;
export const SEQUENCE_COORDINATOR_ARTIFACT_AUTHORITY_SCHEMA =
  "h3.context.generation_coordinator.artifact_authority.v1" as const;
export const COORDINATOR_ERROR_SCHEMA =
  "h3.context.generation_coordinator.error.v1" as const;
export const SEQUENCE_COORDINATOR_ROUTE =
  "/h3-context/v1/generation/coordinator" as const;
export const RELEASE_REQUEST_V2_SCHEMA =
  "h3.context.managed_run_release_request.v2" as const;

export const MANAGED_RUN_ACTIONS = [
  "prepare_managed_run",
  "submit_managed_run",
  "close_managed_run",
  "read_managed_run",
] as const;

export type SequenceCoordinatorAction =
  | (typeof MANAGED_RUN_ACTIONS)[number]
  | "prepare_sequence"
  | "record_submission"
  | "record_running"
  | "record_artifact"
  | "record_terminal"
  | "read_sequence"
  | "cancel_sequence"
  | "release_sequence";

export type SequenceCoordinatorDisposition =
  | "prepared"
  | "submitted"
  | "running"
  | "artifact_verified"
  | "verification_pending"
  | "output_verification_failed"
  | "succeeded"
  | "failed"
  | "interrupted"
  | "cancelled"
  | "unknown_ownership"
  | "current"
  | "released"
  | "detached"
  | "detached_terminal"
  | "detached_unknown_ownership";

export type SequenceCoordinatorErrorCategory =
  | "artifact_content_invalid"
  | "artifact_locator_rejected"
  | "artifact_authority_mismatch"
  | "artifact_store_unavailable"
  | "run_authority_mismatch"
  | "unsupported_failure"
  | "internal_failure";

export type SequenceCoordinatorRetryDisposition =
  "none" | "retry_output_verification" | "inspect_native" | "use_native";

type SequenceCoordinatorCommon = Readonly<{
  runHandle: string;
  disposition: SequenceCoordinatorDisposition;
  artifactAuthority: Readonly<{
    schema: typeof SEQUENCE_COORDINATOR_ARTIFACT_AUTHORITY_SCHEMA;
    receiptFingerprint: string;
    byteLength: number;
  }> | null;
  terminalFingerprint: string | null;
  sequence: GenerationSequenceProjection;
}>;

export type CoordinatorProductionAuthority = Readonly<{
  schema: typeof COORDINATOR_PRODUCTION_AUTHORITY_SCHEMA;
  workspaceHandle: string;
  workspaceId: string;
  workspaceRevision: number;
  workspaceFingerprint: string;
  automaticPlanFingerprint: string;
}>;

/** One member of an accumulating Production project; the sequence is its own execution. */
export type CoordinatorProductionMemberAuthority = Readonly<{
  schema: typeof COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA;
  workspaceHandle: string;
  workspaceId: string;
  memberSegmentId: string;
}>;

export type SequenceCoordinatorResult = SequenceCoordinatorCommon &
  (
    | Readonly<{
        schema: typeof SEQUENCE_COORDINATOR_RESPONSE_SCHEMA;
        production: ProductionWorkbenchProjection;
      }>
    | Readonly<{
        schema: typeof MANAGED_CHILD_COORDINATOR_RESPONSE_SCHEMA;
        production: null;
        productionAuthority: CoordinatorProductionAuthority;
      }>
    | Readonly<{
        schema: typeof MANAGED_MEMBER_COORDINATOR_RESPONSE_SCHEMA;
        production: null;
        productionMemberAuthority: CoordinatorProductionMemberAuthority;
      }>
  );

export type PrepareSequencePayload = Readonly<{
  workspace_handle: string;
  expected_workspace_revision: number;
  expected_workspace_fingerprint: string;
  correlation: Readonly<{ prompt_id: string; execution_node_id: string }>;
  observation: PreparedGraphObservation;
}>;

export type RecordSubmissionPayload = Readonly<{
  run_handle: string;
  expected_state_fingerprint: string;
  job_id: string;
  transaction_id: string;
  graph_fingerprint: string;
  compiled_prompt_fingerprint: string;
  queue_prompt_id: string;
}>;

export type RecordRunningPayload = Readonly<{
  run_handle: string;
  expected_state_fingerprint: string;
  queue_prompt_id: string;
  host_owner_id: string;
}>;

export type RecordArtifactPayload = Readonly<{
  run_handle: string;
  expected_state_fingerprint: string;
  queue_prompt_id: string;
  output_node_id: string;
  locator: SaveVideoArtifactEvent["locator"];
}>;

/**
 * What a `release_sequence` request is asking for. The backend answers from the run's own state,
 * so these are requests rather than assertions: `detach_client` on a run that has already settled
 * answers `detached_terminal`, and that is a success, not a mismatch.
 */
export type ReleaseIntent =
  "cleanup_pre_submit" | "detach_client" | "cleanup_terminal";

/**
 * The legacy payload. It carries no intent and means the only one it ever meant: undo a
 * preparation the host never accepted. It is no longer unconditional -- a run the host may be
 * executing is refused -- so new code should send V2 and read the disposition.
 */
export type ReleaseSequencePayloadV1 = Readonly<{ run_handle: string }>;

export type ReleaseSequencePayloadV2 = Readonly<{
  schema: typeof RELEASE_REQUEST_V2_SCHEMA;
  run_handle: string;
  intent: ReleaseIntent;
  expected_state_fingerprint: string;
  observed_terminal_fingerprint?: string;
}>;

/**
 * Build a V2 release request.
 *
 * IMPORTANT: the terminal proof is required for `cleanup_terminal` and forbidden for every other
 * intent, and the backend closes the payload per intent, so sending the wrong shape is a 400
 * rather than a silently ignored member. Building it here rather than at each call site is what
 * makes that impossible to get wrong.
 */
export function releaseSequencePayload(
  runHandle: string,
  intent: ReleaseIntent,
  expectedStateFingerprint: string,
  observedTerminalFingerprint?: string,
): ReleaseSequencePayloadV2 {
  if (intent === "cleanup_terminal") {
    if (typeof observedTerminalFingerprint !== "string")
      throw new Error(
        "cleanup_terminal requires the observed terminal fingerprint",
      );
    return Object.freeze({
      schema: RELEASE_REQUEST_V2_SCHEMA,
      run_handle: runHandle,
      intent,
      expected_state_fingerprint: expectedStateFingerprint,
      observed_terminal_fingerprint: observedTerminalFingerprint,
    });
  }
  if (observedTerminalFingerprint !== undefined)
    throw new Error(
      "only cleanup_terminal carries an observed terminal fingerprint",
    );
  return Object.freeze({
    schema: RELEASE_REQUEST_V2_SCHEMA,
    run_handle: runHandle,
    intent,
    expected_state_fingerprint: expectedStateFingerprint,
  });
}

/**
 * Whether a failed V2 release means the backend does not implement client detach.
 *
 * The run handle has to be passed in, and that is the whole point of the second argument. The
 * error envelope carries a category, not the backend's own error code, so a 400 is all the client
 * can see -- and `unsupported_release_schema` is not the only 400 the route emits: a malformed
 * `run_handle` produces `invalid_run_handle` from the same status. Validating the handle here
 * first is what separates "this backend predates the detach union" from "my own request was
 * wrong", without inventing a code the wire does not carry.
 *
 * IMPORTANT: everything else in the payload is well formed by construction, because
 * `releaseSequencePayload` builds it. Callers use this to hide the detach affordance, and must
 * never report a detach that did not happen -- an unsupported backend still owns the run, and
 * telling the user they have left it is the one answer that is untrue.
 */
export function isClientDetachUnsupported(
  error: SequenceCoordinatorClientError,
  requestedRunHandle: string,
): boolean {
  if (!runHandle.test(requestedRunHandle)) return false;
  return error.status === 400;
}

/** Whether a disposition means the run was retained rather than removed. */
export function isDetachedDisposition(
  disposition: SequenceCoordinatorDisposition,
): boolean {
  return (
    disposition === "detached" ||
    disposition === "detached_terminal" ||
    disposition === "detached_unknown_ownership"
  );
}

export type RecordTerminalPayload = Readonly<{
  run_handle: string;
  expected_state_fingerprint: string;
  queue_prompt_id: string;
  kind: "success" | "error" | "interrupted";
}>;

type FetchResponse = Readonly<{
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}>;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const runHandle = /^mc_[A-Za-z0-9_-]{32,96}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const dispositions = new Set<SequenceCoordinatorDisposition>([
  "prepared",
  "submitted",
  "running",
  "artifact_verified",
  "verification_pending",
  "output_verification_failed",
  "succeeded",
  "failed",
  "interrupted",
  "cancelled",
  "unknown_ownership",
  "current",
  "released",
  "detached",
  "detached_terminal",
  "detached_unknown_ownership",
]);
const errorCategories = new Set<SequenceCoordinatorErrorCategory>([
  "artifact_content_invalid",
  "artifact_locator_rejected",
  "artifact_authority_mismatch",
  "artifact_store_unavailable",
  "run_authority_mismatch",
  "unsupported_failure",
  "internal_failure",
]);
const retryDispositions = new Set<SequenceCoordinatorRetryDisposition>([
  "none",
  "retry_output_verification",
  "inspect_native",
  "use_native",
]);
const statusCodes: Readonly<Record<number, string>> = Object.freeze({
  400: "invalid_action",
  403: "origin_rejected",
  404: "run_unavailable",
  408: "artifact_inspection_timeout",
  409: "coordinator_conflict",
  410: "run_gone",
  413: "request_too_large",
  415: "media_type_rejected",
  422: "coordinator_action_rejected",
  429: "coordinator_capacity",
  500: "internal_failure",
  503: "coordinator_unavailable",
});

export class SequenceCoordinatorClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly category: SequenceCoordinatorErrorCategory;
  readonly retryDisposition: SequenceCoordinatorRetryDisposition;
  readonly sameRunAuthority: boolean;

  constructor(
    code: string,
    status: number,
    contract: Readonly<{
      category: SequenceCoordinatorErrorCategory;
      retryDisposition: SequenceCoordinatorRetryDisposition;
      sameRunAuthority: boolean;
    }> = {
      category: "internal_failure",
      retryDisposition: "none",
      sameRunAuthority: false,
    },
  ) {
    super(code);
    this.name = "SequenceCoordinatorClientError";
    this.code = code;
    this.status = status;
    this.category = contract.category;
    this.retryDisposition = contract.retryDisposition;
    this.sameRunAuthority = contract.sameRunAuthority;
  }
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new SequenceCoordinatorClientError("invalid_response", 500);
  return value as Record<string, unknown>;
}

function sameSequenceAuthority(
  production: ProductionWorkbenchProjection,
  sequence: GenerationSequenceProjection,
): boolean {
  const summary = production.generationSequence;
  return (
    summary !== null &&
    summary.schema === sequence.schema &&
    summary.sequenceId === sequence.sequence_id &&
    summary.sequenceFingerprint === sequence.sequence_fingerprint &&
    summary.stateFingerprint === sequence.state_fingerprint &&
    summary.workspaceId === sequence.workspace_id &&
    summary.workspaceRevision === sequence.workspace_revision &&
    summary.workspaceFingerprint === sequence.workspace_fingerprint &&
    summary.correlation.promptId === sequence.correlation.prompt_id &&
    summary.correlation.executionNodeId ===
      sequence.correlation.execution_node_id &&
    production.workspaceId === sequence.workspace_id &&
    production.workspaceRevision === sequence.workspace_revision &&
    production.workspaceFingerprint === sequence.workspace_fingerprint
  );
}

export function decodeSequenceCoordinatorError(
  value: unknown,
  status: number,
): SequenceCoordinatorClientError {
  if (JSON.stringify(value).length > 4096)
    throw new SequenceCoordinatorClientError("invalid_error_response", status);
  const wire = object(value);
  if (
    Object.keys(wire).sort().join() !==
      "category,retry_disposition,same_run_authority,schema" ||
    wire.schema !== COORDINATOR_ERROR_SCHEMA ||
    typeof wire.category !== "string" ||
    !errorCategories.has(wire.category as SequenceCoordinatorErrorCategory) ||
    typeof wire.retry_disposition !== "string" ||
    !retryDispositions.has(
      wire.retry_disposition as SequenceCoordinatorRetryDisposition,
    ) ||
    typeof wire.same_run_authority !== "boolean"
  )
    throw new SequenceCoordinatorClientError("invalid_error_response", status);
  const category = wire.category as SequenceCoordinatorErrorCategory;
  return new SequenceCoordinatorClientError(category, status, {
    category,
    retryDisposition:
      wire.retry_disposition as SequenceCoordinatorRetryDisposition,
    sameRunAuthority: wire.same_run_authority,
  });
}

export function decodeSequenceCoordinatorResult(
  value: unknown,
): SequenceCoordinatorResult {
  if (JSON.stringify(value).length > 1_048_576)
    throw new SequenceCoordinatorClientError("invalid_response", 500);
  const wire = object(value);
  const member = wire.schema === MANAGED_MEMBER_COORDINATOR_RESPONSE_SCHEMA;
  const compact =
    member || wire.schema === MANAGED_CHILD_COORDINATOR_RESPONSE_SCHEMA;
  const expectedKeys = member
    ? "artifact_authority,disposition,production_member_authority,run_handle,schema,sequence,terminal_fingerprint"
    : compact
      ? "artifact_authority,disposition,production_authority,run_handle,schema,sequence,terminal_fingerprint"
      : "artifact_authority,disposition,production,run_handle,schema,sequence,terminal_fingerprint";
  if (
    Object.keys(wire).sort().join() !== expectedKeys ||
    (!compact && wire.schema !== SEQUENCE_COORDINATOR_RESPONSE_SCHEMA) ||
    (compact &&
      new TextEncoder().encode(JSON.stringify(wire)).byteLength > 8192) ||
    typeof wire.run_handle !== "string" ||
    !runHandle.test(wire.run_handle) ||
    typeof wire.disposition !== "string" ||
    !dispositions.has(wire.disposition as SequenceCoordinatorDisposition)
  )
    throw new SequenceCoordinatorClientError("invalid_response", 500);
  let artifactAuthority: SequenceCoordinatorResult["artifactAuthority"] = null;
  if (wire.artifact_authority !== null) {
    const authority = object(wire.artifact_authority);
    if (
      Object.keys(authority).sort().join() !==
        "byte_length,receipt_fingerprint,schema" ||
      authority.schema !== SEQUENCE_COORDINATOR_ARTIFACT_AUTHORITY_SCHEMA ||
      typeof authority.receipt_fingerprint !== "string" ||
      !fingerprint.test(authority.receipt_fingerprint) ||
      typeof authority.byte_length !== "number" ||
      !Number.isInteger(authority.byte_length) ||
      authority.byte_length < 1 ||
      authority.byte_length > 128 * 1024 * 1024
    )
      throw new SequenceCoordinatorClientError("invalid_response", 500);
    artifactAuthority = Object.freeze({
      schema: SEQUENCE_COORDINATOR_ARTIFACT_AUTHORITY_SCHEMA,
      receiptFingerprint: authority.receipt_fingerprint,
      byteLength: authority.byte_length,
    });
  }
  if (
    wire.terminal_fingerprint !== null &&
    (typeof wire.terminal_fingerprint !== "string" ||
      !fingerprint.test(wire.terminal_fingerprint))
  )
    throw new SequenceCoordinatorClientError("invalid_response", 500);
  let sequence: GenerationSequenceProjection;
  try {
    sequence = decodeGenerationSequenceProjection(wire.sequence);
  } catch {
    throw new SequenceCoordinatorClientError("invalid_response", 500);
  }
  const common = {
    runHandle: wire.run_handle,
    disposition: wire.disposition as SequenceCoordinatorDisposition,
    artifactAuthority,
    terminalFingerprint: wire.terminal_fingerprint as string | null,
    sequence,
  };
  if (member) {
    const authority = object(wire.production_member_authority);
    if (
      Object.keys(authority).sort().join() !==
        "member_segment_id,schema,workspace_handle,workspace_id" ||
      authority.schema !== COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA ||
      typeof authority.workspace_handle !== "string" ||
      !/^pw_[A-Za-z0-9_-]{32,96}$/.test(authority.workspace_handle) ||
      typeof authority.workspace_id !== "string" ||
      !identifier.test(authority.workspace_id) ||
      typeof authority.member_segment_id !== "string" ||
      !identifier.test(authority.member_segment_id)
    )
      throw new SequenceCoordinatorClientError("invalid_response", 500);
    // CRITICAL: a member sequence describes its own one-job execution workspace, not the
    // project. Join the reference only through that one job; never decode it as a
    // Production projection or compare the project id with the sequence workspace.
    if (
      sequence.progress.length !== 1 ||
      sequence.progress[0].segment_id !== authority.member_segment_id
    )
      throw new SequenceCoordinatorClientError("cross_authority_response", 500);
    return Object.freeze({
      ...common,
      schema: MANAGED_MEMBER_COORDINATOR_RESPONSE_SCHEMA,
      production: null,
      productionMemberAuthority: Object.freeze({
        schema: COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA,
        workspaceHandle: authority.workspace_handle,
        workspaceId: authority.workspace_id,
        memberSegmentId: authority.member_segment_id,
      }),
    });
  }
  if (compact) {
    const authority = object(wire.production_authority);
    if (
      Object.keys(authority).sort().join() !==
        "automatic_plan_fingerprint,schema,workspace_fingerprint,workspace_handle,workspace_id,workspace_revision" ||
      authority.schema !== COORDINATOR_PRODUCTION_AUTHORITY_SCHEMA ||
      typeof authority.workspace_handle !== "string" ||
      !/^pw_[A-Za-z0-9_-]{32,96}$/.test(authority.workspace_handle) ||
      typeof authority.automatic_plan_fingerprint !== "string" ||
      !fingerprint.test(authority.automatic_plan_fingerprint)
    )
      throw new SequenceCoordinatorClientError("invalid_response", 500);
    // CRITICAL: a compact child references its original owner; never decode it as a
    // partial Production workspace or accept identity from a different sequence.
    if (
      authority.workspace_id !== sequence.workspace_id ||
      authority.workspace_revision !== sequence.workspace_revision ||
      authority.workspace_fingerprint !== sequence.workspace_fingerprint
    )
      throw new SequenceCoordinatorClientError("cross_authority_response", 500);
    return Object.freeze({
      ...common,
      schema: MANAGED_CHILD_COORDINATOR_RESPONSE_SCHEMA,
      production: null,
      productionAuthority: Object.freeze({
        schema: COORDINATOR_PRODUCTION_AUTHORITY_SCHEMA,
        workspaceHandle: authority.workspace_handle,
        workspaceId: sequence.workspace_id,
        workspaceRevision: sequence.workspace_revision,
        workspaceFingerprint: sequence.workspace_fingerprint,
        automaticPlanFingerprint: authority.automatic_plan_fingerprint,
      }),
    });
  }
  let production: ProductionWorkbenchProjection;
  try {
    production = decodeProductionWorkbenchProjection(wire.production);
  } catch {
    throw new SequenceCoordinatorClientError("invalid_response", 500);
  }
  if (!sameSequenceAuthority(production, sequence))
    throw new SequenceCoordinatorClientError("cross_authority_response", 500);
  return Object.freeze({
    ...common,
    schema: SEQUENCE_COORDINATOR_RESPONSE_SCHEMA,
    production,
  });
}

export function createSequenceCoordinatorClient({
  fetchApi,
}: {
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
}) {
  if (typeof fetchApi !== "function")
    throw new Error("supported same-origin coordinator seam is absent");
  return Object.freeze({
    async send(
      requestId: string,
      action: SequenceCoordinatorAction,
      payload: Record<string, unknown>,
      signal?: AbortSignal,
    ): Promise<SequenceCoordinatorResult> {
      if (!identifier.test(requestId) || requestId.length > 160)
        throw new SequenceCoordinatorClientError("invalid_request_id", 400);
      const body = JSON.stringify({
        schema: SEQUENCE_COORDINATOR_ACTION_SCHEMA,
        request_id: requestId,
        action,
        payload,
      });
      if (body.length > 32_768)
        throw new SequenceCoordinatorClientError("request_too_large", 413);
      const response = await fetchApi(SEQUENCE_COORDINATOR_ROUTE, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body,
        signal,
      });
      if (!response.ok) {
        let decoded: SequenceCoordinatorClientError | undefined;
        try {
          decoded = decodeSequenceCoordinatorError(
            await response.json(),
            response.status,
          );
        } catch {
          // CRITICAL: a malformed error body may contain private host detail. Collapse it to the
          // fixed status map instead of carrying response members into UI or diagnostics.
        }
        if (decoded !== undefined) throw decoded;
        throw new SequenceCoordinatorClientError(
          statusCodes[response.status] ?? "internal_failure",
          response.status,
        );
      }
      if (response.status !== 200)
        throw new SequenceCoordinatorClientError(
          "unexpected_status",
          response.status,
        );
      const result = decodeSequenceCoordinatorResult(await response.json());
      if (action !== "prepare_sequence" && action !== "prepare_managed_run") {
        const expectedRunHandle = payload.run_handle;
        // CRITICAL: a decoded response is not authority for a different managed run.
        if (
          typeof expectedRunHandle !== "string" ||
          !runHandle.test(expectedRunHandle) ||
          result.runHandle !== expectedRunHandle
        )
          throw new SequenceCoordinatorClientError(
            "cross_run_authority_response",
            500,
            {
              category: "run_authority_mismatch",
              retryDisposition: "none",
              sameRunAuthority: false,
            },
          );
      }
      return result;
    },
  });
}
