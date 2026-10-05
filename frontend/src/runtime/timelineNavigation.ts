import {
  DEFAULT_MIN_TIMELINE_SCALE,
  LANE_LEAD_IN_PX,
  MAX_TIMELINE_SCALE,
  clampTimelineScale,
  clampViewStart,
  formatTimelineTimecode,
  rulerMarks,
  timelineScaleBounds,
  zoomAround,
} from "./timelineGeometry";

export { formatTimelineTimecode };

export type TimelineView = Readonly<{
  pixelsPerFrame: number;
  fitLocked: boolean;
  laneWidth: number;
  startFrame: number;
  frameCount: number;
  endFrameExclusive: number;
}>;

export type TimelineViewportState = Readonly<{
  pixelsPerFrame: number;
  viewStart: number;
  fitLocked: boolean;
}>;

export const CONTENT_FIT_TRAILING_PX = 24;
export const EMPTY_FIT_SECONDS = 10;

/**
 * Fit is an authored-content operation, not an edit-capacity operation. `laneWidth` is the lane
 * width after the track header; the shared lead-in and trailing room are subtracted exactly once.
 */
export function fitTimelineContentView(
  input: Readonly<{
    laneWidth: number;
    contentEndExclusive: number;
    editCapacityFrames: number;
    fps: number;
    lastValidScale: number;
  }>,
): TimelineViewportState {
  exactExtent(input.editCapacityFrames);
  const fallbackExtent = Math.max(
    1,
    Math.min(
      input.editCapacityFrames,
      Math.round(
        (Number.isFinite(input.fps) && input.fps > 0 ? input.fps : 24) *
          EMPTY_FIT_SECONDS,
      ),
    ),
  );
  const contentExtent =
    Number.isSafeInteger(input.contentEndExclusive) &&
    input.contentEndExclusive > 0
      ? Math.min(input.contentEndExclusive, input.editCapacityFrames)
      : fallbackExtent;
  const usableWidth =
    input.laneWidth - LANE_LEAD_IN_PX - CONTENT_FIT_TRAILING_PX;
  const rawScale =
    Number.isFinite(usableWidth) && usableWidth > 0
      ? usableWidth / contentExtent
      : input.lastValidScale;
  const pixelsPerFrame = clampTimelineScale(rawScale, {
    min: DEFAULT_MIN_TIMELINE_SCALE,
    max: MAX_TIMELINE_SCALE,
  });
  return Object.freeze({ pixelsPerFrame, viewStart: 0, fitLocked: true });
}

/** One pure transition for every anchored zoom intent; callers commit the returned object once. */
export function zoomTimelineViewport(
  input: Readonly<{
    current: TimelineViewportState;
    laneWidth: number;
    durationFrames: number;
    nextScale: number;
    anchorX: number;
  }>,
): TimelineViewportState {
  const next = zoomAround({
    viewStart: input.current.viewStart,
    scale: input.current.pixelsPerFrame,
    nextScale: input.nextScale,
    anchorX: input.anchorX,
    laneWidth: input.laneWidth,
    durationFrames: input.durationFrames,
  });
  return Object.freeze({
    pixelsPerFrame: next.scale,
    viewStart: next.viewStart,
    fitLocked: false,
  });
}

function exactExtent(extentFrames: number): void {
  if (!Number.isSafeInteger(extentFrames) || extentFrames < 1)
    throw new Error("timeline extent must be a positive integer");
}

function createView(
  extentFrames: number,
  laneWidth: number,
  pixelsPerFrame: number,
  startFrame: number,
  fitLocked: boolean,
): TimelineView {
  exactExtent(extentFrames);
  const width = Number.isFinite(laneWidth) && laneWidth > 0 ? laneWidth : 512;
  const bounds = timelineScaleBounds(width, extentFrames, pixelsPerFrame);
  const scale = fitLocked
    ? bounds.fit
    : clampTimelineScale(pixelsPerFrame, bounds);
  const start = fitLocked
    ? 0
    : clampViewStart(startFrame, extentFrames, width, scale);
  const frameCount = Math.min(extentFrames, width / scale);
  return Object.freeze({
    pixelsPerFrame: scale,
    fitLocked,
    laneWidth: width,
    startFrame: start,
    frameCount,
    endFrameExclusive: Math.min(extentFrames, start + frameCount),
  });
}

export function createTimelineView(
  input: Readonly<{
    extentFrames: number;
    pixelsPerFrame: number;
    laneWidth: number;
    startFrame?: number;
    fitLocked?: boolean;
  }>,
): TimelineView {
  return createView(
    input.extentFrames,
    input.laneWidth,
    input.pixelsPerFrame,
    input.startFrame ?? 0,
    input.fitLocked === true,
  );
}

export function zoomTimelineView(
  input: Readonly<{
    view: TimelineView;
    extentFrames: number;
    pixelsPerFrame: number;
    anchorFrame: number;
  }>,
): TimelineView {
  if (
    !Number.isSafeInteger(input.anchorFrame) ||
    input.anchorFrame < 0 ||
    input.anchorFrame >= input.extentFrames
  )
    throw new Error("timeline frame is outside the accepted extent");
  const next = zoomAround({
    viewStart: input.view.startFrame,
    scale: input.view.pixelsPerFrame,
    nextScale: input.pixelsPerFrame,
    anchorX:
      (input.anchorFrame - input.view.startFrame) * input.view.pixelsPerFrame,
    laneWidth: input.view.laneWidth,
    durationFrames: input.extentFrames,
  });
  return createView(
    input.extentFrames,
    input.view.laneWidth,
    next.scale,
    next.viewStart,
    false,
  );
}

export function ensureTimelineFrameVisible(
  input: Readonly<{
    view: TimelineView;
    extentFrames: number;
    frame: number;
  }>,
): TimelineView {
  if (
    !Number.isSafeInteger(input.frame) ||
    input.frame < 0 ||
    input.frame >= input.extentFrames
  )
    throw new Error("timeline frame is outside the accepted extent");
  if (
    input.frame >= input.view.startFrame &&
    input.frame < input.view.endFrameExclusive
  )
    return input.view;
  const start =
    input.frame < input.view.startFrame
      ? input.frame
      : input.frame - input.view.frameCount + 1;
  return createView(
    input.extentFrames,
    input.view.laneWidth,
    input.view.pixelsPerFrame,
    start,
    false,
  );
}

export function panTimelineView(
  input: Readonly<{
    view: TimelineView;
    extentFrames: number;
    frameGrid: number;
    direction: -1 | 1;
  }>,
): TimelineView {
  if (!Number.isSafeInteger(input.frameGrid) || input.frameGrid < 1)
    throw new Error("timeline frame grid must be a positive integer");
  if (input.direction !== -1 && input.direction !== 1)
    throw new Error("timeline pan direction is invalid");
  const quarter = Math.ceil(input.view.frameCount / 4);
  const delta =
    Math.ceil(quarter / input.frameGrid) * input.frameGrid * input.direction;
  return createView(
    input.extentFrames,
    input.view.laneWidth,
    input.view.pixelsPerFrame,
    input.view.startFrame + delta,
    false,
  );
}

export function timelineRulerMarks(
  input: Readonly<{
    view: TimelineView;
    frameGrid: number;
    fps: number;
  }>,
): readonly Readonly<{ frame: number; timecode: string }>[] {
  if (!Number.isSafeInteger(input.frameGrid) || input.frameGrid < 1)
    throw new Error("timeline frame grid must be a positive integer");
  return rulerMarks({
    viewStart: input.view.startFrame,
    laneWidth: input.view.laneWidth,
    pixelsPerFrame: input.view.pixelsPerFrame,
    durationFrames: Math.max(1, Math.ceil(input.view.endFrameExclusive)),
    fps: input.fps,
  }).map((mark) => Object.freeze({ frame: mark.frame, timecode: mark.label }));
}

export function selectedClipNavigationFrame(
  input: Readonly<{
    clips: readonly Readonly<{
      clipId: string;
      startFrame: number;
      lane: number;
    }>[];
    selectedClipIds: readonly string[];
    currentFrame: number;
    direction: -1 | 1;
  }>,
): number | undefined {
  if (!Number.isSafeInteger(input.currentFrame) || input.currentFrame < 0)
    throw new Error("timeline frame is invalid");
  const selected = new Set(input.selectedClipIds);
  const ordered = input.clips
    .filter((clip) => selected.has(clip.clipId))
    .sort(
      (left, right) =>
        left.startFrame - right.startFrame ||
        left.lane - right.lane ||
        left.clipId.localeCompare(right.clipId),
    );
  if (input.direction === 1)
    return ordered.find((clip) => clip.startFrame > input.currentFrame)
      ?.startFrame;
  if (input.direction === -1)
    return [...ordered]
      .reverse()
      .find((clip) => clip.startFrame < input.currentFrame)?.startFrame;
  throw new Error("selected clip navigation direction is invalid");
}

export function adjacentClipId(
  clips: readonly Readonly<{
    clipId: string;
    startFrame: number;
    trackOrder: number;
  }>[],
  currentId: string | null,
  direction: -1 | 1,
): string | null {
  const ordered = [...clips].sort(
    (left, right) =>
      left.trackOrder - right.trackOrder ||
      left.startFrame - right.startFrame ||
      left.clipId.localeCompare(right.clipId),
  );
  if (ordered.length === 0) return null;
  const index =
    currentId === null
      ? -1
      : ordered.findIndex((clip) => clip.clipId === currentId);
  const next =
    index < 0 ? (direction === 1 ? 0 : ordered.length - 1) : index + direction;
  return (
    ordered[Math.min(ordered.length - 1, Math.max(0, next))]?.clipId ?? null
  );
}

export function contiguousClipSelection(
  clips: readonly Readonly<{
    clipId: string;
    startFrame: number;
    trackOrder: number;
  }>[],
  anchorId: string,
  targetId: string,
): readonly string[] {
  const ordered = [...clips].sort(
    (left, right) =>
      left.trackOrder - right.trackOrder ||
      left.startFrame - right.startFrame ||
      left.clipId.localeCompare(right.clipId),
  );
  const first = ordered.findIndex((clip) => clip.clipId === anchorId);
  const second = ordered.findIndex((clip) => clip.clipId === targetId);
  if (first < 0 || second < 0) return Object.freeze([targetId]);
  const low = Math.min(first, second);
  const high = Math.max(first, second);
  return Object.freeze(ordered.slice(low, high + 1).map((clip) => clip.clipId));
}
