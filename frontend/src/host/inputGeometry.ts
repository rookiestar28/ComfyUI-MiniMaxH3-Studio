export const INPUT_GEOMETRY_REQUEST_SCHEMA =
  "h3.context.input_geometry.request.v2" as const;
export const INPUT_GEOMETRY_RECEIPT_SCHEMA =
  "h3.context.input_geometry.receipt.v2" as const;
export const INPUT_GEOMETRY_ERROR_SCHEMA =
  "h3.context.input_geometry.error.v1" as const;
export const INPUT_GEOMETRY_ROUTE = "/h3-context/v1/input/geometry" as const;
const MAX_REQUEST_BYTES = 4_096;
const MAX_RESPONSE_BYTES = 4_096;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const receiptHandle = /^ig_[A-Za-z0-9_-]{32,96}$/;
const windowsReservedName = /^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i;
const safeBackendCategories = [
  "geometry_capacity",
  "geometry_identity_unavailable",
  "host_input_unavailable",
  "image_format_unsupported",
  "image_header_unsupported",
  "image_pixels_exceeded",
  "input_changed",
  "input_locator_unsafe",
  "input_too_large",
  "input_unavailable",
  "input_unsafe",
  "internal_failure",
  "invalid_request",
  "media_type_rejected",
  "origin_rejected",
  "request_too_large",
] as const;
type SafeBackendCategory = (typeof safeBackendCategories)[number];
const safeBackendCategorySet: ReadonlySet<string> = new Set(
  safeBackendCategories,
);

export type InputGeometryRequest = Readonly<{
  schema: typeof INPUT_GEOMETRY_REQUEST_SCHEMA;
  locator: string;
}>;

export type InputGeometryReceipt = Readonly<{
  schema: typeof INPUT_GEOMETRY_RECEIPT_SCHEMA;
  receipt_handle: string;
  source_fingerprint: string;
}>;

export type InputGeometryClientFailure =
  | "invalid_request"
  | "seam_unavailable"
  | "route_rejected"
  | "payload_rejected"
  | SafeBackendCategory;

export class InputGeometryClientError extends Error {
  readonly category: InputGeometryClientFailure;
  readonly status: number;

  constructor(category: InputGeometryClientFailure, status = 0) {
    // CRITICAL: only stable local or allow-listed backend categories may become public text.
    super(category);
    this.name = "InputGeometryClientError";
    this.category = category;
    this.status = status;
  }
}

type FetchResponse = Readonly<{
  ok: boolean;
  status: number;
  text(): Promise<string>;
}>;

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  return Object.keys(value).sort().join("\0") === [...keys].sort().join("\0");
}

function isSafeLocator(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 512 ||
    value !== value.trim() ||
    value.includes("\0") ||
    value.includes(":") ||
    value.startsWith("/") ||
    value.startsWith("\\")
  )
    return false;
  const parts = value.replaceAll("\\", "/").split("/");
  // CRITICAL: mirror the adapter's Windows alias rejection before transport.
  return parts.every(
    (part) =>
      part !== "" &&
      part !== "." &&
      part !== ".." &&
      !part.endsWith(".") &&
      !part.endsWith(" ") &&
      !/[<>"|?*\u0000-\u001f]/.test(part) &&
      !windowsReservedName.test(part.split(".", 1)[0]!),
  );
}

function validateRequest(value: InputGeometryRequest): InputGeometryRequest {
  if (
    !isObject(value) ||
    !hasExactKeys(value, ["schema", "locator"]) ||
    value.schema !== INPUT_GEOMETRY_REQUEST_SCHEMA ||
    !isSafeLocator(value.locator)
  )
    throw new InputGeometryClientError("invalid_request");
  return value;
}

function decodeReceipt(value: unknown): InputGeometryReceipt {
  if (
    !isObject(value) ||
    !hasExactKeys(value, ["schema", "receipt_handle", "source_fingerprint"]) ||
    value.schema !== INPUT_GEOMETRY_RECEIPT_SCHEMA ||
    typeof value.receipt_handle !== "string" ||
    !receiptHandle.test(value.receipt_handle) ||
    typeof value.source_fingerprint !== "string" ||
    !fingerprint.test(value.source_fingerprint)
  )
    throw new InputGeometryClientError("payload_rejected", 200);
  return Object.freeze({
    schema: INPUT_GEOMETRY_RECEIPT_SCHEMA,
    receipt_handle: value.receipt_handle,
    source_fingerprint: value.source_fingerprint,
  });
}

function decodeSafeError(
  body: string,
  status: number,
): InputGeometryClientError {
  try {
    const value: unknown = JSON.parse(body);
    if (
      isObject(value) &&
      hasExactKeys(value, ["schema", "category"]) &&
      value.schema === INPUT_GEOMETRY_ERROR_SCHEMA &&
      typeof value.category === "string" &&
      safeBackendCategorySet.has(value.category)
    )
      return new InputGeometryClientError(
        value.category as SafeBackendCategory,
        status,
      );
  } catch {
    // Fall through to a stable local failure; never expose parser/response text.
  }
  return new InputGeometryClientError("route_rejected", status);
}

export function createInputGeometryClient({
  fetchApi,
}: {
  fetchApi?: (path: string, init: RequestInit) => Promise<FetchResponse>;
}) {
  return Object.freeze({
    async observe(
      value: InputGeometryRequest,
      signal?: AbortSignal,
    ): Promise<InputGeometryReceipt> {
      const request = validateRequest(value);
      if (typeof fetchApi !== "function")
        throw new InputGeometryClientError("seam_unavailable");
      const body = JSON.stringify(request);
      if (body.length > MAX_REQUEST_BYTES)
        throw new InputGeometryClientError("invalid_request");
      let response: FetchResponse;
      try {
        response = await fetchApi(INPUT_GEOMETRY_ROUTE, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            accept: "application/json",
            "content-type": "application/json",
          },
          body,
          signal,
        });
      } catch (error) {
        if (signal?.aborted) throw error;
        throw new InputGeometryClientError("seam_unavailable");
      }
      let responseBody: string;
      try {
        responseBody = await response.text();
      } catch {
        throw new InputGeometryClientError(
          response.ok ? "payload_rejected" : "route_rejected",
          response.status,
        );
      }
      if (
        typeof responseBody !== "string" ||
        responseBody.length > MAX_RESPONSE_BYTES
      )
        throw new InputGeometryClientError(
          response.ok ? "payload_rejected" : "route_rejected",
          response.status,
        );
      if (!response.ok) throw decodeSafeError(responseBody, response.status);
      if (response.status !== 200)
        throw new InputGeometryClientError("route_rejected", response.status);
      try {
        return decodeReceipt(JSON.parse(responseBody));
      } catch (error) {
        if (error instanceof InputGeometryClientError) throw error;
        throw new InputGeometryClientError("payload_rejected", 200);
      }
    },
  });
}
