import type { AuthoringClip } from "../contracts/authoringWorkbenchCodec";

export type AuthoringAdjacentRelation =
  "contiguous" | "gap" | "overlap" | "none" | "unsupported";

export type AuthoringAdjacentPreview = Readonly<{
  currentClipId: string;
  relation: AuthoringAdjacentRelation;
  nextClipId?: string;
  boundaryFrame?: number;
}>;

const MAX_ACCEPTED_TIMELINE_CLIPS = 128;

function ordered(first: AuthoringClip, second: AuthoringClip): number {
  return (
    first.startFrame - second.startFrame ||
    first.clipId.localeCompare(second.clipId)
  );
}

export function classifyAuthoringAdjacentPreview({
  clips,
  currentClipId,
}: Readonly<{
  clips: readonly AuthoringClip[];
  currentClipId: string;
}>): AuthoringAdjacentPreview {
  if (clips.length > MAX_ACCEPTED_TIMELINE_CLIPS)
    throw new Error("adjacent preview exceeds the accepted clip ceiling");
  const current = clips.find((clip) => clip.clipId === currentClipId);
  if (current === undefined || current.kind !== "video")
    return Object.freeze({ currentClipId, relation: "unsupported" });

  const endFrame = current.startFrame + current.frames;
  const sameLane = clips.filter(
    (clip) =>
      clip.clipId !== current.clipId &&
      clip.kind === "video" &&
      clip.lane === current.lane,
  );
  // IMPORTANT: an intersecting accepted interval is never a warm candidate; treating its
  // start as a boundary would silently invent overlap composition the product does not own.
  const overlap = sameLane
    .filter(
      (clip) =>
        clip.startFrame < endFrame &&
        clip.startFrame + clip.frames > current.startFrame,
    )
    .sort(ordered)[0];
  if (overlap !== undefined)
    return Object.freeze({
      currentClipId,
      nextClipId: overlap.clipId,
      relation: "overlap",
      boundaryFrame: endFrame,
    });

  const next = sameLane
    .filter((clip) => clip.startFrame >= endFrame)
    .sort(ordered)[0];
  if (next === undefined)
    return Object.freeze({ currentClipId, relation: "none" });
  return Object.freeze({
    currentClipId,
    nextClipId: next.clipId,
    relation: next.startFrame === endFrame ? "contiguous" : "gap",
    boundaryFrame: endFrame,
  });
}
