// M25-16 virtualized multi-track timeline with `NLE-EDGE-TRIM-V1` edge handles.
//
// Rows and clips are windowed: only tracks inside the scroll viewport (plus one on each side)
// and clips intersecting the visible frame window mount, bounded to 8 rows and 96 clip nodes.
// Zoom/scroll/scrub are `local_transport`; clip selection issues the accepted `select_clips`;
// a committed edge drag issues exactly one `trim_clip` transaction and one undo step.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../../state/authoringViewState";
import { ownRangeKeyDown } from "../ownedRangeKeys";
import { useRetainedSlot } from "../useRetainedSlot";
import type { SidebarRetention } from "../../state/sidebarRetention";
import type {
  CompositionClip,
  CompositionTrack,
  PublicCompositionSnapshot,
} from "../../contracts/compositionCodec";
import type { Locale } from "../../i18n/catalog";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaSourceLeaseClient,
} from "../../host/authoringMediaSourceLease";
import type { NleDecorationDemandBroker } from "../../host/nleDecorationDemandBroker";
import type { FilmstripPaintInput } from "../../runtime/nleFilmstripPainter";
import {
  isNleSchedulerTraceEnabled,
  type WaveformPaintInput,
} from "../../runtime/nleWaveformPainter";
import { useNleFilmstrips } from "./useNleFilmstrips";
import { useNleWaveforms } from "./useNleWaveforms";
import type { NleBinInsertChannel } from "../../runtime/nleBinInsertChannel";
import {
  TRIM_GESTURE_IDLE,
  reduceTrimGesture,
  snapTrimBoundary,
  trimCommandForDraft,
  type TrimEdge,
  type TrimGestureState,
} from "../../runtime/nleTrimGesture";
import {
  NLE_GESTURE_IDLE,
  admitTimelineInsert,
  admitTimelineMove,
  commandForGesture,
  draftOwnerPresent,
  reduceNleTimelineGesture,
  selectTimelineSnap,
  type NleTimelineGestureState,
  type TimelineSnapTarget,
} from "../../runtime/nleTimelineGesture";
import {
  LANE_LEAD_IN_PX,
  MAX_TIMELINE_SCALE,
  clampTimelineScale,
  clampViewStart,
  contentXFromClientX,
  formatRulerLabel,
  frameFromX,
  laneXForBoundary,
  timelineXForBoundary,
  laneXFromClientX,
  quantizeSignedDelta,
  rulerMarks,
  rulerMinorTicks,
  timelineScaleBounds,
  trackHeaderWidthPx,
  zoomToSelection,
} from "../../runtime/timelineGeometry";
import {
  adjacentClipId,
  contiguousClipSelection,
  fitTimelineContentView,
  formatTimelineTimecode,
  zoomTimelineViewport,
  type TimelineViewportState,
} from "../../runtime/timelineNavigation";
import {
  createTimelineRasterScheduler,
  type TimelineDecorationScene,
} from "../../runtime/nleTimelineRaster";
import { build, freshIdentifier } from "./nleCommandBuilders";
import { fill, nleCopy } from "./nleCopy";
import { NleActionIcon } from "./NleIconActions";
import { NleTimelineToolbar } from "./NleTimelineToolbar";
import { NleTrackHeader } from "./NleTrackHeader";
import {
  clipEditAffordances,
  moveRefusalText,
  scrollBarGeometry,
  assetDisplayNames,
  clipDisplayName,
  trackDisplayNames,
  viewStartForScrollThumb,
} from "./nleTimelineSurface";
import {
  NleClipMenu,
  NleTrackMenu,
  timelineMenuLabels,
  type TimelineMenuTarget,
} from "./NleTimelineMenus";
import {
  decideTimelineTool,
  eligibleRollCuts,
  resolveTimelineShortcut,
  toolPlayheadFrame,
  type RollCut,
  type TimelineRebaseAttempt,
  type TimelineToolAction,
  type TimelineToolDecision,
} from "./nleTimelineTools";

export const NLE_ROW_HEIGHT_PX = 56;
export const NLE_MAX_VISIBLE_ROWS = 8;
export const NLE_MAX_CLIP_NODES = 96;
const LEGACY_ZOOM_LEVELS = Object.freeze([0.25, 0.5, 1, 2, 4, 8]);
const DRAG_THRESHOLD_PX = 4;
// M25-62: the clip box is inset 4 px in its 56 px row and has a 1 px border; the raster paints
// inside it, so the filmstrip fills the clip and the waveform is its bottom 14 px band.
const CLIP_CONTENT_TOP_PX = 5;
const CLIP_CONTENT_HEIGHT_PX = NLE_ROW_HEIGHT_PX - 2 * CLIP_CONTENT_TOP_PX;
const WAVEFORM_BAND_HEIGHT_PX = 14;
/**
 * The dialog's 44 px resize corner covers the bottom-right of this region; the rail keeps 54 px
 * clear of it (`.h3-nle-trim-rail`), and so does the scroll bar's track.
 */
const RESIZE_CORNER_CLEARANCE_PX = 54;
/** R2: the roll target under each pointer mode (`controlTargetPx.minimum` / `.coarse`). */
const ROLL_TARGET_PX = { fine: 24, coarse: 44 } as const;

const COARSE_POINTER_QUERY = "(pointer: coarse)";
function subscribeCoarsePointer(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const query = window.matchMedia(COARSE_POINTER_QUERY);
  query.addEventListener?.("change", onChange);
  return () => query.removeEventListener?.("change", onChange);
}
function readCoarsePointer(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia(COARSE_POINTER_QUERY).matches
  );
}
/**
 * M25-62 (R2, R7): the grip, rail and roll rules differ by primary pointer, as the CSS target
 * floor does. jsdom has no `matchMedia` and is treated as a fine pointer.
 */
function useCoarsePointer(): boolean {
  return useSyncExternalStore(
    subscribeCoarsePointer,
    readCoarsePointer,
    () => false,
  );
}

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
  /** M25-57: V2 edits run to fixed capacity while render snapshots stay content-sized. */
  editCapacityFrames?: number;
  /** M25-57: transport navigation ends at accepted content, independently of edit capacity. */
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
  /** M25-47 exact failed command/base provenance, owned by the workspace dispatch seam. */
  rebaseAttempt?: TimelineRebaseAttempt | null;
  /** M25-21: zoom, snap and scroll survive view release for the same workspace. */
  retention?: SidebarRetention;
  binInsert?: NleBinInsertChannel;
  decorationDemandBroker?: NleDecorationDemandBroker<
    AuthoringMediaAssetDecoration["value"]
  >;
  leaseClient?: AuthoringMediaSourceLeaseClient;
  runtimeEpoch?: number;
}>;

function clipEnd(clip: CompositionClip): number {
  return clip.startFrame + clip.durationFrames;
}

function retainedInteger(value: unknown, low: number, high: number): number {
  return typeof value === "number" && Number.isInteger(value)
    ? Math.min(high, Math.max(low, value))
    : low;
}

export function NleTimeline({
  locale,
  snapshot,
  selection,
  authoring,
  gridFrames,
  editCapacityFrames,
  contentEndExclusive,
  playheadFrame,
  transportAvailable = true,
  navigationAvailable = true,
  transportChannel,
  highlightedAssetIds,
  onIntent,
  onEdgeGestureActive,
  onTrimState,
  onBeginTrim,
  onSeek,
  onBeginScrub,
  onTogglePlay,
  onStep,
  bindShortcuts,
  rebaseAttempt = null,
  retention,
  binInsert,
  decorationDemandBroker,
  leaseClient,
  runtimeEpoch = 0,
}: NleTimelineProps) {
  const text = nleCopy(locale);
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
  const scrollRef = useRef<HTMLDivElement>(null);
  const nativeWheelHandlerRef = useRef<(event: WheelEvent) => void>(
    () => undefined,
  );
  const retainedView = useRetainedSlot(
    retention,
    "nle.timeline",
    snapshot.workspaceHandle,
  );
  const restoredView = retainedView.restored;
  const restoredScale =
    typeof restoredView?.pixelsPerFrame === "number" &&
    Number.isFinite(restoredView.pixelsPerFrame) &&
    restoredView.pixelsPerFrame > 0
      ? restoredView.pixelsPerFrame
      : restoredView?.zoomIndex === undefined
        ? 1
        : LEGACY_ZOOM_LEVELS[
            retainedInteger(
              restoredView.zoomIndex,
              0,
              LEGACY_ZOOM_LEVELS.length - 1,
            )
          ];
  const [timelineView, setTimelineView] = useState<TimelineViewportState>(() =>
    Object.freeze({
      pixelsPerFrame: restoredScale ?? 1,
      fitLocked: restoredView?.fitLocked === true,
      viewStart: retainedInteger(
        restoredView?.viewStart,
        0,
        Math.max(
          0,
          (editCapacityFrames ?? Number(snapshot.output.durationFrames)) - 1,
        ),
      ),
    }),
  );
  const { pixelsPerFrame, fitLocked, viewStart } = timelineView;
  const [snapEnabled, setSnapEnabled] = useState(
    restoredView?.snapEnabled === true,
  );
  const [rippleEnabled, setRippleEnabled] = useState(
    restoredView?.rippleEnabled === true,
  );
  const [scrollTop, setScrollTop] = useState(() =>
    retainedInteger(restoredView?.scrollTop, 0, Number.MAX_SAFE_INTEGER),
  );
  const [laneWidth, setLaneWidth] = useState(640);
  const laneWidthRef = useRef(laneWidth);
  laneWidthRef.current = laneWidth;
  const [virtualizationHeight, setVirtualizationHeight] = useState(
    NLE_ROW_HEIGHT_PX * 4,
  );
  const virtualizationHeightRef = useRef(NLE_ROW_HEIGHT_PX * 4);
  const measuredViewportHeightRef = useRef(NLE_ROW_HEIGHT_PX * 4);
  const [gesture, setGesture] = useState<TrimGestureState>(TRIM_GESTURE_IDLE);
  const [directGesture, setDirectGesture] =
    useState<NleTimelineGestureState>(NLE_GESTURE_IDLE);
  const [rollDraft, setRollDraft] = useState<Readonly<{
    cut: RollCut;
    deltaFrames: number;
    input: "pointer" | "keyboard" | "click";
    pointerId: number | null;
    originClientX: number;
  }> | null>(null);
  const rollDraftRef = useRef(rollDraft);
  rollDraftRef.current = rollDraft;
  const suppressRollClickRef = useRef(false);
  const directGestureRef = useRef(directGesture);
  const publishedDirectGesture = useRef(directGesture);
  // IMPORTANT: pointerdown deliberately keeps its subthreshold draft outside React state.
  // An unrelated render must not erase it; authority/mapping cancellation reduces this ref.
  if (publishedDirectGesture.current !== directGesture) {
    publishedDirectGesture.current = directGesture;
    directGestureRef.current = directGesture;
  }
  const shortcutHandlerRef = useRef<NleTimelineShortcutHandler>(
    () => undefined,
  );
  const [focusedClipId, setFocusedClipId] = useState<string | null>(
    selection[0] ?? null,
  );
  const [clickSelectionMode, setClickSelectionMode] = useState<
    "replace" | "toggle" | "range"
  >("replace");
  const [clickMove, setClickMove] = useState<Readonly<{
    clipId: string;
    deltaFrames: number;
    rowDelta: number;
  }> | null>(null);
  const [trackMenuTarget, setTrackMenuTarget] =
    useState<TimelineMenuTarget | null>(null);
  const [clipMenuTarget, setClipMenuTarget] =
    useState<TimelineMenuTarget | null>(null);
  const selectionAnchor = useRef<string | null>(selection[0] ?? null);
  const pendingFocusClipId = useRef<string | null>(null);
  const decorationRef = useRef<HTMLCanvasElement>(null);
  const rulerRef = useRef<HTMLDivElement>(null);
  const previousFrameRef = useRef<HTMLButtonElement>(null);
  const nextFrameRef = useRef<HTMLButtonElement>(null);
  const playheadElementRef = useRef<HTMLDivElement>(null);
  const playheadFrameRef = useRef(playheadFrame);
  const transportAvailableRef = useRef(transportAvailable);
  const navigationAvailableRef = useRef(navigationAvailable);
  if (transportChannel === undefined) {
    playheadFrameRef.current = playheadFrame;
    transportAvailableRef.current = transportAvailable;
    navigationAvailableRef.current = navigationAvailable;
  }
  const rasterRef = useRef<ReturnType<
    typeof createTimelineRasterScheduler
  > | null>(null);
  const viewStartRef = useRef(viewStart);
  viewStartRef.current = viewStart;
  const pixelsPerFrameRef = useRef(pixelsPerFrame);
  pixelsPerFrameRef.current = pixelsPerFrame;
  const autoScrollMappingRef = useRef(false);
  const autoScrollRef = useRef<{
    raf: number | null;
    pointerX: number;
    pointerY: number;
    lastTime: number;
  }>({ raf: null, pointerX: 0, pointerY: 0, lastTime: 0 });
  const scrubRef = useRef<{
    pointerId: number;
    ownerKey: string;
    originFrame: number;
    targetFrame: number;
    raf: number | null;
  } | null>(null);
  const scrollDragRef = useRef<{ pointerId: number; grab: number } | null>(
    null,
  );
  const gestureRef = useRef(gesture);
  gestureRef.current = gesture;
  const submittedRequest = useRef<string | null>(null);
  const submittedDirectRequest = useRef<string | null>(null);
  const directPreviousReceipt = useRef<string | null>(null);
  const previousReceipt = useRef<string | null>(null);
  const captureRef = useRef<{
    element: HTMLButtonElement;
    pointerId: number;
  } | null>(null);
  const suppressClickRef = useRef(false);
  const [marquee, setMarquee] = useState<Readonly<{
    pointerId: number;
    originX: number;
    originY: number;
    x: number;
    y: number;
    width: number;
    height: number;
    additive: boolean;
  }> | null>(null);

  const durationFrames =
    editCapacityFrames ?? Number(snapshot.output.durationFrames);
  const durationFramesRef = useRef(durationFrames);
  durationFramesRef.current = durationFrames;
  const transportEndExclusive =
    contentEndExclusive ?? Number(snapshot.output.durationFrames);
  const transportLastFrame = Math.max(0, transportEndExclusive - 1);
  const frameRate = snapshot.output.frameRate as
    Readonly<{ num: number; den: number }> | undefined;
  const fps =
    frameRate !== undefined && frameRate.num > 0 && frameRate.den > 0
      ? frameRate.num / frameRate.den
      : 24;
  const tracks = useMemo(
    () => [...snapshot.tracks].sort((a, b) => a.order - b.order),
    [snapshot.tracks],
  );
  const coarse = useCoarsePointer();
  // B-M2562-01: the header is 112 px for a fine pointer and today's 160 px for a coarse one. Every
  // client-x conversion reads the ref, so gesture closures never keep a stale width.
  const headerWidth = trackHeaderWidthPx(coarse);
  const headerWidthRef = useRef(headerWidth);
  headerWidthRef.current = headerWidth;
  // M25-62 (R5): display names replace raw ids in every visible text, name and tooltip.
  const trackNames = useMemo(
    () => trackDisplayNames(snapshot.tracks, text.timeline.trackNames),
    [snapshot.tracks, text],
  );
  const assetNames = useMemo(
    () => assetDisplayNames(snapshot.assets, text.assets.card),
    [snapshot.assets, text],
  );
  const clipName = useCallback(
    (clip: CompositionClip) =>
      clipDisplayName(clip, assetNames, {
        cardTemplate: text.assets.card,
        titleClip: text.timeline.titleClip,
      }),
    [assetNames, text],
  );
  const menuLabels = timelineMenuLabels(locale);
  // M25-79: a click on the trigger button that opened a menu closes it. The menu's outside-press
  // dismissal leaves that button alone (`useMenuDismiss`), so this click is the toggle; a
  // context-menu gesture always (re)opens at the pointer.
  const menuOpening = (
    event: ReactMouseEvent<HTMLElement>,
    id: string | null,
  ) => {
    const opener = event.currentTarget;
    const toggles = event.type === "click";
    const next: TimelineMenuTarget = {
      id,
      returnFocus: opener,
      x: event.clientX,
      y: event.clientY,
    };
    return (current: TimelineMenuTarget | null) =>
      toggles && current?.returnFocus === opener ? null : next;
  };
  const openTrackMenu = (
    event: ReactMouseEvent<HTMLElement>,
    trackId: string | null,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    setClipMenuTarget(null);
    setTrackMenuTarget(menuOpening(event, trackId));
  };
  const openClipMenu = (
    event: ReactMouseEvent<HTMLElement>,
    clipId: string,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    setTrackMenuTarget(null);
    setClipMenuTarget(menuOpening(event, clipId));
  };
  const openClipMenuFromKey = (element: HTMLElement, clipId: string) => {
    const rect = element.getBoundingClientRect();
    setTrackMenuTarget(null);
    setClipMenuTarget({
      id: clipId,
      returnFocus: element,
      x: rect.left,
      y: rect.bottom,
    });
  };
  const mappingKey = `${pixelsPerFrame}:${viewStart}:${laneWidth}`;
  const scaleBounds = timelineScaleBounds(
    laneWidth,
    durationFrames,
    pixelsPerFrame,
  );

  // A restored scroll offset is re-applied to the new grid once; the browser clamps it to the
  // current rows and the scroll handler then reports the real offset.
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (element !== null && scrollTop > 0) element.scrollTop = scrollTop;
    // Mount only: later offsets come from the element itself.
  }, []);
  useEffect(() => {
    retainedView.write(snapshot.workspaceHandle, {
      pixelsPerFrame,
      fitLocked,
      snapEnabled,
      rippleEnabled,
      viewStart,
      scrollTop,
    });
  }, [
    retainedView,
    snapshot.workspaceHandle,
    pixelsPerFrame,
    fitLocked,
    snapEnabled,
    rippleEnabled,
    viewStart,
    scrollTop,
  ]);

  useEffect(() => {
    const element = scrollRef.current;
    if (element === null) return;
    // IMPORTANT (M25-53): the accessible trim rail changes this box by less than one 56px track
    // row as part of the same accepted edit. Keep the exact paint height in a ref and repaint
    // directly; a ResizeObserver setState here creates a fifth NLE commit. One extra virtual row
    // covers that geometry change, while larger host resizes still grow the mounted window below.
    const measure = () => {
      const nextHeight = element.clientHeight || NLE_ROW_HEIGHT_PX * 4;
      if (nextHeight !== measuredViewportHeightRef.current) {
        measuredViewportHeightRef.current = nextHeight;
        rasterRef.current?.request();
      }
      const nextCapacity = Math.min(
        NLE_MAX_VISIBLE_ROWS,
        Math.ceil(nextHeight / NLE_ROW_HEIGHT_PX) + 2,
      );
      const currentCapacity = Math.min(
        NLE_MAX_VISIBLE_ROWS,
        Math.ceil(virtualizationHeightRef.current / NLE_ROW_HEIGHT_PX) + 3,
      );
      if (nextCapacity > currentCapacity) {
        virtualizationHeightRef.current = nextHeight;
        setVirtualizationHeight(nextHeight);
      }
      // `laneWidth` is the frame-mapped width, right of the lead-in (M25-61).
      const nextWidth = Math.max(
        120,
        element.clientWidth - headerWidthRef.current - LANE_LEAD_IN_PX,
      );
      if (laneWidthRef.current !== nextWidth) {
        laneWidthRef.current = nextWidth;
        setLaneWidth(nextWidth);
      }
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
    // The header width changes the lane width without resizing the scroller.
  }, [headerWidth]);

  useEffect(() => {
    setTimelineView((current) => {
      if (current.fitLocked)
        return fitTimelineContentView({
          laneWidth: laneWidth + LANE_LEAD_IN_PX,
          contentEndExclusive: transportEndExclusive,
          editCapacityFrames: durationFrames,
          fps,
          lastValidScale: current.pixelsPerFrame,
        });
      const bounds = timelineScaleBounds(
        laneWidth,
        durationFrames,
        current.pixelsPerFrame,
      );
      const nextScale = clampTimelineScale(current.pixelsPerFrame, bounds);
      const nextStart = clampViewStart(
        current.viewStart,
        durationFrames,
        laneWidth,
        nextScale,
      );
      return nextScale === current.pixelsPerFrame &&
        nextStart === current.viewStart
        ? current
        : Object.freeze({
            pixelsPerFrame: nextScale,
            viewStart: nextStart,
            fitLocked: false,
          });
    });
  }, [laneWidth, durationFrames, transportEndExclusive, fps]);

  // A trim draft may begin on the accepted snapshot in `ready` and in `conflict`: after a
  // refusal the projection already carries the refreshed backend history, so a fresh draft
  // captures the current CAS. Requiring `ready` here leaves the row's keyboard buttons enabled
  // with no effect and the pointer grips dead until some unrelated command succeeds, which is
  // exactly the silent no-op control the matrix recovery cases reject. `pending`, `loading`,
  // `error` and `gone` still refuse: their snapshot is not an accepted base.
  const canBegin = (phase: TrimGestureState["phase"]) =>
    phase === "idle" || phase === "accepted" || phase === "rejected";
  const draftable =
    authoring.status === "ready" || authoring.status === "conflict";

  // A zoom/scroll/viewport change invalidates any draft: its pixel mapping is gone.
  useEffect(() => {
    // IMPORTANT (M25-21 B3-D56): same rule as the effect below -- do not enqueue an updater that
    // cannot change the state. `mapping_changed` returns the current state untouched unless a
    // draft is live, but enqueuing it still schedules a render React then bails out of, and that
    // bailed-out pass is a real commit against the per-edit commit ceiling: it was measured at
    // `actualDuration` 0.0 ms on every one of the twenty edge drags. An accepted trim changes the
    // clip geometry, which changes `mappingKey`, so this fired once per accepted edit.
    if (autoScrollMappingRef.current) {
      autoScrollMappingRef.current = false;
      return;
    }
    if (
      gestureRef.current.phase === "dragging" ||
      gestureRef.current.phase === "keyboard_draft"
    )
      setGesture((current) =>
        reduceTrimGesture(current, { type: "mapping_changed", mappingKey }),
      );
    if (
      directGestureRef.current.phase === "dragging" ||
      directGestureRef.current.phase === "keyboard_draft"
    ) {
      const current = directGestureRef.current;
      if (current.draft.identity.mappingKey !== mappingKey)
        cancelDirect("mapping_changed");
    }
    if (rollDraftRef.current !== null) setRollDraft(null);
  }, [mappingKey]);

  // IMPORTANT: an edge draft belongs to the captured accepted snapshot. A new
  // selection, lock, catalog or timeline must cancel it, never silently rebase the cut.
  useEffect(() => {
    // Do not enqueue an idle updater: it adds a commit alongside external-store
    // replacement even when the updater eventually returns the same idle value.
    if (
      gestureRef.current.phase !== "dragging" &&
      gestureRef.current.phase !== "keyboard_draft"
    )
      return;
    setGesture((current) => {
      if (current.phase !== "dragging" && current.phase !== "keyboard_draft")
        return current;
      const identity = current.draft.identity;
      const clip = snapshot.clips.find(
        (member) => member.clipId === identity.clipId,
      );
      const track = snapshot.tracks.find(
        (member) => member.trackId === clip?.trackId,
      );
      const valid =
        selection[0] === identity.clipId &&
        track?.locked === false &&
        identity.workspaceHandle === timelineAuthority.workspaceHandle &&
        draftable &&
        identity.workspaceRevision === timelineAuthority.workspaceRevision &&
        identity.timelineRevision === timelineAuthority.timelineRevision &&
        identity.timelineFingerprint ===
          timelineAuthority.timelineFingerprint &&
        identity.authoringFingerprint === authoringFingerprint;
      return valid ? current : TRIM_GESTURE_IDLE;
    });
  }, [
    snapshot,
    selection,
    draftable,
    timelineAuthority.workspaceHandle,
    timelineAuthority.workspaceRevision,
    timelineAuthority.timelineRevision,
    timelineAuthority.timelineFingerprint,
    authoringFingerprint,
  ]);

  useEffect(() => {
    const current = rollDraftRef.current;
    if (current === null) return;
    const stillOwned =
      draftable &&
      eligibleRollCuts(
        snapshot,
        snapshot.clips.find((clip) => clip.clipId === selection[0])?.trackId ??
          "",
      ).some(
        (cut) =>
          cut.leftClipId === current.cut.leftClipId &&
          cut.rightClipId === current.cut.rightClipId,
      );
    if (!stillOwned) setRollDraft(null);
  }, [snapshot, selection, draftable]);

  useEffect(() => {
    if (
      directGestureRef.current.phase !== "dragging" &&
      directGestureRef.current.phase !== "keyboard_draft"
    )
      return;
    const current = directGestureRef.current;
    const identity = current.draft.identity;
    const insertAssetId = current.draft.insert?.assetId ?? null;
    const valid =
      draftable &&
      identity.workspaceHandle === timelineAuthority.workspaceHandle &&
      identity.workspaceRevision === timelineAuthority.workspaceRevision &&
      identity.timelineRevision === timelineAuthority.timelineRevision &&
      identity.timelineFingerprint === timelineAuthority.timelineFingerprint &&
      identity.authoringFingerprint === authoringFingerprint &&
      (insertAssetId === null ||
        snapshot.assets.some((asset) => asset.assetId === insertAssetId)) &&
      identity.clipIds.every((clipId) =>
        snapshot.clips.some((clip) => clip.clipId === clipId),
      );
    if (!valid) cancelDirect("authority_changed");
  }, [
    draftable,
    snapshot,
    timelineAuthority.workspaceHandle,
    timelineAuthority.workspaceRevision,
    timelineAuthority.timelineRevision,
    timelineAuthority.timelineFingerprint,
    authoringFingerprint,
  ]);

  const releaseCapture = () => {
    const capture = captureRef.current;
    captureRef.current = null;
    if (capture?.element.hasPointerCapture(capture.pointerId))
      capture.element.releasePointerCapture(capture.pointerId);
  };
  useEffect(() => {
    if (gesture.phase !== "dragging") releaseCapture();
  }, [gesture.phase]);
  useEffect(() => () => releaseCapture(), []);

  useEffect(() => {
    onTrimState?.(gesture);
    onEdgeGestureActive(
      gesture.phase === "dragging" ||
        gesture.phase === "keyboard_draft" ||
        rollDraft !== null,
    );
  }, [gesture, rollDraft, onEdgeGestureActive, onTrimState]);

  // Settle a submitted trim from the accepted authoring state, never from the local draft.
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
  // IMPORTANT (M25-21 B3-D56): the accepted settle is derived while rendering the state that
  // accepted it, not in an effect afterwards. React re-renders immediately and commits once, so
  // the accepted revision and the trim's `accepted` notice reach the user in the same commit; run
  // from an effect it is a commit of its own, and the frozen per-edit commit ceiling has no room
  // for it. This is the supported "adjust state when a prop changes" pattern, not a render-time
  // side effect: it only calls this component's own setters, is guarded by `settledReceipt` so it
  // cannot loop, and starts nothing asynchronous. The refusal, lost-response and reconciliation
  // branches stay in the effect below -- they are not on this path and must not be made to race
  // with it. `previousReceipt` still marks the receipt that was current when the trim was
  // submitted, so only a genuinely new receipt settles it.
  const [settledReceipt, setSettledReceipt] = useState<string | null>(null);
  if (
    (gesture.phase === "submitting" || gesture.phase === "reconciling") &&
    authoring.status === "ready" &&
    receiptId !== null &&
    receiptId !== previousReceipt.current &&
    receiptId !== settledReceipt
  ) {
    setSettledReceipt(receiptId);
    setGesture((current) => reduceTrimGesture(current, { type: "accepted" }));
  }
  const [settledDirectReceipt, setSettledDirectReceipt] = useState<
    string | null
  >(null);
  // CRITICAL: settle a successful direct edit while rendering its accepted authoring state.
  // Moving this back into the effect below adds a fifth overlay commit to every move drag and
  // breaks the frozen <=4 per-edit ceiling; refusal and reconciliation still belong to the effect.
  if (
    (directGesture.phase === "submitting" ||
      directGesture.phase === "reconciling") &&
    authoring.status === "ready" &&
    receiptId !== null &&
    receiptId !== directPreviousReceipt.current &&
    receiptId !== settledDirectReceipt
  ) {
    setSettledDirectReceipt(receiptId);
    setDirectGesture((current) =>
      reduceNleTimelineGesture(current, { type: "accepted" }),
    );
  }
  useEffect(() => {
    if (gesture.phase !== "submitting" && gesture.phase !== "reconciling")
      return;
    if (authoring.status === "pending" || authoring.status === "loading")
      return;
    if (
      authoring.status === "ready" &&
      receiptId !== null &&
      receiptId !== previousReceipt.current
    )
      return;
    if (authoring.status === "conflict") {
      setGesture((current) =>
        reduceTrimGesture(current, {
          type: "rejected",
          reason: rejection ?? "conflict",
        }),
      );
      return;
    }
    if (authoring.status === "error" || authoring.status === "gone") {
      // M25-16 corrective F1: a lost reply is reported by the session as
      // `error/outcome_unknown`, never by a rejected intent promise. It must read as unknown
      // (the reducer's `response_lost` -> `reconciling`), not as a rejection, until the
      // session's single read-only reconciliation arrives as `ready`.
      const lost =
        authoring.status === "error" && authoring.reason === "outcome_unknown";
      setGesture((current) =>
        reduceTrimGesture(current, {
          type: lost ? "response_lost" : "rejected",
          reason: authoring.reason,
        }),
      );
      return;
    }
    if (gesture.phase === "reconciling" && authoring.status === "ready")
      setGesture((current) =>
        reduceTrimGesture(current, { type: "reconciled" }),
      );
  }, [authoring, gesture.phase, receiptId, rejection]);

  useEffect(() => {
    if (gesture.phase !== "accepted" && gesture.phase !== "rejected") return;
    const timer = setTimeout(
      () =>
        setGesture((current) => reduceTrimGesture(current, { type: "reset" })),
      1_500,
    );
    return () => clearTimeout(timer);
  }, [gesture.phase]);

  useEffect(() => {
    if (
      directGesture.phase !== "submitting" &&
      directGesture.phase !== "reconciling"
    )
      return;
    if (authoring.status === "pending" || authoring.status === "loading")
      return;
    if (
      authoring.status === "ready" &&
      receiptId !== null &&
      receiptId !== directPreviousReceipt.current
    ) {
      setDirectGesture((current) =>
        reduceNleTimelineGesture(current, { type: "accepted" }),
      );
      return;
    }
    if (authoring.status === "conflict") {
      setDirectGesture((current) =>
        reduceNleTimelineGesture(current, {
          type: "rejected",
          reason: rejection ?? "conflict",
        }),
      );
      return;
    }
    if (authoring.status === "error" || authoring.status === "gone") {
      const lost =
        authoring.status === "error" && authoring.reason === "outcome_unknown";
      setDirectGesture((current) =>
        reduceNleTimelineGesture(current, {
          type: lost ? "response_lost" : "rejected",
          reason: authoring.reason,
        }),
      );
      return;
    }
    if (directGesture.phase === "reconciling" && authoring.status === "ready")
      setDirectGesture((current) =>
        reduceNleTimelineGesture(current, { type: "reconciled" }),
      );
  }, [authoring, directGesture.phase, receiptId, rejection]);

  useEffect(() => {
    if (
      directGesture.phase !== "accepted" &&
      directGesture.phase !== "rejected"
    )
      return;
    const timer = setTimeout(
      () =>
        setDirectGesture((current) =>
          reduceNleTimelineGesture(current, { type: "reset" }),
        ),
      1_500,
    );
    return () => clearTimeout(timer);
  }, [directGesture.phase]);

  const submit = (next: TrimGestureState) => {
    if (
      next.phase !== "submitting" ||
      submittedRequest.current === next.requestId
    )
      return;
    submittedRequest.current = next.requestId;
    previousReceipt.current = receiptId;
    const command = trimCommandForDraft(next.draft);
    // IMPORTANT: the Ripple toggle cancels every uncommitted grip before changing this mode.
    // Do not remove that cancellation or one drag can begin as trim_clip and commit ripple_trim.
    const target = snapshot.clips.find(
      (clip) => clip.clipId === command.clip_id,
    );
    if (target === undefined) return;
    void onIntent({
      action: "apply_timeline_commands",
      capturedTimeline: {
        workspaceHandle: next.draft.identity.workspaceHandle,
        workspaceRevision: next.draft.identity.workspaceRevision,
        timelineRevision: next.draft.identity.timelineRevision,
        timelineFingerprint: next.draft.identity.timelineFingerprint,
        ...("authoringFingerprint" in next.draft.identity &&
        next.draft.identity.authoringFingerprint !== undefined
          ? { authoringFingerprint: next.draft.identity.authoringFingerprint }
          : {}),
      },
      commands: [
        rippleEnabled
          ? build.rippleTrim(
              command.clip_id,
              command.edge,
              command.delta_frames,
              [target.trackId],
            )
          : build.trimClip(command.clip_id, command.edge, command.delta_frames),
      ],
    });
  };

  const identityFor = (clip: CompositionClip, edge: TrimEdge) => ({
    clipId: clip.clipId,
    workspaceHandle: timelineAuthority.workspaceHandle,
    edge,
    workspaceRevision: timelineAuthority.workspaceRevision,
    timelineRevision: timelineAuthority.timelineRevision,
    timelineFingerprint: timelineAuthority.timelineFingerprint,
    ...capturedAuthoringIdentity,
    mappingKey,
  });

  const snapFor = (clip: CompositionClip) => (rawBoundaryFrame: number) =>
    snapEnabled
      ? snapTrimBoundary({
          rawBoundaryFrame,
          pixelsPerFrame,
          clips: snapshot.clips.map((member) => ({
            clipId: member.clipId,
            startFrame: member.startFrame,
            endFrame: clipEnd(member),
          })),
          targetClipId: clip.clipId,
          playheadFrame,
          outputDurationFrames: durationFrames,
        })
      : null;

  const beginPointer = (
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
    edge: TrimEdge,
  ) => {
    if (
      !canBegin(gestureRef.current.phase) ||
      event.button !== 0 ||
      event.isPrimary === false ||
      selection[0] !== clip.clipId ||
      !draftable ||
      snapshot.tracks.find((track) => track.trackId === clip.trackId)
        ?.locked !== false
    )
      return;
    event.currentTarget.setPointerCapture(event.pointerId);
    onBeginTrim?.();
    captureRef.current = {
      element: event.currentTarget,
      pointerId: event.pointerId,
    };
    const next = reduceTrimGesture(gestureRef.current, {
      type: "begin",
      input: "pointer",
      identity: identityFor(clip, edge),
      pointerId: event.pointerId,
      originClientX: event.clientX,
      pixelsPerFrame,
      originStart: clip.startFrame,
      originEnd: clipEnd(clip),
      outputDurationFrames: durationFrames,
    });
    // IMPORTANT: Chromium can deliver move/release before React commits pointerdown. Keep the
    // event ref synchronous or a fresh drag after conflict reads the rejected state and submits
    // nothing even though the visible grip moved.
    gestureRef.current = next;
    setGesture(next);
  };
  const movePointer = (
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ) => {
    const current = gestureRef.current;
    if (
      current.phase !== "dragging" ||
      current.draft.pointerId !== event.pointerId
    )
      return;
    const next = reduceTrimGesture(current, {
      type: "move",
      clientX: event.clientX,
      snap: snapFor(clip),
    });
    gestureRef.current = next;
    setGesture(next);
  };
  const releasePointer = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = gestureRef.current;
    if (
      current.phase !== "dragging" ||
      current.draft.pointerId !== event.pointerId
    )
      return;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    const next = reduceTrimGesture(current, {
      type: "release",
      requestId: `trim-${Date.now()}-${event.pointerId}`,
    });
    gestureRef.current = next;
    setGesture(next);
    submit(next);
  };
  const cancelPointer = (reason: string) => {
    // IMPORTANT (M25-21 B3-D56): only a live draft can be cancelled -- the reducer returns the
    // state untouched for every other phase. Enqueuing the updater anyway still schedules a render
    // React bails out of, and that bailed-out pass is a real commit against the per-edit ceiling.
    // It fires on the ordinary success path, not an edge case: `releasePointer` calls
    // `releasePointerCapture`, which synchronously raises `lostpointercapture` on a gesture that
    // has just become `submitting`, and the following focus move raises `blur` as well. Keep the
    // guard in step with the reducer's own `cancel` case rather than relying on the no-op.
    const current = gestureRef.current;
    if (current.phase !== "dragging" && current.phase !== "keyboard_draft")
      return;
    setGesture((state) => reduceTrimGesture(state, { type: "cancel", reason }));
  };

  const gripKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    clip: CompositionClip,
    edge: TrimEdge,
  ) => {
    const current = gestureRef.current;
    if (event.key === "Escape") {
      if (current.phase === "keyboard_draft" || current.phase === "dragging") {
        event.preventDefault();
        event.stopPropagation();
        cancelPointer("escape");
      }
      return;
    }
    if (event.key === "Enter") {
      if (current.phase !== "keyboard_draft") return;
      event.preventDefault();
      const next = reduceTrimGesture(current, {
        type: "commit",
        requestId: `trim-k-${Date.now()}`,
      });
      setGesture(next);
      submit(next);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    if (
      selection[0] !== clip.clipId ||
      !draftable ||
      snapshot.tracks.find((track) => track.trackId === clip.trackId)
        ?.locked !== false
    )
      return;
    event.preventDefault();
    const direction: 1 | -1 = event.key === "ArrowRight" ? 1 : -1;
    const steps = event.shiftKey ? Math.max(1, gridFrames) : 1;
    let next = current;
    if (canBegin(next.phase)) {
      onBeginTrim?.();
      next = reduceTrimGesture(next, {
        type: "begin",
        input: "keyboard",
        identity: identityFor(clip, edge),
        pointerId: null,
        originClientX: 0,
        pixelsPerFrame,
        originStart: clip.startFrame,
        originEnd: clipEnd(clip),
        outputDurationFrames: durationFrames,
      });
    }
    if (next.phase !== "keyboard_draft") return;
    for (let index = 0; index < steps; index += 1)
      next = reduceTrimGesture(next, { type: "step", deltaFrames: direction });
    gestureRef.current = next;
    setGesture(next);
  };

  // Virtual window.
  const totalRows = tracks.length;
  const firstRow = Math.max(0, Math.floor(scrollTop / NLE_ROW_HEIGHT_PX) - 1);
  const rowCapacity = Math.min(
    NLE_MAX_VISIBLE_ROWS,
    Math.ceil(virtualizationHeight / NLE_ROW_HEIGHT_PX) + 3,
  );
  const lastRow = Math.min(totalRows, firstRow + rowCapacity);
  const visibleTracks = useMemo(
    () => tracks.slice(firstRow, lastRow),
    [tracks, firstRow, lastRow],
  );
  const visibleFrames = Math.ceil(laneWidth / pixelsPerFrame);
  const viewEnd = viewStart + visibleFrames;
  // M25-62 (A62-6): the bar spans the lane columns (lead-in included) and drives the same
  // `viewStart` state and clamp as every other navigation.
  const scrollBarInput = {
    durationFrames,
    laneWidth,
    pixelsPerFrame,
    trackWidth: Math.max(
      0,
      laneWidth + LANE_LEAD_IN_PX - RESIZE_CORNER_CLEARANCE_PX,
    ),
  };
  const scrollBar = scrollBarGeometry({ ...scrollBarInput, viewStart });
  const scrollToThumb = (thumbX: number) => {
    setTimelineView((current) =>
      Object.freeze({
        ...current,
        fitLocked: false,
        viewStart: viewStartForScrollThumb(thumbX, scrollBar, scrollBarInput),
      }),
    );
  };
  const { clipsByTrack, mountedClipIds, visibleCandidates } = useMemo(() => {
    const visibleTrackIds = new Set(
      visibleTracks.map((track) => track.trackId),
    );
    const candidates = snapshot.clips
      .filter(
        (clip) =>
          visibleTrackIds.has(clip.trackId) &&
          clipEnd(clip) > viewStart - LANE_LEAD_IN_PX / pixelsPerFrame &&
          clip.startFrame < viewEnd,
      )
      .sort((left, right) => {
        const leftPriority =
          left.clipId === focusedClipId
            ? 0
            : selection.includes(left.clipId)
              ? 1
              : 2;
        const rightPriority =
          right.clipId === focusedClipId
            ? 0
            : selection.includes(right.clipId)
              ? 1
              : 2;
        if (leftPriority !== rightPriority) return leftPriority - rightPriority;
        // IMPORTANT: passive playback must not reshuffle semantic clip mounts. View interaction,
        // focus and selection are stable authorities; the presented playhead is not.
        return (
          Math.abs(left.startFrame - viewStart) -
            Math.abs(right.startFrame - viewStart) ||
          left.clipId.localeCompare(right.clipId)
        );
      })
      .slice(0, NLE_MAX_CLIP_NODES);
    const byTrack = new Map<string, CompositionClip[]>();
    const mounted = new Set<string>();
    for (const clip of candidates) {
      const list = byTrack.get(clip.trackId) ?? [];
      list.push(clip);
      byTrack.set(clip.trackId, list);
      mounted.add(clip.clipId);
    }
    for (const list of byTrack.values())
      list.sort(
        (left, right) =>
          left.startFrame - right.startFrame ||
          left.clipId.localeCompare(right.clipId),
      );
    return {
      clipsByTrack: byTrack,
      mountedClipIds: mounted,
      visibleCandidates: candidates,
    };
  }, [
    focusedClipId,
    selection,
    snapshot.clips,
    viewEnd,
    viewStart,
    visibleTracks,
  ]);
  useLayoutEffect(() => {
    const clipId = pendingFocusClipId.current;
    if (clipId === null || !mountedClipIds.has(clipId)) return;
    const target = scrollRef.current?.querySelector<HTMLElement>(
      `[data-h3-nle-clip="${CSS.escape(clipId)}"] .h3-nle-clip-body`,
    );
    if (target === undefined || target === null) return;
    target.focus();
    pendingFocusClipId.current = null;
  }, [mountedClipIds]);
  const selected = useMemo(() => new Set(selection), [selection]);
  const selectedClip = snapshot.clips.find(
    (clip) => clip.clipId === selection[0],
  );
  const rollCuts = useMemo(() => {
    if (selectedClip === undefined) return Object.freeze([] as RollCut[]);
    const eligible = eligibleRollCuts(snapshot, selectedClip.trackId);
    // IMPORTANT: adjacent cuts can be closer than the 44 px target floor. Rendering both over
    // a narrow selected clip makes the later handle intercept the earlier one, so selection
    // owns one explicit cut: its right boundary first, then its left boundary at track end.
    const owned =
      eligible.find((cut) => cut.leftClipId === selectedClip.clipId) ??
      eligible.find((cut) => cut.rightClipId === selectedClip.clipId);
    return Object.freeze(owned === undefined ? [] : [owned]);
  }, [selectedClip?.clipId, selectedClip?.trackId, snapshot]);
  const cancelRoll = () => {
    if (rollDraftRef.current !== null) setRollDraft(null);
  };
  const beginRollPointer = (
    event: ReactPointerEvent<HTMLButtonElement>,
    cut: RollCut,
  ) => {
    if (
      !draftable ||
      event.button !== 0 ||
      event.isPrimary === false ||
      rollDraftRef.current !== null ||
      gestureRef.current.phase === "dragging" ||
      gestureRef.current.phase === "keyboard_draft" ||
      directGestureRef.current.phase === "dragging" ||
      directGestureRef.current.phase === "keyboard_draft"
    )
      return;
    onBeginTrim?.();
    event.currentTarget.setPointerCapture(event.pointerId);
    setRollDraft({
      cut,
      deltaFrames: 0,
      input: "pointer",
      pointerId: event.pointerId,
      originClientX: event.clientX,
    });
  };
  const moveRollPointer = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = rollDraftRef.current;
    if (
      current === null ||
      current.input !== "pointer" ||
      current.pointerId !== event.pointerId
    )
      return;
    const delta =
      quantizeSignedDelta(
        (event.clientX - current.originClientX) / pixelsPerFrameRef.current,
      ) ?? 0;
    if (delta !== current.deltaFrames)
      setRollDraft({ ...current, deltaFrames: delta });
  };
  const commitRoll = (cut: RollCut, deltaFrames: number) => {
    if (deltaFrames !== 0)
      runTool({
        kind: "roll",
        leftClipId: cut.leftClipId,
        rightClipId: cut.rightClipId,
        deltaFrames,
      });
    setRollDraft(null);
  };
  const releaseRollPointer = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = rollDraftRef.current;
    if (
      current === null ||
      current.input !== "pointer" ||
      current.pointerId !== event.pointerId
    )
      return;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    suppressRollClickRef.current = current.deltaFrames !== 0;
    commitRoll(current.cut, current.deltaFrames);
  };
  const rollKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    cut: RollCut,
  ) => {
    const current = rollDraftRef.current;
    if (event.key === "Escape" && current?.cut === cut) {
      event.preventDefault();
      event.stopPropagation();
      cancelRoll();
      return;
    }
    if (event.key === "Enter" && current?.cut === cut) {
      event.preventDefault();
      event.stopPropagation();
      commitRoll(cut, current.deltaFrames);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    event.stopPropagation();
    const step =
      (event.shiftKey ? Math.max(1, gridFrames) : 1) *
      (event.key === "ArrowRight" ? 1 : -1);
    setRollDraft({
      cut,
      deltaFrames: current?.cut === cut ? current.deltaFrames + step : step,
      input: "keyboard",
      pointerId: null,
      originClientX: 0,
    });
  };
  // M25-62 (R7): the rail shows where the inline grips cannot -- below 24 px under a fine
  // pointer, below 144 px under a coarse one.
  // IMPORTANT: from the committed width, not the live draft that narrows the inline grips. While
  // a pointer holds a grip the rail's controls cannot be used, and a rail tied to the draft would
  // mount and unmount a layout row each time the drag crossed the threshold. It follows the
  // commit (M25-62 self-review).
  const railClip =
    selectedClip !== undefined &&
    clipEditAffordances({
      width: Math.max(20, selectedClip.durationFrames * pixelsPerFrame),
      selected: true,
      coarse,
      draftEdge: null,
    }).rail
      ? selectedClip
      : undefined;
  const highlighted = new Set(highlightedAssetIds);
  const draft =
    gesture.phase === "dragging" || gesture.phase === "keyboard_draft"
      ? gesture.draft
      : null;
  // CRITICAL: virtualized removal is not a blur. When a row scrolls out of the window React
  // unmounts its grips without dispatching `focusout`, so the `onGripBlur` binding never runs:
  // an uncommitted draft -- and with it the overlay's edge-first Escape guard -- would stay
  // armed on a control that no longer exists, while focus falls to `document.body` outside the
  // dialog where its own key handler can never see Escape (post-corrective review 02, R2-F3).
  // Ownership is therefore checked against the mounted window, not against focus, and against
  // *mount* rather than `showGrips`: a draft that trims its own clip below the inline threshold
  // only sets `hidden` on a still-mounted button, which does blur and is already handled, so
  // cancelling on that would regress the accepted shrink-during-draft behaviour. The rail
  // layout renders outside the scroll container and never unmounts on scroll. `submitting` and
  // `reconciling` own a real backend transaction and must survive the same scroll untouched.
  const draftOwnerMounted =
    draft === null ||
    railClip?.clipId === draft.identity.clipId ||
    mountedClipIds.has(draft.identity.clipId);
  useEffect(() => {
    if (draftOwnerMounted) return;
    const phase = gestureRef.current.phase;
    if (phase !== "dragging" && phase !== "keyboard_draft") return;
    const active = document.activeElement;
    releaseCapture();
    cancelPointer("owner_removed");
    // Restore focus only when the removal actually took it, and only to a connected owned
    // target: the grid carries `tabIndex={-1}`, so it is programmatically focusable while
    // staying out of the dialog's Tab ring.
    if (active === null || active === document.body || !active.isConnected)
      scrollRef.current?.focus();
  }, [draftOwnerMounted]);
  // IMPORTANT: bin inserts and click-only moves are not owned by a clip element (B-M2561-08);
  // `draftOwnerPresent` holds that rule so a virtualized origin never cancels them.
  const directOwnerMounted = draftOwnerPresent(
    directGestureRef.current,
    mountedClipIds,
  );
  useEffect(() => {
    if (directOwnerMounted) return;
    cancelDirect("owner_removed");
    if (document.activeElement === null || !document.activeElement.isConnected)
      scrollRef.current?.focus();
  }, [directOwnerMounted]);

  const maxViewStart = Math.max(0, durationFrames - visibleFrames);
  const gestureStatus =
    gesture.phase === "submitting"
      ? text.timeline.trimSubmitting
      : gesture.phase === "accepted"
        ? text.timeline.trimAccepted
        : gesture.phase === "rejected"
          ? text.timeline.trimRejected
          : gesture.phase === "reconciling"
            ? text.timeline.trimReconciling
            : draft !== null
              ? fill(text.timeline.trimDraft, {
                  edge:
                    draft.identity.edge === "start"
                      ? text.timeline.edgeStart
                      : text.timeline.edgeEnd,
                  delta: draft.geometry.deltaFrames,
                })
              : "";
  const inserting =
    directGesture.phase !== "idle" &&
    directGesture.draft.kind === "insert_from_bin";
  const directStatus =
    directGesture.phase === "submitting"
      ? inserting
        ? text.timeline.insertSubmitting
        : text.timeline.moveSubmitting
      : directGesture.phase === "accepted"
        ? inserting
          ? text.timeline.insertAccepted
          : text.timeline.moveAccepted
        : directGesture.phase === "rejected"
          ? inserting
            ? text.timeline.insertRejected
            : text.timeline.moveRejected
          : directGesture.phase === "reconciling"
            ? inserting
              ? text.timeline.insertReconciling
              : text.timeline.moveReconciling
            : directGesture.phase === "dragging" ||
                directGesture.phase === "keyboard_draft"
              ? directGesture.draft.admitted
                ? fill(
                    inserting
                      ? text.timeline.insertDraft
                      : text.timeline.moveDraft,
                    {
                      frame: directGesture.draft.insert?.targetFrame ?? 0,
                      delta: directGesture.draft.deltaFrames,
                    },
                  )
                : fill(
                    inserting
                      ? text.timeline.insertRefused
                      : text.timeline.moveRefused,
                    {
                      reason: moveRefusalText(
                        text.timeline.moveRefusals,
                        directGesture.draft.reason,
                      ),
                    },
                  )
              : "";

  const orderedNavigationClips = useMemo(
    () =>
      snapshot.clips
        .map((clip) => ({
          clipId: clip.clipId,
          startFrame: clip.startFrame,
          trackOrder:
            tracks.find((track) => track.trackId === clip.trackId)?.order ?? 0,
        }))
        .sort(
          (left, right) =>
            left.trackOrder - right.trackOrder ||
            left.startFrame - right.startFrame ||
            left.clipId.localeCompare(right.clipId),
        ),
    [snapshot.clips, tracks],
  );
  const select = (
    clip: CompositionClip,
    mode: "replace" | "toggle" | "range",
  ) => {
    const anchor = selectionAnchor.current ?? selection[0] ?? clip.clipId;
    const next =
      mode === "range"
        ? contiguousClipSelection(orderedNavigationClips, anchor, clip.clipId)
        : mode === "toggle"
          ? selected.has(clip.clipId)
            ? selection.filter((member) => member !== clip.clipId)
            : [...selection, clip.clipId]
          : [clip.clipId];
    if (
      next.length === selection.length &&
      next.every((member, index) => member === selection[index])
    ) {
      setFocusedClipId(clip.clipId);
      return;
    }
    // IMPORTANT: focus is part of the same selection activation. Queue it before the async
    // transaction changes the external authoring store so React batches the local focus and
    // pending projection; placing it afterwards creates a fifth overlay commit for selection.
    setFocusedClipId(clip.clipId);
    void onIntent({
      action: "apply_timeline_commands",
      commands: [build.selectClips(next)],
    });
    if (mode !== "range") selectionAnchor.current = clip.clipId;
  };

  const revealClip = (clip: CompositionClip) => {
    setFocusedClipId(clip.clipId);
    const trackIndex = tracks.findIndex(
      (track) => track.trackId === clip.trackId,
    );
    const element = scrollRef.current;
    if (trackIndex >= 0 && element !== null) {
      const rowTop = trackIndex * NLE_ROW_HEIGHT_PX;
      const rowBottom = rowTop + NLE_ROW_HEIGHT_PX;
      const nextScrollTop =
        rowTop < element.scrollTop
          ? rowTop
          : rowBottom > element.scrollTop + element.clientHeight
            ? rowBottom - element.clientHeight
            : element.scrollTop;
      if (nextScrollTop !== element.scrollTop) {
        element.scrollTop = nextScrollTop;
        setScrollTop(nextScrollTop);
      }
    }
    const frameWidth = Math.max(1, laneWidth / pixelsPerFrame);
    if (clip.startFrame < viewStart || clipEnd(clip) > viewStart + frameWidth) {
      setTimelineView((current) =>
        Object.freeze({
          ...current,
          fitLocked: false,
          viewStart: clampViewStart(
            clip.startFrame - frameWidth / 4,
            durationFrames,
            laneWidth,
            current.pixelsPerFrame,
          ),
        }),
      );
    }
  };
  const selectAdjacent = (direction: -1 | 1) => {
    const id = adjacentClipId(
      orderedNavigationClips,
      focusedClipId ?? selection[0] ?? null,
      direction,
    );
    const clip = snapshot.clips.find((member) => member.clipId === id);
    if (clip === undefined) return;
    pendingFocusClipId.current = clip.clipId;
    revealClip(clip);
    select(clip, clickSelectionMode);
  };
  const replaceSelection = (ids: readonly string[]) => {
    if (
      ids.length === selection.length &&
      ids.every((id, index) => id === selection[index])
    )
      return;
    void onIntent({
      action: "apply_timeline_commands",
      commands: [build.selectClips(ids)],
    });
  };

  const zoomBy = (factor: number, anchorX?: number) => {
    setTimelineView((current) => {
      const logicalFrame = playheadFrameRef.current;
      const resolvedAnchor =
        anchorX ??
        (logicalFrame !== null &&
        logicalFrame >= current.viewStart &&
        logicalFrame <= current.viewStart + laneWidth / current.pixelsPerFrame
          ? (logicalFrame - current.viewStart) * current.pixelsPerFrame
          : laneWidth / 2);
      return zoomTimelineViewport({
        current,
        nextScale: current.pixelsPerFrame * factor,
        anchorX: resolvedAnchor,
        laneWidth,
        durationFrames,
      });
    });
  };
  const fit = () => {
    setTimelineView(
      fitTimelineContentView({
        laneWidth: laneWidth + LANE_LEAD_IN_PX,
        contentEndExclusive: transportEndExclusive,
        editCapacityFrames: durationFrames,
        fps,
        lastValidScale: pixelsPerFrame,
      }),
    );
  };
  const zoomSelection = () => {
    const chosen = snapshot.clips.filter((clip) => selected.has(clip.clipId));
    if (chosen.length === 0) return;
    const next = zoomToSelection({
      startFrame: Math.min(...chosen.map((clip) => clip.startFrame)),
      endFrame: Math.max(...chosen.map(clipEnd)),
      laneWidth,
      durationFrames,
    });
    setTimelineView(
      Object.freeze({
        pixelsPerFrame: next.scale,
        viewStart: next.viewStart,
        fitLocked: false,
      }),
    );
  };
  const zoomPosition =
    scaleBounds.max === scaleBounds.min
      ? 0
      : ((Math.log(pixelsPerFrame) - Math.log(scaleBounds.min)) /
          (Math.log(scaleBounds.max) - Math.log(scaleBounds.min))) *
        1_000;
  const setZoomPosition = (position: number) => {
    const ratio = Math.min(1, Math.max(0, position / 1_000));
    const nextScale = Math.exp(
      Math.log(scaleBounds.min) +
        ratio * (Math.log(scaleBounds.max) - Math.log(scaleBounds.min)),
    );
    zoomBy(nextScale / pixelsPerFrame);
  };

  nativeWheelHandlerRef.current = (event) => {
    const target = event.target;
    if (
      !(target instanceof Element) ||
      target.closest("input,textarea,select,[contenteditable='true']") !== null
    )
      return;
    const element = scrollRef.current;
    if (element === null) return;
    const modeScale =
      event.deltaMode === WheelEvent.DOM_DELTA_LINE
        ? 16
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
          ? Math.max(1, element.clientHeight)
          : 1;
    if (event.ctrlKey || event.metaKey) {
      const delta = event.deltaY * modeScale;
      if (!Number.isFinite(delta) || delta === 0) return;
      const rect = element.getBoundingClientRect();
      const anchorX = Math.min(
        laneWidthRef.current,
        Math.max(
          0,
          contentXFromClientX(event.clientX, rect.left, headerWidthRef.current),
        ),
      );
      setTimelineView((current) =>
        zoomTimelineViewport({
          current,
          nextScale:
            current.pixelsPerFrame * (delta < 0 ? Math.SQRT2 : 1 / Math.SQRT2),
          anchorX,
          laneWidth: laneWidthRef.current,
          durationFrames: durationFramesRef.current,
        }),
      );
      event.preventDefault();
      return;
    }
    if (!(event.shiftKey || event.altKey)) return;
    const delta =
      (event.deltaX !== 0 ? event.deltaX : event.deltaY) * modeScale;
    if (!Number.isFinite(delta) || delta === 0) return;
    setTimelineView((current) =>
      Object.freeze({
        ...current,
        fitLocked: false,
        viewStart: clampViewStart(
          current.viewStart + delta / current.pixelsPerFrame,
          durationFramesRef.current,
          laneWidthRef.current,
          current.pixelsPerFrame,
        ),
      }),
    );
    event.preventDefault();
  };

  useEffect(() => {
    const element = scrollRef.current;
    if (element === null) return;
    const onWheel = (event: WheelEvent) => nativeWheelHandlerRef.current(event);
    // IMPORTANT: browser zoom/pan is cancellable only through this one scoped non-passive owner.
    // Reintroducing JSX onWheel or a document listener duplicates the intent or blocks unrelated UI.
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, []);

  const gestureIdentity = (clipIds: readonly string[]) =>
    Object.freeze({
      workspaceHandle: timelineAuthority.workspaceHandle,
      workspaceRevision: timelineAuthority.workspaceRevision,
      timelineRevision: timelineAuthority.timelineRevision,
      timelineFingerprint: timelineAuthority.timelineFingerprint,
      ...capturedAuthoringIdentity,
      mappingKey,
      clipIds: Object.freeze([...clipIds]),
    });

  const targetTracksFor = (
    origins: readonly CompositionClip[],
    rowDelta: number,
  ) =>
    origins.map((clip) => {
      const index = tracks.findIndex((track) => track.trackId === clip.trackId);
      return tracks[index + rowDelta]?.trackId ?? "";
    });
  const admitMove = (
    origins: readonly CompositionClip[],
    targetTrackIds: readonly string[],
    deltaFrames: number,
  ) =>
    admitTimelineMove({
      origins,
      targetTrackIds,
      deltaFrames,
      durationFrames,
      tracks,
      clips: snapshot.clips,
      assets: snapshot.assets,
    });
  const snapMove = (
    origins: readonly CompositionClip[],
    deltaFrames: number,
    suspended: boolean,
  ) => {
    if (!snapEnabled || suspended || origins.length === 0) return null;
    const moving = new Set(origins.map((clip) => clip.clipId));
    const rawStart =
      Math.min(...origins.map((clip) => clip.startFrame)) + deltaFrames;
    const rawEnd = Math.max(...origins.map(clipEnd)) + deltaFrames;
    const candidates: TimelineSnapTarget[] = snapshot.clips
      .filter((clip) => !moving.has(clip.clipId))
      .flatMap((clip) => [
        { frame: clip.startFrame, kind: "clip_start" as const },
        { frame: clipEnd(clip), kind: "clip_end" as const },
      ]);
    if (playheadFrameRef.current !== null)
      candidates.push({ frame: playheadFrameRef.current, kind: "playhead" });
    const grid = Math.max(1, gridFrames);
    const gridBase = Math.round(rawStart / grid) * grid;
    candidates.push(
      { frame: gridBase, kind: "grid" },
      { frame: Math.round(rawEnd / grid) * grid, kind: "grid" },
    );
    const winner = selectTimelineSnap(
      rawStart,
      rawEnd,
      candidates,
      pixelsPerFrame,
    );
    return winner === null
      ? null
      : {
          deltaFrames: deltaFrames + winner.deltaFrames,
          lineFrame: winner.lineFrame,
        };
  };

  const stopAutoScroll = () => {
    const controller = autoScrollRef.current;
    if (controller.raf !== null) cancelAnimationFrame(controller.raf);
    controller.raf = null;
    controller.lastTime = 0;
  };
  const autoScrollVelocity = (
    coordinate: number,
    low: number,
    high: number,
  ) => {
    const edge = 32;
    if (coordinate < low + edge)
      return -480 * Math.min(1, Math.max(0, (low + edge - coordinate) / edge));
    if (coordinate > high - edge)
      return (
        480 * Math.min(1, Math.max(0, (coordinate - (high - edge)) / edge))
      );
    return 0;
  };
  const autoScrollTick = (time: number) => {
    const controller = autoScrollRef.current;
    controller.raf = null;
    const current = directGestureRef.current;
    const element = scrollRef.current;
    if (current.phase !== "dragging" || element === null || document.hidden) {
      stopAutoScroll();
      return;
    }
    const rect = element.getBoundingClientRect();
    const horizontalVelocity = autoScrollVelocity(
      controller.pointerX,
      rect.left + headerWidthRef.current,
      rect.right,
    );
    const verticalVelocity = autoScrollVelocity(
      controller.pointerY,
      rect.top,
      rect.bottom,
    );
    const elapsed = Math.min(
      50,
      Math.max(0, time - (controller.lastTime || time)),
    );
    controller.lastTime = time;
    const scale = pixelsPerFrameRef.current;
    const previousView = viewStartRef.current;
    const nextView = clampViewStart(
      previousView + (horizontalVelocity * elapsed) / 1_000 / scale,
      durationFrames,
      laneWidth,
      scale,
    );
    const previousScroll = element.scrollTop;
    if (verticalVelocity !== 0)
      element.scrollTop = Math.max(
        0,
        Math.min(
          element.scrollHeight - element.clientHeight,
          previousScroll + (verticalVelocity * elapsed) / 1_000,
        ),
      );
    const scrollDelta = element.scrollTop - previousScroll;
    if (nextView !== previousView || scrollDelta !== 0) {
      if (
        current.draft.kind === "insert_from_bin" &&
        current.draft.insert !== null
      ) {
        if (nextView !== previousView) {
          autoScrollMappingRef.current = true;
          viewStartRef.current = nextView;
          setTimelineView((current) =>
            Object.freeze({
              ...current,
              fitLocked: false,
              viewStart: nextView,
            }),
          );
        }
        const next = updateBinInsertDraft(
          current,
          controller.pointerX,
          controller.pointerY,
          nextView,
        );
        directGestureRef.current = next;
        setDirectGesture(next);
        if (horizontalVelocity !== 0 || verticalVelocity !== 0)
          controller.raf = requestAnimationFrame(autoScrollTick);
        return;
      }
      const desiredDelta =
        current.draft.deltaFrames + (nextView - previousView);
      let next = reduceNleTimelineGesture(current, {
        type: "auto_scroll_reanchor",
        mappingKey: `${scale}:${nextView}:${laneWidth}`,
        originClientX: controller.pointerX - desiredDelta * scale,
        originClientY: current.draft.originClientY - scrollDelta,
        originFrame: current.draft.originFrame,
      });
      const origins = current.draft.origins
        .map((origin) =>
          snapshot.clips.find((clip) => clip.clipId === origin.clipId),
        )
        .filter((clip): clip is CompositionClip => clip !== undefined);
      const rowDelta = Math.round(
        (controller.pointerY -
          (next.phase === "dragging"
            ? next.draft.originClientY
            : current.draft.originClientY)) /
          NLE_ROW_HEIGHT_PX,
      );
      const targets = targetTracksFor(origins, rowDelta);
      const admission = admitMove(origins, targets, desiredDelta);
      next = reduceNleTimelineGesture(next, {
        type: "move",
        clientX: controller.pointerX,
        clientY: controller.pointerY,
        targetTrackIds: targets,
        snap: null,
        ...admission,
      });
      directGestureRef.current = next;
      setDirectGesture(next);
      if (nextView !== previousView) {
        autoScrollMappingRef.current = true;
        viewStartRef.current = nextView;
        setTimelineView((current) =>
          Object.freeze({
            ...current,
            fitLocked: false,
            viewStart: nextView,
          }),
        );
      }
    }
    if (horizontalVelocity !== 0 || verticalVelocity !== 0)
      controller.raf = requestAnimationFrame(autoScrollTick);
  };
  const updateAutoScroll = (clientX: number, clientY: number) => {
    const controller = autoScrollRef.current;
    controller.pointerX = clientX;
    controller.pointerY = clientY;
    const element = scrollRef.current;
    if (element === null) return;
    const rect = element.getBoundingClientRect();
    const active =
      autoScrollVelocity(
        clientX,
        rect.left + headerWidthRef.current,
        rect.right,
      ) !== 0 || autoScrollVelocity(clientY, rect.top, rect.bottom) !== 0;
    if (!active) {
      stopAutoScroll();
      return;
    }
    if (controller.raf === null) {
      controller.lastTime = 0;
      controller.raf = requestAnimationFrame(autoScrollTick);
    }
  };

  const updateBinInsertDraft = (
    current: NleTimelineGestureState,
    clientX: number,
    clientY: number,
    mappedViewStart = viewStartRef.current,
  ): NleTimelineGestureState => {
    if (
      current.phase !== "dragging" ||
      current.draft.kind !== "insert_from_bin" ||
      current.draft.insert === null
    )
      return current;
    const element = scrollRef.current;
    const insert = current.draft.insert;
    const asset = snapshot.assets.find(
      (candidate) => candidate.assetId === insert.assetId,
    );
    if (element === null || asset === undefined)
      return reduceNleTimelineGesture(current, {
        type: "update_insert_from_bin",
        clientX,
        clientY,
        targetFrame: 0,
        targetTrackId: null,
        durationFrames: insert.durationFrames,
        admitted: false,
        reason: "target_exhaustion",
      });
    const rect = element.getBoundingClientRect();
    const inside =
      clientX >= rect.left + headerWidthRef.current + LANE_LEAD_IN_PX &&
      clientX <= rect.right &&
      clientY >= rect.top &&
      clientY <= rect.bottom;
    const row = Math.floor(
      (clientY - rect.top + element.scrollTop) / NLE_ROW_HEIGHT_PX,
    );
    const targetTrack = inside ? tracks[row] : undefined;
    const targetFrame = frameFromX(
      contentXFromClientX(clientX, rect.left, headerWidthRef.current),
      mappedViewStart,
      pixelsPerFrameRef.current,
      durationFrames,
    );
    const clippedDuration = Math.min(
      insert.sourceDurationFrames,
      durationFrames - targetFrame,
    );
    const admission =
      targetTrack === undefined || timelineAuthority.clips.length >= 128
        ? {
            admitted: false,
            reason: "target_exhaustion" as const,
            trackId: null,
          }
        : admitTimelineInsert({
            asset,
            startFrame: targetFrame,
            durationFrames: clippedDuration,
            timelineDurationFrames: durationFrames,
            tracks,
            clips: snapshot.clips,
            targetTrackId: targetTrack.trackId,
          });
    return reduceNleTimelineGesture(current, {
      type: "update_insert_from_bin",
      clientX,
      clientY,
      targetFrame,
      targetTrackId: admission.trackId,
      durationFrames: clippedDuration,
      admitted: admission.admitted,
      reason: admission.reason,
    });
  };

  const beginMove = (
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ) => {
    if (!draftable || event.button !== 0 || event.isPrimary === false) return;
    const movingIds = selected.has(clip.clipId) ? selection : [clip.clipId];
    const origins = snapshot.clips.filter((member) =>
      movingIds.includes(member.clipId),
    );
    if (origins.length === 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    captureRef.current = {
      element: event.currentTarget,
      pointerId: event.pointerId,
    };
    onBeginTrim?.();
    const next = reduceNleTimelineGesture(directGestureRef.current, {
      type: "begin_move",
      input: "pointer",
      identity: gestureIdentity(movingIds),
      pointerId: event.pointerId,
      originClientX: event.clientX,
      originClientY: event.clientY,
      pixelsPerFrame,
      origins: origins.map((member) => ({
        clipId: member.clipId,
        trackId: member.trackId,
        startFrame: member.startFrame,
      })),
      trackIds: tracks.map((track) => track.trackId),
      targetTrackIds: origins.map((member) => member.trackId),
    });
    // IMPORTANT: pointerdown is still a possible selection click. Keep its captured draft in the
    // ref until movement reaches the 4 px drag threshold; rendering it immediately makes every
    // click pay a begin/end gesture pair before its selection transaction and breaks the <=4
    // accepted-edit commit ceiling. The first qualifying pointermove publishes the same draft.
    directGestureRef.current = next;
  };
  const moveDirect = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = directGestureRef.current;
    if (
      current.phase !== "dragging" ||
      current.draft.pointerId !== event.pointerId
    )
      return;
    const origins = current.draft.origins
      .map((origin) =>
        snapshot.clips.find((clip) => clip.clipId === origin.clipId),
      )
      .filter((clip): clip is CompositionClip => clip !== undefined);
    const rowDelta = Math.round(
      (event.clientY - current.draft.originClientY) / NLE_ROW_HEIGHT_PX,
    );
    const targets = targetTracksFor(origins, rowDelta);
    const rawDelta =
      quantizeSignedDelta(
        (event.clientX - current.draft.originClientX) / pixelsPerFrame,
      ) ?? 0;
    const snapped = snapMove(origins, rawDelta, event.altKey);
    const delta = snapped?.deltaFrames ?? rawDelta;
    const admission = admitMove(origins, targets, delta);
    const next = reduceNleTimelineGesture(current, {
      type: "move",
      clientX: event.clientX,
      clientY: event.clientY,
      targetTrackIds: targets,
      snap:
        snapped === null
          ? null
          : current.draft.origins[0]!.startFrame + snapped.deltaFrames,
      snapLine: snapped?.lineFrame ?? null,
      ...admission,
    });
    directGestureRef.current = next;
    if (next.phase !== "dragging" || next.draft.movementPx < DRAG_THRESHOLD_PX)
      return;
    updateAutoScroll(event.clientX, event.clientY);
    setDirectGesture(next);
  };
  const submitDirect = (next: NleTimelineGestureState) => {
    if (
      next.phase !== "submitting" ||
      submittedDirectRequest.current === next.requestId
    )
      return;
    const command = commandForGesture(next);
    if (command === null) return;
    submittedDirectRequest.current = next.requestId;
    directPreviousReceipt.current = receiptId;
    const commands = [];
    const captured = next.draft.identity.clipIds;
    if (captured.some((id) => !selection.includes(id)))
      commands.push(build.selectClips(captured));
    if (command.kind === "move_clip")
      commands.push(
        build.moveClip(
          command.clipId,
          command.deltaFrames,
          command.targetTrackId,
        ),
      );
    else if (command.kind === "move_group")
      commands.push(
        build.moveGroup(
          command.clipIds.map(
            (clipId, index) =>
              [clipId, command.targetTrackIds[index]!] as const,
          ),
          command.deltaFrames,
        ),
      );
    else
      commands.push(
        build.insertAssetClip({
          clipId: freshIdentifier(timelineAuthority, "clip"),
          trackId: command.targetTrackId,
          assetId: command.assetId,
          startFrame: command.startFrame,
          durationFrames: command.durationFrames,
          sourceStartFrame: 0,
          text: null,
        }),
      );
    void onIntent({
      action: "apply_timeline_commands",
      capturedTimeline: {
        workspaceHandle: next.draft.identity.workspaceHandle,
        workspaceRevision: next.draft.identity.workspaceRevision,
        timelineRevision: next.draft.identity.timelineRevision,
        timelineFingerprint: next.draft.identity.timelineFingerprint,
        ...("authoringFingerprint" in next.draft.identity &&
        next.draft.identity.authoringFingerprint !== undefined
          ? { authoringFingerprint: next.draft.identity.authoringFingerprint }
          : {}),
      },
      commands,
    });
  };
  useEffect(() => {
    if (binInsert === undefined) return;
    let ownedPointer: number | null = null;
    const unsubscribe = binInsert.subscribe((event) => {
      if (event.type === "begin") {
        const authority = event.authority;
        if (
          !draftable ||
          authority.workspaceHandle !== timelineAuthority.workspaceHandle ||
          authority.workspaceRevision !== timelineAuthority.workspaceRevision ||
          authority.timelineRevision !== timelineAuthority.timelineRevision ||
          authority.timelineFingerprint !==
            timelineAuthority.timelineFingerprint ||
          authority.authoringFingerprint !== authoringFingerprint ||
          !snapshot.assets.some((asset) => asset.assetId === authority.assetId)
        ) {
          binInsert.cancel("authority_unavailable", event.pointerId);
          return;
        }
        const next = reduceNleTimelineGesture(directGestureRef.current, {
          type: "begin_insert_from_bin",
          identity: {
            workspaceHandle: authority.workspaceHandle,
            workspaceRevision: authority.workspaceRevision,
            timelineRevision: authority.timelineRevision,
            timelineFingerprint: authority.timelineFingerprint,
            ...(authority.authoringFingerprint === undefined
              ? {}
              : { authoringFingerprint: authority.authoringFingerprint }),
            mappingKey,
            clipIds: [],
          },
          pointerId: event.pointerId,
          originClientX: event.clientX,
          originClientY: event.clientY,
          pixelsPerFrame: pixelsPerFrameRef.current,
          assetId: authority.assetId,
          durationFrames: authority.durationFrames,
        });
        if (next === directGestureRef.current || next.phase !== "dragging") {
          binInsert.cancel("gesture_busy", event.pointerId);
          return;
        }
        ownedPointer = event.pointerId;
        directGestureRef.current = next;
        onBeginTrim?.();
        return;
      }
      const current = directGestureRef.current;
      if (
        current.phase !== "dragging" ||
        current.draft.kind !== "insert_from_bin" ||
        current.draft.pointerId !== event.pointerId
      )
        return;
      if (event.type === "cancel") {
        ownedPointer = null;
        stopAutoScroll();
        const next = reduceNleTimelineGesture(current, {
          type: "cancel",
          reason: event.reason,
        });
        directGestureRef.current = next;
        setDirectGesture(next);
        return;
      }
      const updated = updateBinInsertDraft(
        current,
        event.clientX,
        event.clientY,
      );
      if (event.type === "move") {
        directGestureRef.current = updated;
        if (
          updated.phase !== "dragging" ||
          updated.draft.movementPx < DRAG_THRESHOLD_PX
        )
          return;
        updateAutoScroll(event.clientX, event.clientY);
        setDirectGesture(updated);
        return;
      }
      stopAutoScroll();
      ownedPointer = null;
      const next = reduceNleTimelineGesture(updated, {
        type: "release",
        requestId: `insert-${Date.now()}-${event.pointerId}`,
      });
      directGestureRef.current = next;
      if (next.phase === "idle") {
        setDirectGesture(next);
        return;
      }
      setDirectGesture(next);
      submitDirect(next);
    });
    return () => {
      if (ownedPointer !== null)
        binInsert.cancel("receiver_rebound_or_unmounted", ownedPointer);
      unsubscribe();
    };
  }, [
    binInsert,
    draftable,
    mappingKey,
    snapshot,
    tracks,
    durationFrames,
    onBeginTrim,
    timelineAuthority.workspaceHandle,
    timelineAuthority.workspaceRevision,
    timelineAuthority.timelineRevision,
    timelineAuthority.timelineFingerprint,
    authoringFingerprint,
  ]);
  const releaseDirect = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = directGestureRef.current;
    if (
      current.phase !== "dragging" ||
      current.draft.pointerId !== event.pointerId
    )
      return;
    const next = reduceNleTimelineGesture(current, {
      type: "release",
      requestId: `move-${Date.now()}-${event.pointerId}`,
    });
    stopAutoScroll();
    suppressClickRef.current = current.draft.movementPx >= DRAG_THRESHOLD_PX;
    directGestureRef.current = next;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    // A sub-threshold pointer sequence was never published to React, so there is no gesture state
    // to clear. Its following click remains the one selection activation.
    if (next.phase === "idle") return;
    setDirectGesture(next);
    submitDirect(next);
  };
  const cancelDirect = (reason: string) => {
    const current = directGestureRef.current;
    if (current.phase !== "dragging" && current.phase !== "keyboard_draft")
      return;
    stopAutoScroll();
    releaseCapture();
    const next = reduceNleTimelineGesture(current, { type: "cancel", reason });
    directGestureRef.current = next;
    if (
      current.phase === "dragging" &&
      current.draft.kind === "insert_from_bin"
    )
      binInsert?.cancel(reason, current.draft.pointerId ?? undefined);
    if (publishedDirectGesture.current !== next) setDirectGesture(next);
  };
  const clickClip = (
    clip: CompositionClip,
    mode: "replace" | "toggle" | "range",
  ) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    select(clip, mode);
  };
  const beginDirectKeyboard = (
    clip: CompositionClip,
    input: "keyboard" | "click",
  ) => {
    const movingIds = selected.has(clip.clipId) ? selection : [clip.clipId];
    const origins = snapshot.clips.filter((member) =>
      movingIds.includes(member.clipId),
    );
    return reduceNleTimelineGesture(directGestureRef.current, {
      type: "begin_move",
      input,
      identity: gestureIdentity(movingIds),
      pointerId: null,
      originClientX: 0,
      originClientY: 0,
      pixelsPerFrame,
      origins: origins.map((member) => ({
        clipId: member.clipId,
        trackId: member.trackId,
        startFrame: member.startFrame,
      })),
      trackIds: tracks.map((track) => track.trackId),
      targetTrackIds: origins.map((member) => member.trackId),
    });
  };
  const stepDirectKeyboard = (
    current: NleTimelineGestureState,
    deltaFrames: number,
    rowDelta: number,
  ) => {
    if (current.phase !== "keyboard_draft") return current;
    const origins = current.draft.origins
      .map((origin) =>
        snapshot.clips.find((clip) => clip.clipId === origin.clipId),
      )
      .filter((clip): clip is CompositionClip => clip !== undefined);
    const sourceAnchor = tracks.findIndex(
      (track) => track.trackId === origins[0]?.trackId,
    );
    const targetAnchor = tracks.findIndex(
      (track) => track.trackId === current.draft.targetTrackIds[0],
    );
    const currentRowDelta =
      sourceAnchor < 0 || targetAnchor < 0 ? 0 : targetAnchor - sourceAnchor;
    const targets = targetTracksFor(origins, currentRowDelta + rowDelta);
    const nextDelta = current.draft.deltaFrames + deltaFrames;
    const admission = admitMove(origins, targets, nextDelta);
    return reduceNleTimelineGesture(current, {
      type: "step",
      deltaFrames,
      targetTrackIds: targets,
      ...admission,
    });
  };
  const clipKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ) => {
    const current = directGestureRef.current;
    if (current.phase === "keyboard_draft") {
      if (!current.draft.identity.clipIds.includes(clip.clipId)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        cancelDirect("escape");
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        const next = reduceNleTimelineGesture(current, {
          type: "commit",
          requestId: `move-k-${Date.now()}`,
        });
        setDirectGesture(next);
        submitDirect(next);
        return;
      }
      const horizontal =
        event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
      const vertical =
        event.altKey && event.key === "ArrowUp"
          ? -1
          : event.altKey && event.key === "ArrowDown"
            ? 1
            : 0;
      if (horizontal === 0 && vertical === 0) return;
      event.preventDefault();
      event.stopPropagation();
      const step = event.shiftKey ? Math.max(1, gridFrames) : 1;
      setDirectGesture(
        stepDirectKeyboard(current, horizontal * step, vertical),
      );
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      const next = beginDirectKeyboard(clip, "keyboard");
      setDirectGesture(next);
      return;
    }
    if (event.key === " " || event.code === "Space") {
      event.preventDefault();
      event.stopPropagation();
      select(clip, event.ctrlKey || event.metaKey ? "toggle" : "replace");
      return;
    }
    const direction =
      event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : null;
    const endpoint = event.key === "Home" ? 0 : event.key === "End" ? -1 : null;
    if (direction === null && endpoint === null) return;
    event.preventDefault();
    event.stopPropagation();
    const ordered = [...orderedNavigationClips].sort(
      (left, right) =>
        left.trackOrder - right.trackOrder ||
        left.startFrame - right.startFrame ||
        left.clipId.localeCompare(right.clipId),
    );
    const targetId =
      endpoint === null
        ? adjacentClipId(ordered, clip.clipId, direction!)
        : (ordered.at(endpoint)?.clipId ?? null);
    const target = snapshot.clips.find((member) => member.clipId === targetId);
    if (target === undefined) return;
    pendingFocusClipId.current = target.clipId;
    revealClip(target);
    if (event.shiftKey) select(target, "range");
  };
  const applyClickMove = () => {
    if (clickMove === null) return;
    const clip = snapshot.clips.find(
      (member) => member.clipId === clickMove.clipId,
    );
    if (clip === undefined) return;
    let next = beginDirectKeyboard(clip, "click");
    next = stepDirectKeyboard(next, clickMove.deltaFrames, clickMove.rowDelta);
    if (next.phase === "keyboard_draft" && !next.draft.admitted) {
      setDirectGesture(next);
      return;
    }
    next = reduceNleTimelineGesture(next, {
      type: "commit",
      requestId: `move-c-${Date.now()}`,
    });
    setClickMove(null);
    setDirectGesture(next);
    submitDirect(next);
  };
  useEffect(() => {
    const cancelUncommitted = () => {
      cancelPointer("window_blur");
      cancelDirect("window_blur");
      cancelRoll();
    };
    const visibility = () => {
      if (document.hidden) cancelUncommitted();
    };
    window.addEventListener("blur", cancelUncommitted);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      stopAutoScroll();
      const scrub = scrubRef.current;
      if (scrub !== null && scrub.raf !== null) cancelAnimationFrame(scrub.raf);
      scrubRef.current = null;
      window.removeEventListener("blur", cancelUncommitted);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);

  const ruler = useMemo(
    () =>
      rulerMarks({ viewStart, laneWidth, pixelsPerFrame, durationFrames, fps }),
    [durationFrames, fps, laneWidth, pixelsPerFrame, viewStart],
  );
  // M25-61: minor ticks live in the ruler only, as one path; nothing periodic enters the tracks.
  const rulerMinorPath = useMemo(
    () =>
      rulerMinorTicks({
        viewStart,
        laneWidth,
        pixelsPerFrame,
        durationFrames,
        fps,
      })
        .map(
          (tick) =>
            `M${laneXForBoundary(tick.frame, viewStart, pixelsPerFrame).toFixed(1)} 0V1`,
        )
        .join(""),
    [durationFrames, fps, laneWidth, pixelsPerFrame, viewStart],
  );
  const transportOwnerKey = `${snapshot.workspaceHandle}\u0000${snapshot.timelineFingerprint}`;
  const [rulerRequestedFrame, setRulerRequestedFrame] = useState<number | null>(
    null,
  );
  const rulerRequestedFrameRef = useRef(rulerRequestedFrame);
  // IMPORTANT (B-M2564-05): transport frames reach this component through its channel without a
  // render, so playback does not re-render the editor, but the toolbar's states are computed at
  // render. `apply` re-renders only when a tool decision would flip, which a frame step or
  // playback from the monitor's own transport does not otherwise cause: Split kept the state of
  // the frame last rendered.
  const [, setToolRefresh] = useState(0);
  const renderedToolKeyRef = useRef("");
  const currentToolKeyRef = useRef<() => string>(() => "");
  const rulerQueuedAfterGenerationRef = useRef(0);
  const rulerAcknowledgedGenerationRef = useRef<number | null>(null);
  const latestMonitorRequestGenerationRef = useRef(0);
  const rulerClearReasonRef = useRef("never_requested");
  const clearRulerRequestedFrame = (reason: string, expected?: number) => {
    if (
      rulerRequestedFrameRef.current === null ||
      (expected !== undefined && rulerRequestedFrameRef.current !== expected)
    )
      return;
    rulerRequestedFrameRef.current = null;
    rulerAcknowledgedGenerationRef.current = null;
    rulerClearReasonRef.current = reason;
    setRulerRequestedFrame(null);
  };
  const rulerFrame = Math.min(
    transportLastFrame,
    Math.max(0, rulerRequestedFrame ?? playheadFrameRef.current ?? 0),
  );
  useEffect(() => {
    // IMPORTANT: an idle owner change must not enqueue a no-op render; accepted edits share the
    // frozen four-commit budget with the monitor replacement that changes this fingerprint.
    clearRulerRequestedFrame("owner_replaced");
  }, [transportOwnerKey]);
  useLayoutEffect(() => {
    const apply = () => {
      const next =
        transportChannel?.snapshot() ??
        ({
          frame: playheadFrame,
          request: null,
          settledRequestGeneration: 0,
          transportAvailable,
          navigationAvailable,
        } satisfies NleTimelineTransportSnapshot);
      latestMonitorRequestGenerationRef.current = Math.max(
        latestMonitorRequestGenerationRef.current,
        next.request?.generation ?? 0,
        next.settledRequestGeneration,
      );
      const visibleMonitorFrame =
        next.frame === null
          ? null
          : Math.min(transportLastFrame, Math.max(0, next.frame));
      playheadFrameRef.current = visibleMonitorFrame;
      transportAvailableRef.current = next.transportAvailable;
      const canNavigate = next.navigationAvailable ?? true;
      navigationAvailableRef.current = canNavigate;
      if (!canNavigate) {
        const scrub = scrubRef.current;
        if (scrub?.raf !== null && scrub?.raf !== undefined)
          cancelAnimationFrame(scrub.raf);
        scrubRef.current = null;
        clearRulerRequestedFrame("navigation_unavailable");
      }
      const visibleFrame = Math.min(
        transportLastFrame,
        Math.max(0, rulerRequestedFrameRef.current ?? visibleMonitorFrame ?? 0),
      );
      const rulerElement = rulerRef.current;
      if (rulerElement !== null) {
        rulerElement.setAttribute("aria-valuenow", String(visibleFrame));
        rulerElement.setAttribute(
          "aria-valuetext",
          formatTimelineTimecode(visibleFrame, fps),
        );
        rulerElement.setAttribute("aria-disabled", String(!canNavigate));
        rulerElement.tabIndex = canNavigate ? 0 : -1;
      }
      if (previousFrameRef.current !== null)
        previousFrameRef.current.disabled = !canNavigate;
      if (nextFrameRef.current !== null)
        nextFrameRef.current.disabled = !canNavigate;
      const playheadElement = playheadElementRef.current;
      if (playheadElement !== null) {
        const visible =
          visibleMonitorFrame !== null &&
          visibleMonitorFrame >= viewStartRef.current &&
          visibleMonitorFrame <
            viewStartRef.current + laneWidth / pixelsPerFrameRef.current;
        playheadElement.hidden = !visible;
        // M25-62: the layer's origin is the header edge, the same origin as each lane.
        if (visible)
          playheadElement.style.left = `${laneXForBoundary(
            visibleMonitorFrame!,
            viewStartRef.current,
            pixelsPerFrameRef.current,
          )}px`;
      }
      const requested = rulerRequestedFrameRef.current;
      if (requested !== null) {
        if (
          next.request !== null &&
          next.request.generation > rulerQueuedAfterGenerationRef.current &&
          next.request.frame === requested
        )
          rulerAcknowledgedGenerationRef.current = next.request.generation;
        const acknowledged = rulerAcknowledgedGenerationRef.current;
        if (
          acknowledged !== null &&
          next.settledRequestGeneration >= acknowledged
        )
          // IMPORTANT: frame equality is not request identity. Home can target an already shown
          // zero, and two generations can target the same frame; only the acknowledged generation
          // settling may clear the ruler or stale work makes three PageUps land on 96, not 72.
          clearRulerRequestedFrame("monitor_request_settled", requested);
      }
      if (currentToolKeyRef.current() !== renderedToolKeyRef.current)
        setToolRefresh((count) => count + 1);
    };
    apply();
    return transportChannel?.subscribe(apply);
  }, [
    durationFrames,
    fps,
    laneWidth,
    pixelsPerFrame,
    playheadFrame,
    navigationAvailable,
    transportAvailable,
    transportChannel,
    transportLastFrame,
    viewStart,
  ]);
  const flushRulerSeek = () => {
    const scrub = scrubRef.current;
    if (scrub === null) return;
    if (scrub.raf !== null) cancelAnimationFrame(scrub.raf);
    scrub.raf = null;
    onSeek?.(scrub.targetFrame);
  };
  const queueRulerSeek = (frame: number) => {
    const targetFrame = Math.min(transportLastFrame, Math.max(0, frame));
    // IMPORTANT: keyboard events can batch before React renders. Advance the imperative request
    // cursor synchronously or rapid PageUp presses step from a stale seek and overshoot the ruler.
    rulerRequestedFrameRef.current = targetFrame;
    rulerQueuedAfterGenerationRef.current =
      latestMonitorRequestGenerationRef.current;
    rulerAcknowledgedGenerationRef.current = null;
    rulerClearReasonRef.current = "pending_monitor_acknowledgement";
    setRulerRequestedFrame(targetFrame);
    const scrub = scrubRef.current;
    if (scrub === null) {
      onSeek?.(targetFrame);
      return;
    }
    scrub.targetFrame = targetFrame;
    if (scrub.raf !== null) return;
    scrub.raf = requestAnimationFrame(() => {
      const current = scrubRef.current;
      if (current !== scrub || current.ownerKey !== transportOwnerKey) return;
      current.raf = null;
      onSeek?.(current.targetFrame);
    });
  };
  /** One scrub owner for the ruler and the playhead head: release and cancel restore it alike. */
  const beginRulerScrub = (pointerId: number) => {
    onBeginScrub?.();
    scrubRef.current = {
      pointerId,
      ownerKey: transportOwnerKey,
      originFrame:
        rulerRequestedFrameRef.current ?? playheadFrameRef.current ?? 0,
      targetFrame:
        rulerRequestedFrameRef.current ?? playheadFrameRef.current ?? 0,
      raf: null,
    };
  };
  const rulerSeek = (clientX: number, target: HTMLElement) => {
    const rect = target.getBoundingClientRect();
    queueRulerSeek(
      frameFromX(
        contentXFromClientX(clientX, rect.left, headerWidthRef.current),
        viewStart,
        pixelsPerFrame,
        transportEndExclusive,
      ),
    );
  };
  const cancelScrub = () => {
    const scrub = scrubRef.current;
    if (scrub === null) return;
    if (scrub.raf !== null) cancelAnimationFrame(scrub.raf);
    scrubRef.current = null;
    if (scrub.ownerKey === transportOwnerKey) onSeek?.(scrub.originFrame);
    clearRulerRequestedFrame("scrub_cancelled");
  };
  const visibleVideoAssetIds = useMemo(() => {
    const videoAssets = new Set(
      snapshot.assets
        .filter((asset) => asset.kind === "video")
        .map((asset) => asset.assetId),
    );
    return [
      ...new Set(
        visibleCandidates.flatMap((clip) =>
          clip.assetId !== null && videoAssets.has(clip.assetId)
            ? [clip.assetId]
            : [],
        ),
      ),
    ];
  }, [snapshot.assets, visibleCandidates]);
  const filmstrips = useNleFilmstrips({
    snapshot,
    runtimeEpoch,
    leaseClient,
    decorationDemandBroker,
    visibleAssetIds: visibleVideoAssetIds,
  });
  const waveforms = useNleWaveforms({
    snapshot,
    runtimeEpoch,
    leaseClient,
    decorationDemandBroker,
    visibleAssetIds: visibleVideoAssetIds,
  });
  const decorationScene = useCallback((): TimelineDecorationScene => {
    const directDraft =
      directGesture.phase === "dragging" ||
      directGesture.phase === "keyboard_draft"
        ? directGesture.draft
        : null;
    const moving = directDraft?.origins[0];
    const movingClip = snapshot.clips.find(
      (clip) => clip.clipId === moving?.clipId,
    );
    const movingTrackIndex = movingClip
      ? tracks.findIndex((track) => track.trackId === movingClip.trackId)
      : -1;
    const insert = directDraft?.insert ?? null;
    const insertTrackIndex =
      insert?.targetTrackId === null || insert?.targetTrackId === undefined
        ? -1
        : tracks.findIndex((track) => track.trackId === insert.targetTrackId);
    if (isNleSchedulerTraceEnabled()) {
      const target = window as Window & {
        __h3NleWaveformSceneTrace?: Array<Readonly<Record<string, unknown>>>;
      };
      const trace = (target.__h3NleWaveformSceneTrace ??= []);
      for (const clip of visibleCandidates) {
        const track = tracks.find((item) => item.trackId === clip.trackId);
        const asset = snapshot.assets.find(
          (item) => item.assetId === clip.assetId,
        );
        const envelope =
          clip.assetId === null ? undefined : waveforms.get(clip.assetId);
        const accepted =
          track?.kind === "primary_video" &&
          track.enabled &&
          asset?.kind === "video" &&
          asset.embeddedAudio === "present_bound" &&
          asset.sourceTimeBase !== null &&
          asset.sourceFrameCount !== null &&
          asset.sourceSampleCount !== null &&
          envelope !== undefined;
        trace.push({
          event: "waveform.scene.candidate",
          clipId: clip.clipId,
          assetId: clip.assetId,
          accepted,
          trackKind: track?.kind ?? null,
          trackEnabled: track?.enabled ?? null,
          assetKind: asset?.kind ?? null,
          embeddedAudio: asset?.embeddedAudio ?? null,
          sourceTimeBase: asset?.sourceTimeBase ?? null,
          sourceFrameCount: asset?.sourceFrameCount ?? null,
          sourceSampleCount: asset?.sourceSampleCount ?? null,
          envelopePresent: envelope !== undefined,
          pairCount: envelope?.pairCount ?? null,
          pairLength: envelope?.pairs.length ?? null,
        });
      }
      if (trace.length > 256) trace.splice(0, trace.length - 256);
    }
    return {
      width: laneWidth + LANE_LEAD_IN_PX,
      height: Math.min(
        measuredViewportHeightRef.current,
        NLE_MAX_VISIBLE_ROWS * NLE_ROW_HEIGHT_PX,
      ),
      filmstrips: visibleCandidates.flatMap<FilmstripPaintInput>((clip) => {
        const asset = snapshot.assets.find(
          (item) => item.assetId === clip.assetId,
        );
        const decoration =
          clip.assetId === null ? undefined : filmstrips.get(clip.assetId);
        if (
          asset?.kind !== "video" ||
          asset.sourceTimeBase === null ||
          asset.sourceFrameCount === null ||
          decoration === undefined ||
          frameRate === undefined
        )
          return [];
        return [
          {
            asset: {
              ...asset,
              kind: "video",
              sourceTimeBase: asset.sourceTimeBase,
              sourceFrameCount: asset.sourceFrameCount,
              sourceWidth: decoration.sourceWidth,
              sourceHeight: decoration.sourceHeight,
            },
            outputFrameRate: frameRate,
            clip,
            bounds: {
              x: laneXForBoundary(clip.startFrame, viewStart, pixelsPerFrame),
              y:
                (tracks.findIndex((track) => track.trackId === clip.trackId) -
                  firstRow) *
                  NLE_ROW_HEIGHT_PX +
                CLIP_CONTENT_TOP_PX,
              width: Math.max(1, clip.durationFrames * pixelsPerFrame),
              height: CLIP_CONTENT_HEIGHT_PX,
            },
            visible: { x: 0, width: laneWidth + LANE_LEAD_IN_PX },
            sprite: decoration,
          },
        ];
      }),
      waveforms: visibleCandidates.flatMap<WaveformPaintInput>((clip) => {
        const track = tracks.find((item) => item.trackId === clip.trackId);
        const asset = snapshot.assets.find(
          (item) => item.assetId === clip.assetId,
        );
        const envelope =
          clip.assetId === null ? undefined : waveforms.get(clip.assetId);
        if (
          track?.kind !== "primary_video" ||
          !track.enabled ||
          asset?.kind !== "video" ||
          asset.embeddedAudio !== "present_bound" ||
          asset.sourceTimeBase === null ||
          asset.sourceFrameCount === null ||
          asset.sourceSampleCount === null ||
          envelope === undefined ||
          frameRate === undefined
        )
          return [];
        const clipY =
          (tracks.findIndex((track) => track.trackId === clip.trackId) -
            firstRow) *
            NLE_ROW_HEIGHT_PX +
          CLIP_CONTENT_TOP_PX;
        const clipHeight = CLIP_CONTENT_HEIGHT_PX;
        return [
          {
            asset: {
              ...asset,
              kind: "video",
              embeddedAudio: "present_bound",
              sourceTimeBase: asset.sourceTimeBase,
              sourceFrameCount: asset.sourceFrameCount,
              sourceSampleCount: asset.sourceSampleCount,
            },
            outputFrameRate: frameRate,
            clip,
            bounds: {
              x: laneXForBoundary(clip.startFrame, viewStart, pixelsPerFrame),
              y: clipY + clipHeight - WAVEFORM_BAND_HEIGHT_PX,
              width: Math.max(1, clip.durationFrames * pixelsPerFrame),
              height: WAVEFORM_BAND_HEIGHT_PX,
            },
            visible: { x: 0, width: laneWidth + LANE_LEAD_IN_PX },
            envelope,
          },
        ];
      }),
      // M25-62: resolved at paint time, so the light and dark palettes both apply. Empty where
      // the overlay's role tokens are not mounted; the painter then keeps its own default.
      waveformColor:
        (decorationRef.current === null
          ? ""
          : getComputedStyle(decorationRef.current)
              .getPropertyValue("--h3-nle-waveform")
              .trim()) || undefined,
      marquee,
      snapX:
        directDraft?.snapFrame === null || directDraft?.snapFrame === undefined
          ? null
          : laneXForBoundary(directDraft.snapFrame, viewStart, pixelsPerFrame),
      ghost:
        directDraft !== null &&
        directDraft.kind === "insert_from_bin" &&
        insert !== null
          ? {
              x: laneXForBoundary(
                insert.targetFrame,
                viewStart,
                pixelsPerFrame,
              ),
              y:
                Math.max(0, insertTrackIndex - firstRow) * NLE_ROW_HEIGHT_PX +
                CLIP_CONTENT_TOP_PX,
              width: Math.max(1, insert.durationFrames * pixelsPerFrame),
              height: CLIP_CONTENT_HEIGHT_PX,
              admitted: directDraft.admitted,
            }
          : directDraft === null || movingClip === undefined
            ? null
            : {
                x: laneXForBoundary(
                  movingClip.startFrame + directDraft.deltaFrames,
                  viewStart,
                  pixelsPerFrame,
                ),
                y:
                  Math.max(0, movingTrackIndex - firstRow) * NLE_ROW_HEIGHT_PX +
                  CLIP_CONTENT_TOP_PX,
                width: Math.max(1, movingClip.durationFrames * pixelsPerFrame),
                height: CLIP_CONTENT_HEIGHT_PX,
                admitted: directDraft.admitted,
              },
    };
  }, [
    directGesture,
    filmstrips,
    firstRow,
    frameRate,
    laneWidth,
    marquee,
    pixelsPerFrame,
    ruler,
    selected,
    snapshot.clips,
    snapshot.assets,
    tracks,
    viewStart,
    visibleCandidates,
    waveforms,
  ]);
  useLayoutEffect(() => {
    const canvas = decorationRef.current;
    if (canvas === null) return;
    const scheduler = createTimelineRasterScheduler(canvas, decorationScene);
    rasterRef.current = scheduler;
    scheduler.request();
    return () => {
      // IMPORTANT: bitmap close does not erase copied pixels. Reset the backing store before
      // paint on scene/authority changes, so a revoked filmstrip never survives until the next RAF.
      scheduler.dispose();
      if (rasterRef.current === scheduler) rasterRef.current = null;
    };
  }, [decorationScene]);

  const timelineHistory =
    "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
  const toolContext = () => ({
    snapshot,
    selection,
    authoringStatus: authoring.status,
    // CRITICAL: the latest requested ruler frame is the user's edit position even before the
    // monitor presents it. Falling back to the rendered state makes rapid seek-then-split edit
    // the previous frame and violates the request-generation transport authority.
    playheadFrame: toolPlayheadFrame({
      requestedFrame: rulerRequestedFrameRef.current,
      monitorFrame: playheadFrameRef.current,
      transportAvailable: navigationAvailableRef.current,
    }),
    rippleEnabled,
    undoCursor:
      timelineHistoryV2 !== undefined
        ? timelineHistoryV2.undoCursor
        : (timelineHistory?.undoCursor ?? null),
    redoCursor:
      timelineHistoryV2 !== undefined
        ? timelineHistoryV2.redoCursor
        : (timelineHistory?.redoCursor ?? null),
    rebaseAttempt,
  });
  const decide = (action: TimelineToolAction) =>
    decideTimelineTool(action, toolContext());
  const toolDecisions = {
    undo: decide({ kind: "undo" }),
    redo: decide({ kind: "redo" }),
    split: decide({ kind: "split" }),
    trim_start: decide({ kind: "trim_start" }),
    trim_end: decide({ kind: "trim_end" }),
    delete: decide({ kind: "delete" }),
    rebase: decide({ kind: "rebase" }),
  } as const;
  // B-M2564-05: the tools that read the playhead; the others change only with rendered inputs.
  const playheadToolKey = (
    split: TimelineToolDecision,
    trimStart: TimelineToolDecision,
    trimEnd: TimelineToolDecision,
  ) => `${split.enabled}:${trimStart.enabled}:${trimEnd.enabled}`;
  renderedToolKeyRef.current = playheadToolKey(
    toolDecisions.split,
    toolDecisions.trim_start,
    toolDecisions.trim_end,
  );
  currentToolKeyRef.current = () =>
    playheadToolKey(
      decide({ kind: "split" }),
      decide({ kind: "trim_start" }),
      decide({ kind: "trim_end" }),
    );
  const runTool = (action: TimelineToolAction) => {
    const decision = decide(action);
    if (!decision.enabled) return;
    cancelPointer("toolbar_action");
    cancelDirect("toolbar_action");
    void onIntent({
      action: "apply_timeline_commands",
      capturedTimeline: {
        workspaceHandle: timelineAuthority.workspaceHandle,
        workspaceRevision: timelineAuthority.workspaceRevision,
        timelineRevision: timelineAuthority.timelineRevision,
        timelineFingerprint: timelineAuthority.timelineFingerprint,
        ...capturedAuthoringIdentity,
      },
      commands: decision.commands,
    });
  };
  shortcutHandlerRef.current = (event) => {
    const resolved = resolveTimelineShortcut({
      key: event.key,
      ctrlKey: event.ctrlKey,
      metaKey: event.metaKey,
      shiftKey: event.shiftKey,
      altKey: event.altKey,
      repeat: event.repeat,
      isComposing: event.nativeEvent.isComposing,
      defaultPrevented: event.defaultPrevented,
      target: event.target,
    });
    if (resolved === null) return;
    event.preventDefault();
    event.stopPropagation();
    // CRITICAL: contain shortcuts before checking live draft refs. Keyboard move,
    // trim and roll own their commit/cancel lifecycle; selection or zoom would corrupt it.
    if (
      gestureRef.current.phase === "keyboard_draft" ||
      directGestureRef.current.phase === "keyboard_draft" ||
      rollDraftRef.current?.input === "keyboard"
    )
      return;
    if (!resolved.invoke) return;
    switch (resolved.action) {
      case "split":
      case "trim_start":
      case "trim_end":
      case "delete":
      case "undo":
      case "redo":
        runTool({ kind: resolved.action });
        return;
      case "ripple_delete":
        runTool({ kind: "delete", forceRipple: true });
        return;
      case "select_all":
        replaceSelection(
          orderedNavigationClips.slice(0, 128).map((clip) => clip.clipId),
        );
        return;
      case "zoom_in":
        zoomBy(2);
        return;
      case "zoom_out":
        zoomBy(0.5);
        return;
      case "zoom_fit":
        fit();
        return;
      case "toggle_play":
        if (transportAvailableRef.current) onTogglePlay?.();
        return;
      case "step_back":
        onStep?.(-1);
        return;
      case "step_forward":
        onStep?.(1);
    }
  };
  useEffect(() => {
    if (bindShortcuts === undefined) return;
    return bindShortcuts((event) => shortcutHandlerRef.current(event));
  }, [bindShortcuts]);

  return (
    <section
      className="h3-nle-timeline"
      aria-label={text.timeline.title}
      data-h3-nle-region="timeline"
      data-h3-nle-pixels-per-frame={pixelsPerFrame}
      data-h3-nle-view-start={viewStart}
      data-h3-nle-lane-origin-px={headerWidth + LANE_LEAD_IN_PX}
      style={
        {
          "--h3-nle-track-header-width": `${headerWidth}px`,
        } as CSSProperties
      }
    >
      <NleTimelineToolbar
        locale={locale}
        decisions={toolDecisions}
        rippleEnabled={rippleEnabled}
        snapEnabled={snapEnabled}
        zoom={{
          position: zoomPosition,
          canOut: pixelsPerFrame > scaleBounds.min,
          canIn: pixelsPerFrame < MAX_TIMELINE_SCALE,
          status: fill(text.timeline.zoomStatus, { zoom: pixelsPerFrame }),
        }}
        onTool={(tool) => runTool({ kind: tool })}
        onToggleRipple={() => {
          cancelPointer("ripple_changed");
          cancelDirect("ripple_changed");
          setRippleEnabled((value) => !value);
        }}
        onToggleSnap={() => {
          cancelPointer("snap_changed");
          setSnapEnabled((value) => !value);
        }}
        onZoomOut={() => zoomBy(0.5)}
        onZoomIn={() => zoomBy(2)}
        onZoomFit={fit}
        onZoomPosition={setZoomPosition}
        showConflict={authoring.status === "conflict"}
        overflow={
          <div className="h3-nle-toolbar-alternatives">
            <button
              type="button"
              data-h3-nle-control={
                rollCuts.length === 0 ? "boundary.roll" : undefined
              }
              data-h3-nle-alternative={
                rollCuts.length === 0 ? undefined : "roll.open"
              }
              disabled={rollCuts.length === 0}
              title={
                rollCuts.length === 0
                  ? text.timeline.toolbar.unavailable.cut_unavailable
                  : undefined
              }
              onClick={() => {
                const cut = rollCuts[0];
                if (cut === undefined) return;
                setRollDraft({
                  cut,
                  deltaFrames: 0,
                  input: "click",
                  pointerId: null,
                  originClientX: 0,
                });
              }}
            >
              {text.inspector.operations.roll_edit}
            </button>
            <button
              type="button"
              data-h3-nle-control="transport.zoom_selection"
              disabled={selection.length === 0}
              onClick={zoomSelection}
            >
              {text.timeline.zoomSelection ?? "Zoom to selection"}
            </button>
            <button
              type="button"
              data-h3-nle-control="selection.previous"
              onClick={() => selectAdjacent(-1)}
            >
              {text.timeline.previousClip ?? "Previous clip"}
            </button>
            <button
              type="button"
              data-h3-nle-control="selection.next"
              onClick={() => selectAdjacent(1)}
            >
              {text.timeline.nextClip ?? "Next clip"}
            </button>
            <button
              type="button"
              aria-pressed={clickSelectionMode === "toggle"}
              data-h3-nle-alternative="selection.additive"
              onClick={() =>
                setClickSelectionMode((mode) =>
                  mode === "toggle" ? "replace" : "toggle",
                )
              }
            >
              {text.timeline.additiveSelection}
            </button>
            <button
              type="button"
              aria-pressed={clickSelectionMode === "range"}
              data-h3-nle-alternative="selection.range"
              onClick={() =>
                setClickSelectionMode((mode) =>
                  mode === "range" ? "replace" : "range",
                )
              }
            >
              {text.timeline.rangeSelection}
            </button>
            <button
              type="button"
              data-h3-nle-alternative="selection.clear"
              disabled={selection.length === 0}
              onClick={() => replaceSelection([])}
            >
              {text.timeline.clearSelection}
            </button>
            <button
              type="button"
              data-h3-nle-alternative="selection.all"
              disabled={selection.length === orderedNavigationClips.length}
              onClick={() =>
                replaceSelection(
                  orderedNavigationClips
                    .slice(0, 128)
                    .map((clip) => clip.clipId),
                )
              }
            >
              {text.timeline.selectAll}
            </button>
            <button
              type="button"
              data-h3-nle-alternative="move.open"
              disabled={
                focusedClipId === null || directGesture.phase === "submitting"
              }
              onClick={() => {
                const clipId = focusedClipId ?? selection[0];
                if (clipId !== undefined)
                  setClickMove({ clipId, deltaFrames: 0, rowDelta: 0 });
              }}
            >
              {text.timeline.move ?? "Move…"}
            </button>
            {clickMove !== null ? (
              <div
                className="h3-nle-move-alternative"
                role="group"
                aria-label={text.timeline.move}
              >
                <button
                  type="button"
                  aria-label={text.timeline.moveEarlier}
                  onClick={() =>
                    setClickMove((current) =>
                      current === null
                        ? null
                        : {
                            ...current,
                            deltaFrames:
                              current.deltaFrames - Math.max(1, gridFrames),
                          },
                    )
                  }
                >
                  −
                </button>
                <output>{clickMove.deltaFrames}f</output>
                <button
                  type="button"
                  aria-label={text.timeline.moveLater}
                  onClick={() =>
                    setClickMove((current) =>
                      current === null
                        ? null
                        : {
                            ...current,
                            deltaFrames:
                              current.deltaFrames + Math.max(1, gridFrames),
                          },
                    )
                  }
                >
                  +
                </button>
                <button
                  type="button"
                  aria-label={text.timeline.moveTrackUp}
                  onClick={() =>
                    setClickMove((current) =>
                      current === null
                        ? null
                        : { ...current, rowDelta: current.rowDelta - 1 },
                    )
                  }
                >
                  ↑
                </button>
                <button
                  type="button"
                  aria-label={text.timeline.moveTrackDown}
                  onClick={() =>
                    setClickMove((current) =>
                      current === null
                        ? null
                        : { ...current, rowDelta: current.rowDelta + 1 },
                    )
                  }
                >
                  ↓
                </button>
                <button
                  type="button"
                  data-h3-nle-control={
                    selection.length > 1 ? "clip.move_group" : "clip.move"
                  }
                  aria-label={
                    selection.length > 1
                      ? text.inspector.operations.move_group
                      : text.inspector.operations.move_clip
                  }
                  onClick={applyClickMove}
                >
                  {text.timeline.apply}
                </button>
                <button type="button" onClick={() => setClickMove(null)}>
                  {text.timeline.cancel}
                </button>
              </div>
            ) : null}
            <button
              ref={previousFrameRef}
              type="button"
              aria-label={text.timeline.seekPreviousFrame}
              data-h3-nle-alternative="seek.previous_frame"
              disabled={!navigationAvailableRef.current}
              onClick={() => {
                if (!navigationAvailableRef.current) return;
                onBeginScrub?.();
                onSeek?.(Math.max(0, (playheadFrameRef.current ?? 0) - 1));
              }}
            >
              ◀
            </button>
            <button
              ref={nextFrameRef}
              type="button"
              aria-label={text.timeline.seekNextFrame}
              data-h3-nle-alternative="seek.next_frame"
              disabled={!navigationAvailableRef.current}
              onClick={() => {
                if (!navigationAvailableRef.current) return;
                onBeginScrub?.();
                onSeek?.(
                  Math.min(
                    transportLastFrame,
                    (playheadFrameRef.current ?? 0) + 1,
                  ),
                );
              }}
            >
              ▶
            </button>
            <button
              type="button"
              aria-label={text.timeline.panEarlier}
              data-h3-nle-alternative="pan.earlier"
              disabled={viewStart <= 0}
              onClick={() => {
                setTimelineView((current) =>
                  Object.freeze({
                    ...current,
                    fitLocked: false,
                    viewStart: Math.max(
                      0,
                      current.viewStart - Math.max(1, gridFrames),
                    ),
                  }),
                );
              }}
            >
              ⇤
            </button>
            <button
              type="button"
              aria-label={text.timeline.panLater}
              data-h3-nle-alternative="pan.later"
              disabled={viewStart >= maxViewStart}
              onClick={() => {
                setTimelineView((current) =>
                  Object.freeze({
                    ...current,
                    fitLocked: false,
                    viewStart: Math.min(
                      maxViewStart,
                      current.viewStart + Math.max(1, gridFrames),
                    ),
                  }),
                );
              }}
            >
              ⇥
            </button>
            <input
              type="range"
              aria-label={text.timeline.scroll}
              data-h3-nle-control="transport.scroll"
              min={0}
              max={maxViewStart}
              step={Math.max(1, gridFrames)}
              value={Math.min(viewStart, maxViewStart)}
              onChange={(event) => {
                const nextStart = Number(event.currentTarget.value);
                setTimelineView((current) =>
                  Object.freeze({
                    ...current,
                    fitLocked: false,
                    viewStart: nextStart,
                  }),
                );
              }}
              onKeyDown={(event) =>
                ownRangeKeyDown(
                  event,
                  {
                    value: Math.min(viewStart, maxViewStart),
                    min: 0,
                    max: maxViewStart,
                    step: Math.max(1, gridFrames),
                  },
                  (nextStart) =>
                    setTimelineView((current) =>
                      Object.freeze({
                        ...current,
                        fitLocked: false,
                        viewStart: nextStart,
                      }),
                    ),
                )
              }
            />
            <output aria-live="off">
              {fill(text.timeline.virtualized, {
                first: totalRows === 0 ? 0 : firstRow + 1,
                last: lastRow,
                total: totalRows,
              })}
            </output>
          </div>
        }
      />
      {tracks.length === 0 ? (
        <button
          type="button"
          className="h3-nle-menu-launcher"
          data-h3-nle-menu-trigger="track"
          aria-haspopup="menu"
          aria-expanded={trackMenuTarget !== null}
          aria-label={menuLabels.openTrack}
          onClick={(event) => openTrackMenu(event, null)}
        >
          ⋯
        </button>
      ) : null}
      {/* M25-62: the ruler, the tracks and the scroll bar are one surface with no gap, so the
          playhead layer can span from the ruler top to the lane bottom outside the scroller. */}
      <div className="h3-nle-timeline-surface">
        <div
          ref={rulerRef}
          className="h3-nle-ruler"
          role="slider"
          tabIndex={navigationAvailableRef.current ? 0 : -1}
          aria-disabled={!navigationAvailableRef.current}
          aria-label={text.timeline.playhead}
          aria-valuemin={0}
          aria-valuemax={transportLastFrame}
          aria-valuenow={rulerFrame}
          aria-valuetext={formatTimelineTimecode(rulerFrame, fps)}
          data-h3-nle-control="transport.seek"
          data-h3-nle-request-clear-reason={rulerClearReasonRef.current}
          data-h3-nle-request-generation={
            rulerAcknowledgedGenerationRef.current ?? "unacknowledged"
          }
          onPointerDown={(event) => {
            if (!navigationAvailableRef.current) return;
            if (event.button !== 0 || event.isPrimary === false) return;
            event.currentTarget.setPointerCapture(event.pointerId);
            beginRulerScrub(event.pointerId);
            rulerSeek(event.clientX, event.currentTarget);
          }}
          onPointerMove={(event) => {
            if (!navigationAvailableRef.current) return;
            if (event.currentTarget.hasPointerCapture(event.pointerId))
              rulerSeek(event.clientX, event.currentTarget);
          }}
          onPointerUp={(event) => {
            if (!navigationAvailableRef.current) return;
            if (scrubRef.current?.pointerId !== event.pointerId) return;
            flushRulerSeek();
            scrubRef.current = null;
            if (event.currentTarget.hasPointerCapture(event.pointerId))
              event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onPointerCancel={(event) => {
            if (!navigationAvailableRef.current) return;
            if (scrubRef.current?.pointerId !== event.pointerId) return;
            cancelScrub();
            if (event.currentTarget.hasPointerCapture(event.pointerId))
              event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onLostPointerCapture={() => {
            if (!navigationAvailableRef.current) return;
            if (scrubRef.current !== null) cancelScrub();
          }}
          onKeyDown={(event) => {
            if (!navigationAvailableRef.current) return;
            if (event.key === "Escape" && scrubRef.current !== null) {
              event.preventDefault();
              event.stopPropagation();
              cancelScrub();
              return;
            }
            if (event.altKey || event.ctrlKey || event.metaKey) return;
            const current =
              rulerRequestedFrameRef.current ?? playheadFrameRef.current ?? 0;
            const delta = event.shiftKey ? Math.max(1, gridFrames) : 1;
            const pageDelta = Math.max(1, Math.round(fps));
            const target =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? transportLastFrame
                  : event.key === "PageDown"
                    ? current - pageDelta
                    : event.key === "PageUp"
                      ? current + pageDelta
                      : event.key === "ArrowLeft"
                        ? current - delta
                        : event.key === "ArrowRight"
                          ? current + delta
                          : null;
            if (target === null) return;
            event.preventDefault();
            event.stopPropagation();
            onBeginScrub?.();
            queueRulerSeek(target);
          }}
        >
          <span className="h3-nle-ruler-header" aria-hidden="true" />
          <span className="h3-nle-ruler-lane" aria-hidden="true">
            <svg
              className="h3-nle-ruler-minor"
              data-h3-nle-ruler-minor=""
              viewBox={`0 0 ${laneWidth + LANE_LEAD_IN_PX} 1`}
              preserveAspectRatio="none"
              style={{ width: `${laneWidth + LANE_LEAD_IN_PX}px` }}
            >
              <path d={rulerMinorPath} />
            </svg>
            {ruler.map((mark) => (
              <span
                key={mark.frame}
                className="h3-nle-ruler-mark"
                data-h3-nle-ruler-frame={mark.frame}
                style={{
                  left: `${laneXForBoundary(mark.frame, viewStart, pixelsPerFrame)}px`,
                }}
              >
                {formatRulerLabel(mark.frame, fps)}
              </span>
            ))}
          </span>
        </div>
        <div
          ref={scrollRef}
          className="h3-nle-tracks"
          role="grid"
          // The owned focus target when a virtualized removal takes focus from a grip; -1 keeps
          // it out of the Tab ring the overlay computes from `FOCUSABLE`.
          tabIndex={-1}
          data-h3-nle-focus-fallback="timeline"
          aria-label={text.timeline.grid}
          aria-rowcount={totalRows}
          data-h3-nle-virtual-rows={visibleTracks.length}
          data-h3-nle-mounted-clips={mountedClipIds.size}
          onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
          onKeyDown={(event) => {
            // IMPORTANT: standalone grid select-all must use the same repeat/draft
            // guard as the workspace; a second selection path bypasses both.
            if (
              (event.ctrlKey || event.metaKey) &&
              event.key.toLowerCase() === "a"
            )
              shortcutHandlerRef.current(event);
          }}
          onPointerDown={(event) => {
            const target = event.target as HTMLElement;
            if (
              event.button !== 0 ||
              target.closest("button,[data-h3-nle-clip]")
            )
              return;
            const rect = event.currentTarget.getBoundingClientRect();
            // Lane coordinates (the decoration canvas space), not frame-origin coordinates.
            const x = laneXFromClientX(
              event.clientX,
              rect.left,
              headerWidthRef.current,
            );
            const y = event.clientY - rect.top + event.currentTarget.scrollTop;
            event.currentTarget.setPointerCapture(event.pointerId);
            setMarquee({
              pointerId: event.pointerId,
              originX: x,
              originY: y,
              x,
              y,
              width: 0,
              height: 0,
              additive: event.ctrlKey || event.metaKey,
            });
          }}
          onPointerMove={(event) => {
            if (marquee === null || marquee.pointerId !== event.pointerId)
              return;
            const rect = event.currentTarget.getBoundingClientRect();
            // Lane coordinates (the decoration canvas space), not frame-origin coordinates.
            const x = laneXFromClientX(
              event.clientX,
              rect.left,
              headerWidthRef.current,
            );
            const y = event.clientY - rect.top + event.currentTarget.scrollTop;
            setMarquee({
              ...marquee,
              x: Math.min(marquee.originX, x),
              y: Math.min(marquee.originY, y),
              width: Math.abs(x - marquee.originX),
              height: Math.abs(y - marquee.originY),
            });
          }}
          onPointerUp={(event) => {
            if (marquee === null || marquee.pointerId !== event.pointerId)
              return;
            if (event.currentTarget.hasPointerCapture(event.pointerId))
              event.currentTarget.releasePointerCapture(event.pointerId);
            const intersected = snapshot.clips
              .filter((clip) => {
                const row = tracks.findIndex(
                  (track) => track.trackId === clip.trackId,
                );
                const x = laneXForBoundary(
                  clip.startFrame,
                  viewStart,
                  pixelsPerFrame,
                );
                const y = row * NLE_ROW_HEIGHT_PX;
                const width = Math.max(1, clip.durationFrames * pixelsPerFrame);
                return (
                  x + width >= marquee.x &&
                  x <= marquee.x + marquee.width &&
                  y + NLE_ROW_HEIGHT_PX >= marquee.y &&
                  y <= marquee.y + marquee.height
                );
              })
              .map((clip) => clip.clipId)
              .slice(0, 128);
            const isClick =
              Math.hypot(marquee.width, marquee.height) < DRAG_THRESHOLD_PX;
            const ids = isClick
              ? marquee.additive
                ? [...selection]
                : []
              : marquee.additive
                ? [...new Set([...selection, ...intersected])].slice(0, 128)
                : intersected;
            setMarquee(null);
            if (
              ids.length !== selection.length ||
              ids.some((id, index) => id !== selection[index])
            )
              void onIntent({
                action: "apply_timeline_commands",
                commands: [build.selectClips(ids)],
              });
          }}
          onPointerCancel={(event) => {
            if (marquee?.pointerId !== event.pointerId) return;
            if (event.currentTarget.hasPointerCapture(event.pointerId))
              event.currentTarget.releasePointerCapture(event.pointerId);
            setMarquee(null);
          }}
        >
          <div
            style={{
              position: "relative",
              height: `${Math.max(1, totalRows) * NLE_ROW_HEIGHT_PX}px`,
            }}
          >
            {totalRows === 0 ? (
              <p className="h3-nle-note">{text.timeline.empty}</p>
            ) : null}
            {visibleTracks.map((track, offset) => (
              <TrackRow
                key={track.trackId}
                locale={locale}
                fps={fps}
                track={track}
                trackName={trackNames.get(track.trackId) ?? ""}
                rowIndex={firstRow + offset}
                clips={clipsByTrack.get(track.trackId) ?? []}
                clipName={clipName}
                viewStart={viewStart}
                pixelsPerFrame={pixelsPerFrame}
                coarse={coarse}
                emptyLane={
                  snapshot.clips.length === 0 && track.kind === "primary_video"
                }
                selected={selected}
                highlighted={highlighted}
                draft={draft}
                busy={
                  authoring.status === "pending" ||
                  gesture.phase === "submitting"
                }
                onMovePointerDown={beginMove}
                onMovePointerMove={moveDirect}
                onMovePointerUp={releaseDirect}
                onMovePointerCancel={() => cancelDirect("pointercancel")}
                onMoveLostCapture={() => cancelDirect("lostpointercapture")}
                onMoveBlur={() => {
                  // IMPORTANT: focus is keyboard-draft ownership, not pointer ownership. Browsers
                  // may blur a captured clip body after pointerdown; cancelling there kills a
                  // valid drag before its first move. Pointer cancellation has dedicated events.
                  if (directGestureRef.current.phase === "keyboard_draft")
                    cancelDirect("focus_lost");
                }}
                onClipClick={(clip, mode) =>
                  clickClip(
                    clip,
                    mode === "replace" ? clickSelectionMode : mode,
                  )
                }
                onClipDoubleClick={(frame) => {
                  if (!navigationAvailableRef.current) return;
                  onBeginScrub?.();
                  onSeek?.(frame);
                }}
                onClipKeyDown={clipKeyDown}
                onTrackCommand={(command) =>
                  void onIntent({
                    action: "apply_timeline_commands",
                    commands: [command],
                  })
                }
                onOpenTrackMenu={openTrackMenu}
                onOpenClipMenu={openClipMenu}
                onOpenClipMenuFromKey={openClipMenuFromKey}
                onGripPointerDown={beginPointer}
                onGripPointerMove={movePointer}
                onGripPointerUp={releasePointer}
                onGripPointerCancel={() => cancelPointer("pointercancel")}
                onGripLostCapture={() => cancelPointer("lostpointercapture")}
                onGripBlur={() => {
                  if (gestureRef.current.phase === "keyboard_draft")
                    cancelPointer("focus_lost");
                }}
                onGripKeyDown={gripKeyDown}
              />
            ))}
            {rollCuts.map((cut) => {
              const left = snapshot.clips.find(
                (clip) => clip.clipId === cut.leftClipId,
              );
              const right = snapshot.clips.find(
                (clip) => clip.clipId === cut.rightClipId,
              );
              const other =
                selectedClip?.clipId === left?.clipId ? right : left;
              // IMPORTANT: a centred roll target can cover the entire unselected short clip,
              // making its pointer selection impossible. Keep roll's toolbar/keyboard
              // alternatives while that neighbour is narrow; restore the handle on selection.
              const rollTarget = coarse
                ? ROLL_TARGET_PX.coarse
                : ROLL_TARGET_PX.fine;
              const blocksNeighbourSelection =
                selectedClip !== undefined &&
                selectedClip.durationFrames * pixelsPerFrame >= rollTarget &&
                other !== undefined &&
                other.durationFrames * pixelsPerFrame < rollTarget;
              const row = tracks.findIndex(
                (track) => track.trackId === left?.trackId,
              );
              const inWindow =
                row >= firstRow &&
                row < lastRow &&
                cut.frame >= viewStart &&
                cut.frame < viewEnd &&
                !blocksNeighbourSelection;
              const active =
                rollDraft?.cut.leftClipId === cut.leftClipId &&
                rollDraft.cut.rightClipId === cut.rightClipId;
              return (
                <button
                  key={`${cut.leftClipId}\u0000${cut.rightClipId}`}
                  type="button"
                  className="h3-nle-roll-cut"
                  hidden={!inWindow}
                  data-h3-nle-control="boundary.roll"
                  data-h3-nle-roll-left={cut.leftClipId}
                  data-h3-nle-roll-right={cut.rightClipId}
                  data-drafting={active ? rollDraft.input : undefined}
                  data-admitted={
                    active && rollDraft.deltaFrames !== 0
                      ? decide({
                          kind: "roll",
                          leftClipId: cut.leftClipId,
                          rightClipId: cut.rightClipId,
                          deltaFrames: rollDraft.deltaFrames,
                        }).enabled
                        ? "true"
                        : "false"
                      : undefined
                  }
                  aria-label={text.inspector.operations.roll_edit}
                  disabled={!draftable}
                  // IMPORTANT (M25-62, A62-4): under a fine pointer the 24 px roll target sits in
                  // the lower half of the row, so the clip centres and the upper part of the
                  // 8 px grips on both sides of the cut stay their own targets. A coarse pointer
                  // keeps the centred 44 px target and the neighbour rule above.
                  // IMPORTANT (B-M2562-07): these buttons are positioned in the rows container,
                  // whose origin is the grid's left edge, not a lane's. Their x is the timeline x
                  // (header + lead-in + frame) with the pointer-dependent header width; the lane x
                  // drew every target one header width left of its cut.
                  style={{
                    left: `${timelineXForBoundary(cut.frame, viewStart, pixelsPerFrame, headerWidth)}px`,
                    top: `${row * NLE_ROW_HEIGHT_PX + (coarse ? 6 : NLE_ROW_HEIGHT_PX - rollTarget - 2)}px`,
                  }}
                  onPointerDown={(event) => beginRollPointer(event, cut)}
                  onPointerMove={moveRollPointer}
                  onPointerUp={releaseRollPointer}
                  onPointerCancel={cancelRoll}
                  onLostPointerCapture={() => {
                    if (rollDraftRef.current?.input === "pointer") cancelRoll();
                  }}
                  onKeyDown={(event) => rollKeyDown(event, cut)}
                  onBlur={() => {
                    if (rollDraftRef.current?.input === "keyboard")
                      cancelRoll();
                  }}
                  onClick={() => {
                    if (suppressRollClickRef.current) {
                      suppressRollClickRef.current = false;
                      return;
                    }
                    setRollDraft({
                      cut,
                      deltaFrames: 0,
                      input: "click",
                      pointerId: null,
                      originClientX: 0,
                    });
                  }}
                >
                  ↔
                  {active && rollDraft.deltaFrames !== 0
                    ? ` ${rollDraft.deltaFrames}f`
                    : ""}
                </button>
              );
            })}
            <canvas
              ref={decorationRef}
              className="h3-nle-decoration"
              data-h3-nle-canvas="timeline_decoration"
              data-h3-nle-ghost={
                directGesture.phase === "dragging" ||
                directGesture.phase === "keyboard_draft"
                  ? directGesture.draft.admitted
                    ? "admitted"
                    : "refused"
                  : undefined
              }
              data-h3-nle-snap-line={
                (directGesture.phase === "dragging" ||
                  directGesture.phase === "keyboard_draft") &&
                directGesture.draft.snapFrame !== null
                  ? "present"
                  : "absent"
              }
              aria-hidden="true"
            />
          </div>
        </div>
        <div
          className="h3-nle-scroll-bar"
          style={{ width: `${scrollBarInput.trackWidth}px` }}
          data-h3-nle-control="transport.scroll_bar"
          // `transport.scroll` in More stays the accessible, full-size equivalent.
          aria-hidden="true"
          hidden={!scrollBar.visible}
          onPointerDown={(event) => {
            if (event.button !== 0 || event.isPrimary === false) return;
            const rect = event.currentTarget.getBoundingClientRect();
            const x = event.clientX - rect.left;
            const onThumb =
              x >= scrollBar.thumbX &&
              x <= scrollBar.thumbX + scrollBar.thumbWidth;
            // A press on the track centres the thumb there, then drags from that grab point.
            const grab = onThumb
              ? x - scrollBar.thumbX
              : scrollBar.thumbWidth / 2;
            event.preventDefault();
            event.currentTarget.setPointerCapture(event.pointerId);
            scrollDragRef.current = { pointerId: event.pointerId, grab };
            scrollToThumb(x - grab);
          }}
          onPointerMove={(event) => {
            const drag = scrollDragRef.current;
            if (drag?.pointerId !== event.pointerId) return;
            const rect = event.currentTarget.getBoundingClientRect();
            scrollToThumb(event.clientX - rect.left - drag.grab);
          }}
          onPointerUp={(event) => {
            if (scrollDragRef.current?.pointerId !== event.pointerId) return;
            scrollDragRef.current = null;
            if (event.currentTarget.hasPointerCapture(event.pointerId))
              event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onPointerCancel={() => {
            scrollDragRef.current = null;
          }}
          onLostPointerCapture={() => {
            scrollDragRef.current = null;
          }}
        >
          <span
            className="h3-nle-scroll-thumb"
            style={{
              left: `${scrollBar.thumbX}px`,
              width: `${scrollBar.thumbWidth}px`,
            }}
          />
        </div>
        {/* IMPORTANT (M25-62, A62-3): the playhead is outside the tracks scroller, from the ruler
          top to the lane bottom, clipped to the lane columns. Inside the scroller, vertical
          scroll and row virtualization shortened it and the first clip edge hid it at frame 0. */}
        <div className="h3-nle-playhead-layer" aria-hidden="true">
          <div
            ref={playheadElementRef}
            className="h3-nle-playhead"
            hidden={
              playheadFrameRef.current === null ||
              playheadFrameRef.current < viewStart ||
              playheadFrameRef.current >= viewEnd
            }
            style={{
              left:
                playheadFrameRef.current === null
                  ? undefined
                  : `${laneXForBoundary(
                      playheadFrameRef.current,
                      viewStart,
                      pixelsPerFrame,
                    )}px`,
            }}
          >
            {/* The ruler slider stays the accessible seek control; the head is a pointer grip
              that drives the same seek path. */}
            <span
              className="h3-nle-playhead-head"
              data-h3-nle-playhead-head=""
              aria-hidden="true"
              onPointerDown={(event) => {
                const ruler = rulerRef.current;
                if (!navigationAvailableRef.current || ruler === null) return;
                if (event.button !== 0 || event.isPrimary === false) return;
                event.preventDefault();
                event.stopPropagation();
                // B-M2562-02: a press on the head is a press on the ruler. The head covers
                // +-5.5 px around the playhead, so keeping a grab offset made a ruler scrub that
                // began there lag the pointer by up to five frames at 1 px/f.
                event.currentTarget.setPointerCapture(event.pointerId);
                beginRulerScrub(event.pointerId);
                rulerSeek(event.clientX, ruler);
              }}
              onPointerMove={(event) => {
                const ruler = rulerRef.current;
                if (
                  ruler === null ||
                  scrubRef.current?.pointerId !== event.pointerId
                )
                  return;
                rulerSeek(event.clientX, ruler);
              }}
              onPointerUp={(event) => {
                if (scrubRef.current?.pointerId !== event.pointerId) return;
                flushRulerSeek();
                scrubRef.current = null;
                if (event.currentTarget.hasPointerCapture(event.pointerId))
                  event.currentTarget.releasePointerCapture(event.pointerId);
              }}
              onPointerCancel={(event) => {
                if (scrubRef.current?.pointerId !== event.pointerId) return;
                cancelScrub();
                if (event.currentTarget.hasPointerCapture(event.pointerId))
                  event.currentTarget.releasePointerCapture(event.pointerId);
              }}
              onLostPointerCapture={() => {
                if (scrubRef.current !== null) cancelScrub();
              }}
            >
              <svg viewBox="0 0 11 14" width={11} height={14}>
                <path d="M0.5 0.5h10v8l-5 5-5-5z" />
              </svg>
            </span>
          </div>
        </div>
        {/* The gesture outcomes stay live regions with their attributes (R6); they float over the
          lane bottom instead of taking a footer row the canvas does not have. */}
        <div className="h3-nle-timeline-feedback">
          <output
            role="status"
            aria-live="polite"
            data-h3-nle-status="trim"
            data-h3-nle-trim-phase={gesture.phase}
            data-code={
              gesture.phase === "rejected" ? gesture.reason : undefined
            }
          >
            {gestureStatus}
          </output>
          <output
            role="status"
            aria-live="polite"
            data-h3-nle-status="move"
            data-h3-nle-move-phase={directGesture.phase}
            data-code={
              directGesture.phase === "rejected"
                ? directGesture.reason
                : directGesture.phase === "dragging" ||
                    directGesture.phase === "keyboard_draft"
                  ? (directGesture.draft.reason ?? undefined)
                  : undefined
            }
          >
            {directStatus}
          </output>
        </div>
      </div>
      <p className="h3-nle-vh" id="h3-nle-trim-instructions">
        {text.timeline.trimInstructions}
      </p>
      <NleTrackMenu
        locale={locale}
        snapshot={snapshot}
        target={trackMenuTarget}
        busy={authoring.status === "pending" || authoring.status === "loading"}
        onIntent={onIntent}
        onClose={() => setTrackMenuTarget(null)}
      />
      <NleClipMenu
        locale={locale}
        snapshot={snapshot}
        target={clipMenuTarget}
        busy={authoring.status === "pending" || authoring.status === "loading"}
        rippleEnabled={rippleEnabled}
        onIntent={onIntent}
        onClose={() => setClipMenuTarget(null)}
      />
      {railClip !== undefined ? (
        <div
          className="h3-nle-trim-rail"
          data-h3-nle-trim-clip={railClip.clipId}
          aria-label={fill(text.timeline.clip, {
            name: clipName(railClip),
            start: formatTimelineTimecode(railClip.startFrame, fps),
            end: formatTimelineTimecode(clipEnd(railClip), fps),
            duration: formatTimelineTimecode(railClip.durationFrames, fps),
          })}
        >
          {(["start", "end"] as const).map((edge) => (
            <button
              key={edge}
              type="button"
              className="h3-nle-rail-grip"
              data-h3-nle-trim-edge={edge}
              data-h3-nle-control="clip.trim"
              aria-label={fill(
                edge === "start"
                  ? text.timeline.trimStart
                  : text.timeline.trimEnd,
                { name: clipName(railClip) },
              )}
              aria-describedby="h3-nle-trim-instructions"
              title={text.timeline.trimInstructions}
              disabled={
                !draftable ||
                gesture.phase === "submitting" ||
                snapshot.tracks.find(
                  (track) => track.trackId === railClip.trackId,
                )?.locked !== false
              }
              onPointerDown={(event) => beginPointer(event, railClip, edge)}
              onPointerMove={(event) => movePointer(event, railClip)}
              onPointerUp={releasePointer}
              onPointerCancel={() => cancelPointer("pointercancel")}
              onLostPointerCapture={() => cancelPointer("lostpointercapture")}
              // IMPORTANT: focus loss cancels only an uncommitted draft (the reducer's
              // `cancel` ignores submitting/settled phases). Both grip layouts — this rail and
              // the inline TrackRow grips — must bind it: a draft that survives Tab keeps the
              // overlay's edge-first Escape guard armed on a grip that no longer has focus.
              onBlur={() => cancelPointer("focus_lost")}
              onKeyDown={(event) => gripKeyDown(event, railClip, edge)}
            >
              {/* The label beside the grips carries the live start-end timecodes. */}
              <NleActionIcon
                name={edge === "start" ? "trimStart" : "trimEnd"}
                size={16}
              />
            </button>
          ))}
          <span className="h3-nle-rail-label">
            {fill(text.timeline.railLabel, {
              name: clipName(railClip),
              start: formatTimelineTimecode(
                draft?.geometry.start ?? railClip.startFrame,
                fps,
              ),
              end: formatTimelineTimecode(
                draft?.geometry.end ?? clipEnd(railClip),
                fps,
              ),
            })}
          </span>
          <button
            type="button"
            className="h3-nle-rail-menu"
            data-h3-nle-menu-trigger="clip"
            aria-haspopup="menu"
            aria-label={timelineMenuLabels(locale).openClip}
            onClick={(event) => openClipMenu(event, railClip.clipId)}
          >
            <NleActionIcon name="more" size={16} />
          </button>
        </div>
      ) : null}
      {rollDraft?.input === "click" ? (
        <div
          className="h3-nle-roll-alternative"
          role="group"
          aria-label={text.inspector.operations.roll_edit}
        >
          <button
            type="button"
            aria-label={`${text.inspector.operations.roll_edit} -1`}
            onClick={() =>
              setRollDraft((current) =>
                current === null
                  ? null
                  : { ...current, deltaFrames: current.deltaFrames - 1 },
              )
            }
          >
            −
          </button>
          <output>{rollDraft.deltaFrames}f</output>
          <button
            type="button"
            aria-label={`${text.inspector.operations.roll_edit} +1`}
            onClick={() =>
              setRollDraft((current) =>
                current === null
                  ? null
                  : { ...current, deltaFrames: current.deltaFrames + 1 },
              )
            }
          >
            +
          </button>
          <button
            type="button"
            onClick={() => commitRoll(rollDraft.cut, rollDraft.deltaFrames)}
          >
            {text.timeline.apply}
          </button>
          <button type="button" onClick={cancelRoll}>
            {text.timeline.cancel}
          </button>
        </div>
      ) : null}
    </section>
  );
}

function TrackRow({
  locale,
  fps,
  track,
  trackName,
  rowIndex,
  clips,
  clipName,
  viewStart,
  pixelsPerFrame,
  coarse,
  emptyLane,
  selected,
  highlighted,
  draft,
  busy,
  onMovePointerDown,
  onMovePointerMove,
  onMovePointerUp,
  onMovePointerCancel,
  onMoveLostCapture,
  onMoveBlur,
  onClipClick,
  onClipDoubleClick,
  onClipKeyDown,
  onTrackCommand,
  onOpenTrackMenu,
  onOpenClipMenu,
  onOpenClipMenuFromKey,
  onGripPointerDown,
  onGripPointerMove,
  onGripPointerUp,
  onGripPointerCancel,
  onGripLostCapture,
  onGripBlur,
  onGripKeyDown,
}: {
  locale: Locale;
  fps: number;
  track: CompositionTrack;
  trackName: string;
  rowIndex: number;
  clips: readonly CompositionClip[];
  clipName(clip: CompositionClip): string;
  viewStart: number;
  pixelsPerFrame: number;
  coarse: boolean;
  emptyLane: boolean;
  selected: ReadonlySet<string>;
  highlighted: ReadonlySet<string>;
  draft:
    | Extract<
        TrimGestureState,
        { phase: "dragging" | "keyboard_draft" }
      >["draft"]
    | null;
  busy: boolean;
  onMovePointerDown(
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ): void;
  onMovePointerMove(event: ReactPointerEvent<HTMLButtonElement>): void;
  onMovePointerUp(event: ReactPointerEvent<HTMLButtonElement>): void;
  onMovePointerCancel(): void;
  onMoveLostCapture(): void;
  onMoveBlur(): void;
  onClipClick(
    clip: CompositionClip,
    mode: "replace" | "toggle" | "range",
  ): void;
  onClipDoubleClick(frame: number): void;
  onClipKeyDown(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ): void;
  onTrackCommand(command: ReturnType<typeof build.setTrackLocked>): void;
  onOpenTrackMenu(
    event: ReactMouseEvent<HTMLElement>,
    trackId: string | null,
  ): void;
  onOpenClipMenu(event: ReactMouseEvent<HTMLElement>, clipId: string): void;
  onOpenClipMenuFromKey(element: HTMLElement, clipId: string): void;
  onGripPointerDown(
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
    edge: TrimEdge,
  ): void;
  onGripPointerMove(
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ): void;
  onGripPointerUp(event: ReactPointerEvent<HTMLButtonElement>): void;
  onGripPointerCancel(): void;
  onGripLostCapture(): void;
  onGripBlur(): void;
  onGripKeyDown(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    clip: CompositionClip,
    edge: TrimEdge,
  ): void;
}) {
  const text = nleCopy(locale);
  return (
    <div
      className="h3-nle-track-row"
      role="row"
      aria-rowindex={rowIndex + 1}
      data-h3-nle-track={track.trackId}
      data-kind={track.kind}
      style={{ top: `${rowIndex * NLE_ROW_HEIGHT_PX}px` }}
    >
      <NleTrackHeader
        locale={locale}
        track={track}
        name={trackName}
        onTrackCommand={onTrackCommand}
        onOpenTrackMenu={onOpenTrackMenu}
      />
      <div className="h3-nle-track-lane" role="gridcell">
        {emptyLane ? (
          // M25-62 (A62-7): presentation over the grid's existing bin-insert target, which owns
          // the drop; the zone takes no pointer events, so it never becomes a second target.
          <p className="h3-nle-empty-lane" data-h3-nle-empty-lane="">
            {text.timeline.emptyLane}
          </p>
        ) : null}
        {clips.map((clip) => {
          const isDraft = draft?.identity.clipId === clip.clipId;
          const start = isDraft ? draft.geometry.start : clip.startFrame;
          const end = isDraft ? draft.geometry.end : clipEnd(clip);
          const left = laneXForBoundary(start, viewStart, pixelsPerFrame);
          // IMPORTANT: a one-frame clip can render narrower than its selection button's
          // padding and be clipped out of the hit area. Keep timing exact, but give the
          // timeline control enough visual width to remain selectable at zoom fit.
          const width = Math.max(20, (end - start) * pixelsPerFrame);
          const name = clipName(clip);
          const label = fill(text.timeline.clip, {
            name,
            start: formatTimelineTimecode(start, fps),
            end: formatTimelineTimecode(end, fps),
            duration: formatTimelineTimecode(end - start, fps),
          });
          const isSelected = selected.has(clip.clipId);
          const editable = !track.locked && isSelected;
          // IMPORTANT (M25-62, R7): one rule for every edit affordance. The fine grips sit 8 px
          // inside each edge, so a neighbour's pixels always hit the neighbour; below 24 px the
          // grip at the draft's edge stays mounted, so a draft that shrinks its own clip keeps
          // the control it is driving.
          const affordances = clipEditAffordances({
            width,
            selected: isSelected,
            coarse,
            draftEdge: isDraft ? draft.identity.edge : null,
          });
          const showStart =
            affordances.grips === "both" || affordances.grips === "start";
          const showEnd =
            affordances.grips === "both" || affordances.grips === "end";
          const inlineGrips =
            affordances.grips === "both"
              ? "true"
              : affordances.grips === "none"
                ? "false"
                : "single";
          const grip = (edge: TrimEdge) => (
            <button
              type="button"
              className="h3-nle-grip"
              hidden={edge === "start" ? !showStart : !showEnd}
              data-h3-nle-trim-edge={edge}
              data-h3-nle-control="clip.trim"
              data-dragging={
                isDraft && draft.identity.edge === edge ? "true" : "false"
              }
              aria-label={fill(
                edge === "start"
                  ? text.timeline.trimStart
                  : text.timeline.trimEnd,
                { name },
              )}
              aria-describedby="h3-nle-trim-instructions"
              title={text.timeline.trimInstructions}
              disabled={!editable || busy}
              onPointerDown={(event) => onGripPointerDown(event, clip, edge)}
              onPointerMove={(event) => onGripPointerMove(event, clip)}
              onPointerUp={onGripPointerUp}
              onPointerCancel={onGripPointerCancel}
              onLostPointerCapture={onGripLostCapture}
              onBlur={onGripBlur}
              onKeyDown={(event) => onGripKeyDown(event, clip, edge)}
            />
          );
          return (
            <div
              key={clip.clipId}
              className="h3-nle-clip"
              data-h3-nle-clip={clip.clipId}
              data-h3-nle-clip-kind={
                clip.assetId === null
                  ? "text"
                  : track.kind === "image_overlay"
                    ? "image"
                    : "video"
              }
              data-h3-nle-trim-clip={
                affordances.grips === "both" ? clip.clipId : undefined
              }
              data-h3-nle-inline-grips={inlineGrips}
              data-selected={isSelected ? "true" : "false"}
              data-enabled={clip.enabled ? "true" : "false"}
              data-highlighted={
                clip.assetId !== null && highlighted.has(clip.assetId)
                  ? "true"
                  : "false"
              }
              style={{ left: `${left}px`, width: `${width}px` }}
              onContextMenu={(event) => onOpenClipMenu(event, clip.clipId)}
            >
              <button
                type="button"
                className="h3-nle-clip-body"
                data-h3-nle-control="selection.set"
                aria-pressed={isSelected}
                aria-label={label}
                title={label}
                onPointerDown={(event) => onMovePointerDown(event, clip)}
                onPointerMove={onMovePointerMove}
                onPointerUp={onMovePointerUp}
                onPointerCancel={onMovePointerCancel}
                onLostPointerCapture={onMoveLostCapture}
                onBlur={onMoveBlur}
                onKeyDown={(event) => {
                  // M25-62: the clip menu stays keyboard-reachable at every width, including
                  // where no inline trigger fits (APG: Shift+F10 and the ContextMenu key).
                  if (
                    event.key === "ContextMenu" ||
                    (event.shiftKey && event.key === "F10")
                  ) {
                    event.preventDefault();
                    event.stopPropagation();
                    onOpenClipMenuFromKey(event.currentTarget, clip.clipId);
                    return;
                  }
                  onClipKeyDown(event, clip);
                }}
                onClick={(event) =>
                  onClipClick(
                    clip,
                    event.shiftKey
                      ? "range"
                      : event.ctrlKey || event.metaKey
                        ? "toggle"
                        : "replace",
                  )
                }
                onDoubleClick={(event) => {
                  if (
                    event.altKey ||
                    event.ctrlKey ||
                    event.metaKey ||
                    event.shiftKey ||
                    event.button !== 0
                  )
                    return;
                  const offset = Math.max(
                    0,
                    event.clientX -
                      event.currentTarget.getBoundingClientRect().left,
                  );
                  const target = Math.max(
                    clip.startFrame,
                    Math.min(
                      clipEnd(clip) - 1,
                      clip.startFrame + Math.floor(offset / pixelsPerFrame),
                    ),
                  );
                  onClipDoubleClick(target);
                }}
              >
                {affordances.label ? (
                  <span className="h3-nle-clip-label">{name}</span>
                ) : null}
                {affordances.duration ? (
                  <small className="h3-nle-clip-duration">
                    {formatTimelineTimecode(end - start, fps)}
                  </small>
                ) : null}
              </button>
              {grip("start")}
              {affordances.menu ? (
                <button
                  type="button"
                  className="h3-nle-clip-menu-trigger"
                  data-h3-nle-menu-trigger="clip"
                  aria-haspopup="menu"
                  aria-label={timelineMenuLabels(locale).openClip}
                  onClick={(event) => onOpenClipMenu(event, clip.clipId)}
                >
                  <NleActionIcon name="more" size={14} />
                </button>
              ) : null}
              {grip("end")}
            </div>
          );
        })}
      </div>
    </div>
  );
}
