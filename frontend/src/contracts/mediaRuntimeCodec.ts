// Closed manual media-tool status/request contracts. Unknown keys and vocabulary fail closed.
export const MEDIA_RUNTIME_STATUS_SCHEMA =
  "h3.context.media_runtime_status.v4" as const;
export const MEDIA_RUNTIME_REQUEST_SCHEMA =
  "h3.context.media_runtime_setup_request.v2" as const;
const RESOLUTION_SCHEMA = "h3.context.media_runtime_resolution.v1";

export const MEDIA_RUNTIME_RESOLUTION_STATES = [
  "located",
  "unavailable",
  "invalid_config",
  "discovering",
] as const;
export const MEDIA_RUNTIME_RESOLUTION_REASONS = [
  "pair_admitted",
  "supported_pair_missing",
  "unsupported_platform",
  "unsupported_host",
  "private_root_invalid",
  "override_incomplete",
  "override_invalid_marker",
  "override_invalid_path",
  "override_split_directory",
  "override_unsupported_pair",
  "local_selection_invalid_path",
  "local_selection_unsupported_pair",
  "config_corrupt",
  "config_unsupported",
  "discovery_limit",
  "discovery_timeout",
  "worker_failure",
  "discovery_in_progress",
] as const;
export const MEDIA_RUNTIME_SOURCE_KINDS = [
  "none",
  "explicit_override",
  "local_selection",
  "managed",
  "python_prefix",
  "system_path",
] as const;
export const MEDIA_RUNTIME_CONFIG_SELECTIONS = ["auto", "local"] as const;
export const MEDIA_RUNTIME_FEATURES = [
  "import",
  "preview",
  "derivatives",
  "assembly",
  "render",
] as const;
export const MEDIA_RUNTIME_FEATURE_STATES = [
  "ready",
  "setup_required",
  "unavailable",
] as const;
export const MEDIA_RUNTIME_FEATURE_REASONS = [
  ...MEDIA_RUNTIME_RESOLUTION_REASONS,
  "activating",
  "activation_failed",
  "discovering",
  "invalid_config",
  "render_qualification_unavailable",
] as const;
export const MEDIA_RUNTIME_ACTIONS = [
  "rescan",
  "use_local_directory",
  "restore_auto",
  "reclaim_parked_runtime",
] as const;
export const MEDIA_RUNTIME_RECOVERY_STATES = [
  "parked_runtime",
  "abandoned_setup",
] as const;
export const MEDIA_RUNTIME_REFUSAL_CODES = [
  "config_conflict",
  "config_corrupt",
  "config_unsupported",
  "config_write_failed",
  "discovery_limit",
  "discovery_timeout",
  "internal_failure",
  "invalid_request",
  "media_runtime_busy",
  "media_type_rejected",
  "origin_rejected",
  "private_root_invalid",
  "reclaim_unsafe",
  "request_too_large",
  "selection_invalid_path",
  "selection_unsupported_pair",
  "setup_busy",
  "unsupported_host",
  "unsupported_platform",
  "worker_failure",
] as const;

export type MediaRuntimeFeature = (typeof MEDIA_RUNTIME_FEATURES)[number];
export type MediaRuntimeFeatureState =
  (typeof MEDIA_RUNTIME_FEATURE_STATES)[number];
export type MediaRuntimeFeatureReason =
  (typeof MEDIA_RUNTIME_FEATURE_REASONS)[number];
export type MediaRuntimeAction = (typeof MEDIA_RUNTIME_ACTIONS)[number];
export type MediaRuntimeRefusalCode =
  (typeof MEDIA_RUNTIME_REFUSAL_CODES)[number];

export type MediaRuntimeFeatureReadiness = Readonly<{
  state: MediaRuntimeFeatureState;
  reason: MediaRuntimeFeatureReason | null;
}>;

export type MediaRuntimeStatus = Readonly<{
  schema: typeof MEDIA_RUNTIME_STATUS_SCHEMA;
  resolution: Readonly<{
    state: (typeof MEDIA_RUNTIME_RESOLUTION_STATES)[number];
    reason: (typeof MEDIA_RUNTIME_RESOLUTION_REASONS)[number];
    sourceKind: (typeof MEDIA_RUNTIME_SOURCE_KINDS)[number];
  }>;
  config: Readonly<{
    revision: number;
    selection: (typeof MEDIA_RUNTIME_CONFIG_SELECTIONS)[number];
  }> | null;
  manual: Readonly<{
    archiveName: string;
    archiveSha256: string;
    sourceLabel: string;
    releasePage: string;
    license: string;
    approximateBytes: number;
  }>;
  features: Readonly<Record<MediaRuntimeFeature, MediaRuntimeFeatureReadiness>>;
  recovery: Readonly<{
    state: (typeof MEDIA_RUNTIME_RECOVERY_STATES)[number];
    reclaimable: boolean;
  }> | null;
  actions: readonly MediaRuntimeAction[];
}>;

/** Byte counts share the server's canonical integer range and the manifest's archive bound. */
const MAX_BYTES = 2 ** 53 - 1;
const MAX_TEXT = 256;
const RELEASE_PAGE =
  /^https:\/\/github\.com\/GyanD\/codexffmpeg\/releases\/tag\/[A-Za-z0-9._-]{1,100}$/;

export class MediaRuntimeDecodeError extends Error {
  constructor() {
    super("media_runtime_contract_invalid");
    this.name = "MediaRuntimeDecodeError";
  }
}

function invalid(): never {
  throw new MediaRuntimeDecodeError();
}

function record(value: unknown, keys: readonly string[]) {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    invalid();
  const found = Object.keys(value);
  if (
    found.length !== keys.length ||
    !keys.every((key) => Object.prototype.hasOwnProperty.call(value, key))
  )
    invalid();
  return value as Record<string, unknown>;
}

function member<T extends string>(value: unknown, allowed: readonly T[]): T {
  if (
    typeof value !== "string" ||
    !(allowed as readonly string[]).includes(value)
  )
    invalid();
  return value as T;
}

function text(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_TEXT
  )
    invalid();
  return value;
}

function count(value: unknown): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < 0 ||
    (value as number) > MAX_BYTES
  )
    invalid();
  return value as number;
}

export function decodeMediaRuntimeStatus(value: unknown): MediaRuntimeStatus {
  const wire = record(value, [
    "schema",
    "resolution",
    "config",
    "manual",
    "features",
    "recovery",
    "actions",
  ]);
  if (wire.schema !== MEDIA_RUNTIME_STATUS_SCHEMA) invalid();
  const resolution = record(wire.resolution, [
    "schema",
    "state",
    "reason",
    "source_kind",
  ]);
  if (resolution.schema !== RESOLUTION_SCHEMA) invalid();
  let config: MediaRuntimeStatus["config"] = null;
  if (wire.config !== null) {
    const raw = record(wire.config, ["revision", "selection"]);
    config = Object.freeze({
      revision: count(raw.revision),
      selection: member(raw.selection, MEDIA_RUNTIME_CONFIG_SELECTIONS),
    });
  }
  const manual = record(wire.manual, [
    "archive_name",
    "archive_sha256",
    "source_label",
    "release_page",
    "license",
    "approximate_bytes",
  ]);
  const releasePage = text(manual.release_page);
  if (!RELEASE_PAGE.test(releasePage)) invalid();
  const archiveName = text(manual.archive_name);
  const archiveSha256 = text(manual.archive_sha256);
  if (
    !/^ffmpeg-[A-Za-z0-9._-]{1,100}-full_build\.zip$/.test(archiveName) ||
    !/^[0-9a-f]{64}$/.test(archiveSha256)
  )
    invalid();
  const rawFeatures = record(wire.features, MEDIA_RUNTIME_FEATURES);
  const features = {} as Record<
    MediaRuntimeFeature,
    MediaRuntimeFeatureReadiness
  >;
  for (const feature of MEDIA_RUNTIME_FEATURES) {
    const raw = record(rawFeatures[feature], ["state", "reason"]);
    const state = member(raw.state, MEDIA_RUNTIME_FEATURE_STATES);
    const reason =
      raw.reason === null
        ? null
        : member(raw.reason, MEDIA_RUNTIME_FEATURE_REASONS);
    // Only `ready` carries no reason; a blocked feature always names why.
    if ((state === "ready") !== (reason === null)) invalid();
    features[feature] = Object.freeze({ state, reason });
  }
  let recovery: MediaRuntimeStatus["recovery"] = null;
  if (wire.recovery !== null) {
    const raw = record(wire.recovery, ["state", "reclaimable"]);
    if (typeof raw.reclaimable !== "boolean") invalid();
    recovery = Object.freeze({
      state: member(raw.state, MEDIA_RUNTIME_RECOVERY_STATES),
      reclaimable: raw.reclaimable,
    });
  }
  if (
    !Array.isArray(wire.actions) ||
    wire.actions.length > MEDIA_RUNTIME_ACTIONS.length
  )
    invalid();
  const actions = wire.actions.map((action) =>
    member(action, MEDIA_RUNTIME_ACTIONS),
  );
  if (new Set(actions).size !== actions.length) invalid();
  return Object.freeze({
    schema: MEDIA_RUNTIME_STATUS_SCHEMA,
    resolution: Object.freeze({
      state: member(resolution.state, MEDIA_RUNTIME_RESOLUTION_STATES),
      reason: member(resolution.reason, MEDIA_RUNTIME_RESOLUTION_REASONS),
      sourceKind: member(resolution.source_kind, MEDIA_RUNTIME_SOURCE_KINDS),
    }),
    config,
    manual: Object.freeze({
      archiveName,
      archiveSha256,
      sourceLabel: text(manual.source_label),
      releasePage,
      license: text(manual.license),
      approximateBytes: count(manual.approximate_bytes),
    }),
    features: Object.freeze(features),
    recovery,
    actions: Object.freeze(actions),
  });
}

export function isMediaRuntimeRefusalCode(
  value: unknown,
): value is MediaRuntimeRefusalCode {
  return (
    typeof value === "string" &&
    (MEDIA_RUNTIME_REFUSAL_CODES as readonly string[]).includes(value)
  );
}
