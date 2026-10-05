import {
  executionCorrelationKeys as correlationKeys,
  generationSequenceProgressKeys as progressKeys,
  generationSequenceProjectionKeys as rootKeys,
  segmentDurationKeys as durationKeys,
} from "./generatedSurface";

export const GENERATION_SEQUENCE_PROJECTION_SCHEMA =
  "h3.context.generation_sequence_projection.v1" as const;

export type GenerationJobState =
  | "planned"
  | "projected"
  | "submitted"
  | "running"
  | "output_verification_failed"
  | "succeeded"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "unknown_ownership";
export type GenerationSequenceProgress = {
  job_id: string;
  segment_id: string;
  ordinal: number;
  state: GenerationJobState;
  attempt: number;
  transaction_id: string | null;
  queue_prompt_id: string | null;
  host_owner_id: string | null;
  artifact_receipt_fingerprint: string | null;
  artifact_output_fingerprint: string | null;
  failure_code: string | null;
};
/**
 * Which graph the command's two graph fingerprints were taken over.
 *
 * M17-20 D6. App Mode once materialized the context subgraph alone, so that was
 * the domain; it now materializes the full output-producing graph. The domain
 * travels with the command instead of being inferred, so a stale planner is a
 * named refusal rather than an identity mismatch pointing at the wrong thing.
 */
export type GenerationFingerprintDomain =
  "context_subgraph" | "output_producing_graph";

export type GenerationSequenceCommand = {
  job_id: string;
  segment_id: string;
  ordinal: number;
  task_mode: "t2va" | "i2va" | "fl2va" | "l2va" | "ref2va";
  source_id: string;
  reference_ids: string[];
  duration: {
    duration_milliseconds: number;
    frame_count: number;
    delivered_milliseconds: number;
    snapped: boolean;
  };
  dependency_segment_ids: string[];
  disposition: "dirty_self" | "dirty_upstream";
  reason_codes: string[];
  triggering_segment_ids: string[];
  manifest_fingerprint: string;
  producer_fingerprint: string;
  native_binding_fingerprint: string;
  settings_fingerprint: string;
  graph_fingerprint: string;
  compiled_prompt_fingerprint: string;
  model_fingerprint: string;
  runtime_fingerprint: string;
  expected_format: string;
  expected_shape: number[];
  timeout_ms: number;
  predecessor_receipt_fingerprint: string | null;
  predecessor_artifact_fingerprint: string | null;
  attempt: number;
  transaction_id: string;
  fingerprint_domain: GenerationFingerprintDomain;
};
export type GenerationSequenceProjection = {
  schema: typeof GENERATION_SEQUENCE_PROJECTION_SCHEMA;
  sequence_id: string;
  sequence_fingerprint: string;
  state_fingerprint: string;
  workspace_id: string;
  workspace_revision: number;
  workspace_fingerprint: string;
  max_concurrency: number;
  cancellation_requested: boolean;
  complete: boolean;
  correlation: { prompt_id: string; execution_node_id: string };
  progress: GenerationSequenceProgress[];
  eligible_commands: GenerationSequenceCommand[];
};

const commandKeys: readonly string[] = [
  "attempt",
  "compiled_prompt_fingerprint",
  "dependency_segment_ids",
  "disposition",
  "duration",
  "expected_format",
  "expected_shape",
  "fingerprint_domain",
  "graph_fingerprint",
  "job_id",
  "manifest_fingerprint",
  "model_fingerprint",
  "native_binding_fingerprint",
  "ordinal",
  "predecessor_artifact_fingerprint",
  "predecessor_receipt_fingerprint",
  "producer_fingerprint",
  "reason_codes",
  "reference_ids",
  "runtime_fingerprint",
  "segment_id",
  "settings_fingerprint",
  "source_id",
  "task_mode",
  "timeout_ms",
  "transaction_id",
  "triggering_segment_ids",
];
const commandFingerprintKeys = [
  "manifest_fingerprint",
  "producer_fingerprint",
  "native_binding_fingerprint",
  "settings_fingerprint",
  "graph_fingerprint",
  "compiled_prompt_fingerprint",
  "model_fingerprint",
  "runtime_fingerprint",
] as const;
const commandIdentifierListKeys = [
  "reference_ids",
  "dependency_segment_ids",
  "reason_codes",
  "triggering_segment_ids",
] as const;
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const fingerprintPattern = /^sha256:[0-9a-f]{64}$/;
const sensitive = /authorization|api.?key|password|secret|token/i;
const states: GenerationJobState[] = [
  "planned",
  "projected",
  "submitted",
  "running",
  "output_verification_failed",
  "succeeded",
  "failed",
  "timed_out",
  "cancelled",
  "unknown_ownership",
];
const taskModes = ["t2va", "i2va", "fl2va", "l2va", "ref2va"];
const dispositions = ["dirty_self", "dirty_upstream"];
const fingerprintDomains = ["context_subgraph", "output_producing_graph"];

function invalid(): never {
  throw Error();
}
function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    invalid();
  return value as Record<string, unknown>;
}
function closed(value: Record<string, unknown>, keys: readonly string[]) {
  if (Object.keys(value).sort().join() !== keys.join()) invalid();
}
function id(value: unknown): string {
  if (
    typeof value !== "string" ||
    !idPattern.test(value) ||
    sensitive.test(value)
  )
    invalid();
  return value;
}
function fp(value: unknown): string {
  if (typeof value !== "string" || !fingerprintPattern.test(value)) invalid();
  return value;
}
function pos(value: unknown, max: number): number {
  if (
    !Number.isInteger(value) ||
    (value as number) < 1 ||
    (value as number) > max
  )
    invalid();
  return value as number;
}
function nullable(value: unknown, check: typeof id | typeof fp) {
  return value === null ? null : check(value);
}
function ids(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 64) invalid();
  const result = value.map(id);
  if (new Set(result).size !== result.length) invalid();
  return result;
}

function decodeProgress(value: unknown): GenerationSequenceProgress {
  const wire = object(value);
  closed(wire, progressKeys);
  const state = wire.state as GenerationJobState;
  if (!states.includes(state)) invalid();
  id(wire.job_id);
  id(wire.segment_id);
  pos(wire.ordinal, 64);
  pos(wire.attempt, 8);
  for (const key of [
    "transaction_id",
    "queue_prompt_id",
    "host_owner_id",
    "failure_code",
  ])
    nullable(wire[key], id);
  for (const key of [
    "artifact_receipt_fingerprint",
    "artifact_output_fingerprint",
  ])
    nullable(wire[key], fp);
  return { ...wire, state } as GenerationSequenceProgress;
}

function decodeCommand(value: unknown): GenerationSequenceCommand {
  const wire = object(value);
  closed(wire, commandKeys);
  for (const key of [
    "job_id",
    "segment_id",
    "source_id",
    "expected_format",
    "transaction_id",
  ])
    id(wire[key]);
  for (const key of commandFingerprintKeys) fp(wire[key]);
  for (const key of commandIdentifierListKeys) ids(wire[key]);
  pos(wire.ordinal, 64);
  pos(wire.attempt, 8);
  pos(wire.timeout_ms, 86_400_000);
  if (!taskModes.includes(wire.task_mode as string)) invalid();
  if (!dispositions.includes(wire.disposition as string)) invalid();
  if (!fingerprintDomains.includes(wire.fingerprint_domain as string))
    invalid();
  const duration = object(wire.duration);
  closed(duration, durationKeys);
  // M17-25: duration is the authored value and the frame count is derived. The
  // basis discriminator is gone, so an unrepresentable length is unrepresentable
  // rather than rejected downstream. The lattice itself is not restated here; it
  // belongs to `core.length` alone.
  pos(duration.duration_milliseconds, 86_400_000);
  pos(duration.delivered_milliseconds, 86_400_000);
  pos(duration.frame_count, 3_600);
  if (typeof duration.snapped !== "boolean") invalid();
  if (
    duration.snapped !==
    (duration.duration_milliseconds !== duration.delivered_milliseconds)
  )
    invalid();
  if (
    !Array.isArray(wire.expected_shape) ||
    wire.expected_shape.length < 1 ||
    wire.expected_shape.length > 8
  )
    invalid();
  nullable(wire.predecessor_receipt_fingerprint, fp);
  nullable(wire.predecessor_artifact_fingerprint, fp);
  return wire as GenerationSequenceCommand;
}

export function decodeGenerationSequenceProjection(
  value: unknown,
): GenerationSequenceProjection {
  if (JSON.stringify(value).length > 1_048_576) invalid();
  const wire = object(value);
  closed(wire, rootKeys);
  if (wire.schema !== GENERATION_SEQUENCE_PROJECTION_SCHEMA) invalid();
  for (const key of ["sequence_id", "workspace_id"]) id(wire[key]);
  for (const key of [
    "sequence_fingerprint",
    "state_fingerprint",
    "workspace_fingerprint",
  ])
    fp(wire[key]);
  pos(wire.workspace_revision, 1_000_000);
  const maxConcurrency = pos(wire.max_concurrency, 4);
  if (
    typeof wire.cancellation_requested !== "boolean" ||
    typeof wire.complete !== "boolean"
  )
    invalid();
  const correlation = object(wire.correlation);
  closed(correlation, correlationKeys);
  id(correlation.prompt_id);
  id(correlation.execution_node_id);
  if (!Array.isArray(wire.progress) || wire.progress.length > 64) invalid();
  const progress = wire.progress.map(decodeProgress);
  if (new Set(progress.map((item) => item.job_id)).size !== progress.length)
    invalid();
  if (
    !Array.isArray(wire.eligible_commands) ||
    wire.eligible_commands.length > maxConcurrency
  )
    invalid();
  const eligible = wire.eligible_commands.map(decodeCommand);
  const byJob = new Map(progress.map((item) => [item.job_id, item]));
  if (
    new Set(eligible.map((item) => item.job_id)).size !== eligible.length ||
    eligible.some((command) => {
      const runtime = byJob.get(command.job_id);
      return (
        runtime === undefined ||
        runtime.state !== "planned" ||
        runtime.segment_id !== command.segment_id ||
        runtime.ordinal !== command.ordinal ||
        runtime.attempt !== command.attempt
      );
    }) ||
    ((wire.cancellation_requested || wire.complete) && eligible.length > 0) ||
    wire.complete !==
      progress.every((item) => ["succeeded", "cancelled"].includes(item.state))
  )
    invalid();
  return {
    ...(wire as Omit<
      GenerationSequenceProjection,
      "progress" | "eligible_commands"
    >),
    progress,
    eligible_commands: eligible,
  };
}
