// M25-16: pure builders that turn inspector form values into exact M25-11 command wires.
//
// Each builder produces one `TimelineCommandWire` whose payload keys are exactly the accepted
// codec's closed key set for that kind. Builders never validate semantics the backend owns
// (editability, contiguity, capacity); they only shape the request so a canonical edit control
// maps one-to-one onto its exact command. The rebasable subset mirrors the codec's set.

import type {
  CompositionClip,
  PublicCompositionSnapshot,
} from "../../contracts/compositionCodec";
import type { TimelineCommandWire } from "../../contracts/authoringWorkbenchCodec";

type Kind = TimelineCommandWire["kind"];

export const REBASABLE_KINDS = Object.freeze(
  new Set<Kind>([
    "set_clip_enabled",
    "set_visual_transform",
    "set_crop",
    "set_opacity_blend",
    "set_text_content",
    "set_text_style",
    "set_transition",
    "set_effect",
    "set_clip_audio",
    "select_clips",
  ]),
);

export type TransformWire = Readonly<{
  anchor_x_bp: number;
  anchor_y_bp: number;
  position_x_bp: number;
  position_y_bp: number;
  scale_x_bp: number;
  scale_y_bp: number;
  rotation_mdeg: number;
}>;
export type CropWire = Readonly<{
  left_bp: number;
  top_bp: number;
  right_bp: number;
  bottom_bp: number;
}>;
export type EffectWire = Readonly<{
  kind: string;
  brightness_permille: number;
  contrast_permille: number;
  saturation_permille: number;
}>;

/** A video clip's own gain (millibels), mute and fades (output frames), as the command sends it. */
export type ClipAudioWire = Readonly<{
  gain_mb: number;
  muted: boolean;
  fade_in_frames: number;
  fade_out_frames: number;
}>;

export const IDENTITY_CLIP_AUDIO_WIRE: ClipAudioWire = Object.freeze({
  gain_mb: 0,
  muted: false,
  fade_in_frames: 0,
  fade_out_frames: 0,
});

export const IDENTITY_TRANSFORM: TransformWire = Object.freeze({
  anchor_x_bp: 5_000,
  anchor_y_bp: 5_000,
  position_x_bp: 0,
  position_y_bp: 0,
  scale_x_bp: 10_000,
  scale_y_bp: 10_000,
  rotation_mdeg: 0,
});

export const EMPTY_CROP: CropWire = Object.freeze({
  left_bp: 0,
  top_bp: 0,
  right_bp: 0,
  bottom_bp: 0,
});

export const NO_TRANSITION = Object.freeze({
  kind: "none",
  duration_frames: 0,
});

export const NO_EFFECT: EffectWire = Object.freeze({
  kind: "none",
  brightness_permille: 0,
  contrast_permille: 1_000,
  saturation_permille: 1_000,
});

export type TextStyleWire = Readonly<{
  font_asset_id: string;
  size_px: number;
  weight: 400 | 700;
  style: "normal" | "italic";
  align: "left" | "center" | "right";
  line_height_bp: number;
  fill_rgba: readonly [number, number, number, number];
  background_rgba: readonly [number, number, number, number] | null;
}>;

export const DEFAULT_TEXT_STYLE: Omit<TextStyleWire, "font_asset_id"> =
  Object.freeze({
    size_px: 48,
    weight: 400,
    style: "normal",
    align: "center",
    line_height_bp: 12_000,
    fill_rgba: Object.freeze([255, 255, 255, 255] as const),
    background_rgba: null,
  });

/** Shared valid seed for a new or reset title; an empty title is not an admitted text wire. */
export const DEFAULT_TITLE_TEXT = "Title";

export function transformWire(
  clip: CompositionClip | undefined,
): TransformWire {
  return Object.freeze({ ...IDENTITY_TRANSFORM, ...(clip?.transform ?? {}) });
}

export function cropWire(clip: CompositionClip | undefined): CropWire {
  return Object.freeze({ ...EMPTY_CROP, ...(clip?.crop ?? {}) });
}

export function effectWire(clip: CompositionClip | undefined): EffectWire {
  const effect = clip?.effect;
  if (effect === undefined) return NO_EFFECT;
  return Object.freeze({
    kind: String(effect.kind ?? "none"),
    brightness_permille: Number(effect.brightnessPermille ?? 0),
    contrast_permille: Number(effect.contrastPermille ?? 1_000),
    saturation_permille: Number(effect.saturationPermille ?? 1_000),
  });
}

export function clipAudioWire(
  clip: CompositionClip | undefined,
): ClipAudioWire {
  const audio = clip?.audio;
  if (audio === undefined) return IDENTITY_CLIP_AUDIO_WIRE;
  return Object.freeze({
    gain_mb: audio.gainMb,
    muted: audio.muted,
    fade_in_frames: audio.fadeInFrames,
    fade_out_frames: audio.fadeOutFrames,
  });
}

export function textStyleWire(
  clip: CompositionClip | undefined,
): TextStyleWire | null {
  const text = clip?.text;
  if (text === undefined || text === null) return null;
  return Object.freeze({
    font_asset_id: String(text.fontAssetId),
    size_px: Number(text.sizePx),
    weight: (Number(text.weight) === 700 ? 700 : 400) as 400 | 700,
    style: (text.style === "italic" ? "italic" : "normal") as
      "normal" | "italic",
    align: (["left", "center", "right"].includes(String(text.align))
      ? String(text.align)
      : "center") as "left" | "center" | "right",
    line_height_bp: Number(text.lineHeightBp),
    fill_rgba: Object.freeze([...(text.fillRgba as number[])] as [
      number,
      number,
      number,
      number,
    ]),
    background_rgba:
      text.backgroundRgba === null
        ? null
        : Object.freeze([...(text.backgroundRgba as number[])] as [
            number,
            number,
            number,
            number,
          ]),
  });
}

export function textContentOf(clip: CompositionClip | undefined): string {
  return typeof clip?.text?.content === "string" ? clip.text.content : "";
}

/** A fresh identifier from a stable prefix and the current snapshot, never colliding with it. */
export function freshIdentifier(
  snapshot: Pick<
    PublicCompositionSnapshot,
    "clips" | "tracks" | "timelineRevision"
  >,
  prefix: "clip" | "track",
): string {
  const taken = new Set<string>(
    prefix === "clip"
      ? snapshot.clips.map((clip) => clip.clipId)
      : snapshot.tracks.map((track) => track.trackId),
  );
  const base = `${prefix}-r${snapshot.timelineRevision}`;
  let index = 1;
  while (taken.has(`${base}-${index}`)) index += 1;
  return `${base}-${index}`;
}

export type NewClipDraft = Readonly<{
  clipId: string;
  trackId: string;
  assetId: string | null;
  startFrame: number;
  durationFrames: number;
  sourceStartFrame: number;
  text: (TextStyleWire & { content: string }) | null;
}>;

export type PrimaryAppendRefusal =
  | "primary_missing"
  | "primary_locked"
  | "timing_unavailable"
  | "insufficient_capacity"
  | "append_rejected";

export type PrimaryAppendDecision =
  | Readonly<{ admitted: true; draft: NewClipDraft }>
  | Readonly<{ admitted: false; reason: PrimaryAppendRefusal }>;

export type PrimaryAppendNotice =
  PrimaryAppendRefusal | "pending" | "accepted" | "stale_revision";

export function clipWire(draft: NewClipDraft) {
  return Object.freeze({
    clip_id: draft.clipId,
    asset_id: draft.assetId,
    track_id: draft.trackId,
    start_frame: draft.startFrame,
    duration_frames: draft.durationFrames,
    source_start_frame: draft.text === null ? draft.sourceStartFrame : 0,
    enabled: true,
    transform: IDENTITY_TRANSFORM,
    crop: EMPTY_CROP,
    opacity_bp: 10_000,
    blend: "normal",
    text: draft.text === null ? null : Object.freeze({ ...draft.text }),
    transition: NO_TRANSITION,
    effect: NO_EFFECT,
  });
}

const command = (
  kind: Kind,
  payload: Record<string, unknown>,
): TimelineCommandWire =>
  Object.freeze({ kind, payload: Object.freeze(payload) });

export const build = Object.freeze({
  createTrack: (
    trackId: string,
    kind: "video_overlay" | "image_overlay" | "text_overlay",
    order: number,
  ) => command("create_track", { track_id: trackId, kind, order }),
  removeTrack: (trackId: string) =>
    command("remove_track", { track_id: trackId }),
  reorderTrack: (trackId: string, order: number) =>
    command("reorder_track", { track_id: trackId, order }),
  setTrackEnabled: (trackId: string, enabled: boolean) =>
    command("set_track_enabled", { track_id: trackId, enabled }),
  setTrackLocked: (trackId: string, locked: boolean) =>
    command("set_track_locked", { track_id: trackId, locked }),
  insertAssetClip: (draft: NewClipDraft) =>
    command("insert_asset_clip", { clip: clipWire(draft) }),
  insertTitleClip: (draft: NewClipDraft) =>
    command("insert_title_clip", { clip: clipWire(draft) }),
  replaceClipAsset: (
    clipId: string,
    assetId: string,
    sourceStartFrame: number,
  ) =>
    command("replace_clip_asset", {
      clip_id: clipId,
      asset_id: assetId,
      source_start_frame: sourceStartFrame,
    }),
  removeClip: (clipId: string) => command("remove_clip", { clip_id: clipId }),
  moveClip: (clipId: string, deltaFrames: number, targetTrackId: string) =>
    command("move_clip", {
      clip_id: clipId,
      delta_frames: deltaFrames,
      target_track_id: targetTrackId,
    }),
  moveGroup: (
    pairs: readonly (readonly [string, string])[],
    deltaFrames: number,
  ) =>
    command("move_group", {
      clip_ids: pairs.map(([clipId]) => clipId),
      delta_frames: deltaFrames,
      target_track_ids: pairs.map(([, trackId]) => trackId),
    }),
  trimClip: (clipId: string, edge: "start" | "end", deltaFrames: number) =>
    command("trim_clip", { clip_id: clipId, edge, delta_frames: deltaFrames }),
  splitClip: (clipId: string, atOffsetFrames: number, rightClipId: string) =>
    command("split_clip", {
      clip_id: clipId,
      at_offset_frames: atOffsetFrames,
      right_clip_id: rightClipId,
    }),
  mergeClips: (leftClipId: string, rightClipId: string) =>
    command("merge_clips", {
      left_clip_id: leftClipId,
      right_clip_id: rightClipId,
    }),
  insertRange: (draft: NewClipDraft, scopeTrackIds: readonly string[]) =>
    command("insert_range", {
      clip: clipWire(draft),
      scope_track_ids: [...scopeTrackIds],
    }),
  overwriteRange: (
    draft: NewClipDraft,
    scopeTrackIds: readonly string[],
    remainderIds: Readonly<Record<string, string>>,
  ) =>
    command("overwrite_range", {
      clip: clipWire(draft),
      start_frame: draft.startFrame,
      duration_frames: draft.durationFrames,
      scope_track_ids: [...scopeTrackIds],
      remainder_ids: { ...remainderIds },
    }),
  rippleDelete: (
    startFrame: number,
    durationFrames: number,
    scopeTrackIds: readonly string[],
    remainderIds: Readonly<Record<string, string>>,
  ) =>
    command("ripple_delete", {
      start_frame: startFrame,
      duration_frames: durationFrames,
      scope_track_ids: [...scopeTrackIds],
      remainder_ids: { ...remainderIds },
    }),
  rippleTrim: (
    clipId: string,
    edge: "start" | "end",
    deltaFrames: number,
    scopeTrackIds: readonly string[],
  ) =>
    command("ripple_trim", {
      clip_id: clipId,
      edge,
      delta_frames: deltaFrames,
      scope_track_ids: [...scopeTrackIds],
    }),
  rollEdit: (leftClipId: string, rightClipId: string, deltaFrames: number) =>
    command("roll_edit", {
      left_clip_id: leftClipId,
      right_clip_id: rightClipId,
      delta_frames: deltaFrames,
    }),
  slipClip: (clipId: string, deltaFrames: number) =>
    command("slip_clip", { clip_id: clipId, delta_frames: deltaFrames }),
  slideClip: (
    clipId: string,
    leftClipId: string,
    rightClipId: string,
    deltaFrames: number,
  ) =>
    command("slide_clip", {
      clip_id: clipId,
      left_clip_id: leftClipId,
      right_clip_id: rightClipId,
      delta_frames: deltaFrames,
    }),
  setClipEnabled: (clipId: string, enabled: boolean) =>
    command("set_clip_enabled", { clip_id: clipId, enabled }),
  setVisualTransform: (clipId: string, transform: TransformWire) =>
    command("set_visual_transform", {
      clip_id: clipId,
      transform: { ...transform },
    }),
  setCrop: (clipId: string, crop: CropWire) =>
    command("set_crop", { clip_id: clipId, crop: { ...crop } }),
  setOpacityBlend: (
    clipId: string,
    opacityBp: number,
    blend: "normal" | "multiply" | "screen",
  ) =>
    command("set_opacity_blend", {
      clip_id: clipId,
      opacity_bp: opacityBp,
      blend,
    }),
  setTextContent: (clipId: string, content: string) =>
    command("set_text_content", { clip_id: clipId, content }),
  setTextStyle: (clipId: string, style: TextStyleWire) =>
    command("set_text_style", { clip_id: clipId, style: { ...style } }),
  setTransition: (
    clipId: string,
    kind: "none" | "cross_dissolve_v1",
    durationFrames: number,
  ) =>
    command("set_transition", {
      clip_id: clipId,
      transition: {
        kind,
        duration_frames: kind === "none" ? 0 : durationFrames,
      },
    }),
  setEffect: (clipId: string, effect: EffectWire) =>
    command("set_effect", { clip_id: clipId, effect: { ...effect } }),
  setClipAudio: (clipId: string, audio: ClipAudioWire) =>
    command("set_clip_audio", {
      clip_id: clipId,
      gain_mb: audio.gain_mb,
      muted: audio.muted,
      fade_in_frames: audio.fade_in_frames,
      fade_out_frames: audio.fade_out_frames,
    }),
  selectClips: (clipIds: readonly string[]) =>
    command("select_clips", { clip_ids: [...clipIds] }),
  undo: (historyCursor: string) =>
    command("undo", { history_cursor: historyCursor }),
  redo: (historyCursor: string) =>
    command("redo", { history_cursor: historyCursor }),
  rebaseTransaction: (
    baseTimelineFingerprint: string,
    commands: readonly TimelineCommandWire[],
  ) =>
    command("rebase_transaction", {
      base_timeline_fingerprint: baseTimelineFingerprint,
      commands: commands.map((member) => ({
        kind: member.kind,
        payload: { ...member.payload },
      })),
    }),
});

/** Whether the rejected attempt can be rebased onto a newer accepted timeline. */
export function rebasable(commands: readonly TimelineCommandWire[]): boolean {
  return (
    commands.length > 0 &&
    commands.every((member) => REBASABLE_KINDS.has(member.kind))
  );
}

export function clampInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  const parsed =
    typeof value === "number" ? value : Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.trunc(parsed)));
}
