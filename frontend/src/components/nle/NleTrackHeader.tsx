// M25-62: the track header. Under a fine pointer it is 112 px in two lines -- kind icon and
// display name, then the lock and eye toggles (deviation D-2). Under a coarse pointer it keeps
// today's 160 px on one line, so the trigger and both toggles are disjoint 44 px targets
// (DD-8, B-M2562-01; `trackHeaderWidthPx`).
//
// IMPORTANT: the menu trigger and both toggles stay direct children of `.h3-nle-track-header`.
// The two lines are CSS grid areas, not wrappers: the command recipes find the toggles through the
// trigger's parent, and the raw track id lives only in the row's `data-h3-nle-track`.

import type { MouseEvent as ReactMouseEvent } from "react";

import type { CompositionTrack } from "../../contracts/compositionCodec";
import type { TimelineCommandWire } from "../../contracts/authoringWorkbenchCodec";
import type { Locale } from "../../i18n/catalog";
import { NleActionIcon, type NleIconName } from "./NleIconActions";
import { build } from "./nleCommandBuilders";
import { fill, nleCopy } from "./nleCopy";
import { timelineMenuLabels } from "./NleTimelineMenus";

const KIND_ICON: Record<CompositionTrack["kind"], NleIconName> = {
  primary_video: "trackVideo",
  video_overlay: "trackVideo",
  image_overlay: "trackImage",
  text_overlay: "text",
};

export function NleTrackHeader({
  locale,
  track,
  name,
  onTrackCommand,
  onOpenTrackMenu,
}: {
  locale: Locale;
  track: CompositionTrack;
  name: string;
  onTrackCommand(command: TimelineCommandWire): void;
  onOpenTrackMenu(
    event: ReactMouseEvent<HTMLElement>,
    trackId: string | null,
  ): void;
}) {
  const text = nleCopy(locale).timeline;
  return (
    <div
      className="h3-nle-track-header"
      role="rowheader"
      data-kind={track.kind}
      data-locked={track.locked ? "true" : "false"}
      data-enabled={track.enabled ? "true" : "false"}
      onContextMenu={(event) => onOpenTrackMenu(event, track.trackId)}
    >
      <button
        type="button"
        className="h3-nle-track-menu-trigger"
        data-h3-nle-menu-trigger="track"
        aria-haspopup="menu"
        aria-label={`${timelineMenuLabels(locale).openTrack}: ${name}`}
        title={name}
        onClick={(event) => onOpenTrackMenu(event, track.trackId)}
      >
        <NleActionIcon name={KIND_ICON[track.kind]} size={14} />
        <span className="h3-nle-track-name">{name}</span>
      </button>
      <button
        type="button"
        className="h3-nle-track-toggle"
        data-h3-nle-control="track.locked"
        aria-pressed={track.locked}
        aria-label={fill(text.trackToggles.lock, { name })}
        title={track.locked ? text.trackActions.unlock : text.trackActions.lock}
        onClick={() =>
          onTrackCommand(build.setTrackLocked(track.trackId, !track.locked))
        }
      >
        <NleActionIcon name={track.locked ? "lock" : "unlock"} size={14} />
      </button>
      <button
        type="button"
        className="h3-nle-track-toggle"
        data-h3-nle-control="track.enabled"
        aria-pressed={track.enabled}
        aria-label={fill(text.trackToggles.show, { name })}
        title={
          track.enabled ? text.trackActions.disable : text.trackActions.enable
        }
        onClick={() =>
          onTrackCommand(build.setTrackEnabled(track.trackId, !track.enabled))
        }
      >
        <NleActionIcon name={track.enabled ? "eye" : "eyeOff"} size={14} />
      </button>
    </div>
  );
}
