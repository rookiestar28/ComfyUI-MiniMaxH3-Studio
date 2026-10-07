import { Profiler, useState } from "react";
import { createRoot } from "react-dom/client";
import compositionFixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";

import {
  CompositionPreview,
  type CompositionPreviewSessionFactory,
} from "../src/components/CompositionPreview";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import type { RuntimeSceneResolver } from "../src/runtime/editorRuntime";
import {
  RUNTIME_PROFILE,
  RUNTIME_PROFILE_FINGERPRINT,
  type RuntimeCapabilityDisposition,
} from "../src/runtime/mediaCapabilities";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import type {
  VisualCompositionBinding,
  VisualCompositionSession,
  VisualCompositionStatus,
} from "../src/runtime/visualCompositionSession";
import type { VisualCompositorReceipt } from "../src/runtime/visualCompositor";

type ResourceCounters = Readonly<{
  imageOwners: number;
  fontOwners: number;
  videoOwners: number;
  staticLeases: number;
  pendingOperations: number;
  blobBytes: number;
}>;

export type VisualCompositionHarnessSnapshot = Readonly<{
  ready: boolean;
  mounted: boolean;
  activeSessions: number;
  reactRenderCount: number;
  statusListenerCount: number;
  frameListenerCount: number;
  currentFrame: number | null;
  currentStatus: VisualCompositionStatus;
  publicFingerprint: string;
  bindingMatched: boolean;
  operations: Readonly<{
    factory: number;
    open: number;
    seek: number;
    previous: number;
    next: number;
    play: number;
    pause: number;
    recover: number;
    replace: number;
    close: number;
    framePublications: number;
  }>;
  resources: ResourceCounters;
}>;

type HarnessApi = Readonly<{
  snapshot(): VisualCompositionHarnessSnapshot;
}>;

declare global {
  interface Window {
    visualCompositionPreviewHarness: HarnessApi;
  }
}

const EMPTY_RESOURCES: ResourceCounters = Object.freeze({
  imageOwners: 0,
  fontOwners: 0,
  videoOwners: 0,
  staticLeases: 0,
  pendingOperations: 0,
  blobBytes: 0,
});
const SYNTHETIC_RESOURCES: ResourceCounters = Object.freeze({
  imageOwners: 1,
  fontOwners: 1,
  videoOwners: 2,
  staticLeases: 2,
  pendingOperations: 0,
  blobBytes: 96,
});
const capability: RuntimeCapabilityDisposition = Object.freeze({
  status: "available",
  blocker: null,
  profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
  engineProfileId: "h3.native_media_canvas_backend.v1",
  fallback: "selected_source_only",
  frameObserver: "request_video_frame_callback",
  qualified: true,
  limits: RUNTIME_PROFILE.limits,
});
const unusedResolver: RuntimeSceneResolver = async () => {
  throw new Error("synthetic session boundary does not resolve media");
};
const unusedLeaseClient = Object.freeze(
  {},
) as unknown as AuthoringMediaSourceLeaseClient;

function fixtureWire(revisionOffset: number): Record<string, unknown> {
  const wire = structuredClone(
    compositionFixture.snapshot,
  ) as unknown as Record<string, unknown>;
  wire.timeline_revision = Number(wire.timeline_revision) + revisionOffset;
  wire.timeline_fingerprint = `sha256:${revisionOffset === 0 ? "2" : "c".repeat(1)}${"2".repeat(63)}`;
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return wire;
}

function createBinding(revisionOffset: number): VisualCompositionBinding {
  const snapshot = decodePublicCompositionSnapshot(fixtureWire(revisionOffset));
  return Object.freeze({
    snapshot,
    manifest: buildPublicAssetManifest(snapshot),
    capability,
    resolveScene: unusedResolver,
    leaseClient: unusedLeaseClient,
  });
}

function makeStatus(
  source: VisualCompositionBinding,
  status: VisualCompositionStatus["status"],
  blocker: VisualCompositionStatus["blocker"] = null,
): VisualCompositionStatus {
  return Object.freeze({
    status,
    blocker,
    browserPreviewOnly: true,
    durationFrames: Number(source.snapshot.output.durationFrames),
  });
}

function makePresentation(
  source: VisualCompositionBinding,
  frame: number,
  generation: number,
): VisualCompositorReceipt {
  return Object.freeze({
    schema: "h3.visual_compositor_receipt.v1",
    status: "presented",
    profileId: source.snapshot.profileId,
    publicFingerprint: source.snapshot.publicFingerprint,
    frame,
    generation,
    previewWidth: 320,
    previewHeight: 180,
    renderedLayerCount: 1,
    blocker: null,
    browserPreviewOnly: true,
  });
}

const operations = {
  factory: 0,
  open: 0,
  seek: 0,
  previous: 0,
  next: 0,
  play: 0,
  pause: 0,
  recover: 0,
  replace: 0,
  close: 0,
  framePublications: 0,
};
let ready = false;
let mounted = true;
let activeSessions = 0;
let reactRenderCount = 0;
let bindingMatched = false;
let statusListenerCount = 0;
let frameListenerCount = 0;
let currentStatus = makeStatus(createBinding(0), "closed");
let currentPresentation: VisualCompositorReceipt | null = null;
let currentResources = EMPTY_RESOURCES;
let activeController:
  | Readonly<{
      loseSource(): void;
      publishBurst(): void;
    }>
  | undefined;

const sessionFactory: CompositionPreviewSessionFactory = (options) => {
  operations.factory += 1;
  activeSessions += 1;
  let source: VisualCompositionBinding = options;
  let frame = 0;
  let generation = 0;
  let closed = false;
  const statusListeners = new Set<() => void>();
  const frameListeners = new Set<() => void>();
  bindingMatched =
    source.snapshot.publicFingerprint === source.manifest.publicFingerprint &&
    options.canvas.isConnected;

  const publishStatus = (
    status: VisualCompositionStatus["status"],
    blocker: VisualCompositionStatus["blocker"] = null,
  ) => {
    currentStatus = makeStatus(source, status, blocker);
    for (const listener of statusListeners) listener();
  };
  const paint = (nextFrame: number) => {
    const context = options.canvas.getContext("2d", {
      alpha: true,
      willReadFrequently: true,
    });
    if (context === null) throw new Error("synthetic canvas unavailable");
    if (options.canvas.width !== 320) options.canvas.width = 320;
    if (options.canvas.height !== 180) options.canvas.height = 180;
    context.fillStyle = `rgb(${(nextFrame * 17) % 256}, ${(nextFrame * 29) % 256}, ${(nextFrame * 43) % 256})`;
    context.fillRect(0, 0, options.canvas.width, options.canvas.height);
    frame = nextFrame;
    currentPresentation = makePresentation(source, frame, ++generation);
    operations.framePublications += 1;
    for (const listener of frameListeners) listener();
  };
  const session = Object.freeze({
    canRebind: () => false,
    async open() {
      operations.open += 1;
      publishStatus("opening");
      await Promise.resolve();
      if (closed) return;
      currentResources = SYNTHETIC_RESOURCES;
      paint(0);
      publishStatus("paused");
    },
    async seek(nextFrame: number) {
      operations.seek += 1;
      if (closed || currentStatus.status === "blocked") return;
      paint(Math.max(0, Math.min(currentStatus.durationFrames - 1, nextFrame)));
    },
    async step(direction: -1 | 1) {
      if (direction === -1) operations.previous += 1;
      else operations.next += 1;
      if (closed || currentStatus.status === "blocked") return;
      paint(
        Math.max(
          0,
          Math.min(currentStatus.durationFrames - 1, frame + direction),
        ),
      );
    },
    async play() {
      operations.play += 1;
      if (!closed && currentStatus.status === "paused")
        publishStatus("playing");
    },
    async pause() {
      operations.pause += 1;
      if (!closed && currentStatus.status === "playing")
        publishStatus("paused");
    },
    async recover() {
      operations.recover += 1;
      if (closed || currentStatus.status !== "blocked") return;
      publishStatus("opening");
      await Promise.resolve();
      if (closed) return;
      currentResources = SYNTHETIC_RESOURCES;
      paint(frame);
      publishStatus("paused");
    },
    async replace(next: VisualCompositionBinding) {
      operations.replace += 1;
      if (closed) return;
      publishStatus("opening");
      await Promise.resolve();
      if (closed) return;
      source = next;
      bindingMatched =
        source.snapshot.publicFingerprint === source.manifest.publicFingerprint;
      paint(frame);
      publishStatus("paused");
    },
    async close() {
      operations.close += 1;
      if (closed) return;
      closed = true;
      publishStatus("closing");
      currentPresentation = null;
      currentResources = EMPTY_RESOURCES;
      activeSessions -= 1;
      statusListeners.clear();
      frameListeners.clear();
      statusListenerCount = 0;
      frameListenerCount = 0;
      activeController = undefined;
      currentStatus = makeStatus(source, "closed");
    },
    getSnapshot: () => currentStatus,
    getPresentation: () => currentPresentation,
    getResources: () => currentResources,
    subscribe(listener: () => void) {
      statusListeners.add(listener);
      statusListenerCount = statusListeners.size;
      return () => {
        statusListeners.delete(listener);
        statusListenerCount = statusListeners.size;
      };
    },
    subscribeFrame(listener: () => void) {
      frameListeners.add(listener);
      frameListenerCount = frameListeners.size;
      return () => {
        frameListeners.delete(listener);
        frameListenerCount = frameListeners.size;
      };
    },
    // This harness paints a solid fill at one fixed size and never reaches the compositor, so a
    // measured picture box has nothing to change here; the real session's resize is exercised by
    // `visualCompositor.test.ts` and by the reference shell in a browser.
    resize() {},
    previewVisualTransform() {
      return null;
    },
    getVisualLayerGeometry() {
      return null;
    },
  }) satisfies VisualCompositionSession;
  activeController = Object.freeze({
    loseSource() {
      if (closed) return;
      currentPresentation = null;
      currentResources = EMPTY_RESOURCES;
      options.canvas.getContext("2d")?.clearRect(0, 0, 320, 180);
      for (const listener of frameListeners) listener();
      publishStatus("blocked", "source_unavailable");
    },
    publishBurst() {
      if (closed || currentStatus.status === "blocked") return;
      for (let nextFrame = 0; nextFrame < 32; nextFrame += 1) paint(nextFrame);
    },
  });
  return session;
};

function HarnessApp() {
  const [binding, setBinding] = useState(() => createBinding(0));
  const [showPreview, setShowPreview] = useState(true);
  const [replacement, setReplacement] = useState(0);
  mounted = showPreview;
  return (
    <main>
      <h1>Visual composition preview hermetic journey</h1>
      <p>
        Synthetic session boundary; this page does not qualify media decoding.
      </p>
      <div role="group" aria-label="Hermetic session controls">
        <button type="button" onClick={() => activeController?.loseSource()}>
          Simulate source loss
        </button>
        <button type="button" onClick={() => activeController?.publishBurst()}>
          Publish frame burst
        </button>
        <button
          type="button"
          onClick={() => {
            const next = replacement + 1;
            setReplacement(next);
            setBinding(createBinding(next));
          }}
        >
          Replace public binding
        </button>
        <button type="button" onClick={() => setShowPreview(false)}>
          Unmount preview
        </button>
      </div>
      {showPreview ? (
        <Profiler
          id="visual-composition-preview"
          onRender={() => {
            reactRenderCount += 1;
          }}
        >
          <CompositionPreview
            binding={binding}
            sessionFactory={sessionFactory}
          />
        </Profiler>
      ) : (
        <p role="status">Composition preview unmounted.</p>
      )}
    </main>
  );
}

window.visualCompositionPreviewHarness = Object.freeze({
  snapshot: (): VisualCompositionHarnessSnapshot =>
    Object.freeze({
      ready,
      mounted,
      activeSessions,
      reactRenderCount,
      statusListenerCount,
      frameListenerCount,
      currentFrame: currentPresentation?.frame ?? null,
      currentStatus,
      publicFingerprint:
        currentStatus.status === "closed"
          ? ""
          : (currentPresentation?.publicFingerprint ?? ""),
      bindingMatched,
      operations: Object.freeze({ ...operations }),
      resources: currentResources,
    }),
});

const root = document.getElementById("root");
if (root === null)
  throw new Error("visual composition preview root is missing");
createRoot(root).render(<HarnessApp />);
ready = true;
