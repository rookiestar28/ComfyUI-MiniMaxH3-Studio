import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
  publicCompositionFingerprint,
  type ClipAudio,
} from "../src/contracts/compositionCodec";
import { scheduleClipAudioGain } from "../src/runtime/clipAudioEnvelope";
import {
  createEmbeddedAudioFollower,
  type EmbeddedAudioFollower,
} from "../src/runtime/embeddedAudioFollower";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import { resolveCompositionScene } from "../src/runtime/sceneResolver";
import { createVisualCompositionResources } from "../src/runtime/visualCompositionResources";
import {
  createNleLeaseScheduler,
  type NlePlaybackLeaseScheduler,
} from "../src/host/nleLeaseScheduler";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  evaluateMediaCapabilities,
  RUNTIME_PROFILE_FINGERPRINT,
} from "../src/runtime/mediaCapabilities";
import { runtimeContractAdmission } from "./fixtures/browserNleRuntimeFixture";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import {
  createEditorRuntime,
  type HtmlMediaElementSourceOwner,
  type MediaTransportFactory,
} from "../src/runtime/editorRuntime";

const active: EmbeddedAudioFollower[] = [];
const contexts: Array<ReturnType<typeof fakeAudioContext>> = [];
afterEach(async () => {
  await Promise.all(active.splice(0).map((item) => item.runtime.close()));
  // Every source plays through its own gain, created right after it. Whichever path tore
  // a source down -- stop, seek, suspend, close, its own end, a promotion, a refusal --
  // disconnected its gain with it.
  for (const audio of contexts.splice(0)) {
    expect(audio.gains).toHaveLength(audio.nodes.length);
    audio.nodes.forEach((node, index) =>
      expect(
        audio.gains[index]!.disconnect.mock.calls.length > 0,
        `gain ${index}`,
      ).toBe(node.disconnect.mock.calls.length > 0),
    );
  }
});

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function nativeVideo(playTask?: () => Promise<void>) {
  const element = new EventTarget() as HTMLVideoElement;
  let time = 0;
  let paused = true;
  Object.assign(element, {
    duration: 2,
    src: "blob:synthetic-video",
    seeking: false,
    muted: false,
    controls: true,
    error: null,
    play: vi.fn(async () => {
      await playTask?.();
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

function fakeAudioContext(sampleCount: number, channels = 2) {
  const nodes: Array<{
    connect: ReturnType<typeof vi.fn>;
    start: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
    onended: (() => void) | null;
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
      numberOfChannels: channels,
      length: sampleCount,
      duration: sampleCount / 48_000,
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

function harness({
  silent = false,
  overlay = false,
  sourceAudio,
  sourceSampleCount = 96_000,
  audioBodySampleCount = sourceSampleCount,
  sparseLandmarks = false,
  adjacentClips = 0,
  prefetchAudioBodySampleCount = sourceSampleCount,
  playTask,
  mutateScene,
  retain = false,
  playbackScheduler,
  runtimeFactory,
  clipAudio,
  innerClip,
}: {
  silent?: boolean;
  overlay?: boolean;
  sourceAudio?: "unavailable" | "excluded_overlay_policy";
  sourceSampleCount?: number;
  audioBodySampleCount?: number;
  sparseLandmarks?: boolean;
  adjacentClips?: number;
  prefetchAudioBodySampleCount?: number;
  playTask?: () => Promise<void>;
  mutateScene?: (scene: Record<string, unknown>) => void;
  retain?: boolean;
  playbackScheduler?: NlePlaybackLeaseScheduler;
  runtimeFactory?: typeof createEditorRuntime;
  clipAudio?: Readonly<Record<string, ClipAudio>>;
  // A second clip on the primary track that starts inside the first: it owns the audio while it
  // plays (the later start wins), and the first owns it again after it.
  innerClip?: Readonly<{ startFrame: number; durationFrames: number }>;
} = {}) {
  const wire = structuredClone(fixture.snapshot);
  wire.output.duration_frames = 48;
  wire.assets = [wire.assets[0]!];
  wire.assets[0]!.embedded_audio =
    sourceAudio ?? (silent ? "absent" : "present_bound");
  wire.assets[0]!.source_sample_count = silent ? null! : sourceSampleCount;
  if (!sparseLandmarks)
    wire.assets[0]!.landmarks = Array.from({ length: 48 }, (_, frame) => ({
      frame_index: frame,
      pts: frame * 512,
      dts: frame * 512,
      duration_ticks: 512,
    }));
  wire.clips = [wire.clips[0]!];
  if (adjacentClips > 0) {
    wire.output.duration_frames = 48 * (adjacentClips + 1);
    for (let index = 1; index <= adjacentClips; index += 1)
      wire.clips.push({
        ...structuredClone(wire.clips[0]!),
        clip_id: `clip-next-${index}`,
        start_frame: 48 * index,
      });
  }
  wire.clips[0]!.duration_frames = 48;
  if (innerClip !== undefined)
    wire.clips.push({
      ...structuredClone(wire.clips[0]!),
      clip_id: "clip-inner",
      start_frame: innerClip.startFrame,
      duration_frames: innerClip.durationFrames,
    });
  if (overlay)
    wire.clips.push({
      ...wire.clips[0]!,
      clip_id: "clip-overlay",
      track_id: "track-video",
    });
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  const decoded = decodePublicCompositionSnapshot(wire);
  // A clip's audio member as a decoded snapshot carries it. The follower reads the member
  // from the snapshot it was given and never decodes one itself.
  const snapshot =
    clipAudio === undefined
      ? decoded
      : Object.freeze({
          ...decoded,
          clips: Object.freeze(
            decoded.clips.map((clip) =>
              clipAudio[clip.clipId] === undefined
                ? clip
                : Object.freeze({ ...clip, audio: clipAudio[clip.clipId]! }),
            ),
          ),
        });
  const manifest = buildPublicAssetManifest(snapshot);
  const elements = new Map<string, HTMLVideoElement>();
  const audio = fakeAudioContext(sourceSampleCount);
  contexts.push(audio);
  const release = vi.fn(async () => undefined);
  const releasePrefetch = vi.fn(async () => undefined);
  const acquirePreview = vi.fn<
    NonNullable<AuthoringMediaSourceLeaseClient["acquireAudioPreview"]>
  >(async () => ({
    audioBody: pcmWav(prefetchAudioBodySampleCount),
    release: releasePrefetch,
  }));
  const acquire = vi.fn<AuthoringMediaSourceLeaseClient["acquireVideoSource"]>(
    async (request) => {
      const element = nativeVideo(playTask);
      elements.set(request.ownerId, element);
      return {
        element,
        audioBody: pcmWav(audioBodySampleCount),
        release,
        ...(retain ? { rebind } : {}),
      };
    },
  );
  const rebind = vi.fn<NonNullable<HtmlMediaElementSourceOwner["rebind"]>>(
    async () => true,
  );
  const layered = adjacentClips > 0 || innerClip !== undefined;
  const resolve = vi.fn(async (frame: number, source = snapshot) => {
    const activeClips = layered
      ? wire.clips.filter(
          (clip) =>
            clip.start_frame <= frame &&
            frame < clip.start_frame + clip.duration_frames,
        )
      : wire.clips;
    const audioClip = layered ? activeClips.at(-1)! : wire.clips[0]!;
    const sourceFrame = frame - audioClip.start_frame;
    const scene: Record<string, unknown> = {
      schema: "h3.context.resolved_scene.v1",
      profile_id: snapshot.profileId,
      public_fingerprint: source.publicFingerprint,
      frame,
      layers: activeClips.map((clip) => ({
        clip_id: clip.clip_id,
        asset_id: clip.asset_id,
        track_id: clip.track_id,
        source_frame: frame - clip.start_frame,
        source_pts: (frame - clip.start_frame) * 512,
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
      })),
      audio_span: silent
        ? null
        : {
            clip_id: audioClip.clip_id,
            asset_id: wire.assets[0]!.asset_id,
            output_start_sample: frame * 2000,
            output_end_sample: (frame + 1) * 2000,
            source_start_sample: sourceFrame * 2000,
            source_end_sample: (sourceFrame + 1) * 2000,
          },
      blockers: [],
    };
    mutateScene?.(scene);
    return scene;
  });
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
  const follower = createEmbeddedAudioFollower({
    snapshot,
    manifest,
    capability,
    resolveScene: resolve,
    leaseClient: {
      acquireVideoSource: acquire,
      ...(layered ? { acquireAudioPreview: acquirePreview } : {}),
      close: vi.fn(),
      create: vi.fn(),
    },
    transportOptions: { frameObserver: "event_fallback" },
    createAudioContext: () => audio.context,
    ...(playbackScheduler === undefined ? {} : { playbackScheduler }),
    ...(runtimeFactory === undefined ? {} : { runtimeFactory }),
  });
  active.push(follower);
  return {
    follower,
    elements,
    release,
    releasePrefetch,
    acquire,
    acquirePreview,
    resolve,
    snapshot,
    manifest,
    audio,
    wire,
    rebind,
    open: () =>
      follower.runtime.open(manifest, snapshot, RUNTIME_PROFILE_FINGERPRINT),
  };
}

describe("embedded audio follows the accepted VIDEO transport", () => {
  it("keeps the old source catalog after a cross-workspace replacement is refused", async () => {
    const h = harness({ retain: true });
    await h.open();
    const wire = structuredClone(h.wire);
    wire.workspace_handle = "another-workspace";
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    await expect(
      h.follower.runtime.replace(buildPublicAssetManifest(next), next),
    ).resolves.toMatchObject({ status: "blocked" });
    await expect(h.follower.runtime.seek(0)).resolves.toMatchObject({
      status: "applied",
    });
    await expect(h.follower.runtime.play()).resolves.toMatchObject({
      status: "applied",
    });
    expect(h.follower.status().state).toBe("following");
  });
  it("mutes a supplied source at the transport wrapper's rebind boundary", async () => {
    let openTransport!: MediaTransportFactory;
    const h = harness({
      retain: true,
      runtimeFactory: (dependencies) => {
        openTransport = dependencies.openTransport;
        return createEditorRuntime(dependencies);
      },
    });
    const signal = new AbortController().signal;
    const request = {
      asset: h.manifest.assets[0]!,
      ownerId: "clip-main",
      epoch: 1,
      signal,
    };
    const owner = await openTransport(request);
    expect(Object.isFrozen(owner)).toBe(true);
    const element = h.elements.get("clip-main")!;
    element.muted = false;
    try {
      await expect(owner.rebind!({ ...request, epoch: 2 })).resolves.toBe(true);
      expect(element.muted).toBe(true);
    } finally {
      await owner.close();
    }
  });
  it("decodes a newly introduced asset using the replacement source catalog", async () => {
    const h = harness();
    await h.open();
    h.wire.timeline_revision += 1;
    h.wire.assets[0]!.asset_id = "vid-newly-introduced";
    h.wire.clips[0]!.asset_id = "vid-newly-introduced";
    h.wire.public_fingerprint = publicCompositionFingerprint(h.wire);
    const next = decodePublicCompositionSnapshot(h.wire);
    const manifest = buildPublicAssetManifest(next);
    await expect(
      h.follower.runtime.replace(manifest, next),
    ).resolves.toMatchObject({ status: "applied" });
    await expect(h.follower.runtime.play()).resolves.toMatchObject({
      status: "applied",
    });
    expect(h.follower.status().state).toBe("following");
    expect(h.audio.context.decodeAudioData).toHaveBeenCalledTimes(2);
    expect(h.acquire.mock.calls.at(-1)![0].asset.assetId).toBe(
      "vid-newly-introduced",
    );
  });
  it("takes audio acquisition from running asset preparation only after cleanup joins", async () => {
    const events: string[] = [];
    let finishCleanup!: () => void;
    const cleanup = new Promise<void>((resolve) => {
      finishCleanup = resolve;
    });
    const scheduler = createNleLeaseScheduler<string>();
    const h = harness({ adjacentClips: 1, playbackScheduler: scheduler });
    const acquire = h.acquirePreview.getMockImplementation()!;
    h.acquirePreview.mockImplementation(async (...args) => {
      events.push("audio.acquire");
      if (!events.includes("preparation.cleaned")) throw new Error("busy");
      return acquire(...args);
    });
    await h.open();
    scheduler.updateDecorationDemand(
      [
        {
          key: "asset-preparation",
          kind: "playback_preparation",
          async acquire(signal) {
            events.push("preparation.started");
            await new Promise<void>((resolve) =>
              signal.addEventListener(
                "abort",
                () => {
                  events.push("preparation.aborted");
                  resolve();
                },
                { once: true },
              ),
            );
            await cleanup;
            events.push("preparation.cleaned");
            return { value: "discarded", release: async () => undefined };
          },
        },
      ],
      vi.fn(),
    );
    await vi.waitFor(() => expect(events).toContain("preparation.started"));
    const playing = h.follower.runtime.play();
    try {
      await vi.waitFor(() => expect(events).toContain("preparation.aborted"));
      expect(h.acquirePreview).not.toHaveBeenCalled();
      expect(h.release).not.toHaveBeenCalled();
      scheduler.updateDecorationDemand([], vi.fn());
      finishCleanup();
      expect((await playing).status).toBe("applied");
      expect(events).toEqual([
        "preparation.started",
        "preparation.aborted",
        "preparation.cleaned",
        "audio.acquire",
      ]);
      expect(h.acquirePreview).toHaveBeenCalledOnce();
      expect(h.releasePrefetch).toHaveBeenCalledOnce();
      expect(h.follower.status().state).toBe("following");
      expect((await h.follower.runtime.close()).status).toBe("closed");
      expect(h.release).toHaveBeenCalledOnce();
    } finally {
      scheduler.close();
      finishCleanup();
      await playing;
      await scheduler.whenIdle();
    }
  });

  it("retains verified PCM and decoder through a revision while forwarding current owner authority", async () => {
    const h = harness({ retain: true });
    await h.open();
    await h.follower.runtime.seek(17);
    await h.follower.runtime.play();
    await h.follower.runtime.pause();
    const beforeElement = h.elements.get("clip-main");
    const wire = structuredClone(h.wire);
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    const manifest = buildPublicAssetManifest(next);
    expect(
      await h.follower.runtime.replace(manifest, next, () => 17),
    ).toMatchObject({ status: "applied", outputFrame: 17 });
    expect(h.rebind).toHaveBeenCalledOnce();
    expect(h.rebind.mock.calls[0]![1]).toMatchObject({
      snapshot: next,
      manifest,
    });
    expect(h.acquire).toHaveBeenCalledOnce();
    expect(h.release).not.toHaveBeenCalled();
    expect(h.elements.get("clip-main")).toBe(beforeElement);
    expect(h.audio.context.decodeAudioData).toHaveBeenCalledOnce();
    expect((await h.follower.runtime.play()).status).toBe("applied");
    expect(h.follower.status()).toMatchObject({
      state: "following",
      owner_clip_id: "clip-main",
    });
    expect(h.audio.context.decodeAudioData).toHaveBeenCalledOnce();
    await h.follower.runtime.close();
    expect(h.release).toHaveBeenCalledOnce();
  });

  it("uses the replacement authority for adjacent-cut resolution and audio acquisition", async () => {
    const h = harness({ retain: true, adjacentClips: 1 });
    await h.open();
    const wire = structuredClone(h.wire);
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    const manifest = buildPublicAssetManifest(next);
    expect((await h.follower.runtime.replace(manifest, next, 17)).status).toBe(
      "applied",
    );
    h.resolve.mockClear();
    expect((await h.follower.runtime.play()).status).toBe("applied");
    expect(h.resolve).not.toHaveBeenCalled();
    expect(h.follower.runtime.snapshot().outputFrame).toBe(17);
    expect(h.acquirePreview).toHaveBeenCalledOnce();
    const [request, context] = h.acquirePreview.mock.calls[0]!;
    expect(context.snapshot).toBe(next);
    expect(context.manifest).toBe(manifest);
    expect(request.asset).toBe(manifest.assets[0]);
    expect(request.ownerId).toBe("clip-next-1");
    expect(h.acquire).toHaveBeenCalledOnce();
    expect(h.audio.context.decodeAudioData).toHaveBeenCalledTimes(2);
    await h.follower.runtime.close();
    expect(h.release).toHaveBeenCalledOnce();
    expect(h.releasePrefetch).toHaveBeenCalledOnce();
  });

  it("reopens an owner without optional retention instead of claiming successful reuse", async () => {
    const h = harness();
    await h.open();
    const wire = structuredClone(h.wire);
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    expect(
      (
        await h.follower.runtime.replace(
          buildPublicAssetManifest(next),
          next,
          17,
        )
      ).status,
    ).toBe("applied");
    expect(h.acquire).toHaveBeenCalledTimes(2);
    expect(h.rebind).not.toHaveBeenCalled();
    expect(h.release).toHaveBeenCalledOnce();
    expect(h.follower.status().reason).not.toBe("invalid_scene");
  });

  it("refuses playback and recovery when constructed in an already hidden document", async () => {
    const prior = Object.getOwnPropertyDescriptor(document, "hidden");
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    try {
      const h = harness();
      await h.open();
      expect((await h.follower.runtime.play()).status).toBe("blocked");
      expect((await h.follower.resume()).status).toBe("blocked");
      expect([...h.elements.values()].every((element) => element.muted)).toBe(
        true,
      );
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: false,
      });
      await h.follower.resume();
      expect((await h.follower.runtime.play()).status).toBe("applied");
      expect(h.follower.status().state).toBe("following");
    } finally {
      if (prior) Object.defineProperty(document, "hidden", prior);
      else Reflect.deleteProperty(document, "hidden");
    }
  });

  it("suspends actual media on document hiding and removes lifecycle listeners on close", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    const prior = Object.getOwnPropertyDescriptor(document, "hidden");
    try {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
      expect(
        [...h.elements.values()].every(
          (element) => element.muted && element.paused,
        ),
      ).toBe(true);
      expect(h.follower.status().reason).toBe("suspended");
      await h.follower.runtime.close();
      document.dispatchEvent(new Event("visibilitychange"));
      expect(h.follower.status().reason).toBe("closed");
    } finally {
      if (prior) Object.defineProperty(document, "hidden", prior);
      else Reflect.deleteProperty(document, "hidden");
    }
  });

  it("closes the native lease on page departure without a new playback completion", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    window.dispatchEvent(new Event("pagehide"));
    expect([...h.elements.values()].every((element) => element.muted)).toBe(
      true,
    );
    await vi.waitFor(() => expect(h.follower.status().reason).toBe("closed"));
    expect(h.release).toHaveBeenCalledTimes(1);
  });

  it("keeps a resolved timeline gap silent without acquiring media", async () => {
    const h = harness({
      mutateScene(scene) {
        scene.layers = [];
        scene.audio_span = null;
      },
    });
    expect((await h.open()).status).toBe("applied");
    expect(h.acquire).not.toHaveBeenCalled();
    expect(h.follower.status()).toMatchObject({
      state: "silent",
      reason: "no_primary",
    });
  });

  it("plays one primary source and mutes a same-asset overlay without another resolver", async () => {
    const h = harness({ overlay: true });
    expect((await h.open()).status).toBe("applied");
    expect([...h.elements.values()].every((v) => v.muted && !v.controls)).toBe(
      true,
    );
    expect((await h.follower.runtime.play()).status).toBe("applied");
    expect([...h.elements.values()].every((v) => v.muted)).toBe(true);
    expect(h.audio.nodes).toHaveLength(1);
    expect(h.audio.nodes[0]!.start).toHaveBeenCalledWith(1, 0, 2);
    expect(h.follower.status()).toMatchObject({
      state: "following",
      reason: "none",
      final_render_capability: "not_evaluated",
    });
    expect(h.resolve).toHaveBeenCalledTimes(1);
    await h.follower.runtime.pause();
    expect([...h.elements.values()].every((v) => v.muted)).toBe(true);
    await h.follower.runtime.close();
    expect(h.release).toHaveBeenCalledTimes(2);
    expect(h.follower.status().reason).toBe("closed");
  });

  it("plays each source through its own gain, at unity for a clip without adjustments", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    expect(h.audio.gains).toHaveLength(1);
    const [gain] = h.audio.gains;
    expect(h.audio.nodes[0]!.connect).toHaveBeenCalledWith(gain);
    expect(gain!.connect).toHaveBeenCalledWith(h.audio.context.destination);
    expect(gain!.gain.setValueAtTime.mock.calls).toEqual([[1, 1]]);
    expect(gain!.gain.linearRampToValueAtTime).not.toHaveBeenCalled();
    await h.follower.runtime.pause();
    expect(h.audio.nodes[0]!.disconnect).toHaveBeenCalled();
    expect(gain!.disconnect).toHaveBeenCalled();
  });

  it("schedules an adjusted clip's envelope from the clip sample its source starts at", async () => {
    const audio: ClipAudio = Object.freeze({
      gainMb: -600,
      muted: false,
      fadeInFrames: 12,
      fadeOutFrames: 12,
    });
    const h = harness({ clipAudio: { "clip-main": audio } });
    await h.open();
    expect((await h.follower.runtime.seek(6)).status).toBe("applied");
    await h.follower.runtime.play();
    // Frame 6 of the clip is its output sample 12000, inside the fade-in.
    expect(h.audio.nodes[0]!.start).toHaveBeenCalledWith(1, 0.25, 1.75);
    const expected = {
      setValueAtTime: vi.fn(),
      linearRampToValueAtTime: vi.fn(),
    };
    scheduleClipAudioGain(expected, audio, 48, 12_000, 1);
    const [gain] = h.audio.gains;
    expect(gain!.gain.setValueAtTime.mock.calls).toEqual(
      expected.setValueAtTime.mock.calls,
    );
    expect(gain!.gain.linearRampToValueAtTime.mock.calls).toEqual(
      expected.linearRampToValueAtTime.mock.calls,
    );
    expect(gain!.gain.setValueAtTime.mock.calls[0]).toEqual([
      Math.pow(10, -600 / 2_000) * 0.5,
      1,
    ]);
  });

  it("starts a later source at the clip sample the native clock reports", async () => {
    const audio: ClipAudio = Object.freeze({
      gainMb: 0,
      muted: false,
      fadeInFrames: 24,
      fadeOutFrames: 0,
    });
    const h = harness({ clipAudio: { "clip-main": audio } });
    await h.open();
    h.elements.get("clip-main")!.currentTime = 0.5;
    await h.follower.runtime.play();
    // Native playback is 24000 samples in when play resolves: the envelope starts there,
    // half-way up its fade-in.
    expect(h.audio.nodes[0]!.start).toHaveBeenCalledWith(1, 0.5, 1.5);
    const [gain] = h.audio.gains;
    expect(gain!.gain.setValueAtTime.mock.calls[0]).toEqual([0.5, 1]);
    expect(gain!.gain.linearRampToValueAtTime.mock.calls).toEqual([
      [1, 1 + 24_000 / 48_000],
    ]);
  });

  it("keeps one decoded owner scheduled across steady scene advancement", async () => {
    const h = harness();
    await h.open();
    expect(h.audio.context.decodeAudioData).toHaveBeenCalledTimes(1);
    await h.follower.runtime.play();
    const element = h.elements.get("clip-main")!;
    element.currentTime = 1 / 24;
    element.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(1)).status).toBe("applied");
    expect(h.audio.nodes).toHaveLength(1);
    expect(h.audio.nodes[0]!.start).toHaveBeenCalledTimes(1);
    expect(element.play).toHaveBeenCalledTimes(1);
    expect(h.audio.context.resume).toHaveBeenCalledTimes(1);
    expect(
      vi.mocked(h.audio.context.resume).mock.invocationCallOrder[0],
    ).toBeLessThan(vi.mocked(element.play).mock.invocationCallOrder[0]!);
    expect(element.pause).not.toHaveBeenCalled();
    expect(element.muted).toBe(true);
    expect(h.follower.status()).toMatchObject({
      state: "following",
      owner_clip_id: "clip-main",
    });
  });

  it.each([
    { label: "adjacent audio", successorStart: 48 },
    { label: "a silent gap", successorStart: 72 },
  ])(
    "retains the current image and font while looking ahead to $label",
    async ({ successorStart }) => {
      const h = harness({ retain: true, adjacentClips: 1 });
      await h.open();
      const wire = structuredClone(h.wire);
      wire.clips[1]!.start_frame = successorStart;
      wire.output.duration_frames = successorStart + 48;
      const font = structuredClone(fixture.snapshot.assets[3]!);
      font.asset_id = "h3.font.noto_sans.v1";
      wire.assets.push(structuredClone(fixture.snapshot.assets[2]!), font);
      const image = structuredClone(fixture.snapshot.clips[2]!);
      image.start_frame = 12;
      image.duration_frames = 1;
      const title = structuredClone(fixture.snapshot.clips[3]!);
      title.start_frame = 12;
      title.duration_frames = 24;
      title.text!.font_asset_id = font.asset_id;
      wire.clips.push(image, title);
      wire.public_fingerprint = publicCompositionFingerprint(wire);
      const snapshot = decodePublicCompositionSnapshot(wire);
      const manifest = buildPublicAssetManifest(snapshot);
      const bitmap = { width: 320, height: 180, close: vi.fn() };
      const face = { family: "", close: vi.fn() };
      const createStatic = vi.fn(
        async (request: { derivativeKind: string }) => ({
          open: async () => ({
            blob: new Blob([new Uint8Array(8)], {
              type:
                request.derivativeKind === "image_proxy"
                  ? "image/png"
                  : "font/ttf",
            }),
            geometry: {
              schema: "h3.authoring.media_geometry.v1",
              sourceWidth: 640,
              sourceHeight: 360,
              derivativeWidth: 320,
              derivativeHeight: 180,
            },
          }),
          release: vi.fn(async () => undefined),
          subscribeFailure: () => () => undefined,
        }),
      );
      const resources = createVisualCompositionResources({
        snapshot,
        manifest,
        leaseClient: {
          create: createStatic,
          acquireVideoSource: vi.fn(),
          close: vi.fn(),
        } as unknown as AuthoringMediaSourceLeaseClient,
        decodeImage: async () => bitmap,
        loadFont: async (_bytes, family) => {
          face.family = family;
          return face;
        },
      });
      // Use the same presenting resolver boundary as the monitor: resolving a frame also
      // prepares its real visual owners, so a future lookup can actually dispose current data.
      h.resolve.mockImplementation(
        async (
          frame,
          source = h.snapshot,
          context?: {
            epoch: number;
            signal: AbortSignal;
          },
        ) => {
          if (context === undefined) throw new Error("missing runtime context");
          const scene = resolveCompositionScene(source, frame);
          await resources.prepare(
            decodeResolvedScene(scene),
            context.epoch,
            context.signal,
          );
          return scene;
        },
      );
      try {
        expect(
          (await h.follower.runtime.replace(manifest, snapshot, 12)).status,
        ).toBe("applied");
        const epoch = h.follower.runtime.snapshot().epoch;
        const staticScene = (frame: number) => {
          const scene = decodeResolvedScene(
            resolveCompositionScene(snapshot, frame),
          );
          return {
            ...scene,
            layers: scene.layers.filter(
              (layer) => layer.text !== null || layer.clipId === image.clip_id,
            ),
          };
        };
        const originalFamily = face.family;
        expect(resources.read(staticScene(12), epoch).size).toBe(2);
        expect((await h.follower.runtime.play()).status).toBe("applied");
        expect(h.follower.runtime.snapshot().outputFrame).toBe(12);
        expect(
          resources.read(staticScene(12), epoch).get(title.clip_id),
        ).toEqual({
          kind: "font",
          family: originalFamily,
        });
        expect(bitmap.close).not.toHaveBeenCalled();
        expect(face.close).not.toHaveBeenCalled();
        expect(h.audio.nodes).toHaveLength(successorStart === 48 ? 2 : 1);
        if (successorStart === 48)
          expect(h.audio.nodes[1]!.start).toHaveBeenCalledWith(2.5, 0, 2);
        const video = h.elements.get(snapshot.clips[0]!.clipId)!;
        video.currentTime = 13 / 24;
        video.dispatchEvent(new Event("timeupdate"));
        expect((await h.follower.runtime.advance!(13)).status).toBe("applied");
        expect(
          resources.read(staticScene(13), epoch).get(title.clip_id),
        ).toEqual({
          kind: "font",
          family: originalFamily,
        });
        expect(createStatic).toHaveBeenCalledTimes(2);
        expect(bitmap.close).toHaveBeenCalledOnce();
        expect(face.close).not.toHaveBeenCalled();
      } finally {
        await h.follower.runtime.close();
        await resources.close();
      }
    },
  );

  it("pre-schedules adjacent audio and preserves it across the automatic owner handoff", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    expect((await h.follower.runtime.play()).status).toBe("applied");
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[0]!.start).toHaveBeenCalledWith(1, 0, 2);
    expect(h.audio.nodes[1]!.start).toHaveBeenCalledWith(3, 0, 2);
    expect(h.releasePrefetch).toHaveBeenCalledTimes(1);

    const outgoing = h.elements.get("clip-main")!;
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(48)).blocker?.code).toBe(
      "unsupported",
    );
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.seek(48)).status).toBe("applied");
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.play()).status).toBe("applied");

    // The successor was started on the original AudioContext clock; the owner transition neither
    // stops it nor mints a cut-time replacement node.
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
    expect(h.follower.runtime.snapshot().outputFrame).toBe(48);
  });

  it("gives a scheduled successor its own clip's envelope from its transition", async () => {
    const audio: ClipAudio = Object.freeze({
      gainMb: 0,
      muted: false,
      fadeInFrames: 12,
      fadeOutFrames: 0,
    });
    const h = harness({
      adjacentClips: 1,
      clipAudio: { "clip-next-1": audio },
    });
    await h.open();
    await h.follower.runtime.play();
    expect(h.audio.gains).toHaveLength(2);
    const [outgoing, incoming] = h.audio.gains;
    expect(outgoing!.gain.setValueAtTime.mock.calls).toEqual([[1, 1]]);
    // The successor starts at the outgoing source's end, at its own clip's first sample.
    expect(h.audio.nodes[1]!.start).toHaveBeenCalledWith(3, 0, 2);
    expect(h.audio.nodes[1]!.connect).toHaveBeenCalledWith(incoming);
    expect(incoming!.connect).toHaveBeenCalledWith(h.audio.context.destination);
    expect(incoming!.gain.setValueAtTime.mock.calls).toEqual([[0, 3]]);
    expect(incoming!.gain.linearRampToValueAtTime.mock.calls).toEqual([
      [1, 3 + 24_000 / 48_000],
    ]);

    // The outgoing source ends: its gain goes with it, the successor's stays until its own
    // source ends, and the successor plays on.
    h.audio.nodes[0]!.onended?.();
    expect(outgoing!.disconnect).toHaveBeenCalled();
    expect(incoming!.disconnect).not.toHaveBeenCalled();
    expect(h.follower.status().state).toBe("following");
    h.audio.nodes[1]!.onended?.();
    expect(incoming!.disconnect).toHaveBeenCalled();
  });

  it("resumes a clip's own envelope when it owns the audio again after a clip inside it", async () => {
    const audio: ClipAudio = Object.freeze({
      gainMb: 0,
      muted: false,
      fadeInFrames: 36,
      fadeOutFrames: 0,
    });
    const h = harness({
      innerClip: { startFrame: 12, durationFrames: 12 },
      clipAudio: { "clip-main": audio },
    });
    await h.open();
    expect((await h.follower.runtime.seek(12)).status).toBe("applied");
    expect((await h.follower.runtime.play()).status).toBe("applied");
    expect(h.follower.status()).toMatchObject({ owner_clip_id: "clip-inner" });
    // The inner clip owns frames 12 to 23. At 24 the outer clip owns the audio again, 24 frames
    // into itself: its source is scheduled from its own sample 48000, two thirds up its fade-in,
    // and not from the fade's start.
    expect(h.audio.nodes[1]!.start).toHaveBeenCalledWith(1.5, 1, 1);
    const expected = {
      setValueAtTime: vi.fn(),
      linearRampToValueAtTime: vi.fn(),
    };
    scheduleClipAudioGain(expected, audio, 48, 48_000, 1.5);
    const resumed = h.audio.gains[1]!;
    expect(resumed.gain.setValueAtTime.mock.calls).toEqual(
      expected.setValueAtTime.mock.calls,
    );
    expect(resumed.gain.linearRampToValueAtTime.mock.calls).toEqual(
      expected.linearRampToValueAtTime.mock.calls,
    );
    expect(resumed.gain.setValueAtTime.mock.calls[0]).toEqual([
      48_000 / 72_000,
      1.5,
    ]);
  });

  it("releases a source's gain with it when the source ends with nothing after it", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    const [gain] = h.audio.gains;
    h.audio.nodes[0]!.onended?.();
    expect(h.audio.nodes[0]!.disconnect).toHaveBeenCalledTimes(1);
    expect(gain!.disconnect).toHaveBeenCalledTimes(1);
    expect(h.follower.status()).toMatchObject({
      state: "suspended",
      reason: "boundary_reached",
    });
  });

  it("holds a muted clip's gain at zero from before its source starts", async () => {
    const audio: ClipAudio = Object.freeze({
      gainMb: -600,
      muted: true,
      fadeInFrames: 12,
      fadeOutFrames: 12,
    });
    const h = harness({ clipAudio: { "clip-main": audio } });
    await h.open();
    await h.follower.runtime.play();
    const [gain] = h.audio.gains;
    expect(gain!.gain.setValueAtTime.mock.calls).toEqual([[0, 1]]);
    expect(gain!.gain.linearRampToValueAtTime).not.toHaveBeenCalled();
    // Written before the source starts: no render quantum plays at the gain's default 1.
    expect(gain!.gain.setValueAtTime.mock.invocationCallOrder[0]).toBeLessThan(
      h.audio.nodes[0]!.start.mock.invocationCallOrder[0]!,
    );
  });

  it("preserves scheduled audio when a late advance falls back to the next boundary frame", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    await h.follower.runtime.play();
    const successor = h.audio.nodes[1]!;
    const outgoing = h.elements.get("clip-main")!;
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(47)).status).toBe("applied");
    expect(h.follower.runtime.snapshot().outputFrame).toBe(47);
    expect((await h.follower.runtime.advance(52)).blocker?.code).toBe(
      "unsupported",
    );
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.seek(48)).status).toBe("applied");
    await h.follower.runtime.pause();
    await h.follower.runtime.play();
    expect(h.audio.nodes).toHaveLength(2);
    expect(successor.stop).not.toHaveBeenCalled();
    expect(h.follower.runtime.snapshot().outputFrame).toBe(48);
  });

  it("pins the scheduled cut when the boundary arrives before the outgoing endpoint frame was presented", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    await h.follower.runtime.play();
    const successor = h.audio.nodes[1]!;
    const outgoing = h.elements.get("clip-main")!;
    outgoing.currentTime = 46 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(46)).status).toBe("applied");
    // The native clock reaches the last frame and the wall clock crosses the cut before a paint
    // presents frame 47: the runtime names the ended owner with 46 still committed.
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(48)).blocker).toEqual({
      code: "unsupported",
      subjectId: "clip-main",
    });
    expect(h.follower.runtime.snapshot().outputFrame).toBe(46);
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.seek(48)).status).toBe("applied");
    await h.follower.runtime.pause();
    await h.follower.runtime.play();
    expect(h.audio.nodes).toHaveLength(2);
    expect(successor.stop).not.toHaveBeenCalled();
    expect(h.follower.runtime.snapshot().outputFrame).toBe(48);
  });

  it("treats a boundary seek inside the outgoing owner as a user seek and cancels scheduled audio", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    await h.follower.runtime.play();
    const successor = h.audio.nodes[1]!;
    const outgoing = h.elements.get("clip-main")!;
    outgoing.currentTime = 46 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    await h.follower.runtime.advance(46);
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(48)).blocker?.code).toBe(
      "unsupported",
    );
    // Frame 47 is the next unpresented frame but still belongs to the owner that ended. A
    // scheduler that moves there, rather than to the cut at 48, loses the scheduled audio.
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.seek(47)).status).toBe("applied");
    expect(successor.stop).toHaveBeenCalledTimes(1);
  });

  it("cancels prepared audio for a manual seek beyond the automatic boundary", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    await h.follower.runtime.play();
    const successor = h.audio.nodes[1]!;
    const outgoing = h.elements.get("clip-main")!;
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    await h.follower.runtime.advance(47);
    expect((await h.follower.runtime.advance(52)).blocker?.code).toBe(
      "unsupported",
    );
    expect((await h.follower.runtime.seek(50)).status).toBe("applied");
    expect(successor.stop).toHaveBeenCalledTimes(1);
  });

  it("preserves a successor promoted by ended before the delayed boundary seek", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    await h.follower.runtime.play();
    const successor = h.audio.nodes[1]!;
    const outgoing = h.elements.get("clip-main")!;
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    await h.follower.runtime.advance(47);
    h.audio.nodes[0]!.onended?.();
    expect((await h.follower.runtime.advance(52)).blocker?.code).toBe(
      "unsupported",
    );
    await h.follower.runtime.pause();
    await h.follower.runtime.seek(48);
    await h.follower.runtime.pause();
    await h.follower.runtime.play();
    expect(h.audio.nodes).toHaveLength(2);
    expect(successor.stop).not.toHaveBeenCalled();
  });

  it("uses the new scheduled cut after a promoted owner reaches a second delayed handoff", async () => {
    const h = harness({ adjacentClips: 2 });
    await h.open();
    await h.follower.runtime.play();
    const outgoing = h.elements.get("clip-main")!;
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    await h.follower.runtime.advance(47);
    await h.follower.runtime.advance(52);
    await h.follower.runtime.pause();
    await h.follower.runtime.seek(48);
    await h.follower.runtime.pause();
    await h.follower.runtime.play();
    const third = h.audio.nodes[2]!;
    h.audio.nodes[0]!.onended?.();
    const second = h.elements.get("clip-next-1")!;
    second.currentTime = 47 / 24;
    second.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(95)).status).toBe("applied");
    expect((await h.follower.runtime.advance(100)).blocker?.code).toBe(
      "unsupported",
    );
    await h.follower.runtime.pause();
    await h.follower.runtime.seek(96);
    await h.follower.runtime.pause();
    await h.follower.runtime.play();
    expect(h.audio.nodes).toHaveLength(3);
    expect(third.stop).not.toHaveBeenCalled();
    expect(h.follower.runtime.snapshot().outputFrame).toBe(96);
  });

  it("chains a prepared third owner without restarting audio at the second adjacent cut", async () => {
    const h = harness({ adjacentClips: 2 });
    await h.open();
    expect((await h.follower.runtime.play()).status).toBe("applied");
    expect(h.audio.nodes).toHaveLength(2);

    const outgoing = h.elements.get("clip-main")!;
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(48)).blocker?.code).toBe(
      "unsupported",
    );
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.seek(48)).status).toBe("applied");
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.play()).status).toBe("applied");

    expect(h.audio.nodes).toHaveLength(3);
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
    expect(h.audio.nodes[2]!.start).toHaveBeenCalledWith(5, 0, 2);
    expect(h.releasePrefetch).toHaveBeenCalledTimes(2);
    expect((await h.follower.runtime.close()).status).toBe("closed");
    expect(h.audio.nodes[1]!.stop).toHaveBeenCalledTimes(1);
    expect(h.audio.nodes[2]!.stop).toHaveBeenCalledTimes(1);
    expect(h.audio.context.close).toHaveBeenCalledTimes(1);
  });

  // The owner of the next cut is prepared while the outgoing one plays. Through the follower
  // that must be inaudible, must leave nothing to fetch or decode on the cut's path, and must
  // never put two acquisitions on the lease route at once.
  async function reachCut(h: ReturnType<typeof harness>, cut: number) {
    const outgoingId = cut === 48 ? "clip-main" : `clip-next-${cut / 48 - 1}`;
    const outgoing = h.elements.get(outgoingId)!;
    outgoing.currentTime = 47 / 24;
    outgoing.dispatchEvent(new Event("timeupdate"));
    expect((await h.follower.runtime.advance(cut)).blocker?.code).toBe(
      "unsupported",
    );
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.seek(cut)).status).toBe("applied");
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.play()).status).toBe("applied");
  }

  it("prepares the incoming owner without touching the audible one, and decodes nothing at the cut", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    await h.follower.runtime.play();
    expect(h.audio.nodes).toHaveLength(2);
    const decodes = vi.mocked(h.audio.context.decodeAudioData);
    expect(decodes).toHaveBeenCalledTimes(2);

    await expect(
      h.follower.runtime.prepare!([await h.resolve(48)]),
    ).resolves.toMatchObject({ status: "applied" });
    // The preroll seek of the prepared owner is not a move of the playhead.
    expect(h.acquire).toHaveBeenCalledTimes(2);
    expect(h.elements.get("clip-next-1")!.muted).toBe(true);
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[0]!.stop).not.toHaveBeenCalled();
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
    expect(h.follower.status()).toMatchObject({ state: "following" });
    expect(h.follower.runtime.snapshot().resources).toMatchObject({
      activeVideoOwners: 1,
      warmVideoOwners: 1,
    });

    await reachCut(h, 48);
    // Adopted, not acquired again; the scheduled successor is the audio that plays, and the
    // incoming owner's own audio was not decoded on the way.
    expect(h.acquire).toHaveBeenCalledTimes(2);
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
    expect(decodes).toHaveBeenCalledTimes(2);
    expect(h.follower.runtime.snapshot()).toMatchObject({
      outputFrame: 48,
      resources: { activeVideoOwners: 1, warmVideoOwners: 0 },
    });
    expect(h.follower.status()).toMatchObject({ state: "following" });
  });

  it("decodes the incoming owner's own audio when it must start a node after the handoff", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    await h.follower.runtime.play();
    await h.follower.runtime.prepare!([await h.resolve(48)]);
    await reachCut(h, 48);
    const decodes = vi.mocked(h.audio.context.decodeAudioData);
    expect(decodes).toHaveBeenCalledTimes(2);

    // A user pause stops the scheduled node; the next play starts a fresh one from this owner's
    // own buffer, decoded before native playback begins.
    await h.follower.runtime.pause();
    expect((await h.follower.runtime.play()).status).toBe("applied");
    expect(decodes).toHaveBeenCalledTimes(3);
    expect(h.audio.nodes).toHaveLength(3);
    expect(h.follower.status()).toMatchObject({ state: "following" });
  });

  it("fetches the audio of the clip after the incoming one ahead, so play at the cut acquires nothing", async () => {
    const h = harness({ adjacentClips: 2 });
    await h.open();
    await h.follower.runtime.play();
    expect(h.acquirePreview).toHaveBeenCalledTimes(1);

    await expect(
      h.follower.runtime.prepare!([await h.resolve(48), await h.resolve(96)]),
    ).resolves.toMatchObject({ status: "applied" });
    expect(h.acquirePreview).toHaveBeenCalledTimes(2);
    expect(h.acquirePreview.mock.calls[1]![0]).toMatchObject({
      ownerId: "clip-next-2",
    });
    expect(h.releasePrefetch).toHaveBeenCalledTimes(2);
    // Held for the cut, not scheduled yet.
    expect(h.audio.nodes).toHaveLength(2);

    const resolvedAhead = h.resolve.mock.calls.length;
    await reachCut(h, 48);
    expect(h.acquirePreview).toHaveBeenCalledTimes(2);
    // The cut's own play resolved one scene -- frame 48 -- and none for the next transition.
    expect(
      h.resolve.mock.calls.slice(resolvedAhead).map(([frame]) => frame),
    ).toEqual([48]);
    expect(h.audio.nodes).toHaveLength(3);
    expect(h.audio.nodes[1]!.stop).not.toHaveBeenCalled();
    expect(h.audio.nodes[2]!.start).toHaveBeenCalledWith(5, 0, 2);
  });

  it("does not fetch ahead from a scene that is not the incoming clip's own handover", async () => {
    const h = harness({ adjacentClips: 2 });
    await h.open();
    await h.follower.runtime.play();
    await h.follower.runtime.prepare!([
      await h.resolve(48),
      await h.resolve(72),
    ]);
    expect(h.acquirePreview).toHaveBeenCalledTimes(1);
    // The cut still works: play fetches the successor there, as it did before.
    await reachCut(h, 48);
    expect(h.acquirePreview).toHaveBeenCalledTimes(2);
    expect(h.audio.nodes).toHaveLength(3);
  });

  it("makes play wait for a preparation in flight before it fetches the successor", async () => {
    const h = harness({ adjacentClips: 1 });
    await h.open();
    await h.follower.runtime.play();
    const slow = deferred();
    const acquire = h.acquire.getMockImplementation()!;
    h.acquire.mockImplementationOnce(async (request, context) => {
      await slow.promise;
      return acquire(request, context);
    });
    const preparing = h.follower.runtime.prepare!([await h.resolve(48)]);
    await vi.waitFor(() => expect(h.acquire).toHaveBeenCalledTimes(2));

    await h.follower.runtime.pause();
    const replay = h.follower.runtime.play();
    await new Promise((resolve) => setImmediate(resolve));
    // The source acquisition is still on the route: the successor's audio is not requested yet.
    expect(h.acquirePreview).toHaveBeenCalledTimes(1);
    slow.resolve();
    await expect(preparing).resolves.toMatchObject({ status: "applied" });
    await expect(replay).resolves.toMatchObject({ status: "applied" });
    expect(h.acquirePreview).toHaveBeenCalledTimes(2);
  });

  it("makes a move that opens an owner wait for the audio being fetched ahead", async () => {
    const h = harness({ adjacentClips: 2 });
    await h.open();
    await h.follower.runtime.play();
    const slow = deferred();
    const preview = h.acquirePreview.getMockImplementation()!;
    h.acquirePreview.mockImplementationOnce(async (request, context) => {
      await slow.promise;
      return preview(request, context);
    });
    const preparing = h.follower.runtime.prepare!([
      await h.resolve(48),
      await h.resolve(96),
    ]);
    await vi.waitFor(() => expect(h.acquirePreview).toHaveBeenCalledTimes(2));
    expect(h.acquire).toHaveBeenCalledTimes(2);

    // The user seeks into the third clip: its owner is opened only after the fetch has ended.
    const seeking = h.follower.runtime.seek(100);
    await new Promise((resolve) => setImmediate(resolve));
    expect(h.acquire).toHaveBeenCalledTimes(2);
    slow.resolve();
    await expect(seeking).resolves.toMatchObject({
      status: "applied",
      outputFrame: 100,
    });
    await preparing;
    expect(h.acquire).toHaveBeenCalledTimes(3);
    expect(h.acquire.mock.calls[2]![0]).toMatchObject({
      ownerId: "clip-next-2",
    });
    // The audio fetched for the abandoned cut was released and is not scheduled.
    expect(h.releasePrefetch).toHaveBeenCalledTimes(2);
  });

  it("returns a typed blocked receipt and releases a rejected future-audio prefetch", async () => {
    const h = harness({
      adjacentClips: 1,
      prefetchAudioBodySampleCount: 95_999,
    });
    await h.open();

    await expect(h.follower.runtime.play()).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "unsupported" },
    });
    expect(h.releasePrefetch).toHaveBeenCalledTimes(1);
    expect(h.audio.nodes).toHaveLength(0);
    expect(h.follower.status()).toMatchObject({
      state: "unavailable",
      reason: "playback_unavailable",
    });
    expect((await h.follower.runtime.close()).status).toBe("closed");
    expect(h.release).toHaveBeenCalledTimes(1);
    expect(h.audio.context.close).toHaveBeenCalledTimes(1);
  });

  it("anchors audible scheduling to native progress observed when play resolves", async () => {
    const h = harness();
    await h.open();
    const element = h.elements.get("clip-main")!;
    element.currentTime = 1 / 24;
    expect((await h.follower.runtime.play()).status).toBe("applied");
    expect(h.audio.nodes[0]!.start).toHaveBeenCalledWith(1, 1 / 24, 2 - 1 / 24);
  });

  it("admits native PTS observations between sparse validation landmarks", async () => {
    const h = harness({ sparseLandmarks: true });
    await h.open();
    await h.follower.runtime.play();
    const element = h.elements.get("clip-main")!;
    element.currentTime = 1 / 24;
    element.dispatchEvent(new Event("timeupdate"));

    expect((await h.follower.runtime.advance(1)).status).toBe("applied");
    expect(h.follower.status().state).toBe("following");
    expect(h.audio.nodes).toHaveLength(1);
  });

  it("fails closed before decode when WAV coverage disagrees with source authority", async () => {
    const h = harness({ audioBodySampleCount: 95_999 });
    expect((await h.open()).status).toBe("blocked");
    expect(h.audio.context.decodeAudioData).not.toHaveBeenCalled();
    expect(h.audio.nodes).toHaveLength(0);
    expect([...h.elements.values()].every((element) => element.muted)).toBe(
      true,
    );
  });

  it("stops the node and closes the one workspace context on final teardown", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    await h.follower.runtime.close();
    expect(h.audio.nodes[0]!.stop).toHaveBeenCalledTimes(1);
    expect(h.audio.nodes[0]!.disconnect).toHaveBeenCalled();
    expect(h.audio.context.close).toHaveBeenCalledTimes(1);
  });

  it("keeps an actually absent soundtrack silent while VIDEO plays", async () => {
    const h = harness({ silent: true });
    await h.open();
    await h.follower.runtime.play();
    expect(h.follower.status()).toMatchObject({
      state: "silent",
      reason: "no_embedded_audio",
      owner_clip_id: null,
    });
    expect([...h.elements.values()].every((v) => v.muted && !v.paused)).toBe(
      true,
    );
  });

  it.each(["unavailable", "excluded_overlay_policy"] as const)(
    "does not report primary %s metadata as a genuinely absent soundtrack",
    async (sourceAudio) => {
      const h = harness({ silent: true, sourceAudio });
      expect((await h.open()).status).toBe("blocked");
      expect(h.acquire).not.toHaveBeenCalled();
      expect(h.follower.status().reason).toBe("invalid_scene");
    },
  );

  it("rejects an overlay-selected span before acquisition", async () => {
    const h = harness({
      overlay: true,
      mutateScene(scene) {
        (scene.audio_span as Record<string, unknown>).clip_id = "clip-overlay";
      },
    });
    expect((await h.open()).status).toBe("blocked");
    expect(h.acquire).not.toHaveBeenCalled();
    expect(h.follower.status().reason).toBe("invalid_scene");
  });

  it("physically mutes a pending play immediately and never unmutes its late completion", async () => {
    const pending = deferred();
    const h = harness({ playTask: () => pending.promise });
    await h.open();
    const playing = h.follower.runtime.play();
    await vi.waitFor(() =>
      expect([...h.elements.values()][0]!.play).toHaveBeenCalled(),
    );
    const seek = h.follower.runtime.seek(12);
    expect([...h.elements.values()][0]!.muted).toBe(true);
    pending.resolve();
    await playing;
    await seek;
    expect([...h.elements.values()][0]!.muted).toBe(true);
    expect(h.follower.runtime.snapshot().outputFrame).toBe(12);
    expect(h.follower.status().state).not.toBe("following");
  });

  it("suspends audibility and requires an explicit re-anchor before replay", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    await h.follower.suspend();
    expect(h.follower.status()).toMatchObject({
      state: "suspended",
      reason: "suspended",
    });
    expect([...h.elements.values()][0]!.muted).toBe(true);
    expect((await h.follower.runtime.play()).status).toBe("blocked");
    expect(h.release).not.toHaveBeenCalled();
    await h.follower.resume();
    await h.follower.runtime.play();
    expect(h.follower.status().state).toBe("following");
  });

  it("keeps failed playback unavailable instead of declaring successful silence", async () => {
    const h = harness({
      playTask: () =>
        Promise.reject(new DOMException("denied", "NotAllowedError")),
    });
    await h.open();
    expect((await h.follower.runtime.play()).status).toBe("blocked");
    expect(h.follower.status().state).toBe("unavailable");
    expect(h.follower.status().reason).toBe("autoplay_blocked");
    expect([...h.elements.values()][0]!.muted).toBe(true);
  });

  it("reports an asynchronous decode error and mutes before native failure cleanup", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    const element = [...h.elements.values()][0]!;
    Object.assign(element, { error: { code: 3 } });
    element.dispatchEvent(new Event("error"));
    expect(element.muted).toBe(true);
    expect(h.follower.status()).toMatchObject({
      state: "unavailable",
      reason: "decode_failed",
    });
    await vi.waitFor(() => expect(h.release).toHaveBeenCalled());
  });

  it("expires the admitted interval on native observation and reanchors explicitly", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    const element = [...h.elements.values()][0]!;
    element.currentTime = 2;
    element.dispatchEvent(new Event("timeupdate"));
    expect(element.muted).toBe(true);
    expect(h.follower.status()).toMatchObject({
      state: "suspended",
      reason: "boundary_reached",
    });
    await h.follower.runtime.pause();
    await h.follower.runtime.seek(6);
    await h.follower.runtime.play();
    expect(h.follower.status().state).toBe("following");
  });

  it("preserves the accepted resume-after-seek behavior while changing audio epoch", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    const previous = h.follower.status().transport_epoch;
    expect((await h.follower.runtime.seek(12)).status).toBe("applied");
    expect(h.follower.runtime.snapshot().status).toBe("playing");
    expect(h.follower.status().state).toBe("following");
    expect([...h.elements.values()].every((element) => element.muted)).toBe(
      true,
    );
    expect(h.audio.nodes).toHaveLength(2);
    expect(h.follower.status().transport_epoch).toBeGreaterThan(previous);
  });

  it("does not reopen a disposed follower or create new sources", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.close();
    const calls = h.acquire.mock.calls.length;
    expect((await h.open()).status).toBe("blocked");
    expect(h.acquire).toHaveBeenCalledTimes(calls);
    expect((await h.follower.runtime.close()).status).toBe("closed");
  });

  it("mutes synchronously on source substitution and rejects the foreign binding", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    const replacing = h.follower.runtime.replace(h.manifest, {
      ...h.snapshot,
      timelineRevision: 99,
    });
    expect([...h.elements.values()].every((element) => element.muted)).toBe(
      true,
    );
    expect((await replacing).status).toBe("blocked");
    expect(h.follower.status()).toMatchObject({
      state: "unavailable",
      reason: "invalid_scene",
    });
  });

  it.each(["output_start_sample", "output_end_sample", "source_end_sample"])(
    "rejects a forged %s before lease acquisition",
    async (field) => {
      const h = harness({
        mutateScene(scene) {
          (scene.audio_span as Record<string, unknown>)[field] = 999_999;
        },
      });
      expect((await h.open()).status).toBe("blocked");
      expect(h.acquire).not.toHaveBeenCalled();
      expect(h.follower.status().reason).toBe("invalid_scene");
    },
  );

  it("keeps a missing audible span blocked, distinct from genuine silence", async () => {
    const h = harness({
      mutateScene(scene) {
        scene.audio_span = null;
      },
    });
    expect((await h.open()).status).toBe("blocked");
    expect(h.acquire).not.toHaveBeenCalled();
    expect(h.follower.status().state).toBe("unavailable");
  });

  it("retains failed cleanup until native release succeeds on retry", async () => {
    const h = harness();
    await h.open();
    await h.follower.runtime.play();
    h.release.mockRejectedValueOnce(new Error("synthetic_release_failure"));
    expect((await h.follower.runtime.close()).blocker?.code).toBe(
      "transport_failure",
    );
    expect(h.follower.status().reason).toBe("cleanup_pending");
    expect([...h.elements.values()].every((element) => element.muted)).toBe(
      true,
    );
    expect((await h.follower.runtime.close()).status).toBe("closed");
    expect(h.follower.status().reason).toBe("closed");
  });
});
