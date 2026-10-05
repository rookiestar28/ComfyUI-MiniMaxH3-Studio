// M25-00: content-free Authoring source-preview protocol. Transport and media ownership are
// deliberately absent; this module validates only logical currentness and closed dispositions.

export const AUTHORING_PREVIEW_REQUEST_SCHEMA =
  "h3.context.authoring_source_preview.request.v1" as const;
export const AUTHORING_PREVIEW_CAPABILITY_SCHEMA =
  "h3.context.authoring_source_preview.capability.v1" as const;
export const AUTHORING_PREVIEW_SUCCESS_SCHEMA =
  "h3.context.authoring_source_preview.success.v1" as const;
export const AUTHORING_PREVIEW_ERROR_SCHEMA =
  "h3.context.authoring_source_preview.error.v1" as const;

export type AuthoringPreviewCapabilityReason =
  "not_bound" | "stale" | "expired" | "unsupported" | "unavailable";

export type AuthoringPreviewErrorReason =
  | "invalid_request"
  | "authority_mismatch"
  | "stale"
  | "unsupported"
  | "source_too_large"
  | "source_too_long"
  | "busy"
  | "cancelled"
  | "timeout"
  | "conversion_failed"
  | "internal_failure";

export type AuthoringPreviewRequest = Readonly<{
  schema: typeof AUTHORING_PREVIEW_REQUEST_SCHEMA;
  requestId: string;
  workspaceHandle: string;
  referenceRevision: number;
  timelineRevision: number;
  timelineContentFingerprint: string;
  clipId: string;
}>;

export type AuthoringPreviewCapability = Readonly<{
  schema: typeof AUTHORING_PREVIEW_CAPABILITY_SCHEMA;
  available: boolean;
  reason: AuthoringPreviewCapabilityReason | null;
}>;

export type AuthoringPreviewSuccess = Readonly<{
  schema: typeof AUTHORING_PREVIEW_SUCCESS_SCHEMA;
  requestId: string;
  workspaceHandle: string;
  referenceRevision: number;
  timelineRevision: number;
  timelineContentFingerprint: string;
  clipId: string;
  sourceId: string;
}>;

export type AuthoringPreviewError = Readonly<{
  schema: typeof AUTHORING_PREVIEW_ERROR_SCHEMA;
  requestId: string | null;
  reason: AuthoringPreviewErrorReason;
}>;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;

function object(
  value: unknown,
  keys: readonly string[],
  name: string,
): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} must be an object`);
  const wire = value as Record<string, unknown>;
  const actual = Object.keys(wire).sort();
  const expected = [...keys].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected))
    throw new Error(`${name} must be closed`);
  return wire;
}

function exactString(value: unknown, pattern: RegExp, name: string): string {
  if (typeof value !== "string" || !pattern.test(value))
    throw new Error(`${name} is invalid`);
  return value;
}

function revision(value: unknown, name: string): number {
  if (
    !Number.isInteger(value) ||
    (value as number) < 1 ||
    (value as number) > 1_000_000
  )
    throw new Error(`${name} is invalid`);
  return value as number;
}

function requestFields(wire: Record<string, unknown>) {
  return {
    requestId: exactString(wire.requestId, identifier, "requestId"),
    workspaceHandle: exactString(
      wire.workspaceHandle,
      identifier,
      "workspaceHandle",
    ),
    referenceRevision: revision(wire.referenceRevision, "referenceRevision"),
    timelineRevision: revision(wire.timelineRevision, "timelineRevision"),
    timelineContentFingerprint: exactString(
      wire.timelineContentFingerprint,
      fingerprint,
      "timelineContentFingerprint",
    ),
    clipId: exactString(wire.clipId, identifier, "clipId"),
  };
}

export function decodeAuthoringPreviewRequest(
  value: unknown,
): AuthoringPreviewRequest {
  const wire = object(
    value,
    [
      "schema",
      "requestId",
      "workspaceHandle",
      "referenceRevision",
      "timelineRevision",
      "timelineContentFingerprint",
      "clipId",
    ],
    "authoring preview request",
  );
  if (wire.schema !== AUTHORING_PREVIEW_REQUEST_SCHEMA)
    throw new Error("authoring preview request schema is unsupported");
  return Object.freeze({
    schema: AUTHORING_PREVIEW_REQUEST_SCHEMA,
    ...requestFields(wire),
  });
}

export function encodeAuthoringPreviewRequest(
  value: AuthoringPreviewRequest,
): AuthoringPreviewRequest {
  return decodeAuthoringPreviewRequest(value);
}

export function decodeAuthoringPreviewCapability(
  value: unknown,
): AuthoringPreviewCapability {
  const wire = object(
    value,
    ["schema", "available", "reason"],
    "authoring preview capability",
  );
  if (wire.schema !== AUTHORING_PREVIEW_CAPABILITY_SCHEMA)
    throw new Error("authoring preview capability schema is unsupported");
  if (typeof wire.available !== "boolean")
    throw new Error("authoring preview capability available is invalid");
  const reasons: readonly AuthoringPreviewCapabilityReason[] = [
    "not_bound",
    "stale",
    "expired",
    "unsupported",
    "unavailable",
  ];
  if (wire.available) {
    if (wire.reason !== null)
      throw new Error("authoring preview capability reason is invalid");
  } else if (
    !reasons.includes(wire.reason as AuthoringPreviewCapabilityReason)
  ) {
    throw new Error("authoring preview capability reason is invalid");
  }
  return Object.freeze({
    schema: AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
    available: wire.available,
    reason: wire.reason as AuthoringPreviewCapabilityReason | null,
  });
}

export function decodeAuthoringPreviewSuccess(
  value: unknown,
): AuthoringPreviewSuccess {
  const wire = object(
    value,
    [
      "schema",
      "requestId",
      "workspaceHandle",
      "referenceRevision",
      "timelineRevision",
      "timelineContentFingerprint",
      "clipId",
      "sourceId",
    ],
    "authoring preview success",
  );
  if (wire.schema !== AUTHORING_PREVIEW_SUCCESS_SCHEMA)
    throw new Error("authoring preview success schema is unsupported");
  return Object.freeze({
    schema: AUTHORING_PREVIEW_SUCCESS_SCHEMA,
    ...requestFields(wire),
    sourceId: exactString(wire.sourceId, identifier, "sourceId"),
  });
}

export function decodeAuthoringPreviewError(
  value: unknown,
): AuthoringPreviewError {
  const wire = object(
    value,
    ["schema", "requestId", "reason"],
    "authoring preview error",
  );
  if (wire.schema !== AUTHORING_PREVIEW_ERROR_SCHEMA)
    throw new Error("authoring preview error schema is unsupported");
  const reasons: readonly AuthoringPreviewErrorReason[] = [
    "invalid_request",
    "authority_mismatch",
    "stale",
    "unsupported",
    "source_too_large",
    "source_too_long",
    "busy",
    "cancelled",
    "timeout",
    "conversion_failed",
    "internal_failure",
  ];
  if (!reasons.includes(wire.reason as AuthoringPreviewErrorReason))
    throw new Error("authoring preview error reason is invalid");
  return Object.freeze({
    schema: AUTHORING_PREVIEW_ERROR_SCHEMA,
    requestId:
      wire.requestId === null
        ? null
        : exactString(wire.requestId, identifier, "requestId"),
    reason: wire.reason as AuthoringPreviewErrorReason,
  });
}
