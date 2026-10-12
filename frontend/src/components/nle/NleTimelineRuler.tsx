import { memo } from "react";
import {
  LANE_LEAD_IN_PX,
  formatRulerLabel,
  laneXForBoundary,
} from "../../runtime/timelineGeometry";
import { formatTimelineTimecode } from "../../runtime/timelineNavigation";
import { nleCopy } from "./nleCopy";
import { type NleTimelineProps } from "./nleTimelineTypes";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTransport } from "./useNleTimelineTransport";

type ViewProps = Pick<NleTimelineProps, "gridFrames" | "onBeginScrub"> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    "pixelsPerFrame" | "viewStart" | "laneWidth" | "transportLastFrame" | "fps"
  > &
  Pick<
    ReturnType<typeof useNleTimelineTransport>,
    | "rulerRef"
    | "playheadFrameRef"
    | "navigationAvailableRef"
    | "scrubRef"
    | "ruler"
    | "rulerMinorPath"
    | "rulerFrame"
    | "rulerRequestedFrameRef"
    | "queueRulerSeek"
    | "flushRulerSeek"
    | "beginRulerScrub"
    | "rulerSeek"
    | "cancelScrub"
  > &
  Readonly<{
    text: ReturnType<typeof nleCopy>;
    /** The refs' values when the timeline rendered; the handlers still read the refs live. */
    navigationAvailable: boolean;
    requestClearReason: string;
    requestGeneration: number | null;
  }>;

function NleTimelineRulerView({
  gridFrames,
  onBeginScrub,
  pixelsPerFrame,
  viewStart,
  laneWidth,
  transportLastFrame,
  fps,
  rulerRef,
  playheadFrameRef,
  navigationAvailableRef,
  scrubRef,
  ruler,
  rulerMinorPath,
  rulerFrame,
  rulerRequestedFrameRef,
  queueRulerSeek,
  flushRulerSeek,
  beginRulerScrub,
  rulerSeek,
  cancelScrub,
  text,
  navigationAvailable,
  requestClearReason,
  requestGeneration,
}: ViewProps) {
  return (
    <>
      <div
        ref={rulerRef}
        className="h3-nle-ruler"
        role="slider"
        tabIndex={navigationAvailable ? 0 : -1}
        aria-disabled={!navigationAvailable}
        aria-label={text.timeline.playhead}
        aria-valuemin={0}
        aria-valuemax={transportLastFrame}
        aria-valuenow={rulerFrame}
        aria-valuetext={formatTimelineTimecode(rulerFrame, fps)}
        data-h3-nle-control="transport.seek"
        data-h3-nle-request-clear-reason={requestClearReason}
        data-h3-nle-request-generation={requestGeneration ?? "unacknowledged"}
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
    </>
  );
}

// IMPORTANT: an accepted edit re-renders the timeline while the ruler's marks, frame and seek
// delegates stay equal; keep this boundary and keep every render-time input a prop (see the
// timeline's guard): a value read from a ref here would go stale behind the memo.
export const NleTimelineRuler = memo(NleTimelineRulerView);
