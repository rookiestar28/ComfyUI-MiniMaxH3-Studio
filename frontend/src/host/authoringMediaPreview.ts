import {
  decodeAuthoringPreviewError,
  encodeAuthoringPreviewRequest,
  type AuthoringPreviewErrorReason,
  type AuthoringPreviewRequest,
} from "../contracts/authoringPreviewCodec";

export const AUTHORING_MEDIA_PREVIEW_ROUTE =
  "/h3-context/v1/authoring/media-preview";
export const MAX_AUTHORING_MEDIA_PREVIEW_BYTES = 8 * 1024 * 1024;
const MAX_ERROR_BYTES = 8 * 1024;
const BYOB_BYTES = 64 * 1024;

export type AuthoringMediaPreviewAudioDisposition =
  "present_bound" | "absent" | "unavailable";
export type AuthoringMediaPreviewDisposition =
  | "unavailable"
  | "stale"
  | "unsupported"
  | "too_large"
  | "too_long"
  | "busy"
  | "cancelled"
  | "timeout"
  | "failed";

export class AuthoringMediaPreviewError extends Error {
  constructor(
    readonly disposition: AuthoringMediaPreviewDisposition,
    readonly status: number,
  ) {
    super(disposition);
  }
}

function forbiddenResponseHeader(response: Response): boolean {
  return [
    "location",
    "content-range",
    "accept-ranges",
    "content-encoding",
    "transfer-encoding",
  ].some((name) => response.headers.has(name));
}

async function rejectResponse(
  response: Response,
  error: AuthoringMediaPreviewError,
): Promise<never> {
  await cancelResponse(response);
  throw error;
}

async function cancelResponse(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // Preserve the content-free disposition if response teardown also fails.
  }
}

function disposition(reason: AuthoringPreviewErrorReason) {
  const values: Readonly<
    Record<AuthoringPreviewErrorReason, AuthoringMediaPreviewDisposition>
  > = {
    invalid_request: "failed",
    authority_mismatch: "unavailable",
    stale: "stale",
    unsupported: "unsupported",
    source_too_large: "too_large",
    source_too_long: "too_long",
    busy: "busy",
    cancelled: "cancelled",
    timeout: "timeout",
    conversion_failed: "failed",
    internal_failure: "failed",
  };
  return values[reason];
}

function statusMatchesReason(
  status: number,
  reason: AuthoringPreviewErrorReason,
): boolean {
  const allowed: Readonly<
    Record<AuthoringPreviewErrorReason, readonly number[]>
  > = {
    invalid_request: [400, 403, 413, 415],
    authority_mismatch: [404],
    stale: [409],
    unsupported: [422],
    source_too_large: [413],
    source_too_long: [422],
    busy: [429],
    cancelled: [499],
    timeout: [504],
    conversion_failed: [422],
    internal_failure: [500],
  };
  return allowed[reason].includes(status);
}

async function decodeError(
  response: Response,
  requestId: string,
): Promise<AuthoringMediaPreviewError> {
  const lengthText = response.headers.get("content-length");
  if (
    response.headers.get("content-type") !== "application/json" ||
    response.headers.get("cache-control") !== "no-store" ||
    response.headers.get("x-content-type-options") !== "nosniff" ||
    forbiddenResponseHeader(response) ||
    lengthText === null ||
    !/^[1-9][0-9]*$/.test(lengthText)
  ) {
    await cancelResponse(response);
    return new AuthoringMediaPreviewError("failed", response.status);
  }
  const length = Number(lengthText);
  if (!Number.isSafeInteger(length) || length > MAX_ERROR_BYTES) {
    await cancelResponse(response);
    return new AuthoringMediaPreviewError("failed", response.status);
  }
  try {
    const bytes = await readExactByob(response, length);
    const wire = decodeAuthoringPreviewError(
      JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)),
    );
    if (
      (wire.requestId !== null && wire.requestId !== requestId) ||
      !statusMatchesReason(response.status, wire.reason)
    )
      return new AuthoringMediaPreviewError("failed", response.status);
    return new AuthoringMediaPreviewError(
      disposition(wire.reason),
      response.status,
    );
  } catch {
    return new AuthoringMediaPreviewError("failed", response.status);
  }
}

async function readExactByob(
  response: Response,
  length: number,
): Promise<Uint8Array> {
  if (response.body === null)
    throw new AuthoringMediaPreviewError("unsupported", 200);
  let reader: ReadableStreamBYOBReader;
  try {
    reader = response.body.getReader({ mode: "byob" });
  } catch {
    return rejectResponse(
      response,
      new AuthoringMediaPreviewError("unsupported", 200),
    );
  }
  try {
    const packed = new Uint8Array(length);
    let backing = new ArrayBuffer(BYOB_BYTES);
    let offset = 0;
    for (;;) {
      const supplied = new Uint8Array(backing);
      const suppliedBuffer = backing;
      const result = await reader.read(supplied);
      if (suppliedBuffer.byteLength !== 0)
        throw new AuthoringMediaPreviewError("failed", 200);
      const view = result.value;
      if (!(view instanceof Uint8Array)) {
        if (result.done === true && offset === length) break;
        throw new AuthoringMediaPreviewError("failed", 200);
      }
      if (
        (view.byteLength === 0 && result.done !== true) ||
        view.byteLength > BYOB_BYTES ||
        view.buffer.byteLength > BYOB_BYTES ||
        view.byteOffset + view.byteLength > view.buffer.byteLength ||
        offset + view.byteLength > length
      )
        throw new AuthoringMediaPreviewError("too_large", 200);
      packed.set(view, offset);
      offset += view.byteLength;
      backing = view.buffer as ArrayBuffer;
      if (result.done === true) {
        if (offset !== length)
          throw new AuthoringMediaPreviewError("failed", 200);
        break;
      }
      if (backing.byteLength === 0)
        throw new AuthoringMediaPreviewError("failed", 200);
    }
    return packed;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function createAuthoringMediaPreviewClient({
  fetchApi,
}: {
  fetchApi: (route: string, init: RequestInit) => Promise<Response>;
}) {
  let generation = 0;
  let active: Readonly<{
    generation: number;
    controller: AbortController;
  }> | null = null;
  return Object.freeze({
    async open(request: AuthoringPreviewRequest, signal: AbortSignal) {
      const exact = encodeAuthoringPreviewRequest(request);
      active?.controller.abort();
      const ownedGeneration = generation + 1;
      generation = ownedGeneration;
      const controller = new AbortController();
      active = Object.freeze({ generation: ownedGeneration, controller });
      const abortOwned = () => controller.abort();
      signal.addEventListener("abort", abortOwned, { once: true });
      if (signal.aborted) controller.abort();
      const assertCurrent = async (response?: Response) => {
        if (
          controller.signal.aborted ||
          active?.generation !== ownedGeneration
        ) {
          try {
            await response?.body?.cancel();
          } catch {
            // Cancellation remains authoritative if response teardown also fails.
          }
          throw new AuthoringMediaPreviewError("cancelled", 499);
        }
      };
      try {
        const response = await fetchApi(AUTHORING_MEDIA_PREVIEW_ROUTE, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(exact),
          signal: controller.signal,
          redirect: "error",
          cache: "no-store",
          credentials: "same-origin",
        });
        await assertCurrent(response);
        if (response.status !== 200) {
          const error = await decodeError(response, exact.requestId);
          await assertCurrent();
          throw error;
        }
        const lengthText = response.headers.get("content-length");
        const audio = response.headers.get("x-h3-context-embedded-audio");
        if (
          response.headers.get("content-type") !== "video/mp4" ||
          response.headers.get("cache-control") !== "no-store" ||
          response.headers.get("x-content-type-options") !== "nosniff" ||
          response.headers.get("content-disposition") !==
            'inline; filename="h3-authoring-preview.mp4"' ||
          !["present_bound", "absent", "unavailable"].includes(audio ?? "") ||
          lengthText === null ||
          !/^[1-9][0-9]*$/.test(lengthText) ||
          forbiddenResponseHeader(response)
        )
          return rejectResponse(
            response,
            new AuthoringMediaPreviewError("failed", 200),
          );
        const length = Number(lengthText);
        if (
          !Number.isSafeInteger(length) ||
          length > MAX_AUTHORING_MEDIA_PREVIEW_BYTES
        )
          return rejectResponse(
            response,
            new AuthoringMediaPreviewError("too_large", 200),
          );
        const bytes = await readExactByob(response, length);
        await assertCurrent();
        return Object.freeze({
          blob: new Blob([bytes.buffer as ArrayBuffer], { type: "video/mp4" }),
          audioDisposition: audio as AuthoringMediaPreviewAudioDisposition,
        });
      } catch (error) {
        if (controller.signal.aborted || active?.generation !== ownedGeneration)
          throw new AuthoringMediaPreviewError("cancelled", 499);
        if (error instanceof AuthoringMediaPreviewError) throw error;
        throw new AuthoringMediaPreviewError("failed", 0);
      } finally {
        signal.removeEventListener("abort", abortOwned);
        if (active?.generation === ownedGeneration) active = null;
      }
    },
    close() {
      active?.controller.abort();
      active = null;
    },
  });
}
