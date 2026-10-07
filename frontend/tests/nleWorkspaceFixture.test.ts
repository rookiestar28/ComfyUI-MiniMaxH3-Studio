import { describe, expect, it } from "vitest";

import {
  SMOKE_SHAPE,
  REFERENCE_SHAPE,
  REFERENCE_PRELOAD_SHAPE,
  referenceAssemblyCommands,
  VIRTUALIZED_SHAPE,
  authoringReady,
  historyFixture,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";

describe("M25-16 fixtures", () => {
  it("builds the plan's smoke and virtualized shapes as accepted snapshots", () => {
    const smoke = snapshotFixture(SMOKE_SHAPE);
    expect(smoke.clips).toHaveLength(64);
    expect(smoke.tracks).toHaveLength(4);
    expect(Number(smoke.output.durationFrames)).toBe(120 * 24);
    const big = snapshotFixture(VIRTUALIZED_SHAPE);
    expect(big.clips).toHaveLength(128);
    expect(big.tracks).toHaveLength(8);
    expect(Number(big.output.durationFrames)).toBe(600 * 24);
    expect(historyFixture(SMOKE_SHAPE).selection).toEqual([]);
    const ready = authoringReady(VIRTUALIZED_SHAPE, { selection: ["clip-0"] });
    expect(ready.status).toBe("ready");
  });

  it("builds the three-distinct-source 16:9 reference sequence", () => {
    const snapshot = snapshotFixture(REFERENCE_SHAPE);
    expect(snapshot.output).toMatchObject({
      width: 320,
      height: 180,
      durationFrames: 144,
    });
    expect(snapshot.clips.map((clip) => clip.assetId)).toEqual([
      "vid-primary",
      "vid-overlay",
      "vid-timing",
    ]);
    expect(
      snapshot.clips.map((clip) => [clip.startFrame, clip.durationFrames]),
    ).toEqual([
      [0, 48],
      [48, 24],
      [72, 48],
    ]);
    expect(snapshot.clips.map((clip) => clip.trackId)).toEqual([
      "track-primary",
      "track-primary",
      "track-primary",
    ]);
    expect(
      snapshot.assets.filter((asset) => asset.kind === "video"),
    ).toHaveLength(3);
    const preload = snapshotFixture(REFERENCE_PRELOAD_SHAPE);
    expect(preload.clips).toHaveLength(0);
    expect(preload.assets.map((asset) => asset.assetId)).toEqual(
      snapshot.assets.map((asset) => asset.assetId),
    );
    expect(referenceAssemblyCommands().map((command) => command.kind)).toEqual([
      "insert_asset_clip",
      "insert_asset_clip",
      "insert_asset_clip",
    ]);
  });
});
