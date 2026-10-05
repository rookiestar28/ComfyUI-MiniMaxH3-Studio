import type {
  NleAuthoringStateV2,
  TimelineCommandWire,
} from "../../contracts/authoringWorkbenchCodec";
import type { PublicCompositionSnapshot } from "../../contracts/compositionCodec";
import type { AuthoringIntent } from "../../state/authoringViewState";
import {
  build,
  DEFAULT_TEXT_STYLE,
  freshIdentifier,
} from "./nleCommandBuilders";

export type TitleInsertionAuthority =
  PublicCompositionSnapshot | NleAuthoringStateV2;
export type TitleInsertionRefusal =
  | "busy"
  | "missing_font"
  | "invalid_content"
  | "unavailable_frame"
  | "insufficient_capacity"
  | "clip_limit"
  | "track_limit";
export type TitleInsertionDecision =
  | Readonly<{ admitted: false; reason: TitleInsertionRefusal }>
  | Readonly<{
      admitted: true;
      intent: Extract<AuthoringIntent, { action: "apply_timeline_commands" }>;
    }>;

const refuse = (reason: TitleInsertionRefusal): TitleInsertionDecision => ({
  admitted: false,
  reason,
});

export function planTitleInsertion(
  authority: TitleInsertionAuthority,
  input: Readonly<{
    busy: boolean;
    frame: number;
    fontId: string;
    content: string;
  }>,
): TitleInsertionDecision {
  if (input.busy) return refuse("busy");
  if (
    !authority.assets.some(
      (asset) => asset.kind === "font" && asset.assetId === input.fontId,
    )
  )
    return refuse("missing_font");
  const content = input.content.normalize("NFC");
  const scalarCount = Array.from(content).length;
  // Both validators must admit a title: the transaction counts Python splitlines,
  // while the display projection also counts the empty segment after a final LF.
  const terminalBreak = /[\n\u0085\u2028\u2029]$/u.test(content);
  const transactionLines =
    content.split(/[\n\u0085\u2028\u2029]/u).length - (terminalBreak ? 1 : 0);
  if (
    scalarCount < 1 ||
    scalarCount > 2_048 ||
    transactionLines > 32 ||
    content.split("\n").length > 32 ||
    /[\u0000-\u0008\u000b-\u001f\ud800-\udfff]/u.test(content)
  )
    return refuse("invalid_content");
  if (!Number.isSafeInteger(input.frame)) return refuse("unavailable_frame");
  const capacity =
    "editCapacityFrames" in authority
      ? authority.editCapacityFrames
      : Number(authority.output.durationFrames);
  if (!Number.isSafeInteger(capacity) || capacity < 1)
    return refuse("insufficient_capacity");
  const startFrame = Math.min(capacity - 1, Math.max(0, input.frame));
  const durationFrames = Math.min(24, capacity - startFrame);
  if (authority.clips.length >= 128) return refuse("clip_limit");
  const freeTrack = [...authority.tracks]
    .sort((left, right) => left.order - right.order)
    .find(
      (track) =>
        track.kind === "text_overlay" &&
        !track.locked &&
        !authority.clips.some(
          (clip) =>
            clip.trackId === track.trackId &&
            clip.startFrame < startFrame + durationFrames &&
            clip.startFrame + clip.durationFrames > startFrame,
        ),
    );
  if (freeTrack === undefined && authority.tracks.length >= 8)
    return refuse("track_limit");
  const trackId = freeTrack?.trackId ?? freshIdentifier(authority, "track");
  const commands: TimelineCommandWire[] = [];
  // Keep allocation and insertion in one transaction; separate dispatches leave
  // an empty track on refusal and require two undos for one Add title action.
  if (freeTrack === undefined)
    commands.push(
      build.createTrack(trackId, "text_overlay", authority.tracks.length),
    );
  commands.push(
    build.insertTitleClip({
      clipId: freshIdentifier(authority, "clip"),
      trackId,
      assetId: null,
      startFrame,
      durationFrames,
      sourceStartFrame: 0,
      text: { ...DEFAULT_TEXT_STYLE, font_asset_id: input.fontId, content },
    }),
  );
  return {
    admitted: true,
    intent: {
      action: "apply_timeline_commands",
      commands,
      capturedTimeline: {
        workspaceHandle: authority.workspaceHandle,
        workspaceRevision: authority.workspaceRevision,
        timelineRevision: authority.timelineRevision,
        timelineFingerprint: authority.timelineFingerprint,
        ...("authoringFingerprint" in authority
          ? { authoringFingerprint: authority.authoringFingerprint }
          : {}),
      },
    },
  };
}
