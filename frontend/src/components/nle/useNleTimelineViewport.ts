import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { CompositionClip } from "../../contracts/compositionCodec";
import { useRetainedSlot } from "../useRetainedSlot";
import {
  LANE_LEAD_IN_PX,
  clampTimelineScale,
  clampViewStart,
  contentXFromClientX,
  timelineScaleBounds,
  trackHeaderWidthPx,
  zoomToSelection,
} from "../../runtime/timelineGeometry";
import {
  fitTimelineContentView,
  zoomTimelineViewport,
  type TimelineViewportState,
} from "../../runtime/timelineNavigation";
import {
  scrollBarGeometry,
  viewStartForScrollThumb,
} from "./nleTimelineSurface";
import {
  NLE_ROW_HEIGHT_PX,
  NLE_MAX_VISIBLE_ROWS,
  NLE_MAX_CLIP_NODES,
  RESIZE_CORNER_CLEARANCE_PX,
  clipEnd,
  type NleTimelineProps,
} from "./nleTimelineTypes";

const LEGACY_ZOOM_LEVELS = Object.freeze([0.25, 0.5, 1, 2, 4, 8]);
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
 *  (R2, R7): the grip, rail and roll rules differ by primary pointer, as the CSS target
 * floor does. jsdom has no `matchMedia` and is treated as a fine pointer.
 */
function useCoarsePointer(): boolean {
  return useSyncExternalStore(
    subscribeCoarsePointer,
    readCoarsePointer,
    () => false,
  );
}

function retainedInteger(value: unknown, low: number, high: number): number {
  return typeof value === "number" && Number.isInteger(value)
    ? Math.min(high, Math.max(low, value))
    : low;
}

export function useNleTimelineViewport({
  snapshot,
  selection,
  retention,
  editCapacityFrames,
  contentEndExclusive,
  focusedClipId,
  readPlayheadFrame,
  requestPaint,
}: Pick<
  NleTimelineProps,
  | "snapshot"
  | "selection"
  | "retention"
  | "editCapacityFrames"
  | "contentEndExclusive"
> &
  Readonly<{
    focusedClipId: string | null;
    readPlayheadFrame(): number | null;
    requestPaint(): void;
  }>) {
  const paintRequestRef = useRef(requestPaint);
  paintRequestRef.current = requestPaint;
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
  const viewStartRef = useRef(viewStart);
  viewStartRef.current = viewStart;
  const pixelsPerFrameRef = useRef(pixelsPerFrame);
  pixelsPerFrameRef.current = pixelsPerFrame;
  const scrollDragRef = useRef<{ pointerId: number; grab: number } | null>(
    null,
  );
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
    // IMPORTANT: the accessible trim rail changes this box by less than one 56px track
    // row as part of the same accepted edit. Keep the exact paint height in a ref and repaint
    // directly; a ResizeObserver setState here creates a fifth NLE commit. One extra virtual row
    // covers that geometry change, while larger host resizes still grow the mounted window below.
    const measure = () => {
      const nextHeight = element.clientHeight || NLE_ROW_HEIGHT_PX * 4;
      if (nextHeight !== measuredViewportHeightRef.current) {
        measuredViewportHeightRef.current = nextHeight;
        paintRequestRef.current();
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
      // `laneWidth` is the frame-mapped width, right of the lead-in.
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
  //  (A62-6): the bar spans the lane columns (lead-in included) and drives the same
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
  const zoomBy = (factor: number, anchorX?: number) => {
    setTimelineView((current) => {
      const logicalFrame = readPlayheadFrame();
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
    const chosen = snapshot.clips.filter((clip) =>
      selection.includes(clip.clipId),
    );
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

  return {
    scrollRef,
    timelineView,
    setTimelineView,
    pixelsPerFrame,
    fitLocked,
    viewStart,
    snapEnabled,
    setSnapEnabled,
    rippleEnabled,
    setRippleEnabled,
    scrollTop,
    setScrollTop,
    laneWidth,
    laneWidthRef,
    virtualizationHeight,
    measuredViewportHeightRef,
    durationFrames,
    durationFramesRef,
    transportEndExclusive,
    transportLastFrame,
    fps,
    tracks,
    coarse,
    headerWidth,
    headerWidthRef,
    viewStartRef,
    pixelsPerFrameRef,
    scrollDragRef,
    mappingKey,
    scaleBounds,
    totalRows,
    firstRow,
    lastRow,
    visibleTracks,
    visibleFrames,
    viewEnd,
    scrollBarInput,
    scrollBar,
    scrollToThumb,
    clipsByTrack,
    mountedClipIds,
    visibleCandidates,
    zoomBy,
    fit,
    zoomSelection,
    zoomPosition,
    setZoomPosition,
  };
}
