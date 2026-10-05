export const EMBEDDED_AUDIO_FOLLOWER_SCHEMA =
  "h3.authoring.embedded_audio_follower_status.v1" as const;
export const EMBEDDED_AUDIO_POLICY =
  "primary_embedded_follow_video_v1" as const;

export const EMBEDDED_AUDIO_STATES = Object.freeze([
  "silent",
  "following",
  "seeking",
  "suspended",
  "unavailable",
] as const);
export const EMBEDDED_AUDIO_REASONS = Object.freeze([
  "none",
  "closed",
  "no_primary",
  "no_embedded_audio",
  "paused",
  "opening",
  "seek_in_progress",
  "suspended",
  "boundary_reached",
  "capability_unavailable",
  "invalid_scene",
  "source_unavailable",
  "playback_unavailable",
  "autoplay_blocked",
  "decode_failed",
  "cleanup_pending",
] as const);

export type EmbeddedAudioFollowerStatus = Readonly<{
  schema: typeof EMBEDDED_AUDIO_FOLLOWER_SCHEMA;
  policy_id: typeof EMBEDDED_AUDIO_POLICY;
  transport_epoch: number;
  scene_fingerprint: string | null;
  owner_clip_id: string | null;
  state: (typeof EMBEDDED_AUDIO_STATES)[number];
  reason: (typeof EMBEDDED_AUDIO_REASONS)[number];
  preview_capability: "available" | "unavailable" | "blocked";
  final_render_capability: "not_evaluated";
}>;

const keys = [
  "schema",
  "policy_id",
  "transport_epoch",
  "scene_fingerprint",
  "owner_clip_id",
  "state",
  "reason",
  "preview_capability",
  "final_render_capability",
].sort();
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

function invalid(): never {
  throw new Error("invalid_audio_status");
}

export function decodeEmbeddedAudioFollowerStatus(
  value: unknown,
): EmbeddedAudioFollowerStatus {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    invalid();
  const row = value as Record<string, unknown>;
  if (
    JSON.stringify(Object.keys(row).sort()) !== JSON.stringify(keys) ||
    row.schema !== EMBEDDED_AUDIO_FOLLOWER_SCHEMA ||
    row.policy_id !== EMBEDDED_AUDIO_POLICY ||
    !Number.isSafeInteger(row.transport_epoch) ||
    (row.transport_epoch as number) < 0 ||
    (row.scene_fingerprint !== null &&
      (typeof row.scene_fingerprint !== "string" ||
        !fingerprint.test(row.scene_fingerprint))) ||
    (row.owner_clip_id !== null &&
      (typeof row.owner_clip_id !== "string" ||
        !identifier.test(row.owner_clip_id))) ||
    !(EMBEDDED_AUDIO_STATES as readonly unknown[]).includes(row.state) ||
    !(EMBEDDED_AUDIO_REASONS as readonly unknown[]).includes(row.reason) ||
    !["available", "unavailable", "blocked"].includes(
      row.preview_capability as string,
    ) ||
    row.final_render_capability !== "not_evaluated"
  )
    invalid();
  // CRITICAL: a successful play promise is not final-render qualification. The follower owns
  // only browser policy; accepting a promoted final capability would invent renderer evidence.
  if (
    row.state === "following" &&
    (row.owner_clip_id === null ||
      row.scene_fingerprint === null ||
      row.preview_capability !== "available" ||
      row.reason !== "none")
  )
    invalid();
  if (row.state === "silent" && row.owner_clip_id !== null) invalid();
  if (
    row.reason === "closed" &&
    (row.state !== "silent" ||
      row.scene_fingerprint !== null ||
      row.owner_clip_id !== null)
  )
    invalid();
  return Object.freeze({ ...row }) as EmbeddedAudioFollowerStatus;
}
