import { ownRangeKeyDown } from "../ownedRangeKeys";
import { MAX_TIMELINE_SCALE } from "../../runtime/timelineGeometry";
import { fill, nleCopy } from "./nleCopy";
import { NleTimelineToolbar } from "./NleTimelineToolbar";
import { type NleTimelineProps } from "./nleTimelineTypes";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTransport } from "./useNleTimelineTransport";
import type { useNleTimelineTrim } from "./useNleTimelineTrim";
import type { useNleTimelineDirectGesture } from "./useNleTimelineDirectGesture";
import type { useNleTimelineRoll } from "./useNleTimelineRoll";
import type { useNleTimelineSelection } from "./useNleTimelineSelection";
import type { useNleTimelineTools } from "./useNleTimelineTools";

type ViewProps = Pick<
  NleTimelineProps,
  | "locale"
  | "selection"
  | "authoring"
  | "gridFrames"
  | "onSeek"
  | "onBeginScrub"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    | "setTimelineView"
    | "pixelsPerFrame"
    | "fitLocked"
    | "viewStart"
    | "snapEnabled"
    | "setSnapEnabled"
    | "rippleEnabled"
    | "setRippleEnabled"
    | "transportLastFrame"
    | "scaleBounds"
    | "totalRows"
    | "firstRow"
    | "lastRow"
    | "zoomBy"
    | "fit"
    | "zoomSelection"
    | "zoomPosition"
    | "setZoomPosition"
  > &
  Pick<
    ReturnType<typeof useNleTimelineTransport>,
    | "previousFrameRef"
    | "nextFrameRef"
    | "playheadFrameRef"
    | "navigationAvailableRef"
  > &
  Pick<ReturnType<typeof useNleTimelineTrim>, "cancelPointer"> &
  Pick<
    ReturnType<typeof useNleTimelineDirectGesture>,
    | "directGesture"
    | "clickMove"
    | "setClickMove"
    | "cancelDirect"
    | "applyClickMove"
  > &
  Pick<ReturnType<typeof useNleTimelineRoll>, "setRollDraft" | "rollCuts"> &
  Pick<
    ReturnType<typeof useNleTimelineSelection>,
    | "clickSelectionMode"
    | "setClickSelectionMode"
    | "orderedNavigationClips"
    | "selectAdjacent"
    | "replaceSelection"
  > &
  Pick<ReturnType<typeof useNleTimelineTools>, "toolDecisions" | "runTool"> &
  Readonly<{
    text: ReturnType<typeof nleCopy>;
    maxViewStart: number;
    focusedClipId: string | null;
  }>;

export function NleTimelineNavigation({
  locale,
  selection,
  authoring,
  gridFrames,
  onSeek,
  onBeginScrub,
  setTimelineView,
  pixelsPerFrame,
  fitLocked,
  viewStart,
  snapEnabled,
  setSnapEnabled,
  rippleEnabled,
  setRippleEnabled,
  transportLastFrame,
  scaleBounds,
  totalRows,
  firstRow,
  lastRow,
  zoomBy,
  fit,
  zoomSelection,
  zoomPosition,
  setZoomPosition,
  previousFrameRef,
  nextFrameRef,
  playheadFrameRef,
  navigationAvailableRef,
  cancelPointer,
  directGesture,
  clickMove,
  setClickMove,
  cancelDirect,
  applyClickMove,
  setRollDraft,
  rollCuts,
  clickSelectionMode,
  setClickSelectionMode,
  orderedNavigationClips,
  selectAdjacent,
  replaceSelection,
  toolDecisions,
  runTool,
  text,
  maxViewStart,
  focusedClipId,
}: ViewProps) {
  return (
    <>
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
    </>
  );
}
