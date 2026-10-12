import type { CompositionClip } from "../../contracts/compositionCodec";
import { type TrimGestureState } from "../../runtime/nleTrimGesture";
import { timelineXForBoundary } from "../../runtime/timelineGeometry";
import { nleCopy } from "./nleCopy";
import { trackDisplayNames } from "./nleTimelineSurface";
import { NleTimelineTrackRow } from "./NleTimelineTrackRow";
import {
  NLE_ROW_HEIGHT_PX,
  ROLL_TARGET_PX,
  type NleTimelineProps,
} from "./nleTimelineTypes";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTransport } from "./useNleTimelineTransport";
import type { useNleTimelineTrim } from "./useNleTimelineTrim";
import type { useNleTimelineDirectGesture } from "./useNleTimelineDirectGesture";
import type { useNleTimelineRoll } from "./useNleTimelineRoll";
import type { useNleTimelineSelection } from "./useNleTimelineSelection";
import type { useNleTimelineTools } from "./useNleTimelineTools";
import type { useNleTimelineInteraction } from "./useNleTimelineInteraction";

type ViewProps = Pick<
  NleTimelineProps,
  | "locale"
  | "snapshot"
  | "selection"
  | "authoring"
  | "onIntent"
  | "onSeek"
  | "onBeginScrub"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    | "scrollRef"
    | "pixelsPerFrame"
    | "viewStart"
    | "scrollTop"
    | "setScrollTop"
    | "durationFrames"
    | "fps"
    | "tracks"
    | "coarse"
    | "headerWidth"
    | "totalRows"
    | "firstRow"
    | "lastRow"
    | "visibleTracks"
    | "viewEnd"
    | "clipsByTrack"
    | "mountedClipIds"
  > &
  Pick<ReturnType<typeof useNleTimelineTransport>, "navigationAvailableRef"> &
  Pick<
    ReturnType<typeof useNleTimelineTrim>,
    | "gesture"
    | "gestureRef"
    | "beginPointer"
    | "movePointer"
    | "releasePointer"
    | "cancelPointer"
    | "gripKeyDown"
  > &
  Pick<
    ReturnType<typeof useNleTimelineDirectGesture>,
    | "directGesture"
    | "directGestureRef"
    | "beginMove"
    | "moveDirect"
    | "releaseDirect"
    | "cancelDirect"
    | "clickClip"
    | "clipKeyDown"
  > &
  Pick<
    ReturnType<typeof useNleTimelineRoll>,
    | "rollDraft"
    | "setRollDraft"
    | "rollDraftRef"
    | "suppressRollClickRef"
    | "selectedClip"
    | "rollCuts"
    | "cancelRoll"
    | "beginRollPointer"
    | "moveRollPointer"
    | "releaseRollPointer"
    | "rollKeyDown"
  > &
  Pick<
    ReturnType<typeof useNleTimelineSelection>,
    | "clickSelectionMode"
    | "openTrackMenu"
    | "openClipMenu"
    | "openClipMenuFromKey"
    | "selected"
  > &
  Pick<ReturnType<typeof useNleTimelineTools>, "decide"> &
  Pick<
    ReturnType<typeof useNleTimelineInteraction>,
    | "gridKeyDown"
    | "gridPointerDown"
    | "gridPointerMove"
    | "gridPointerUp"
    | "gridPointerCancel"
  > &
  Readonly<{
    draftable: boolean;
    text: ReturnType<typeof nleCopy>;
    trackNames: ReturnType<typeof trackDisplayNames>;
    clipName: (clip: CompositionClip) => string;
    highlighted: ReadonlySet<string>;
    draft:
      | Extract<
          TrimGestureState,
          { phase: "dragging" | "keyboard_draft" }
        >["draft"]
      | null;
    decorationRef: import("react").RefObject<HTMLCanvasElement | null>;
  }>;

export function NleTimelineTracks({
  locale,
  snapshot,
  selection,
  authoring,
  onIntent,
  onSeek,
  onBeginScrub,
  scrollRef,
  pixelsPerFrame,
  viewStart,
  scrollTop,
  setScrollTop,
  durationFrames,
  fps,
  tracks,
  coarse,
  headerWidth,
  totalRows,
  firstRow,
  lastRow,
  visibleTracks,
  viewEnd,
  clipsByTrack,
  mountedClipIds,
  navigationAvailableRef,
  gesture,
  gestureRef,
  beginPointer,
  movePointer,
  releasePointer,
  cancelPointer,
  gripKeyDown,
  directGesture,
  directGestureRef,
  beginMove,
  moveDirect,
  releaseDirect,
  cancelDirect,
  clickClip,
  clipKeyDown,
  rollDraft,
  setRollDraft,
  rollDraftRef,
  suppressRollClickRef,
  selectedClip,
  rollCuts,
  cancelRoll,
  beginRollPointer,
  moveRollPointer,
  releaseRollPointer,
  rollKeyDown,
  clickSelectionMode,
  openTrackMenu,
  openClipMenu,
  openClipMenuFromKey,
  selected,
  decide,
  gridKeyDown,
  gridPointerDown,
  gridPointerMove,
  gridPointerUp,
  gridPointerCancel,
  text,
  trackNames,
  clipName,
  highlighted,
  draft,
  decorationRef,
  draftable,
}: ViewProps) {
  return (
    <>
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
        onKeyDown={gridKeyDown}
        onPointerDown={gridPointerDown}
        onPointerMove={gridPointerMove}
        onPointerUp={gridPointerUp}
        onPointerCancel={gridPointerCancel}
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
            <NleTimelineTrackRow
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
                authoring.status === "pending" || gesture.phase === "submitting"
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
                clickClip(clip, mode === "replace" ? clickSelectionMode : mode)
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
            const other = selectedClip?.clipId === left?.clipId ? right : left;
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
                // IMPORTANT (, A62-4): under a fine pointer the 24 px roll target sits in
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
                  if (rollDraftRef.current?.input === "keyboard") cancelRoll();
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
    </>
  );
}
