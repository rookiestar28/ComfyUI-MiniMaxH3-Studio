import { WORKSPACE_STATE_CODES } from "./workspaceStateCodec";

export const RETAINED_RESPONSE_SCHEMA =
  "h3.context.retained_assets_response.v1" as const;
export const RETAINED_STATUS_SCHEMA =
  "h3.context.retained_assets_status.v1" as const;
const CODES = [
  ...WORKSPACE_STATE_CODES,
  "asset_invalid",
  "catalog_invalid",
  "asset_unavailable",
  "asset_changed",
  "media_unqualified",
  "source_stale",
  "source_unavailable",
  "lease_invalid",
  "lease_bound",
  "asset_protected",
  "reference_invalid",
  "cancelled",
  "timed_out",
  "catalog_corrupt",
  "quota_assets",
  "work_busy",
] as const;
export type RetainedAssetsCode = (typeof CODES)[number];
export type RetainedAssetRow = Readonly<{
  asset_id: string;
  byte_length: number;
  width: number;
  height: number;
  frame_count: number;
  duration_ms: number;
  created_at_ms: number;
  closed_at_ms: number | null;
  referenced: boolean;
  state: "retained" | "expired";
}>;
export type RetainedAssetsProjection = Readonly<{
  schema: typeof RETAINED_STATUS_SCHEMA;
  supported: boolean;
  enabled: boolean;
  revision: number;
  count: number;
  charged_bytes: number;
  remnant_count: number;
  protected: number;
  assets: readonly RetainedAssetRow[];
}>;
export type RestoredAsset = Readonly<{
  asset_id: string;
  use_handle: string;
  width: number;
  height: number;
  frame_count: number;
  preview_available: boolean;
}>;
export type RetainedAssetsResponse = Readonly<{
  schema: typeof RETAINED_RESPONSE_SCHEMA;
  projection: RetainedAssetsProjection;
  error: RetainedAssetsCode | null;
  retained_id: string | null;
  restored: RestoredAsset | null;
  cleanup: Readonly<{ removed: number; protected: number }> | null;
}>;
export type RetainSelection = Readonly<{
  workspace_handle: string;
  workspace_id: string;
  expected_workspace_revision: number;
  expected_workspace_fingerprint: string;
  segment_id: string;
  output_handle: string;
}>;
export function retainSelectionKey(selection: RetainSelection): string {
  return JSON.stringify([
    selection.workspace_handle,
    selection.workspace_id,
    selection.expected_workspace_revision,
    selection.expected_workspace_fingerprint,
    selection.segment_id,
    selection.output_handle,
  ]);
}
export type RetainedAssetsAction =
  | Readonly<{ intent: "status" | "list" }>
  | Readonly<{
      intent: "set_enabled";
      enabled: boolean;
      expected_revision: number;
    }>
  | Readonly<{ intent: "restore"; asset_id: string; expected_revision: number }>
  | Readonly<{ intent: "release"; use_handle: string }>
  | Readonly<{ intent: "clear" | "collect"; expected_revision: number }>
  | (Readonly<{ intent: "retain"; expected_revision: number }> &
      RetainSelection);

function reject(): never {
  throw new Error("retained_assets_wire_invalid");
}
function object(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype ||
    Object.keys(value).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(value, key))
  )
    return reject();
  return value as Record<string, unknown>;
}
function integer(
  value: unknown,
  maximum = Number.MAX_SAFE_INTEGER,
  minimum = 0,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  )
    return reject();
  return value;
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") return reject();
  return value;
}
function assetId(value: unknown): string {
  if (typeof value !== "string" || !/^asset_[a-f0-9]{32}$/.test(value))
    return reject();
  return value;
}
export function retainedUseHandle(value: unknown): string {
  if (typeof value !== "string" || !/^retained_[a-f0-9]{32}$/.test(value))
    return reject();
  return value;
}
function row(value: unknown): RetainedAssetRow {
  const wire = object(value, [
    "asset_id",
    "byte_length",
    "width",
    "height",
    "frame_count",
    "duration_ms",
    "created_at_ms",
    "closed_at_ms",
    "referenced",
    "state",
  ]);
  const created = integer(wire.created_at_ms, 9_999_999_999_999, 1),
    closed =
      wire.closed_at_ms === null
        ? null
        : integer(wire.closed_at_ms, 9_999_999_999_999, created),
    referenced = boolean(wire.referenced);
  if (
    (referenced && closed !== null) ||
    (!referenced && closed === null) ||
    !["retained", "expired"].includes(wire.state as string)
  )
    return reject();
  return Object.freeze({
    asset_id: assetId(wire.asset_id),
    byte_length: integer(wire.byte_length, 64 * 1024 * 1024, 1),
    width: integer(wire.width, 2048, 1),
    height: integer(wire.height, 2048, 1),
    frame_count: integer(wire.frame_count, 512, 1),
    duration_ms: integer(wire.duration_ms, 30_000, 1),
    created_at_ms: created,
    closed_at_ms: closed,
    referenced,
    state: wire.state as RetainedAssetRow["state"],
  });
}
export function decodeRetainedAssetsResponse(
  value: unknown,
): RetainedAssetsResponse {
  const wire = object(value, [
      "schema",
      "projection",
      "error",
      "retained_id",
      "restored",
      "cleanup",
    ]),
    projection = object(wire.projection, [
      "schema",
      "supported",
      "enabled",
      "revision",
      "count",
      "charged_bytes",
      "remnant_count",
      "protected",
      "assets",
    ]);
  if (
    wire.schema !== RETAINED_RESPONSE_SCHEMA ||
    projection.schema !== RETAINED_STATUS_SCHEMA ||
    !Array.isArray(projection.assets) ||
    projection.assets.length > 16
  )
    return reject();
  const assets = Object.freeze(projection.assets.map(row)),
    count = integer(projection.count, 16),
    charged = integer(projection.charged_bytes, 256 * 1024 * 1024),
    protectedCount = integer(projection.protected, count),
    supported = boolean(projection.supported),
    enabled = boolean(projection.enabled),
    revision = integer(projection.revision),
    remnants = integer(projection.remnant_count, 128);
  // IMPORTANT: missing/truncated copies keep catalog lengths but reduce physical charge.
  // Never use historical lengths as a charge floor; that blocks explicit recovery/cleanup.
  if (
    count !== assets.length ||
    new Set(assets.map((asset) => asset.asset_id)).size !== count ||
    (!supported &&
      (enabled || revision || count || charged || remnants || protectedCount))
  )
    return reject();
  let restored: RestoredAsset | null = null;
  if (wire.restored !== null) {
    const fresh = object(wire.restored, [
        "asset_id",
        "use_handle",
        "width",
        "height",
        "frame_count",
        "preview_available",
      ]),
      id = assetId(fresh.asset_id),
      asset = assets.find((entry) => entry.asset_id === id);
    if (
      !enabled ||
      !asset ||
      asset.state === "expired" ||
      fresh.width !== asset.width ||
      fresh.height !== asset.height ||
      fresh.frame_count !== asset.frame_count
    )
      return reject();
    restored = Object.freeze({
      asset_id: id,
      use_handle: retainedUseHandle(fresh.use_handle),
      width: asset.width,
      height: asset.height,
      frame_count: asset.frame_count,
      preview_available: boolean(fresh.preview_available),
    });
  }
  const retained = wire.retained_id === null ? null : assetId(wire.retained_id);
  if (
    retained !== null &&
    (!enabled || !assets.some((asset) => asset.asset_id === retained))
  )
    return reject();
  let cleanup = null;
  if (wire.cleanup !== null) {
    const clean = object(wire.cleanup, ["removed", "protected"]);
    cleanup = Object.freeze({
      removed: integer(clean.removed, 16),
      protected: integer(clean.protected, 16),
    });
    if (cleanup.protected !== protectedCount) return reject();
  }
  if (
    wire.error !== null &&
    (typeof wire.error !== "string" ||
      !CODES.includes(wire.error as RetainedAssetsCode) ||
      restored !== null ||
      retained !== null ||
      cleanup !== null)
  )
    return reject();
  if (
    [restored, retained, cleanup].filter((field) => field !== null).length > 1
  )
    return reject();
  return Object.freeze({
    schema: RETAINED_RESPONSE_SCHEMA,
    projection: Object.freeze({
      schema: RETAINED_STATUS_SCHEMA,
      supported,
      enabled,
      revision,
      count,
      charged_bytes: charged,
      remnant_count: remnants,
      protected: protectedCount,
      assets,
    }),
    error: wire.error as RetainedAssetsCode | null,
    retained_id: retained,
    restored,
    cleanup,
  });
}

export function encodeRetainedAssetsAction(
  action: RetainedAssetsAction,
): string {
  const fields: Record<RetainedAssetsAction["intent"], readonly string[]> = {
    status: ["intent"],
    list: ["intent"],
    set_enabled: ["intent", "enabled", "expected_revision"],
    restore: ["intent", "asset_id", "expected_revision"],
    release: ["intent", "use_handle"],
    clear: ["intent", "expected_revision"],
    collect: ["intent", "expected_revision"],
    retain: [
      "intent",
      "expected_revision",
      "workspace_handle",
      "workspace_id",
      "expected_workspace_revision",
      "expected_workspace_fingerprint",
      "segment_id",
      "output_handle",
    ],
  };
  if (!fields[action?.intent]) return reject();
  const wire = object(action, fields[action.intent]);
  if ("expected_revision" in wire) integer(wire.expected_revision);
  if (action.intent === "set_enabled") boolean(wire.enabled);
  if (action.intent === "restore") assetId(wire.asset_id);
  if (action.intent === "release") retainedUseHandle(wire.use_handle);
  if (action.intent === "retain") {
    integer(wire.expected_workspace_revision);
    for (const key of [
      "workspace_handle",
      "workspace_id",
      "expected_workspace_fingerprint",
      "segment_id",
      "output_handle",
    ])
      if (
        typeof wire[key] !== "string" ||
        wire[key].length < 1 ||
        wire[key].length > 160
      )
        return reject();
  }
  const encoded = JSON.stringify(action);
  if (new TextEncoder().encode(encoded).byteLength > 8192) return reject();
  return encoded;
}
