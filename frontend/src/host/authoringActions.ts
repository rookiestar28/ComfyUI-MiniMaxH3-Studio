// M20-03: the one same-origin client for the authoring-workspace route. A 409 is not an
// error: it carries the untouched backend projection plus the machine-readable rejection
// code, so the caller discards its draft, announces the reason and re-renders accepted
// truth. Everything else non-2xx maps through one closed status table and throws.

import {
  decodeAuthoringProjection,
  decodeAuthoringSnap,
  decodeTimelineHistoryProjection,
  decodeTimelineHistoryProjectionV2,
  decodeTimelineReceipt,
  decodeTimelineReceiptV2,
  encodeAuthoringAction,
  TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
  TIMELINE_RECEIPT_SCHEMA_V2,
  type AuthoringAction,
  type AuthoringActionPayload,
  type AuthoringProjection,
  type AuthoringSnap,
  type TimelineHistoryProjection,
  type TimelineHistoryProjectionV2,
  type TimelineReceipt,
  type TimelineReceiptV2,
} from "../contracts/authoringWorkbenchCodec";

const route = "/h3-context/v1/authoring/action";

type FetchResponse = {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
};

const statusCodes = {
  400: "invalid_action",
  403: "origin_rejected",
  404: "workspace_unavailable",
  410: "workspace_gone",
  413: "request_too_large",
  415: "media_type_rejected",
  422: "action_rejected",
  429: "workspace_capacity",
  500: "internal_failure",
} as const;

export class AuthoringClientError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(code);
    this.name = "AuthoringClientError";
    this.code = code;
    this.status = status;
  }
}

function decodeWorkspaceBound<T>(
  decode: (value: unknown) => T,
  value: unknown,
): T {
  try {
    return decode(value);
  } catch (error) {
    if (error instanceof Error && error.message.includes("cross-workspace"))
      throw new AuthoringClientError("cross_workspace_response", 500);
    throw error;
  }
}

// CRITICAL: response headers are not a decoded mutation receipt. Once the request has left the
// browser the backend may have committed it, so every failure between the status line and a
// bound receipt has to stay distinguishable from the decoded statuses that do establish an
// outcome. `response_body_unavailable` is a body that could not be obtained or parsed at all
// (connection reset mid-body, truncated JSON); `outcome_evidence_undecodable` is a body that
// parsed but is not decodable outcome evidence. Folding either into the generic failure path
// reported a possibly-applied mutation as a rejection against pre-submit history
// (post-corrective review 02, R2-F1). The workspace-binding and receipt-binding checks below
// are unaffected: they still fire and still refuse the response.
async function responseBody(response: FetchResponse): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new AuthoringClientError(
      "response_body_unavailable",
      response.status,
    );
  }
}

function decodeOutcomeEvidence<T>(
  decode: (value: unknown) => T,
  value: unknown,
  status: number,
): T {
  try {
    return decodeWorkspaceBound(decode, value);
  } catch (error) {
    if (error instanceof AuthoringClientError) throw error;
    throw new AuthoringClientError("outcome_evidence_undecodable", status);
  }
}

export type AuthoringClientResult = Readonly<{
  status: 200 | 201 | 204 | 409;
  projection?: AuthoringProjection;
  snap?: AuthoringSnap;
  receipt?: TimelineReceipt;
  receiptV2?: TimelineReceiptV2;
  history?: TimelineHistoryProjection;
  historyV2?: TimelineHistoryProjectionV2;
}>;

function decodeHistoryResult(
  value: unknown,
):
  | Readonly<{ history: TimelineHistoryProjection }>
  | Readonly<{ historyV2: TimelineHistoryProjectionV2 }> {
  if (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    (value as Record<string, unknown>).schema ===
      TIMELINE_HISTORY_PROJECTION_SCHEMA_V2
  )
    return { historyV2: decodeTimelineHistoryProjectionV2(value) };
  return { history: decodeTimelineHistoryProjection(value) };
}

export function createAuthoringActionClient({
  fetchApi,
}: {
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
}) {
  if (typeof fetchApi !== "function")
    throw new Error("supported same-origin authoring seam is absent");
  return {
    async send(
      requestId: string,
      action: AuthoringAction,
      payload: AuthoringActionPayload,
      expectedHandle?: string,
      signal?: AbortSignal,
    ): Promise<AuthoringClientResult> {
      const request = encodeAuthoringAction(requestId, action, payload);
      let response: FetchResponse;
      try {
        response = await fetchApi(route, {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(request),
          signal,
        });
      } catch {
        // CRITICAL: a rejected fetch (network drop, abort) means no reply was decoded, so the
        // backend may or may not have applied the request. Surface it under its own code: every
        // decoded status keeps its own code, and a caller that folded this into a generic failure
        // would report a possibly-committed mutation as rejected (post-closeout finding F1).
        throw new AuthoringClientError("transport_failure", 0);
      }
      if (response.status === 204) {
        // CRITICAL: only release owns the bodiless response. Treating 204 as generic success lets
        // timeline reads/writes cross the transport boundary without their required projection.
        if (action !== "release_workspace")
          throw new AuthoringClientError("unexpected_status", response.status);
        return Object.freeze({ status: 204 as const });
      }
      if (response.ok || response.status === 409) {
        if (![200, 201, 409].includes(response.status))
          throw new AuthoringClientError("unexpected_status", response.status);
        if (
          action === "initialize_timeline_history" &&
          response.status !== 200
        ) {
          // CRITICAL: initialization conflicts have no accepted history to project. Reading an
          // arbitrary 409 body would let conflict input masquerade as backend-owned history.
          if (response.status === 409)
            throw new AuthoringClientError(
              "timeline_initialization_conflict",
              409,
            );
          throw new AuthoringClientError("unexpected_status", response.status);
        }
        const body = await responseBody(response);
        if (action === "read_snap") {
          const snap = decodeAuthoringSnap(body);
          if (
            expectedHandle !== undefined &&
            snap.workspaceHandle !== expectedHandle
          )
            throw new AuthoringClientError("cross_workspace_response", 500);
          return Object.freeze({ status: 200 as const, snap });
        }
        if (action === "read_timeline_history") {
          if (response.status !== 200)
            throw new AuthoringClientError(
              "unexpected_status",
              response.status,
            );
          const decoded = decodeHistoryResult(body);
          const historyWorkspaceHandle =
            "historyV2" in decoded
              ? decoded.historyV2.workspaceHandle
              : decoded.history.workspaceHandle;
          if (
            historyWorkspaceHandle !== request.payload.workspace_handle ||
            (expectedHandle !== undefined &&
              historyWorkspaceHandle !== expectedHandle)
          )
            throw new AuthoringClientError("cross_workspace_response", 500);
          // CRITICAL: HTTP status and the closed rejection body are one protocol fact.
          // Accepting a rejection on 200 would install conflict state as a successful read.
          const rejection =
            "historyV2" in decoded
              ? decoded.historyV2.rejection
              : decoded.history.rejection;
          if (rejection !== null)
            throw new AuthoringClientError("timeline_response_mismatch", 500);
          return Object.freeze({ status: 200 as const, ...decoded });
        }
        if (action === "initialize_timeline_history") {
          const decoded = decodeHistoryResult(body);
          const historyWorkspaceHandle =
            "historyV2" in decoded
              ? decoded.historyV2.workspaceHandle
              : decoded.history.workspaceHandle;
          if (
            historyWorkspaceHandle !== request.payload.workspace_handle ||
            (expectedHandle !== undefined &&
              historyWorkspaceHandle !== expectedHandle)
          )
            throw new AuthoringClientError("cross_workspace_response", 500);
          const rejection =
            "historyV2" in decoded
              ? decoded.historyV2.rejection
              : decoded.history.rejection;
          if (rejection !== null)
            throw new AuthoringClientError("timeline_response_mismatch", 500);
          return Object.freeze({ status: 200 as const, ...decoded });
        }
        if (action === "apply_timeline_transaction") {
          if (response.status === 409) {
            let decoded:
              | Readonly<{ history: TimelineHistoryProjection }>
              | Readonly<{ historyV2: TimelineHistoryProjectionV2 }>;
            try {
              decoded = decodeHistoryResult(body);
            } catch {
              throw new AuthoringClientError(
                "outcome_evidence_undecodable",
                409,
              );
            }
            const historyWorkspaceHandle =
              "historyV2" in decoded
                ? decoded.historyV2.workspaceHandle
                : decoded.history.workspaceHandle;
            if (
              historyWorkspaceHandle !== request.payload.workspace_handle ||
              (expectedHandle !== undefined &&
                historyWorkspaceHandle !== expectedHandle)
            )
              throw new AuthoringClientError("cross_workspace_response", 500);
            const rejection =
              "historyV2" in decoded
                ? decoded.historyV2.rejection
                : decoded.history.rejection;
            if (rejection === null)
              throw new AuthoringClientError("timeline_response_mismatch", 500);
            return Object.freeze({ status: 409 as const, ...decoded });
          }
          if (response.status !== 200)
            throw new AuthoringClientError(
              "unexpected_status",
              response.status,
            );
          const isV2Receipt =
            body !== null &&
            typeof body === "object" &&
            !Array.isArray(body) &&
            (body as Record<string, unknown>).schema ===
              TIMELINE_RECEIPT_SCHEMA_V2;
          if (isV2Receipt) {
            const receipt = decodeOutcomeEvidence(
              decodeTimelineReceiptV2,
              body,
              response.status,
            );
            const transaction = request.payload;
            if (
              expectedHandle !== undefined &&
              receipt.workspaceHandle !== expectedHandle
            )
              throw new AuthoringClientError("cross_workspace_response", 500);
            if (
              receipt.requestId !== requestId ||
              receipt.transactionId !== transaction.transaction_id ||
              receipt.workspaceHandle !== transaction.workspace_handle ||
              receipt.beforeWorkspaceRevision !==
                transaction.expected_workspace_revision ||
              receipt.beforeTimelineRevision !==
                transaction.expected_timeline_revision ||
              receipt.beforeTimelineFingerprint !==
                transaction.expected_timeline_fingerprint ||
              receipt.beforeAuthoringFingerprint !==
                transaction.expected_authoring_fingerprint ||
              JSON.stringify(receipt.commands) !==
                JSON.stringify(transaction.commands)
            )
              throw new AuthoringClientError("timeline_response_mismatch", 500);
            return Object.freeze({ status: 200 as const, receiptV2: receipt });
          }
          const receipt = decodeOutcomeEvidence(
            decodeTimelineReceipt,
            body,
            response.status,
          );
          if (
            expectedHandle !== undefined &&
            receipt.workspaceHandle !== expectedHandle
          )
            throw new AuthoringClientError("cross_workspace_response", 500);
          // CRITICAL: a structurally valid receipt is still untrusted until it is bound to the
          // exact request, transaction and pre-mutation CAS sent on this call. Omitting any one
          // comparison could install another tab's accepted state as this caller's result.
          if (
            receipt.requestId !== requestId ||
            receipt.transactionId !== request.payload.transaction_id ||
            receipt.workspaceHandle !== request.payload.workspace_handle ||
            receipt.beforeWorkspaceRevision !==
              request.payload.expected_workspace_revision ||
            receipt.beforeTimelineRevision !==
              request.payload.expected_timeline_revision ||
            receipt.beforeTimelineFingerprint !==
              request.payload.expected_timeline_fingerprint ||
            JSON.stringify(receipt.commands) !==
              JSON.stringify(request.payload.commands)
          )
            throw new AuthoringClientError("timeline_response_mismatch", 500);
          return Object.freeze({ status: 200 as const, receipt });
        }
        const projection = decodeAuthoringProjection(body);
        if (
          expectedHandle !== undefined &&
          projection.workspaceHandle !== expectedHandle
        )
          throw new AuthoringClientError("cross_workspace_response", 500);
        return Object.freeze({
          status: response.status as 200 | 201 | 409,
          projection,
        });
      }
      const code = statusCodes[response.status as keyof typeof statusCodes];
      throw new AuthoringClientError(
        code ?? "internal_failure",
        response.status,
      );
    },
  };
}
