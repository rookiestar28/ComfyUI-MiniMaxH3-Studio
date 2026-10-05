import type {
  AuthoringProjection,
  TimelineCommandWire,
  TimelineHistoryProjection,
  TimelineHistoryProjectionV2,
  TimelineReceipt,
  TimelineReceiptV2,
} from "../contracts/authoringWorkbenchCodec";

// These session contracts outlive the retired React editor. Keep them in a neutral,
// type-only module so timeline reconciliation cannot accidentally acquire a component runtime.
export type AuthoringViewState =
  | Readonly<{ status: "absent" | "loading" | "released" }>
  | Readonly<{
      status: "ready" | "pending" | "conflict";
      projection: AuthoringProjection;
      timelineHistory?: TimelineHistoryProjection;
      timelineHistoryV2?: TimelineHistoryProjectionV2;
      lastTimelineReceipt?: TimelineReceipt;
      lastTimelineReceiptV2?: TimelineReceiptV2;
    }>
  | Readonly<{
      status: "error" | "gone";
      projection?: AuthoringProjection;
      reason: string;
      timelineHistory?: TimelineHistoryProjection;
      timelineHistoryV2?: TimelineHistoryProjectionV2;
      lastTimelineReceipt?: TimelineReceipt;
      lastTimelineReceiptV2?: TimelineReceiptV2;
      /** Present only with `reason: "outcome_unknown"`: the transaction whose reply was lost. */
      outcomeUnknown?: UnknownTimelineOutcome;
    }>;

/**
 * Identity of a timeline transaction whose outcome the backend never established, plus the state
 * of the one bounded read that reconciles it. The identity is retained for attribution only: the
 * session re-reads history exactly once and never re-sends the transaction. `reconciliation`
 * exists so no surface can claim a completed re-read while the read is pending or after it
 * failed; a read that succeeds leaves this state entirely.
 */
export type UnknownTimelineOutcome = Readonly<{
  requestId: string;
  transactionId: string;
  expectedWorkspaceRevision: number;
  expectedTimelineRevision: number;
  expectedTimelineFingerprint: string;
  expectedAuthoringFingerprint?: string;
  reconciliation: "pending" | "failed";
}>;

export type AuthoringIntent =
  | Readonly<{ action: "create_authoring_workspace" }>
  | Readonly<{ action: "read_projection" }>
  | Readonly<{ action: "read_timeline_history" }>
  | Readonly<{ action: "initialize_timeline_history" }>
  | Readonly<{
      action: "apply_timeline_commands";
      commands: readonly TimelineCommandWire[];
      capturedTimeline?: Readonly<{
        workspaceHandle: string;
        workspaceRevision: number;
        timelineRevision: number;
        timelineFingerprint: string;
        authoringFingerprint?: string;
      }>;
    }>
  | Readonly<{ action: "release_workspace" }>
  | Readonly<{ action: "refresh_availability" }>
  | Readonly<{ action: "add_source"; sourceId: string }>
  | Readonly<{ action: "remove_source"; sourceId: string }>
  | Readonly<{ action: "reorder_source"; sourceId: string; newIndex: number }>
  | Readonly<{ action: "include_soundtrack"; videoId: string; audioId: string }>
  | Readonly<{ action: "exclude_soundtrack"; videoId: string }>
  | Readonly<{
      action: "add_clip";
      assetId: string;
      lane: number;
      startFrame: number;
      frames: number;
      sourceStartFrame: number;
    }>
  | Readonly<{ action: "remove_clip"; clipId: string }>
  | Readonly<{
      action: "move_clip";
      clipId: string;
      deltaFrames: number;
      deltaLanes: number;
    }>
  | Readonly<{
      action: "move_group";
      clipIds: readonly string[];
      deltaFrames: number;
    }>
  | Readonly<{ action: "snap_clip"; clipId: string }>
  | Readonly<{
      action: "trim_clip";
      clipId: string;
      edge: "start" | "end";
      deltaFrames: number;
    }>
  | Readonly<{ action: "split_clip"; clipId: string; atOffsetFrames: number }>
  | Readonly<{
      action: "merge_clips";
      firstClipId: string;
      secondClipId: string;
    }>
  | Readonly<{ action: "link_clips"; videoClipId: string; audioClipId: string }>
  | Readonly<{ action: "unlink_clips"; videoClipId: string }>
  | Readonly<{ action: "select_clips"; clipIds: readonly string[] }>;
