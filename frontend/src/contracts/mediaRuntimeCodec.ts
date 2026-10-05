// M25-33: the fail-closed decoder for the media runtime status (v3) and setup job (v1) wires.
//
// Every key set is exact and every enumerated value is closed. An unknown value is a typed decode
// failure, which the surface shows as "status unavailable" and never as ready: a newer server
// vocabulary must not be read as a capability this client cannot describe. The closed lists below
// are pinned against the server's own vocabularies by `tests/fixtures/media_runtime_wire_v3.json`,
// which the backend suite regenerates from the service and this suite decodes.

export const MEDIA_RUNTIME_STATUS_SCHEMA =
  "h3.context.media_runtime_status.v3" as const;
export const MEDIA_RUNTIME_JOB_SCHEMA =
  "h3.context.media_runtime_setup_job.v1" as const;
export const MEDIA_RUNTIME_REQUEST_SCHEMA =
  "h3.context.media_runtime_setup_request.v1" as const;
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
  "install_supported",
  "rescan",
  "use_local_directory",
  "restore_auto",
  "cancel_setup",
  "reclaim_parked_runtime",
] as const;
export const MEDIA_RUNTIME_JOB_STATES = [
  "running",
  "succeeded",
  "failed",
  "cancelled",
] as const;
export const MEDIA_RUNTIME_JOB_PHASES = [
  "checking",
  "downloading",
  "extracting",
  "verifying",
  "publishing",
  "activating",
  "done",
] as const;
export const MEDIA_RUNTIME_JOB_REASONS = [
  ...MEDIA_RUNTIME_RESOLUTION_REASONS,
  "advanced_override_active",
  "already_available",
  "archive_invalid",
  "cancelled",
  "config_conflict",
  "config_invalid",
  "config_write_failed",
  "digest_mismatch",
  "download_failed",
  "download_timeout",
  "egress_refused",
  "installed",
  "insufficient_space",
  "internal_failure",
  "invalid_request",
  "local_selection_active",
  "media_runtime_busy",
  "network_timeout",
  "network_unavailable",
  "permission_denied",
  "publication_failed",
  "reclaim_unsafe",
  "redirect_refused",
  "runtime_busy",
  "selection_invalid_path",
  "selection_unsupported_pair",
  "setup_busy",
  "size_mismatch",
  "source_unavailable",
  "tls_failed",
  "verification_failed",
  "write_failed",
] as const;
export const MEDIA_RUNTIME_RECOVERY_STATES = ["parked_runtime"] as const;
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
  "setup_job_not_found",
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
export type MediaRuntimeJobState = (typeof MEDIA_RUNTIME_JOB_STATES)[number];
export type MediaRuntimeJobPhase = (typeof MEDIA_RUNTIME_JOB_PHASES)[number];
export type MediaRuntimeJobReason = (typeof MEDIA_RUNTIME_JOB_REASONS)[number];
export type MediaRuntimeRefusalCode =
  (typeof MEDIA_RUNTIME_REFUSAL_CODES)[number];

export type MediaRuntimeJob = Readonly<{
  schema: typeof MEDIA_RUNTIME_JOB_SCHEMA;
  jobId: string;
  state: MediaRuntimeJobState;
  phase: MediaRuntimeJobPhase;
  reason: MediaRuntimeJobReason | null;
  completedBytes: number;
  totalBytes: number;
}>;

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
  setup: MediaRuntimeJob | null;
  install: Readonly<{
    profile: string;
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
const JOB_ID = /^[0-9a-f]{32}$/;
const RELEASE_PAGE = /^https:\/\/github\.com\/[A-Za-z0-9._/-]{1,200}$/;

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

export function decodeMediaRuntimeJob(value: unknown): MediaRuntimeJob {
  const wire = record(value, [
    "schema",
    "job_id",
    "state",
    "phase",
    "reason",
    "progress",
  ]);
  if (wire.schema !== MEDIA_RUNTIME_JOB_SCHEMA) invalid();
  if (typeof wire.job_id !== "string" || !JOB_ID.test(wire.job_id)) invalid();
  const state = member(wire.state, MEDIA_RUNTIME_JOB_STATES);
  const phase = member(wire.phase, MEDIA_RUNTIME_JOB_PHASES);
  const reason =
    wire.reason === null
      ? null
      : member(wire.reason, MEDIA_RUNTIME_JOB_REASONS);
  // A running job has no terminal reason yet, and a terminal job always names one.
  if ((state === "running") !== (reason === null)) invalid();
  const progress = record(wire.progress, ["completed_bytes", "total_bytes"]);
  const completedBytes = count(progress.completed_bytes);
  const totalBytes = count(progress.total_bytes);
  if (totalBytes > 0 && completedBytes > totalBytes) invalid();
  return Object.freeze({
    schema: MEDIA_RUNTIME_JOB_SCHEMA,
    jobId: wire.job_id,
    state,
    phase,
    reason,
    completedBytes,
    totalBytes,
  });
}

export function decodeMediaRuntimeStatus(value: unknown): MediaRuntimeStatus {
  const wire = record(value, [
    "schema",
    "resolution",
    "config",
    "setup",
    "install",
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
  const install = record(wire.install, [
    "profile",
    "source_label",
    "release_page",
    "license",
    "approximate_bytes",
  ]);
  const releasePage = text(install.release_page);
  if (!RELEASE_PAGE.test(releasePage)) invalid();
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
    setup: wire.setup === null ? null : decodeMediaRuntimeJob(wire.setup),
    install: Object.freeze({
      profile: text(install.profile),
      sourceLabel: text(install.source_label),
      releasePage,
      license: text(install.license),
      approximateBytes: count(install.approximate_bytes),
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
