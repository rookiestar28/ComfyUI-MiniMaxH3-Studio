import {
  canonicalDurationResolutions,
  durationResolutionContract,
} from "../contracts/generatedDurationResolution";

export const DURATION_RESOLUTION_ROUTE = durationResolutionContract.route;
export const DURATION_RESOLUTION_REQUEST_SCHEMA =
  durationResolutionContract.request_schema;
export const DURATION_RESOLUTION_RESPONSE_SCHEMA =
  durationResolutionContract.response_schema;
export const APP_MODE_MIN_DURATION_SECONDS =
  durationResolutionContract.minimum_seconds;
export const APP_MODE_MAX_DURATION_SECONDS =
  durationResolutionContract.maximum_seconds;
export const MAX_DURATION_RESOLUTION_RESPONSE_BYTES = 1024;

export type DurationResolution = Readonly<{
  schema: typeof durationResolutionContract.response_schema;
  requested_seconds: number;
  requested_milliseconds: number;
  effective_milliseconds: number;
  frame_count: number;
  snapped: boolean;
}>;

type FetchResponse = {
  ok: boolean;
  status: number;
  text(): Promise<string>;
};

export type DurationResolutionFailure =
  | "invalid_request"
  | "seam_unavailable"
  | "route_rejected"
  | "payload_rejected";

export class DurationResolutionClientError extends Error {
  readonly failure: DurationResolutionFailure;
  readonly status: number;

  constructor(failure: DurationResolutionFailure, status = 0) {
    // CRITICAL: the route response and host error are untrusted; expose only a stable local code.
    super(failure);
    this.name = "DurationResolutionClientError";
    this.failure = failure;
    this.status = status;
  }
}

function matchingCanonicalDurationResolution(
  value: unknown,
  requestedSeconds?: number,
): DurationResolution | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return undefined;
  const wire = value as Record<string, unknown>;
  const expected = canonicalDurationResolutions.find(
    (candidate) =>
      candidate.requested_seconds === wire.requested_seconds &&
      (requestedSeconds === undefined ||
        candidate.requested_seconds === requestedSeconds),
  );
  if (
    expected === undefined ||
    Object.keys(wire).sort().join("\u0000") !==
      Object.keys(expected).sort().join("\u0000")
  )
    return undefined;
  // CRITICAL: requested and delivered milliseconds can differ on the H3 lattice. A raw 15000-ms
  // delivered ceiling rejects the valid maximum; require the generated backend tuple instead.
  for (const key of Object.keys(expected) as Array<keyof typeof expected>)
    if (wire[key] !== expected[key]) return undefined;
  return expected;
}

export function isCanonicalDurationResolution(
  value: unknown,
): value is DurationResolution {
  return matchingCanonicalDurationResolution(value) !== undefined;
}

function decodeDurationResolution(
  value: unknown,
  requestedSeconds: number,
): DurationResolution {
  const expected = matchingCanonicalDurationResolution(value, requestedSeconds);
  if (expected === undefined)
    throw new DurationResolutionClientError("payload_rejected", 200);
  return expected;
}

export function createDurationResolutionClient({
  fetchApi,
}: {
  fetchApi?: (path: string, init: RequestInit) => Promise<FetchResponse>;
}) {
  return {
    async resolve(
      requestedSeconds: number,
      signal?: AbortSignal,
    ): Promise<DurationResolution> {
      if (
        !Number.isInteger(requestedSeconds) ||
        requestedSeconds < APP_MODE_MIN_DURATION_SECONDS ||
        requestedSeconds > APP_MODE_MAX_DURATION_SECONDS
      )
        throw new DurationResolutionClientError("invalid_request");
      if (typeof fetchApi !== "function")
        throw new DurationResolutionClientError("seam_unavailable");
      let response: FetchResponse;
      try {
        response = await fetchApi(DURATION_RESOLUTION_ROUTE, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            accept: "application/json",
            "content-type": "application/json",
          },
          body: JSON.stringify({
            schema: DURATION_RESOLUTION_REQUEST_SCHEMA,
            requested_seconds: requestedSeconds,
          }),
          signal,
        });
      } catch (error) {
        if (signal?.aborted) throw error;
        throw new DurationResolutionClientError("seam_unavailable");
      }
      if (response?.ok !== true)
        throw new DurationResolutionClientError(
          "route_rejected",
          typeof response?.status === "number" ? response.status : 0,
        );
      let body: string;
      try {
        body = await response.text();
      } catch {
        throw new DurationResolutionClientError("payload_rejected", 200);
      }
      if (
        typeof body !== "string" ||
        body.length > MAX_DURATION_RESOLUTION_RESPONSE_BYTES
      )
        throw new DurationResolutionClientError("payload_rejected", 200);
      try {
        return decodeDurationResolution(JSON.parse(body), requestedSeconds);
      } catch (error) {
        if (error instanceof DurationResolutionClientError) throw error;
        throw new DurationResolutionClientError("payload_rejected", 200);
      }
    },
  };
}
