// M25-16 `NLE-EDGE-TRIM-V1`: pure edge-trim geometry and the local gesture state machine.
//
// The gesture is a view concern layered over the accepted `trim_clip` command. Nothing here
// owns a new API, persisted schema, source lease, media dependency, worker or storage key. A
// draft is content-free and bounded to one selected clip and one pointer; the backend still
// decides admission (source PTS representability, locks, transitions), so a geometrically
// legal draft is only ever *provisionally* valid.

export type TrimEdge = "start" | "end";

/**
 * Quantize a signed output-frame displacement with half ties away from zero:
 * `sign(d) * floor(|d| + 0.5)`, normalizing negative zero. Non-finite input yields `null`.
 */
export function quantizeSignedDelta(delta: number): number | null {
  if (!Number.isFinite(delta)) return null;
  const magnitude = Math.floor(Math.abs(delta) + 0.5);
  const signed = Math.sign(delta) * magnitude;
  return Object.is(signed, -0) ? 0 : signed;
}

/** Total pointer displacement in pixels -> exact signed output frames, or `null` when unmapped. */
export function frameDeltaFromDisplacement(
  displacementPx: number,
  pixelsPerFrame: number,
): number | null {
  if (
    !Number.isFinite(displacementPx) ||
    !Number.isFinite(pixelsPerFrame) ||
    pixelsPerFrame <= 0
  )
    return null;
  return displacementPx / pixelsPerFrame;
}

export type TrimGeometryRequest = Readonly<{
  edge: TrimEdge;
  startFrame: number;
  endFrame: number; // exclusive
  deltaFrames: number;
  outputDurationFrames: number;
}>;

export type TrimGeometry = Readonly<{
  edge: TrimEdge;
  requestedStart: number;
  requestedEnd: number;
  start: number;
  end: number;
  deltaFrames: number;
  clamped: boolean;
  noop: boolean;
}>;

/**
 * Map a signed delta onto the half-open interval `[s, e)`: a start trim requests `[s+d, e)`,
 * an end trim `[s, e+d)`. The result keeps at least one frame and stays inside
 * `[0, outputDurationFrames]`; the exclusive end may equal the output duration.
 */
export function trimGeometry(request: TrimGeometryRequest): TrimGeometry {
  const { edge, startFrame, endFrame, deltaFrames, outputDurationFrames } =
    request;
  if (
    !Number.isSafeInteger(startFrame) ||
    !Number.isSafeInteger(endFrame) ||
    !Number.isSafeInteger(deltaFrames) ||
    !Number.isSafeInteger(outputDurationFrames) ||
    startFrame < 0 ||
    endFrame <= startFrame ||
    endFrame > outputDurationFrames
  )
    throw new Error("trim geometry requires an exact frame interval");
  if (edge === "start") {
    const requestedStart = startFrame + deltaFrames;
    const start = Math.min(endFrame - 1, Math.max(0, requestedStart));
    return Object.freeze({
      edge,
      requestedStart,
      requestedEnd: endFrame,
      start,
      end: endFrame,
      deltaFrames: start - startFrame,
      clamped: start !== requestedStart,
      noop: start === startFrame,
    });
  }
  const requestedEnd = endFrame + deltaFrames;
  const end = Math.max(
    startFrame + 1,
    Math.min(outputDurationFrames, requestedEnd),
  );
  return Object.freeze({
    edge,
    requestedStart: startFrame,
    requestedEnd,
    start: startFrame,
    end,
    deltaFrames: end - endFrame,
    clamped: end !== requestedEnd,
    noop: end === endFrame,
  });
}

export const SNAP_THRESHOLD_PX = 8;
export const SNAP_MAX_CANDIDATES = 32;

export type SnapTargetKind = "boundary" | "grid" | "playhead";
export type SnapTarget = Readonly<{ kind: SnapTargetKind; frame: number }>;
export type SnapCandidateClip = Readonly<{
  clipId: string;
  startFrame: number;
  endFrame: number;
}>;

/**
 * Advisory magnetic snapping for an unquantized boundary position. Candidates are other
 * accepted clip boundaries, the output frame grid and the accepted playhead within
 * `SNAP_THRESHOLD_PX`; rank by distance, then boundary before grid before playhead, then the
 * lower frame; keep at most `SNAP_MAX_CANDIDATES`. This never consults private source facts and
 * never stands in for backend admission.
 */
export function snapTrimBoundary(input: {
  rawBoundaryFrame: number;
  pixelsPerFrame: number;
  clips: readonly SnapCandidateClip[];
  targetClipId: string;
  playheadFrame: number | null;
  outputDurationFrames: number;
}): SnapTarget | null {
  const { rawBoundaryFrame, pixelsPerFrame } = input;
  if (
    !Number.isFinite(rawBoundaryFrame) ||
    !Number.isFinite(pixelsPerFrame) ||
    pixelsPerFrame <= 0
  )
    return null;
  const thresholdFrames = SNAP_THRESHOLD_PX / pixelsPerFrame;
  const rank: Record<SnapTargetKind, number> = {
    boundary: 0,
    grid: 1,
    playhead: 2,
  };
  const candidates: SnapTarget[] = [];
  const push = (kind: SnapTargetKind, frame: number) => {
    if (
      !Number.isSafeInteger(frame) ||
      frame < 0 ||
      frame > input.outputDurationFrames ||
      Math.abs(frame - rawBoundaryFrame) > thresholdFrames
    )
      return;
    candidates.push(Object.freeze({ kind, frame }));
  };
  for (const clip of input.clips) {
    if (clip.clipId === input.targetClipId) continue;
    push("boundary", clip.startFrame);
    push("boundary", clip.endFrame);
  }
  push("grid", Math.floor(rawBoundaryFrame));
  push("grid", Math.ceil(rawBoundaryFrame));
  if (input.playheadFrame !== null) push("playhead", input.playheadFrame);
  candidates.sort((left, right) => {
    const distance =
      Math.abs(left.frame - rawBoundaryFrame) -
      Math.abs(right.frame - rawBoundaryFrame);
    if (distance !== 0) return distance;
    const kind = rank[left.kind] - rank[right.kind];
    if (kind !== 0) return kind;
    return left.frame - right.frame;
  });
  const bounded = candidates.slice(0, SNAP_MAX_CANDIDATES);
  return bounded[0] ?? null;
}

// ---------------------------------------------------------------------------------------------
// Local gesture state machine.
//
// idle -> dragging | keyboard_draft -> submitting -> accepted | rejected | reconciling
// Pre-submit cancellation returns to idle. `accepted` and `rejected` reset to idle after a
// notice period or when the next gesture begins; `reconciling` returns to idle only once
// current state is re-read. These are view states, not persisted timeline states.

export type TrimGestureIdentity = Readonly<{
  clipId: string;
  workspaceHandle: string;
  edge: TrimEdge;
  workspaceRevision: number;
  timelineRevision: number;
  timelineFingerprint: string;
  authoringFingerprint?: string;
  /** Signature of zoom/scroll/viewport at capture; a change cancels the draft. */
  mappingKey: string;
}>;

export type TrimGestureDraft = Readonly<{
  identity: TrimGestureIdentity;
  input: "pointer" | "keyboard";
  pointerId: number | null;
  originClientX: number;
  pixelsPerFrame: number;
  originStart: number;
  originEnd: number;
  outputDurationFrames: number;
  rawDeltaFrames: number;
  geometry: TrimGeometry;
  snap: SnapTarget | null;
}>;

export type TrimGestureState =
  | Readonly<{ phase: "idle" }>
  | Readonly<{ phase: "dragging" | "keyboard_draft"; draft: TrimGestureDraft }>
  | Readonly<{
      phase: "submitting";
      draft: TrimGestureDraft;
      requestId: string;
    }>
  | Readonly<{
      phase: "accepted" | "rejected" | "reconciling";
      draft: TrimGestureDraft;
      requestId: string;
      reason: string | null;
    }>;

export type TrimGestureEvent =
  | Readonly<{
      type: "begin";
      input: "pointer" | "keyboard";
      identity: TrimGestureIdentity;
      pointerId: number | null;
      originClientX: number;
      pixelsPerFrame: number;
      originStart: number;
      originEnd: number;
      outputDurationFrames: number;
    }>
  | Readonly<{
      type: "move";
      clientX: number;
      snap: (rawBoundaryFrame: number) => SnapTarget | null;
    }>
  | Readonly<{ type: "step"; deltaFrames: 1 | -1 }>
  | Readonly<{ type: "cancel"; reason: string }>
  | Readonly<{ type: "mapping_changed"; mappingKey: string }>
  | Readonly<{ type: "release"; requestId: string }>
  | Readonly<{ type: "commit"; requestId: string }>
  | Readonly<{ type: "accepted" }>
  | Readonly<{ type: "rejected"; reason: string }>
  | Readonly<{ type: "response_lost"; reason: string }>
  | Readonly<{ type: "reconciled" }>
  | Readonly<{ type: "reset" }>;

export const TRIM_GESTURE_IDLE: TrimGestureState = Object.freeze({
  phase: "idle",
});

function withDelta(
  draft: TrimGestureDraft,
  rawDeltaFrames: number,
  snap: SnapTarget | null,
): TrimGestureDraft {
  const quantized = quantizeSignedDelta(rawDeltaFrames) ?? 0;
  const boundary =
    draft.identity.edge === "start" ? draft.originStart : draft.originEnd;
  const snappedDelta = snap === null ? quantized : snap.frame - boundary;
  const geometry = trimGeometry({
    edge: draft.identity.edge,
    startFrame: draft.originStart,
    endFrame: draft.originEnd,
    deltaFrames: snappedDelta,
    outputDurationFrames: draft.outputDurationFrames,
  });
  return Object.freeze({ ...draft, rawDeltaFrames, geometry, snap });
}

/**
 * The pure reducer behind the grip controller. It never dispatches: the component reads
 * `phase` transitions and issues exactly one `trim_clip` transaction on `release`/`commit`
 * when the draft is non-noop, or zero transactions otherwise.
 */
export function reduceTrimGesture(
  state: TrimGestureState,
  event: TrimGestureEvent,
): TrimGestureState {
  switch (event.type) {
    case "begin": {
      // `accepted` and `rejected` are settled notices holding no draft: a new gesture
      // supersedes them. `reconciling` still refuses: the last request's outcome is unknown and
      // a fresh draft could replay it. `submitting` and the draft phases refuse too.
      if (
        state.phase !== "idle" &&
        state.phase !== "accepted" &&
        state.phase !== "rejected"
      )
        return state;
      if (
        !Number.isFinite(event.originClientX) ||
        !Number.isFinite(event.pixelsPerFrame) ||
        event.pixelsPerFrame <= 0
      )
        return state;
      const draft = withDelta(
        {
          identity: event.identity,
          input: event.input,
          pointerId: event.pointerId,
          originClientX: event.originClientX,
          pixelsPerFrame: event.pixelsPerFrame,
          originStart: event.originStart,
          originEnd: event.originEnd,
          outputDurationFrames: event.outputDurationFrames,
          rawDeltaFrames: 0,
          geometry: trimGeometry({
            edge: event.identity.edge,
            startFrame: event.originStart,
            endFrame: event.originEnd,
            deltaFrames: 0,
            outputDurationFrames: event.outputDurationFrames,
          }),
          snap: null,
        },
        0,
        null,
      );
      return Object.freeze({
        phase: event.input === "pointer" ? "dragging" : "keyboard_draft",
        draft,
      });
    }
    case "move": {
      if (state.phase !== "dragging") return state;
      const raw = frameDeltaFromDisplacement(
        event.clientX - state.draft.originClientX,
        state.draft.pixelsPerFrame,
      );
      // Non-finite coordinates fail without submit: the draft is cancelled, not guessed.
      if (raw === null) return TRIM_GESTURE_IDLE;
      const boundary =
        state.draft.identity.edge === "start"
          ? state.draft.originStart
          : state.draft.originEnd;
      const snap = event.snap(boundary + raw);
      return Object.freeze({
        phase: "dragging",
        draft: withDelta(state.draft, raw, snap),
      });
    }
    case "step": {
      if (state.phase !== "keyboard_draft") return state;
      const raw = state.draft.rawDeltaFrames + event.deltaFrames;
      return Object.freeze({
        phase: "keyboard_draft",
        draft: withDelta(state.draft, raw, null),
      });
    }
    case "mapping_changed": {
      if (state.phase !== "dragging" && state.phase !== "keyboard_draft")
        return state;
      // A changed zoom/scroll/resize mapping cancels the draft rather than rebasing it.
      return state.draft.identity.mappingKey === event.mappingKey
        ? state
        : TRIM_GESTURE_IDLE;
    }
    case "cancel":
      // Cancellation after submission cannot promise a rollback; only the local draft dies.
      if (state.phase === "dragging" || state.phase === "keyboard_draft")
        return TRIM_GESTURE_IDLE;
      return state;
    case "release":
      if (state.phase !== "dragging") return state;
      if (state.draft.geometry.noop) return TRIM_GESTURE_IDLE;
      return Object.freeze({
        phase: "submitting",
        draft: state.draft,
        requestId: event.requestId,
      });
    case "commit":
      if (state.phase !== "keyboard_draft") return state;
      if (state.draft.geometry.noop) return TRIM_GESTURE_IDLE;
      return Object.freeze({
        phase: "submitting",
        draft: state.draft,
        requestId: event.requestId,
      });
    case "accepted":
      if (state.phase !== "submitting" && state.phase !== "reconciling")
        return state;
      return Object.freeze({
        phase: "accepted",
        draft: state.draft,
        requestId: state.requestId,
        reason: null,
      });
    case "rejected":
      if (state.phase !== "submitting" && state.phase !== "reconciling")
        return state;
      return Object.freeze({
        phase: "rejected",
        draft: state.draft,
        requestId: state.requestId,
        reason: event.reason,
      });
    case "response_lost":
      if (state.phase !== "submitting") return state;
      // A timeout/abort/lost response is unknown, not proof of rejection.
      return Object.freeze({
        phase: "reconciling",
        draft: state.draft,
        requestId: state.requestId,
        reason: event.reason,
      });
    case "reconciled":
      // Read-only reconciliation establishes current state but cannot attribute this request;
      // the gesture returns to idle without replaying or inferring a safe retry.
      return state.phase === "reconciling" ? TRIM_GESTURE_IDLE : state;
    case "reset":
      return state.phase === "accepted" || state.phase === "rejected"
        ? TRIM_GESTURE_IDLE
        : state;
    default:
      return state;
  }
}

/** The exact `trim_clip` wire payload for a submitted draft. */
export function trimCommandForDraft(draft: TrimGestureDraft): Readonly<{
  kind: "trim_clip";
  clip_id: string;
  edge: TrimEdge;
  delta_frames: number;
}> {
  return Object.freeze({
    kind: "trim_clip",
    clip_id: draft.identity.clipId,
    edge: draft.identity.edge,
    delta_frames: draft.geometry.deltaFrames,
  });
}
