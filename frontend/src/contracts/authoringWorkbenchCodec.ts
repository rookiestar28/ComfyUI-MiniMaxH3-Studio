// M20-03: the authoring-workbench wire codec. The backend domains (M20-00 reference set,
// M20-02 timeline) stay canonical: this codec validates structure and internal consistency,
// and never recomputes a label, pairing, capacity or geometry rule — restating any of them
// here would recreate the second authority the chain exists to prevent.

import {
  AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
  decodeAuthoringPreviewCapability,
  type AuthoringPreviewCapability,
} from "./authoringPreviewCodec";
import {
  BLOCKER_CODES,
  CLIP_AUDIO_FADE_MAX_FRAMES,
  CLIP_AUDIO_GAIN_MAX_MB,
  CLIP_AUDIO_GAIN_MIN_MB,
  compositionClipWireKeys,
  compositionContractFingerprint,
  decodeClipAudio,
  decodeCompositionAudioExtension,
  decodeCompositionBlocker,
  decodeCompositionClip,
  decodeCompositionTrack,
  decodePublicCompositionAsset,
  decodePublicCompositionSnapshot,
  INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
  NLE_OPERATION_IDS,
  type CompositionClip,
  type CompositionTrack,
  type PublicCompositionAsset,
  type PublicCompositionSnapshot,
} from "./compositionCodec";
import {
  cropKeys,
  effectKeys,
  textStyleKeys,
  transform2DKeys,
  transitionKeys,
} from "./generatedSurface";

export const AUTHORING_ACTION_SCHEMA =
  "h3.context.authoring_workbench.action.v1" as const;
export const AUTHORING_PROJECTION_SCHEMA =
  "h3.context.authoring_workbench.projection.v1" as const;
export const AUTHORING_SNAP_SCHEMA =
  "h3.context.authoring_workbench.snap.v1" as const;
export const TIMELINE_TRANSACTION_SCHEMA =
  "h3.context.timeline_transaction.v1" as const;
export const TIMELINE_RECEIPT_SCHEMA =
  "h3.context.timeline_receipt.v1" as const;
export const TIMELINE_HISTORY_PROJECTION_SCHEMA =
  "h3.context.timeline_history_projection.v1" as const;
export const NLE_AUTHORING_SCHEMA =
  "h3.context.nle_authoring_state.v1" as const;
export const NLE_AUTHORING_PROFILE_ID =
  "h3.authoring.nle_content_extent.v1" as const;
export const NLE_OPERATION_PROFILE_ID_V2 =
  "h3.authoring.nle_operation.v2" as const;
export const TIMELINE_TRANSACTION_SCHEMA_V2 =
  "h3.context.timeline_transaction.v2" as const;
export const TIMELINE_RECEIPT_SCHEMA_V2 =
  "h3.context.timeline_receipt.v2" as const;
export const TIMELINE_HISTORY_PROJECTION_SCHEMA_V2 =
  "h3.context.timeline_history_projection.v2" as const;
// The one frozen availability producer identity (M20-00 metadata); the backend
// refuses any other value at its wire, and this constant is the only place the
// frontend spells it.
export const AUTHORING_AVAILABILITY_PRODUCER =
  "frontend.host.graphReferenceQualification" as const;

const NOT_BOUND_PREVIEW = decodeAuthoringPreviewCapability({
  schema: AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
  available: false,
  reason: "not_bound",
});

export type AuthoringReferenceAction =
  | "add_source"
  | "remove_source"
  | "reorder_source"
  | "include_soundtrack"
  | "exclude_soundtrack";
export type AuthoringTimelineAction =
  | "add_clip"
  | "remove_clip"
  | "move_clip"
  | "move_group"
  | "trim_clip"
  | "split_clip"
  | "merge_clips"
  | "link_clips"
  | "unlink_clips"
  | "set_envelope"
  | "select_clips";
export type AuthoringAction =
  | "create_authoring_workspace"
  | "ensure_authoring_from_production"
  | "read_projection"
  | "read_snap"
  | "initialize_timeline_history"
  | "read_timeline_history"
  | "apply_timeline_transaction"
  | "release_workspace"
  | "set_availability"
  | AuthoringReferenceAction
  | AuthoringTimelineAction;

export type AuthoringMediaKind = "image" | "video" | "audio";
export type AuthoringDerivedSoundtrack =
  "included" | "excluded" | "unavailable" | "blocked_unknown";
export type AuthoringAvailability = "available" | "unavailable" | "unknown";

export type AuthoringSourceRow = Readonly<{
  sourceId: string;
  kind: AuthoringMediaKind;
  admitted: boolean;
  admissible: boolean;
  reason: string | null;
  durationMilliseconds: number | null;
  label: string | null;
  pairedWith: string | null;
  derivedSoundtrack: AuthoringDerivedSoundtrack | null;
  preview: AuthoringPreviewCapability;
}>;

export type AuthoringCanonicalAsset = Readonly<{
  sourceId: string;
  kind: AuthoringMediaKind;
  label: string;
  pairedWith: string | null;
}>;

export type AuthoringSoundtrackRow = Readonly<{
  videoId: string;
  derivedState: AuthoringDerivedSoundtrack;
  soundtrackSourceId: string | null;
}>;

export type AuthoringQueueBlocker = Readonly<{ videoId: string; code: string }>;

export type AuthoringCapacity = Readonly<{
  aggregateMax: number;
  aggregateUsed: number;
  aggregateRemaining: number;
  imageUsed: number;
  imageRemaining: number;
  videoUsed: number;
  videoRemaining: number;
  pairedAudioUsed: number;
  pairedAudioRemaining: number;
  standaloneAudioUsed: number;
  standaloneAudioRemaining: number;
  maxDurationMilliseconds: number;
  maxFrames: number;
}>;

export type AuthoringEnvelopePoint = Readonly<{
  offsetFrames: number;
  strengthPerMille: number;
}>;

export type AuthoringClip = Readonly<{
  clipId: string;
  assetId: string;
  kind: "video" | "audio";
  lane: number;
  startFrame: number;
  frames: number;
  sourceStartFrame: number;
  envelope: readonly AuthoringEnvelopePoint[];
}>;

export type AuthoringLink = Readonly<{
  videoClipId: string;
  audioClipId: string;
}>;

export type AuthoringTimelineBlocker = Readonly<{
  clipId: string;
  code: string;
}>;

export type AuthoringTimelineProfile = Readonly<{
  videoFps: number;
  frameGrid: number;
  audioPeriodFrames: number;
  maxExtentFrames: number;
}>;

export type AuthoringProjection = Readonly<{
  schema: typeof AUTHORING_PROJECTION_SCHEMA;
  workspaceHandle: string;
  contextSourceId: string;
  taskMode: string;
  registryFingerprint: string;
  reference: Readonly<{
    revision: number;
    sources: readonly AuthoringSourceRow[];
    canonical: readonly AuthoringCanonicalAsset[];
    soundtracks: readonly AuthoringSoundtrackRow[];
    queueBlockers: readonly AuthoringQueueBlocker[];
    capacity: AuthoringCapacity;
  }>;
  availability: Readonly<{ producer: string | null; revision: number }>;
  timeline: Readonly<{
    revision: number;
    contentFingerprint: string;
    profile: AuthoringTimelineProfile;
    clips: readonly AuthoringClip[];
    links: readonly AuthoringLink[];
    selection: readonly string[];
    blockers: readonly AuthoringTimelineBlocker[];
  }>;
  rejection: Readonly<{ code: string }> | null;
}>;

export type AuthoringSnapCandidate = Readonly<{
  frame: number;
  kind: "clip_boundary" | "grid" | "playhead";
  distance: number;
}>;

export type AuthoringSnap = Readonly<{
  schema: typeof AUTHORING_SNAP_SCHEMA;
  workspaceHandle: string;
  timelineRevision: number;
  candidates: readonly AuthoringSnapCandidate[];
}>;

export type TimelineCommandWire = Readonly<{
  kind: (typeof NLE_OPERATION_IDS)[number];
  payload: Readonly<Record<string, unknown>>;
}>;

export type TimelineTransactionWire = Readonly<{
  schema: typeof TIMELINE_TRANSACTION_SCHEMA;
  request_id: string;
  transaction_id: string;
  workspace_handle: string;
  expected_workspace_revision: number;
  expected_timeline_revision: number;
  expected_timeline_fingerprint: string;
  commands: readonly TimelineCommandWire[];
}>;

export type TimelineReceipt = Readonly<{
  schema: typeof TIMELINE_RECEIPT_SCHEMA;
  requestId: string;
  transactionId: string;
  workspaceHandle: string;
  beforeWorkspaceRevision: number;
  afterWorkspaceRevision: number;
  beforeWorkspaceFingerprint: string;
  afterWorkspaceFingerprint: string;
  beforeTimelineRevision: number;
  afterTimelineRevision: number;
  beforeTimelineFingerprint: string;
  afterTimelineFingerprint: string;
  commands: readonly TimelineCommandWire[];
  affectedIds: readonly string[];
  inverse: Readonly<{ kind: string; historyCursor: string }>;
  historyCursor: string;
  selection: readonly string[];
  snapshot: PublicCompositionSnapshot;
}>;

export type TimelineHistoryProjection = Readonly<{
  schema: typeof TIMELINE_HISTORY_PROJECTION_SCHEMA;
  workspaceHandle: string;
  snapshot: PublicCompositionSnapshot;
  selection: readonly string[];
  undoCursor: string | null;
  redoCursor: string | null;
  rejection: Readonly<{ code: string }> | null;
}>;

export type NleAuthoringStateV2 = Readonly<{
  schema: typeof NLE_AUTHORING_SCHEMA;
  profileId: typeof NLE_AUTHORING_PROFILE_ID;
  operationProfileId: typeof NLE_OPERATION_PROFILE_ID_V2;
  projectId: string;
  workspaceHandle: string;
  workspaceRevision: number;
  workspaceFingerprint: string;
  timelineRevision: number;
  timelineFingerprint: string;
  authoringFingerprint: string;
  editCapacityFrames: number;
  contentEndExclusive: number;
  assets: readonly PublicCompositionAsset[];
  tracks: readonly CompositionTrack[];
  clips: readonly CompositionClip[];
  audioExtension: Readonly<Record<string, unknown>>;
  blockers: readonly Readonly<{ code: string; subjectId: string | null }>[];
}>;

export type TimelineHistoryProjectionV2 = Readonly<{
  schema: typeof TIMELINE_HISTORY_PROJECTION_SCHEMA_V2;
  workspaceHandle: string;
  authoring: NleAuthoringStateV2;
  renderSnapshot: PublicCompositionSnapshot | null;
  selection: readonly string[];
  undoCursor: string | null;
  redoCursor: string | null;
  rejection: Readonly<{ code: string }> | null;
}>;

export type TimelineReceiptV2 = Readonly<{
  schema: typeof TIMELINE_RECEIPT_SCHEMA_V2;
  requestId: string;
  transactionId: string;
  workspaceHandle: string;
  beforeAuthoringFingerprint: string;
  afterAuthoringFingerprint: string;
  beforeWorkspaceRevision: number;
  afterWorkspaceRevision: number;
  beforeWorkspaceFingerprint: string;
  afterWorkspaceFingerprint: string;
  beforeTimelineRevision: number;
  afterTimelineRevision: number;
  beforeTimelineFingerprint: string;
  afterTimelineFingerprint: string;
  commands: readonly TimelineCommandWire[];
  affectedIds: readonly string[];
  inverse: Readonly<{
    kind: string;
    historyCursor?: string;
    selection?: readonly string[];
  }>;
  historyCursor: string | null;
  selection: readonly string[];
  authoring: NleAuthoringStateV2;
  renderSnapshot: PublicCompositionSnapshot | null;
}>;

export type TimelineTransactionV2 = Readonly<{
  schema: typeof TIMELINE_TRANSACTION_SCHEMA_V2;
  authoring_schema: typeof NLE_AUTHORING_SCHEMA;
  profile_id: typeof NLE_AUTHORING_PROFILE_ID;
  operation_profile_id: typeof NLE_OPERATION_PROFILE_ID_V2;
  request_id: string;
  transaction_id: string;
  workspace_handle: string;
  expected_workspace_revision: number;
  expected_timeline_revision: number;
  expected_timeline_fingerprint: string;
  expected_authoring_fingerprint: string;
  commands: readonly TimelineCommandWire[];
}>;

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const historyCursor =
  /^h3\.context\.timeline_history_cursor\.v1:[1-9][0-9]{0,6}:[0-9a-f]{64}$/;
// The exact accepted label shape, byte-identical to `sidebarWorkspaceCodec`'s
// candidate rule; agreement between the two derivations is a tested contract.
const labelShape = /^<(Picture|Video|Audio) [1-9][0-9]{0,2}>$/;
const videoLabelShape = /^<Video [1-9][0-9]{0,2}>$/;
const MAX_ROWS = 64;
const MAX_TRANSACTION_COMMANDS = 32;
const MAX_TRANSACTION_BYTES = 262_144;
const MAX_TRANSACTION_WORK_UNITS = 4_096;
const MAX_TRANSACTION_DEPTH = 32;

const commandPayloadKeys = Object.freeze({
  create_track: ["track_id", "kind", "order"],
  remove_track: ["track_id"],
  reorder_track: ["track_id", "order"],
  set_track_enabled: ["track_id", "enabled"],
  set_track_locked: ["track_id", "locked"],
  insert_asset_clip: ["clip"],
  insert_title_clip: ["clip"],
  replace_clip_asset: ["clip_id", "asset_id", "source_start_frame"],
  remove_clip: ["clip_id"],
  move_clip: ["clip_id", "delta_frames", "target_track_id"],
  move_group: ["clip_ids", "delta_frames", "target_track_ids"],
  trim_clip: ["clip_id", "edge", "delta_frames"],
  split_clip: ["clip_id", "at_offset_frames", "right_clip_id"],
  merge_clips: ["left_clip_id", "right_clip_id"],
  insert_range: ["clip", "scope_track_ids"],
  overwrite_range: [
    "clip",
    "start_frame",
    "duration_frames",
    "scope_track_ids",
    "remainder_ids",
  ],
  ripple_delete: [
    "start_frame",
    "duration_frames",
    "scope_track_ids",
    "remainder_ids",
  ],
  ripple_trim: ["clip_id", "edge", "delta_frames", "scope_track_ids"],
  roll_edit: ["left_clip_id", "right_clip_id", "delta_frames"],
  slip_clip: ["clip_id", "delta_frames"],
  slide_clip: ["clip_id", "left_clip_id", "right_clip_id", "delta_frames"],
  set_clip_enabled: ["clip_id", "enabled"],
  set_visual_transform: ["clip_id", "transform"],
  set_crop: ["clip_id", "crop"],
  set_opacity_blend: ["clip_id", "opacity_bp", "blend"],
  set_text_content: ["clip_id", "content"],
  set_text_style: ["clip_id", "style"],
  set_transition: ["clip_id", "transition"],
  set_effect: ["clip_id", "effect"],
  set_clip_audio: [
    "clip_id",
    "gain_mb",
    "muted",
    "fade_in_frames",
    "fade_out_frames",
  ],
  select_clips: ["clip_ids"],
  undo: ["history_cursor"],
  redo: ["history_cursor"],
  rebase_transaction: ["base_timeline_fingerprint", "commands"],
} satisfies Record<(typeof NLE_OPERATION_IDS)[number], readonly string[]>);

const deferredAudioCommands = new Set([
  "create_audio_track",
  "insert_audio",
  "import_audio",
  "replace_audio",
  "link_audio",
  "unlink_audio",
  "set_gain",
  "set_pan",
  "set_mute",
  "set_solo",
  "set_envelope",
  "set_waveform",
  "mix_audio",
]);

const historyRejectionCodes = new Set<string>([
  ...BLOCKER_CODES,
  "invalid_transaction",
  "invalid_command",
  "stale_workspace_revision",
  "stale_timeline_revision",
  "stale_timeline_fingerprint",
  "idempotency_conflict",
  "history_cursor_invalid",
  "history_branch_invalid",
  "rebase_conflict",
  "workspace_released",
  "resource_limit",
  "audio_editing_deferred",
]);

const privateFields = new Set([
  "private_source_manifest",
  "derivative_manifest",
  "runtime_identity",
  "source_path",
  "source_url",
  "blob_url",
  "lease",
  "credential",
]);

function object(value: unknown, keys: readonly string[], name: string) {
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

function exactInteger(value: unknown, min: number, max: number, name: string) {
  if (
    !Number.isInteger(value) ||
    (value as number) < min ||
    (value as number) > max
  )
    throw new Error(`${name} is invalid`);
  return value as number;
}

function exactBoolean(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${name} is invalid`);
  return value;
}

function nullableString(
  value: unknown,
  pattern: RegExp,
  name: string,
): string | null {
  if (value === null) return null;
  return exactString(value, pattern, name);
}

function boundedArray(value: unknown, name: string): readonly unknown[] {
  if (!Array.isArray(value) || value.length > MAX_ROWS)
    throw new Error(`${name} is invalid`);
  return value;
}

function resourceArray(
  value: unknown,
  minimum: number,
  maximum: number,
  name: string,
): readonly unknown[] {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum)
    throw new Error(`${name} is invalid`);
  return value;
}

function boundedIdentifiers(
  value: unknown,
  maximum: number,
  name: string,
  { allowEmpty = true, unique = true } = {},
): string[] {
  const rows = resourceArray(value, allowEmpty ? 0 : 1, maximum, name).map(
    (member, index) => exactString(member, identifier, `${name}[${index}]`),
  );
  if (unique && new Set(rows).size !== rows.length)
    throw new Error(`${name} must contain unique identifiers`);
  return rows;
}

function cloneCanonical(
  value: unknown,
  name: string,
  state = { nodes: 0 },
  depth = 0,
): unknown {
  state.nodes += 1;
  if (state.nodes > MAX_TRANSACTION_WORK_UNITS || depth > MAX_TRANSACTION_DEPTH)
    throw new Error(`${name} exceeds the resource profile`);
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value))
      throw new Error(`${name} must contain bounded canonical JSON`);
    return value;
  }
  if (typeof value === "string") {
    const normalized = value.normalize("NFC");
    if (normalized.length > 65_536 || /[\u0000\uD800-\uDFFF]/u.test(normalized))
      throw new Error(`${name} must contain bounded canonical JSON`);
    return normalized;
  }
  if (Array.isArray(value)) {
    if (value.length > 256)
      throw new Error(`${name} exceeds the resource profile`);
    return Object.freeze(
      value.map((member) => cloneCanonical(member, name, state, depth + 1)),
    );
  }
  if (typeof value !== "object")
    throw new Error(`${name} must contain bounded canonical JSON`);
  const wire = value as Record<string, unknown>;
  const keys = Object.keys(wire);
  if (keys.length > 256)
    throw new Error(`${name} exceeds the resource profile`);
  const clone: Record<string, unknown> = {};
  for (const key of keys.sort()) {
    if (!identifier.test(key) || privateFields.has(key))
      throw new Error(`${name} contains a private or invalid member`);
    clone[key] = cloneCanonical(wire[key], name, state, depth + 1);
  }
  return Object.freeze(clone);
}

function transactionWorkUnits(value: unknown, depth = 0): number {
  if (depth > MAX_TRANSACTION_DEPTH)
    throw new Error("timeline transaction exceeds the work profile");
  if (typeof value === "string")
    return 1 + Math.floor(new TextEncoder().encode(value).length / 64);
  if (Array.isArray(value))
    return (
      1 +
      value.reduce(
        (total, member) => total + transactionWorkUnits(member, depth + 1),
        0,
      )
    );
  if (value !== null && typeof value === "object")
    return (
      1 +
      Object.entries(value).reduce(
        (total, [key, member]) =>
          total +
          transactionWorkUnits(key, depth + 1) +
          transactionWorkUnits(member, depth + 1),
        0,
      )
    );
  return 1;
}

function mediaKind(value: unknown, name: string): AuthoringMediaKind {
  if (value !== "image" && value !== "video" && value !== "audio")
    throw new Error(`${name} is invalid`);
  return value;
}

function derived(value: unknown, name: string): AuthoringDerivedSoundtrack {
  if (
    value !== "included" &&
    value !== "excluded" &&
    value !== "unavailable" &&
    value !== "blocked_unknown"
  )
    throw new Error(`${name} is invalid`);
  return value;
}

function decodeCapacity(value: unknown): AuthoringCapacity {
  const wire = object(
    value,
    [
      "schema",
      "aggregate_max",
      "aggregate_used",
      "aggregate_remaining",
      "image_used",
      "image_remaining",
      "video_used",
      "video_remaining",
      "paired_audio_used",
      "paired_audio_remaining",
      "standalone_audio_used",
      "standalone_audio_remaining",
      "timed",
    ],
    "authoring capacity",
  );
  const timed = object(
    wire.timed,
    ["max_duration_milliseconds", "max_frames"],
    "authoring capacity timed",
  );
  const bound = (key: string) =>
    exactInteger(wire[key], 0, 1_000, `authoring capacity ${key}`);
  const capacity: AuthoringCapacity = Object.freeze({
    aggregateMax: bound("aggregate_max"),
    aggregateUsed: bound("aggregate_used"),
    aggregateRemaining: bound("aggregate_remaining"),
    imageUsed: bound("image_used"),
    imageRemaining: bound("image_remaining"),
    videoUsed: bound("video_used"),
    videoRemaining: bound("video_remaining"),
    pairedAudioUsed: bound("paired_audio_used"),
    pairedAudioRemaining: bound("paired_audio_remaining"),
    standaloneAudioUsed: bound("standalone_audio_used"),
    standaloneAudioRemaining: bound("standalone_audio_remaining"),
    maxDurationMilliseconds: exactInteger(
      timed.max_duration_milliseconds,
      1,
      86_400_000,
      "authoring capacity max duration",
    ),
    maxFrames: exactInteger(
      timed.max_frames,
      1,
      1_000_000,
      "authoring capacity max frames",
    ),
  });
  // A remaining figure the aggregate would refuse is exactly the defect the
  // backend projection exists to prevent; reject it instead of rendering it.
  for (const remaining of [
    capacity.imageRemaining,
    capacity.videoRemaining,
    capacity.pairedAudioRemaining,
    capacity.standaloneAudioRemaining,
  ]) {
    if (remaining > capacity.aggregateRemaining)
      throw new Error("authoring capacity advertises an impossible maximum");
  }
  return capacity;
}

function decodeSourceRow(value: unknown, index: number): AuthoringSourceRow {
  const hasPreview =
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.prototype.hasOwnProperty.call(value, "preview");
  const wire = object(
    value,
    [
      "source_id",
      "kind",
      "admitted",
      "admissible",
      "reason",
      "duration_milliseconds",
      "label",
      "paired_with",
      "derived_soundtrack",
      ...(hasPreview ? ["preview"] : []),
    ],
    `authoring source[${index}]`,
  );
  const admitted = exactBoolean(wire.admitted, "source admitted");
  const label = nullableString(wire.label, labelShape, "source label");
  if (!admitted && label !== null)
    throw new Error("a source that is not admitted cannot carry a label");
  return Object.freeze({
    sourceId: exactString(wire.source_id, identifier, "source id"),
    kind: mediaKind(wire.kind, "source kind"),
    admitted,
    admissible: exactBoolean(wire.admissible, "source admissible"),
    reason: nullableString(wire.reason, identifier, "source reason"),
    durationMilliseconds:
      wire.duration_milliseconds === null
        ? null
        : exactInteger(
            wire.duration_milliseconds,
            1,
            86_400_000,
            "source duration",
          ),
    label,
    pairedWith: nullableString(
      wire.paired_with,
      videoLabelShape,
      "source paired_with",
    ),
    derivedSoundtrack:
      wire.derived_soundtrack === null
        ? null
        : derived(wire.derived_soundtrack, "source derived soundtrack"),
    preview: hasPreview
      ? decodeAuthoringPreviewCapability(wire.preview)
      : NOT_BOUND_PREVIEW,
  });
}

function decodeCanonical(
  value: unknown,
  index: number,
): AuthoringCanonicalAsset {
  const wire = object(
    value,
    ["source_id", "kind", "label", "paired_with"],
    `authoring canonical[${index}]`,
  );
  const kind = mediaKind(wire.kind, "canonical kind");
  const pairedWith = nullableString(
    wire.paired_with,
    videoLabelShape,
    "canonical paired_with",
  );
  if (pairedWith !== null && kind !== "audio")
    throw new Error("only an audio asset can be paired with a video");
  return Object.freeze({
    sourceId: exactString(wire.source_id, identifier, "canonical id"),
    kind,
    label: exactString(wire.label, labelShape, "canonical label"),
    pairedWith,
  });
}

function decodeClip(value: unknown, index: number): AuthoringClip {
  const wire = object(
    value,
    [
      "clip_id",
      "asset_id",
      "kind",
      "lane",
      "start_frame",
      "frames",
      "source_start_frame",
      "envelope",
    ],
    `authoring clip[${index}]`,
  );
  const kind = wire.kind;
  if (kind !== "video" && kind !== "audio")
    throw new Error("clip kind is invalid");
  const envelope = boundedArray(wire.envelope, "clip envelope").map(
    (item, pointIndex) => {
      const point = object(
        item,
        ["offset_frames", "strength_per_mille"],
        `clip envelope[${pointIndex}]`,
      );
      return Object.freeze({
        offsetFrames: exactInteger(
          point.offset_frames,
          0,
          1_000_000,
          "envelope offset",
        ),
        strengthPerMille: exactInteger(
          point.strength_per_mille,
          0,
          1_000,
          "envelope strength",
        ),
      });
    },
  );
  return Object.freeze({
    clipId: exactString(wire.clip_id, identifier, "clip id"),
    assetId: exactString(wire.asset_id, identifier, "clip asset id"),
    kind,
    lane: exactInteger(wire.lane, 0, 63, "clip lane"),
    startFrame: exactInteger(wire.start_frame, 0, 1_000_000, "clip start"),
    frames: exactInteger(wire.frames, 1, 1_000_000, "clip frames"),
    sourceStartFrame: exactInteger(
      wire.source_start_frame,
      0,
      1_000_000,
      "clip source start",
    ),
    envelope: Object.freeze(envelope),
  });
}

export function decodeAuthoringProjection(value: unknown): AuthoringProjection {
  const wire = object(
    value,
    [
      "schema",
      "workspace_handle",
      "context_source_id",
      "task_mode",
      "registry_fingerprint",
      "reference",
      "availability",
      "timeline",
      "rejection",
    ],
    "authoring projection",
  );
  if (wire.schema !== AUTHORING_PROJECTION_SCHEMA)
    throw new Error("authoring projection schema is unsupported");
  const reference = object(
    wire.reference,
    [
      "revision",
      "sources",
      "canonical",
      "soundtracks",
      "queue_blockers",
      "capacity",
    ],
    "authoring reference",
  );
  const availability = object(
    wire.availability,
    ["producer", "revision"],
    "authoring availability",
  );
  const timeline = object(
    wire.timeline,
    [
      "revision",
      "content_fingerprint",
      "profile",
      "clips",
      "links",
      "selection",
      "blockers",
    ],
    "authoring timeline",
  );
  const profile = object(
    timeline.profile,
    ["video_fps", "frame_grid", "audio_period_frames", "max_extent_frames"],
    "authoring timeline profile",
  );
  const sources = boundedArray(reference.sources, "authoring sources").map(
    decodeSourceRow,
  );
  const canonical = boundedArray(
    reference.canonical,
    "authoring canonical",
  ).map(decodeCanonical);
  const soundtracks = boundedArray(
    reference.soundtracks,
    "authoring soundtracks",
  ).map((item, index) => {
    const row = object(
      item,
      ["video_id", "derived_state", "soundtrack_source_id"],
      `authoring soundtrack[${index}]`,
    );
    return Object.freeze({
      videoId: exactString(row.video_id, identifier, "soundtrack video id"),
      derivedState: derived(row.derived_state, "soundtrack derived state"),
      soundtrackSourceId: nullableString(
        row.soundtrack_source_id,
        identifier,
        "soundtrack source id",
      ),
    });
  });
  const queueBlockers = boundedArray(
    reference.queue_blockers,
    "authoring queue blockers",
  ).map((item, index) => {
    const row = object(
      item,
      ["video_id", "code"],
      `authoring blocker[${index}]`,
    );
    return Object.freeze({
      videoId: exactString(row.video_id, identifier, "blocker video id"),
      code: exactString(row.code, identifier, "blocker code"),
    });
  });
  const clips = boundedArray(timeline.clips, "authoring clips").map(decodeClip);
  const links = boundedArray(timeline.links, "authoring links").map(
    (item, index) => {
      const row = object(
        item,
        ["video_clip_id", "audio_clip_id"],
        `authoring link[${index}]`,
      );
      return Object.freeze({
        videoClipId: exactString(row.video_clip_id, identifier, "link video"),
        audioClipId: exactString(row.audio_clip_id, identifier, "link audio"),
      });
    },
  );
  const selection = boundedArray(timeline.selection, "authoring selection").map(
    (item, index) => exactString(item, identifier, `selection[${index}]`),
  );
  const blockers = boundedArray(timeline.blockers, "authoring blockers").map(
    (item, index) => {
      const row = object(
        item,
        ["clip_id", "code"],
        `authoring timeline blocker[${index}]`,
      );
      return Object.freeze({
        clipId:
          row.clip_id === ""
            ? ""
            : exactString(row.clip_id, identifier, "timeline blocker clip"),
        code: exactString(row.code, identifier, "timeline blocker code"),
      });
    },
  );
  const rejection =
    wire.rejection === null
      ? null
      : Object.freeze({
          code: exactString(
            object(wire.rejection, ["code"], "authoring rejection").code,
            identifier,
            "rejection code",
          ),
        });
  return Object.freeze({
    schema: AUTHORING_PROJECTION_SCHEMA,
    workspaceHandle: exactString(
      wire.workspace_handle,
      identifier,
      "workspace handle",
    ),
    contextSourceId: exactString(
      wire.context_source_id,
      identifier,
      "context source id",
    ),
    taskMode: exactString(wire.task_mode, identifier, "task mode"),
    registryFingerprint: exactString(
      wire.registry_fingerprint,
      fingerprint,
      "registry fingerprint",
    ),
    reference: Object.freeze({
      revision: exactInteger(
        reference.revision,
        1,
        1_000_000,
        "reference revision",
      ),
      sources: Object.freeze(sources),
      canonical: Object.freeze(canonical),
      soundtracks: Object.freeze(soundtracks),
      queueBlockers: Object.freeze(queueBlockers),
      capacity: decodeCapacity(reference.capacity),
    }),
    availability: Object.freeze({
      producer:
        availability.producer === null
          ? null
          : exactString(
              availability.producer,
              /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/,
              "availability producer",
            ),
      revision: exactInteger(
        availability.revision,
        0,
        1_000_000,
        "availability revision",
      ),
    }),
    timeline: Object.freeze({
      revision: exactInteger(
        timeline.revision,
        1,
        1_000_000,
        "timeline revision",
      ),
      contentFingerprint: exactString(
        timeline.content_fingerprint,
        fingerprint,
        "timeline content fingerprint",
      ),
      profile: Object.freeze({
        videoFps: exactInteger(profile.video_fps, 1, 1_000, "profile fps"),
        frameGrid: exactInteger(
          profile.frame_grid,
          1,
          1_000_000,
          "profile grid",
        ),
        audioPeriodFrames: exactInteger(
          profile.audio_period_frames,
          1,
          1_000,
          "profile audio period",
        ),
        maxExtentFrames: exactInteger(
          profile.max_extent_frames,
          1,
          1_000_000,
          "profile extent",
        ),
      }),
      clips: Object.freeze(clips),
      links: Object.freeze(links),
      selection: Object.freeze(selection),
      blockers: Object.freeze(blockers),
    }),
    rejection,
  });
}

export function decodeAuthoringSnap(value: unknown): AuthoringSnap {
  const wire = object(
    value,
    ["schema", "workspace_handle", "timeline_revision", "candidates"],
    "authoring snap",
  );
  if (wire.schema !== AUTHORING_SNAP_SCHEMA)
    throw new Error("authoring snap schema is unsupported");
  const candidates = boundedArray(wire.candidates, "snap candidates").map(
    (item, index) => {
      const row = object(
        item,
        ["frame", "kind", "distance"],
        `snap candidate[${index}]`,
      );
      const kind = row.kind;
      if (kind !== "clip_boundary" && kind !== "grid" && kind !== "playhead")
        throw new Error("snap candidate kind is invalid");
      return Object.freeze({
        frame: exactInteger(row.frame, 0, 1_000_000, "snap frame"),
        kind,
        distance: exactInteger(row.distance, 0, 1_000_000, "snap distance"),
      });
    },
  );
  return Object.freeze({
    schema: AUTHORING_SNAP_SCHEMA,
    workspaceHandle: exactString(
      wire.workspace_handle,
      identifier,
      "snap workspace handle",
    ),
    timelineRevision: exactInteger(
      wire.timeline_revision,
      1,
      1_000_000,
      "snap timeline revision",
    ),
    candidates: Object.freeze(candidates),
  });
}

function exactHistoryCursor(value: unknown, name: string): string {
  const cursor = exactString(value, historyCursor, name);
  const sequence = Number(cursor.split(":")[1]);
  if (!Number.isInteger(sequence) || sequence < 1 || sequence > 1_000_000)
    throw new Error(`${name} is invalid`);
  return cursor;
}

function exactChoice(
  value: unknown,
  choices: readonly string[],
  name: string,
): string {
  if (typeof value !== "string" || !choices.includes(value))
    throw new Error(`${name} is outside the closed vocabulary`);
  return value;
}

function validateRgba(value: unknown, name: string): void {
  const channels = resourceArray(value, 4, 4, name);
  channels.forEach((channel, index) =>
    exactInteger(channel, 0, 255, `${name}[${index}]`),
  );
}

function validateTransform(value: unknown, name: string): void {
  const wire = object(value, transform2DKeys, name);
  const bounds: Readonly<Record<string, readonly [number, number]>> = {
    anchor_x_bp: [0, 10_000],
    anchor_y_bp: [0, 10_000],
    position_x_bp: [-40_000, 40_000],
    position_y_bp: [-40_000, 40_000],
    scale_x_bp: [1, 80_000],
    scale_y_bp: [1, 80_000],
    rotation_mdeg: [-180_000, 180_000],
  };
  for (const [key, [minimum, maximum]] of Object.entries(bounds))
    exactInteger(wire[key], minimum, maximum, `${name}.${key}`);
}

function validateCrop(value: unknown, name: string): void {
  const wire = object(value, cropKeys, name);
  for (const key of cropKeys)
    exactInteger(wire[key], 0, 9_999, `${name}.${key}`);
  if (
    Number(wire.left_bp) + Number(wire.right_bp) >= 10_000 ||
    Number(wire.top_bp) + Number(wire.bottom_bp) >= 10_000
  )
    throw new Error(`${name} removes the full image`);
}

function validateTextContent(value: unknown, name: string): void {
  const lineBreak = /\r\n|[\n\v\f\r\u001c-\u001e\u0085\u2028\u2029]/u;
  const terminalLineBreak =
    /(?:\r\n|[\n\v\f\r\u001c-\u001e\u0085\u2028\u2029])$/u;
  const lineCount =
    typeof value === "string"
      ? value.split(lineBreak).length - (terminalLineBreak.test(value) ? 1 : 0)
      : 0;
  if (
    typeof value !== "string" ||
    Array.from(value).length < 1 ||
    Array.from(value).length > 2_048 ||
    lineCount > 32 ||
    /[\u0000-\u0008\u000b-\u001f]/u.test(value)
  )
    throw new Error(`${name} is outside text bounds`);
}

function validateTextStyle(
  value: unknown,
  name: string,
  { includeContent }: Readonly<{ includeContent: boolean }>,
): void {
  const keys = includeContent
    ? textStyleKeys
    : textStyleKeys.filter((key) => key !== "content");
  const wire = object(value, keys, name);
  if (includeContent) validateTextContent(wire.content, `${name}.content`);
  exactString(wire.font_asset_id, identifier, `${name}.font_asset_id`);
  exactInteger(wire.size_px, 8, 512, `${name}.size_px`);
  const weight = exactInteger(wire.weight, 400, 700, `${name}.weight`);
  if (weight !== 400 && weight !== 700)
    throw new Error(`${name}.weight is unsupported`);
  exactChoice(wire.style, ["normal", "italic"], `${name}.style`);
  exactChoice(wire.align, ["left", "center", "right"], `${name}.align`);
  exactInteger(wire.line_height_bp, 7_500, 30_000, `${name}.line_height_bp`);
  validateRgba(wire.fill_rgba, `${name}.fill_rgba`);
  if (wire.background_rgba !== null)
    validateRgba(wire.background_rgba, `${name}.background_rgba`);
}

function validateTransition(value: unknown, name: string): number {
  const wire = object(value, transitionKeys, name);
  const kind = exactChoice(
    wire.kind,
    ["none", "cross_dissolve_v1"],
    `${name}.kind`,
  );
  const duration = exactInteger(
    wire.duration_frames,
    0,
    300,
    `${name}.duration_frames`,
  );
  if (
    (kind === "none" && duration !== 0) ||
    (kind === "cross_dissolve_v1" && duration < 1)
  )
    throw new Error(`${name}.duration_frames is inconsistent with its kind`);
  return duration;
}

function validateEffect(value: unknown, name: string): void {
  const wire = object(value, effectKeys, name);
  const kind = exactChoice(
    wire.kind,
    ["none", "color_adjust_v1"],
    `${name}.kind`,
  );
  const brightness = exactInteger(
    wire.brightness_permille,
    -1_000,
    1_000,
    `${name}.brightness_permille`,
  );
  const contrast = exactInteger(
    wire.contrast_permille,
    0,
    2_000,
    `${name}.contrast_permille`,
  );
  const saturation = exactInteger(
    wire.saturation_permille,
    0,
    2_000,
    `${name}.saturation_permille`,
  );
  if (
    kind === "none" &&
    (brightness !== 0 || contrast !== 1_000 || saturation !== 1_000)
  )
    throw new Error(`${name} none must use identity values`);
}

function validateClip(
  value: unknown,
  name: string,
  expected: "asset" | "title" | "either",
): void {
  const wire = object(value, compositionClipWireKeys(value), name);
  exactString(wire.clip_id, identifier, `${name}.clip_id`);
  exactString(wire.track_id, identifier, `${name}.track_id`);
  const hasAsset = wire.asset_id !== null;
  const hasText = wire.text !== null;
  if (hasAsset) exactString(wire.asset_id, identifier, `${name}.asset_id`);
  if (
    hasAsset === hasText ||
    (expected === "asset" && !hasAsset) ||
    (expected === "title" && !hasText)
  )
    throw new Error(`${name} media/title shape is invalid`);
  exactInteger(wire.start_frame, 0, 999_999, `${name}.start_frame`);
  const durationFrames = exactInteger(
    wire.duration_frames,
    1,
    1_000_000,
    `${name}.duration_frames`,
  );
  const sourceStart = exactInteger(
    wire.source_start_frame,
    0,
    999_999,
    `${name}.source_start_frame`,
  );
  if (hasText && sourceStart !== 0)
    throw new Error(`${name}.source_start_frame is invalid for a title`);
  exactBoolean(wire.enabled, `${name}.enabled`);
  validateTransform(wire.transform, `${name}.transform`);
  validateCrop(wire.crop, `${name}.crop`);
  exactInteger(wire.opacity_bp, 0, 10_000, `${name}.opacity_bp`);
  exactChoice(wire.blend, ["normal", "multiply", "screen"], `${name}.blend`);
  if (hasText)
    validateTextStyle(wire.text, `${name}.text`, { includeContent: true });
  if (
    validateTransition(wire.transition, `${name}.transition`) > durationFrames
  )
    throw new Error(`${name}.transition duration exceeds the clip`);
  validateEffect(wire.effect, `${name}.effect`);
  if (Object.hasOwn(wire, "audio"))
    decodeClipAudio(wire.audio, durationFrames, `${name}.audio`);
}

function validateRemainderIds(value: unknown, name: string): void {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} must be an object`);
  const wire = value as Record<string, unknown>;
  const keys = Object.keys(wire);
  if (keys.length > 32) throw new Error(`${name} exceeds the resource profile`);
  const values = keys.map((key) => {
    exactString(key, identifier, `${name} key`);
    return exactString(wire[key], identifier, `${name} value`);
  });
  if (new Set(values).size !== values.length)
    throw new Error(`${name} values must be unique`);
}

function validateCommandPayload(
  kind: TimelineCommandWire["kind"],
  payload: Readonly<Record<string, unknown>>,
  name: string,
): void {
  const id = (key: string) =>
    exactString(payload[key], identifier, `${name}.${key}`);
  const integer = (key: string, minimum: number, maximum: number) =>
    exactInteger(payload[key], minimum, maximum, `${name}.${key}`);
  switch (kind) {
    case "create_track":
      id("track_id");
      exactChoice(
        payload.kind,
        ["video_overlay", "image_overlay", "text_overlay"],
        `${name}.kind`,
      );
      integer("order", 0, 7);
      return;
    case "remove_track":
      id("track_id");
      return;
    case "reorder_track":
      id("track_id");
      integer("order", 0, 7);
      return;
    case "set_track_enabled":
    case "set_track_locked":
      id("track_id");
      exactBoolean(
        payload[kind === "set_track_enabled" ? "enabled" : "locked"],
        name,
      );
      return;
    case "insert_asset_clip":
      validateClip(payload.clip, `${name}.clip`, "asset");
      return;
    case "insert_title_clip":
      validateClip(payload.clip, `${name}.clip`, "title");
      return;
    case "insert_range":
      validateClip(payload.clip, `${name}.clip`, "either");
      return;
    case "overwrite_range":
      validateClip(payload.clip, `${name}.clip`, "either");
      integer("start_frame", 0, 999_999);
      integer("duration_frames", 1, 1_000_000);
      validateRemainderIds(payload.remainder_ids, `${name}.remainder_ids`);
      return;
    case "ripple_delete":
      integer("start_frame", 0, 999_999);
      integer("duration_frames", 1, 1_000_000);
      validateRemainderIds(payload.remainder_ids, `${name}.remainder_ids`);
      return;
    case "replace_clip_asset":
      id("clip_id");
      id("asset_id");
      integer("source_start_frame", 0, 1_000_000);
      return;
    case "remove_clip":
      id("clip_id");
      return;
    case "move_clip":
      id("clip_id");
      id("target_track_id");
      integer("delta_frames", -1_000_000, 1_000_000);
      return;
    case "move_group":
      integer("delta_frames", -1_000_000, 1_000_000);
      return;
    case "trim_clip":
    case "ripple_trim":
      id("clip_id");
      exactChoice(payload.edge, ["start", "end"], `${name}.edge`);
      if (integer("delta_frames", -1_000_000, 1_000_000) === 0)
        throw new Error(`${name}.delta_frames must be non-zero`);
      return;
    case "split_clip":
      id("clip_id");
      id("right_clip_id");
      integer("at_offset_frames", 1, 999_999);
      return;
    case "merge_clips":
    case "roll_edit":
      id("left_clip_id");
      id("right_clip_id");
      if (
        kind === "roll_edit" &&
        integer("delta_frames", -1_000_000, 1_000_000) === 0
      )
        throw new Error(`${name}.delta_frames must be non-zero`);
      return;
    case "slip_clip":
      id("clip_id");
      if (integer("delta_frames", -1_000_000, 1_000_000) === 0)
        throw new Error(`${name}.delta_frames must be non-zero`);
      return;
    case "slide_clip":
      id("clip_id");
      id("left_clip_id");
      id("right_clip_id");
      if (integer("delta_frames", -1_000_000, 1_000_000) === 0)
        throw new Error(`${name}.delta_frames must be non-zero`);
      return;
    case "set_clip_enabled":
      id("clip_id");
      exactBoolean(payload.enabled, `${name}.enabled`);
      return;
    case "set_visual_transform":
      id("clip_id");
      validateTransform(payload.transform, `${name}.transform`);
      return;
    case "set_crop":
      id("clip_id");
      validateCrop(payload.crop, `${name}.crop`);
      return;
    case "set_opacity_blend":
      id("clip_id");
      integer("opacity_bp", 0, 10_000);
      exactChoice(
        payload.blend,
        ["normal", "multiply", "screen"],
        `${name}.blend`,
      );
      return;
    case "set_text_content":
      id("clip_id");
      validateTextContent(payload.content, `${name}.content`);
      return;
    case "set_text_style":
      id("clip_id");
      validateTextStyle(payload.style, `${name}.style`, {
        includeContent: false,
      });
      return;
    case "set_transition":
      id("clip_id");
      validateTransition(payload.transition, `${name}.transition`);
      return;
    case "set_effect":
      id("clip_id");
      validateEffect(payload.effect, `${name}.effect`);
      return;
    case "set_clip_audio":
      // The member's bounds only. Whether the clip's source has bound audio and whether both
      // fades fit the clip are the core's to judge against the timeline it holds.
      id("clip_id");
      integer("gain_mb", CLIP_AUDIO_GAIN_MIN_MB, CLIP_AUDIO_GAIN_MAX_MB);
      exactBoolean(payload.muted, `${name}.muted`);
      integer("fade_in_frames", 0, CLIP_AUDIO_FADE_MAX_FRAMES);
      integer("fade_out_frames", 0, CLIP_AUDIO_FADE_MAX_FRAMES);
      return;
    case "select_clips":
    case "undo":
    case "redo":
    case "rebase_transaction":
      return;
  }
}

function decodeTimelineCommand(
  value: unknown,
  name: string,
  historyCursorVersion: 1 | 2 = 1,
): TimelineCommandWire {
  const wire = object(value, ["kind", "payload"], name);
  if (typeof wire.kind !== "string") throw new Error(`${name} kind is invalid`);
  if (
    deferredAudioCommands.has(wire.kind) ||
    wire.kind.startsWith(INDEPENDENT_AUDIO_COMMAND_NAMESPACE)
  )
    throw new Error("audio_editing_deferred");
  if (!(NLE_OPERATION_IDS as readonly string[]).includes(wire.kind))
    throw new Error(`${name} kind is outside the closed profile`);
  const kind = wire.kind as TimelineCommandWire["kind"];
  const rawPayload = object(
    wire.payload,
    commandPayloadKeys[kind],
    `${name} payload`,
  );
  let payload = cloneCanonical(rawPayload, `${name} payload`) as Readonly<
    Record<string, unknown>
  >;

  // CRITICAL: this is wire validation only; state and geometry stay backend-canonical. Omitting
  // nested closed-shape, identifier, boolean, and finite-integer checks lets malformed commands
  // cross the browser boundary even though the outer command object appears closed.
  validateCommandPayload(kind, payload, `${name} payload`);

  // IMPORTANT: mirror the backend's canonical list normalization here. If browser order differs,
  // the request fingerprint and idempotency identity differ even though the command is
  // semantically identical, which would turn a safe retry into an idempotency conflict.
  if (kind === "select_clips") {
    const clipIds = boundedIdentifiers(payload.clip_ids, 128, "clip_ids", {
      unique: false,
    });
    payload = Object.freeze({
      ...payload,
      clip_ids: Object.freeze([...new Set(clipIds)].sort()),
    });
  } else if (kind === "move_group") {
    const clipIds = boundedIdentifiers(payload.clip_ids, 32, "clip_ids", {
      allowEmpty: false,
    });
    const targetTrackIds = boundedIdentifiers(
      payload.target_track_ids,
      32,
      "target_track_ids",
      { allowEmpty: false, unique: false },
    );
    if (clipIds.length !== targetTrackIds.length)
      throw new Error("move_group identifiers must align");
    const pairs = clipIds
      .map(
        (clipId, index) => [clipId, targetTrackIds[index] as string] as const,
      )
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
    payload = Object.freeze({
      ...payload,
      clip_ids: Object.freeze(pairs.map(([clipId]) => clipId)),
      target_track_ids: Object.freeze(pairs.map(([, trackId]) => trackId)),
    });
  } else if (
    kind === "insert_range" ||
    kind === "overwrite_range" ||
    kind === "ripple_delete" ||
    kind === "ripple_trim"
  ) {
    const scope = boundedIdentifiers(
      payload.scope_track_ids,
      8,
      "scope_track_ids",
      { allowEmpty: false },
    );
    payload = Object.freeze({
      ...payload,
      scope_track_ids: Object.freeze(scope.sort()),
    });
  } else if (kind === "undo" || kind === "redo") {
    // CRITICAL: V2 branch cursors have a distinct schema; applying the V1 validator here
    // rejects valid Undo/Redo before the transaction reaches the host.
    if (historyCursorVersion === 2)
      exactHistoryCursorV2(payload.history_cursor, `${name} history cursor`);
    else exactHistoryCursor(payload.history_cursor, `${name} history cursor`);
  } else if (kind === "rebase_transaction") {
    exactString(
      payload.base_timeline_fingerprint,
      fingerprint,
      `${name} base timeline fingerprint`,
    );
    const nested = resourceArray(
      payload.commands,
      1,
      MAX_TRANSACTION_COMMANDS,
      `${name} rebase commands`,
    ).map((member, index) =>
      decodeTimelineCommand(
        member,
        `${name} rebase command[${index}]`,
        historyCursorVersion,
      ),
    );
    const rebasable = new Set([
      "set_clip_enabled",
      "set_visual_transform",
      "set_crop",
      "set_opacity_blend",
      "set_text_content",
      "set_text_style",
      "set_transition",
      "set_effect",
      "set_clip_audio",
      "select_clips",
    ]);
    if (nested.some((member) => !rebasable.has(member.kind)))
      throw new Error("rebase_conflict");
    payload = Object.freeze({
      ...payload,
      commands: Object.freeze(nested),
    });
  }
  return Object.freeze({ kind, payload });
}

function decodeTimelineTransactionWire(
  value: unknown,
): TimelineTransactionWire {
  const wire = object(
    value,
    [
      "schema",
      "request_id",
      "transaction_id",
      "workspace_handle",
      "expected_workspace_revision",
      "expected_timeline_revision",
      "expected_timeline_fingerprint",
      "commands",
    ],
    "timeline transaction",
  );
  if (wire.schema !== TIMELINE_TRANSACTION_SCHEMA)
    throw new Error("timeline transaction schema is unsupported");
  const decodedCommands = resourceArray(
    wire.commands,
    1,
    MAX_TRANSACTION_COMMANDS,
    "timeline transaction commands",
  ).map((command, index) =>
    decodeTimelineCommand(command, `timeline command[${index}]`),
  );
  const historyKinds = new Set(["undo", "redo", "rebase_transaction"]);
  if (
    decodedCommands.some((command) => historyKinds.has(command.kind)) &&
    decodedCommands.length !== 1
  )
    throw new Error("a history command must be the sole command");
  const transaction = Object.freeze({
    schema: TIMELINE_TRANSACTION_SCHEMA,
    request_id: exactString(wire.request_id, identifier, "request id"),
    transaction_id: exactString(
      wire.transaction_id,
      identifier,
      "transaction id",
    ),
    workspace_handle: exactString(
      wire.workspace_handle,
      identifier,
      "workspace handle",
    ),
    expected_workspace_revision: exactInteger(
      wire.expected_workspace_revision,
      0,
      1_000_000,
      "expected workspace revision",
    ),
    expected_timeline_revision: exactInteger(
      wire.expected_timeline_revision,
      0,
      1_000_000,
      "expected timeline revision",
    ),
    expected_timeline_fingerprint: exactString(
      wire.expected_timeline_fingerprint,
      fingerprint,
      "expected timeline fingerprint",
    ),
    commands: Object.freeze(decodedCommands),
  });
  if (
    new TextEncoder().encode(JSON.stringify(transaction)).length >
    MAX_TRANSACTION_BYTES
  )
    throw new Error("timeline transaction exceeds the byte profile");
  if (transactionWorkUnits(transaction) > MAX_TRANSACTION_WORK_UNITS)
    throw new Error("timeline transaction exceeds the work profile");
  return transaction;
}

export function encodeTimelineTransaction({
  requestId,
  transactionId,
  workspaceHandle,
  expectedWorkspaceRevision,
  expectedTimelineRevision,
  expectedTimelineFingerprint,
  commands,
}: Readonly<{
  requestId: string;
  transactionId: string;
  workspaceHandle: string;
  expectedWorkspaceRevision: number;
  expectedTimelineRevision: number;
  expectedTimelineFingerprint: string;
  commands: readonly Readonly<{
    kind: string;
    payload: Readonly<Record<string, unknown>>;
  }>[];
}>): TimelineTransactionWire {
  return decodeTimelineTransactionWire({
    schema: TIMELINE_TRANSACTION_SCHEMA,
    request_id: requestId,
    transaction_id: transactionId,
    workspace_handle: workspaceHandle,
    expected_workspace_revision: expectedWorkspaceRevision,
    expected_timeline_revision: expectedTimelineRevision,
    expected_timeline_fingerprint: expectedTimelineFingerprint,
    commands,
  });
}

function sortedIdentifiers(value: unknown, name: string): readonly string[] {
  const rows = boundedIdentifiers(value, 128, name);
  if (JSON.stringify(rows) !== JSON.stringify([...rows].sort()))
    throw new Error(`${name} must be sorted and unique`);
  return Object.freeze(rows);
}

function ensureSelectionBelongsToSnapshot(
  selection: readonly string[],
  snapshot: PublicCompositionSnapshot,
): void {
  const clipIds = new Set(snapshot.clips.map((clip) => clip.clipId));
  if (selection.some((clipId) => !clipIds.has(clipId)))
    throw new Error("timeline selection references an unknown snapshot clip");
}

export function decodeTimelineReceipt(value: unknown): TimelineReceipt {
  const wire = object(
    value,
    [
      "schema",
      "request_id",
      "transaction_id",
      "workspace_handle",
      "before_workspace_revision",
      "after_workspace_revision",
      "before_workspace_fingerprint",
      "after_workspace_fingerprint",
      "before_timeline_revision",
      "after_timeline_revision",
      "before_timeline_fingerprint",
      "after_timeline_fingerprint",
      "commands",
      "affected_ids",
      "inverse",
      "history_cursor",
      "selection",
      "snapshot",
    ],
    "timeline receipt",
  );
  if (wire.schema !== TIMELINE_RECEIPT_SCHEMA)
    throw new Error("timeline receipt schema is unsupported");
  const commands = resourceArray(
    wire.commands,
    1,
    MAX_TRANSACTION_COMMANDS,
    "timeline receipt commands",
  ).map((command, index) =>
    decodeTimelineCommand(command, `timeline receipt command[${index}]`),
  );
  const historyKinds = new Set(["undo", "redo", "rebase_transaction"]);
  if (
    commands.some((command) => historyKinds.has(command.kind)) &&
    commands.length !== 1
  )
    throw new Error("timeline receipt history command must be sole");
  const affectedIds = sortedIdentifiers(wire.affected_ids, "affected ids");
  const selection = sortedIdentifiers(wire.selection, "timeline selection");
  const inverseWire = object(
    wire.inverse,
    ["kind", "history_cursor"],
    "timeline receipt inverse",
  );
  if (
    inverseWire.kind !== "restore_transaction_state" &&
    inverseWire.kind !== "undo" &&
    inverseWire.kind !== "redo"
  )
    throw new Error("timeline receipt inverse kind is invalid");
  const cursor = exactHistoryCursor(
    wire.history_cursor,
    "timeline receipt history cursor",
  );
  const inverseCursor = exactHistoryCursor(
    inverseWire.history_cursor,
    "timeline receipt inverse history cursor",
  );
  if (cursor !== inverseCursor)
    throw new Error("timeline receipt inverse cursor is inconsistent");
  const snapshot = decodePublicCompositionSnapshot(wire.snapshot);
  const workspaceHandle = exactString(
    wire.workspace_handle,
    identifier,
    "timeline receipt workspace handle",
  );
  if (snapshot.workspaceHandle !== workspaceHandle)
    throw new Error("timeline receipt contains a cross-workspace snapshot");
  ensureSelectionBelongsToSnapshot(selection, snapshot);
  const beforeWorkspaceRevision = exactInteger(
    wire.before_workspace_revision,
    0,
    1_000_000,
    "timeline receipt before workspace revision",
  );
  const afterWorkspaceRevision = exactInteger(
    wire.after_workspace_revision,
    0,
    1_000_000,
    "timeline receipt after workspace revision",
  );
  const beforeTimelineRevision = exactInteger(
    wire.before_timeline_revision,
    0,
    1_000_000,
    "timeline receipt before timeline revision",
  );
  const afterTimelineRevision = exactInteger(
    wire.after_timeline_revision,
    0,
    1_000_000,
    "timeline receipt after timeline revision",
  );
  const beforeWorkspaceFingerprint = exactString(
    wire.before_workspace_fingerprint,
    fingerprint,
    "timeline receipt before workspace fingerprint",
  );
  const afterWorkspaceFingerprint = exactString(
    wire.after_workspace_fingerprint,
    fingerprint,
    "timeline receipt after workspace fingerprint",
  );
  const beforeTimelineFingerprint = exactString(
    wire.before_timeline_fingerprint,
    fingerprint,
    "timeline receipt before timeline fingerprint",
  );
  const afterTimelineFingerprint = exactString(
    wire.after_timeline_fingerprint,
    fingerprint,
    "timeline receipt after timeline fingerprint",
  );
  if (
    afterWorkspaceRevision !== beforeWorkspaceRevision + 1 ||
    afterWorkspaceRevision !== snapshot.workspaceRevision
  )
    throw new Error(
      "timeline receipt after workspace revision is inconsistent",
    );
  if (
    afterTimelineRevision !== beforeTimelineRevision + 1 ||
    afterTimelineRevision !== snapshot.timelineRevision
  )
    throw new Error("timeline receipt after timeline revision is inconsistent");
  if (
    afterWorkspaceFingerprint !== snapshot.workspaceFingerprint ||
    afterTimelineFingerprint !== snapshot.timelineFingerprint
  )
    throw new Error("timeline receipt after fingerprints are inconsistent");
  return Object.freeze({
    schema: TIMELINE_RECEIPT_SCHEMA,
    requestId: exactString(
      wire.request_id,
      identifier,
      "timeline receipt request id",
    ),
    transactionId: exactString(
      wire.transaction_id,
      identifier,
      "timeline receipt transaction id",
    ),
    workspaceHandle,
    beforeWorkspaceRevision,
    afterWorkspaceRevision,
    beforeWorkspaceFingerprint,
    afterWorkspaceFingerprint,
    beforeTimelineRevision,
    afterTimelineRevision,
    beforeTimelineFingerprint,
    afterTimelineFingerprint,
    commands: Object.freeze(commands),
    affectedIds,
    inverse: Object.freeze({
      kind: inverseWire.kind,
      historyCursor: inverseCursor,
    }),
    historyCursor: cursor,
    selection,
    snapshot,
  });
}

const historyCursorV2 =
  /^h3\.context\.timeline_history_cursor\.v2:[1-9][0-9]{0,6}:[0-9a-f]{64}$/;

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

function exactHistoryCursorV2(value: unknown, name: string): string {
  const cursor = exactString(value, historyCursorV2, name);
  if (Number(cursor.split(":")[1]) > 1_000_000)
    throw new Error(`${name} is invalid`);
  return cursor;
}

export function decodeNleAuthoringStateV2(value: unknown): NleAuthoringStateV2 {
  const wire = object(
    value,
    [
      "schema",
      "profile_id",
      "operation_profile_id",
      "project_id",
      "workspace_handle",
      "workspace_revision",
      "workspace_fingerprint",
      "timeline_revision",
      "timeline_fingerprint",
      "authoring_fingerprint",
      "edit_capacity_frames",
      "content_end_exclusive",
      "assets",
      "tracks",
      "clips",
      "audio_extension",
      "blockers",
    ],
    "NLE authoring state v2",
  );
  if (
    wire.schema !== NLE_AUTHORING_SCHEMA ||
    wire.profile_id !== NLE_AUTHORING_PROFILE_ID ||
    wire.operation_profile_id !== NLE_OPERATION_PROFILE_ID_V2
  )
    throw new Error("NLE authoring schema or profile is unsupported");
  const projectId = exactString(
    wire.project_id,
    identifier,
    "authoring project id",
  );
  const workspaceHandle = exactString(
    wire.workspace_handle,
    identifier,
    "authoring workspace handle",
  );
  const workspaceRevision = exactInteger(
    wire.workspace_revision,
    0,
    1_000_000,
    "authoring workspace revision",
  );
  const timelineRevision = exactInteger(
    wire.timeline_revision,
    0,
    1_000_000,
    "authoring timeline revision",
  );
  const workspaceFingerprint = exactString(
    wire.workspace_fingerprint,
    fingerprint,
    "authoring workspace fingerprint",
  );
  const timelineFingerprint = exactString(
    wire.timeline_fingerprint,
    fingerprint,
    "authoring timeline fingerprint",
  );
  const authoringFingerprint = exactString(
    wire.authoring_fingerprint,
    fingerprint,
    "authoring state fingerprint",
  );
  const editCapacityFrames = exactInteger(
    wire.edit_capacity_frames,
    1,
    3_600,
    "authoring edit capacity",
  );
  if (editCapacityFrames !== 3_600)
    throw new Error("NLE authoring edit capacity is unsupported");
  const assets = Object.freeze(
    resourceArray(wire.assets, 0, 128, "authoring assets").map(
      decodePublicCompositionAsset,
    ),
  );
  if (assets.reduce((sum, asset) => sum + asset.landmarks.length, 0) > 2_048)
    throw new Error("authoring landmarks exceed the profile");
  const tracks = Object.freeze(
    resourceArray(wire.tracks, 1, 8, "authoring tracks").map(
      decodeCompositionTrack,
    ),
  );
  const clips = Object.freeze(
    resourceArray(wire.clips, 0, 128, "authoring clips").map(
      decodeCompositionClip,
    ),
  );
  const audioExtension = decodeCompositionAudioExtension(wire.audio_extension);
  const blockers = Object.freeze(
    resourceArray(wire.blockers, 0, 128, "authoring blockers").map(
      decodeCompositionBlocker,
    ),
  );
  if (
    new Set(assets.map((asset) => asset.assetId)).size !== assets.length ||
    new Set(tracks.map((track) => track.trackId)).size !== tracks.length ||
    new Set(tracks.map((track) => track.order)).size !== tracks.length ||
    new Set(clips.map((clip) => clip.clipId)).size !== clips.length ||
    tracks.filter((track) => track.kind === "primary_video").length !== 1
  )
    throw new Error(
      "authoring asset, track, clip, or order identities are invalid",
    );
  const canonicalTracks = [...tracks].sort(
    (left, right) =>
      left.order - right.order ||
      (left.trackId < right.trackId
        ? -1
        : left.trackId > right.trackId
          ? 1
          : 0),
  );
  if (canonicalJson(canonicalTracks) !== canonicalJson(tracks))
    throw new Error("authoring tracks are not in canonical order");
  const trackOrder = new Map(
    tracks.map((track) => [track.trackId, track.order]),
  );
  if (
    clips.some(
      (clip) =>
        !trackOrder.has(clip.trackId) ||
        clip.startFrame + clip.durationFrames > editCapacityFrames,
    )
  )
    throw new Error("authoring clip exceeds its track or edit capacity");
  const canonicalClips = [...clips].sort(
    (left, right) =>
      (trackOrder.get(left.trackId) ?? 8) -
        (trackOrder.get(right.trackId) ?? 8) ||
      left.startFrame - right.startFrame ||
      (left.clipId < right.clipId ? -1 : left.clipId > right.clipId ? 1 : 0),
  );
  if (canonicalJson(canonicalClips) !== canonicalJson(clips))
    throw new Error("authoring clips are not in canonical order");
  const derivedEnd = clips.reduce(
    (end, clip) => Math.max(end, clip.startFrame + clip.durationFrames),
    0,
  );
  const contentEndExclusive = exactInteger(
    wire.content_end_exclusive,
    0,
    editCapacityFrames,
    "authoring content extent",
  );
  if (contentEndExclusive !== derivedEnd)
    throw new Error("authoring content extent does not match clips");
  if (
    contentEndExclusive === 0 &&
    (clips.length !== 0 ||
      tracks.filter((track) => track.kind === "primary_video").length !== 1)
  )
    throw new Error("empty authoring state is invalid");

  const expectedTimelineFingerprint = compositionContractFingerprint({
    operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
    edit_capacity_frames: editCapacityFrames,
    content_end_exclusive: contentEndExclusive,
    tracks: wire.tracks,
    clips: wire.clips,
    audio_extension: wire.audio_extension,
  });
  const expectedWorkspaceFingerprint = compositionContractFingerprint({
    project_id: projectId,
    workspace_handle: workspaceHandle,
    workspace_revision: workspaceRevision,
    timeline_revision: timelineRevision,
    timeline_fingerprint: timelineFingerprint,
  });
  const authoringMaterial = { ...wire };
  delete authoringMaterial.authoring_fingerprint;
  const expectedAuthoringFingerprint =
    compositionContractFingerprint(authoringMaterial);
  if (
    timelineFingerprint !== expectedTimelineFingerprint ||
    workspaceFingerprint !== expectedWorkspaceFingerprint ||
    authoringFingerprint !== expectedAuthoringFingerprint
  )
    throw new Error("NLE authoring state fingerprints are inconsistent");

  return Object.freeze({
    schema: NLE_AUTHORING_SCHEMA,
    profileId: NLE_AUTHORING_PROFILE_ID,
    operationProfileId: NLE_OPERATION_PROFILE_ID_V2,
    projectId,
    workspaceHandle,
    workspaceRevision,
    workspaceFingerprint,
    timelineRevision,
    timelineFingerprint,
    authoringFingerprint,
    editCapacityFrames,
    contentEndExclusive,
    assets,
    tracks,
    clips,
    audioExtension,
    blockers,
  });
}

function validateNleRenderBoundary(
  authoring: NleAuthoringStateV2,
  renderSnapshot: PublicCompositionSnapshot | null,
): void {
  if ((authoring.contentEndExclusive === 0) !== (renderSnapshot === null))
    throw new Error(
      "NLE render snapshot presence does not match content extent",
    );
  if (renderSnapshot === null) return;
  const durationFrames = renderSnapshot.output.durationFrames;
  if (
    renderSnapshot.workspaceHandle !== authoring.workspaceHandle ||
    renderSnapshot.projectId !== authoring.projectId ||
    renderSnapshot.workspaceRevision !== authoring.workspaceRevision ||
    renderSnapshot.timelineRevision !== authoring.timelineRevision ||
    durationFrames !== authoring.contentEndExclusive ||
    canonicalJson(renderSnapshot.assets) !== canonicalJson(authoring.assets) ||
    canonicalJson(renderSnapshot.tracks) !== canonicalJson(authoring.tracks) ||
    canonicalJson(renderSnapshot.clips) !== canonicalJson(authoring.clips) ||
    canonicalJson(renderSnapshot.audioExtension) !==
      canonicalJson(authoring.audioExtension) ||
    canonicalJson(renderSnapshot.blockers) !== canonicalJson(authoring.blockers)
  )
    throw new Error(
      "V1 render snapshot does not project the accepted NLE authoring state",
    );
}

export function decodeTimelineTransactionV2(
  value: unknown,
): TimelineTransactionV2 {
  const wire = object(
    value,
    [
      "schema",
      "authoring_schema",
      "profile_id",
      "operation_profile_id",
      "request_id",
      "transaction_id",
      "workspace_handle",
      "expected_workspace_revision",
      "expected_timeline_revision",
      "expected_timeline_fingerprint",
      "expected_authoring_fingerprint",
      "commands",
    ],
    "timeline transaction v2",
  );
  if (
    wire.schema !== TIMELINE_TRANSACTION_SCHEMA_V2 ||
    wire.authoring_schema !== NLE_AUTHORING_SCHEMA ||
    wire.profile_id !== NLE_AUTHORING_PROFILE_ID ||
    wire.operation_profile_id !== NLE_OPERATION_PROFILE_ID_V2
  )
    throw new Error("timeline transaction authoring profile is unsupported");
  const commands = resourceArray(
    wire.commands,
    1,
    MAX_TRANSACTION_COMMANDS,
    "timeline transaction v2 commands",
  ).map((command, index) =>
    decodeTimelineCommand(
      command,
      `timeline transaction v2 command[${index}]`,
      2,
    ),
  );
  const historyKinds = new Set(["undo", "redo", "rebase_transaction"]);
  if (
    commands.some((command) => historyKinds.has(command.kind)) &&
    commands.length !== 1
  )
    throw new Error("a history command must be the sole command");
  const transaction = Object.freeze({
    schema: TIMELINE_TRANSACTION_SCHEMA_V2,
    authoring_schema: NLE_AUTHORING_SCHEMA,
    profile_id: NLE_AUTHORING_PROFILE_ID,
    operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
    request_id: exactString(wire.request_id, identifier, "request id"),
    transaction_id: exactString(
      wire.transaction_id,
      identifier,
      "transaction id",
    ),
    workspace_handle: exactString(
      wire.workspace_handle,
      identifier,
      "workspace handle",
    ),
    expected_workspace_revision: exactInteger(
      wire.expected_workspace_revision,
      0,
      1_000_000,
      "expected workspace revision",
    ),
    expected_timeline_revision: exactInteger(
      wire.expected_timeline_revision,
      0,
      1_000_000,
      "expected timeline revision",
    ),
    expected_timeline_fingerprint: exactString(
      wire.expected_timeline_fingerprint,
      fingerprint,
      "expected timeline fingerprint",
    ),
    expected_authoring_fingerprint: exactString(
      wire.expected_authoring_fingerprint,
      fingerprint,
      "expected authoring fingerprint",
    ),
    commands: Object.freeze(commands),
  });
  if (
    new TextEncoder().encode(JSON.stringify(transaction)).length >
      MAX_TRANSACTION_BYTES ||
    transactionWorkUnits(transaction) > MAX_TRANSACTION_WORK_UNITS
  )
    throw new Error("timeline transaction exceeds its bounded profile");
  return transaction;
}

export function encodeTimelineTransactionV2({
  requestId,
  transactionId,
  workspaceHandle,
  expectedWorkspaceRevision,
  expectedTimelineRevision,
  expectedTimelineFingerprint,
  expectedAuthoringFingerprint,
  commands,
}: Readonly<{
  requestId: string;
  transactionId: string;
  workspaceHandle: string;
  expectedWorkspaceRevision: number;
  expectedTimelineRevision: number;
  expectedTimelineFingerprint: string;
  expectedAuthoringFingerprint: string;
  commands: readonly Readonly<{
    kind: string;
    payload: Readonly<Record<string, unknown>>;
  }>[];
}>): TimelineTransactionV2 {
  return decodeTimelineTransactionV2({
    schema: TIMELINE_TRANSACTION_SCHEMA_V2,
    authoring_schema: NLE_AUTHORING_SCHEMA,
    profile_id: NLE_AUTHORING_PROFILE_ID,
    operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
    request_id: requestId,
    transaction_id: transactionId,
    workspace_handle: workspaceHandle,
    expected_workspace_revision: expectedWorkspaceRevision,
    expected_timeline_revision: expectedTimelineRevision,
    expected_timeline_fingerprint: expectedTimelineFingerprint,
    expected_authoring_fingerprint: expectedAuthoringFingerprint,
    commands,
  });
}

export function decodeTimelineReceiptV2(value: unknown): TimelineReceiptV2 {
  const wire = object(
    value,
    [
      "schema",
      "request_id",
      "transaction_id",
      "workspace_handle",
      "before_authoring_fingerprint",
      "after_authoring_fingerprint",
      "before_workspace_revision",
      "after_workspace_revision",
      "before_workspace_fingerprint",
      "after_workspace_fingerprint",
      "before_timeline_revision",
      "after_timeline_revision",
      "before_timeline_fingerprint",
      "after_timeline_fingerprint",
      "commands",
      "affected_ids",
      "inverse",
      "history_cursor",
      "selection",
      "authoring",
      "render_snapshot",
    ],
    "timeline receipt v2",
  );
  if (wire.schema !== TIMELINE_RECEIPT_SCHEMA_V2)
    throw new Error("timeline receipt v2 schema is unsupported");
  const authoring = decodeNleAuthoringStateV2(wire.authoring);
  const renderSnapshot =
    wire.render_snapshot === null
      ? null
      : decodePublicCompositionSnapshot(wire.render_snapshot);
  validateNleRenderBoundary(authoring, renderSnapshot);
  const workspaceHandle = exactString(
    wire.workspace_handle,
    identifier,
    "receipt workspace handle",
  );
  if (workspaceHandle !== authoring.workspaceHandle)
    throw new Error(
      "timeline receipt v2 contains a cross-workspace authoring state",
    );
  const commands = resourceArray(
    wire.commands,
    1,
    MAX_TRANSACTION_COMMANDS,
    "receipt commands",
  ).map((command, index) =>
    decodeTimelineCommand(command, `timeline receipt v2 command[${index}]`, 2),
  );
  const historyKinds = new Set(["undo", "redo", "rebase_transaction"]);
  if (
    commands.some((command) => historyKinds.has(command.kind)) &&
    commands.length !== 1
  )
    throw new Error("timeline receipt v2 history command must be sole");
  const affectedIds = sortedIdentifiers(
    wire.affected_ids,
    "receipt affected ids",
  );
  const selection = sortedIdentifiers(wire.selection, "receipt selection");
  const clipIds = new Set(authoring.clips.map((clip) => clip.clipId));
  if (selection.some((clipId) => !clipIds.has(clipId)))
    throw new Error("timeline receipt v2 selection references an unknown clip");
  const inverseWire = wire.inverse as Record<string, unknown>;
  if (inverseWire?.kind === "restore_selection") {
    object(inverseWire, ["kind", "selection"], "timeline receipt v2 inverse");
    sortedIdentifiers(inverseWire.selection, "receipt inverse selection");
  } else {
    const inverse = object(
      inverseWire,
      ["kind", "history_cursor"],
      "timeline receipt v2 inverse",
    );
    if (
      !["restore_transaction_state", "undo", "redo"].includes(
        String(inverse.kind),
      )
    )
      throw new Error("timeline receipt v2 inverse kind is invalid");
    exactHistoryCursorV2(
      inverse.history_cursor,
      "receipt inverse history cursor",
    );
  }
  const historyCursor =
    wire.history_cursor === null
      ? null
      : exactHistoryCursorV2(wire.history_cursor, "receipt history cursor");
  if ((historyCursor === null) !== (inverseWire.kind === "restore_selection"))
    throw new Error("timeline receipt v2 inverse cursor is inconsistent");
  const beforeWorkspaceRevision = exactInteger(
    wire.before_workspace_revision,
    0,
    1_000_000,
    "receipt before workspace revision",
  );
  const afterWorkspaceRevision = exactInteger(
    wire.after_workspace_revision,
    0,
    1_000_000,
    "receipt after workspace revision",
  );
  const beforeTimelineRevision = exactInteger(
    wire.before_timeline_revision,
    0,
    1_000_000,
    "receipt before timeline revision",
  );
  const afterTimelineRevision = exactInteger(
    wire.after_timeline_revision,
    0,
    1_000_000,
    "receipt after timeline revision",
  );
  const beforeWorkspaceFingerprint = exactString(
    wire.before_workspace_fingerprint,
    fingerprint,
    "receipt before workspace fingerprint",
  );
  const afterWorkspaceFingerprint = exactString(
    wire.after_workspace_fingerprint,
    fingerprint,
    "receipt after workspace fingerprint",
  );
  const beforeTimelineFingerprint = exactString(
    wire.before_timeline_fingerprint,
    fingerprint,
    "receipt before timeline fingerprint",
  );
  const afterTimelineFingerprint = exactString(
    wire.after_timeline_fingerprint,
    fingerprint,
    "receipt after timeline fingerprint",
  );
  const beforeAuthoringFingerprint = exactString(
    wire.before_authoring_fingerprint,
    fingerprint,
    "receipt before authoring fingerprint",
  );
  const afterAuthoringFingerprint = exactString(
    wire.after_authoring_fingerprint,
    fingerprint,
    "receipt after authoring fingerprint",
  );
  const revisionDelta = historyCursor === null ? 0 : 1;
  const expectedBeforeWorkspaceFingerprint = compositionContractFingerprint({
    project_id: authoring.projectId,
    workspace_handle: workspaceHandle,
    workspace_revision: beforeWorkspaceRevision,
    timeline_revision: beforeTimelineRevision,
    timeline_fingerprint: beforeTimelineFingerprint,
  });
  if (
    afterWorkspaceRevision !== beforeWorkspaceRevision + revisionDelta ||
    afterTimelineRevision !== beforeTimelineRevision + revisionDelta ||
    afterWorkspaceRevision !== authoring.workspaceRevision ||
    afterTimelineRevision !== authoring.timelineRevision ||
    afterWorkspaceFingerprint !== authoring.workspaceFingerprint ||
    afterTimelineFingerprint !== authoring.timelineFingerprint ||
    afterAuthoringFingerprint !== authoring.authoringFingerprint ||
    beforeWorkspaceFingerprint !== expectedBeforeWorkspaceFingerprint ||
    (historyCursor === null &&
      (beforeWorkspaceFingerprint !== afterWorkspaceFingerprint ||
        beforeTimelineFingerprint !== afterTimelineFingerprint ||
        beforeAuthoringFingerprint !== afterAuthoringFingerprint))
  )
    throw new Error(
      "timeline receipt v2 revision or fingerprint transition is inconsistent",
    );
  return Object.freeze({
    schema: TIMELINE_RECEIPT_SCHEMA_V2,
    requestId: exactString(wire.request_id, identifier, "receipt request id"),
    transactionId: exactString(
      wire.transaction_id,
      identifier,
      "receipt transaction id",
    ),
    workspaceHandle,
    beforeWorkspaceRevision,
    afterWorkspaceRevision,
    beforeWorkspaceFingerprint,
    afterWorkspaceFingerprint,
    beforeTimelineRevision,
    afterTimelineRevision,
    beforeTimelineFingerprint,
    afterTimelineFingerprint,
    commands: Object.freeze(commands),
    affectedIds,
    inverse:
      inverseWire.kind === "restore_selection"
        ? Object.freeze({
            kind: "restore_selection",
            selection: sortedIdentifiers(
              inverseWire.selection,
              "receipt inverse selection",
            ),
          })
        : Object.freeze({
            kind: String(inverseWire.kind),
            historyCursor: exactHistoryCursorV2(
              inverseWire.history_cursor,
              "receipt inverse history cursor",
            ),
          }),
    historyCursor,
    selection,
    authoring,
    renderSnapshot,
    beforeAuthoringFingerprint,
    afterAuthoringFingerprint,
  });
}

export function decodeTimelineHistoryProjection(
  value: unknown,
): TimelineHistoryProjection {
  const wire = object(
    value,
    [
      "schema",
      "workspace_handle",
      "snapshot",
      "selection",
      "undo_cursor",
      "redo_cursor",
      "rejection",
    ],
    "timeline history projection",
  );
  if (wire.schema !== TIMELINE_HISTORY_PROJECTION_SCHEMA)
    throw new Error("timeline history projection schema is unsupported");
  const workspaceHandle = exactString(
    wire.workspace_handle,
    identifier,
    "timeline history workspace handle",
  );
  const snapshot = decodePublicCompositionSnapshot(wire.snapshot);
  if (snapshot.workspaceHandle !== workspaceHandle)
    throw new Error(
      "timeline history projection contains a cross-workspace snapshot",
    );
  const selection = sortedIdentifiers(
    wire.selection,
    "timeline history selection",
  );
  ensureSelectionBelongsToSnapshot(selection, snapshot);
  const undoCursor =
    wire.undo_cursor === null
      ? null
      : exactHistoryCursor(wire.undo_cursor, "timeline history undo cursor");
  const redoCursor =
    wire.redo_cursor === null
      ? null
      : exactHistoryCursor(wire.redo_cursor, "timeline history redo cursor");
  if (undoCursor !== null && undoCursor === redoCursor)
    throw new Error("timeline history branch cursors must differ");
  let rejection: Readonly<{ code: string }> | null = null;
  if (wire.rejection !== null) {
    const rejectionWire = object(
      wire.rejection,
      ["code"],
      "timeline history rejection",
    );
    if (
      typeof rejectionWire.code !== "string" ||
      !historyRejectionCodes.has(rejectionWire.code)
    )
      throw new Error("timeline history rejection code is invalid");
    rejection = Object.freeze({ code: rejectionWire.code });
  }
  return Object.freeze({
    schema: TIMELINE_HISTORY_PROJECTION_SCHEMA,
    workspaceHandle,
    snapshot,
    selection,
    undoCursor,
    redoCursor,
    rejection,
  });
}

export function decodeTimelineHistoryProjectionV2(
  value: unknown,
): TimelineHistoryProjectionV2 {
  const wire = object(
    value,
    [
      "schema",
      "workspace_handle",
      "authoring",
      "render_snapshot",
      "selection",
      "undo_cursor",
      "redo_cursor",
      "rejection",
    ],
    "timeline history projection v2",
  );
  if (wire.schema !== TIMELINE_HISTORY_PROJECTION_SCHEMA_V2)
    throw new Error("timeline history projection v2 schema is unsupported");
  const workspaceHandle = exactString(
    wire.workspace_handle,
    identifier,
    "timeline history workspace handle",
  );
  const authoring = decodeNleAuthoringStateV2(wire.authoring);
  const renderSnapshot =
    wire.render_snapshot === null
      ? null
      : decodePublicCompositionSnapshot(wire.render_snapshot);
  validateNleRenderBoundary(authoring, renderSnapshot);
  if (authoring.workspaceHandle !== workspaceHandle)
    throw new Error(
      "timeline history projection contains a cross-workspace state",
    );
  const selection = sortedIdentifiers(
    wire.selection,
    "timeline history selection",
  );
  const clipIds = new Set(authoring.clips.map((clip) => clip.clipId));
  if (selection.some((clipId) => !clipIds.has(clipId)))
    throw new Error("timeline selection references an unknown authoring clip");
  const undoCursor =
    wire.undo_cursor === null
      ? null
      : exactHistoryCursorV2(wire.undo_cursor, "timeline history undo cursor");
  const redoCursor =
    wire.redo_cursor === null
      ? null
      : exactHistoryCursorV2(wire.redo_cursor, "timeline history redo cursor");
  if (undoCursor !== null && undoCursor === redoCursor)
    throw new Error("timeline history branch cursors must differ");
  let rejection: Readonly<{ code: string }> | null = null;
  if (wire.rejection !== null) {
    const rejectionWire = object(
      wire.rejection,
      ["code"],
      "timeline history rejection",
    );
    if (
      typeof rejectionWire.code !== "string" ||
      !historyRejectionCodes.has(rejectionWire.code)
    )
      throw new Error("timeline history rejection code is invalid");
    rejection = Object.freeze({ code: rejectionWire.code });
  }
  return Object.freeze({
    schema: TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
    workspaceHandle,
    authoring,
    renderSnapshot,
    selection,
    undoCursor,
    redoCursor,
    rejection,
  });
}

export type AuthoringActionPayload = Readonly<Record<string, unknown>>;

export function authoringMoveGroupPayload({
  workspaceHandle,
  expectedTimelineRevision,
  clipIds,
  deltaFrames,
}: Readonly<{
  workspaceHandle: string;
  expectedTimelineRevision: number;
  clipIds: readonly string[];
  deltaFrames: number;
}>): AuthoringActionPayload {
  return Object.freeze({
    workspace_handle: workspaceHandle,
    expected_timeline_revision: expectedTimelineRevision,
    clip_ids: Object.freeze([...clipIds]),
    delta_frames: deltaFrames,
  });
}

export function encodeAuthoringAction(
  requestId: string,
  action: AuthoringAction,
  payload: AuthoringActionPayload,
): Readonly<{
  schema: typeof AUTHORING_ACTION_SCHEMA;
  request_id: string;
  action: AuthoringAction;
  payload: AuthoringActionPayload;
}> {
  if (!identifier.test(requestId)) throw new Error("request id is invalid");
  let encodedPayload = payload;
  if (action === "apply_timeline_transaction") {
    const transaction =
      payload.schema === TIMELINE_TRANSACTION_SCHEMA_V2
        ? decodeTimelineTransactionV2(payload)
        : decodeTimelineTransactionWire(payload);
    if (transaction.request_id !== requestId)
      throw new Error(
        "timeline transaction request id must match the outer request id",
      );
    encodedPayload = transaction;
  }
  if (action === "read_timeline_history") {
    const historyRead = object(
      payload,
      ["workspace_handle"],
      "timeline history read payload",
    );
    exactString(
      historyRead.workspace_handle,
      identifier,
      "timeline history workspace handle",
    );
  }
  if (action === "ensure_authoring_from_production") {
    const production = object(
      payload,
      [
        "production_workspace_handle",
        "production_workspace_id",
        "preferred_authoring_handle",
      ],
      "Production authoring preparation payload",
    );
    encodedPayload = Object.freeze({
      production_workspace_handle: exactString(
        production.production_workspace_handle,
        identifier,
        "Production workspace handle",
      ),
      production_workspace_id: exactString(
        production.production_workspace_id,
        identifier,
        "Production workspace ID",
      ),
      preferred_authoring_handle:
        production.preferred_authoring_handle === null
          ? null
          : exactString(
              production.preferred_authoring_handle,
              identifier,
              "preferred Authoring workspace handle",
            ),
    });
  }
  if (action === "initialize_timeline_history") {
    const initialization = object(
      payload,
      [
        "workspace_handle",
        "expected_reference_revision",
        "expected_timeline_revision",
        "authoring_schema",
        "profile_id",
        "operation_profile_id",
      ],
      "timeline history initialization payload",
    );
    encodedPayload = Object.freeze({
      workspace_handle: exactString(
        initialization.workspace_handle,
        identifier,
        "timeline history initialization workspace handle",
      ),
      expected_reference_revision: exactInteger(
        initialization.expected_reference_revision,
        1,
        1_000_000,
        "timeline history initialization expected reference revision",
      ),
      expected_timeline_revision: exactInteger(
        initialization.expected_timeline_revision,
        1,
        1_000_000,
        "timeline history initialization expected timeline revision",
      ),
      authoring_schema: exactString(
        initialization.authoring_schema,
        identifier,
        "timeline history authoring schema",
      ),
      profile_id: exactString(
        initialization.profile_id,
        identifier,
        "timeline history authoring profile",
      ),
      operation_profile_id: exactString(
        initialization.operation_profile_id,
        identifier,
        "timeline history operation profile",
      ),
    });
    if (
      encodedPayload.authoring_schema !== NLE_AUTHORING_SCHEMA ||
      encodedPayload.profile_id !== NLE_AUTHORING_PROFILE_ID ||
      encodedPayload.operation_profile_id !== NLE_OPERATION_PROFILE_ID_V2
    )
      throw new Error("timeline history initialization profile is unsupported");
  }
  return Object.freeze({
    schema: AUTHORING_ACTION_SCHEMA,
    request_id: requestId,
    action,
    payload: Object.freeze({ ...encodedPayload }),
  });
}
