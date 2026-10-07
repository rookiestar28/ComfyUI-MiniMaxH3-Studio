import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import { createEmbeddedAudioFollower } from "../src/runtime/embeddedAudioFollower";
import {
  evaluateMediaCapabilities,
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
} from "../src/runtime/mediaCapabilities";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import { resolveCompositionScene } from "../src/runtime/sceneResolver";
import {
  createVisualCompositionSession,
  type VisualCompositionSession,
} from "../src/runtime/visualCompositionSession";
import { runtimeContractAdmission } from "./fixtures/browserNleRuntimeFixture";

// The picture of an incoming clip must start with its audio. The audio of the successor is
// scheduled ahead on the AudioContext clock, so whatever the picture waits for at the cut -- a
// source acquisition, a lease, a decode -- is heard as the picture trailing the sound. This suite
// runs the real scheduler, audio follower, editor runtime and resource layer over fake media in
// which every acquisition takes a set wall time, and reads when the incoming owner's native
// playback begins relative to the cut. Only the painter is inert.
vi.mock("../src/runtime/visualCompositor", () => ({
  createVisualCompositor: (
    _canvas: HTMLCanvasElement,
    snapshot: { publicFingerprint: string; profileId: string },
  ) => {
    const receipt = (frame: number | null, generation: number) =>
      Object.freeze({
        schema: "h3.visual_compositor_receipt.v1",
        status: frame === null ? "unavailable" : "presented",
        profileId: snapshot.profileId,
        publicFingerprint: snapshot.publicFingerprint,
        frame,
        generation,
        previewWidth: 320,
        previewHeight: 180,
        renderedLayerCount: 1,
        blocker: null,
        browserPreviewOnly: true,
      });
    return {
      resize: () => receipt(null, 0),
      path: () => "native" as const,
      render: (
        scene: { frame: number },
        _resources: unknown,
        generation: number,
      ) => receipt(scene.frame, generation),
      layerGeometry: () => null,
      clear: (generation = 0) => receipt(null, generation),
      close: () => receipt(null, 0),
    };
  },
}));

const CLIP_FRAMES = 48;
const CLIP_SAMPLES = CLIP_FRAMES * 2_000;
const FRAME_MS = 1000 / 24;
const PRIMARY_IDS = ["clip-main", "clip-next", "clip-third"] as const;
const OVERLAY_ID = "clip-video-overlay";
const sessions: VisualCompositionSession[] = [];
afterEach(async () => {
  await Promise.all(sessions.splice(0).map((session) => session.close()));
});

function pcmWav(samples: number, channels = 2) {
  const dataBytes = samples * channels * 2;
  const body = new ArrayBuffer(44 + dataBytes);
  const bytes = new Uint8Array(body);
  const view = new DataView(body);
  const ascii = (offset: number, value: string) =>
    [...value].forEach((character, index) => {
      bytes[offset + index] = character.charCodeAt(0);
    });
  ascii(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, 48_000, true);
  view.setUint32(28, 48_000 * channels * 2, true);
  view.setUint16(32, channels * 2, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, dataBytes, true);
  return new Blob([body], { type: "audio/wav" });
}

function fakeAudioContext() {
  const nodes: Array<{
    start: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
  }> = [];
  const context = {
    currentTime: 1,
    destination: {},
    state: "running",
    resume: vi.fn(async () => undefined),
    close: vi.fn(async () => undefined),
    decodeAudioData: vi.fn(async () => ({
      sampleRate: 48_000,
      numberOfChannels: 2,
      length: CLIP_SAMPLES,
      duration: CLIP_SAMPLES / 48_000,
    })),
    createBufferSource: vi.fn(() => {
      const node = {
        buffer: null,
        connect: vi.fn(),
        disconnect: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
        onended: null as (() => void) | null,
      };
      nodes.push(node);
      return node;
    }),
    createGain: vi.fn(() => ({
      gain: { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() },
      connect: vi.fn(),
      disconnect: vi.fn(),
    })),
  };
  return { context: context as unknown as AudioContext, nodes };
}

type Shape = Readonly<{
  /** Adjacent primary clips of one audible source, 48 frames each. */
  primaryClips: 2 | 3;
  /** Wall time one video source acquisition takes. */
  videoMs: number;
  /** Wall time one audio preview acquisition takes. */
  audioMs: number;
  /** A video overlay that spans the whole timeline (a second active owner). */
  overlay?: boolean;
  /** Owners whose next video acquisition is refused once. */
  refuseOnce?: readonly string[];
}>;

function preview(shape: Shape) {
  const totalFrames = CLIP_FRAMES * shape.primaryClips;
  const landmarks = (frames: number) =>
    Array.from({ length: frames }, (_, frame) => ({
      frame_index: frame,
      pts: frame * 512,
      dts: frame * 512,
      duration_ticks: 512,
    }));
  const wire = structuredClone(fixture.snapshot);
  wire.output.duration_frames = totalFrames;
  wire.assets = shape.overlay
    ? [wire.assets[0]!, wire.assets[1]!]
    : [wire.assets[0]!];
  wire.assets[0]!.embedded_audio = "present_bound";
  wire.assets[0]!.source_sample_count = CLIP_SAMPLES;
  wire.assets[0]!.landmarks = landmarks(CLIP_FRAMES);
  const first = wire.clips[0]!;
  const overlay = wire.clips[1]!;
  first.duration_frames = CLIP_FRAMES;
  wire.clips = PRIMARY_IDS.slice(0, shape.primaryClips).map(
    (clipId, index) => ({
      ...structuredClone(first),
      clip_id: clipId,
      start_frame: index * CLIP_FRAMES,
    }),
  );
  if (shape.overlay) {
    wire.assets[1]!.source_frame_count = totalFrames;
    wire.assets[1]!.landmarks = landmarks(totalFrames);
    wire.clips.push({
      ...structuredClone(overlay),
      clip_id: OVERLAY_ID,
      start_frame: 0,
      duration_frames: totalFrames,
      transition: { kind: "none", duration_frames: 0 },
    });
  }
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  const snapshot = decodePublicCompositionSnapshot(wire);
  const admission = runtimeContractAdmission();
  const capability = evaluateMediaCapabilities(
    {
      schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      htmlMediaElement: true,
      canPlayMp4H264Aac: "probably",
      requestVideoFrameCallback: true,
      seekedEvent: true,
      timeupdateEvent: true,
      canvas2d: true,
      crossOriginIsolated: false,
    },
    admission.qualification,
    admission.authority,
  );

  let milliseconds = 0;
  let callback: FrameRequestCallback | undefined;
  // Acquisitions settle on the hand-driven clock, never on real time.
  let waiting: Array<{ due: number; resolve: () => void }> = [];
  const after = (duration: number) =>
    duration <= 0
      ? Promise.resolve()
      : new Promise<void>((resolve) => {
          waiting.push({ due: milliseconds + duration, resolve });
        });
  // The session's own timer (the step armed for an ownership change), on the same clock.
  let timers: Array<{ due: number; callback: () => void; live: boolean }> = [];
  const settleDue = () => {
    const due = waiting.filter((row) => row.due <= milliseconds);
    waiting = waiting.filter((row) => row.due > milliseconds);
    for (const row of due) row.resolve();
    const fired = timers.filter((row) => row.live && row.due <= milliseconds);
    timers = timers.filter((row) => row.live && row.due > milliseconds);
    for (const row of fired) row.callback();
  };
  // The lease route admits one acquisition at a time: a second one in flight is refused there.
  let inFlight = 0;
  let maximumInFlight = 0;
  const acquisition = async <T>(duration: number, result: () => T) => {
    inFlight += 1;
    maximumInFlight = Math.max(maximumInFlight, inFlight);
    try {
      await after(duration);
      return result();
    } finally {
      inFlight -= 1;
    }
  };

  const elements = new Map<string, HTMLVideoElement>();
  const playedAt = new Map<string, number>();
  const requestedAt = new Map<string, number[]>();
  const releases = new Map<string, ReturnType<typeof vi.fn>[]>();
  const refusals = new Set(shape.refuseOnce ?? []);
  const nativeVideo = (ownerId: string) => {
    const element = new EventTarget() as HTMLVideoElement;
    let time = 0;
    let paused = true;
    Object.assign(element, {
      duration: totalFrames / 24,
      videoWidth: 320,
      videoHeight: 180,
      src: "blob:synthetic-video",
      seeking: false,
      muted: false,
      controls: true,
      error: null,
      play: vi.fn(async () => {
        paused = false;
        if (!playedAt.has(ownerId)) playedAt.set(ownerId, milliseconds);
      }),
      pause: vi.fn(() => {
        paused = true;
      }),
      load: vi.fn(),
      removeAttribute: vi.fn(),
    });
    Object.defineProperties(element, {
      paused: { get: () => paused },
      currentTime: {
        get: () => time,
        set: (value: number) => {
          time = value;
          queueMicrotask(() => element.dispatchEvent(new Event("seeked")));
        },
      },
    });
    return element;
  };
  const audio = fakeAudioContext();
  const acquireVideoSource = vi.fn((request: { ownerId: string }) => {
    requestedAt.set(request.ownerId, [
      ...(requestedAt.get(request.ownerId) ?? []),
      milliseconds,
    ]);
    return acquisition(shape.videoMs, () => {
      if (refusals.delete(request.ownerId))
        throw new Error("synthetic_source_refused");
      const element = nativeVideo(request.ownerId);
      elements.set(request.ownerId, element);
      const release = vi.fn(async () => undefined);
      releases.set(request.ownerId, [
        ...(releases.get(request.ownerId) ?? []),
        release,
      ]);
      return {
        element,
        ...(request.ownerId === OVERLAY_ID
          ? {}
          : { audioBody: pcmWav(CLIP_SAMPLES) }),
        geometry: {
          schema: "h3.authoring.media_geometry.v1",
          sourceWidth: 320,
          sourceHeight: 180,
          derivativeWidth: 320,
          derivativeHeight: 180,
        },
        release,
      };
    });
  });
  const acquireAudioPreview = vi.fn(() =>
    acquisition(shape.audioMs, () => ({
      audioBody: pcmWav(CLIP_SAMPLES),
      release: vi.fn(async () => undefined),
    })),
  );
  const leaseClient = {
    create: vi.fn(),
    close: vi.fn(async () => undefined),
    acquireVideoSource,
    acquireAudioPreview,
  } as unknown as AuthoringMediaSourceLeaseClient;

  // The binding's own resolver: the pure port the product monitor is bound to.
  const resolveScene = async (frame: number) =>
    resolveCompositionScene(snapshot, frame);

  const canvas = document.createElement("canvas");
  const session = createVisualCompositionSession({
    snapshot,
    manifest: buildPublicAssetManifest(snapshot),
    capability,
    leaseClient,
    resolveScene,
    canvas,
    runtimeFactory: (options) =>
      createEmbeddedAudioFollower({
        ...options,
        transportOptions: { frameObserver: "event_fallback" },
        createAudioContext: () => audio.context,
      }).runtime,
    now: () => milliseconds,
    requestFrame: (next: FrameRequestCallback) => {
      callback = next;
      return 1;
    },
    cancelFrame: () => {
      callback = undefined;
    },
    setTimer: (step: () => void, delayMs: number) => {
      const row = { due: milliseconds + delayMs, callback: step, live: true };
      timers.push(row);
      return row;
    },
    clearTimer: (handle: unknown) => {
      (handle as { live: boolean }).live = false;
    },
  });
  sessions.push(session);

  /**
   * Let every promise chain that is ready run. A macrotask turn, not a timer: a zero timeout is
   * a 15 ms wait on Windows and this runs a few hundred times per case.
   */
  const settle = async () => {
    for (let turn = 0; turn < 4; turn += 1)
      await new Promise((resolve) => setImmediate(resolve));
  };
  let playStart = 0;
  const h = {
    session,
    audio,
    elements,
    acquireVideoSource,
    acquireAudioPreview,
    releases,
    /** The wall time of an output frame's start, on the clock playback started on. */
    wallOf: (outputFrame: number) => playStart + outputFrame * FRAME_MS,
    playedAt: (ownerId: string) => playedAt.get(ownerId),
    requestedAt: (ownerId: string) => requestedAt.get(ownerId) ?? [],
    maximumInFlight: () => maximumInFlight,
    /** Run a transport call to completion while the wall clock runs under it. */
    async during(task: Promise<unknown>) {
      let done = false;
      void task.then(
        () => {
          done = true;
        },
        () => {
          done = true;
        },
      );
      await settle();
      while (!done) {
        milliseconds += FRAME_MS;
        settleDue();
        await settle();
      }
    },
    async open() {
      await h.during(session.open());
    },
    async play() {
      await h.during(session.play());
      playStart = milliseconds;
    },
    /**
     * One display frame with the wall clock on an output frame. Every live owner's native clock
     * is on the source frame that output frame shows, as a healthy decoder's would be.
     */
    async display(outputFrame: number, intoFrameMs = 0.01) {
      milliseconds = h.wallOf(outputFrame) + intoFrameMs;
      settleDue();
      await settle();
      for (const clip of wire.clips) {
        const element = elements.get(clip.clip_id);
        if (element === undefined || element.paused) continue;
        const sourceFrame = Math.min(
          outputFrame - clip.start_frame,
          clip.duration_frames - 1,
        );
        if (sourceFrame < 0) continue;
        element.currentTime = sourceFrame / 24;
        element.dispatchEvent(new Event("timeupdate"));
      }
      callback?.(milliseconds);
      await settle();
    },
    async playThrough(fromFrame: number, toFrame: number) {
      for (let frame = fromFrame; frame <= toFrame; frame += 1)
        await h.display(frame);
    },
    /** Let the wall clock reach a time with no display tick in between. */
    async idleUntil(wallMilliseconds: number) {
      milliseconds = wallMilliseconds;
      settleDue();
      await settle();
    },
  };
  return h;
}

describe("the incoming clip's picture starts with its audio at an adjacent cut", () => {
  it("starts the incoming owner within one frame of the cut when a source acquisition takes half a second", async () => {
    const h = preview({ primaryClips: 2, videoMs: 500, audioMs: 0 });
    await h.open();
    await h.play();
    await h.playThrough(1, 60);

    expect(h.session.getSnapshot().status).toBe("playing");
    expect(h.session.getPresentation()?.frame).toBeGreaterThanOrEqual(48);
    const late = h.playedAt("clip-next")! - h.wallOf(48);
    expect(late).toBeLessThanOrEqual(FRAME_MS);
    // The successor scheduled at play is the audio that plays: not stopped, not replaced.
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
    expect(h.maximumInFlight()).toBe(1);
  });

  it("starts the incoming owner at the cut's wall time, not at the display tick after it", async () => {
    const h = preview({ primaryClips: 2, videoMs: 500, audioMs: 0 });
    await h.open();
    await h.play();
    await h.playThrough(1, 47);
    // The last display tick before the cut lands 25 ms into frame 47: the cut is 16.7 ms away.
    await h.display(47, 25);
    expect(h.playedAt("clip-next")).toBeUndefined();

    // The cut's wall time passes. The next display tick is still to come.
    await h.idleUntil(h.wallOf(48) + 1);
    expect(h.session.getSnapshot().status).toBe("playing");
    expect(h.session.getPresentation()?.frame).toBe(48);
    expect(h.playedAt("clip-next")! - h.wallOf(48)).toBeLessThanOrEqual(2);
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
  });

  it("does not wait for the next successor's audio lease at a cut in a three-clip sequence", async () => {
    const h = preview({ primaryClips: 3, videoMs: 0, audioMs: 500 });
    await h.open();
    await h.play();
    await h.playThrough(1, 60);

    expect(h.session.getSnapshot().status).toBe("playing");
    const late = h.playedAt("clip-next")! - h.wallOf(48);
    expect(late).toBeLessThanOrEqual(FRAME_MS);
    // The third clip's audio is chained from the second's end on the same AudioContext clock,
    // long before the second cut.
    expect(h.audio.nodes).toHaveLength(3);
    expect(h.audio.nodes[2]!.start).toHaveBeenCalledWith(5, 0, 2);

    await h.playThrough(61, 108);
    expect(h.session.getSnapshot().status).toBe("playing");
    expect(h.playedAt("clip-third")! - h.wallOf(96)).toBeLessThanOrEqual(
      FRAME_MS,
    );
    expect(h.audio.nodes).toHaveLength(3);
    expect(h.maximumInFlight()).toBe(1);
  });

  it("hands over as before when a second active owner leaves no room to prepare", async () => {
    const h = preview({
      primaryClips: 2,
      videoMs: 500,
      audioMs: 0,
      overlay: true,
    });
    await h.open();
    await h.play();
    await h.playThrough(1, 72);

    // Two owners are admitted in all, so the successor is acquired only once the outgoing
    // owner has been released at the cut. The handoff completes; nothing is blocked.
    expect(h.session.getSnapshot().status).toBe("playing");
    expect(h.session.getSnapshot().blocker).toBeNull();
    expect(h.session.getPresentation()?.frame).toBeGreaterThanOrEqual(48);
    expect(h.requestedAt("clip-next")).toHaveLength(1);
    expect(h.requestedAt("clip-next")[0]).toBeGreaterThanOrEqual(h.wallOf(48));
    expect(h.playedAt("clip-next")).toBeDefined();
    expect(h.maximumInFlight()).toBe(1);
  });

  it("keeps playing when the preparation is refused, and acquires at the cut", async () => {
    const h = preview({
      primaryClips: 2,
      videoMs: 500,
      audioMs: 0,
      refuseOnce: ["clip-next"],
    });
    await h.open();
    await h.play();
    await h.playThrough(1, 40);
    expect(h.session.getSnapshot().status).toBe("playing");
    expect(h.session.getSnapshot().blocker).toBeNull();

    await h.playThrough(41, 72);
    expect(h.session.getSnapshot().status).toBe("playing");
    expect(h.session.getPresentation()?.frame).toBeGreaterThanOrEqual(48);
    expect(h.playedAt("clip-next")).toBeDefined();
    expect(h.maximumInFlight()).toBe(1);
  });

  it("releases the prepared owner on a user pause and leaves no owner after close", async () => {
    const h = preview({ primaryClips: 2, videoMs: 500, audioMs: 0 });
    await h.open();
    await h.play();
    await h.playThrough(1, 24);
    // The successor was acquired ahead of the cut and is held, paused.
    expect(h.requestedAt("clip-next")).toHaveLength(1);
    expect(h.elements.get("clip-next")).toBeDefined();
    expect(h.session.getResources().videoOwners).toBe(2);

    await h.during(h.session.pause());
    await vi.waitFor(() =>
      expect(h.releases.get("clip-next")![0]).toHaveBeenCalledTimes(1),
    );
    expect(h.session.getSnapshot().status).toBe("paused");
    expect(h.session.getResources().videoOwners).toBe(1);

    await h.during(h.session.close());
    expect(h.session.getResources().videoOwners).toBe(0);
    expect(h.releases.get("clip-main")![0]).toHaveBeenCalledTimes(1);
  });

  it("lets a preparation in flight finish before a seek opens another owner", async () => {
    const h = preview({ primaryClips: 3, videoMs: 500, audioMs: 0 });
    await h.open();
    await h.play();
    await h.playThrough(1, 4);
    // The second clip is being acquired; the user seeks into the third.
    expect(h.requestedAt("clip-next")).toHaveLength(1);
    expect(h.elements.get("clip-next")).toBeUndefined();

    await h.during(h.session.seek(100));
    expect(h.session.getPresentation()?.frame).toBe(100);
    expect(h.session.getSnapshot().blocker).toBeNull();
    // Never two acquisitions at once, and the abandoned preparation was released, not leaked.
    expect(h.maximumInFlight()).toBe(1);
    expect(h.requestedAt("clip-third")[0]).toBeGreaterThanOrEqual(
      h.requestedAt("clip-next")[0]! + 500,
    );
    await vi.waitFor(() =>
      expect(h.releases.get("clip-next")![0]).toHaveBeenCalledTimes(1),
    );
  });
});
