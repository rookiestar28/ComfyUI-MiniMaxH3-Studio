// Which coming ownership change the preview may prepare for while it plays.
//
// At a cut the incoming clip's audio is already scheduled; whatever its picture still has to
// acquire there is heard as the picture trailing the sound. The session therefore asks the
// runtime to open the incoming owner ahead of the cut. This module answers, from the accepted
// snapshot alone, where that next change is and whether the owner and the leases it holds are
// guaranteed to fit. It is pure: no resolver, no runtime, no clock.

import type {
  CompositionClip,
  PublicCompositionSnapshot,
} from "../contracts/compositionCodec";
import { RUNTIME_PROFILE } from "./mediaCapabilities";

/**
 * The lease pool the authority keeps per workspace, and the part of it that is kept free of
 * decorations so that playback can always acquire (`authoring_media.MAX_WORKSPACE_LEASES` and
 * the `- 2` in `MediaLeaseAuthority._capacity`). A Python test pins both ends to each other.
 */
export const WORKSPACE_LEASE_POOL = 8;
export const WORKSPACE_PLAYBACK_RESERVE = 2;

export type PreviewLookahead = Readonly<{
  /**
   * The frame of the next change that brings in a video owner, then -- when that owner is a
   * primary clip that hands over to another frame of the timeline -- the frame at which it does:
   * the scenes the runtime and the audio follower prepare from.
   */
  frames: readonly number[];
}>;

type Span = Readonly<{
  start: number;
  end: number;
  cost: number;
  video: boolean;
  primary: boolean;
}>;

/** Every enabled clip on an enabled track that holds a lease while it is on screen. */
function leaseSpans(snapshot: PublicCompositionSnapshot): Span[] {
  const tracks = new Map(
    snapshot.tracks.map((track) => [track.trackId, track]),
  );
  const assets = new Map(
    snapshot.assets.map((asset) => [asset.assetId, asset]),
  );
  const spans: Span[] = [];
  for (const clip of snapshot.clips) {
    const track = tracks.get(clip.trackId);
    if (!clip.enabled || track === undefined || !track.enabled) continue;
    const cost = leaseCost(clip, track.kind, assets);
    if (cost === 0) continue;
    spans.push({
      start: clip.startFrame,
      end: clip.startFrame + clip.durationFrames,
      cost,
      video:
        clip.assetId !== null && assets.get(clip.assetId)?.kind === "video",
      primary: track.kind === "primary_video",
    });
  }
  return spans;
}

/**
 * The next frame after `frame` at which the set of video owners changes -- a video clip starts
 * or ends -- or `null`. The scheduler makes an exact, ownership-changing move at each of them.
 */
export function nextVideoOwnerChange(
  snapshot: PublicCompositionSnapshot,
  frame: number,
): number | null {
  const durationFrames = Number(snapshot.output.durationFrames);
  if (
    !Number.isSafeInteger(frame) ||
    frame < 0 ||
    !Number.isSafeInteger(durationFrames)
  )
    return null;
  const changes = leaseSpans(snapshot)
    .filter((span) => span.video)
    .flatMap((span) => [span.start, span.end])
    .filter((change) => change > frame && change < durationFrames);
  return changes.length === 0 ? null : Math.min(...changes);
}

/**
 * The next ownership change to prepare for from `frame`, or `null` when there is none or when
 * preparing for it could take an owner or a lease the frames before it need.
 *
 * GUARD: the prepared owner is held for as long as the outgoing clip still plays, and it is held
 * out of two shared budgets. The resource layer admits two video owners in all, so a frame that
 * already shows two leaves no room. And the workspace has one pool of eight leases, shared with
 * every image, text and video layer on screen and with the decorations of the bin and the
 * timeline, of which the authority admits a decoration only while fewer than six are held. So
 * the owner is prepared only when the peak demand of every frame up to the change, plus the
 * owner's own leases, stays at five or fewer. Above either limit the handoff is left exactly as
 * it was: late, but never the cause of a refused layer or of a filmstrip that stops loading. Do
 * not replace the peak with the demand of the current frame: a title that appears halfway to the
 * cut is acquired after the owner was prepared and would be the one refused.
 */
export function planPreviewLookahead(
  snapshot: PublicCompositionSnapshot,
  frame: number,
): PreviewLookahead | null {
  const durationFrames = Number(snapshot.output.durationFrames);
  if (
    !Number.isSafeInteger(frame) ||
    frame < 0 ||
    !Number.isSafeInteger(durationFrames) ||
    frame >= durationFrames
  )
    return null;
  const spans = leaseSpans(snapshot);
  const videoStarts = spans
    .filter(
      (span) => span.video && span.start > frame && span.start < durationFrames,
    )
    .map((span) => span.start);
  if (videoStarts.length === 0) return null;
  const target = Math.min(...videoStarts);
  const entering = spans.filter((span) => span.video && span.start === target);
  // Demand is constant between clip boundaries, so a peak is reached at `frame` or at a start.
  const samples = [
    frame,
    ...spans
      .map((span) => span.start)
      .filter((start) => start > frame && start < target),
  ];
  const active = (sample: number) =>
    spans.filter((span) => span.start <= sample && sample < span.end);
  const videoPeak = Math.max(
    ...samples.map(
      (sample) => active(sample).filter((span) => span.video).length,
    ),
  );
  if (
    RUNTIME_PROFILE.limits.warmVideoOwners < 1 ||
    videoPeak + RUNTIME_PROFILE.limits.warmVideoOwners >
      RUNTIME_PROFILE.limits.activeVideoOwners
  )
    return null;
  const leasePeak = Math.max(
    ...samples.map((sample) =>
      active(sample).reduce((total, span) => total + span.cost, 0),
    ),
  );
  const incoming = Math.max(...entering.map((span) => span.cost));
  if (
    leasePeak + incoming >
    WORKSPACE_LEASE_POOL - WORKSPACE_PLAYBACK_RESERVE - 1
  )
    return null;
  // Where the entering primary clip stops being the audible owner: its end or the next primary
  // clip's start, whichever is first -- the same fence the audio follower puts on its selection.
  const primary = entering.find((span) => span.primary);
  const handover =
    primary === undefined
      ? null
      : Math.min(
          primary.end,
          ...spans
            .filter((span) => span.primary && span.start > target)
            .map((span) => span.start),
        );
  return Object.freeze({
    frames: Object.freeze(
      handover === null || handover >= durationFrames
        ? [target]
        : [target, handover],
    ),
  });
}

/** Leases one layer holds while it is on screen: a primary video with bound audio holds two. */
function leaseCost(
  clip: CompositionClip,
  trackKind: string,
  assets: ReadonlyMap<
    string,
    Readonly<{ kind: string; embeddedAudio: string }>
  >,
): number {
  if (clip.text !== null) return 1;
  if (clip.assetId === null) return 0;
  const asset = assets.get(clip.assetId);
  if (asset === undefined) return 0;
  if (asset.kind === "image") return 1;
  if (asset.kind !== "video") return 0;
  return trackKind === "primary_video" &&
    asset.embeddedAudio === "present_bound"
    ? 2
    : 1;
}
