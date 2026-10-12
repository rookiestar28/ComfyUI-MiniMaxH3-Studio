import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTransport } from "./useNleTimelineTransport";

type ViewProps = Pick<
  ReturnType<typeof useNleTimelineViewport>,
  "tracks" | "scrollDragRef" | "scrollBarInput" | "scrollBar" | "scrollToThumb"
> &
  Pick<ReturnType<typeof useNleTimelineTransport>, "ruler">;

export function NleTimelineScrollBar({
  tracks,
  scrollDragRef,
  scrollBarInput,
  scrollBar,
  scrollToThumb,
  ruler,
}: ViewProps) {
  return (
    <>
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
      {/* IMPORTANT (, A62-3): the playhead is outside the tracks scroller, from the ruler
          top to the lane bottom, clipped to the lane columns. Inside the scroller, vertical
          scroll and row virtualization shortened it and the first clip edge hid it at frame 0. */}
    </>
  );
}
