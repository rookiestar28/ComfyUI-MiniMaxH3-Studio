import { describe, expect, it } from "vitest";

import { authoringFilmstripTileCount } from "../src/contracts/authoringFilmstrip";
import type { PublicRuntimeAsset } from "../src/runtime/publicAssetManifest";

describe("authoring filmstrip tile budget", () => {
  it("accepts sparse public source landmarks", () => {
    const asset: PublicRuntimeAsset = {
      assetId: "video-sparse",
      kind: "video",
      sourceTimeBase: { num: 1, den: 10 },
      sourceFrameCount: 72,
      sourceSampleCount: null,
      embeddedAudio: "absent",
      timestampPolicy: "nonnegative_monotonic_v1",
      landmarks: [
        { frameIndex: 0, pts: 0, dts: 0, durationTicks: 1 },
        { frameIndex: 12, pts: 120, dts: 120, durationTicks: 1 },
        { frameIndex: 47, pts: 240, dts: 240, durationTicks: 1 },
      ],
    };

    expect(authoringFilmstripTileCount(asset)).toBe(49);
  });
});
