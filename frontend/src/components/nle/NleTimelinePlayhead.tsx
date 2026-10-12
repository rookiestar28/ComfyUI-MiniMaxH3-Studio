import { memo } from "react";
import { laneXForBoundary } from "../../runtime/timelineGeometry";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTransport } from "./useNleTimelineTransport";

type ViewProps = Pick<
  ReturnType<typeof useNleTimelineViewport>,
  "pixelsPerFrame" | "viewStart" | "viewEnd"
> &
  Pick<
    ReturnType<typeof useNleTimelineTransport>,
    | "rulerRef"
    | "playheadElementRef"
    | "navigationAvailableRef"
    | "scrubRef"
    | "ruler"
    | "flushRulerSeek"
    | "beginRulerScrub"
    | "rulerSeek"
    | "cancelScrub"
  > &
  Readonly<{
    /** The playhead ref's value when the timeline rendered. */
    playheadFrame: number | null;
  }>;

function NleTimelinePlayheadView({
  pixelsPerFrame,
  viewStart,
  viewEnd,
  rulerRef,
  playheadElementRef,
  playheadFrame,
  navigationAvailableRef,
  scrubRef,
  ruler,
  flushRulerSeek,
  beginRulerScrub,
  rulerSeek,
  cancelScrub,
}: ViewProps) {
  return (
    <>
      <div className="h3-nle-playhead-layer" aria-hidden="true">
        <div
          ref={playheadElementRef}
          className="h3-nle-playhead"
          hidden={
            playheadFrame === null ||
            playheadFrame < viewStart ||
            playheadFrame >= viewEnd
          }
          style={{
            left:
              playheadFrame === null
                ? undefined
                : `${laneXForBoundary(
                    playheadFrame,
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
    </>
  );
}

// IMPORTANT: the head is memoized like the ruler; the timeline passes the playhead frame by value
// so that the memo still re-renders it when the frame moves between edits.
export const NleTimelinePlayhead = memo(NleTimelinePlayheadView);
