// Pure session state projected by the NLE workspace.
//
// Everything here is mount memory. It holds opaque identifiers, revisions, fingerprints,
// enumerated statuses, view geometry and explicit user script/storyboard drafts. No media bytes,
// paths, URLs, credentials or provider payloads. Draft persistence requires explicit consent.

import type { NleSurfaceCapability } from "../contracts/nleSurfaceCapability";
import type { ManagedReadiness } from "../contracts/managedQualificationCodec";
import type {
  AutomaticPlanProjection,
  PlanningProjection,
  SegmentationPolicy,
} from "../contracts/productionPlanningCodec";
import type {
  ProductionAuthoringImportReceipt,
  ProductionAuthoringImportReceiptV2,
  ProductionAuthoringImportRequest,
} from "../contracts/productionAuthoringImportCodec";
import type { ProductionAuthoringImportRefusal } from "../host/productionAuthoringImportClient";
import type { OutputCapability } from "../contracts/authoringOutputCodec";
import type { ManagedSequenceProjection } from "../host/managedSequenceClient";
import type { ManagedSerialReattachResult } from "../host/managedSequenceRunnerContract";
import type {
  OverlayBounds,
  OverlayInternalPane,
} from "../runtime/nleOverlayGeometry";
import {
  DEFAULT_NLE_LAYOUT,
  type NleLayout,
} from "../runtime/nleLayoutGeometry";

export const NLE_CLOSE_REASONS = Object.freeze([
  "explicit_close",
  "escape",
  "function_switch",
  "top_level_navigation",
  "capability_or_mount_failure",
  "view_destroy",
] as const);
export type NleCloseReason = (typeof NLE_CLOSE_REASONS)[number];

export type NleSurfaceStatus =
  | "compact_ready"
  | "opening"
  | "expanded"
  | "closing"
  | "compact_unsupported"
  | "disposed";

export type NleSurfaceState = Readonly<{
  status: NleSurfaceStatus;
  capability: NleSurfaceCapability | null;
  bounds: OverlayBounds;
  pane: OverlayInternalPane;
  /** M25-44: the three splitter positions as workspace shares; resolved against the stage. */
  layout: NleLayout;
  /** The last recorded close reason; cleared when the next open succeeds. */
  lastCloseReason: NleCloseReason | null;
  /** Monotonic open generation so late async work cannot remount a closed view. */
  generation: number;
}>;

export type NleImportStatus =
  | "idle"
  | "ensuring_target"
  | "importing"
  | "succeeded"
  | "uncertain"
  | "refused";

export type NleImportState = Readonly<{
  status: NleImportStatus;
  editorStatus: "unverified" | "ready" | "needs_action";
  requestId: string | null;
  /** The exact captured request retained for explicit same-ID replay after a lost response. */
  request: ProductionAuthoringImportRequest | null;
  receipt:
    | ProductionAuthoringImportReceipt
    | ProductionAuthoringImportReceiptV2
    | null;
  refusal: ProductionAuthoringImportRefusal | "target_unavailable" | null;
  highlightedAssetIds: readonly string[];
}>;

export type NlePlanningStatus =
  | "idle"
  | "preparing"
  | "prepared"
  | "admitting"
  | "admitted"
  | "proposing"
  | "proposed"
  | "importing"
  | "imported"
  | "error";

export type StoryboardShotDraft = Readonly<{
  shotId: string;
  ordinal: number;
  startMilliseconds: number;
  endMilliseconds: number;
  text: string;
  hardBoundary: boolean;
}>;

export type NlePlanningState = Readonly<{
  /** User draft; session memory unless explicitly exported or recovery is enabled. */
  script?: string;
  targetSeconds: number;
  policy: SegmentationPolicy;
  status: NlePlanningStatus;
  projection: PlanningProjection | null;
  plan: AutomaticPlanProjection | null;
  /** Identity of the Production workspace the projection/plan were derived from. */
  boundWorkspaceFingerprint: string | null;
  storyboardReviewOpen: boolean;
  storyboardRows: readonly StoryboardShotDraft[];
  error: string | null;
}>;

export type NleReadinessStatus =
  "idle" | "requesting" | "held" | "ready" | "error";

export type NleReadinessState = Readonly<{
  status: NleReadinessStatus;
  readiness: ManagedReadiness | null;
  boundPlanFingerprint: string | null;
  error: string | null;
}>;

export type NleSequenceUiState =
  | "idle"
  | "starting"
  | "attached_current_child"
  | "detach_ready"
  | "detaching"
  | "safe_to_leave"
  | "reattach_reconciling"
  | "current_segment_completed"
  | "current_segment_still_owned"
  | "paused_unknown_ownership"
  | "resume_ready"
  | "recovery_unavailable_or_expired"
  | "finished"
  | "failed";

export type NleSequenceState = Readonly<{
  ui: NleSequenceUiState;
  parentSequenceId: string | null;
  recoveryExpiresAtEpochMs: number | null;
  projection: ManagedSequenceProjection | null;
  etag: string | null;
  reattach: ManagedSerialReattachResult | null;
  failure: string | null;
  polling: boolean;
  busy: boolean;
}>;

/** A one-shot request for the sidebar to select a Production function (consumed by generation). */
export type NleFunctionRequest = Readonly<{
  id: "clip_editor" | "production_workbench";
  generation: number;
}>;

/** The M25-19 render capability as last read; `null` until the overlay reads it. */
export type NleRenderState = Readonly<{
  status: "unread" | "reading" | "read";
  capability: OutputCapability | null;
}>;

export type NleWorkspaceState = Readonly<{
  surface: NleSurfaceState;
  import: NleImportState;
  planning: NlePlanningState;
  readiness: NleReadinessState;
  sequence: NleSequenceState;
  functionRequest: NleFunctionRequest;
  render: NleRenderState;
}>;

export const NLE_DEFAULT_TARGET_SECONDS = 60;
export const NLE_MAX_TARGET_SECONDS = 60;
export const NLE_MIN_TARGET_SECONDS = 4;

export const initialNleSurfaceState: NleSurfaceState = Object.freeze({
  status: "compact_ready",
  capability: null,
  bounds: Object.freeze({ width: 0, height: 0 }),
  pane: "assets",
  layout: DEFAULT_NLE_LAYOUT,
  lastCloseReason: null,
  generation: 0,
});

export const initialNleImportState: NleImportState = Object.freeze({
  status: "idle",
  editorStatus: "unverified",
  requestId: null,
  request: null,
  receipt: null,
  refusal: null,
  highlightedAssetIds: Object.freeze([]),
});

export const initialNlePlanningState: NlePlanningState = Object.freeze({
  script: "",
  targetSeconds: NLE_DEFAULT_TARGET_SECONDS,
  policy: "auto_storyboard",
  status: "idle",
  projection: null,
  plan: null,
  boundWorkspaceFingerprint: null,
  storyboardReviewOpen: false,
  storyboardRows: Object.freeze([]),
  error: null,
});

export const initialNleReadinessState: NleReadinessState = Object.freeze({
  status: "idle",
  readiness: null,
  boundPlanFingerprint: null,
  error: null,
});

export const initialNleSequenceState: NleSequenceState = Object.freeze({
  ui: "idle",
  parentSequenceId: null,
  recoveryExpiresAtEpochMs: null,
  projection: null,
  etag: null,
  reattach: null,
  failure: null,
  polling: false,
  busy: false,
});

export const initialNleWorkspaceState: NleWorkspaceState = Object.freeze({
  surface: initialNleSurfaceState,
  import: initialNleImportState,
  planning: initialNlePlanningState,
  readiness: initialNleReadinessState,
  sequence: initialNleSequenceState,
  functionRequest: Object.freeze({ id: "clip_editor", generation: 0 }),
  render: Object.freeze({ status: "unread", capability: null }),
});

export function clampTargetSeconds(value: number): number {
  if (!Number.isFinite(value)) return NLE_DEFAULT_TARGET_SECONDS;
  return Math.min(
    NLE_MAX_TARGET_SECONDS,
    Math.max(NLE_MIN_TARGET_SECONDS, Math.round(value)),
  );
}

/**
 * A planning projection or plan is stale once the Production workspace it was derived from
 * has moved: a material edit invalidates storyboard admission and proposal approval.
 */
export function planningIsStale(
  planning: NlePlanningState,
  currentWorkspaceFingerprint: string | undefined,
): boolean {
  return (
    planning.boundWorkspaceFingerprint !== null &&
    planning.boundWorkspaceFingerprint !== currentWorkspaceFingerprint
  );
}

export function readinessIsStale(
  readiness: NleReadinessState,
  planFingerprint: string | undefined,
): boolean {
  return (
    readiness.boundPlanFingerprint !== null &&
    readiness.boundPlanFingerprint !== planFingerprint
  );
}
