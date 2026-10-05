import {
  PRODUCTION_ACCUMULATION_ACTION_VERSION,
  decodeProductionWorkbenchProjection,
  type ProductionWorkbenchProjection,
} from "./productionWorkbenchCodec";
import { sha256Text } from "./canonicalFingerprint";

export { PRODUCTION_ACCUMULATION_ACTION_VERSION };
export const PRODUCTION_ACCUMULATED_PROJECT_SCHEMA =
  "h3.context.production_accumulated_project.v1" as const;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const workspaceHandle = /^pw_[A-Za-z0-9_-]{32,96}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const statuses = new Set([
  "admitted",
  "planned",
  "projected",
  "submitted",
  "running",
  "executing",
  "verification_pending",
  "output_verification_failed",
  "succeeded",
  "failed",
  "timed_out",
  "cancelled",
  "unknown_ownership",
]);
const recoveries = new Set([null, "start", "retry", "verify_output"]);

function record(value: unknown, keys: readonly string[], label: string) {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value as object)
      .sort()
      .join() !== [...keys].sort().join()
  )
    throw new Error(`${label} is not a closed object`);
  return value as Record<string, unknown>;
}

function text(value: unknown, pattern: RegExp, label: string): string {
  if (typeof value !== "string" || !pattern.test(value))
    throw new Error(`${label} is invalid`);
  return value;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function productionAccumulatedProjectFingerprint(
  wireWithoutFingerprint: Record<string, unknown>,
): string {
  return sha256Text(canonicalJson(wireWithoutFingerprint));
}

export type ProductionAccumulationAttempt = Readonly<{
  candidateId: string;
  attemptId: string;
  memberSegmentId: string;
  status: string;
  recovery: "start" | "retry" | "verify_output" | null;
  committed: boolean;
}>;

export type ProductionAccumulatedProject = Readonly<{
  schema: typeof PRODUCTION_ACCUMULATED_PROJECT_SCHEMA;
  workspaceHandle: string;
  workspaceId: string;
  projectRevision: number;
  projectFingerprint: string;
  workspace: ProductionWorkbenchProjection | null;
  attempts: readonly ProductionAccumulationAttempt[];
}>;

export function decodeProductionAccumulatedProject(
  value: unknown,
): ProductionAccumulatedProject {
  const wire = record(
    value,
    [
      "schema",
      "workspace_handle",
      "workspace_id",
      "project_revision",
      "project_fingerprint",
      "workspace",
      "attempts",
      "capabilities",
    ],
    "accumulated project",
  );
  if (wire.schema !== PRODUCTION_ACCUMULATED_PROJECT_SCHEMA)
    throw new Error("accumulated project schema is unsupported");
  if (
    !Number.isInteger(wire.project_revision) ||
    (wire.project_revision as number) < 1 ||
    (wire.project_revision as number) > 1_000_000
  )
    throw new Error("accumulated project revision is invalid");
  if (
    !Array.isArray(wire.capabilities) ||
    wire.capabilities.join() !== "read,admit_generation,release_generation"
  )
    throw new Error("accumulated project capabilities are invalid");
  if (!Array.isArray(wire.attempts) || wire.attempts.length > 2)
    throw new Error("accumulated project attempt limit is invalid");
  const attempts = wire.attempts.map((value) => {
    const attempt = record(
      value,
      [
        "candidate_id",
        "attempt_id",
        "member_segment_id",
        "status",
        "recovery",
        "committed",
      ],
      "accumulation attempt",
    );
    if (
      typeof attempt.status !== "string" ||
      !statuses.has(attempt.status) ||
      !recoveries.has(attempt.recovery as string | null) ||
      typeof attempt.committed !== "boolean" ||
      (attempt.committed && attempt.status !== "succeeded")
    )
      throw new Error("accumulation attempt state is invalid");
    return Object.freeze({
      candidateId: text(attempt.candidate_id, identifier, "candidate id"),
      attemptId: text(attempt.attempt_id, identifier, "attempt id"),
      memberSegmentId: text(
        attempt.member_segment_id,
        identifier,
        "member segment id",
      ),
      status: attempt.status,
      recovery: attempt.recovery as ProductionAccumulationAttempt["recovery"],
      committed: attempt.committed,
    });
  });
  if (
    new Set(attempts.map((attempt) => attempt.attemptId)).size !==
    attempts.length
  )
    throw new Error("duplicate accumulation attempt id");
  const workspace =
    wire.workspace === null
      ? null
      : decodeProductionWorkbenchProjection(wire.workspace);
  const workspaceHandleValue = text(
    wire.workspace_handle,
    workspaceHandle,
    "workspace handle",
  );
  const workspaceId = text(wire.workspace_id, identifier, "workspace id");
  if (
    workspace !== null &&
    (workspace.workspaceHandle !== workspaceHandleValue ||
      workspace.workspaceId !== workspaceId)
  )
    throw new Error("accumulated workspace identity mismatch");
  const projectFingerprint = text(
    wire.project_fingerprint,
    fingerprint,
    "project fingerprint",
  );
  const { project_fingerprint: _ignored, ...material } = wire;
  if (productionAccumulatedProjectFingerprint(material) !== projectFingerprint)
    throw new Error("accumulated project fingerprint mismatch");
  return Object.freeze({
    schema: PRODUCTION_ACCUMULATED_PROJECT_SCHEMA,
    workspaceHandle: workspaceHandleValue,
    workspaceId,
    projectRevision: wire.project_revision as number,
    projectFingerprint,
    workspace,
    attempts: Object.freeze(attempts),
  });
}
