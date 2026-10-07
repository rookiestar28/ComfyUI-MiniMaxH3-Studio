import { describe, expect, it } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  nextVideoOwnerChange,
  planPreviewLookahead,
  WORKSPACE_LEASE_POOL,
  WORKSPACE_PLAYBACK_RESERVE,
} from "../src/runtime/previewLookahead";

// Which ownership change the preview may prepare for, decided from the snapshot alone. The cases
// are timelines; the numbers are leases: a primary clip with bound audio holds two, every other
// layer one, and the owner prepared ahead of a cut is held on top of what is on screen.

const CLIP_FRAMES = 48;
type Wire = typeof fixture.snapshot;
type ClipWire = Wire["clips"][number];

const landmarks = (frames: number) =>
  Array.from({ length: frames }, (_, frame) => ({
    frame_index: frame,
    pts: frame * 512,
    dts: frame * 512,
    duration_ticks: 512,
  }));

/** Adjacent primary clips of the audible source, then whatever the case lays over them. */
function timeline(
  primaryClips: number,
  layers: (wire: Wire, source: Readonly<Wire>) => ClipWire[] = () => [],
  mutate: (wire: Wire) => void = () => undefined,
) {
  const source = structuredClone(fixture.snapshot);
  const wire = structuredClone(fixture.snapshot);
  const totalFrames = CLIP_FRAMES * primaryClips;
  wire.output.duration_frames = totalFrames;
  wire.assets[0]!.landmarks = landmarks(CLIP_FRAMES);
  wire.assets[1]!.source_frame_count = totalFrames;
  wire.assets[1]!.landmarks = landmarks(totalFrames);
  wire.clips = [
    ...Array.from({ length: primaryClips }, (_, index) => ({
      ...structuredClone(source.clips[0]!),
      clip_id: `clip-primary-${index}`,
      start_frame: index * CLIP_FRAMES,
      duration_frames: CLIP_FRAMES,
    })),
    ...layers(wire, source),
  ];
  mutate(wire);
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return decodePublicCompositionSnapshot(wire);
}

const overlay = (source: Readonly<Wire>, start: number, duration: number) => ({
  ...structuredClone(source.clips[1]!),
  clip_id: `clip-overlay-${start}`,
  start_frame: start,
  duration_frames: duration,
  transition: { kind: "none", duration_frames: 0 },
});
const image = (source: Readonly<Wire>, start: number, duration: number) => ({
  ...structuredClone(source.clips[2]!),
  clip_id: `clip-image-${start}`,
  start_frame: start,
  duration_frames: duration,
});
const title = (source: Readonly<Wire>, start: number, duration: number) => ({
  ...structuredClone(source.clips[3]!),
  clip_id: `clip-title-${start}`,
  start_frame: start,
  duration_frames: duration,
});

describe("the ownership change the preview prepares for", () => {
  it("mirrors a pool in which a decoration stays admissible", () => {
    expect(WORKSPACE_LEASE_POOL).toBe(8);
    expect(WORKSPACE_PLAYBACK_RESERVE).toBe(2);
  });

  it("names the next cut, and the incoming clip's own handover when the timeline goes on", () => {
    const two = timeline(2);
    expect(planPreviewLookahead(two, 0)?.frames).toEqual([48]);
    expect(planPreviewLookahead(two, 47)?.frames).toEqual([48]);
    // Past the last cut nothing is coming.
    expect(planPreviewLookahead(two, 48)).toBeNull();

    const three = timeline(3);
    expect(planPreviewLookahead(three, 0)?.frames).toEqual([48, 96]);
    expect(planPreviewLookahead(three, 48)?.frames).toEqual([96]);
  });

  it("takes the handover from the primary track, not from an overlay's boundary", () => {
    const snapshot = timeline(3, (_wire, source) => [overlay(source, 60, 10)]);
    expect(planPreviewLookahead(snapshot, 0)?.frames).toEqual([48, 96]);
  });

  it("names an overlay that enters over a continuing primary, without a handover", () => {
    const snapshot = timeline(1, (_wire, source) => [overlay(source, 12, 12)]);
    expect(planPreviewLookahead(snapshot, 0)?.frames).toEqual([12]);
    expect(planPreviewLookahead(snapshot, 12)).toBeNull();
  });

  it("does not prepare while two video owners are on screen", () => {
    const spanning = timeline(2, (_wire, source) => [overlay(source, 0, 96)]);
    expect(planPreviewLookahead(spanning, 0)).toBeNull();
    expect(planPreviewLookahead(spanning, 47)).toBeNull();

    // An overlay that ends before the cut stops counting once it is off screen.
    const ending = timeline(2, (_wire, source) => [overlay(source, 0, 24)]);
    expect(planPreviewLookahead(ending, 0)).toBeNull();
    expect(planPreviewLookahead(ending, 23)).toBeNull();
    expect(planPreviewLookahead(ending, 24)?.frames).toEqual([48]);
  });

  it("prepares at five held leases and not at six", () => {
    // Primary 2 + image 1, and the incoming primary's 2: five.
    const five = timeline(2, (_wire, source) => [image(source, 0, 96)]);
    expect(planPreviewLookahead(five, 0)?.frames).toEqual([48]);
    // A title on top of that is the sixth.
    const six = timeline(2, (_wire, source) => [
      image(source, 0, 96),
      title(source, 0, 96),
    ]);
    expect(planPreviewLookahead(six, 0)).toBeNull();
  });

  it("counts a layer that appears between the playhead and the cut", () => {
    // The title is not on screen at frame 0, but it will be acquired before the cut: an owner
    // prepared at frame 0 would hold the lease that title then needs.
    const snapshot = timeline(2, (_wire, source) => [
      image(source, 0, 96),
      title(source, 30, 10),
    ]);
    expect(planPreviewLookahead(snapshot, 0)).toBeNull();
    expect(planPreviewLookahead(snapshot, 39)).toBeNull();
    // Once it has ended, the frames up to the cut hold three again.
    expect(planPreviewLookahead(snapshot, 40)?.frames).toEqual([48]);
  });

  it("counts nothing for a disabled clip or a disabled track", () => {
    const disabledClip = timeline(
      2,
      (_wire, source) => [image(source, 0, 96), title(source, 0, 96)],
      (wire) => {
        wire.clips.at(-1)!.enabled = false;
      },
    );
    expect(planPreviewLookahead(disabledClip, 0)?.frames).toEqual([48]);

    const disabledTrack = timeline(
      2,
      (_wire, source) => [overlay(source, 0, 96)],
      (wire) => {
        wire.tracks.find((track) => track.kind === "video_overlay")!.enabled =
          false;
      },
    );
    expect(planPreviewLookahead(disabledTrack, 0)?.frames).toEqual([48]);
  });

  it("names every frame at which the video owners change, and no other", () => {
    const two = timeline(2);
    expect(nextVideoOwnerChange(two, 0)).toBe(48);
    expect(nextVideoOwnerChange(two, 47)).toBe(48);
    // The timeline's own end is not a change: there is nothing to move to.
    expect(nextVideoOwnerChange(two, 48)).toBeNull();

    // An overlay's start and its end are changes; an image's and a title's are not.
    const layered = timeline(3, (_wire, source) => [
      overlay(source, 60, 10),
      image(source, 10, 20),
      title(source, 50, 5),
    ]);
    expect(nextVideoOwnerChange(layered, 0)).toBe(48);
    expect(nextVideoOwnerChange(layered, 48)).toBe(60);
    expect(nextVideoOwnerChange(layered, 60)).toBe(70);
    expect(nextVideoOwnerChange(layered, 70)).toBe(96);
    expect(nextVideoOwnerChange(layered, 96)).toBeNull();
    expect(nextVideoOwnerChange(layered, -1)).toBeNull();
  });

  it("answers nothing for a playhead outside the timeline", () => {
    const snapshot = timeline(2);
    expect(planPreviewLookahead(snapshot, -1)).toBeNull();
    expect(planPreviewLookahead(snapshot, 96)).toBeNull();
    expect(planPreviewLookahead(snapshot, 0.5)).toBeNull();
  });
});
