import {
  decodeRetainedAssetsResponse,
  encodeRetainedAssetsAction,
  retainedUseHandle,
  type RetainedAssetsAction,
  type RetainedAssetsCode,
  type RetainedAssetsResponse,
} from "../contracts/retainedAssetsCodec";
import {
  readExactByob,
  MAX_AUTHORING_MEDIA_PREVIEW_BYTES,
} from "./authoringMediaPreview";

export type RetainedAssetsClientCode =
  | RetainedAssetsCode
  | "transport_failure"
  | "malformed_response"
  | "unexpected_status"
  | "aborted";
type Failure = Readonly<{
  ok: false;
  code: RetainedAssetsClientCode;
  outcomeUnknown: boolean;
}>;
export type RetainedAssetsResult =
  Readonly<{ ok: true; response: RetainedAssetsResponse }> | Failure;
export type RetainedPreviewResult =
  | Readonly<{
      ok: true;
      blob: Blob;
      audio: "present_bound" | "absent" | "unavailable";
    }>
  | Failure;
export type RetainedAssetsFetch = (
  path: string,
  init: RequestInit,
) => Promise<Response>;
const ROUTE = "/h3-context/retained-assets";
function bodyLength(
  response: Response,
  maximum: number,
  contentType: string,
): number {
  const length = response.headers.get("content-length");
  if (
    response.headers.get("content-type") !== contentType ||
    response.headers.get("cache-control") !== "no-store" ||
    response.headers.get("x-content-type-options") !== "nosniff" ||
    [
      "location",
      "content-range",
      "accept-ranges",
      "content-encoding",
      "transfer-encoding",
    ].some((name) => response.headers.has(name)) ||
    length === null ||
    !/^[1-9][0-9]*$/.test(length)
  )
    throw new Error("response_bound");
  const number = Number(length);
  if (!Number.isSafeInteger(number) || number > maximum)
    throw new Error("response_bound");
  return number;
}
async function decoded(response: Response): Promise<RetainedAssetsResponse> {
  const bytes = await readExactByob(
    response,
    bodyLength(response, 32 * 1024, "application/json"),
  );
  return decodeRetainedAssetsResponse(
    JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)),
  );
}
function failure(
  code: RetainedAssetsClientCode,
  outcomeUnknown = false,
): Failure {
  return { ok: false, code, outcomeUnknown };
}
export function createRetainedAssetsClient({
  fetchApi,
}: {
  fetchApi: RetainedAssetsFetch;
}) {
  const init = (body: string, signal?: AbortSignal): RequestInit => ({
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body,
    signal,
    cache: "no-store",
    redirect: "error",
  });
  const discardKnownUse = async (wire: RetainedAssetsResponse) => {
    if (wire.restored === null) return;
    // CRITICAL: even a mismatched decoded reply can own fresh bytes. Revoke its known handle
    // once without the aborted request's signal; do not replay or recursively decode cleanup.
    try {
      const cleanup = await fetchApi(
        ROUTE,
        init(
          encodeRetainedAssetsAction({
            intent: "release",
            use_handle: wire.restored.use_handle,
          }),
        ),
      );
      await cleanup.body?.cancel();
    } catch {
      return;
    }
  };
  async function send(
    action: RetainedAssetsAction,
    signal?: AbortSignal,
  ): Promise<RetainedAssetsResult> {
    let body: string;
    try {
      body = encodeRetainedAssetsAction(action);
    } catch {
      return failure("invalid_request");
    }
    const mutation = !["status", "list"].includes(action.intent);
    let response: Response;
    try {
      response = await fetchApi(ROUTE, init(body, signal));
    } catch {
      return failure(
        signal?.aborted ? "aborted" : "transport_failure",
        mutation,
      );
    }
    try {
      const wire = await decoded(response);
      if (response.status === 200) {
        if (
          wire.error !== null ||
          (action.intent === "restore"
            ? wire.restored?.asset_id !== action.asset_id
            : wire.restored !== null) ||
          (action.intent === "retain"
            ? wire.retained_id === null
            : wire.retained_id !== null) ||
          (["clear", "collect"].includes(action.intent)
            ? wire.cleanup === null
            : wire.cleanup !== null)
        ) {
          await discardKnownUse(wire);
          throw new Error("response_intent_mismatch");
        }
        // IMPORTANT: return a valid late restore handle to the session even after abort, so it
        // can release the unseen fresh resource instead of abandoning known authority.
        return { ok: true, response: wire };
      }
      if (wire.restored !== null) {
        await discardKnownUse(wire);
        return failure("malformed_response", mutation);
      }
      return failure(
        wire.error ?? "unexpected_status",
        mutation && response.status >= 500,
      );
    } catch {
      await response.body?.cancel().catch(() => undefined);
      return failure("malformed_response", mutation);
    }
  }
  async function preview(
    useHandle: string,
    signal: AbortSignal,
  ): Promise<RetainedPreviewResult> {
    try {
      retainedUseHandle(useHandle);
    } catch {
      return failure("lease_invalid");
    }
    if (signal.aborted) return failure("aborted");
    let response: Response;
    try {
      response = await fetchApi(
        ROUTE + "/preview",
        init(JSON.stringify({ use_handle: useHandle }), signal),
      );
    } catch {
      return failure(signal.aborted ? "aborted" : "transport_failure");
    }
    try {
      if (response.status !== 200)
        return failure((await decoded(response)).error ?? "unexpected_status");
      const length = bodyLength(
          response,
          MAX_AUTHORING_MEDIA_PREVIEW_BYTES,
          "video/mp4",
        ),
        audio = response.headers.get("x-h3-context-embedded-audio");
      if (
        response.headers.get("content-disposition") !==
          'inline; filename="h3-retained-preview.mp4"' ||
        !["present_bound", "absent", "unavailable"].includes(audio ?? "")
      )
        throw new Error("response_bound");
      const bytes = await readExactByob(response, length);
      if (signal.aborted) return failure("aborted");
      return {
        ok: true,
        blob: new Blob([bytes.buffer as ArrayBuffer], { type: "video/mp4" }),
        audio: audio as "present_bound" | "absent" | "unavailable",
      };
    } catch {
      await response.body?.cancel().catch(() => undefined);
      return failure(signal.aborted ? "aborted" : "malformed_response");
    }
  }
  return Object.freeze({ send, preview });
}
