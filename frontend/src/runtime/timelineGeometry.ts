// M25-46: the one continuous frame/pixel authority shared by timeline rendering, gestures,
// navigation and the decoration raster. Frames address presented samples [0, duration), while
// boundaries address edit edges [0, duration]; keeping the domains explicit prevents end trims
// and ruler seeks from disagreeing at the composition boundary.

// M25-62 (R3, D-2): the two-line 112 px header of the redesign.
export const TRACK_HEADER_WIDTH_PX = 112;
/**
 * M25-62 (R2, B-M2562-01): a coarse pointer keeps today's touch-safe 160 px header. Its menu
 * trigger and both toggles are 44 px targets side by side, and three of them cannot fit in 112 px
 * without the toggles covering the trigger.
 *
 * IMPORTANT: a timeline that switches header width must pass the same width to
 * `timelineXForBoundary`, `contentXFromClientX` and `laneXFromClientX`, and publish it in
 * `--h3-nle-track-header-width` and `data-h3-nle-lane-origin-px`; the defaults are the fine width.
 */
export const COARSE_TRACK_HEADER_WIDTH_PX = 160;
export function trackHeaderWidthPx(coarse: boolean): number {
  return coarse ? COARSE_TRACK_HEADER_WIDTH_PX : TRACK_HEADER_WIDTH_PX;
}
/**
 * M25-61: an empty inset between the track header and frame 0. Without it the first clip's edge,
 * the frame-0 playhead and the header edge share one pixel column, and the playhead disappears
 * under the clip (review UX-09).
 *
 * IMPORTANT: every frame<->pixel conversion in the timeline goes through `laneXForBoundary`,
 * `timelineXForBoundary` or `contentXFromClientX`. A conversion that subtracts
 * `TRACK_HEADER_WIDTH_PX` alone lands this many pixels early, which misaligns clicks, drops and
 * the playhead against the clips drawn by the other consumers.
 */
export const LANE_LEAD_IN_PX = 16;
/** Pixel offset of frame 0 at `viewStart` 0 from the timeline's left edge. */
export const LANE_ORIGIN_PX = TRACK_HEADER_WIDTH_PX + LANE_LEAD_IN_PX;
export const DEFAULT_TIMELINE_SCALE = 1;
export const DEFAULT_MIN_TIMELINE_SCALE = 0.25;
export const MAX_TIMELINE_SCALE = 16;
export const RULER_MIN_SPACING_PX = 72;
export const RULER_MIN_MINOR_SPACING_PX = 6;

function positive(value: number, fallback: number): number {
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function exactExtent(extentFrames: number): void {
  if (!Number.isSafeInteger(extentFrames) || extentFrames < 1)
    throw new Error("timeline extent is invalid");
}

function exactPlayableFrame(frame: number, extentFrames: number): void {
  exactExtent(extentFrames);
  if (!Number.isSafeInteger(frame) || frame < 0 || frame >= extentFrames)
    throw new Error("timeline frame is outside the accepted extent");
}

export function timelineFrameFromClientX(
  input: Readonly<{
    clientX: number;
    startX: number;
    endX: number;
    extentFrames: number;
    currentFrame: number;
  }>,
): number {
  exactPlayableFrame(input.currentFrame, input.extentFrames);
  if (
    !Number.isFinite(input.clientX) ||
    !Number.isFinite(input.startX) ||
    !Number.isFinite(input.endX) ||
    input.startX === input.endX
  )
    return input.currentFrame;
  const left = Math.min(input.startX, input.endX);
  const right = Math.max(input.startX, input.endX);
  const ratio = Math.min(
    1,
    Math.max(0, (input.clientX - left) / (right - left)),
  );
  return Math.min(
    input.extentFrames - 1,
    Math.max(0, Math.floor(ratio * (input.extentFrames - 1) + 0.5)),
  );
}

export function timelineFramePercent(
  frame: number,
  extentFrames: number,
): number {
  exactPlayableFrame(frame, extentFrames);
  return extentFrames === 1 ? 0 : (frame / (extentFrames - 1)) * 100;
}

export function stepTimelineFrame(
  input: Readonly<{
    frame: number;
    key: string;
    shiftKey: boolean;
    frameGrid: number;
    extentFrames: number;
  }>,
): number | undefined {
  exactPlayableFrame(input.frame, input.extentFrames);
  if (!Number.isSafeInteger(input.frameGrid) || input.frameGrid < 1)
    throw new Error("timeline frame grid is invalid");
  if (input.key === "Home") return 0;
  if (input.key === "End") return input.extentFrames - 1;
  if (input.key !== "ArrowLeft" && input.key !== "ArrowRight") return undefined;
  const delta =
    (input.key === "ArrowLeft" ? -1 : 1) *
    (input.shiftKey ? input.frameGrid : 1);
  return Math.min(input.extentFrames - 1, Math.max(0, input.frame + delta));
}

export function quantizeSignedDelta(value: number): number | null {
  if (!Number.isFinite(value)) return null;
  const result = Math.sign(value) * Math.floor(Math.abs(value) + 0.5);
  return Object.is(result, -0) ? 0 : result;
}

export function fitTimelineScale(
  laneWidth: number,
  durationFrames: number,
  lastValid: number,
): number {
  if (!Number.isFinite(laneWidth) || laneWidth <= 0)
    return positive(lastValid, DEFAULT_TIMELINE_SCALE);
  if (!Number.isSafeInteger(durationFrames) || durationFrames < 1)
    return positive(lastValid, DEFAULT_TIMELINE_SCALE);
  return laneWidth / durationFrames;
}

export function timelineScaleBounds(
  laneWidth: number,
  durationFrames: number,
  lastValid: number,
): Readonly<{ min: number; max: number; fit: number }> {
  const fit = fitTimelineScale(laneWidth, durationFrames, lastValid);
  return Object.freeze({
    min: Math.min(DEFAULT_MIN_TIMELINE_SCALE, fit),
    max: MAX_TIMELINE_SCALE,
    fit,
  });
}

export function clampTimelineScale(
  scale: number,
  bounds: Readonly<{ min: number; max: number }>,
): number {
  return Math.min(
    bounds.max,
    Math.max(bounds.min, positive(scale, bounds.min)),
  );
}

export function maxViewStart(
  durationFrames: number,
  laneWidth: number,
  pixelsPerFrame: number,
): number {
  if (!Number.isSafeInteger(durationFrames) || durationFrames < 1) return 0;
  const visible = Math.max(0, laneWidth) / positive(pixelsPerFrame, 1);
  return Math.max(0, durationFrames - visible);
}

export function clampViewStart(
  viewStart: number,
  durationFrames: number,
  laneWidth: number,
  pixelsPerFrame: number,
): number {
  const high = maxViewStart(durationFrames, laneWidth, pixelsPerFrame);
  return Math.min(
    high,
    Math.max(0, Number.isFinite(viewStart) ? viewStart : 0),
  );
}

export function frameBoundaryToX(
  boundaryFrame: number,
  viewStart: number,
  pixelsPerFrame: number,
): number {
  if (!Number.isFinite(boundaryFrame) || !Number.isFinite(viewStart))
    throw new Error("timeline boundary is invalid");
  return (boundaryFrame - viewStart) * positive(pixelsPerFrame, 1);
}

/** Offset of a boundary from the lane's left edge (the header edge), lead-in included. */
export function laneXForBoundary(
  boundaryFrame: number,
  viewStart: number,
  pixelsPerFrame: number,
): number {
  return (
    LANE_LEAD_IN_PX + frameBoundaryToX(boundaryFrame, viewStart, pixelsPerFrame)
  );
}

/** Offset of a boundary from the timeline's left edge, header and lead-in included. */
export function timelineXForBoundary(
  boundaryFrame: number,
  viewStart: number,
  pixelsPerFrame: number,
  headerWidth = TRACK_HEADER_WIDTH_PX,
): number {
  return (
    headerWidth +
    LANE_LEAD_IN_PX +
    frameBoundaryToX(boundaryFrame, viewStart, pixelsPerFrame)
  );
}

/**
 * The frame-origin x (the coordinate `frameFromX`/`boundaryFromX` take) of a client x, given the
 * left edge of an element that starts at the timeline's left edge (the ruler or the track grid).
 */
export function contentXFromClientX(
  clientX: number,
  timelineLeft: number,
  headerWidth = TRACK_HEADER_WIDTH_PX,
): number {
  return clientX - timelineLeft - headerWidth - LANE_LEAD_IN_PX;
}

/**
 * The lane x (the decoration canvas space, where frame 0 sits at `LANE_LEAD_IN_PX`) of a client x,
 * given the left edge of an element that starts at the timeline's left edge.
 */
export function laneXFromClientX(
  clientX: number,
  timelineLeft: number,
  headerWidth = TRACK_HEADER_WIDTH_PX,
): number {
  return clientX - timelineLeft - headerWidth;
}

export function frameFromX(
  x: number,
  viewStart: number,
  pixelsPerFrame: number,
  durationFrames: number,
): number {
  if (!Number.isSafeInteger(durationFrames) || durationFrames < 1)
    throw new Error("timeline duration is invalid");
  const raw = viewStart + x / positive(pixelsPerFrame, 1);
  return Math.min(durationFrames - 1, Math.max(0, Math.floor(raw + 0.5)));
}

export function boundaryFromX(
  x: number,
  viewStart: number,
  pixelsPerFrame: number,
  durationFrames: number,
): number {
  if (!Number.isSafeInteger(durationFrames) || durationFrames < 1)
    throw new Error("timeline duration is invalid");
  const raw = viewStart + x / positive(pixelsPerFrame, 1);
  return Math.min(durationFrames, Math.max(0, Math.floor(raw + 0.5)));
}

export function proposalFromCapturedOrigin(
  originFrame: number,
  displacementPx: number,
  pixelsPerFrame: number,
): number {
  if (!Number.isSafeInteger(originFrame))
    throw new Error("timeline origin is invalid");
  return (
    originFrame +
    (quantizeSignedDelta(displacementPx / positive(pixelsPerFrame, 1)) ?? 0)
  );
}

export function zoomAround(
  input: Readonly<{
    viewStart: number;
    scale: number;
    nextScale: number;
    anchorX: number;
    laneWidth: number;
    durationFrames: number;
  }>,
): Readonly<{ scale: number; viewStart: number }> {
  const bounds = timelineScaleBounds(
    input.laneWidth,
    input.durationFrames,
    input.scale,
  );
  const nextScale = clampTimelineScale(input.nextScale, bounds);
  const anchorFrame =
    input.viewStart + input.anchorX / positive(input.scale, bounds.min);
  const requestedStart = anchorFrame - input.anchorX / nextScale;
  return Object.freeze({
    scale: nextScale,
    viewStart: clampViewStart(
      requestedStart,
      input.durationFrames,
      input.laneWidth,
      nextScale,
    ),
  });
}

export function zoomToSelection(
  input: Readonly<{
    startFrame: number;
    endFrame: number;
    laneWidth: number;
    durationFrames: number;
    minScale?: number;
    maxScale?: number;
  }>,
): Readonly<{ scale: number; viewStart: number }> {
  const span = Math.max(1, input.endFrame - input.startFrame);
  const bounds = timelineScaleBounds(input.laneWidth, input.durationFrames, 1);
  const min = input.minScale ?? bounds.min;
  const max = input.maxScale ?? MAX_TIMELINE_SCALE;
  const scale = Math.min(max, Math.max(min, (input.laneWidth * 0.6) / span));
  const midpoint = (input.startFrame + input.endFrame) / 2;
  return Object.freeze({
    scale,
    viewStart: clampViewStart(
      midpoint - input.laneWidth / scale / 2,
      input.durationFrames,
      input.laneWidth,
      scale,
    ),
  });
}

export function formatTimelineTimecode(frame: number, fps: number): string {
  if (!Number.isSafeInteger(frame) || frame < 0)
    throw new Error("timeline frame is invalid");
  const rate = positive(fps, 1);
  const wholeRate = Math.max(1, Math.round(rate));
  const framePart = frame % wholeRate;
  const secondsTotal = Math.floor(frame / rate);
  const seconds = secondsTotal % 60;
  const minutesTotal = Math.floor(secondsTotal / 60);
  const minutes = minutesTotal % 60;
  const hours = Math.floor(minutesTotal / 60);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}:${pad(framePart)}`;
}

// IMPORTANT (B-M2562-08): strides count the project's own frame rate. Below one second a stride
// divides the whole frame rate, so every whole second is a labelled mark and the `NNf` labels
// restart at each second, as the approved canvas draws them; above it the stride is whole seconds.
// A fixed list of 24 fps frame counts (1, 2, 5, 10, 12, 24, 48, ...) marked 5 and 10 frames from
// frame 0: at 8 and 16 px per frame no second was labelled, and around Fit (12, 24, 48 frames)
// none was at 25 or 30 fps.
const SECOND_STRIDES = Object.freeze([
  1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1_800, 3_600,
]);

type RulerInput = Readonly<{
  viewStart: number;
  laneWidth: number;
  pixelsPerFrame: number;
  durationFrames: number;
  fps: number;
}>;

function rulerStride(scale: number, fps: number): number {
  const rate = Math.max(1, Math.round(positive(fps, 1)));
  const minimumFrames = RULER_MIN_SPACING_PX / scale;
  if (minimumFrames <= rate)
    for (let frames = Math.max(1, Math.ceil(minimumFrames)); ; frames += 1)
      if (rate % frames === 0) return frames;
  const seconds = SECOND_STRIDES.find((value) => value * rate >= minimumFrames);
  return (seconds ?? Math.ceil(minimumFrames / rate / 3_600) * 3_600) * rate;
}

/** Major marks: the ruler's labelled ticks. `x` is frame-origin relative, like `frameBoundaryToX`. */
export function rulerMarks(
  input: RulerInput,
): readonly Readonly<{ frame: number; x: number; label: string }>[] {
  const scale = positive(input.pixelsPerFrame, 1);
  const stride = rulerStride(scale, input.fps);
  const end = Math.min(
    input.durationFrames,
    input.viewStart + Math.max(0, input.laneWidth) / scale,
  );
  const first = Math.max(0, Math.ceil(input.viewStart / stride) * stride);
  const marks: Array<Readonly<{ frame: number; x: number; label: string }>> =
    [];
  const limit = Math.min(
    512,
    Math.ceil(Math.max(0, input.laneWidth) / RULER_MIN_SPACING_PX) + 2,
  );
  for (let frame = first; frame <= end && marks.length < limit; frame += stride)
    marks.push(
      Object.freeze({
        frame,
        x: frameBoundaryToX(frame, input.viewStart, scale),
        label: formatTimelineTimecode(frame, input.fps),
      }),
    );
  return Object.freeze(marks);
}

// Largest first: the finest subdivision whose ticks stay at least 6 px apart wins.
const MINOR_DIVISIONS = Object.freeze([12, 6, 5, 4, 2]);

/**
 * M25-61: unlabelled minor ticks between the major marks, drawn in the ruler only. The major
 * stride is divided by the largest of 12, 6, 5, 4 or 2 that divides it evenly while keeping the
 * ticks at least `RULER_MIN_MINOR_SPACING_PX` apart; at 6 px per frame and above this reaches
 * every frame. `x` is frame-origin relative, like `rulerMarks`.
 */
export function rulerMinorTicks(
  input: RulerInput,
): readonly Readonly<{ frame: number; x: number }>[] {
  const scale = positive(input.pixelsPerFrame, 1);
  const stride = rulerStride(scale, input.fps);
  const division = MINOR_DIVISIONS.find(
    (count) =>
      stride % count === 0 &&
      (stride / count) * scale >= RULER_MIN_MINOR_SPACING_PX,
  );
  if (division === undefined) return Object.freeze([]);
  const step = stride / division;
  const end = Math.min(
    input.durationFrames,
    input.viewStart + Math.max(0, input.laneWidth) / scale,
  );
  const first = Math.max(0, Math.ceil(input.viewStart / step) * step);
  const limit = Math.min(
    4_096,
    Math.ceil(Math.max(0, input.laneWidth) / RULER_MIN_MINOR_SPACING_PX) + 2,
  );
  const ticks: Array<Readonly<{ frame: number; x: number }>> = [];
  for (let frame = first; frame <= end && ticks.length < limit; frame += step)
    if (frame % stride !== 0)
      ticks.push(
        Object.freeze({
          frame,
          x: frameBoundaryToX(frame, input.viewStart, scale),
        }),
      );
  return Object.freeze(ticks);
}

/**
 * M25-61: the ruler's short label. A whole second reads `MM:SS` (`H:MM:SS` from one hour); a mark
 * inside a second reads the frame within that second, `NNf`. The full `HH:MM:SS:FF` timecode stays
 * with `formatTimelineTimecode` for the transport and the ruler's accessible value.
 */
export function formatRulerLabel(frame: number, fps: number): string {
  if (!Number.isSafeInteger(frame) || frame < 0)
    throw new Error("timeline frame is invalid");
  const rate = positive(fps, 1);
  const wholeRate = Math.max(1, Math.round(rate));
  const pad = (value: number) => String(value).padStart(2, "0");
  const framePart = frame % wholeRate;
  if (framePart !== 0) return `${pad(framePart)}f`;
  const secondsTotal = Math.floor(frame / rate);
  const seconds = secondsTotal % 60;
  const minutesTotal = Math.floor(secondsTotal / 60);
  const minutes = minutesTotal % 60;
  const hours = Math.floor(minutesTotal / 60);
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(seconds)}`
    : `${pad(minutes)}:${pad(seconds)}`;
}
