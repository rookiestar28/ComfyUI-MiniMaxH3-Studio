import { useEffect, useRef } from "react";

/** The mutually exclusive trim/direct drafts share one DOM capture lease. */
export function useNleTimelineCapture() {
  const captureRef = useRef<{
    element: HTMLButtonElement;
    pointerId: number;
  } | null>(null);
  const releaseCapture = () => {
    const capture = captureRef.current;
    // IMPORTANT: clear the lease before DOM release; lostpointercapture can re-enter
    // cancellation. Clearing it afterwards can release the same capture twice.
    captureRef.current = null;
    if (capture?.element.hasPointerCapture(capture.pointerId))
      capture.element.releasePointerCapture(capture.pointerId);
  };
  useEffect(() => () => releaseCapture(), []);
  return { captureRef, releaseCapture };
}
