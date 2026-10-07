import { describe, expect, it } from "vitest";

import {
  NLE_GESTURE_IDLE,
  admitTimelineInsert,
  admitTimelineMove,
  commandForGesture,
  draftOwnerPresent,
  reduceNleTimelineGesture,
  selectTimelineSnap,
  type NleGestureIdentity,
} from "../src/runtime/nleTimelineGesture";
import {
  IDENTITY_CLIP_AUDIO,
  type CompositionClip,
  type CompositionTrack,
  type PublicCompositionAsset,
} from "../src/contracts/compositionCodec";

const identity: NleGestureIdentity = Object.freeze({
  workspaceHandle: "authoring-" + "a".repeat(32),
  workspaceRevision: 4,
  timelineRevision: 8,
  timelineFingerprint: "sha256:" + "b".repeat(64),
  mappingKey: "0.5:0:1000",
  clipIds: ["clip-a"],
});

describe("M25-48 bin insertion admission", () => {
  const tracks: readonly CompositionTrack[] = [
    {
      trackId: "locked-primary",
      kind: "primary_video",
      order: 0,
      enabled: true,
      locked: true,
    },
    {
      trackId: "video-overlay",
      kind: "video_overlay",
      order: 1,
      enabled: true,
      locked: false,
    },
    {
      trackId: "image-overlay",
      kind: "image_overlay",
      order: 2,
      enabled: true,
      locked: false,
    },
  ];
  const video: PublicCompositionAsset = {
    assetId: "private-video-id",
    kind: "video",
    sourceTimeBase: { num: 1, den: 24 },
    sourceFrameCount: 48,
    sourceSampleCount: null,
    embeddedAudio: "absent",
    timestampPolicy: "nonnegative_monotonic_v1",
    landmarks: [],
  };

  it("uses the first compatible unlocked non-overlapping track", () => {
    expect(
      admitTimelineInsert({
        asset: video,
        startFrame: 24,
        durationFrames: 24,
        timelineDurationFrames: 96,
        tracks,
        clips: [],
      }),
    ).toEqual({ admitted: true, reason: null, trackId: "video-overlay" });
  });

  it("fails closed for incompatible, overlapping and out-of-bounds proposals", () => {
    expect(
      admitTimelineInsert({
        asset: { ...video, kind: "image" },
        startFrame: 24,
        durationFrames: 1,
        timelineDurationFrames: 96,
        tracks: tracks.slice(0, 2),
        clips: [],
      }),
    ).toEqual({ admitted: false, reason: "target_exhaustion", trackId: null });
    expect(
      admitTimelineInsert({
        asset: video,
        startFrame: 24,
        durationFrames: 24,
        timelineDurationFrames: 96,
        tracks,
        clips: [
          {
            clipId: "existing",
            assetId: video.assetId,
            trackId: "video-overlay",
            startFrame: 30,
            durationFrames: 2,
            sourceStartFrame: 0,
            enabled: true,
            transform: {},
            crop: {},
            opacityBp: 10_000,
            blend: "normal",
            text: null,
            transition: { kind: "cut", durationFrames: 0 },
            effect: {},
            audio: IDENTITY_CLIP_AUDIO,
          },
        ],
      }),
    ).toEqual({ admitted: false, reason: "overlap", trackId: null });
    expect(
      admitTimelineInsert({
        asset: video,
        startFrame: 90,
        durationFrames: 24,
        timelineDurationFrames: 96,
        tracks,
        clips: [],
      }),
    ).toEqual({ admitted: false, reason: "bounds", trackId: null });
  });

  it("admits only the explicitly dropped compatible track", () => {
    expect(
      admitTimelineInsert({
        asset: video,
        startFrame: 24,
        durationFrames: 24,
        timelineDurationFrames: 96,
        tracks,
        clips: [],
        targetTrackId: "image-overlay",
      }),
    ).toEqual({
      admitted: false,
      reason: "incompatible_track",
      trackId: null,
    });
    expect(
      admitTimelineInsert({
        asset: video,
        startFrame: 24,
        durationFrames: 24,
        timelineDurationFrames: 96,
        tracks,
        clips: [],
        targetTrackId: "video-overlay",
      }),
    ).toEqual({ admitted: true, reason: null, trackId: "video-overlay" });
  });

  it.each(["insert_range", "overwrite_range"] as const)(
    "%s selects the first compatible unlocked track even when occupied",
    (operation) => {
      const occupied: CompositionClip = {
        clipId: "existing",
        assetId: video.assetId,
        trackId: "video-overlay",
        startFrame: 24,
        durationFrames: 24,
        sourceStartFrame: 0,
        enabled: true,
        transform: {},
        crop: {},
        opacityBp: 10_000,
        blend: "normal",
        text: null,
        transition: { kind: "cut", durationFrames: 0 },
        effect: {},
        audio: IDENTITY_CLIP_AUDIO,
      };
      const proposal = {
        asset: video,
        startFrame: 24,
        durationFrames: 24,
        timelineDurationFrames: 96,
        tracks: [
          ...tracks,
          { ...tracks[1]!, trackId: "later-empty-video", order: 3 },
        ],
        clips: [occupied],
        operation,
      };
      expect(admitTimelineInsert(proposal)).toEqual({
        admitted: true,
        reason: null,
        trackId: "video-overlay",
      });
      expect(
        admitTimelineInsert({ ...proposal, targetTrackId: "locked-primary" }),
      ).toEqual({ admitted: false, reason: "locked_track", trackId: null });
      expect(
        admitTimelineInsert({ ...proposal, targetTrackId: "image-overlay" }),
      ).toEqual({
        admitted: false,
        reason: "incompatible_track",
        trackId: null,
      });
      expect(admitTimelineInsert({ ...proposal, startFrame: 90 })).toEqual({
        admitted: false,
        reason: "bounds",
        trackId: null,
      });
      expect(
        admitTimelineInsert({
          ...proposal,
          operation: "add",
          targetTrackId: "video-overlay",
        }),
      ).toEqual({ admitted: false, reason: "overlap", trackId: null });
    },
  );

  it("refuses range insert inside an undeclared clip split but allows a boundary", () => {
    const occupied: CompositionClip = {
      clipId: "existing",
      assetId: video.assetId,
      trackId: "video-overlay",
      startFrame: 24,
      durationFrames: 24,
      sourceStartFrame: 0,
      enabled: true,
      transform: {},
      crop: {},
      opacityBp: 10_000,
      blend: "normal",
      text: null,
      transition: { kind: "cut", durationFrames: 0 },
      effect: {},
      audio: IDENTITY_CLIP_AUDIO,
    };
    const proposal = {
      asset: video,
      durationFrames: 24,
      timelineDurationFrames: 96,
      tracks: [tracks[1]!],
      clips: [occupied],
    };
    expect(
      admitTimelineInsert({
        ...proposal,
        operation: "insert_range",
        startFrame: 30,
      }),
    ).toEqual({ admitted: false, reason: "overlap", trackId: null });
    expect(
      admitTimelineInsert({
        ...proposal,
        operation: "insert_range",
        startFrame: 48,
      }),
    ).toEqual({ admitted: true, reason: null, trackId: "video-overlay" });
    expect(
      admitTimelineInsert({
        ...proposal,
        operation: "overwrite_range",
        startFrame: 30,
      }),
    ).toEqual({ admitted: true, reason: null, trackId: "video-overlay" });
  });
});

describe("NLE timeline gesture reducer", () => {
  const tracks: readonly CompositionTrack[] = [
    {
      trackId: "primary",
      kind: "primary_video",
      order: 0,
      enabled: true,
      locked: false,
    },
    {
      trackId: "video",
      kind: "video_overlay",
      order: 1,
      enabled: true,
      locked: false,
    },
    {
      trackId: "image",
      kind: "image_overlay",
      order: 2,
      enabled: true,
      locked: false,
    },
    {
      trackId: "text",
      kind: "text_overlay",
      order: 3,
      enabled: true,
      locked: false,
    },
  ];
  const assets: readonly PublicCompositionAsset[] = [
    {
      assetId: "video-a",
      kind: "video",
      sourceTimeBase: null,
      sourceFrameCount: 100,
      sourceSampleCount: null,
      embeddedAudio: "absent",
      timestampPolicy: "not_applicable",
      landmarks: [],
    },
    {
      assetId: "image-a",
      kind: "image",
      sourceTimeBase: null,
      sourceFrameCount: null,
      sourceSampleCount: null,
      embeddedAudio: "absent",
      timestampPolicy: "not_applicable",
      landmarks: [],
    },
  ];
  const clip = (overrides: Partial<CompositionClip> = {}): CompositionClip => ({
    clipId: "moving",
    assetId: "video-a",
    trackId: "video",
    startFrame: 20,
    durationFrames: 10,
    sourceStartFrame: 0,
    enabled: true,
    transform: {},
    crop: {},
    opacityBp: 10_000,
    blend: "normal",
    text: null,
    transition: { kind: "cut", durationFrames: 0 },
    effect: {},
    audio: IDENTITY_CLIP_AUDIO,
    ...overrides,
  });
  const admission = (
    origin: CompositionClip,
    targetTrackId: string,
    deltaFrames: number,
    options: Readonly<{
      tracks?: readonly CompositionTrack[];
      clips?: readonly CompositionClip[];
    }> = {},
  ) =>
    admitTimelineMove({
      origins: [origin],
      targetTrackIds: [targetTrackId],
      deltaFrames,
      durationFrames: 100,
      tracks: options.tracks ?? tracks,
      clips: options.clips ?? [origin],
      assets,
    });

  it("distinguishes every local move admission refusal without mutating inputs", () => {
    const moving = clip();
    expect(admission(moving, "video", 1)).toEqual({
      admitted: true,
      reason: null,
    });
    expect(admission(moving, "missing", 1)).toMatchObject({
      reason: "target_exhaustion",
    });
    expect(
      admission(moving, "video", 1, {
        tracks: tracks.map((track) =>
          track.trackId === "video" ? { ...track, locked: true } : track,
        ),
      }),
    ).toMatchObject({ reason: "locked_track" });
    expect(admission(moving, "primary", 1)).toMatchObject({
      reason: "primary_track",
    });
    expect(admission(moving, "image", 1)).toMatchObject({
      reason: "incompatible_track",
    });
    expect(admission(moving, "video", -21)).toMatchObject({ reason: "bounds" });
    expect(
      admission(moving, "video", 5, {
        clips: [moving, clip({ clipId: "other", startFrame: 32 })],
      }),
    ).toMatchObject({ reason: "overlap" });
    expect(
      admission(moving, "video", 1, {
        tracks: tracks.filter((track) => track.trackId !== "video"),
      }),
    ).toMatchObject({ reason: "source_missing" });
  });
  it("ranks snap candidates by distance, kind, frame and edge with a bounded result", () => {
    expect(
      selectTimelineSnap(
        10,
        20,
        [
          { frame: 21, kind: "grid" },
          { frame: 9, kind: "playhead" },
          { frame: 9, kind: "clip_end" },
          { frame: 9, kind: "clip_start" },
        ],
        1,
      ),
    ).toEqual({ deltaFrames: -1, lineFrame: 9, kind: "clip_start" });
    expect(
      selectTimelineSnap(10, 20, [{ frame: 30, kind: "grid" }], 1),
    ).toBeNull();
    expect(
      selectTimelineSnap(10, 20, [{ frame: 10, kind: "grid" }], 1, 8, 0),
    ).toBeNull();
  });

  it("B-M2561-08: a click-only draft keeps its owner when its first origin is virtualized", () => {
    // The 33-member refusal was cancelled as `owner_removed` whenever the selection walk had
    // scrolled the first origin out of the mounted window. The move panel owns a click draft.
    const origins = Array.from({ length: 33 }, (_, index) => ({
      clipId: `clip-${index}`,
      trackId: "track-1",
      startFrame: index * 10,
    }));
    const begin = (input: "pointer" | "keyboard" | "click") =>
      reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
        type: "begin_move",
        input,
        identity,
        pointerId: input === "pointer" ? 3 : null,
        originClientX: 0,
        originClientY: 0,
        pixelsPerFrame: 1,
        origins,
        trackIds: ["track-1"],
        targetTrackIds: origins.map((origin) => origin.trackId),
      });
    const unmounted = new Set(["clip-32"]);
    const mounted = new Set(["clip-0"]);
    expect(begin("click").phase).toBe("keyboard_draft");
    expect(draftOwnerPresent(begin("click"), unmounted)).toBe(true);
    // Pointer and keyboard drafts are still owned by the first origin's element.
    expect(draftOwnerPresent(begin("keyboard"), unmounted)).toBe(false);
    expect(draftOwnerPresent(begin("pointer"), unmounted)).toBe(false);
    expect(draftOwnerPresent(begin("keyboard"), mounted)).toBe(true);
    expect(draftOwnerPresent(NLE_GESTURE_IDLE, unmounted)).toBe(true);
  });

  it("commits a move exactly once from its captured origin", () => {
    let state = reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
      type: "begin_move",
      input: "pointer",
      identity,
      pointerId: 7,
      originClientX: 100,
      originClientY: 50,
      pixelsPerFrame: 2,
      origins: [{ clipId: "clip-a", trackId: "track-1", startFrame: 20 }],
      trackIds: ["track-1", "track-2"],
      targetTrackIds: ["track-1"],
    });
    state = reduceNleTimelineGesture(state, {
      type: "move",
      clientX: 111,
      clientY: 50,
      targetTrackIds: ["track-1"],
      snap: null,
    });
    state = reduceNleTimelineGesture(state, {
      type: "release",
      requestId: "request-1",
    });
    expect(state.phase).toBe("submitting");
    expect(commandForGesture(state)).toEqual({
      kind: "move_clip",
      clipId: "clip-a",
      deltaFrames: 6,
      targetTrackId: "track-1",
    });
    expect(
      reduceNleTimelineGesture(state, {
        type: "release",
        requestId: "request-2",
      }),
    ).toBe(state);
  });

  it("commits one insert-from-bin only after threshold and accepted release", () => {
    const insertIdentity = { ...identity, clipIds: [] };
    let state = reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
      type: "begin_insert_from_bin",
      identity: insertIdentity,
      pointerId: 17,
      originClientX: 10,
      originClientY: 20,
      pixelsPerFrame: 2,
      assetId: "private-video-id",
      durationFrames: 48,
    });
    state = reduceNleTimelineGesture(state, {
      type: "update_insert_from_bin",
      clientX: 40,
      clientY: 80,
      targetFrame: 72,
      targetTrackId: "video-overlay",
      durationFrames: 24,
      admitted: true,
      reason: null,
    });
    state = reduceNleTimelineGesture(state, {
      type: "release",
      requestId: "insert-17",
    });
    expect(commandForGesture(state)).toEqual({
      kind: "insert_from_bin",
      assetId: "private-video-id",
      startFrame: 72,
      durationFrames: 24,
      targetTrackId: "video-overlay",
    });
    expect(
      reduceNleTimelineGesture(state, {
        type: "release",
        requestId: "duplicate",
      }),
    ).toBe(state);

    let click = reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
      type: "begin_insert_from_bin",
      identity: insertIdentity,
      pointerId: 18,
      originClientX: 10,
      originClientY: 20,
      pixelsPerFrame: 2,
      assetId: "private-video-id",
      durationFrames: 48,
    });
    click = reduceNleTimelineGesture(click, {
      type: "update_insert_from_bin",
      clientX: 12,
      clientY: 21,
      targetFrame: 10,
      targetTrackId: "video-overlay",
      durationFrames: 48,
      admitted: true,
      reason: null,
    });
    expect(
      reduceNleTimelineGesture(click, {
        type: "release",
        requestId: "below-threshold",
      }),
    ).toBe(NLE_GESTURE_IDLE);
  });

  it("keeps authority byte-identical during controller auto-scroll reanchor", () => {
    const started = reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
      type: "begin_scrub",
      input: "pointer",
      identity,
      pointerId: 3,
      originClientX: 20,
      pixelsPerFrame: 1,
      originFrame: 10,
    });
    const authority = JSON.stringify(started);
    const reanchored = reduceNleTimelineGesture(started, {
      type: "auto_scroll_reanchor",
      mappingKey: "1:4:1000",
      originClientX: 16,
      originFrame: 14,
    });
    expect(
      JSON.stringify(reanchored).replace("1:4:1000", "0.5:0:1000"),
    ).toContain(JSON.parse(authority).draft.identity.timelineFingerprint);
    expect(
      (reanchored as { draft: { identity: unknown } }).draft.identity,
    ).toBe(identity);
  });

  it("keeps a sub-four-pixel pointer activation as a click", () => {
    let state = reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
      type: "begin_move",
      input: "pointer",
      identity,
      pointerId: 9,
      originClientX: 100,
      originClientY: 50,
      pixelsPerFrame: 0.25,
      origins: [{ clipId: "clip-a", trackId: "track-1", startFrame: 20 }],
      trackIds: ["track-1"],
      targetTrackIds: ["track-1"],
    });
    state = reduceNleTimelineGesture(state, {
      type: "move",
      clientX: 103,
      clientY: 50,
      targetTrackIds: ["track-1"],
      snap: null,
    });
    expect(
      reduceNleTimelineGesture(state, { type: "release", requestId: "click" }),
    ).toBe(NLE_GESTURE_IDLE);
  });

  it("cancels drafts on external mapping changes and reconciles lost responses", () => {
    const started = reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
      type: "begin_scrub",
      input: "pointer",
      identity,
      pointerId: 3,
      originClientX: 20,
      pixelsPerFrame: 1,
      originFrame: 10,
    });
    expect(
      reduceNleTimelineGesture(started, {
        type: "mapping_changed",
        mappingKey: "changed",
      }),
    ).toBe(NLE_GESTURE_IDLE);

    let moved = reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
      type: "begin_move",
      input: "keyboard",
      identity,
      pointerId: null,
      originClientX: 0,
      originClientY: 0,
      pixelsPerFrame: 1,
      origins: [{ clipId: "clip-a", trackId: "track-1", startFrame: 20 }],
      trackIds: ["track-1"],
      targetTrackIds: ["track-1"],
    });
    moved = reduceNleTimelineGesture(moved, { type: "step", deltaFrames: 1 });
    moved = reduceNleTimelineGesture(moved, {
      type: "commit",
      requestId: "request-2",
    });
    moved = reduceNleTimelineGesture(moved, {
      type: "response_lost",
      reason: "timeout",
    });
    expect(moved.phase).toBe("reconciling");
  });

  it("admits 32 group members atomically and refuses 33 without a command", () => {
    const begin = (count: number) =>
      reduceNleTimelineGesture(NLE_GESTURE_IDLE, {
        type: "begin_move",
        input: "keyboard",
        identity: {
          ...identity,
          clipIds: Array.from({ length: count }, (_, index) => `clip-${index}`),
        },
        pointerId: null,
        originClientX: 0,
        originClientY: 0,
        pixelsPerFrame: 1,
        origins: Array.from({ length: count }, (_, index) => ({
          clipId: `clip-${index}`,
          trackId: `track-${index % 4}`,
          startFrame: index * 10,
        })),
        trackIds: ["track-0", "track-1", "track-2", "track-3"],
        targetTrackIds: Array.from(
          { length: count },
          (_, index) => `track-${index % 4}`,
        ),
      });
    const admitted = reduceNleTimelineGesture(begin(32), {
      type: "step",
      deltaFrames: 1,
    });
    const committed = reduceNleTimelineGesture(admitted, {
      type: "commit",
      requestId: "group-32",
    });
    expect(commandForGesture(committed)).toMatchObject({
      kind: "move_group",
      clipIds: expect.arrayContaining(["clip-0", "clip-31"]),
      deltaFrames: 1,
    });

    const refused = begin(33);
    expect(refused).toMatchObject({
      phase: "keyboard_draft",
      draft: { admitted: false, reason: "group_limit" },
    });
    expect(
      reduceNleTimelineGesture(refused, {
        type: "commit",
        requestId: "group-33",
      }),
    ).toBe(NLE_GESTURE_IDLE);
    expect(commandForGesture(refused)).toBeNull();
  });
});
