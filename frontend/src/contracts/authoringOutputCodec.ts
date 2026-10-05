import { OUTPUT_PROFILE_ID } from "./compositionCodec";

export const OUTPUT_PHASES = [
  "queued",
  "probing",
  "preparing",
  "rendering",
  "encoding",
  "muxing",
  "validating",
  "succeeded",
  "failed",
  "cancelled",
] as const;
export const OUTPUT_FAILURES = [
  "cancelled",
  "workspace_released",
  "source_replaced",
  "source_expired",
  "source_unavailable",
  "font_changed",
  "deadline",
  "resource_limit",
  "process_failed",
  "output_invalid",
  "receipt_invalid",
  "store_unavailable",
  "service_closed",
  "runtime_unavailable",
] as const;
export type OutputPhase = (typeof OUTPUT_PHASES)[number];
export type OutputFailure = (typeof OUTPUT_FAILURES)[number];
export const OUTPUT_ERROR_STATUS = Object.freeze({
  invalid_request: 400,
  forbidden: 403,
  unavailable: 404,
  expired: 410,
  revision_conflict: 409,
  idempotency_conflict: 409,
  not_ready: 409,
  resource_limit: 429,
  runtime_unavailable: 503,
  preview_unavailable: 503,
  internal_error: 500,
});
export type OutputErrorCode = keyof typeof OUTPUT_ERROR_STATUS;
export const OUTPUT_CAPABILITY = Object.freeze({
  schema: "h3.authoring.output_capability.v1",
  supported: false as boolean,
  output_profile_id: OUTPUT_PROFILE_ID,
  preview_profile_id: "authoring.final_preview.h264_aac_640x360_24fps.v1",
  job_path: "/h3-context/v1/authoring/render",
  output_path: "/h3-context/v1/authoring/output",
  max_output_bytes: 536870912,
  max_preview_bytes: 16777216,
  max_duration_frames: 3600,
  frame_rate_num: 24,
  frame_rate_den: 1,
  job_ttl_seconds: 3600,
  max_jobs: 32,
  max_outputs: 16,
  max_byte_responses: 1,
  max_http_requests: 4,
  max_response_seconds: 120,
  max_write_seconds: 10,
  preview_threads: 2,
  preview_memory_bytes: 268435456,
  preview_seconds: 60,
} as const);
export type OutputCapability = typeof OUTPUT_CAPABILITY;
export type OutputBinding = Readonly<{
  workspace_handle: string;
  workspace_revision: number;
  timeline_revision: number;
  snapshot_fingerprint: string;
}>;
export type OutputCreate = OutputBinding &
  Readonly<{
    schema: "h3.authoring.output_create.v1";
    output_profile_id: typeof OUTPUT_PROFILE_ID;
    idempotency_key: string;
  }>;
export type OutputSummary = Readonly<{
  output_fingerprint: string;
  byte_length: number;
  width: number;
  height: number;
  frame_count: number;
  frame_rate_num: 24;
  frame_rate_den: 1;
  audio_streams: 0 | 1;
  output_profile_id: typeof OUTPUT_PROFILE_ID;
  verified: true;
}>;
export type OutputStatus = OutputBinding &
  Readonly<{
    schema: "h3.authoring.output_status.v1";
    job_handle: string;
    output_handle: string | null;
    state_version: number;
    phase: OutputPhase;
    progress_bp: number;
    failure: OutputFailure | null;
    currency: "current" | "old_revision";
    availability: "available" | "gone" | "expired";
    output: OutputSummary | null;
  }>;

function invalid(): never {
  throw new Error("invalid_output_contract");
}
function object(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  )
    invalid();
  const actual = Object.keys(value);
  if (
    actual.length !== keys.length ||
    actual.some((key) => !keys.includes(key))
  )
    invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, pattern: RegExp): string {
  if (typeof value !== "string" || !pattern.test(value)) invalid();
  return value;
}
function integer(value: unknown, max: number, min = 0): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < min ||
    value > max
  )
    invalid();
  return value;
}
function choice<T extends string>(value: unknown, values: readonly T[]): T {
  if (typeof value !== "string" || !values.includes(value as T)) invalid();
  return value as T;
}
// SECURITY: JS $ also matches before a final newline; use an absolute end assertion.
export function outputWorkspace(value: unknown): string {
  return text(value, /^authoring-[0-9a-f]{32}(?![\s\S])/);
}
export function outputHandle(value: unknown, kind: "job" | "output"): string {
  return text(
    value,
    kind === "job"
      ? /^arj_[A-Za-z0-9_-]{22}(?![\s\S])/
      : /^aro_[A-Za-z0-9_-]{22}(?![\s\S])/,
  );
}
const fingerprint = (value: unknown) =>
  text(value, /^sha256:[0-9a-f]{64}(?![\s\S])/);
const bindingKeys = [
  "workspace_handle",
  "workspace_revision",
  "timeline_revision",
  "snapshot_fingerprint",
];
function binding(wire: Record<string, unknown>): OutputBinding {
  return {
    workspace_handle: outputWorkspace(wire.workspace_handle),
    workspace_revision: integer(wire.workspace_revision, 1000000),
    timeline_revision: integer(wire.timeline_revision, 1000000),
    snapshot_fingerprint: fingerprint(wire.snapshot_fingerprint),
  };
}
export function decodeOutputCreate(value: unknown): OutputCreate {
  const wire = object(value, [
    "schema",
    ...bindingKeys,
    "output_profile_id",
    "idempotency_key",
  ]);
  if (
    wire.schema !== "h3.authoring.output_create.v1" ||
    wire.output_profile_id !== OUTPUT_PROFILE_ID
  )
    invalid();
  return Object.freeze({
    schema: "h3.authoring.output_create.v1",
    ...binding(wire),
    output_profile_id: OUTPUT_PROFILE_ID,
    idempotency_key: text(
      wire.idempotency_key,
      /^[A-Za-z0-9][A-Za-z0-9_-]{15,127}(?![\s\S])/,
    ),
  });
}
export function decodeOutputCapability(value: unknown): OutputCapability {
  const wire = object(value, Object.keys(OUTPUT_CAPABILITY));
  if (typeof wire.supported !== "boolean") invalid();
  for (const key of Object.keys(
    OUTPUT_CAPABILITY,
  ) as (keyof OutputCapability)[]) {
    if (key !== "supported" && wire[key] !== OUTPUT_CAPABILITY[key]) invalid();
  }
  return Object.freeze({ ...OUTPUT_CAPABILITY, supported: wire.supported });
}
function summary(value: unknown): OutputSummary {
  const wire = object(value, [
    "output_fingerprint",
    "byte_length",
    "width",
    "height",
    "frame_count",
    "frame_rate_num",
    "frame_rate_den",
    "audio_streams",
    "output_profile_id",
    "verified",
  ]);
  if (
    wire.frame_rate_num !== 24 ||
    wire.frame_rate_den !== 1 ||
    wire.output_profile_id !== OUTPUT_PROFILE_ID ||
    wire.verified !== true ||
    (wire.audio_streams !== 0 && wire.audio_streams !== 1)
  )
    invalid();
  const width = integer(wire.width, 1920, 2),
    height = integer(wire.height, 1080, 2);
  if (width % 2 || height % 2) invalid();
  return Object.freeze({
    output_fingerprint: fingerprint(wire.output_fingerprint),
    byte_length: integer(
      wire.byte_length,
      OUTPUT_CAPABILITY.max_output_bytes,
      1,
    ),
    width,
    height,
    frame_count: integer(wire.frame_count, 3600, 1),
    frame_rate_num: 24,
    frame_rate_den: 1,
    audio_streams: wire.audio_streams,
    output_profile_id: OUTPUT_PROFILE_ID,
    verified: true,
  });
}
export function decodeOutputStatus(value: unknown): OutputStatus {
  const wire = object(value, [
    "schema",
    ...bindingKeys,
    "job_handle",
    "output_handle",
    "state_version",
    "phase",
    "progress_bp",
    "failure",
    "currency",
    "availability",
    "output",
  ]);
  if (wire.schema !== "h3.authoring.output_status.v1") invalid();
  const phase = choice(wire.phase, OUTPUT_PHASES);
  const failure =
    wire.failure === null ? null : choice(wire.failure, OUTPUT_FAILURES);
  const progress = integer(wire.progress_bp, 10000);
  const output = wire.output === null ? null : summary(wire.output);
  const handle =
    wire.output_handle === null
      ? null
      : outputHandle(wire.output_handle, "output");
  const availability = choice(wire.availability, [
    "available",
    "gone",
    "expired",
  ]);
  if (phase === "succeeded") {
    if (failure !== null || progress !== 10000) invalid();
  } else {
    if (output !== null || handle !== null || availability === "available")
      invalid();
    if (phase === "failed") {
      if (failure === null || failure === "cancelled") invalid();
    } else if (phase === "cancelled") {
      if (failure !== "cancelled") invalid();
    } else if (failure !== null || progress === 10000) invalid();
  }
  // IMPORTANT: retained success may become gone/expired without erasing its summary.
  // Only available success confers a byte action; historical metadata cannot resurrect it.
  if (
    (handle === null) !== (output === null) ||
    (availability === "available" && output === null)
  )
    invalid();
  return Object.freeze({
    schema: "h3.authoring.output_status.v1",
    ...binding(wire),
    job_handle: outputHandle(wire.job_handle, "job"),
    output_handle: handle,
    state_version: integer(wire.state_version, 1000000),
    phase,
    progress_bp: progress,
    failure,
    currency: choice(wire.currency, ["current", "old_revision"]),
    availability,
    output,
  });
}
export function decodeOutputError(
  value: unknown,
  status: number,
): Readonly<{ schema: "h3.authoring.output_error.v1"; code: OutputErrorCode }> {
  const wire = object(value, ["schema", "code"]);
  const code = choice(
    wire.code,
    Object.keys(OUTPUT_ERROR_STATUS) as OutputErrorCode[],
  );
  if (
    wire.schema !== "h3.authoring.output_error.v1" ||
    OUTPUT_ERROR_STATUS[code] !== status
  )
    invalid();
  return Object.freeze({ schema: "h3.authoring.output_error.v1", code });
}

export function parseOutputJson(source: string): unknown {
  if (
    typeof source !== "string" ||
    new TextEncoder().encode(source).byteLength > 8192
  )
    invalid();
  try {
    // SECURITY: reject duplicate decoded member names before JSON.parse can hide earlier values.
    // These tiny closed objects need at most two levels; four is a defensive parser ceiling.
    const scopes: (Set<string> | null)[] = [];
    const tokens = /"(?:[^"\\]|\\.)*"|[{}[\]]/g;
    for (const match of source.matchAll(tokens)) {
      const token = match[0];
      if (token === "{" || token === "[") {
        scopes.push(token === "{" ? new Set() : null);
        if (scopes.length > 4) invalid();
      } else if (token === "}" || token === "]") scopes.pop();
      else if (
        source
          .slice(match.index + token.length)
          .trimStart()
          .startsWith(":")
      ) {
        const keys = scopes.at(-1),
          key = JSON.parse(token) as string;
        if (!keys || keys.has(key)) invalid();
        keys.add(key);
      }
    }
    const value: unknown = JSON.parse(source, (_key, value: unknown) => {
      if (typeof value === "number" && !Number.isFinite(value)) invalid();
      return value;
    });
    if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
    return value;
  } catch {
    invalid();
  }
}
