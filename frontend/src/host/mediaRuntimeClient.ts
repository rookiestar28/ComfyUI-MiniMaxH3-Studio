// Same-origin offline selection client. Lost mutations are reconciled by a read, never resent.

import {
  MEDIA_RUNTIME_REQUEST_SCHEMA,
  decodeMediaRuntimeStatus,
  isMediaRuntimeRefusalCode,
  type MediaRuntimeAction,
  type MediaRuntimeRefusalCode,
  type MediaRuntimeStatus,
} from "../contracts/mediaRuntimeCodec";

export const MEDIA_RUNTIME_STATUS_ROUTE =
  "/h3-context/v1/media-runtime" as const;
export const MEDIA_RUNTIME_SETUP_ROUTE =
  "/h3-context/v1/media-runtime/setup" as const;
const MAX_RESPONSE_CHARS = 16 * 1024;

export type MediaRuntimeClientCode =
  | MediaRuntimeRefusalCode
  | "transport_failure"
  | "malformed_response"
  | "unexpected_status"
  | "aborted";

export type MediaRuntimeClientFailure = Readonly<{
  ok: false;
  code: MediaRuntimeClientCode;
  /** `true` when a mutation may have reached the server and its result is unknown. */
  outcomeUnknown: boolean;
}>;

export type MediaRuntimeStatusResult =
  | Readonly<{ ok: true; status: MediaRuntimeStatus }>
  | MediaRuntimeClientFailure;
export type MediaRuntimeActionPayload = Readonly<{
  directory?: string;
  expectedRevision?: number;
}>;

export type MediaRuntimeFetch = (
  path: string,
  init: RequestInit,
) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

type Response = Awaited<ReturnType<MediaRuntimeFetch>>;

function failure(
  code: MediaRuntimeClientCode,
  outcomeUnknown: boolean,
): MediaRuntimeClientFailure {
  return Object.freeze({ ok: false, code, outcomeUnknown });
}

function aborted(error: unknown, signal: AbortSignal | undefined): boolean {
  return (
    (error as { name?: string } | null)?.name === "AbortError" ||
    signal?.aborted === true
  );
}

async function body(response: Response): Promise<unknown> {
  const value = await response.json();
  if (JSON.stringify(value).length > MAX_RESPONSE_CHARS)
    throw new Error("response too large");
  return value;
}

async function refusal(
  response: Response,
  mutation: boolean,
): Promise<MediaRuntimeClientFailure> {
  let code: unknown;
  try {
    code = ((await response.json()) as { error?: unknown } | null)?.error;
  } catch {
    code = undefined;
  }
  if (isMediaRuntimeRefusalCode(code)) return failure(code, false);
  // A 5xx without a closed code may still have run server-side work for a mutation.
  return failure("unexpected_status", mutation && response.status >= 500);
}

function requestBody(
  action: MediaRuntimeAction,
  payload: MediaRuntimeActionPayload,
): Record<string, unknown> | undefined {
  const envelope = { schema: MEDIA_RUNTIME_REQUEST_SCHEMA, action };
  switch (action) {
    case "rescan":
    case "reclaim_parked_runtime":
      return envelope;
    case "use_local_directory":
      return typeof payload.directory === "string" &&
        payload.directory.length > 0 &&
        payload.directory.length <= 4096 &&
        Number.isSafeInteger(payload.expectedRevision) &&
        (payload.expectedRevision ?? -1) >= 0
        ? {
            ...envelope,
            directory: payload.directory,
            expected_revision: payload.expectedRevision,
          }
        : undefined;
    case "restore_auto":
      return Number.isSafeInteger(payload.expectedRevision) &&
        (payload.expectedRevision ?? -1) >= 0
        ? { ...envelope, expected_revision: payload.expectedRevision }
        : undefined;
  }
}

export function createMediaRuntimeClient({
  fetchApi,
}: {
  fetchApi: MediaRuntimeFetch;
}) {
  async function read<T>(
    path: string,
    decode: (value: unknown) => T,
    signal: AbortSignal | undefined,
  ): Promise<Readonly<{ ok: true; value: T }> | MediaRuntimeClientFailure> {
    let response: Response;
    try {
      response = await fetchApi(path, {
        method: "GET",
        credentials: "same-origin",
        headers: { accept: "application/json" },
        signal,
      });
    } catch (error) {
      return failure(
        aborted(error, signal) ? "aborted" : "transport_failure",
        false,
      );
    }
    if (response.status !== 200) return refusal(response, false);
    try {
      return Object.freeze({ ok: true, value: decode(await body(response)) });
    } catch {
      return failure("malformed_response", false);
    }
  }

  return Object.freeze({
    async readStatus(signal?: AbortSignal): Promise<MediaRuntimeStatusResult> {
      const result = await read(
        MEDIA_RUNTIME_STATUS_ROUTE,
        decodeMediaRuntimeStatus,
        signal,
      );
      return result.ok
        ? Object.freeze({ ok: true, status: result.value })
        : result;
    },

    async send(
      action: MediaRuntimeAction,
      payload: MediaRuntimeActionPayload,
      signal?: AbortSignal,
    ): Promise<MediaRuntimeStatusResult> {
      const request = requestBody(action, payload);
      if (request === undefined) return failure("invalid_request", false);
      let response: Response;
      try {
        response = await fetchApi(MEDIA_RUNTIME_SETUP_ROUTE, {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(request),
          signal,
        });
      } catch (error) {
        // CRITICAL: the request may have left the browser. Only a status re-read may decide
        // whether the selection changed; resending would be a second click the user never made.
        return failure(
          aborted(error, signal) ? "aborted" : "transport_failure",
          true,
        );
      }
      const expected = 200;
      if (response.status !== expected) {
        if (response.status >= 200 && response.status < 300)
          return failure("unexpected_status", true);
        return refusal(response, true);
      }
      try {
        const value = await body(response);
        return Object.freeze({
          ok: true,
          status: decodeMediaRuntimeStatus(value),
        });
      } catch {
        return failure("malformed_response", true);
      }
    },
  });
}

export type MediaRuntimeClient = ReturnType<typeof createMediaRuntimeClient>;
