import type { PublicCompositionSnapshot } from "../../src/contracts/compositionCodec";
import { resolveCompositionScene } from "../../src/runtime/sceneResolver";

export type PtsClockAuthority = Readonly<{
  ownerId: string;
  assetId: string;
  clipId: string;
  epoch: number;
}>;

export type NativePtsObservation = Readonly<
  PtsClockAuthority & { observedSourcePts: number }
>;

export type PtsSceneClockResult = Readonly<{
  disposition: "presented" | "ignored" | "blocked";
  reason?:
    | "stale_authority"
    | "duplicate_or_non_monotonic"
    | "closed"
    | "source_range_unavailable"
    | "no_active_primary_clip";
  frame?: number;
  sourceFrame?: number;
  clipId?: string;
  scene?: ReturnType<typeof resolveCompositionScene>;
}>;

/**
 * Test-only feasibility prototype. A PTS is mappable only when the admitted asset carries one
 * exact frame landmark for it. It converts elapsed source PTS through the asset timebase onto the
 * output frame grid, and explicit clip identity prevents repeated placements from being conflated.
 */
export class PtsSceneClockPrototype {
  private authority: PtsClockAuthority;
  private lastPts: number | null = null;
  private closed = false;

  constructor(
    private readonly snapshot: PublicCompositionSnapshot,
    authority: PtsClockAuthority,
  ) {
    this.assertAuthority(authority);
    this.authority = Object.freeze({ ...authority });
  }

  observe(observation: NativePtsObservation): PtsSceneClockResult {
    if (this.closed) return { disposition: "ignored", reason: "closed" };
    if (
      observation.ownerId !== this.authority.ownerId ||
      observation.assetId !== this.authority.assetId ||
      observation.clipId !== this.authority.clipId ||
      observation.epoch !== this.authority.epoch
    )
      return { disposition: "ignored", reason: "stale_authority" };
    if (
      !Number.isSafeInteger(observation.observedSourcePts) ||
      observation.observedSourcePts < 0
    )
      return { disposition: "blocked", reason: "source_range_unavailable" };
    if (this.lastPts !== null && observation.observedSourcePts <= this.lastPts)
      return { disposition: "ignored", reason: "duplicate_or_non_monotonic" };

    const asset = this.snapshot.assets.find(
      (candidate) => candidate.assetId === this.authority.assetId,
    );
    const clip = this.snapshot.clips.find(
      (candidate) => candidate.clipId === this.authority.clipId,
    );
    const primaryTrack = this.snapshot.tracks.find(
      (track) => track.trackId === clip?.trackId,
    );
    if (
      asset?.kind !== "video" ||
      clip === undefined ||
      !clip.enabled ||
      clip.assetId !== this.authority.assetId ||
      primaryTrack?.kind !== "primary_video" ||
      !primaryTrack.enabled
    )
      return { disposition: "blocked", reason: "no_active_primary_clip" };

    const landmarks = asset.landmarks.filter(
      (landmark) => landmark.pts === observation.observedSourcePts,
    );
    if (landmarks.length !== 1)
      return { disposition: "blocked", reason: "source_range_unavailable" };
    const sourceFrame = landmarks[0]!.frameIndex;
    const sourceStartLandmarks = asset.landmarks.filter(
      (landmark) => landmark.frameIndex === clip.sourceStartFrame,
    );
    const timeBase = asset.sourceTimeBase;
    const frameRate = this.snapshot.output.frameRate as
      Readonly<{ num: number; den: number }> | undefined;
    if (
      !Number.isSafeInteger(sourceFrame) ||
      sourceFrame < clip.sourceStartFrame ||
      sourceStartLandmarks.length !== 1 ||
      timeBase === null ||
      timeBase === undefined ||
      !Number.isSafeInteger(timeBase.num) ||
      !Number.isSafeInteger(timeBase.den) ||
      timeBase.num < 1 ||
      timeBase.den < 1 ||
      frameRate === undefined ||
      !Number.isSafeInteger(frameRate.num) ||
      !Number.isSafeInteger(frameRate.den) ||
      frameRate.num < 1 ||
      frameRate.den < 1
    )
      return { disposition: "blocked", reason: "source_range_unavailable" };

    const elapsedSourcePts = BigInt(
      observation.observedSourcePts - sourceStartLandmarks[0]!.pts,
    );
    if (elapsedSourcePts < 0n)
      return { disposition: "blocked", reason: "source_range_unavailable" };
    const elapsedOutputFrameNumerator =
      elapsedSourcePts * BigInt(timeBase.num) * BigInt(frameRate.num);
    const elapsedOutputFrameDenominator =
      BigInt(timeBase.den) * BigInt(frameRate.den);
    const clipDurationNumerator =
      BigInt(clip.durationFrames) *
      BigInt(frameRate.den) *
      BigInt(timeBase.den);
    if (
      elapsedSourcePts * BigInt(timeBase.num) * BigInt(frameRate.num) >=
      clipDurationNumerator
    )
      return { disposition: "blocked", reason: "source_range_unavailable" };

    // PTS may land between output boundaries. Keep the scene on the last whole output frame;
    // source frame counts cannot substitute here because source and output rates may differ.
    const elapsedOutputFrames = Number(
      elapsedOutputFrameNumerator / elapsedOutputFrameDenominator,
    );
    const frame = clip.startFrame + elapsedOutputFrames;
    const durationFrames = this.snapshot.output.durationFrames;
    if (
      !Number.isSafeInteger(elapsedOutputFrames) ||
      !Number.isSafeInteger(frame) ||
      frame < 0 ||
      !Number.isSafeInteger(durationFrames) ||
      frame >= (durationFrames as number)
    )
      return { disposition: "blocked", reason: "source_range_unavailable" };
    const scene = resolveCompositionScene(this.snapshot, frame);
    const primaryLayer = (
      scene.layers as readonly Readonly<Record<string, unknown>>[]
    ).find((layer) => layer.track_id === clip.trackId);
    if (primaryLayer?.clip_id !== clip.clipId)
      return { disposition: "blocked", reason: "no_active_primary_clip" };

    this.lastPts = observation.observedSourcePts;
    return {
      disposition: "presented",
      frame,
      sourceFrame,
      clipId: clip.clipId,
      scene,
    };
  }

  replaceOwner(authority: PtsClockAuthority): void {
    if (this.closed) throw new Error("closed PTS clock cannot be replaced");
    this.assertAuthority(authority);
    if (authority.epoch <= this.authority.epoch)
      throw new Error("replacement must advance the owner epoch");
    this.authority = Object.freeze({ ...authority });
    this.lastPts = null;
  }

  close(): void {
    this.closed = true;
    this.lastPts = null;
  }

  private assertAuthority(authority: PtsClockAuthority): void {
    if (
      authority.ownerId.length === 0 ||
      authority.assetId.length === 0 ||
      authority.clipId.length === 0 ||
      !Number.isSafeInteger(authority.epoch) ||
      authority.epoch < 1
    )
      throw new TypeError("PTS clock authority is invalid");
  }
}
