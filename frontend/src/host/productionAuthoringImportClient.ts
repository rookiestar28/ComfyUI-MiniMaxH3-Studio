// M25-16: the dedicated same-origin client for the accepted M25-29 import action.
//
// This client is deliberately separate from the generic Production/Authoring action clients:
// the import route answers success with HTTP 200 and a closed receipt+projection body, and
// every refusal is status-only with no JSON error or projection body. A bodiless 409 here is
// a conflict/currentness/replay class, never an Authoring projection, so the generic clients'
// projection-bearing 409 behaviour must not be applied to it.

import {
  decodeProductionAuthoringImportResponse,
  encodeProductionAuthoringImportRequest,
  type ProductionAuthoringImportRequest,
  type ProductionAuthoringImportRequestV1,
  type ProductionAuthoringImportRequestV2,
  type ProductionAuthoringImportResponse,
  type ProductionAuthoringImportResponseV1,
  type ProductionAuthoringImportResponseV2,
} from "../contracts/productionAuthoringImportCodec";

export const PRODUCTION_AUTHORING_IMPORT_ROUTE =
  "/h3-context/v1/production/authoring-import" as const;

/**
 * Status-only refusal classes. Status alone cannot distinguish every backend cause, so the
 * class names stay coarse: 409 is unresolved conflict/currentness/replay, 422 is ineligible/
 * unsupported/capacity, 408 does not reveal timeout versus cancellation.
 */
export const PRODUCTION_AUTHORING_IMPORT_REFUSALS = Object.freeze({
  400: "invalid_request",
  403: "origin_rejected",
  404: "workspace_unavailable",
  408: "request_timeout_or_cancelled",
  409: "conflict_or_replay",
  410: "workspace_gone",
  413: "request_too_large",
  422: "ineligible_or_unsupported",
  503: "service_unavailable",
} as const);

export type ProductionAuthoringImportRefusal =
  | (typeof PRODUCTION_AUTHORING_IMPORT_REFUSALS)[keyof typeof PRODUCTION_AUTHORING_IMPORT_REFUSALS]
  | "unexpected_status"
  | "malformed_response"
  | "response_mismatch"
  | "transport_failure"
  | "aborted";

export class ProductionAuthoringImportError extends Error {
  readonly code: ProductionAuthoringImportRefusal;
  readonly status: number | null;
  /** `true` when the request may have reached the backend and its outcome is unknown. */
  readonly outcomeUnknown: boolean;

  constructor(
    code: ProductionAuthoringImportRefusal,
    status: number | null,
    outcomeUnknown: boolean,
  ) {
    super(code);
    this.name = "ProductionAuthoringImportError";
    this.code = code;
    this.status = status;
    this.outcomeUnknown = outcomeUnknown;
  }
}

export type ProductionAuthoringImportFetch = (
  path: string,
  init: RequestInit,
) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

export type ProductionAuthoringImportClient = Readonly<{
  send(
    request: ProductionAuthoringImportRequest,
    signal?: AbortSignal,
  ): Promise<ProductionAuthoringImportResponse>;
}>;

function rowsMatchRequest(
  request: ProductionAuthoringImportRequest,
  response: ProductionAuthoringImportResponse,
): boolean {
  const receipt = response.receipt;
  return (
    receipt.rows.length === request.entries.length &&
    receipt.rows.every(
      (row, index) =>
        row.segmentId === request.entries[index]?.segmentId &&
        row.outputHandle === request.entries[index]?.outputHandle &&
        row.assetId !== row.outputHandle,
    )
  );
}

function responseMatchesRequestV1(
  request: ProductionAuthoringImportRequestV1,
  response: ProductionAuthoringImportResponseV1,
): boolean {
  const receipt = response.receipt;
  if (
    receipt.requestId !== request.requestId ||
    receipt.productionWorkspaceId !== request.productionWorkspaceId ||
    receipt.authoringWorkspaceHandle !== request.authoringWorkspaceHandle ||
    receipt.rows.length !== request.entries.length
  )
    return false;
  return rowsMatchRequest(request, response);
}

function responseMatchesRequestV2(
  request: ProductionAuthoringImportRequestV2,
  response: ProductionAuthoringImportResponseV2,
): boolean {
  const receipt = response.receipt;
  const prior = receipt.nleAuthoring;
  return (
    receipt.requestId === request.requestId &&
    receipt.productionWorkspaceId === request.productionWorkspaceId &&
    receipt.productionWorkspaceRevision ===
      request.expectedProductionWorkspaceRevision &&
    receipt.productionWorkspaceFingerprint ===
      request.expectedProductionWorkspaceFingerprint &&
    receipt.authoringWorkspaceHandle === request.authoringWorkspaceHandle &&
    receipt.authoringRegistryFingerprint ===
      request.expectedAuthoringRegistryFingerprint &&
    receipt.reference.priorRevision ===
      request.expectedAuthoringReferenceRevision &&
    receipt.legacyTimeline.priorRevision ===
      request.expectedAuthoringTimelineRevision &&
    receipt.legacyTimeline.priorContentFingerprint ===
      request.expectedAuthoringTimelineContentFingerprint &&
    prior.authoringSchema === request.authoringSchema &&
    prior.profileId === request.profileId &&
    prior.priorWorkspaceRevision === request.expectedNleWorkspaceRevision &&
    prior.priorTimelineRevision === request.expectedNleTimelineRevision &&
    prior.priorTimelineFingerprint === request.expectedNleTimelineFingerprint &&
    prior.priorAuthoringFingerprint ===
      request.expectedNleAuthoringFingerprint &&
    rowsMatchRequest(request, response)
  );
}

function responseMatchesRequest(
  request: ProductionAuthoringImportRequest,
  response: ProductionAuthoringImportResponse,
): boolean {
  if ("expectedNlePublicFingerprint" in request)
    return (
      response.schema ===
        "h3.context.production_authoring_import.response.v1" &&
      responseMatchesRequestV1(request, response)
    );
  return (
    response.schema === "h3.context.production_authoring_import.response.v2" &&
    responseMatchesRequestV2(request, response)
  );
}

export function createProductionAuthoringImportClient({
  fetchApi,
}: {
  fetchApi: ProductionAuthoringImportFetch;
}): ProductionAuthoringImportClient {
  return Object.freeze({
    async send(request, signal) {
      const body = encodeProductionAuthoringImportRequest(request);
      let response: Awaited<ReturnType<ProductionAuthoringImportFetch>>;
      try {
        response = await fetchApi(PRODUCTION_AUTHORING_IMPORT_ROUTE, {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
          signal,
        });
      } catch (error) {
        // CRITICAL: a transport failure or abort after the request left the browser is an
        // unknown outcome, not a refusal. The caller keeps the exact request for explicit
        // replay against the backend ledger and never issues a fresh-ID resend automatically.
        const aborted =
          (error as { name?: string } | null)?.name === "AbortError" ||
          signal?.aborted === true;
        throw new ProductionAuthoringImportError(
          aborted ? "aborted" : "transport_failure",
          null,
          true,
        );
      }
      if (response.status === 200) {
        let decoded: ProductionAuthoringImportResponse;
        try {
          decoded = decodeProductionAuthoringImportResponse(
            await response.json(),
          );
        } catch {
          throw new ProductionAuthoringImportError(
            "malformed_response",
            200,
            true,
          );
        }
        if (!responseMatchesRequest(request, decoded))
          throw new ProductionAuthoringImportError(
            "response_mismatch",
            200,
            true,
          );
        return decoded;
      }
      const refusal = (
        PRODUCTION_AUTHORING_IMPORT_REFUSALS as Record<
          number,
          ProductionAuthoringImportRefusal | undefined
        >
      )[response.status];
      // Refusals carry no body by contract; do not parse or display one.
      throw new ProductionAuthoringImportError(
        refusal ?? "unexpected_status",
        response.status,
        response.status === 408 || refusal === undefined,
      );
    },
  });
}
