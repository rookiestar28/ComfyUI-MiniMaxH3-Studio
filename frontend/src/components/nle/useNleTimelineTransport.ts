import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  contentXFromClientX,
  frameFromX,
  laneXForBoundary,
  rulerMarks,
  rulerMinorTicks,
} from "../../runtime/timelineGeometry";
import { formatTimelineTimecode } from "../../runtime/timelineNavigation";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type {
  NleTimelineProps,
  NleTimelineTransportSnapshot,
} from "./nleTimelineTypes";

type TransportInput = Pick<
  NleTimelineProps,
  | "snapshot"
  | "playheadFrame"
  | "transportAvailable"
  | "navigationAvailable"
  | "transportChannel"
  | "onSeek"
  | "onBeginScrub"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    | "durationFrames"
    | "fps"
    | "laneWidth"
    | "pixelsPerFrame"
    | "viewStart"
    | "viewStartRef"
    | "pixelsPerFrameRef"
    | "headerWidthRef"
    | "transportEndExclusive"
    | "transportLastFrame"
  >;

export function useNleTimelineTransport({
  snapshot,
  playheadFrame,
  transportAvailable = true,
  navigationAvailable = true,
  transportChannel,
  onSeek,
  onBeginScrub,
  durationFrames,
  fps,
  laneWidth,
  pixelsPerFrame,
  viewStart,
  viewStartRef,
  pixelsPerFrameRef,
  headerWidthRef,
  transportEndExclusive,
  transportLastFrame,
}: TransportInput) {
  const playheadFrameRef = useRef(playheadFrame);
  const transportAvailableRef = useRef(transportAvailable);
  const navigationAvailableRef = useRef(navigationAvailable);
  if (transportChannel === undefined) {
    playheadFrameRef.current = playheadFrame;
    transportAvailableRef.current = transportAvailable;
    navigationAvailableRef.current = navigationAvailable;
  }
  const rulerRef = useRef<HTMLDivElement>(null);
  const previousFrameRef = useRef<HTMLButtonElement>(null);
  const nextFrameRef = useRef<HTMLButtonElement>(null);
  const playheadElementRef = useRef<HTMLDivElement>(null);
  const scrubRef = useRef<{
    pointerId: number;
    ownerKey: string;
    originFrame: number;
    targetFrame: number;
    raf: number | null;
  } | null>(null);
  const ruler = useMemo(
    () =>
      rulerMarks({ viewStart, laneWidth, pixelsPerFrame, durationFrames, fps }),
    [durationFrames, fps, laneWidth, pixelsPerFrame, viewStart],
  );
  // minor ticks live in the ruler only, as one path; nothing periodic enters the tracks.
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
        // the layer's origin is the header edge, the same origin as each lane.
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
  useEffect(
    () => () => {
      const scrub = scrubRef.current;
      if (scrub !== null && scrub.raf !== null) cancelAnimationFrame(scrub.raf);
      scrubRef.current = null;
    },
    [],
  );
  // IMPORTANT: the ruler and the playhead head are memoized views, and these delegates never change
  // identity. They run the seek handlers published after the latest commit. Publishing during
  // render would let an aborted render's owner key and seek bounds take a later pointer or key, and
  // capturing the mount handlers would clamp seeks to the first timeline's last frame.
  const committedSeek = useRef({
    queueRulerSeek,
    flushRulerSeek,
    beginRulerScrub,
    rulerSeek,
    cancelScrub,
  });
  useLayoutEffect(() => {
    committedSeek.current = {
      queueRulerSeek,
      flushRulerSeek,
      beginRulerScrub,
      rulerSeek,
      cancelScrub,
    };
  });
  const seekDelegates = useMemo(
    () => ({
      queueRulerSeek: (frame: number) =>
        committedSeek.current.queueRulerSeek(frame),
      flushRulerSeek: () => committedSeek.current.flushRulerSeek(),
      beginRulerScrub: (pointerId: number) =>
        committedSeek.current.beginRulerScrub(pointerId),
      rulerSeek: (clientX: number, target: HTMLElement) =>
        committedSeek.current.rulerSeek(clientX, target),
      cancelScrub: () => committedSeek.current.cancelScrub(),
    }),
    [],
  );
  return {
    rulerRef,
    previousFrameRef,
    nextFrameRef,
    playheadElementRef,
    playheadFrameRef,
    transportAvailableRef,
    navigationAvailableRef,
    scrubRef,
    ruler,
    rulerMinorPath,
    rulerFrame,
    rulerRequestedFrameRef,
    rulerClearReasonRef,
    rulerAcknowledgedGenerationRef,
    renderedToolKeyRef,
    currentToolKeyRef,
    ...seekDelegates,
  };
}
