import {
  createEditorRuntime,
  createHtmlMediaElementTransportFactory,
} from "../src/runtime/editorRuntime";
import {
  evaluateMediaCapabilities,
  observeBrowserMediaCapabilities,
  RUNTIME_PROFILE_FINGERPRINT,
} from "../src/runtime/mediaCapabilities";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import {
  runtimeContractAdmission,
  runtimeFixtureScene,
  runtimeFixtureSnapshot,
} from "../tests/fixtures/browserNleRuntimeFixture";

let liveVideos = 0;
let maximumVideos = 0;
let revision = 11;
let notifications = 0;
const frames: number[] = [];
const observedPts: number[] = [];
const seekObservations: Array<{
  outputFrame: number;
  elapsedMs: number;
  sources: Array<{ requestedPts: number; observedPts: number | null }>;
}> = [];
let maximumPendingOperations = 0;
let maximumPendingRvfc = 0;
let maximumCloseMs = 0;
let maximumSeekMs = 0;
let maximumCancelMs = 0;
const elements = new Set<HTMLVideoElement>();
const observation = observeBrowserMediaCapabilities();
const params = new URLSearchParams(location.search);
const eventFallback = params.get("observer") === "events";
const selectedMedia = params.get("media") ?? "cfr";
const dual = params.get("dual") === "1";
const allowedMedia = new Set(["cfr", "vfr", "invalid", "truncated", "mse"]);
if (!allowedMedia.has(selectedMedia)) throw new Error("unknown_fixture");
const testAdmission = runtimeContractAdmission();
const runtime = createEditorRuntime({
  capability: evaluateMediaCapabilities(
    eventFallback
      ? { ...observation, requestVideoFrameCallback: false }
      : observation,
    testAdmission.qualification,
    testAdmission.authority,
  ),
  resolveScene: (frame, snapshot) => runtimeFixtureScene(frame, snapshot, dual),
  openTransport: createHtmlMediaElementTransportFactory(
    async ({ signal, asset }) => {
      const element = document.createElement("video");
      element.muted = true;
      element.playsInline = true;
      element.preload = "auto";
      element.src = `/runtime-media/${asset.assetId === "vid-overlay" ? "lane" : selectedMedia}.mp4`;
      document.body.append(element);
      elements.add(element);
      liveVideos += 1;
      maximumVideos = Math.max(maximumVideos, liveVideos);
      let released = false;
      const release = async () => {
        if (released) return;
        released = true;
        element.removeAttribute("src");
        element.load();
        element.remove();
        elements.delete(element);
        liveVideos -= 1;
      };
      try {
        await new Promise<void>((resolve, reject) => {
          const cleanup = () => {
            element.removeEventListener("loadeddata", ready);
            element.removeEventListener("error", error);
            signal.removeEventListener("abort", aborted);
            clearTimeout(timer);
          };
          const ready = () => {
            cleanup();
            resolve();
          };
          const error = () => {
            cleanup();
            reject(new Error("synthetic_media_decode_failed"));
          };
          const aborted = () => {
            cleanup();
            reject(new Error("synthetic_media_cancelled"));
          };
          const timer = setTimeout(error, 3000);
          element.addEventListener("loadeddata", ready, { once: true });
          element.addEventListener("error", error, { once: true });
          signal.addEventListener("abort", aborted, { once: true });
          if (signal.aborted) aborted();
          else element.load();
        });
      } catch (error) {
        await release();
        throw error;
      }
      return { element, release };
    },
    {
      frameObserver: eventFallback
        ? "event_fallback"
        : "request_video_frame_callback",
    },
  ),
});
const unsubscribe = runtime.subscribe(() => {
  notifications += 1;
  const frame = runtime.snapshot().sources[0]?.sourceFrame;
  if (frame !== undefined && frames.at(-1) !== frame) frames.push(frame);
  if (frames.length > 128) frames.shift();
  const measured = runtime.snapshot().sources[0]?.observedSourcePts;
  if (measured !== undefined && observedPts.at(-1) !== measured)
    observedPts.push(measured);
  if (observedPts.length > 128) observedPts.shift();
  maximumPendingOperations = Math.max(
    maximumPendingOperations,
    runtime.snapshot().resources.pendingOperations,
  );
  maximumPendingRvfc = Math.max(
    maximumPendingRvfc,
    runtime.snapshot().resources.pendingRvfcOwners,
  );
});

export const browserNleRuntime = {
  observation,
  async open() {
    const snapshot = runtimeFixtureSnapshot(revision, selectedMedia);
    return runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
  },
  async replace() {
    const snapshot = runtimeFixtureSnapshot(++revision, selectedMedia);
    return runtime.replace(buildPublicAssetManifest(snapshot), snapshot);
  },
  async cancelPendingOpen() {
    const pending = browserNleRuntime.open();
    const waitStarted = performance.now();
    while (liveVideos === 0 && performance.now() - waitStarted < 1000)
      await new Promise((resolve) => setTimeout(resolve, 0));
    const liveBefore = liveVideos;
    const started = performance.now();
    const close = await browserNleRuntime.close();
    const opening = await pending;
    maximumCancelMs = Math.max(maximumCancelMs, performance.now() - started);
    return { liveBefore, close, opening };
  },
  async seek(frame: number) {
    const started = performance.now();
    const receipt = await runtime.seek(frame);
    const elapsedMs = performance.now() - started;
    maximumSeekMs = Math.max(maximumSeekMs, elapsedMs);
    if (receipt.status === "applied") {
      seekObservations.push({
        outputFrame: frame,
        elapsedMs,
        sources: runtime.snapshot().sources.map((source) => ({
          requestedPts: source.sourcePts,
          observedPts: source.observedSourcePts ?? null,
        })),
      });
      if (seekObservations.length > 64) seekObservations.shift();
    }
    return receipt;
  },
  play: () => runtime.play(),
  pause: () => runtime.pause(),
  async close() {
    const started = performance.now();
    const receipt = await runtime.close();
    maximumCloseMs = Math.max(maximumCloseMs, performance.now() - started);
    return receipt;
  },
  pixel() {
    const element = [...elements][0];
    if (!element) throw new Error("fixture_not_open");
    // Measurement-only canvas; the substrate does not own a product compositor.
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const context = canvas.getContext("2d")!;
    context.drawImage(element, 0, 0, 1, 1);
    const rgb = Array.from(context.getImageData(0, 0, 1, 1).data).slice(0, 3);
    canvas.width = 0;
    canvas.height = 0;
    return rgb;
  },
  snapshot: () => runtime.snapshot(),
  metrics: () => ({
    liveVideos,
    maximumVideos,
    notifications,
    frames: [...frames],
    observedPts: [...observedPts],
    seekObservations: structuredClone(seekObservations),
    maximumPendingOperations,
    maximumPendingRvfc,
    maximumCloseMs,
    maximumSeekMs,
    maximumCancelMs,
    nativeTimes: [...elements].map((element) => element.currentTime),
    nativePaused: [...elements].map((element) => element.paused),
  }),
};
Object.assign(globalThis, { browserNleRuntime });
addEventListener(
  "pagehide",
  () => {
    unsubscribe();
    void runtime.close();
  },
  { once: true },
);
