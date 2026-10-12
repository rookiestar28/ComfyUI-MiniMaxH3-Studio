import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../../state/authoringViewState";
import type {
  CompositionClip,
  PublicCompositionSnapshot,
} from "../../contracts/compositionCodec";
import type { Locale } from "../../i18n/catalog";
import type { SidebarRetention } from "../../state/sidebarRetention";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaSourceLeaseClient,
} from "../../host/authoringMediaSourceLease";
import type { NleDecorationDemandBroker } from "../../host/nleDecorationDemandBroker";
import type { NleBinInsertChannel } from "../../runtime/nleBinInsertChannel";
import type { TrimGestureState } from "../../runtime/nleTrimGesture";
import type { TimelineRebaseAttempt } from "./nleTimelineTools";

export const NLE_ROW_HEIGHT_PX = 56;
export const NLE_MAX_VISIBLE_ROWS = 8;
export const NLE_MAX_CLIP_NODES = 96;
export const DRAG_THRESHOLD_PX = 4;
// the clip box is inset 4 px in its 56 px row and has a 1 px border; the raster paints
// inside it, so the filmstrip fills the clip and the waveform is its bottom 14 px band.
export const CLIP_CONTENT_TOP_PX = 5;
export const CLIP_CONTENT_HEIGHT_PX =
  NLE_ROW_HEIGHT_PX - 2 * CLIP_CONTENT_TOP_PX;
export const WAVEFORM_BAND_HEIGHT_PX = 14;
/**
 * The dialog's 44 px resize corner covers the bottom-right of this region; the rail keeps 54 px
 * clear of it (`.h3-nle-trim-rail`), and so does the scroll bar's track.
 */
export const RESIZE_CORNER_CLEARANCE_PX = 54;
/** R2: the roll target under each pointer mode (`controlTargetPx.minimum` / `.coarse`). */
export const ROLL_TARGET_PX = { fine: 24, coarse: 44 } as const;

export type NleTimelineTransportSnapshot = Readonly<{
  frame: number | null;
  request: Readonly<{ generation: number; frame: number }> | null;
  settledRequestGeneration: number;
  transportAvailable: boolean;
  navigationAvailable?: boolean;
}>;

export type NleTimelineTransportChannel = Readonly<{
  snapshot(): NleTimelineTransportSnapshot;
  subscribe(listener: () => void): () => void;
}>;

export type NleTimelineShortcutHandler = (
  event: ReactKeyboardEvent<HTMLElement>,
) => void;

export type NleTimelineProps = Readonly<{
  locale: Locale;
  snapshot: PublicCompositionSnapshot;
  selection: readonly string[];
  authoring: AuthoringViewState;
  gridFrames: number;
  /** V2 edits run to fixed capacity while render snapshots stay content-sized. */
  editCapacityFrames?: number;
  /** transport navigation ends at accepted content, independently of edit capacity. */
  contentEndExclusive?: number;
  playheadFrame: number | null;
  /** The accepted monitor session is the sole authority for whether ruler seeking is legal. */
  transportAvailable?: boolean;
  /** Logical authoring navigation remains available when media presentation is blocked. */
  navigationAvailable?: boolean;
  /**
   * The monitor's high-frequency projection. It updates owned DOM without scheduling a timeline
   * React commit; edits and playback therefore retain their accepted render budgets.
   */
  transportChannel?: NleTimelineTransportChannel;
  highlightedAssetIds: readonly string[];
  onIntent(intent: AuthoringIntent): Promise<void>;
  onEdgeGestureActive(active: boolean): void;
  onTrimState?(state: TrimGestureState): void;
  onBeginTrim?(): void;
  onSeek?(frame: number): void;
  onBeginScrub?(): void;
  onTogglePlay?(): void;
  onStep?(direction: -1 | 1): void;
  bindShortcuts?(handler: NleTimelineShortcutHandler | null): () => void;
  /**  exact failed command/base provenance, owned by the workspace dispatch seam. */
  rebaseAttempt?: TimelineRebaseAttempt | null;
  /** zoom, snap and scroll survive view release for the same workspace. */
  retention?: SidebarRetention;
  binInsert?: NleBinInsertChannel;
  decorationDemandBroker?: NleDecorationDemandBroker<
    AuthoringMediaAssetDecoration["value"]
  >;
  leaseClient?: AuthoringMediaSourceLeaseClient;
  runtimeEpoch?: number;
}>;

export function clipEnd(clip: CompositionClip): number {
  return clip.startFrame + clip.durationFrames;
}

export function timelineAuthoringContext(
  authoring: AuthoringViewState,
  snapshot: PublicCompositionSnapshot,
) {
  const authoringTimelineState =
    "timelineHistoryV2" in authoring
      ? authoring.timelineHistoryV2?.authoring
      : undefined;
  // CRITICAL: the V1 render snapshot can carry a different timeline fingerprint from V2
  // authoring after an accepted edit. V2 commands must keep the V2 CAS identity or Undo/Redo
  // and later edits are rejected as stale_timeline_fingerprint.
  const timelineAuthority = authoringTimelineState ?? snapshot;
  const authoringFingerprint = authoringTimelineState?.authoringFingerprint;
  const capturedAuthoringIdentity =
    authoringFingerprint === undefined ? {} : { authoringFingerprint };
  const timelineHistoryV2 =
    "timelineHistoryV2" in authoring ? authoring.timelineHistoryV2 : undefined;
  const receiptV2 =
    "lastTimelineReceiptV2" in authoring
      ? authoring.lastTimelineReceiptV2
      : undefined;
  const hasV2Authority =
    timelineHistoryV2 !== undefined || receiptV2 !== undefined;
  const receiptId = hasV2Authority
    ? (receiptV2?.requestId ?? null)
    : "lastTimelineReceipt" in authoring
      ? (authoring.lastTimelineReceipt?.requestId ?? null)
      : null;
  const rejection = hasV2Authority
    ? (timelineHistoryV2?.rejection?.code ?? null)
    : "timelineHistory" in authoring
      ? (authoring.timelineHistory?.rejection?.code ?? null)
      : null;
  return {
    timelineAuthority,
    authoringFingerprint,
    capturedAuthoringIdentity,
    timelineHistoryV2,
    receiptId,
    rejection,
  };
}
