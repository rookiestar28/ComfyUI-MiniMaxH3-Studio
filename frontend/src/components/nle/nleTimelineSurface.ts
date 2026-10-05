// M25-62: the pure rules of the redesigned timeline surface. Track display names replace the raw
// ids in visible text (R5), the clip edit affordances implement R7 for both pointer modes (R2),
// and the scroll bar maps the view onto a thumb within the one existing view clamp.

import type {
  CompositionClip,
  CompositionTrack,
} from "../../contracts/compositionCodec";
import { clampViewStart, maxViewStart } from "../../runtime/timelineGeometry";
import { fill } from "./nleCopy";

export type TrackNameCopy = Readonly<{
  main: string;
  overlay: string;
  text: string;
}>;

/**
 * "Main" for the primary track, "Overlay N" across video and picture overlays and "Text N" for
 * text overlays, numbered in canonical `order`. The id stays in `data-h3-nle-track` only.
 */
export function trackDisplayNames(
  tracks: readonly CompositionTrack[],
  copy: TrackNameCopy,
): ReadonlyMap<string, string> {
  const names = new Map<string, string>();
  let overlays = 0;
  let texts = 0;
  let primaries = 0;
  for (const track of [...tracks].sort((a, b) => a.order - b.order)) {
    if (track.kind === "primary_video") {
      primaries += 1;
      // One primary track is the admitted shape; a second would still get a distinct name.
      names.set(
        track.trackId,
        primaries === 1 ? copy.main : `${copy.main} ${primaries}`,
      );
    } else if (track.kind === "text_overlay") {
      texts += 1;
      names.set(track.trackId, fill(copy.text, { n: texts }));
    } else {
      overlays += 1;
      names.set(track.trackId, fill(copy.overlay, { n: overlays }));
    }
  }
  return names;
}

/**
 * R5: each media asset's bin card name ("Clip 01"), numbered over the non-font assets in snapshot
 * order. M25-64: the timeline and the inspector's clip header both name a clip from this one map,
 * so the two never disagree.
 */
export function assetDisplayNames(
  assets: readonly Readonly<{ assetId: string; kind: string }>[],
  cardTemplate: string,
): ReadonlyMap<string, string> {
  const names = new Map<string, string>();
  assets
    .filter((asset) => asset.kind !== "font")
    .forEach((asset, index) =>
      names.set(
        asset.assetId,
        fill(cardTemplate, { ordinal: String(index + 1).padStart(2, "0") }),
      ),
    );
  return names;
}

/** A clip's display name: its asset's card name, or the title-clip name for text. */
export function clipDisplayName(
  clip: Pick<CompositionClip, "assetId">,
  assetNames: ReadonlyMap<string, string>,
  copy: Readonly<{ cardTemplate: string; titleClip: string }>,
): string {
  return clip.assetId === null
    ? copy.titleClip
    : (assetNames.get(clip.assetId) ??
        fill(copy.cardTemplate, { ordinal: "01" }));
}

/** R7: a fine-pointer clip shows both compact grips from this width. */
export const FINE_GRIPS_MIN_CLIP_PX = 24;
/** R7 and R2: the touch-safe rule a coarse pointer keeps (44 px grips). */
export const COARSE_GRIPS_MIN_CLIP_PX = 144;
/**
 * B-M2562-03: the 24 px menu trigger sits 10 px from the end edge, so its left edge is w - 34.
 * It shows only where that edge stays at least 12 px right of the body centre (w / 2 + 12 <=
 * w - 34, so w >= 92), rounded to the duration threshold. A drag that starts at a clip's centre
 * must never land on the trigger; at 48 px it did, and a group drag never began.
 */
export const FINE_MENU_MIN_CLIP_PX = 96;
/** The trigger's left edge, measured from the clip's start edge, at width `w`. */
export const FINE_MENU_TRIGGER_INSET_PX = 34;
/** Half of the grab zone kept free around the body centre. */
export const CLIP_CENTRE_GRAB_HALF_PX = 12;
export const CLIP_LABEL_MIN_PX = 40;
export const CLIP_DURATION_MIN_PX = 96;

export type ClipEditAffordances = Readonly<{
  grips: "both" | "start" | "end" | "none";
  rail: boolean;
  menu: boolean;
  label: boolean;
  duration: boolean;
}>;

/**
 * R7, one formula per pointer mode. Hit areas never leave the clip: the fine grips are 8 px
 * inside each edge, so a neighbour's pixels stay the neighbour's target. Below 24 px one grip
 * remains at the edge a draft is on (the end edge otherwise), and the rail offers both edges and
 * the clip menu at full target size.
 */
export function clipEditAffordances(
  input: Readonly<{
    width: number;
    selected: boolean;
    coarse: boolean;
    draftEdge: "start" | "end" | null;
  }>,
): ClipEditAffordances {
  const label = input.width >= CLIP_LABEL_MIN_PX;
  const duration = input.width >= CLIP_DURATION_MIN_PX;
  if (!input.selected)
    return { grips: "none", rail: false, menu: false, label, duration };
  if (input.coarse)
    return input.width >= COARSE_GRIPS_MIN_CLIP_PX
      ? { grips: "both", rail: false, menu: true, label, duration }
      : { grips: "none", rail: true, menu: false, label, duration };
  if (input.width >= FINE_GRIPS_MIN_CLIP_PX)
    return {
      grips: "both",
      rail: false,
      menu: input.width >= FINE_MENU_MIN_CLIP_PX,
      label,
      duration,
    };
  return {
    grips: input.draftEdge ?? "end",
    rail: true,
    menu: false,
    label,
    duration,
  };
}

export const SCROLL_THUMB_MIN_PX = 24;

export type ScrollBarInput = Readonly<{
  viewStart: number;
  durationFrames: number;
  /** The frame-mapped lane width, right of the lead-in. */
  laneWidth: number;
  pixelsPerFrame: number;
  /** The bar's own width. */
  trackWidth: number;
}>;

export type ScrollBarGeometry = Readonly<{
  visible: boolean;
  thumbX: number;
  thumbWidth: number;
}>;

// Fit maps the whole extent into the lane; float division can leave a sub-frame remainder that
// must not show a bar.
const EPSILON_FRAMES = 1e-6;

export function scrollBarGeometry(input: ScrollBarInput): ScrollBarGeometry {
  const high = maxViewStart(
    input.durationFrames,
    input.laneWidth,
    input.pixelsPerFrame,
  );
  if (high <= EPSILON_FRAMES || input.trackWidth <= 0)
    return { visible: false, thumbX: 0, thumbWidth: input.trackWidth };
  const visibleFrames = input.durationFrames - high;
  const thumbWidth = Math.min(
    input.trackWidth,
    Math.max(
      SCROLL_THUMB_MIN_PX,
      (input.trackWidth * visibleFrames) / input.durationFrames,
    ),
  );
  const start = clampViewStart(
    input.viewStart,
    input.durationFrames,
    input.laneWidth,
    input.pixelsPerFrame,
  );
  return {
    visible: true,
    thumbX: ((input.trackWidth - thumbWidth) * start) / high,
    thumbWidth,
  };
}

/** The view start a thumb at `thumbX` shows, through the same clamp every navigation uses. */
export function viewStartForScrollThumb(
  thumbX: number,
  geometry: ScrollBarGeometry,
  input: Omit<ScrollBarInput, "viewStart">,
): number {
  const travel = input.trackWidth - geometry.thumbWidth;
  const high = maxViewStart(
    input.durationFrames,
    input.laneWidth,
    input.pixelsPerFrame,
  );
  const ratio = travel <= 0 ? 0 : Math.min(1, Math.max(0, thumbX / travel));
  return clampViewStart(
    ratio * high,
    input.durationFrames,
    input.laneWidth,
    input.pixelsPerFrame,
  );
}

export type MoveRefusalCopy = Readonly<Record<string, string>> &
  Readonly<{ other: string }>;

/**
 * B-M2562-06: the words for a move refusal. The code (`group_limit`, `locked_track`, ...) stays in
 * `data-code` for tests and diagnostics and never reaches visible or announced text; an unknown
 * code reads as the general sentence rather than as itself.
 */
export function moveRefusalText(
  copy: MoveRefusalCopy,
  reason: string | null,
): string {
  if (reason === null || reason === "other") return copy.other;
  return Object.prototype.hasOwnProperty.call(copy, reason)
    ? copy[reason]!
    : copy.other;
}
