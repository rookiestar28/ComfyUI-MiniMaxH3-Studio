import { sha256Text } from "./canonicalFingerprint";
import {
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
} from "./authoringWorkbenchCodec";
import type { PublicRuntimeAsset } from "../runtime/publicAssetManifest";

export const AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA =
  "h3.context.authoring_media_lease.request.v1" as const;
export const NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA =
  "h3.context.authoring_asset_lease.request.v1" as const;
export const AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA =
  "h3.context.authoring_media_lease.success.v1" as const;
export const AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA =
  "h3.context.authoring_media_lease.released.v1" as const;
export const AUTHORING_MEDIA_LEASE_ERROR_SCHEMA =
  "h3.context.authoring_media_lease.error.v1" as const;
export const AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID =
  "h3.authoring.media_derivatives.v6" as const;
export const AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER =
  "X-H3-Context-Media-Lease" as const;
export const AUTHORING_MEDIA_LEASE_REVISION_HEADER =
  "X-H3-Context-Media-Lease-Revision" as const;
export const AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER =
  "X-H3-Context-Media-Derivative" as const;
export const AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER =
  "X-H3-Context-Media-Geometry" as const;
export type AuthoringMediaGeometry = Readonly<{
  schema: "h3.authoring.media_geometry.v1";
  sourceWidth: number;
  sourceHeight: number;
  derivativeWidth: number;
  derivativeHeight: number;
}>;

export function decodeAuthoringMediaGeometry(
  header: string | null,
  kind: AuthoringMediaDerivativeKind,
): AuthoringMediaGeometry | null {
  if (header === null || header === "null") return null;
  if (
    header.length > 512 ||
    !["video_proxy", "image_proxy", "thumbnail", "filmstrip"].includes(kind)
  )
    throw new Error("invalid media geometry");
  const keys = [
    "schema",
    "sourceWidth",
    "sourceHeight",
    "derivativeWidth",
    "derivativeHeight",
  ];
  // A JSON header is still untrusted. Duplicate keys must not select a different geometry
  // in two consumers while the independently verified media body stays unchanged.
  if (
    (
      header.match(
        /"(?:schema|sourceWidth|sourceHeight|derivativeWidth|derivativeHeight)"\s*:/gu,
      ) ?? []
    ).length !== keys.length
  )
    throw new Error("invalid media geometry");
  const wire = object(JSON.parse(header), keys, "media geometry");
  if (wire.schema !== "h3.authoring.media_geometry.v1")
    throw new Error("invalid media geometry");
  const sourceWidth = integer(wire.sourceWidth, 1, 16_384, "sourceWidth");
  const sourceHeight = integer(wire.sourceHeight, 1, 16_384, "sourceHeight");
  const derivativeWidth = integer(
    wire.derivativeWidth,
    1,
    kind === "video_proxy" ? 1_280 : 16_384,
    "derivativeWidth",
  );
  const derivativeHeight = integer(
    wire.derivativeHeight,
    1,
    kind === "video_proxy" ? 1_280 : 16_384,
    "derivativeHeight",
  );
  return Object.freeze({
    schema: "h3.authoring.media_geometry.v1",
    sourceWidth,
    sourceHeight,
    derivativeWidth,
    derivativeHeight,
  });
}

export type AuthoringMediaDerivativeKind =
  | "video_proxy"
  | "audio_preview"
  | "frame_timing_index"
  | "thumbnail"
  | "filmstrip"
  | "audio_peaks"
  | "image_proxy"
  | "packaged_font_face";
export type AuthoringMediaLeaseOperation =
  "create" | "open" | "renew" | "transfer" | "release";
export type AuthoringMediaLeaseScope = "clip" | "asset";
export type AuthoringMediaLeaseAudioDisposition =
  "present_bound" | "absent" | "unavailable" | "excluded_overlay_policy";
export type AuthoringMediaLeaseErrorReason =
  | "invalid_request"
  | "authority_mismatch"
  | "stale"
  | "lease_gone"
  | "unsupported"
  | "resource_limit"
  | "busy"
  | "cancelled"
  | "timeout"
  | "generation_failed"
  | "internal_failure";

type CommonRequest = Readonly<{
  schema: typeof AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA;
  operation: AuthoringMediaLeaseOperation;
  requestId: string;
}>;

export type AuthoringMediaLeaseCreateRequest = CommonRequest &
  Readonly<{
    operation: "create";
    workspaceHandle: string;
    workspaceRevision: number;
    timelineRevision: number;
    publicFingerprint: string;
    manifestFingerprint: string;
    profileFingerprint: string;
    scope: AuthoringMediaLeaseScope;
    clipId: string | null;
    assetId: string;
    derivativeKind: AuthoringMediaDerivativeKind;
    ownerId: string;
    runtimeEpoch: number;
    sourceStartFrame: number;
    sourceEndFrame: number;
  }>;

export type NleAuthoringAssetLeaseCreateRequest = Readonly<{
  schema: typeof NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA;
  operation: "create";
  requestId: string;
  authoringSchema: typeof NLE_AUTHORING_SCHEMA;
  profileId: typeof NLE_AUTHORING_PROFILE_ID;
  workspaceHandle: string;
  workspaceRevision: number;
  timelineRevision: number;
  authoringFingerprint: string;
  manifestFingerprint: string;
  profileFingerprint: string;
  assetId: string;
  derivativeKind:
    "thumbnail" | "filmstrip" | "audio_peaks" | "video_proxy" | "audio_preview";
  ownerId: string;
  runtimeEpoch: number;
  sourceStartFrame: number;
  sourceEndFrame: number;
}>;

export type AuthoringMediaLeaseOwnerRequest = CommonRequest &
  Readonly<{
    operation: "open" | "renew" | "release";
    leaseId: string;
    revision: number;
    ownerId: string;
    runtimeEpoch: number;
  }>;

export type AuthoringMediaLeaseTransferRequest = CommonRequest &
  Readonly<{
    operation: "transfer";
    leaseId: string;
    revision: number;
    ownerId: string;
    runtimeEpoch: number;
    nextOwnerId: string;
    nextRuntimeEpoch: number;
  }>;

export type AuthoringMediaLeaseRequest =
  | AuthoringMediaLeaseCreateRequest
  | NleAuthoringAssetLeaseCreateRequest
  | AuthoringMediaLeaseOwnerRequest
  | AuthoringMediaLeaseTransferRequest;

export type AuthoringMediaLeaseSuccess = Readonly<{
  schema: typeof AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA;
  requestId: string;
  operation: "create" | "renew" | "transfer";
  leaseId: string;
  revision: number;
  ownerId: string;
  runtimeEpoch: number;
  ttlMs: number;
  derivativeKind: AuthoringMediaDerivativeKind;
  mediaType: string;
  byteCount: number;
  derivativeFingerprint: string;
  assetFingerprint: string;
  derivativeProfileId: typeof AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID;
  profileFingerprint: string;
  audioDisposition: AuthoringMediaLeaseAudioDisposition;
}>;

export type AuthoringMediaLeaseReleased = Readonly<{
  schema: typeof AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA;
  requestId: string;
  operation: "release";
  leaseId: string;
}>;

export type AuthoringMediaLeaseError = Readonly<{
  schema: typeof AUTHORING_MEDIA_LEASE_ERROR_SCHEMA;
  requestId: string | null;
  reason: AuthoringMediaLeaseErrorReason;
}>;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const mediaTypes: Readonly<Record<AuthoringMediaDerivativeKind, string>> = {
  video_proxy: "video/mp4",
  audio_preview: "audio/wav",
  frame_timing_index: "application/json",
  thumbnail: "image/png",
  filmstrip: "image/jpeg",
  audio_peaks: "application/octet-stream",
  image_proxy: "image/png",
  packaged_font_face: "font/ttf",
};
const byteLimits: Readonly<Record<AuthoringMediaDerivativeKind, number>> = {
  video_proxy: 24 * 1024 * 1024,
  audio_preview: 8 * 1024 * 1024,
  frame_timing_index: 16 * 1024,
  thumbnail: 512 * 1024,
  filmstrip: 512 * 1024,
  audio_peaks: 64 * 1024,
  image_proxy: 16 * 1024 * 1024,
  packaged_font_face: 1024 * 1024,
};
const kinds = Object.freeze(
  Object.keys(mediaTypes) as AuthoringMediaDerivativeKind[],
);
const audioDispositions: readonly AuthoringMediaLeaseAudioDisposition[] = [
  "present_bound",
  "absent",
  "unavailable",
  "excluded_overlay_policy",
];
const reasons: readonly AuthoringMediaLeaseErrorReason[] = [
  "invalid_request",
  "authority_mismatch",
  "stale",
  "lease_gone",
  "unsupported",
  "resource_limit",
  "busy",
  "cancelled",
  "timeout",
  "generation_failed",
  "internal_failure",
];

function object(value: unknown, keys: readonly string[], name: string) {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} must be an object`);
  const wire = value as Record<string, unknown>;
  if (
    JSON.stringify(Object.keys(wire).sort()) !==
    JSON.stringify([...keys].sort())
  )
    throw new Error(`${name} must be closed`);
  return wire;
}

function exactString(value: unknown, pattern: RegExp, name: string): string {
  if (typeof value !== "string" || !pattern.test(value))
    throw new Error(`${name} is invalid`);
  return value;
}

function integer(value: unknown, min: number, max: number, name: string) {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < min ||
    (value as number) > max
  )
    throw new Error(`${name} is invalid`);
  return value as number;
}

function exactKind(value: unknown): AuthoringMediaDerivativeKind {
  if (!kinds.includes(value as AuthoringMediaDerivativeKind))
    throw new Error("derivativeKind is invalid");
  return value as AuthoringMediaDerivativeKind;
}

function ownerFields(wire: Record<string, unknown>) {
  return {
    requestId: exactString(wire.requestId, identifier, "requestId"),
    leaseId: exactString(wire.leaseId, identifier, "leaseId"),
    revision: integer(wire.revision, 1, Number.MAX_SAFE_INTEGER, "revision"),
    ownerId: exactString(wire.ownerId, identifier, "ownerId"),
    runtimeEpoch: integer(
      wire.runtimeEpoch,
      1,
      Number.MAX_SAFE_INTEGER,
      "runtimeEpoch",
    ),
  };
}

export function encodeAuthoringMediaLeaseRequest(
  value: AuthoringMediaLeaseRequest,
): AuthoringMediaLeaseRequest {
  if (value.operation === "create") {
    if (value.schema === NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA) {
      const wire = object(
        value,
        [
          "schema",
          "operation",
          "requestId",
          "authoringSchema",
          "profileId",
          "workspaceHandle",
          "workspaceRevision",
          "timelineRevision",
          "authoringFingerprint",
          "manifestFingerprint",
          "profileFingerprint",
          "assetId",
          "derivativeKind",
          "ownerId",
          "runtimeEpoch",
          "sourceStartFrame",
          "sourceEndFrame",
        ],
        "NLE authoring asset lease create request",
      );
      if (
        wire.schema !== NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA ||
        wire.operation !== "create" ||
        wire.authoringSchema !== NLE_AUTHORING_SCHEMA ||
        wire.profileId !== NLE_AUTHORING_PROFILE_ID
      )
        throw new Error(
          "NLE authoring asset lease request profile is unsupported",
        );
      const requestId = exactString(wire.requestId, identifier, "requestId");
      const workspaceHandle = exactString(
        wire.workspaceHandle,
        identifier,
        "workspaceHandle",
      );
      const workspaceRevision = integer(
        wire.workspaceRevision,
        0,
        Number.MAX_SAFE_INTEGER,
        "workspaceRevision",
      );
      const timelineRevision = integer(
        wire.timelineRevision,
        0,
        Number.MAX_SAFE_INTEGER,
        "timelineRevision",
      );
      const authoringFingerprint = exactString(
        wire.authoringFingerprint,
        fingerprint,
        "authoringFingerprint",
      );
      const manifestFingerprint = exactString(
        wire.manifestFingerprint,
        fingerprint,
        "manifestFingerprint",
      );
      const profileFingerprint = exactString(
        wire.profileFingerprint,
        fingerprint,
        "profileFingerprint",
      );
      const assetId = exactString(wire.assetId, identifier, "assetId");
      const derivativeKind = wire.derivativeKind;
      // IMPORTANT: this form alone may name a playback kind without a clip; it prepares a
      // catalog asset before any clip uses it. The snapshot form's asset scope below stays the
      // three decorations, as the service's request contract does: admitting a playback kind
      // there would send a request that the service refuses.
      if (
        derivativeKind !== "thumbnail" &&
        derivativeKind !== "filmstrip" &&
        derivativeKind !== "audio_peaks" &&
        derivativeKind !== "video_proxy" &&
        derivativeKind !== "audio_preview"
      )
        throw new Error("derivativeKind is invalid for authoring asset scope");
      const ownerId = exactString(wire.ownerId, identifier, "ownerId");
      const runtimeEpoch = integer(
        wire.runtimeEpoch,
        1,
        Number.MAX_SAFE_INTEGER,
        "runtimeEpoch",
      );
      const sourceStartFrame = integer(
        wire.sourceStartFrame,
        0,
        511,
        "sourceStartFrame",
      );
      const sourceEndFrame = integer(
        wire.sourceEndFrame,
        1,
        512,
        "sourceEndFrame",
      );
      if (sourceStartFrame >= sourceEndFrame)
        throw new Error("source span is invalid");
      return Object.freeze({
        schema: NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
        operation: "create",
        requestId,
        authoringSchema: NLE_AUTHORING_SCHEMA,
        profileId: NLE_AUTHORING_PROFILE_ID,
        workspaceHandle,
        workspaceRevision,
        timelineRevision,
        authoringFingerprint,
        manifestFingerprint,
        profileFingerprint,
        assetId,
        derivativeKind,
        ownerId,
        runtimeEpoch,
        sourceStartFrame,
        sourceEndFrame,
      });
    }
    const wire = object(
      value,
      [
        "schema",
        "operation",
        "requestId",
        "workspaceHandle",
        "workspaceRevision",
        "timelineRevision",
        "publicFingerprint",
        "manifestFingerprint",
        "profileFingerprint",
        "scope",
        "clipId",
        "assetId",
        "derivativeKind",
        "ownerId",
        "runtimeEpoch",
        "sourceStartFrame",
        "sourceEndFrame",
      ],
      "authoring media lease create request",
    );
    if (wire.schema !== AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA)
      throw new Error("authoring media lease request schema is unsupported");
    const derivativeKind = exactKind(wire.derivativeKind);
    if (wire.scope !== "clip" && wire.scope !== "asset")
      throw new Error("authoring media lease scope is invalid");
    const scope = wire.scope;
    const clipId =
      scope === "clip"
        ? exactString(wire.clipId, identifier, "clipId")
        : wire.clipId === null
          ? null
          : (() => {
              throw new Error("clipId is invalid for asset scope");
            })();
    if (
      scope === "asset" &&
      derivativeKind !== "thumbnail" &&
      derivativeKind !== "filmstrip" &&
      derivativeKind !== "audio_peaks"
    )
      throw new Error("derivativeKind is invalid for asset scope");
    // CRITICAL: full-source leases must admit the composition's 512 timing rows; a 64-frame
    // mirror rejects valid Production originals before fetch. Byte and lease quotas stay separate.
    const sourceStartFrame = integer(
      wire.sourceStartFrame,
      0,
      511,
      "sourceStartFrame",
    );
    const sourceEndFrame = integer(
      wire.sourceEndFrame,
      1,
      512,
      "sourceEndFrame",
    );
    if (sourceStartFrame >= sourceEndFrame)
      throw new Error("source span is invalid");
    const clipSpanInvalid =
      scope === "clip" &&
      (((derivativeKind === "video_proxy" ||
        derivativeKind === "audio_preview" ||
        derivativeKind === "frame_timing_index") &&
        sourceStartFrame !== 0) ||
        ((derivativeKind === "thumbnail" ||
          derivativeKind === "image_proxy" ||
          derivativeKind === "packaged_font_face") &&
          sourceEndFrame !== sourceStartFrame + 1) ||
        derivativeKind === "filmstrip" ||
        derivativeKind === "audio_peaks" ||
        ((derivativeKind === "image_proxy" ||
          derivativeKind === "packaged_font_face") &&
          sourceStartFrame !== 0));
    if (
      (scope === "asset" &&
        (sourceStartFrame !== 0 ||
          (derivativeKind === "thumbnail" && sourceEndFrame !== 1))) ||
      clipSpanInvalid
    )
      throw new Error("source span is invalid");
    return Object.freeze({
      schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
      operation: "create",
      requestId: exactString(wire.requestId, identifier, "requestId"),
      workspaceHandle: exactString(
        wire.workspaceHandle,
        identifier,
        "workspaceHandle",
      ),
      workspaceRevision: integer(
        wire.workspaceRevision,
        1,
        Number.MAX_SAFE_INTEGER,
        "workspaceRevision",
      ),
      timelineRevision: integer(
        wire.timelineRevision,
        1,
        Number.MAX_SAFE_INTEGER,
        "timelineRevision",
      ),
      publicFingerprint: exactString(
        wire.publicFingerprint,
        fingerprint,
        "publicFingerprint",
      ),
      manifestFingerprint: exactString(
        wire.manifestFingerprint,
        fingerprint,
        "manifestFingerprint",
      ),
      profileFingerprint: exactString(
        wire.profileFingerprint,
        fingerprint,
        "profileFingerprint",
      ),
      scope,
      clipId,
      assetId: exactString(wire.assetId, identifier, "assetId"),
      derivativeKind,
      ownerId: exactString(wire.ownerId, identifier, "ownerId"),
      runtimeEpoch: integer(
        wire.runtimeEpoch,
        1,
        Number.MAX_SAFE_INTEGER,
        "runtimeEpoch",
      ),
      sourceStartFrame,
      sourceEndFrame,
    });
  }
  const transfer = value.operation === "transfer";
  const wire = object(
    value,
    transfer
      ? [
          "schema",
          "operation",
          "requestId",
          "leaseId",
          "revision",
          "ownerId",
          "runtimeEpoch",
          "nextOwnerId",
          "nextRuntimeEpoch",
        ]
      : [
          "schema",
          "operation",
          "requestId",
          "leaseId",
          "revision",
          "ownerId",
          "runtimeEpoch",
        ],
    "authoring media lease owner request",
  );
  if (wire.schema !== AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA)
    throw new Error("authoring media lease request schema is unsupported");
  const fields = ownerFields(wire);
  if (transfer) {
    const nextOwnerId = exactString(
      wire.nextOwnerId,
      identifier,
      "nextOwnerId",
    );
    const nextRuntimeEpoch = integer(
      wire.nextRuntimeEpoch,
      1,
      Number.MAX_SAFE_INTEGER,
      "nextRuntimeEpoch",
    );
    if (
      nextOwnerId === fields.ownerId &&
      nextRuntimeEpoch === fields.runtimeEpoch
    )
      throw new Error("transfer owner is unchanged");
    return Object.freeze({
      schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
      operation: "transfer",
      ...fields,
      nextOwnerId,
      nextRuntimeEpoch,
    });
  }
  if (!["open", "renew", "release"].includes(wire.operation as string))
    throw new Error("authoring media lease operation is unsupported");
  return Object.freeze({
    schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
    operation: wire.operation as "open" | "renew" | "release",
    ...fields,
  });
}

export function decodeAuthoringMediaLeaseSuccess(
  value: unknown,
): AuthoringMediaLeaseSuccess {
  const wire = object(
    value,
    [
      "schema",
      "requestId",
      "operation",
      "leaseId",
      "revision",
      "ownerId",
      "runtimeEpoch",
      "ttlMs",
      "derivativeKind",
      "mediaType",
      "byteCount",
      "derivativeFingerprint",
      "assetFingerprint",
      "derivativeProfileId",
      "profileFingerprint",
      "audioDisposition",
    ],
    "authoring media lease success",
  );
  if (wire.schema !== AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA)
    throw new Error("authoring media lease success schema is unsupported");
  if (!["create", "renew", "transfer"].includes(wire.operation as string))
    throw new Error("authoring media lease success operation is invalid");
  const derivativeKind = exactKind(wire.derivativeKind);
  if (wire.mediaType !== mediaTypes[derivativeKind])
    throw new Error("mediaType is invalid");
  if (
    !audioDispositions.includes(
      wire.audioDisposition as AuthoringMediaLeaseAudioDisposition,
    )
  )
    throw new Error("audioDisposition is invalid");
  if (
    ((derivativeKind === "audio_peaks" || derivativeKind === "audio_preview") &&
      wire.audioDisposition !== "present_bound") ||
    (derivativeKind !== "video_proxy" &&
      derivativeKind !== "audio_preview" &&
      derivativeKind !== "audio_peaks" &&
      wire.audioDisposition !== "absent")
  )
    throw new Error("audioDisposition is invalid for derivativeKind");
  if (wire.derivativeProfileId !== AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID)
    throw new Error("derivativeProfileId is invalid");
  return Object.freeze({
    schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
    requestId: exactString(wire.requestId, identifier, "requestId"),
    operation: wire.operation as "create" | "renew" | "transfer",
    leaseId: exactString(wire.leaseId, identifier, "leaseId"),
    revision: integer(wire.revision, 1, Number.MAX_SAFE_INTEGER, "revision"),
    ownerId: exactString(wire.ownerId, identifier, "ownerId"),
    runtimeEpoch: integer(
      wire.runtimeEpoch,
      1,
      Number.MAX_SAFE_INTEGER,
      "runtimeEpoch",
    ),
    ttlMs: integer(wire.ttlMs, 0, 60_000, "ttlMs"),
    derivativeKind,
    mediaType: wire.mediaType,
    byteCount: integer(
      wire.byteCount,
      1,
      byteLimits[derivativeKind],
      "byteCount",
    ),
    derivativeFingerprint: exactString(
      wire.derivativeFingerprint,
      fingerprint,
      "derivativeFingerprint",
    ),
    assetFingerprint: exactString(
      wire.assetFingerprint,
      fingerprint,
      "assetFingerprint",
    ),
    derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
    profileFingerprint: exactString(
      wire.profileFingerprint,
      fingerprint,
      "profileFingerprint",
    ),
    audioDisposition:
      wire.audioDisposition as AuthoringMediaLeaseAudioDisposition,
  });
}

export function decodeAuthoringMediaLeaseReleased(
  value: unknown,
): AuthoringMediaLeaseReleased {
  const wire = object(
    value,
    ["schema", "requestId", "operation", "leaseId"],
    "authoring media lease released",
  );
  if (
    wire.schema !== AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA ||
    wire.operation !== "release"
  )
    throw new Error("authoring media lease released envelope is invalid");
  return Object.freeze({
    schema: AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA,
    requestId: exactString(wire.requestId, identifier, "requestId"),
    operation: "release",
    leaseId: exactString(wire.leaseId, identifier, "leaseId"),
  });
}

export function decodeAuthoringMediaLeaseError(
  value: unknown,
): AuthoringMediaLeaseError {
  const wire = object(
    value,
    ["schema", "requestId", "reason"],
    "authoring media lease error",
  );
  if (wire.schema !== AUTHORING_MEDIA_LEASE_ERROR_SCHEMA)
    throw new Error("authoring media lease error schema is unsupported");
  if (!reasons.includes(wire.reason as AuthoringMediaLeaseErrorReason))
    throw new Error("authoring media lease error reason is invalid");
  return Object.freeze({
    schema: AUTHORING_MEDIA_LEASE_ERROR_SCHEMA,
    requestId:
      wire.requestId === null
        ? null
        : exactString(wire.requestId, identifier, "requestId"),
    reason: wire.reason as AuthoringMediaLeaseErrorReason,
  });
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function canonicalPublicRuntimeAssetFingerprint(
  asset: PublicRuntimeAsset,
): string {
  return sha256Text(canonicalJson(asset));
}
