import {
  closedProjectObject,
  decodeSnapshotOwner,
  type ProjectSnapshotOwner,
} from "./projectDocumentCodec";

export type RecoveryRecord = Readonly<{
  project_id: string;
  revision: number;
  saved_at_ms: number;
  closed_at_ms: number | null;
  active: boolean;
}>;
export type RecoveryCurrent = Readonly<{
  project_id: string;
  generation: number;
  saved_generation: number;
  saved_revision: number | null;
  saved_at_ms: number | null;
  saved_owner: ProjectSnapshotOwner | null;
  state: "dirty" | "saving" | "saved" | "error";
  reason: string | null;
}>;
export type RecoveryStatus = Readonly<{
  schema: "h3.context.project_recovery.status.v1";
  supported: true;
  enabled: boolean;
  include_video: boolean;
  revision: number;
  charged_bytes: number;
  records: readonly RecoveryRecord[];
  current: RecoveryCurrent | null;
  writer_active: boolean;
}>;
function integer(value: unknown): asserts value is number {
  if (!Number.isSafeInteger(value) || (value as number) < 0)
    throw new Error("malformed_response");
}
export function recoveryProject(value: unknown): asserts value is string {
  if (typeof value !== "string" || !/^project_[0-9a-f]{32}$/.test(value))
    throw new Error("malformed_response");
}
export function decodeRecoveryStatus(value: unknown): RecoveryStatus {
  const row = closedProjectObject(value, [
    "schema",
    "supported",
    "enabled",
    "include_video",
    "revision",
    "charged_bytes",
    "records",
    "current",
    "writer_active",
  ]);
  if (
    row.schema !== "h3.context.project_recovery.status.v1" ||
    row.supported !== true ||
    [row.enabled, row.include_video, row.writer_active].some(
      (value) => typeof value !== "boolean",
    ) ||
    !Array.isArray(row.records) ||
    row.records.length > 16
  )
    throw new Error("malformed_response");
  integer(row.revision);
  integer(row.charged_bytes);
  if (row.charged_bytes > 640 * 1024 * 1024)
    throw new Error("malformed_response");
  const records = row.records.map((value) => {
    const item = closedProjectObject(value, [
      "project_id",
      "revision",
      "saved_at_ms",
      "closed_at_ms",
      "active",
    ]);
    recoveryProject(item.project_id);
    integer(item.revision);
    integer(item.saved_at_ms);
    if (item.closed_at_ms !== null) integer(item.closed_at_ms);
    if (typeof item.active !== "boolean") throw new Error("malformed_response");
    return item as RecoveryRecord;
  });
  if (new Set(records.map((item) => item.project_id)).size !== records.length)
    throw new Error("malformed_response");
  let current: RecoveryCurrent | null = null;
  if (row.current !== null) {
    const item = closedProjectObject(row.current, [
      "project_id",
      "generation",
      "saved_generation",
      "saved_revision",
      "saved_at_ms",
      "saved_owner",
      "state",
      "reason",
    ]);
    recoveryProject(item.project_id);
    integer(item.generation);
    integer(item.saved_generation);
    if (
      item.saved_generation > item.generation ||
      !["dirty", "saving", "saved", "error"].includes(item.state as string) ||
      (item.reason !== null &&
        (typeof item.reason !== "string" ||
          !/^[a-z_]{1,64}$/.test(item.reason)))
    )
      throw new Error("malformed_response");
    if (item.saved_revision !== null) integer(item.saved_revision);
    if (item.saved_at_ms !== null) integer(item.saved_at_ms);
    if (
      item.state === "saved" &&
      (item.saved_generation !== item.generation ||
        item.saved_revision === null ||
        item.reason !== null)
    )
      throw new Error("malformed_response");
    current = {
      ...item,
      saved_owner:
        item.saved_owner === null
          ? null
          : decodeSnapshotOwner(item.saved_owner),
    } as RecoveryCurrent;
  }
  return {
    schema: "h3.context.project_recovery.status.v1",
    supported: true,
    enabled: row.enabled as boolean,
    include_video: row.include_video as boolean,
    revision: row.revision,
    charged_bytes: row.charged_bytes,
    records,
    current,
    writer_active: row.writer_active as boolean,
  };
}
