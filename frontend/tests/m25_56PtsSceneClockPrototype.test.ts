import { describe, expect, it } from "vitest";

import parity from "../../tests/fixtures/m25_16_scene_resolver_parity_v1.json";
import {
  decodePublicCompositionSnapshot,
  type PublicCompositionSnapshot,
} from "../src/contracts/compositionCodec";
import {
  PtsSceneClockPrototype,
  type NativePtsObservation,
} from "./prototypes/m25_56PtsSceneClockPrototype";

type ParityCase = Readonly<{
  name: string;
  snapshot: Record<string, unknown>;
}>;

const splitSnapshotWire = (() => {
  const value = (parity as { cases: readonly ParityCase[] }).cases.find(
    (candidate) => candidate.name === "primary_split_two_owners",
  );
  if (value === undefined) throw new Error("primary split fixture is missing");
  return value.snapshot;
})();

function makeSnapshot(): PublicCompositionSnapshot {
  const snapshot = decodePublicCompositionSnapshot(splitSnapshotWire);
  const primary = snapshot.assets.find(
    (asset) => asset.assetId === "vid-primary",
  );
  if (primary === undefined)
    throw new Error("primary fixture asset is missing");
  const first = primary.landmarks[0];
  const second = primary.landmarks[1];
  if (first === undefined || second === undefined)
    throw new Error("primary fixture landmarks are missing");
  const frames = second.frameIndex - first.frameIndex;
  const ticks = second.pts - first.pts;
  if (frames <= 0 || ticks % frames !== 0)
    throw new Error("primary fixture is not an exact CFR landmark table");
  const ticksPerFrame = ticks / frames;
  if (
    primary.landmarks.some(
      (landmark) =>
        landmark.pts !==
        first.pts + (landmark.frameIndex - first.frameIndex) * ticksPerFrame,
    )
  )
    throw new Error("primary fixture landmarks do not agree on the CFR step");
  return {
    ...snapshot,
    assets: snapshot.assets.map((asset) =>
      asset.assetId !== "vid-primary"
        ? asset
        : {
            ...asset,
            landmarks: Array.from(
              { length: asset.sourceFrameCount ?? 0 },
              (_, frameIndex) => ({
                frameIndex,
                pts:
                  first.pts + (frameIndex - first.frameIndex) * ticksPerFrame,
                dts:
                  first.dts + (frameIndex - first.frameIndex) * ticksPerFrame,
                durationTicks: ticksPerFrame,
              }),
            ),
          },
    ),
  } as PublicCompositionSnapshot;
}

function observation(
  observedSourcePts: number,
  overrides: Partial<NativePtsObservation> = {},
): NativePtsObservation {
  return {
    ownerId: "primary-owner-1",
    assetId: "vid-primary",
    clipId: "clip-main",
    epoch: 1,
    observedSourcePts,
    ...overrides,
  };
}

describe("M25-56 test-only PTS scene-clock prototype", () => {
  it("maps skipped native PTS observations through a rational split cut", () => {
    const clock = new PtsSceneClockPrototype(makeSnapshot(), {
      ownerId: "primary-owner-1",
      assetId: "vid-primary",
      clipId: "clip-main",
      epoch: 1,
    });

    const mapped = [0, 3_072, 5_632, 6_144, 7_680].map((pts) => {
      const afterSplit = pts >= 6_144;
      if (pts === 6_144)
        clock.replaceOwner({
          ownerId: "right-owner-2",
          assetId: "vid-primary",
          clipId: "clip-main-right",
          epoch: 2,
        });
      return clock.observe(
        observation(pts, {
          ownerId: afterSplit ? "right-owner-2" : "primary-owner-1",
          clipId: afterSplit ? "clip-main-right" : "clip-main",
          epoch: afterSplit ? 2 : 1,
        }),
      );
    });
    expect(mapped.map((row) => row.disposition)).toEqual([
      "presented",
      "presented",
      "presented",
      "presented",
      "presented",
    ]);
    expect(mapped.map((row) => row.frame)).toEqual([0, 6, 11, 12, 15]);
    expect(mapped.map((row) => row.clipId)).toEqual([
      "clip-main",
      "clip-main",
      "clip-main",
      "clip-main-right",
      "clip-main-right",
    ]);
    expect(mapped.map((row) => row.scene?.frame)).toEqual([0, 6, 11, 12, 15]);
  });

  it("ignores duplicate, stale-owner and stale-epoch callbacks", () => {
    const clock = new PtsSceneClockPrototype(makeSnapshot(), {
      ownerId: "primary-owner-1",
      assetId: "vid-primary",
      clipId: "clip-main",
      epoch: 1,
    });
    expect(clock.observe(observation(0)).disposition).toBe("presented");
    expect(clock.observe(observation(0))).toMatchObject({
      disposition: "ignored",
      reason: "duplicate_or_non_monotonic",
    });
    expect(
      clock.observe(observation(512, { ownerId: "replaced-owner" })),
    ).toMatchObject({
      disposition: "ignored",
      reason: "stale_authority",
    });

    clock.replaceOwner({
      ownerId: "replacement-owner",
      assetId: "vid-primary",
      clipId: "clip-main",
      epoch: 2,
    });
    expect(clock.observe(observation(512))).toMatchObject({
      disposition: "ignored",
      reason: "stale_authority",
    });
    expect(
      clock.observe(
        observation(512, {
          ownerId: "replacement-owner",
          clipId: "clip-main",
          epoch: 2,
        }),
      ),
    ).toMatchObject({ disposition: "presented", frame: 1 });
  });

  it("refuses a PTS gap and ignores every callback after close", () => {
    const complete = makeSnapshot();
    const snapshot = {
      ...complete,
      clips: complete.clips.filter((clip) => clip.clipId !== "clip-main-right"),
    } as PublicCompositionSnapshot;
    const clock = new PtsSceneClockPrototype(snapshot, {
      ownerId: "primary-owner-1",
      assetId: "vid-primary",
      clipId: "clip-main",
      epoch: 1,
    });

    expect(clock.observe(observation(6_144))).toMatchObject({
      disposition: "blocked",
      reason: "source_range_unavailable",
    });
    clock.close();
    expect(clock.observe(observation(7_168))).toMatchObject({
      disposition: "ignored",
      reason: "closed",
    });
    expect(() =>
      clock.replaceOwner({
        ownerId: "late-owner",
        assetId: "vid-primary",
        clipId: "clip-main",
        epoch: 2,
      }),
    ).toThrow(/closed/u);
  });

  it("maps nonzero 30 fps source elapsed time onto the 24 fps output grid", () => {
    const complete = makeSnapshot();
    const snapshot = {
      ...complete,
      assets: complete.assets.map((asset) =>
        asset.assetId !== "vid-primary"
          ? asset
          : {
              ...asset,
              sourceTimeBase: { num: 1, den: 90_000 },
              sourceFrameCount: 60,
              landmarks: Array.from({ length: 60 }, (_, frameIndex) => ({
                frameIndex,
                pts: frameIndex * 3_000,
                dts: frameIndex * 3_000,
                durationTicks: 3_000,
              })),
            },
      ),
      clips: complete.clips.map((clip) =>
        clip.clipId === "clip-main" ? { ...clip, sourceStartFrame: 30 } : clip,
      ),
    } as PublicCompositionSnapshot;
    const clock = new PtsSceneClockPrototype(snapshot, {
      ownerId: "30fps-owner",
      assetId: "vid-primary",
      clipId: "clip-main",
      epoch: 1,
    });

    expect(
      clock.observe({
        ownerId: "30fps-owner",
        assetId: "vid-primary",
        clipId: "clip-main",
        epoch: 1,
        observedSourcePts: 90_000,
      }),
    ).toMatchObject({ disposition: "presented", sourceFrame: 30, frame: 0 });
    expect(
      clock.observe({
        ownerId: "30fps-owner",
        assetId: "vid-primary",
        clipId: "clip-main",
        epoch: 1,
        observedSourcePts: 105_000,
      }),
    ).toMatchObject({ disposition: "presented", sourceFrame: 35, frame: 4 });
  });

  it("maps variable-rate source time from PTS instead of counting source frames", () => {
    const complete = makeSnapshot();
    const snapshot = {
      ...complete,
      assets: complete.assets.map((asset) =>
        asset.assetId !== "vid-primary"
          ? asset
          : {
              ...asset,
              sourceTimeBase: { num: 1, den: 90_000 },
              sourceFrameCount: 60,
              landmarks: Array.from({ length: 60 }, (_, frameIndex) => {
                const pts =
                  frameIndex <= 34
                    ? frameIndex * 3_000
                    : 106_500 + (frameIndex - 35) * 3_000;
                return {
                  frameIndex,
                  pts,
                  dts: pts,
                  durationTicks: 3_000,
                };
              }),
            },
      ),
      clips: complete.clips.map((clip) =>
        clip.clipId === "clip-main" ? { ...clip, sourceStartFrame: 30 } : clip,
      ),
    } as PublicCompositionSnapshot;
    const clock = new PtsSceneClockPrototype(snapshot, {
      ownerId: "vfr-owner",
      assetId: "vid-primary",
      clipId: "clip-main",
      epoch: 1,
    });

    expect(
      clock.observe({
        ownerId: "vfr-owner",
        assetId: "vid-primary",
        clipId: "clip-main",
        epoch: 1,
        observedSourcePts: 106_500,
      }),
    ).toMatchObject({ disposition: "presented", sourceFrame: 35, frame: 4 });
  });

  it("uses clip identity to disambiguate repeated placements of the same source range", () => {
    const complete = makeSnapshot();
    const snapshot = {
      ...complete,
      clips: complete.clips.map((clip) =>
        clip.clipId === "clip-main-right"
          ? { ...clip, sourceStartFrame: 0 }
          : clip,
      ),
    } as PublicCompositionSnapshot;
    const clock = new PtsSceneClockPrototype(snapshot, {
      ownerId: "repeated-range-owner",
      assetId: "vid-primary",
      clipId: "clip-main-right",
      epoch: 1,
    });

    expect(
      clock.observe({
        ownerId: "repeated-range-owner",
        assetId: "vid-primary",
        clipId: "clip-main-right",
        epoch: 1,
        observedSourcePts: 0,
      }),
    ).toMatchObject({
      disposition: "presented",
      sourceFrame: 0,
      frame: 12,
      clipId: "clip-main-right",
    });
  });
});
