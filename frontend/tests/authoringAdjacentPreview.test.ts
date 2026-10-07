import { describe, expect, it } from "vitest";

import { classifyAuthoringAdjacentPreview } from "../src/host/authoringAdjacentPreview";

const clip = (
  clipId: string,
  startFrame: number,
  overrides: Partial<{
    kind: "video" | "audio";
    lane: number;
    frames: number;
  }> = {},
) => ({
  clipId,
  assetId: `asset-${clipId}`,
  kind: overrides.kind ?? "video",
  lane: overrides.lane ?? 0,
  startFrame,
  frames: overrides.frames ?? 24,
  sourceStartFrame: 0,
  envelope: [],
});

describe("authoring adjacent preview classification", () => {
  it("selects one exact contiguous same-lane VIDEO boundary", () => {
    expect(
      classifyAuthoringAdjacentPreview({
        currentClipId: "clip-1",
        clips: [
          clip("other-lane", 24, { lane: 1 }),
          clip("clip-2", 24),
          clip("clip-1", 0),
          clip("clip-3", 48),
        ],
      }),
    ).toEqual({
      currentClipId: "clip-1",
      nextClipId: "clip-2",
      relation: "contiguous",
      boundaryFrame: 24,
    });
  });

  it("distinguishes a gap, overlap, other lane, and unsupported current clip", () => {
    expect(
      classifyAuthoringAdjacentPreview({
        currentClipId: "clip-1",
        clips: [clip("clip-1", 0), clip("clip-2", 30)],
      }),
    ).toMatchObject({ relation: "gap", nextClipId: "clip-2" });
    expect(
      classifyAuthoringAdjacentPreview({
        currentClipId: "clip-1",
        clips: [clip("clip-1", 0), clip("clip-2", 20)],
      }),
    ).toMatchObject({ relation: "overlap", nextClipId: "clip-2" });
    expect(
      classifyAuthoringAdjacentPreview({
        currentClipId: "clip-1",
        clips: [clip("clip-1", 0), clip("clip-2", 24, { lane: 1 })],
      }),
    ).toEqual({ currentClipId: "clip-1", relation: "none" });
    expect(
      classifyAuthoringAdjacentPreview({
        currentClipId: "audio-1",
        clips: [clip("audio-1", 0, { kind: "audio" })],
      }),
    ).toEqual({ currentClipId: "audio-1", relation: "unsupported" });
  });

  it("uses stable start/id ordering without scanning beyond the accepted clip ceiling", () => {
    const clips = Array.from({ length: 128 }, (_, index) =>
      clip(`clip-${String(index).padStart(3, "0")}`, index * 24),
    );
    expect(
      classifyAuthoringAdjacentPreview({
        currentClipId: "clip-000",
        clips: [...clips].reverse(),
      }),
    ).toMatchObject({
      relation: "contiguous",
      nextClipId: "clip-001",
      boundaryFrame: 24,
    });
  });
});
