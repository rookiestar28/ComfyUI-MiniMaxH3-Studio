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
import {
  createVisualCompositionSession,
  type VisualCompositionSession,
} from "../src/runtime/visualCompositionSession";
import { runtimeContractAdmission } from "./fixtures/browserNleRuntimeFixture";

// The scheduler, the audio follower and the editor runtime are each pinned in their own suite
// against doubles of the other two. This suite runs the three real ones together over fake media,
// because the defect it guards was an agreement between layers that no single-layer double holds:
// which frame the automatic boundary move goes to. Only the painter is inert.
vi.mock("../src/runtime/visualCompositor", () => ({
  createVisualCompositor: (
    canvas: HTMLCanvasElement,
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
      ) => {
        canvas.dataset.paintTrace = [
          ...(canvas.dataset.paintTrace ?? "").split(",").filter(Boolean),
          String(scene.frame),
        ].join(",");
        return receipt(scene.frame, generation);
      },
      layerGeometry: () => null,
      clear: (generation = 0) => receipt(null, generation),
      close: () => receipt(null, 0),
    };
  },
}));

const CLIP_FRAMES = 48;
const CLIP_SAMPLES = CLIP_FRAMES * 2_000;
const sessions: VisualCompositionSession[] = [];
afterEach(async () => {
  await Promise.all(sessions.splice(0).map((session) => session.close()));
});

function nativeVideo() {
  const element = new EventTarget() as HTMLVideoElement;
  let time = 0;
  let paused = true;
  Object.assign(element, {
    duration: CLIP_FRAMES / 24,
    videoWidth: 320,
    videoHeight: 180,
    src: "blob:synthetic-video",
    seeking: false,
    muted: false,
    controls: true,
    error: null,
    play: vi.fn(async () => {
      paused = false;
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
}

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
    connect: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
  }> = [];
  const gains: Array<{
    gain: {
      setValueAtTime: ReturnType<typeof vi.fn>;
      linearRampToValueAtTime: ReturnType<typeof vi.fn>;
    };
    connect: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
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
    createGain: vi.fn(() => {
      const gain = {
        gain: { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() },
        connect: vi.fn(),
        disconnect: vi.fn(),
      };
      gains.push(gain);
      return gain;
    }),
  };
  return { context: context as unknown as AudioContext, nodes, gains };
}

/** A gain's scheduled events in call order, as `[method, value, time]`. */
function gainEvents(
  param: ReturnType<typeof fakeAudioContext>["gains"][number]["gain"],
) {
  const events = (method: "set" | "ramp", mock: ReturnType<typeof vi.fn>) =>
    mock.mock.calls.map((call, index) => ({
      order: mock.mock.invocationCallOrder[index]!,
      event: [method, ...call],
    }));
  return [
    ...events("set", param.setValueAtTime),
    ...events("ramp", param.linearRampToValueAtTime),
  ]
    .sort((left, right) => left.order - right.order)
    .map(({ event }) => event);
}

/**
 * Two primary clips of one audible source meeting at frame 48. `clipAudio` sets each clip's audio
 * member on the wire (absent when not given).
 */
function adjacentCutPreview(
  clipAudio: Readonly<{ main?: object; next?: object }> = {},
) {
  const wire = structuredClone(fixture.snapshot);
  wire.output.duration_frames = CLIP_FRAMES * 2;
  wire.assets = [wire.assets[0]!];
  wire.assets[0]!.embedded_audio = "present_bound";
  wire.assets[0]!.source_sample_count = CLIP_SAMPLES;
  wire.assets[0]!.landmarks = Array.from(
    { length: CLIP_FRAMES },
    (_, frame) => ({
      frame_index: frame,
      pts: frame * 512,
      dts: frame * 512,
      duration_ticks: 512,
    }),
  );
  const first = wire.clips[0]!;
  first.duration_frames = CLIP_FRAMES;
  wire.clips = [
    first,
    {
      ...structuredClone(first),
      clip_id: "clip-next",
      start_frame: CLIP_FRAMES,
    },
  ];
  if (clipAudio.main !== undefined)
    Object.assign(wire.clips[0]!, { audio: clipAudio.main });
  if (clipAudio.next !== undefined)
    Object.assign(wire.clips[1]!, { audio: clipAudio.next });
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
  const elements = new Map<string, HTMLVideoElement>();
  const audio = fakeAudioContext();
  const acquireVideoSource = vi.fn(async (request: { ownerId: string }) => {
    const element = nativeVideo();
    elements.set(request.ownerId, element);
    return {
      element,
      audioBody: pcmWav(CLIP_SAMPLES),
      geometry: {
        schema: "h3.authoring.media_geometry.v1",
        sourceWidth: 320,
        sourceHeight: 180,
        derivativeWidth: 320,
        derivativeHeight: 180,
      },
      release: vi.fn(async () => undefined),
    };
  });
  const leaseClient = {
    create: vi.fn(),
    close: vi.fn(async () => undefined),
    acquireVideoSource,
    acquireAudioPreview: vi.fn(async () => ({
      audioBody: pcmWav(CLIP_SAMPLES),
      release: vi.fn(async () => undefined),
    })),
  } as unknown as AuthoringMediaSourceLeaseClient;
  const resolveScene = async (frame: number) => {
    const clip = wire.clips.find(
      (row) =>
        row.start_frame <= frame &&
        frame < row.start_frame + row.duration_frames,
    )!;
    const sourceFrame = frame - clip.start_frame;
    return {
      schema: "h3.context.resolved_scene.v1",
      profile_id: snapshot.profileId,
      public_fingerprint: snapshot.publicFingerprint,
      frame,
      layers: [
        {
          clip_id: clip.clip_id,
          asset_id: clip.asset_id,
          track_id: clip.track_id,
          source_frame: sourceFrame,
          source_pts: sourceFrame * 512,
          transition_elapsed_frames: null,
          operation_ids: [
            "SelectSourceRangeV1",
            "CropV1",
            "Transform2DV1",
            "OpacityV1",
            "BlendV1",
          ],
          transform: clip.transform,
          crop: clip.crop,
          opacity_bp: clip.opacity_bp,
          blend: clip.blend,
          text: null,
          effect: clip.effect,
        },
      ],
      audio_span: {
        clip_id: clip.clip_id,
        asset_id: clip.asset_id,
        output_start_sample: frame * 2_000,
        output_end_sample: (frame + 1) * 2_000,
        source_start_sample: sourceFrame * 2_000,
        source_end_sample: (sourceFrame + 1) * 2_000,
      },
      blockers: [],
    };
  };
  let milliseconds = 0;
  let callback: FrameRequestCallback | undefined;
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
  });
  sessions.push(session);
  return {
    session,
    canvas,
    audio,
    elements,
    /** The native clock of one owner reaches a frame of its source. */
    nativeFrame(ownerId: string, sourceFrame: number) {
      const element = elements.get(ownerId)!;
      element.currentTime = sourceFrame / 24;
      element.dispatchEvent(new Event("timeupdate"));
    },
    /** One display frame, with the wall clock on an output frame. */
    display(outputFrame: number) {
      milliseconds = (outputFrame * 1000) / 24 + 0.01;
      callback?.(milliseconds);
    },
  };
}

describe("preview audio stays scheduled across an adjacent cut", () => {
  it("keeps the scheduled successor when the wall clock crosses the cut before the outgoing last frame was presented", async () => {
    const h = adjacentCutPreview();
    await h.session.open();
    await h.session.play();
    expect(h.session.getSnapshot().status).toBe("playing");
    // The outgoing clip and its successor are both scheduled on the AudioContext clock at play.
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[0]!.start).toHaveBeenCalledWith(1, 0, 2);
    expect(h.audio.nodes[1]!.start).toHaveBeenCalledWith(3, 0, 2);

    h.nativeFrame("clip-main", 46);
    h.display(46);
    await vi.waitFor(() => expect(h.session.getPresentation()?.frame).toBe(46));

    // The native clock reaches the outgoing clip's last frame, but the next display frame
    // arrives with the wall clock already on the cut: frame 47 is never presented.
    h.nativeFrame("clip-main", 47);
    h.display(48);
    // The wall clock keeps running until the cut is presented, however many moves that takes.
    let wallFrame = 48;
    await vi.waitFor(() => {
      h.display(++wallFrame);
      expect(h.session.getPresentation()?.frame).toBe(48);
    });
    await vi.waitFor(() =>
      expect(h.elements.get("clip-next")!.play).toHaveBeenCalledTimes(1),
    );

    // The successor scheduled at play is still the one that plays: not stopped, not replaced.
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
    // The move was one ownership-changing seek; the ended owner was never sought again.
    expect(h.canvas.dataset.paintTrace?.split(",").slice(-2)).toEqual([
      "46",
      "48",
    ]);
    expect(h.session.getSnapshot().status).toBe("playing");
  });

  it("keeps the scheduled successor when the outgoing last frame was presented first", async () => {
    const h = adjacentCutPreview();
    await h.session.open();
    await h.session.play();
    h.nativeFrame("clip-main", 47);
    h.display(47);
    await vi.waitFor(() => expect(h.session.getPresentation()?.frame).toBe(47));
    h.display(48);
    await vi.waitFor(() => expect(h.session.getPresentation()?.frame).toBe(48));
    await vi.waitFor(() =>
      expect(h.elements.get("clip-next")!.play).toHaveBeenCalledTimes(1),
    );
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
    expect(h.session.getSnapshot().status).toBe("playing");
  });

  it("schedules an adjusted successor's envelope from the cut and releases the outgoing gain with its source", async () => {
    // The outgoing clip is 6 dB down and fades out over its last half second; the successor fades
    // in over its first second. On the AudioContext clock the outgoing owner starts at 1 and the
    // cut is at 3.
    const h = adjacentCutPreview({
      main: {
        gain_mb: -600,
        muted: false,
        fade_in_frames: 0,
        fade_out_frames: 12,
      },
      next: {
        gain_mb: 0,
        muted: false,
        fade_in_frames: 24,
        fade_out_frames: 0,
      },
    });
    await h.session.open();
    await h.session.play();
    const { context, nodes, gains } = h.audio;
    expect(nodes).toHaveLength(2);
    expect(gains).toHaveLength(2);
    nodes.forEach((node, index) => {
      expect(node.connect).toHaveBeenCalledWith(gains[index]);
      expect(gains[index]!.connect).toHaveBeenCalledWith(context.destination);
    });
    const level = expect.closeTo(Math.pow(10, -600 / 2_000), 12);
    expect(gainEvents(gains[0]!.gain)).toEqual([
      ["set", level, 1],
      ["set", level, 2.5],
      ["ramp", 0, 3],
    ]);
    // The successor's schedule begins at the cut, where its source starts: silent at its first
    // sample, not at the outgoing owner's level or at unity.
    expect(nodes[1]!.start).toHaveBeenCalledWith(3, 0, 2);
    expect(gainEvents(gains[1]!.gain)).toEqual([
      ["set", 0, 3],
      ["ramp", 1, 4],
    ]);

    h.nativeFrame("clip-main", 47);
    h.display(47);
    await vi.waitFor(() => expect(h.session.getPresentation()?.frame).toBe(47));
    h.display(48);
    await vi.waitFor(() => expect(h.session.getPresentation()?.frame).toBe(48));
    // The outgoing gain is released with its source at the hand-off; the successor keeps both.
    await vi.waitFor(() =>
      expect(gains[0]!.disconnect).toHaveBeenCalledTimes(1),
    );
    expect(nodes[0]!.disconnect).toHaveBeenCalledTimes(1);
    expect(nodes[1]!.stop).not.toHaveBeenCalled();
    expect(nodes[1]!.disconnect).not.toHaveBeenCalled();
    expect(gains[1]!.disconnect).not.toHaveBeenCalled();
    // Neither schedule was rewritten at the hand-off.
    expect(gainEvents(gains[1]!.gain)).toHaveLength(2);
    expect(gainEvents(gains[0]!.gain)).toHaveLength(3);

    await h.session.close();
    expect(nodes[1]!.disconnect).toHaveBeenCalledTimes(1);
    expect(gains[1]!.disconnect).toHaveBeenCalledTimes(1);
  });
});
