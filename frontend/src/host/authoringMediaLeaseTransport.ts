import {
  decodeAuthoringMediaLeaseError,
  type AuthoringMediaLeaseCreateRequest,
  type AuthoringMediaLeaseErrorReason,
  type AuthoringMediaLeaseSuccess,
} from "../contracts/authoringMediaLeaseCodec";

const MAX_CONTROL_BYTES = 8 * 1024;

export type AuthoringMediaSourceLeaseDisposition =
  AuthoringMediaLeaseErrorReason | "contract_mismatch" | "integrity_failure";

export class AuthoringMediaSourceLeaseError extends Error {
  constructor(
    readonly disposition: AuthoringMediaSourceLeaseDisposition,
    readonly status: number,
  ) {
    super(disposition);
  }
}

export function forbiddenHeaders(response: Response): boolean {
  return [
    "location",
    "content-range",
    "accept-ranges",
    "content-encoding",
    "transfer-encoding",
  ].some((name) => response.headers.has(name));
}

export async function cancelResponse(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // A content-free failure remains authoritative if transport teardown also fails.
  }
}

export async function readBounded(
  response: Response,
  maximum: number,
  exact?: number,
) {
  if (response.body === null)
    throw new AuthoringMediaSourceLeaseError(
      "contract_mismatch",
      response.status,
    );
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (
        !ArrayBuffer.isView(value) ||
        value.BYTES_PER_ELEMENT !== 1 ||
        value.byteLength === 0
      )
        throw new AuthoringMediaSourceLeaseError(
          "contract_mismatch",
          response.status,
        );
      const bytes = new Uint8Array(
        value.buffer,
        value.byteOffset,
        value.byteLength,
      );
      size += bytes.byteLength;
      if (size > maximum || (exact !== undefined && size > exact))
        throw new AuthoringMediaSourceLeaseError(
          "resource_limit",
          response.status,
        );
      chunks.push(bytes);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  if (exact !== undefined && size !== exact)
    throw new AuthoringMediaSourceLeaseError(
      "contract_mismatch",
      response.status,
    );
  const packed = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    packed.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return packed;
}

export function declaredLength(response: Response, maximum: number) {
  const text = response.headers.get("content-length");
  if (text === null || !/^[1-9][0-9]*$/.test(text))
    throw new AuthoringMediaSourceLeaseError(
      "contract_mismatch",
      response.status,
    );
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value > maximum)
    throw new AuthoringMediaSourceLeaseError("resource_limit", response.status);
  return value;
}

export async function decodeJsonResponse(
  response: Response,
  requestId: string,
) {
  if (
    response.headers.get("content-type") !== "application/json" ||
    response.headers.get("cache-control") !== "no-store" ||
    response.headers.get("x-content-type-options") !== "nosniff" ||
    forbiddenHeaders(response)
  ) {
    await cancelResponse(response);
    throw new AuthoringMediaSourceLeaseError(
      "contract_mismatch",
      response.status,
    );
  }
  const bytes = await readBounded(
    response,
    MAX_CONTROL_BYTES,
    declaredLength(response, MAX_CONTROL_BYTES),
  );
  let value: unknown;
  try {
    value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new AuthoringMediaSourceLeaseError(
      "contract_mismatch",
      response.status,
    );
  }
  if (!response.ok) {
    try {
      const error = decodeAuthoringMediaLeaseError(value);
      if (error.requestId !== null && error.requestId !== requestId)
        throw new Error("foreign request");
      throw new AuthoringMediaSourceLeaseError(error.reason, response.status);
    } catch (error) {
      if (error instanceof AuthoringMediaSourceLeaseError) throw error;
      throw new AuthoringMediaSourceLeaseError(
        "contract_mismatch",
        response.status,
      );
    }
  }
  return value;
}

export function assertReceipt(
  receipt: AuthoringMediaLeaseSuccess,
  expected: Readonly<{
    requestId: string;
    operation: "create" | "renew" | "transfer";
    ownerId: string;
    runtimeEpoch: number;
    derivativeKind: AuthoringMediaLeaseCreateRequest["derivativeKind"];
    profileFingerprint: string;
    assetFingerprint: string;
    prior?: AuthoringMediaLeaseSuccess;
  }>,
) {
  if (
    receipt.requestId !== expected.requestId ||
    receipt.operation !== expected.operation ||
    receipt.ownerId !== expected.ownerId ||
    receipt.runtimeEpoch !== expected.runtimeEpoch ||
    receipt.derivativeKind !== expected.derivativeKind ||
    receipt.profileFingerprint !== expected.profileFingerprint ||
    receipt.assetFingerprint !== expected.assetFingerprint ||
    (expected.prior !== undefined &&
      (receipt.leaseId !== expected.prior.leaseId ||
        receipt.derivativeFingerprint !==
          expected.prior.derivativeFingerprint ||
        receipt.assetFingerprint !== expected.prior.assetFingerprint ||
        receipt.mediaType !== expected.prior.mediaType ||
        receipt.byteCount !== expected.prior.byteCount ||
        receipt.audioDisposition !== expected.prior.audioDisposition))
  )
    throw new AuthoringMediaSourceLeaseError("contract_mismatch", 200);
}

export async function defaultDigest(bytes: Uint8Array) {
  const input = Uint8Array.from(bytes).buffer;
  const result = new Uint8Array(await crypto.subtle.digest("SHA-256", input));
  return `sha256:${Array.from(result, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}
