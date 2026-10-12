import {
  useEffect,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { CompositionClip } from "../../contracts/compositionCodec";
import { reduceTrimGesture } from "../../runtime/nleTrimGesture";
import {
  laneXForBoundary,
  laneXFromClientX,
} from "../../runtime/timelineGeometry";
import { build } from "./nleCommandBuilders";
import {
  NLE_ROW_HEIGHT_PX,
  DRAG_THRESHOLD_PX,
  type NleTimelineProps,
} from "./nleTimelineTypes";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTrim } from "./useNleTimelineTrim";
import type { useNleTimelineDirectGesture } from "./useNleTimelineDirectGesture";
import type { useNleTimelineRoll } from "./useNleTimelineRoll";
import type { useNleTimelineSelection } from "./useNleTimelineSelection";
import type { useNleTimelineTools } from "./useNleTimelineTools";

type InteractionInput = Pick<
  NleTimelineProps,
  "snapshot" | "selection" | "onIntent" | "onTrimState" | "onEdgeGestureActive"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    | "mappingKey"
    | "mountedClipIds"
    | "scrollRef"
    | "headerWidthRef"
    | "tracks"
    | "viewStart"
    | "pixelsPerFrame"
  > &
  Pick<
    ReturnType<typeof useNleTimelineTrim>,
    "gesture" | "gestureRef" | "setGesture" | "cancelPointer"
  > &
  Pick<
    ReturnType<typeof useNleTimelineDirectGesture>,
    "directGestureRef" | "autoScrollMappingRef" | "cancelDirect"
  > &
  Pick<
    ReturnType<typeof useNleTimelineRoll>,
    "rollDraft" | "rollDraftRef" | "setRollDraft" | "cancelRoll"
  > &
  Pick<ReturnType<typeof useNleTimelineSelection>, "pendingFocusClipId"> &
  Pick<ReturnType<typeof useNleTimelineTools>, "shortcutHandlerRef"> &
  Readonly<{
    draft:
      | Extract<
          ReturnType<typeof useNleTimelineTrim>["gesture"],
          { phase: "dragging" | "keyboard_draft" }
        >["draft"]
      | null;
    railClip: CompositionClip | undefined;
    releaseCapture(): void;
  }>;

export function useNleTimelineInteraction({
  snapshot,
  selection,
  onIntent,
  onTrimState,
  onEdgeGestureActive,
  draft,
  railClip,
  releaseCapture,
  mappingKey,
  mountedClipIds,
  scrollRef,
  headerWidthRef,
  tracks,
  viewStart,
  pixelsPerFrame,
  gesture,
  gestureRef,
  setGesture,
  cancelPointer,
  directGestureRef,
  autoScrollMappingRef,
  cancelDirect,
  rollDraft,
  rollDraftRef,
  setRollDraft,
  cancelRoll,
  pendingFocusClipId,
  shortcutHandlerRef,
}: InteractionInput) {
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

  useEffect(() => {
    onTrimState?.(gesture);
    onEdgeGestureActive(
      gesture.phase === "dragging" ||
        gesture.phase === "keyboard_draft" ||
        rollDraft !== null,
    );
  }, [gesture, rollDraft, onEdgeGestureActive, onTrimState]);

  // A zoom/scroll/viewport change invalidates any draft: its pixel mapping is gone.
  useEffect(() => {
    // IMPORTANT ( B3-D56): same rule as the effect below -- do not enqueue an updater that
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
      window.removeEventListener("blur", cancelUncommitted);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);

  const gridKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    // IMPORTANT: standalone grid select-all must use the same repeat/draft
    // guard as the workspace; a second selection path bypasses both.
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a")
      shortcutHandlerRef.current(event);
  };
  const gridPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (event.button !== 0 || target.closest("button,[data-h3-nle-clip]"))
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
  };
  const gridPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (marquee === null || marquee.pointerId !== event.pointerId) return;
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
  };
  const gridPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (marquee === null || marquee.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    const intersected = snapshot.clips
      .filter((clip) => {
        const row = tracks.findIndex((track) => track.trackId === clip.trackId);
        const x = laneXForBoundary(clip.startFrame, viewStart, pixelsPerFrame);
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
  };
  const gridPointerCancel = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (marquee?.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    setMarquee(null);
  };
  return {
    marquee,
    gridKeyDown,
    gridPointerDown,
    gridPointerMove,
    gridPointerUp,
    gridPointerCancel,
  };
}
