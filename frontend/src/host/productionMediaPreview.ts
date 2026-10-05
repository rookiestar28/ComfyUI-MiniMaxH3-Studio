import type {
  ProductionOutput,
  ProductionWorkbenchProjection,
} from "../contracts/productionWorkbenchCodec";

export const MEDIA_PREVIEW_REQUEST_SCHEMA =
  "h3.context.production.media_preview.request.v1";
export const MEDIA_PREVIEW_ROUTE = "/h3-context/v1/production/media-preview";
export const MAX_MEDIA_PREVIEW_BYTES = 8 * 1024 * 1024;
const BYOB_BYTES = 64 * 1024;

export type ProductionMediaPreviewState =
  | Readonly<{ status: "closed" }>
  | Readonly<{
      status: "loading";
      workspaceRevision: number;
      outputHandle: string;
    }>
  | Readonly<{
      status: "ready";
      workspaceRevision: number;
      outputHandle: string;
      url: string;
    }>
  | Readonly<{
      status: "error";
      workspaceRevision: number;
      outputHandle: string;
      reason: ProductionMediaPreviewDisposition;
    }>;

export type ProductionMediaPreviewDisposition =
  | "invalid_request"
  | "forbidden"
  | "unavailable"
  | "stale"
  | "gone"
  | "too_large"
  | "unsupported"
  | "busy"
  | "client_aborted"
  | "timed_out"
  | "failed";

export class ProductionMediaPreviewError extends Error {
  constructor(
    readonly disposition: ProductionMediaPreviewDisposition,
    readonly status: number,
  ) {
    super(disposition);
  }
}

const dispositionByStatus: Readonly<
  Record<number, ProductionMediaPreviewDisposition>
> = Object.freeze({
  400: "invalid_request",
  403: "forbidden",
  404: "unavailable",
  409: "stale",
  410: "gone",
  413: "too_large",
  415: "unsupported",
  422: "unavailable",
  423: "busy",
  499: "client_aborted",
  504: "timed_out",
  500: "failed",
});

function forbiddenResponseHeader(response: Response): boolean {
  return [
    "location",
    "content-range",
    "accept-ranges",
    "content-encoding",
    "transfer-encoding",
  ].some((name) => response.headers.has(name));
}

async function rejectPreviewResponse(
  response: Response,
  error: ProductionMediaPreviewError,
): Promise<never> {
  try {
    await response.body?.cancel();
  } catch {
    // The original closed disposition remains authoritative even if teardown also fails.
  }
  throw error;
}

async function readExactByob(
  response: Response,
  length: number,
): Promise<Uint8Array> {
  if (response.body === null)
    throw new ProductionMediaPreviewError("unsupported", 200);
  let reader: ReadableStreamBYOBReader;
  try {
    reader = response.body.getReader({ mode: "byob" });
  } catch {
    return rejectPreviewResponse(
      response,
      new ProductionMediaPreviewError("unsupported", 200),
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
        throw new ProductionMediaPreviewError("failed", 200);
      const view = result.value;
      if (!(view instanceof Uint8Array)) {
        if (result.done === true && offset === length) break;
        throw new ProductionMediaPreviewError("failed", 200);
      }
      if (
        (view.byteLength === 0 && result.done !== true) ||
        view.byteLength > BYOB_BYTES ||
        view.buffer.byteLength > BYOB_BYTES ||
        view.byteOffset + view.byteLength > view.buffer.byteLength ||
        offset + view.byteLength > length
      )
        throw new ProductionMediaPreviewError("too_large", 200);
      packed.set(view, offset);
      offset += view.byteLength;
      backing = view.buffer as ArrayBuffer;
      if (result.done === true) {
        if (offset !== length)
          throw new ProductionMediaPreviewError("failed", 200);
        break;
      }
      if (backing.byteLength === 0)
        throw new ProductionMediaPreviewError("failed", 200);
    }
    if (offset !== length) throw new ProductionMediaPreviewError("failed", 200);
    return packed;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function encodeMediaPreviewRequest(
  projection: ProductionWorkbenchProjection,
  output: ProductionOutput,
): string {
  return JSON.stringify({
    schema: MEDIA_PREVIEW_REQUEST_SCHEMA,
    workspace_handle: projection.workspaceHandle,
    expected_workspace_revision: projection.workspaceRevision,
    expected_workspace_fingerprint: projection.workspaceFingerprint,
    output_handle: output.outputHandle,
  });
}

export function createProductionMediaPreviewClient({
  fetchApi,
}: {
  fetchApi: (route: string, init: RequestInit) => Promise<Response>;
}) {
  return Object.freeze({
    async open(
      projection: ProductionWorkbenchProjection,
      output: ProductionOutput,
      signal: AbortSignal,
    ): Promise<Blob> {
      const response = await fetchApi(MEDIA_PREVIEW_ROUTE, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: encodeMediaPreviewRequest(projection, output),
        signal,
        redirect: "error",
        cache: "no-store",
        credentials: "same-origin",
      });
      if (response.status !== 200) {
        const disposition = dispositionByStatus[response.status] ?? "failed";
        return rejectPreviewResponse(
          response,
          new ProductionMediaPreviewError(disposition, response.status),
        );
      }
      const lengthText = response.headers.get("content-length");
      if (
        response.headers.get("content-type") !== "video/mp4" ||
        response.headers.get("cache-control") !== "no-store" ||
        response.headers.get("x-content-type-options") !== "nosniff" ||
        response.headers.get("content-disposition") !==
          'inline; filename="h3-preview.mp4"' ||
        lengthText === null ||
        !/^[1-9][0-9]*$/.test(lengthText) ||
        forbiddenResponseHeader(response)
      )
        return rejectPreviewResponse(
          response,
          new ProductionMediaPreviewError("failed", 200),
        );
      const length = Number(lengthText);
      if (!Number.isSafeInteger(length) || length > MAX_MEDIA_PREVIEW_BYTES)
        return rejectPreviewResponse(
          response,
          new ProductionMediaPreviewError("too_large", 200),
        );
      const bytes = await readExactByob(response, length);
      return new Blob([bytes.buffer as ArrayBuffer], { type: "video/mp4" });
    },
  });
}
