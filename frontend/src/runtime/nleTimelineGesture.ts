import type {
  CompositionClip,
  CompositionTrack,
  PublicCompositionAsset,
} from "../contracts/compositionCodec";
import {
  proposalFromCapturedOrigin,
  quantizeSignedDelta,
} from "./timelineGeometry";

export type NleGestureKind =
  "move" | "move_group" | "insert_from_bin" | "trim" | "marquee" | "scrub";
export type NleGestureIdentity = Readonly<{
  workspaceHandle: string;
  workspaceRevision: number;
  timelineRevision: number;
  timelineFingerprint: string;
  authoringFingerprint?: string;
  mappingKey: string;
  clipIds: readonly string[];
}>;
type MoveOrigin = Readonly<{
  clipId: string;
  trackId: string;
  startFrame: number;
}>;
export type NleGestureDraft = Readonly<{
  kind: NleGestureKind;
  identity: NleGestureIdentity;
  input: "pointer" | "keyboard" | "click";
  pointerId: number | null;
  originClientX: number;
  originClientY: number;
  pixelsPerFrame: number;
  originFrame: number;
  origins: readonly MoveOrigin[];
  trackIds: readonly string[];
  targetTrackIds: readonly string[];
  deltaFrames: number;
  movementPx: number;
  edge: "start" | "end" | null;
  admitted: boolean;
  reason: string | null;
  snapFrame: number | null;
  marquee: Readonly<{
    x: number;
    y: number;
    width: number;
    height: number;
  }> | null;
  insert: Readonly<{
    assetId: string;
    sourceDurationFrames: number;
    durationFrames: number;
    targetFrame: number;
    targetTrackId: string | null;
  }> | null;
}>;
export type NleTimelineGestureState =
  | Readonly<{ phase: "idle" }>
  | Readonly<{ phase: "dragging" | "keyboard_draft"; draft: NleGestureDraft }>
  | Readonly<{ phase: "submitting"; draft: NleGestureDraft; requestId: string }>
  | Readonly<{
      phase: "accepted" | "rejected" | "reconciling";
      draft: NleGestureDraft;
      requestId: string;
      reason: string | null;
    }>;

export const NLE_GESTURE_IDLE: NleTimelineGestureState = Object.freeze({
  phase: "idle",
});

/**
 * Whether an open move or insert draft still has its owner. A pointer or keyboard draft is owned
 * by its first origin clip's element, so unmounting that clip ends the draft (`owner_removed`).
 *
 * IMPORTANT (B-M2561-08): a click-only draft is owned by the move panel and a bin insert by its
 * card, so a virtualized origin never ends them. Treating a click draft's first origin as its
 * owner cancelled a 33-member `group_limit` refusal before it could be shown, whenever the
 * selection walk had scrolled that origin out of the mounted window.
 */
export function draftOwnerPresent(
  state: NleTimelineGestureState,
  mountedClipIds: ReadonlySet<string>,
): boolean {
  if (state.phase !== "dragging" && state.phase !== "keyboard_draft")
    return true;
  if (state.draft.kind === "insert_from_bin" || state.draft.input === "click")
    return true;
  return mountedClipIds.has(state.draft.origins[0]?.clipId ?? "");
}

export type TimelineSnapKind = "clip_start" | "clip_end" | "playhead" | "grid";
export type TimelineSnapTarget = Readonly<{
  frame: number;
  kind: TimelineSnapKind;
}>;
export type TimelineSnapResult = Readonly<{
  deltaFrames: number;
  lineFrame: number;
  kind: TimelineSnapKind;
}>;

export type TimelineMoveRefusal =
  | "group_limit"
  | "source_missing"
  | "target_exhaustion"
  | "locked_track"
  | "primary_track"
  | "incompatible_track"
  | "bounds"
  | "overlap";

export type TimelineInsertRefusal =
  | "target_exhaustion"
  | "locked_track"
  | "incompatible_track"
  | "bounds"
  | "overlap";

export function admitTimelineInsert(
  input: Readonly<{
    asset: PublicCompositionAsset;
    startFrame: number;
    durationFrames: number;
    timelineDurationFrames: number;
    tracks: readonly CompositionTrack[];
    clips: readonly CompositionClip[];
    targetTrackId?: string;
    operation?: "add" | "insert_range" | "overwrite_range";
  }>,
): Readonly<{
  admitted: boolean;
  reason: TimelineInsertRefusal | null;
  trackId: string | null;
}> {
  const endFrame = input.startFrame + input.durationFrames;
  if (
    !Number.isSafeInteger(input.startFrame) ||
    !Number.isSafeInteger(input.durationFrames) ||
    input.startFrame < 0 ||
    input.durationFrames < 1 ||
    endFrame > input.timelineDurationFrames
  )
    return { admitted: false, reason: "bounds", trackId: null };

  let compatibleLocked = false;
  let compatibleOverlap = false;
  const candidates = [...input.tracks]
    .sort((left, right) => left.order - right.order)
    .filter(
      (track) =>
        input.targetTrackId === undefined ||
        track.trackId === input.targetTrackId,
    );
  for (const track of candidates) {
    const compatible =
      input.asset.kind === "video"
        ? track.kind === "primary_video" || track.kind === "video_overlay"
        : input.asset.kind === "image"
          ? track.kind === "image_overlay"
          : false;
    if (!compatible) {
      if (input.targetTrackId !== undefined)
        return { admitted: false, reason: "incompatible_track", trackId: null };
      continue;
    }
    if (track.locked) {
      compatibleLocked = true;
      continue;
    }
    // IMPORTANT: core insert_range shifts whole clips; an interior start requires a declared
    // split. Admit only a boundary here or the visible Insert sends 409 invalid_command.
    if (
      input.operation === "insert_range" &&
      input.clips.some(
        (clip) =>
          clip.trackId === track.trackId &&
          clip.startFrame < input.startFrame &&
          input.startFrame < clip.startFrame + clip.durationFrames,
      )
    ) {
      compatibleOverlap = true;
      continue;
    }
    const overlaps = input.clips.some(
      (clip) =>
        clip.trackId === track.trackId &&
        clip.startFrame < endFrame &&
        clip.startFrame + clip.durationFrames > input.startFrame,
    );
    // Range commands shift/replace occupied clips in core; treating them as Add
    // skips the intended track or refuses valid edits. Add/drag must still be empty.
    if (
      overlaps &&
      (input.operation === undefined || input.operation === "add")
    ) {
      compatibleOverlap = true;
      continue;
    }
    return { admitted: true, reason: null, trackId: track.trackId };
  }
  return {
    admitted: false,
    reason:
      input.targetTrackId !== undefined && candidates.length === 0
        ? "target_exhaustion"
        : compatibleOverlap
          ? "overlap"
          : compatibleLocked
            ? "locked_track"
            : "target_exhaustion",
    trackId: null,
  };
}

export function admitTimelineMove(
  input: Readonly<{
    origins: readonly CompositionClip[];
    targetTrackIds: readonly string[];
    deltaFrames: number;
    durationFrames: number;
    tracks: readonly CompositionTrack[];
    clips: readonly CompositionClip[];
    assets: readonly PublicCompositionAsset[];
  }>,
): Readonly<{ admitted: boolean; reason: TimelineMoveRefusal | null }> {
  if (input.origins.length > 32)
    return { admitted: false, reason: "group_limit" };
  const movingIds = new Set(input.origins.map((clip) => clip.clipId));
  for (let index = 0; index < input.origins.length; index += 1) {
    const clip = input.origins[index]!;
    const source = input.tracks.find((track) => track.trackId === clip.trackId);
    if (source === undefined)
      return { admitted: false, reason: "source_missing" };
    const target = input.tracks.find(
      (track) => track.trackId === input.targetTrackIds[index],
    );
    if (target === undefined)
      return { admitted: false, reason: "target_exhaustion" };
    if (source.locked || target.locked)
      return { admitted: false, reason: "locked_track" };
    const asset = input.assets.find(
      (member) => member.assetId === clip.assetId,
    );
    if (
      target.kind === "primary_video" &&
      (clip.trackId !== target.trackId || asset?.kind !== "video")
    )
      return { admitted: false, reason: "primary_track" };
    const compatible =
      target.kind === "primary_video"
        ? asset?.kind === "video"
        : target.kind === "video_overlay"
          ? asset?.kind === "video"
          : target.kind === "image_overlay"
            ? asset?.kind === "image"
            : clip.text !== null;
    if (!compatible) return { admitted: false, reason: "incompatible_track" };
    const proposedStart = clip.startFrame + input.deltaFrames;
    const proposedEnd = proposedStart + clip.durationFrames;
    if (proposedStart < 0 || proposedEnd > input.durationFrames)
      return { admitted: false, reason: "bounds" };
    const overlaps = input.clips.some(
      (member) =>
        !movingIds.has(member.clipId) &&
        member.trackId === target.trackId &&
        member.startFrame < proposedEnd &&
        member.startFrame + member.durationFrames > proposedStart,
    );
    if (overlaps) return { admitted: false, reason: "overlap" };
  }
  return { admitted: true, reason: null };
}

const SNAP_KIND_ORDER: Readonly<Record<TimelineSnapKind, number>> =
  Object.freeze({
    clip_start: 0,
    clip_end: 1,
    playhead: 2,
    grid: 3,
  });

export function selectTimelineSnap(
  movedStart: number,
  movedEnd: number,
  targets: readonly TimelineSnapTarget[],
  pixelsPerFrame: number,
  radiusPx = 8,
  limit = 32,
): TimelineSnapResult | null {
  if (!Number.isFinite(pixelsPerFrame) || pixelsPerFrame <= 0 || limit <= 0)
    return null;
  const ranked = targets
    .flatMap((target) => [
      {
        ...target,
        edge: 0,
        distance: Math.abs(target.frame - movedStart),
        correction: target.frame - movedStart,
      },
      {
        ...target,
        edge: 1,
        distance: Math.abs(target.frame - movedEnd),
        correction: target.frame - movedEnd,
      },
    ])
    .filter((candidate) => candidate.distance * pixelsPerFrame <= radiusPx)
    .sort(
      (left, right) =>
        left.distance - right.distance ||
        SNAP_KIND_ORDER[left.kind] - SNAP_KIND_ORDER[right.kind] ||
        left.frame - right.frame ||
        left.edge - right.edge,
    )
    .slice(0, limit);
  const winner = ranked[0];
  return winner === undefined
    ? null
    : Object.freeze({
        deltaFrames: winner.correction,
        lineFrame: winner.frame,
        kind: winner.kind,
      });
}

type BeginMove = Readonly<{
  type: "begin_move";
  input: "pointer" | "keyboard" | "click";
  identity: NleGestureIdentity;
  pointerId: number | null;
  originClientX: number;
  originClientY: number;
  pixelsPerFrame: number;
  origins: readonly MoveOrigin[];
  trackIds: readonly string[];
  targetTrackIds: readonly string[];
}>;
type BeginScrub = Readonly<{
  type: "begin_scrub";
  input: "pointer" | "keyboard" | "click";
  identity: NleGestureIdentity;
  pointerId: number | null;
  originClientX: number;
  pixelsPerFrame: number;
  originFrame: number;
}>;
type BeginInsertFromBin = Readonly<{
  type: "begin_insert_from_bin";
  identity: NleGestureIdentity;
  pointerId: number;
  originClientX: number;
  originClientY: number;
  pixelsPerFrame: number;
  assetId: string;
  durationFrames: number;
}>;
export type NleTimelineGestureEvent =
  | BeginMove
  | BeginScrub
  | BeginInsertFromBin
  | Readonly<{
      type: "move";
      clientX: number;
      clientY: number;
      targetTrackIds: readonly string[];
      snap: number | null;
      snapLine?: number | null;
      admitted?: boolean;
      reason?: string | null;
    }>
  | Readonly<{
      type: "update_insert_from_bin";
      clientX: number;
      clientY: number;
      targetFrame: number;
      targetTrackId: string | null;
      durationFrames: number;
      admitted: boolean;
      reason: string | null;
    }>
  | Readonly<{
      type: "step";
      deltaFrames: number;
      targetTrackIds?: readonly string[];
      admitted?: boolean;
      reason?: string | null;
    }>
  | Readonly<{ type: "release" | "commit"; requestId: string }>
  | Readonly<{ type: "cancel"; reason: string }>
  | Readonly<{ type: "mapping_changed"; mappingKey: string }>
  | Readonly<{
      type: "auto_scroll_reanchor";
      mappingKey: string;
      originClientX: number;
      originClientY?: number;
      originFrame: number;
    }>
  | Readonly<{ type: "accepted" }>
  | Readonly<{ type: "rejected" | "response_lost"; reason: string }>
  | Readonly<{ type: "reconciled" | "reset" }>;

function canBegin(state: NleTimelineGestureState): boolean {
  return (
    state.phase === "idle" ||
    state.phase === "accepted" ||
    state.phase === "rejected"
  );
}

function moveDraft(event: BeginMove): NleGestureDraft {
  return Object.freeze({
    kind: event.origins.length > 1 ? "move_group" : "move",
    identity: event.identity,
    input: event.input,
    pointerId: event.pointerId,
    originClientX: event.originClientX,
    originClientY: event.originClientY,
    pixelsPerFrame: event.pixelsPerFrame,
    originFrame: event.origins[0]?.startFrame ?? 0,
    origins: Object.freeze([...event.origins]),
    trackIds: Object.freeze([...event.trackIds]),
    targetTrackIds: Object.freeze([...event.targetTrackIds]),
    deltaFrames: 0,
    movementPx: 0,
    edge: null,
    admitted: event.origins.length > 0 && event.origins.length <= 32,
    reason: event.origins.length > 32 ? "group_limit" : null,
    snapFrame: null,
    marquee: null,
    insert: null,
  });
}

function scrubDraft(event: BeginScrub): NleGestureDraft {
  return Object.freeze({
    kind: "scrub",
    identity: event.identity,
    input: event.input,
    pointerId: event.pointerId,
    originClientX: event.originClientX,
    originClientY: 0,
    pixelsPerFrame: event.pixelsPerFrame,
    originFrame: event.originFrame,
    origins: Object.freeze([]),
    trackIds: Object.freeze([]),
    targetTrackIds: Object.freeze([]),
    deltaFrames: 0,
    movementPx: 0,
    edge: null,
    admitted: true,
    reason: null,
    snapFrame: null,
    marquee: null,
    insert: null,
  });
}

function insertFromBinDraft(event: BeginInsertFromBin): NleGestureDraft {
  return Object.freeze({
    kind: "insert_from_bin",
    identity: event.identity,
    input: "pointer",
    pointerId: event.pointerId,
    originClientX: event.originClientX,
    originClientY: event.originClientY,
    pixelsPerFrame: event.pixelsPerFrame,
    originFrame: 0,
    origins: Object.freeze([]),
    trackIds: Object.freeze([]),
    targetTrackIds: Object.freeze([]),
    deltaFrames: 0,
    movementPx: 0,
    edge: null,
    admitted: false,
    reason: "target_exhaustion",
    snapFrame: null,
    marquee: null,
    insert: Object.freeze({
      assetId: event.assetId,
      sourceDurationFrames: event.durationFrames,
      durationFrames: event.durationFrames,
      targetFrame: 0,
      targetTrackId: null,
    }),
  });
}

export function reduceNleTimelineGesture(
  state: NleTimelineGestureState,
  event: NleTimelineGestureEvent,
): NleTimelineGestureState {
  switch (event.type) {
    case "begin_move":
      if (
        !canBegin(state) ||
        !Number.isFinite(event.pixelsPerFrame) ||
        event.pixelsPerFrame <= 0
      )
        return state;
      return Object.freeze({
        phase: event.input === "pointer" ? "dragging" : "keyboard_draft",
        draft: moveDraft(event),
      });
    case "begin_scrub":
      if (
        !canBegin(state) ||
        !Number.isFinite(event.pixelsPerFrame) ||
        event.pixelsPerFrame <= 0
      )
        return state;
      return Object.freeze({
        phase: event.input === "pointer" ? "dragging" : "keyboard_draft",
        draft: scrubDraft(event),
      });
    case "begin_insert_from_bin":
      if (
        !canBegin(state) ||
        !Number.isFinite(event.pixelsPerFrame) ||
        event.pixelsPerFrame <= 0 ||
        !Number.isSafeInteger(event.durationFrames) ||
        event.durationFrames < 1
      )
        return state;
      return Object.freeze({
        phase: "dragging",
        draft: insertFromBinDraft(event),
      });
    case "move": {
      if (state.phase !== "dragging") return state;
      const raw = proposalFromCapturedOrigin(
        state.draft.originFrame,
        event.clientX - state.draft.originClientX,
        state.draft.pixelsPerFrame,
      );
      const delta =
        event.snap === null
          ? raw - state.draft.originFrame
          : event.snap - state.draft.originFrame;
      return Object.freeze({
        ...state,
        draft: Object.freeze({
          ...state.draft,
          deltaFrames: delta,
          movementPx: Math.hypot(
            event.clientX - state.draft.originClientX,
            event.clientY - state.draft.originClientY,
          ),
          targetTrackIds: Object.freeze([...event.targetTrackIds]),
          admitted: event.admitted ?? state.draft.admitted,
          reason: event.reason ?? null,
          snapFrame: event.snapLine ?? event.snap,
        }),
      });
    }
    case "update_insert_from_bin":
      if (
        state.phase !== "dragging" ||
        state.draft.kind !== "insert_from_bin" ||
        state.draft.insert === null
      )
        return state;
      return Object.freeze({
        ...state,
        draft: Object.freeze({
          ...state.draft,
          movementPx: Math.hypot(
            event.clientX - state.draft.originClientX,
            event.clientY - state.draft.originClientY,
          ),
          admitted: event.admitted,
          reason: event.reason,
          insert: Object.freeze({
            ...state.draft.insert,
            targetFrame: event.targetFrame,
            targetTrackId: event.targetTrackId,
            durationFrames: event.durationFrames,
          }),
        }),
      });
    case "step":
      if (state.phase !== "keyboard_draft") return state;
      return Object.freeze({
        ...state,
        draft: Object.freeze({
          ...state.draft,
          deltaFrames:
            state.draft.deltaFrames +
            (quantizeSignedDelta(event.deltaFrames) ?? 0),
          targetTrackIds: Object.freeze([
            ...(event.targetTrackIds ?? state.draft.targetTrackIds),
          ]),
          admitted: event.admitted ?? state.draft.admitted,
          reason: event.reason ?? null,
        }),
      });
    case "auto_scroll_reanchor":
      if (state.phase !== "dragging") return state;
      // IMPORTANT: auto-scroll changes only the controller's pixel anchor. The captured CAS and
      // clip authority object remains byte-identical; replacing it silently rebases an edit.
      return Object.freeze({
        ...state,
        draft: Object.freeze({
          ...state.draft,
          originClientX: event.originClientX,
          originClientY: event.originClientY ?? state.draft.originClientY,
          originFrame: event.originFrame,
        }),
      });
    case "mapping_changed":
      if (
        (state.phase !== "dragging" && state.phase !== "keyboard_draft") ||
        state.draft.identity.mappingKey === event.mappingKey
      )
        return state;
      return NLE_GESTURE_IDLE;
    case "cancel":
      return state.phase === "dragging" || state.phase === "keyboard_draft"
        ? NLE_GESTURE_IDLE
        : state;
    case "release":
    case "commit":
      if (state.phase !== "dragging" && state.phase !== "keyboard_draft")
        return state;
      if (
        state.draft.kind === "scrub" ||
        !state.draft.admitted ||
        (state.draft.input === "pointer" && state.draft.movementPx < 4) ||
        (state.draft.kind !== "insert_from_bin" &&
          state.draft.deltaFrames === 0 &&
          state.draft.targetTrackIds.every(
            (trackId, index) => trackId === state.draft.origins[index]?.trackId,
          ))
      )
        return NLE_GESTURE_IDLE;
      return Object.freeze({
        phase: "submitting",
        draft: state.draft,
        requestId: event.requestId,
      });
    case "accepted":
      return state.phase === "submitting" || state.phase === "reconciling"
        ? Object.freeze({ ...state, phase: "accepted", reason: null })
        : state;
    case "rejected":
      return state.phase === "submitting" || state.phase === "reconciling"
        ? Object.freeze({ ...state, phase: "rejected", reason: event.reason })
        : state;
    case "response_lost":
      return state.phase === "submitting"
        ? Object.freeze({
            ...state,
            phase: "reconciling",
            reason: event.reason,
          })
        : state;
    case "reconciled":
      return state.phase === "reconciling" ? NLE_GESTURE_IDLE : state;
    case "reset":
      return state.phase === "accepted" || state.phase === "rejected"
        ? NLE_GESTURE_IDLE
        : state;
  }
}

export type NleGestureCommand =
  | Readonly<{
      kind: "move_clip";
      clipId: string;
      deltaFrames: number;
      targetTrackId: string;
    }>
  | Readonly<{
      kind: "move_group";
      clipIds: readonly string[];
      deltaFrames: number;
      targetTrackIds: readonly string[];
    }>
  | Readonly<{
      kind: "insert_from_bin";
      assetId: string;
      startFrame: number;
      durationFrames: number;
      targetTrackId: string;
    }>;

export function commandForGesture(
  state: NleTimelineGestureState,
): NleGestureCommand | null {
  if (state.phase !== "submitting") return null;
  const draft = state.draft;
  if (draft.kind === "move")
    return Object.freeze({
      kind: "move_clip",
      clipId: draft.origins[0]!.clipId,
      deltaFrames: draft.deltaFrames,
      targetTrackId: draft.targetTrackIds[0]!,
    });
  if (draft.kind === "move_group")
    return Object.freeze({
      kind: "move_group",
      clipIds: Object.freeze(draft.origins.map((origin) => origin.clipId)),
      deltaFrames: draft.deltaFrames,
      targetTrackIds: Object.freeze([...draft.targetTrackIds]),
    });
  if (
    draft.kind === "insert_from_bin" &&
    draft.insert !== null &&
    draft.insert.targetTrackId !== null
  )
    return Object.freeze({
      kind: "insert_from_bin",
      assetId: draft.insert.assetId,
      startFrame: draft.insert.targetFrame,
      durationFrames: draft.insert.durationFrames,
      targetTrackId: draft.insert.targetTrackId,
    });
  return null;
}
