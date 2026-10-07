import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import { createEmbeddedAudioFollower } from "../src/runtime/embeddedAudioFollower";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  evaluateMediaCapabilities,
} from "../src/runtime/mediaCapabilities";
import { runtimeContractAdmission } from "../tests/fixtures/browserNleRuntimeFixture";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";

// Explicit synthetic native boundary: no encoded-media or hardware-audibility claim.
function syntheticVideo(): HTMLVideoElement {
  const element = new EventTarget();
  let time = 0,
    paused = true;
  Object.assign(element, {
    src: "blob:synthetic",
    duration: 3,
    seeking: false,
    muted: true,
    play: async () => {
      paused = false;
    },
    pause: () => {
      paused = true;
    },
    load: () => undefined,
    removeAttribute: () => undefined,
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
  return element as HTMLVideoElement;
}
function audioPreview(samples: number): Blob {
  const body = new ArrayBuffer(44 + samples * 2);
  const bytes = new Uint8Array(body);
  const view = new DataView(body);
  const ascii = (offset: number, value: string) =>
    [...value].forEach((character, index) => {
      bytes[offset + index] = character.charCodeAt(0);
    });
  ascii(0, "RIFF");
  view.setUint32(4, body.byteLength - 8, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 48_000, true);
  view.setUint32(28, 96_000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, samples * 2, true);
  return new Blob([body], { type: "audio/wav" });
}
const wire = structuredClone(fixture.snapshot);
wire.output.duration_frames = 24;
wire.assets = [wire.assets[0]!];
wire.clips = [
  { ...wire.clips[0]!, duration_frames: 24 },
  {
    ...wire.clips[0]!,
    clip_id: "clip-overlay",
    track_id: "track-video",
    duration_frames: 24,
  },
];
wire.public_fingerprint = publicCompositionFingerprint(wire);
const snapshot = decodePublicCompositionSnapshot(wire);
const manifest = buildPublicAssetManifest(snapshot);
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
const owners = new Set<HTMLVideoElement>();
const audioNodes = new Set<AudioBufferSourceNode>();
const audioContexts = new Set<AudioContext>();
let resolverCalls = 0,
  acquired = 0,
  released = 0;
const follower = createEmbeddedAudioFollower({
  snapshot,
  manifest,
  capability,
  transportOptions: { frameObserver: "event_fallback" },
  createAudioContext() {
    const context = new AudioContext({ sampleRate: 48_000 });
    audioContexts.add(context);
    const createBufferSource = context.createBufferSource.bind(context);
    context.createBufferSource = () => {
      const node = createBufferSource();
      const start = node.start.bind(node);
      const stop = node.stop.bind(node);
      const disconnect = node.disconnect.bind(node);
      node.start = (...args) => {
        start(...args);
        audioNodes.add(node);
      };
      node.stop = (...args) => {
        stop(...args);
        audioNodes.delete(node);
      };
      node.disconnect = () => {
        disconnect();
        audioNodes.delete(node);
      };
      node.addEventListener("ended", () => audioNodes.delete(node));
      return node;
    };
    const close = context.close.bind(context);
    context.close = async () => {
      await close();
      audioContexts.delete(context);
    };
    return context;
  },
  leaseClient: {
    async acquireVideoSource() {
      const element = syntheticVideo();
      owners.add(element);
      acquired++;
      return {
        element,
        // CRITICAL: Open decodes the accepted primary preview before play; a VIDEO-only lease
        // fails at seek. Audio uses Web Audio nodes and must never unmute native VIDEO owners.
        audioBody: audioPreview(wire.assets[0]!.source_sample_count!),
        async release() {
          owners.delete(element);
          released++;
        },
      };
    },
  } as unknown as AuthoringMediaSourceLeaseClient,
  resolveScene: async (frame) => {
    resolverCalls++;
    return {
      schema: "h3.context.resolved_scene.v1",
      profile_id: snapshot.profileId,
      public_fingerprint: snapshot.publicFingerprint,
      frame,
      blockers: [],
      layers: wire.clips.map((clip) => ({
        clip_id: clip.clip_id,
        asset_id: clip.asset_id,
        track_id: clip.track_id,
        source_frame: frame,
        source_pts: frame * 512,
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
      audio_span: {
        clip_id: wire.clips[0]!.clip_id,
        asset_id: wire.assets[0]!.asset_id,
        output_start_sample: frame * 2000,
        output_end_sample: (frame + 1) * 2000,
        source_start_sample: frame * 2000,
        source_end_sample: (frame + 1) * 2000,
      },
    };
  },
});
function render() {
  document.querySelector("#status")!.textContent =
    `${follower.status().state}: ${follower.status().reason}`;
}
follower.subscribe(render);
for (const [id, action] of Object.entries({
  open: () =>
    follower.runtime.open(manifest, snapshot, RUNTIME_PROFILE_FINGERPRINT),
  play: () => follower.runtime.play(),
  pause: () => follower.runtime.pause(),
  suspend: () => follower.suspend(),
  resume: () => follower.resume(),
  close: () => follower.runtime.close(),
}))
  document
    .querySelector(`#${id}`)!
    .addEventListener("click", () => void action().then(render));
document
  .querySelector<HTMLInputElement>("#seek")!
  .addEventListener("input", (event) => {
    void follower.runtime
      .seek(Number((event.target as HTMLInputElement).value))
      .then(render);
  });
declare global {
  interface Window {
    embeddedAudioFollowerHarness: {
      snapshot: () => {
        status: ReturnType<typeof follower.status>;
        transport: ReturnType<typeof follower.runtime.snapshot>;
        audible: number;
        nativeAudible: number;
        audioContexts: number;
        active: number;
        resolverCalls: number;
        acquired: number;
        released: number;
      };
    };
  }
}
window.embeddedAudioFollowerHarness = {
  snapshot: () => ({
    status: follower.status(),
    transport: follower.runtime.snapshot(),
    audible: audioNodes.size,
    nativeAudible: [...owners].filter((owner) => !owner.muted).length,
    audioContexts: audioContexts.size,
    active: owners.size,
    resolverCalls,
    acquired,
    released,
  }),
};
render();
