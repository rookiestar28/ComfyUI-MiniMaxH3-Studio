import type { AuthoringViewState } from "../../state/authoringViewState";
import type { TimelineCommandWire } from "../../contracts/authoringWorkbenchCodec";
import type {
  CompositionClip,
  PublicCompositionSnapshot,
} from "../../contracts/compositionCodec";
import { resolveSourceLandmarkAfterElapsed } from "../../runtime/sceneResolver";
import { build, freshIdentifier, rebasable } from "./nleCommandBuilders";

const MAX_CLIPS = 128;
const MAX_TRANSACTION_COMMANDS = 32;

export type TimelineToolAction =
  | Readonly<{ kind: "split" | "trim_start" | "trim_end" }>
  | Readonly<{ kind: "delete"; forceRipple?: boolean }>
  | Readonly<{ kind: "undo" | "redo" | "rebase" }>
  | Readonly<{
      kind: "roll";
      leftClipId: string;
      rightClipId: string;
      deltaFrames: number;
    }>;

export type TimelineToolReason =
  | "busy"
  | "selection_required"
  | "single_selection_required"
  | "stale_selection"
  | "locked_track"
  | "playhead_unavailable"
  | "playhead_outside_clip"
  | "clip_capacity"
  | "command_limit"
  | "history_unavailable"
  | "rebase_unavailable"
  | "cut_unavailable"
  | "source_range_unavailable"
  | "empty_roll_side";

export type TimelineRebaseAttempt = Readonly<{
  baseTimelineFingerprint: string;
  commands: readonly TimelineCommandWire[];
}>;

export type TimelineToolContext = Readonly<{
  snapshot: PublicCompositionSnapshot;
  selection: readonly string[];
  authoringStatus: AuthoringViewState["status"];
  /** The synchronous ruler-request authority, or monitor fallback when no request is pending. */
  playheadFrame: number | null;
  rippleEnabled: boolean;
  undoCursor: string | null;
  redoCursor: string | null;
  rebaseAttempt: TimelineRebaseAttempt | null;
}>;

export type TimelineToolDecision = Readonly<{
  enabled: boolean;
  reason: TimelineToolReason | null;
  commands: readonly TimelineCommandWire[];
}>;

export function decideTimelineHistoryTool(
  action: "undo" | "redo",
  context: Readonly<{
    authoringStatus: AuthoringViewState["status"];
    undoCursor: string | null;
    redoCursor: string | null;
  }>,
): TimelineToolDecision {
  if (
    context.authoringStatus !== "ready" &&
    context.authoringStatus !== "conflict"
  )
    return refuse("busy");
  const cursor = action === "undo" ? context.undoCursor : context.redoCursor;
  if (cursor === null) return refuse("history_unavailable");
  return admit([action === "undo" ? build.undo(cursor) : build.redo(cursor)]);
}

const refuse = (reason: TimelineToolReason): TimelineToolDecision =>
  Object.freeze({ enabled: false, reason, commands: Object.freeze([]) });

const admit = (
  commands: readonly TimelineCommandWire[],
): TimelineToolDecision =>
  Object.freeze({
    enabled: true,
    reason: null,
    commands: Object.freeze([...commands]),
  });

export function toolPlayheadFrame(
  input: Readonly<{
    requestedFrame: number | null;
    monitorFrame: number | null;
    transportAvailable: boolean;
  }>,
): number | null {
  if (!input.transportAvailable) return null;
  return input.requestedFrame ?? input.monitorFrame;
}

function selectedClips(
  context: TimelineToolContext,
): readonly CompositionClip[] | null {
  const byId = new Map(
    context.snapshot.clips.map((clip) => [clip.clipId, clip]),
  );
  const clips = context.selection.map((clipId) => byId.get(clipId));
  return clips.some((clip) => clip === undefined)
    ? null
    : (clips as readonly CompositionClip[]);
}

function editableReason(
  context: TimelineToolContext,
  clips: readonly CompositionClip[],
): TimelineToolReason | null {
  const locked = new Set(
    context.snapshot.tracks
      .filter((track) => track.locked)
      .map((track) => track.trackId),
  );
  return clips.some((clip) => locked.has(clip.trackId)) ? "locked_track" : null;
}

function sourceShiftReason(
  context: TimelineToolContext,
  clip: CompositionClip,
  deltaFrames: number,
): TimelineToolReason | null {
  const track = context.snapshot.tracks.find(
    (candidate) => candidate.trackId === clip.trackId,
  );
  if (track?.kind !== "primary_video" && track?.kind !== "video_overlay")
    return null;
  const asset = context.snapshot.assets.find(
    (candidate) => candidate.assetId === clip.assetId,
  );
  const frameRate = context.snapshot.output.frameRate as
    Readonly<{ num: number; den: number }> | undefined;
  if (asset?.kind !== "video" || frameRate === undefined)
    return "source_range_unavailable";
  try {
    resolveSourceLandmarkAfterElapsed(
      asset,
      clip.sourceStartFrame,
      deltaFrames,
      frameRate,
    );
    return null;
  } catch {
    // CRITICAL: output-frame deltas are not source-frame ordinals. A command whose boundary has
    // no exact admitted landmark is guaranteed to be rejected by the canonical backend.
    return "source_range_unavailable";
  }
}

function onePositionClip(
  context: TimelineToolContext,
): Readonly<{ clip: CompositionClip; frame: number }> | TimelineToolDecision {
  if (context.selection.length !== 1)
    return refuse("single_selection_required");
  const clips = selectedClips(context);
  if (clips === null) return refuse("stale_selection");
  const clip = clips[0]!;
  const blocked = editableReason(context, clips);
  if (blocked !== null) return refuse(blocked);
  if (context.playheadFrame === null) return refuse("playhead_unavailable");
  const end = clip.startFrame + clip.durationFrames;
  if (context.playheadFrame <= clip.startFrame || context.playheadFrame >= end)
    return refuse("playhead_outside_clip");
  return Object.freeze({ clip, frame: context.playheadFrame });
}

function isDecision(
  value:
    Readonly<{ clip: CompositionClip; frame: number }> | TimelineToolDecision,
): value is TimelineToolDecision {
  return "enabled" in value;
}

function decideDelete(
  action: Extract<TimelineToolAction, { kind: "delete" }>,
  context: TimelineToolContext,
): TimelineToolDecision {
  const ripple = context.rippleEnabled || action.forceRipple === true;
  if (context.selection.length === 0) return refuse("selection_required");
  if (ripple && context.selection.length !== 1)
    return refuse("single_selection_required");
  if (!ripple && context.selection.length > MAX_TRANSACTION_COMMANDS)
    return refuse("command_limit");
  const clips = selectedClips(context);
  if (clips === null) return refuse("stale_selection");
  const blocked = editableReason(context, clips);
  if (blocked !== null) return refuse(blocked);
  if (ripple) {
    const clip = clips[0]!;
    return admit([
      build.rippleDelete(
        clip.startFrame,
        clip.durationFrames,
        [clip.trackId],
        {},
      ),
    ]);
  }
  const trackOrder = new Map(
    context.snapshot.tracks.map((track) => [track.trackId, track.order]),
  );
  const ordered = [...clips].sort(
    (left, right) =>
      (trackOrder.get(left.trackId) ?? Number.MAX_SAFE_INTEGER) -
        (trackOrder.get(right.trackId) ?? Number.MAX_SAFE_INTEGER) ||
      left.startFrame - right.startFrame ||
      left.clipId.localeCompare(right.clipId),
  );
  return admit(ordered.map((clip) => build.removeClip(clip.clipId)));
}

export type RollCut = Readonly<{
  leftClipId: string;
  rightClipId: string;
  frame: number;
}>;

export function eligibleRollCuts(
  snapshot: PublicCompositionSnapshot,
  trackId: string,
): readonly RollCut[] {
  const track = snapshot.tracks.find((member) => member.trackId === trackId);
  if (track === undefined || track.locked) return Object.freeze([]);
  const clips = snapshot.clips
    .filter((clip) => clip.trackId === trackId)
    .sort(
      (left, right) =>
        left.startFrame - right.startFrame ||
        left.clipId.localeCompare(right.clipId),
    );
  const cuts: RollCut[] = [];
  for (let index = 0; index + 1 < clips.length; index += 1) {
    const left = clips[index]!;
    const right = clips[index + 1]!;
    const frame = left.startFrame + left.durationFrames;
    if (left.clipId !== right.clipId && frame === right.startFrame)
      cuts.push(
        Object.freeze({
          leftClipId: left.clipId,
          rightClipId: right.clipId,
          frame,
        }),
      );
  }
  return Object.freeze(cuts);
}

export function decideTimelineTool(
  action: TimelineToolAction,
  context: TimelineToolContext,
): TimelineToolDecision {
  if (
    context.authoringStatus !== "ready" &&
    context.authoringStatus !== "conflict"
  )
    return refuse("busy");
  if (action.kind === "delete") return decideDelete(action, context);
  if (action.kind === "undo" || action.kind === "redo")
    return decideTimelineHistoryTool(action.kind, context);
  if (action.kind === "rebase") {
    const attempt = context.rebaseAttempt;
    if (
      context.authoringStatus !== "conflict" ||
      attempt === null ||
      attempt.baseTimelineFingerprint ===
        context.snapshot.timelineFingerprint ||
      !rebasable(attempt.commands)
    )
      return refuse("rebase_unavailable");
    return admit([
      build.rebaseTransaction(
        attempt.baseTimelineFingerprint,
        attempt.commands,
      ),
    ]);
  }
  if (action.kind === "roll") {
    const left = context.snapshot.clips.find(
      (clip) => clip.clipId === action.leftClipId,
    );
    const right = context.snapshot.clips.find(
      (clip) => clip.clipId === action.rightClipId,
    );
    if (left === undefined || right === undefined)
      return refuse("cut_unavailable");
    const blocked = editableReason(context, [left, right]);
    if (blocked !== null) return refuse(blocked);
    if (
      left.trackId !== right.trackId ||
      left.startFrame + left.durationFrames !== right.startFrame
    )
      return refuse("cut_unavailable");
    if (
      action.deltaFrames === 0 ||
      left.durationFrames + action.deltaFrames < 1 ||
      right.durationFrames - action.deltaFrames < 1
    )
      return refuse("empty_roll_side");
    const sourceBlocked = sourceShiftReason(context, right, action.deltaFrames);
    if (sourceBlocked !== null) return refuse(sourceBlocked);
    return admit([
      build.rollEdit(left.clipId, right.clipId, action.deltaFrames),
    ]);
  }

  const target = onePositionClip(context);
  if (isDecision(target)) return target;
  const delta =
    action.kind === "trim_end"
      ? target.frame - (target.clip.startFrame + target.clip.durationFrames)
      : target.frame - target.clip.startFrame;
  if (action.kind === "split") {
    if (context.snapshot.clips.length >= MAX_CLIPS)
      return refuse("clip_capacity");
    const sourceBlocked = sourceShiftReason(context, target.clip, delta);
    if (sourceBlocked !== null) return refuse(sourceBlocked);
    return admit([
      build.splitClip(
        target.clip.clipId,
        delta,
        freshIdentifier(context.snapshot, "clip"),
      ),
    ]);
  }
  const edge = action.kind === "trim_start" ? "start" : "end";
  if (edge === "start") {
    const sourceBlocked = sourceShiftReason(context, target.clip, delta);
    if (sourceBlocked !== null) return refuse(sourceBlocked);
  }
  return admit([
    context.rippleEnabled
      ? build.rippleTrim(target.clip.clipId, edge, delta, [target.clip.trackId])
      : build.trimClip(target.clip.clipId, edge, delta),
  ]);
}

export type TimelineShortcutAction =
  | "split"
  | "trim_start"
  | "trim_end"
  | "delete"
  | "ripple_delete"
  | "undo"
  | "redo"
  | "select_all"
  | "zoom_in"
  | "zoom_out"
  | "zoom_fit"
  | "toggle_play"
  | "step_back"
  | "step_forward";

export type TimelineShortcutInput = Readonly<{
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  repeat: boolean;
  isComposing: boolean;
  defaultPrevented: boolean;
  target: EventTarget | null;
}>;

const DISCRETE_SHORTCUTS = new Set<TimelineShortcutAction>([
  "split",
  "trim_start",
  "trim_end",
  "delete",
  "ripple_delete",
  "undo",
  "redo",
  "select_all",
]);

function childOwnsShortcut(target: EventTarget | null, key: string): boolean {
  if (!(target instanceof Element)) return false;
  if (
    target.closest(
      'input,textarea,select,[contenteditable="true"],[role="slider"],[role="spinbutton"],[role="menu"],[role="menuitem"],[data-h3-nle-shortcut-owner]',
    ) !== null
  )
    return true;
  const button = target.closest("button");
  // CRITICAL: timeline buttons keep Space activation but share edit/view shortcuts.
  // Giving every button full ownership disables shortcuts after an ordinary clip press.
  return (
    button !== null &&
    (key === " " || button.closest('[data-h3-nle-region="timeline"]') === null)
  );
}

/** Resolve only workspace-owned keys; callers contain every non-null result even when disabled. */
export function resolveTimelineShortcut(
  event: TimelineShortcutInput,
): Readonly<{ action: TimelineShortcutAction; invoke: boolean }> | null {
  if (
    event.defaultPrevented ||
    event.isComposing ||
    event.altKey ||
    childOwnsShortcut(event.target, event.key)
  )
    return null;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  const primary = event.ctrlKey || event.metaKey;
  let action: TimelineShortcutAction | null = null;
  if (primary && !event.shiftKey && key === "b") action = "split";
  else if (primary && !event.shiftKey && key === "z") action = "undo";
  else if (primary && event.shiftKey && key === "z") action = "redo";
  else if (primary && !event.shiftKey && key === "a") action = "select_all";
  else if ((key === "=" || key === "+") && (primary || !event.shiftKey))
    action = "zoom_in";
  else if (key === "-" && !event.shiftKey) action = "zoom_out";
  else if (!primary && !event.shiftKey && key === "q") action = "trim_start";
  else if (!primary && !event.shiftKey && key === "w") action = "trim_end";
  else if (!primary && key === "Delete")
    action = event.shiftKey ? "ripple_delete" : "delete";
  else if (!primary && event.shiftKey && key === "z") action = "zoom_fit";
  else if (!primary && !event.shiftKey && key === " ") action = "toggle_play";
  else if (!primary && !event.shiftKey && key === ",") action = "step_back";
  else if (!primary && !event.shiftKey && key === ".") action = "step_forward";
  if (action === null) return null;
  return Object.freeze({
    action,
    invoke: !(event.repeat && DISCRETE_SHORTCUTS.has(action)),
  });
}
