import {
  memo,
  useLayoutEffect,
  useMemo,
  useRef,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type {
  CompositionClip,
  CompositionTrack,
} from "../../contracts/compositionCodec";
import type { Locale } from "../../i18n/catalog";
import type { TrimEdge, TrimGestureState } from "../../runtime/nleTrimGesture";
import { laneXForBoundary } from "../../runtime/timelineGeometry";
import { formatTimelineTimecode } from "../../runtime/timelineNavigation";
import { build } from "./nleCommandBuilders";
import { fill, nleCopy } from "./nleCopy";
import { NleActionIcon } from "./NleIconActions";
import { NleTrackHeader } from "./NleTrackHeader";
import { clipEditAffordances } from "./nleTimelineSurface";
import { timelineMenuLabels } from "./NleTimelineMenus";
import { NLE_ROW_HEIGHT_PX, clipEnd } from "./nleTimelineTypes";

type RowProps = {
  locale: Locale;
  fps: number;
  track: CompositionTrack;
  trackName: string;
  rowIndex: number;
  clips: readonly CompositionClip[];
  clipName(clip: CompositionClip): string;
  viewStart: number;
  pixelsPerFrame: number;
  coarse: boolean;
  emptyLane: boolean;
  selected: ReadonlySet<string>;
  highlighted: ReadonlySet<string>;
  draft:
    | Extract<
        TrimGestureState,
        { phase: "dragging" | "keyboard_draft" }
      >["draft"]
    | null;
  busy: boolean;
  onMovePointerDown(
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ): void;
  onMovePointerMove(event: ReactPointerEvent<HTMLButtonElement>): void;
  onMovePointerUp(event: ReactPointerEvent<HTMLButtonElement>): void;
  onMovePointerCancel(): void;
  onMoveLostCapture(): void;
  onMoveBlur(): void;
  onClipClick(
    clip: CompositionClip,
    mode: "replace" | "toggle" | "range",
  ): void;
  onClipDoubleClick(frame: number): void;
  onClipKeyDown(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ): void;
  onTrackCommand(command: ReturnType<typeof build.setTrackLocked>): void;
  onOpenTrackMenu(
    event: ReactMouseEvent<HTMLElement>,
    trackId: string | null,
  ): void;
  onOpenClipMenu(event: ReactMouseEvent<HTMLElement>, clipId: string): void;
  onOpenClipMenuFromKey(element: HTMLElement, clipId: string): void;
  onGripPointerDown(
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
    edge: TrimEdge,
  ): void;
  onGripPointerMove(
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ): void;
  onGripPointerUp(event: ReactPointerEvent<HTMLButtonElement>): void;
  onGripPointerCancel(): void;
  onGripLostCapture(): void;
  onGripBlur(): void;
  onGripKeyDown(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    clip: CompositionClip,
    edge: TrimEdge,
  ): void;
};

function useCommittedClipEvents(props: RowProps) {
  const committed = useRef(props);
  // IMPORTANT: publish event data only after commit. Render-phase writes expose aborted
  // snapshots; caching the mount props instead dispatches stale clips and authoring callbacks.
  useLayoutEffect(() => {
    committed.current = props;
  });
  return useMemo(
    () => ({
      onMovePointerDown: (
        event: ReactPointerEvent<HTMLButtonElement>,
        clipId: string,
      ) => {
        const current = committed.current;
        const clip = current.clips.find(
          (candidate) => candidate.clipId === clipId,
        );
        if (clip) current.onMovePointerDown(event, clip);
      },
      onClipClick: (clipId: string, mode: "replace" | "toggle" | "range") => {
        const current = committed.current;
        const clip = current.clips.find(
          (candidate) => candidate.clipId === clipId,
        );
        if (clip) current.onClipClick(clip, mode);
      },
      onClipKeyDown: (
        event: ReactKeyboardEvent<HTMLButtonElement>,
        clipId: string,
      ) => {
        const current = committed.current;
        const clip = current.clips.find(
          (candidate) => candidate.clipId === clipId,
        );
        if (clip) current.onClipKeyDown(event, clip);
      },
      onGripPointerDown: (
        event: ReactPointerEvent<HTMLButtonElement>,
        clipId: string,
        edge: TrimEdge,
      ) => {
        const current = committed.current;
        const clip = current.clips.find(
          (candidate) => candidate.clipId === clipId,
        );
        if (clip) current.onGripPointerDown(event, clip, edge);
      },
      onGripPointerMove: (
        event: ReactPointerEvent<HTMLButtonElement>,
        clipId: string,
      ) => {
        const current = committed.current;
        const clip = current.clips.find(
          (candidate) => candidate.clipId === clipId,
        );
        if (clip) current.onGripPointerMove(event, clip);
      },
      onGripKeyDown: (
        event: ReactKeyboardEvent<HTMLButtonElement>,
        clipId: string,
        edge: TrimEdge,
      ) => {
        const current = committed.current;
        const clip = current.clips.find(
          (candidate) => candidate.clipId === clipId,
        );
        if (clip) current.onGripKeyDown(event, clip, edge);
      },
      onMovePointerMove: (event: ReactPointerEvent<HTMLButtonElement>) =>
        committed.current.onMovePointerMove(event),
      onMovePointerUp: (event: ReactPointerEvent<HTMLButtonElement>) =>
        committed.current.onMovePointerUp(event),
      onMovePointerCancel: () => committed.current.onMovePointerCancel(),
      onMoveLostCapture: () => committed.current.onMoveLostCapture(),
      onMoveBlur: () => committed.current.onMoveBlur(),
      onClipDoubleClick: (frame: number) =>
        committed.current.onClipDoubleClick(frame),
      onOpenClipMenu: (event: ReactMouseEvent<HTMLElement>, clipId: string) =>
        committed.current.onOpenClipMenu(event, clipId),
      onOpenClipMenuFromKey: (element: HTMLElement, clipId: string) =>
        committed.current.onOpenClipMenuFromKey(element, clipId),
      onGripPointerUp: (event: ReactPointerEvent<HTMLButtonElement>) =>
        committed.current.onGripPointerUp(event),
      onGripPointerCancel: () => committed.current.onGripPointerCancel(),
      onGripLostCapture: () => committed.current.onGripLostCapture(),
      onGripBlur: () => committed.current.onGripBlur(),
      onTrackCommand: (command: Parameters<RowProps["onTrackCommand"]>[0]) =>
        committed.current.onTrackCommand(command),
      onOpenTrackMenu: (
        event: ReactMouseEvent<HTMLElement>,
        trackId: string | null,
      ) => committed.current.onOpenTrackMenu(event, trackId),
    }),
    [],
  );
}

type ClipProps = Readonly<{
  locale: Locale;
  fps: number;
  clipId: string;
  assetId: string | null;
  trackKind: CompositionTrack["kind"];
  enabled: boolean;
  name: string;
  start: number;
  end: number;
  clipStartFrame: number;
  clipEndFrame: number;
  left: number;
  width: number;
  pixelsPerFrame: number;
  coarse: boolean;
  isSelected: boolean;
  isHighlighted: boolean;
  draftEdge: TrimEdge | null;
  gripDisabled: boolean;
  events: ReturnType<typeof useCommittedClipEvents>;
}>;
const NleTimelineClip = memo(function NleTimelineClip({
  locale,
  fps,
  clipId,
  assetId,
  trackKind,
  enabled,
  name,
  start,
  end,
  clipStartFrame,
  clipEndFrame,
  left,
  width,
  pixelsPerFrame,
  coarse,
  isSelected,
  isHighlighted,
  draftEdge,
  gripDisabled,
  events,
}: ClipProps) {
  const text = nleCopy(locale);
  const label = fill(text.timeline.clip, {
    name,
    start: formatTimelineTimecode(start, fps),
    end: formatTimelineTimecode(end, fps),
    duration: formatTimelineTimecode(end - start, fps),
  });
  // IMPORTANT: one rule for every edit affordance. The fine grips sit 8 px
  // inside each edge, so a neighbour's pixels always hit the neighbour; below 24 px the
  // grip at the draft's edge stays mounted, so a draft that shrinks its own clip keeps
  // the control it is driving.
  const affordances = clipEditAffordances({
    width,
    selected: isSelected,
    coarse,
    draftEdge,
  });
  const showStart =
    affordances.grips === "both" || affordances.grips === "start";
  const showEnd = affordances.grips === "both" || affordances.grips === "end";
  const inlineGrips =
    affordances.grips === "both"
      ? "true"
      : affordances.grips === "none"
        ? "false"
        : "single";
  const grip = (edge: TrimEdge) => (
    <button
      type="button"
      className="h3-nle-grip"
      hidden={edge === "start" ? !showStart : !showEnd}
      data-h3-nle-trim-edge={edge}
      data-h3-nle-control="clip.trim"
      data-dragging={draftEdge === edge ? "true" : "false"}
      aria-label={fill(
        edge === "start" ? text.timeline.trimStart : text.timeline.trimEnd,
        { name },
      )}
      aria-describedby="h3-nle-trim-instructions"
      title={text.timeline.trimInstructions}
      disabled={gripDisabled}
      onPointerDown={(event) => events.onGripPointerDown(event, clipId, edge)}
      onPointerMove={(event) => events.onGripPointerMove(event, clipId)}
      onPointerUp={events.onGripPointerUp}
      onPointerCancel={events.onGripPointerCancel}
      onLostPointerCapture={events.onGripLostCapture}
      onBlur={events.onGripBlur}
      onKeyDown={(event) => events.onGripKeyDown(event, clipId, edge)}
    />
  );
  return (
    <div
      key={clipId}
      className="h3-nle-clip"
      data-h3-nle-clip={clipId}
      data-h3-nle-clip-kind={
        assetId === null
          ? "text"
          : trackKind === "image_overlay"
            ? "image"
            : "video"
      }
      data-h3-nle-trim-clip={affordances.grips === "both" ? clipId : undefined}
      data-h3-nle-inline-grips={inlineGrips}
      data-selected={isSelected ? "true" : "false"}
      data-enabled={enabled ? "true" : "false"}
      data-highlighted={isHighlighted ? "true" : "false"}
      style={{ left: `${left}px`, width: `${width}px` }}
      onContextMenu={(event) => events.onOpenClipMenu(event, clipId)}
    >
      <button
        type="button"
        className="h3-nle-clip-body"
        data-h3-nle-control="selection.set"
        aria-pressed={isSelected}
        aria-label={label}
        title={label}
        onPointerDown={(event) => events.onMovePointerDown(event, clipId)}
        onPointerMove={events.onMovePointerMove}
        onPointerUp={events.onMovePointerUp}
        onPointerCancel={events.onMovePointerCancel}
        onLostPointerCapture={events.onMoveLostCapture}
        onBlur={events.onMoveBlur}
        onKeyDown={(event) => {
          // The clip menu stays keyboard-reachable at every width, including
          // where no inline trigger fits (APG: Shift+F10 and the ContextMenu key).
          if (
            event.key === "ContextMenu" ||
            (event.shiftKey && event.key === "F10")
          ) {
            event.preventDefault();
            event.stopPropagation();
            events.onOpenClipMenuFromKey(event.currentTarget, clipId);
            return;
          }
          events.onClipKeyDown(event, clipId);
        }}
        onClick={(event) =>
          events.onClipClick(
            clipId,
            event.shiftKey
              ? "range"
              : event.ctrlKey || event.metaKey
                ? "toggle"
                : "replace",
          )
        }
        onDoubleClick={(event) => {
          if (
            event.altKey ||
            event.ctrlKey ||
            event.metaKey ||
            event.shiftKey ||
            event.button !== 0
          )
            return;
          const offset = Math.max(
            0,
            event.clientX - event.currentTarget.getBoundingClientRect().left,
          );
          const target = Math.max(
            clipStartFrame,
            Math.min(
              clipEndFrame - 1,
              clipStartFrame + Math.floor(offset / pixelsPerFrame),
            ),
          );
          events.onClipDoubleClick(target);
        }}
      >
        {affordances.label ? (
          <span className="h3-nle-clip-label">{name}</span>
        ) : null}
        {affordances.duration ? (
          <small className="h3-nle-clip-duration">
            {formatTimelineTimecode(end - start, fps)}
          </small>
        ) : null}
      </button>
      {grip("start")}
      {affordances.menu ? (
        <button
          type="button"
          className="h3-nle-clip-menu-trigger"
          data-h3-nle-menu-trigger="clip"
          aria-haspopup="menu"
          aria-label={timelineMenuLabels(locale).openClip}
          onClick={(event) => events.onOpenClipMenu(event, clipId)}
        >
          <NleActionIcon name="more" size={14} />
        </button>
      ) : null}
      {grip("end")}
    </div>
  );
});

export function NleTimelineTrackRow(props: RowProps) {
  const {
    locale,
    fps,
    track,
    trackName,
    rowIndex,
    clips,
    clipName,
    viewStart,
    pixelsPerFrame,
    coarse,
    emptyLane,
    selected,
    highlighted,
    draft,
    busy,
  } = props;
  const events = useCommittedClipEvents(props);
  // IMPORTANT: accepted decodes replace track objects even for clip-only edits.
  // Reuse only the header's explicit display fields; publish callbacks after commit.
  const headerTrack = useMemo(
    () => ({
      trackId: track.trackId,
      kind: track.kind,
      locked: track.locked,
      enabled: track.enabled,
    }),
    [track.trackId, track.kind, track.locked, track.enabled],
  );
  const text = nleCopy(locale);
  return (
    <div
      className="h3-nle-track-row"
      role="row"
      aria-rowindex={rowIndex + 1}
      data-h3-nle-track={track.trackId}
      data-kind={track.kind}
      style={{ top: `${rowIndex * NLE_ROW_HEIGHT_PX}px` }}
    >
      <NleTrackHeader
        locale={locale}
        track={headerTrack}
        name={trackName}
        onTrackCommand={events.onTrackCommand}
        onOpenTrackMenu={events.onOpenTrackMenu}
      />
      <div className="h3-nle-track-lane" role="gridcell">
        {emptyLane ? (
          // Presentation over the grid's existing bin-insert target, which owns
          // the drop; the zone takes no pointer events, so it never becomes a second target.
          <p className="h3-nle-empty-lane" data-h3-nle-empty-lane="">
            {text.timeline.emptyLane}
          </p>
        ) : null}
        {clips.map((clip) => {
          const isDraft = draft?.identity.clipId === clip.clipId;
          const start = isDraft ? draft.geometry.start : clip.startFrame;
          const end = isDraft ? draft.geometry.end : clipEnd(clip);
          const isSelected = selected.has(clip.clipId);
          // IMPORTANT: a one-frame clip can render narrower than its selection button's
          // padding and be clipped out of the hit area. Keep timing exact, but give the
          // timeline control enough visual width to remain selectable at zoom fit.
          const width = Math.max(20, (end - start) * pixelsPerFrame);
          return (
            <NleTimelineClip
              key={clip.clipId}
              locale={locale}
              fps={fps}
              clipId={clip.clipId}
              assetId={clip.assetId}
              trackKind={track.kind}
              enabled={clip.enabled}
              name={clipName(clip)}
              start={start}
              end={end}
              clipStartFrame={clip.startFrame}
              clipEndFrame={clipEnd(clip)}
              left={laneXForBoundary(start, viewStart, pixelsPerFrame)}
              width={width}
              pixelsPerFrame={pixelsPerFrame}
              coarse={coarse}
              isSelected={isSelected}
              isHighlighted={
                clip.assetId !== null && highlighted.has(clip.assetId)
              }
              draftEdge={isDraft ? draft.identity.edge : null}
              gripDisabled={track.locked || !isSelected || busy}
              events={events}
            />
          );
        })}
      </div>
    </div>
  );
}
