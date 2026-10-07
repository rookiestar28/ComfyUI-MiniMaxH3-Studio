import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjectionV2,
  type AuthoringProjection,
  type TimelineHistoryProjectionV2,
} from "./authoringWorkbenchCodec";
import {
  decodeProductionWorkbenchProjection,
  type ProductionWorkbenchProjection,
} from "./productionWorkbenchCodec";
import type { SegmentationPolicy } from "./productionPlanningCodec";

export const MAX_PROJECT_FILE_BYTES = 2 * 1024 * 1024;
export const MAX_PROJECT_RESPONSE_BYTES = 3 * 1024 * 1024;
export const MAX_PROJECT_REQUEST_BYTES = 5 * 1024 * 1024;
export type ProjectOwner = Readonly<{
  production_handle: string;
  production_id: string;
  production_revision: number;
  production_fingerprint: string;
  authoring_handle: string;
  reference_revision: number;
  legacy_timeline_revision: number;
  workspace_revision: number;
  timeline_revision: number;
  authoring_fingerprint: string;
}>;
export type ProjectSnapshotOwner =
  | ProjectOwner
  | Readonly<{
      production_handle: string;
      production_id: string;
      production_revision: number;
      production_fingerprint: string;
      authoring_handle: null;
      reference_revision: null;
      legacy_timeline_revision: null;
      workspace_revision: null;
      timeline_revision: null;
      authoring_fingerprint: null;
    }>
  | Readonly<{
      production_handle: null;
      production_id: null;
      production_revision: null;
      production_fingerprint: null;
      authoring_handle: string;
      reference_revision: number;
      legacy_timeline_revision: number;
      workspace_revision: number;
      timeline_revision: number;
      authoring_fingerprint: string;
    }>;
export type ProjectPlanning = Readonly<{
  intent: string;
  script: string;
  target_seconds: number;
  policy: SegmentationPolicy;
  shots: readonly Readonly<{
    shot_id: string;
    ordinal: number;
    start_milliseconds: number;
    end_milliseconds: number;
    text: string;
    hard_boundary: boolean;
  }>[];
}>;
export type ProjectDocumentWire = Readonly<Record<string, unknown>>;
export type ProjectExport = Readonly<{
  owner: ProjectSnapshotOwner | null;
  document: ProjectDocumentWire;
}>;
export type ProjectOpened = Readonly<{
  owner: ProjectOwner;
  production: ProductionWorkbenchProjection | null;
  authoring: AuthoringProjection;
  history: TimelineHistoryProjectionV2;
  planning: ProjectPlanning;
  title: string;
  missingMedia: readonly string[];
}>;
export type ProjectRelinked = Omit<ProjectOpened, "planning" | "title">;
export function closedProjectObject(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype ||
    Object.keys(value).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(value, key))
  )
    throw new Error("malformed_response");
  return value as Record<string, unknown>;
}
const OWNER_KEYS = [
  "production_handle",
  "production_id",
  "production_revision",
  "production_fingerprint",
  "authoring_handle",
  "reference_revision",
  "legacy_timeline_revision",
  "workspace_revision",
  "timeline_revision",
  "authoring_fingerprint",
];
export function decodeSnapshotOwner(value: unknown): ProjectSnapshotOwner {
  const row = closedProjectObject(value, OWNER_KEYS);
  const partial = row.authoring_handle === null,
    standalone = row.production_handle === null;
  if (partial && standalone) throw new Error("malformed_response");
  for (const key of OWNER_KEYS) {
    const entry = row[key];
    if (
      (partial && !key.startsWith("production_")) ||
      (standalone && key.startsWith("production_"))
    ) {
      if (entry !== null) throw new Error("malformed_response");
      continue;
    }
    if (key.endsWith("revision")) {
      if (!Number.isSafeInteger(entry) || Number(entry) < 1)
        throw new Error("malformed_response");
    } else if (
      typeof entry !== "string" ||
      entry.length < 1 ||
      entry.length > 128 ||
      (key.endsWith("fingerprint") && !/^sha256:[a-f0-9]{64}$/.test(entry))
    )
      throw new Error("malformed_response");
  }
  return Object.freeze(row) as ProjectSnapshotOwner;
}
export function decodeProjectOwner(value: unknown): ProjectOwner {
  const owner = decodeSnapshotOwner(value);
  if (owner.authoring_handle === null || owner.production_handle === null)
    throw new Error("malformed_response");
  return owner;
}
export function decodeProjectPlanning(value: unknown): ProjectPlanning {
  const row = closedProjectObject(value, [
    "intent",
    "script",
    "target_seconds",
    "policy",
    "shots",
  ]);
  if (
    typeof row.intent !== "string" ||
    typeof row.script !== "string" ||
    row.intent.length > 65536 ||
    row.script.length > 65536 ||
    !Number.isSafeInteger(row.target_seconds) ||
    Number(row.target_seconds) < 4 ||
    Number(row.target_seconds) > 60 ||
    ![
      "auto_storyboard",
      "fixed_5",
      "fixed_10",
      "fixed_12",
      "fixed_15",
    ].includes(String(row.policy)) ||
    !Array.isArray(row.shots) ||
    row.shots.length > 32
  )
    throw new Error("malformed_response");
  for (const value of row.shots) {
    const shot = closedProjectObject(value, [
      "shot_id",
      "ordinal",
      "start_milliseconds",
      "end_milliseconds",
      "text",
      "hard_boundary",
    ]);
    if (
      typeof shot.shot_id !== "string" ||
      typeof shot.text !== "string" ||
      shot.text.length > 8192 ||
      typeof shot.hard_boundary !== "boolean" ||
      !Number.isSafeInteger(shot.ordinal) ||
      !Number.isSafeInteger(shot.start_milliseconds) ||
      !Number.isSafeInteger(shot.end_milliseconds)
    )
      throw new Error("malformed_response");
  }
  return row as ProjectPlanning;
}
export function projectFileBlob(document: ProjectDocumentWire): Blob {
  closedProjectObject(document, [
    "format",
    "schema_version",
    "title",
    "planning",
    "production",
    "editor",
    "media",
  ]);
  if (document.format !== "h3proj" || document.schema_version !== 1)
    throw new Error("document_version");
  const text = JSON.stringify(document);
  if (new TextEncoder().encode(text).byteLength > MAX_PROJECT_FILE_BYTES)
    throw new Error("document_size");
  return new Blob([text], { type: "application/json;charset=utf-8" });
}
function missing(value: unknown): readonly string[] {
  if (
    !Array.isArray(value) ||
    value.length > 128 ||
    value.some((id) => typeof id !== "string" || id.length > 128) ||
    new Set(value).size !== value.length
  )
    throw new Error("malformed_response");
  return value;
}
export function decodeProjectReply(
  value: unknown,
  intent: "export" | "open" | "relink",
) {
  if (intent === "export") {
    const row = closedProjectObject(value, ["schema", "owner", "document"]);
    if (row.schema !== "h3.context.project_document.export.v1")
      throw new Error("malformed_response");
    projectFileBlob(row.document as ProjectDocumentWire);
    return {
      owner: row.owner === null ? null : decodeSnapshotOwner(row.owner),
      document: row.document as ProjectDocumentWire,
    };
  }
  const row = closedProjectObject(
    value,
    intent === "open"
      ? [
          "schema",
          "owner",
          "production",
          "authoring",
          "history",
          "planning",
          "title",
          "missing_media",
        ]
      : [
          "schema",
          "owner",
          "production",
          "authoring",
          "history",
          "missing_media",
        ],
  );
  if (
    row.schema !==
    `h3.context.project_document.${intent === "open" ? "response" : "relink"}.v1`
  )
    throw new Error("malformed_response");
  const owner = decodeProjectOwner(row.owner),
    authoring = decodeAuthoringProjection(row.authoring),
    history = decodeTimelineHistoryProjectionV2(row.history);
  if (
    authoring.workspaceHandle !== owner.authoring_handle ||
    history.workspaceHandle !== owner.authoring_handle ||
    history.authoring.projectId !== owner.production_id ||
    history.authoring.workspaceRevision !== owner.workspace_revision ||
    history.authoring.timelineRevision !== owner.timeline_revision ||
    history.authoring.authoringFingerprint !== owner.authoring_fingerprint ||
    authoring.reference.revision !== owner.reference_revision ||
    authoring.timeline.revision !== owner.legacy_timeline_revision
  )
    throw new Error("cross_workspace_response");
  const result = {
    owner,
    authoring,
    history,
    missingMedia: missing(row.missing_media),
  };
  const production =
    row.production === null
      ? null
      : decodeProductionWorkbenchProjection(row.production);
  if (
    production !== null &&
    (production.workspaceHandle !== owner.production_handle ||
      production.workspaceId !== owner.production_id ||
      production.workspaceRevision !== owner.production_revision ||
      production.workspaceFingerprint !== owner.production_fingerprint)
  )
    throw new Error("cross_workspace_response");
  if (intent === "relink") return { ...result, production };
  if (typeof row.title !== "string" || row.title.length > 256)
    throw new Error("malformed_response");
  return {
    ...result,
    production,
    planning: decodeProjectPlanning(row.planning),
    title: row.title,
  };
}
