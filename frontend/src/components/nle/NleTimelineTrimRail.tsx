import type { CompositionClip } from "../../contracts/compositionCodec";
import { type TrimGestureState } from "../../runtime/nleTrimGesture";
import { formatTimelineTimecode } from "../../runtime/timelineNavigation";
import { fill, nleCopy } from "./nleCopy";
import { NleActionIcon } from "./NleIconActions";
import { timelineMenuLabels } from "./NleTimelineMenus";
import { clipEnd, type NleTimelineProps } from "./nleTimelineTypes";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTrim } from "./useNleTimelineTrim";
import type { useNleTimelineSelection } from "./useNleTimelineSelection";

type ViewProps = Pick<NleTimelineProps, "locale" | "snapshot"> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    "durationFrames" | "fps" | "tracks"
  > &
  Pick<
    ReturnType<typeof useNleTimelineTrim>,
    | "gesture"
    | "beginPointer"
    | "movePointer"
    | "releasePointer"
    | "cancelPointer"
    | "gripKeyDown"
  > &
  Pick<ReturnType<typeof useNleTimelineSelection>, "openClipMenu"> &
  Readonly<{
    draftable: boolean;
    text: ReturnType<typeof nleCopy>;
    clipName: (clip: CompositionClip) => string;
    railClip: CompositionClip | undefined;
    draft:
      | Extract<
          TrimGestureState,
          { phase: "dragging" | "keyboard_draft" }
        >["draft"]
      | null;
  }>;

export function NleTimelineTrimRail({
  locale,
  snapshot,
  durationFrames,
  fps,
  tracks,
  gesture,
  beginPointer,
  movePointer,
  releasePointer,
  cancelPointer,
  gripKeyDown,
  openClipMenu,
  text,
  clipName,
  railClip,
  draft,
  draftable,
}: ViewProps) {
  return (
    <>
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
    </>
  );
}
