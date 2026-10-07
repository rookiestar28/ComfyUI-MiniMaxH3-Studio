import { describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  RESOLVED_SCENE_SCHEMA,
} from "../src/contracts/compositionCodec";
import {
  createVisualCompositionSession,
  type VisualCompositionBinding,
} from "../src/runtime/visualCompositionSession";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import {
  evaluateMediaCapabilities,
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
} from "../src/runtime/mediaCapabilities";
import {
  createEditorRuntime,
  type EditorRuntime,
  type MediaTransport,
  type MediaTransportFactory,
  type RuntimeSceneResolver,
} from "../src/runtime/editorRuntime";
import {
  runtimeContractAdmission,
  runtimeFixtureScene,
} from "./fixtures/browserNleRuntimeFixture";

const compositor = vi.hoisted(() => ({ painted: [] as number[] }));

vi.mock("../src/runtime/visualCompositor", () => ({
  createVisualCompositor: () => {
    const receipt = (frame: number | null, generation: number) =>
      Object.freeze({
        schema: "h3.visual_compositor_receipt.v1",
        status: frame === null ? "unavailable" : "presented",
        profileId: "h3.composition.v1",
        publicFingerprint: "sha256:test",
        frame,
        generation,
        previewWidth: 320,
        previewHeight: 180,
        renderedLayerCount: 0,
        blocker: null,
        browserPreviewOnly: true,
      });
    return {
      // These rows isolate the full replacement path's epoch cancellation; retained owners have
      // separate session/runtime coverage. This painter deliberately cannot keep its rung.
      replace: () => false,
      render: (
        scene: { frame: number },
        _resources: unknown,
        generation: number,
      ) => {
        compositor.painted.push(scene.frame);
        return receipt(scene.frame, generation);
      },
      clear: (generation = 0) => receipt(null, generation),
      close: () => receipt(null, 0),
    };
  },
}));

function qualifiedCapability() {
  const admission = runtimeContractAdmission();
  return evaluateMediaCapabilities(
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
}

type HeldResolution = {
  frame: number;
  signal: AbortSignal;
  resolve(): void;
};

function createCancellationHarness() {
  compositor.painted.length = 0;
  const snapshot = decodePublicCompositionSnapshot(
    structuredClone(fixture.snapshot),
  );
  const manifest = buildPublicAssetManifest(snapshot);
  const held: HeldResolution[] = [];
  const runtimes: EditorRuntime[] = [];
  let maximumPendingOperations = 0;
  const scene = (frame: number) => ({
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: snapshot.profileId,
    public_fingerprint: snapshot.publicFingerprint,
    frame,
    layers: [],
    audio_span: null,
    blockers: [],
  });
  const resolveScene: RuntimeSceneResolver = (frame, _snapshot, context) => {
    if (frame === 0) return scene(frame);
    return new Promise((resolve, reject) => {
      let settled = false;
      const abort = () => {
        if (settled) return;
        settled = true;
        context.signal.removeEventListener("abort", abort);
        reject(new Error("cancelled"));
      };
      context.signal.addEventListener("abort", abort, { once: true });
      held.push({
        frame,
        signal: context.signal,
        resolve: () => {
          if (settled) return;
          settled = true;
          context.signal.removeEventListener("abort", abort);
          resolve(scene(frame));
        },
      });
      if (context.signal.aborted) abort();
    });
  };
  const capability = qualifiedCapability();
  const binding: VisualCompositionBinding = {
    snapshot,
    manifest,
    capability,
    resolveScene,
    leaseClient: {
      create: vi.fn(),
      acquireVideoSource: vi.fn(),
      close: vi.fn(async () => undefined),
    },
  };
  const session = createVisualCompositionSession({
    ...binding,
    canvas: document.createElement("canvas"),
    runtimeFactory: (options) => {
      const runtime = createEditorRuntime({
        capability: options.capability,
        resolveScene: options.resolveScene,
        openTransport: vi.fn(async () => {
          throw new Error("unexpected_video_transport");
        }),
      });
      runtime.subscribe(() => {
        maximumPendingOperations = Math.max(
          maximumPendingOperations,
          runtime.snapshot().resources.pendingOperations,
        );
      });
      runtimes.push(runtime);
      return runtime;
    },
  });
  return {
    binding,
    held,
    runtimes,
    session,
    maximumPendingOperations: () => maximumPendingOperations,
  };
}

function createTransportControlHarness() {
  compositor.painted.length = 0;
  const snapshot = decodePublicCompositionSnapshot(
    structuredClone(fixture.snapshot),
  );
  const manifest = buildPublicAssetManifest(snapshot);
  const capability = qualifiedCapability();
  let runtime!: EditorRuntime;
  let holdNextPlay = false;
  let holdNextPause = false;
  let releaseHeldPause: (() => void) | undefined;
  let acquireVideoOwner!: (
    request: Parameters<MediaTransportFactory>[0],
  ) => Promise<Readonly<{ release(): Promise<void> }>>;
  const factory: MediaTransportFactory = vi.fn(async (request) => {
    const { asset, ownerId, epoch } = request;
    const sourceOwner = await acquireVideoOwner(request);
    const transport: MediaTransport = {
      ownerId,
      assetId: asset.assetId,
      openedEpoch: epoch,
      pendingFrameObservations: 0,
      async seek(request) {
        return {
          ownerId,
          assetId: asset.assetId,
          epoch: request.epoch,
          sourceFrame: request.sourceFrame,
          sourcePts: request.sourcePts,
          observedSourcePts: request.sourcePts,
          observation: "request_video_frame_callback",
        };
      },
      async play(signal) {
        if (!holdNextPlay) return;
        holdNextPlay = false;
        await new Promise<void>((resolve, reject) => {
          const abort = () => {
            signal.removeEventListener("abort", abort);
            reject(new Error("cancelled"));
          };
          signal.addEventListener("abort", abort, { once: true });
          if (signal.aborted) abort();
        });
      },
      async pause() {
        if (!holdNextPause) return;
        holdNextPause = false;
        await new Promise<void>((resolve) => {
          releaseHeldPause = resolve;
        });
      },
      subscribe() {
        return () => undefined;
      },
      async close() {
        await sourceOwner.release();
      },
    };
    return transport;
  });
  const binding: VisualCompositionBinding = {
    snapshot,
    manifest,
    capability,
    resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
    leaseClient: {
      create: vi.fn(),
      acquireVideoSource: vi.fn(async () => {
        const element = document.createElement("video");
        Object.defineProperties(element, {
          videoWidth: { configurable: true, value: 320 },
          videoHeight: { configurable: true, value: 180 },
        });
        return {
          element,
          geometry: {
            schema: "h3.authoring.media_geometry.v1" as const,
            sourceWidth: 1920,
            sourceHeight: 1080,
            derivativeWidth: 320,
            derivativeHeight: 180,
          },
          release: vi.fn(async () => undefined),
        };
      }),
      close: vi.fn(async () => undefined),
    },
  };
  const session = createVisualCompositionSession({
    ...binding,
    canvas: document.createElement("canvas"),
    runtimeFactory: (options) => {
      acquireVideoOwner = (request) =>
        options.leaseClient.acquireVideoSource(request, {
          snapshot,
          manifest,
          clipId: request.ownerId,
          sourceStartFrame: 0,
          sourceEndFrame: request.asset.sourceFrameCount!,
        });
      runtime = createEditorRuntime({
        capability: options.capability,
        resolveScene: options.resolveScene,
        openTransport: factory,
      });
      return runtime;
    },
  });
  return {
    runtime: () => runtime,
    session,
    holdPlay: () => {
      holdNextPlay = true;
    },
    holdPause: () => {
      holdNextPause = true;
    },
    releasePause: () => {
      releaseHeldPause?.();
      releaseHeldPause = undefined;
    },
  };
}

type ObservedSettlement = {
  completion: Promise<unknown>;
  settledAt(): number | null;
};

function observeSettlement(promise: Promise<unknown>): ObservedSettlement {
  let timestamp: number | null = null;
  return {
    completion: promise.then(
      (value) => {
        timestamp = performance.now();
        return value;
      },
      (error: unknown) => {
        timestamp = performance.now();
        throw error;
      },
    ),
    settledAt: () => timestamp,
  };
}

async function expectSettledWithin(
  settlement: ObservedSettlement,
  supersededAt: number,
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const outcome = await Promise.race([
    settlement.completion.then(
      () => "settled" as const,
      () => "rejected" as const,
    ),
    new Promise<"timeout">((resolve) => {
      timer = setTimeout(() => resolve("timeout"), 250);
    }),
  ]);
  if (timer !== undefined) clearTimeout(timer);
  expect(outcome).toBe("settled");
  expect(settlement.settledAt()).not.toBeNull();
  expect(settlement.settledAt()! - supersededAt).toBeLessThanOrEqual(250);
}

describe("visual composition runtime cancellation", () => {
  it("lets an explicit seek supersede a pending native play", async () => {
    const harness = createTransportControlHarness();
    await harness.session.open();
    expect(harness.session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(harness.session.getPresentation()?.frame).toBe(0);
    try {
      harness.holdPlay();
      const playing = harness.session.play();
      await vi.waitFor(() =>
        expect(harness.runtime().snapshot().resources.pendingOperations).toBe(
          1,
        ),
      );
      const seeking = harness.session.seek(12);
      await Promise.all([playing, seeking]);
      expect(harness.session.getSnapshot()).toMatchObject({
        status: "paused",
        blocker: null,
      });
      expect(harness.session.getPresentation()?.frame).toBe(12);
      expect(compositor.painted).toEqual([0, 12]);
    } finally {
      await harness.session.close();
    }
  });

  it("lets an explicit seek supersede a pending native pause", async () => {
    const harness = createTransportControlHarness();
    await harness.session.open();
    expect(harness.session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(harness.session.getPresentation()?.frame).toBe(0);
    await harness.session.play();
    try {
      harness.holdPause();
      const pausing = harness.session.pause();
      await vi.waitFor(() =>
        expect(harness.runtime().snapshot().resources.pendingOperations).toBe(
          1,
        ),
      );
      const seeking = harness.session.seek(12);
      harness.releasePause();
      await Promise.all([pausing, seeking]);
      expect(harness.session.getSnapshot()).toMatchObject({
        status: "paused",
        blocker: null,
      });
      expect(harness.session.getPresentation()?.frame).toBe(12);
      expect(compositor.painted).toEqual([0, 12]);
    } finally {
      harness.releasePause();
      await harness.session.close();
    }
  });

  it("does not let a stale step move over a newer seek", async () => {
    const harness = createTransportControlHarness();
    await harness.session.open();
    expect(harness.session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(harness.session.getPresentation()?.frame).toBe(0);
    await harness.session.play();
    try {
      harness.holdPause();
      const stepping = harness.session.step(1);
      await vi.waitFor(() =>
        expect(harness.runtime().snapshot().resources.pendingOperations).toBe(
          1,
        ),
      );
      const seeking = harness.session.seek(12);
      harness.releasePause();
      await Promise.all([stepping, seeking]);
      expect(harness.session.getSnapshot()).toMatchObject({
        status: "paused",
        blocker: null,
      });
      expect(harness.session.getPresentation()?.frame).toBe(12);
      expect(compositor.painted).toEqual([0, 12]);
    } finally {
      harness.releasePause();
      await harness.session.close();
    }
  });

  it("supersedes the actual runtime epoch and fences repeated-frame intents", async () => {
    const { held, maximumPendingOperations, session } =
      createCancellationHarness();
    await session.open();
    expect(compositor.painted).toEqual([0]);

    const seek8 = observeSettlement(session.seek(8));
    await vi.waitFor(() => expect(held.map(({ frame }) => frame)).toEqual([8]));
    const seek8SupersededAt = performance.now();
    const seek12 = observeSettlement(session.seek(12));
    await Promise.all([
      expectSettledWithin(seek8, seek8SupersededAt),
      vi.waitFor(() => expect(held.map(({ frame }) => frame)).toEqual([8, 12])),
    ]);
    expect(held[0]!.signal.aborted).toBe(true);
    const seek12SupersededAt = performance.now();
    const seek19 = observeSettlement(session.seek(19));
    await Promise.all([
      expectSettledWithin(seek12, seek12SupersededAt),
      vi.waitFor(() =>
        expect(held.map(({ frame }) => frame)).toEqual([8, 12, 19]),
      ),
    ]);
    expect(held[1]!.signal.aborted).toBe(true);
    const seek19SupersededAt = performance.now();
    const repeated19 = observeSettlement(session.seek(19));
    await Promise.all([
      expectSettledWithin(seek19, seek19SupersededAt),
      vi.waitFor(() =>
        expect(held.map(({ frame }) => frame)).toEqual([8, 12, 19, 19]),
      ),
    ]);
    expect(held[2]!.signal.aborted).toBe(true);
    expect(compositor.painted).toEqual([0]);

    held[3]!.resolve();
    await repeated19.completion;
    expect(session.getPresentation()?.frame).toBe(19);
    expect(compositor.painted).toEqual([0, 19]);
    expect(maximumPendingOperations()).toBeLessThanOrEqual(2);
    await session.close();
  });

  it("supersedes a pending seek back to the last presented frame on pause", async () => {
    const { held, session } = createCancellationHarness();
    await session.open();
    const pending = observeSettlement(session.seek(8));
    await vi.waitFor(() => expect(held.map(({ frame }) => frame)).toEqual([8]));
    const supersededAt = performance.now();
    const paused = session.pause();
    await Promise.all([
      expectSettledWithin(pending, supersededAt),
      vi.waitFor(() => expect(held[0]!.signal.aborted).toBe(true)),
    ]);
    await paused;
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(session.getPresentation()?.frame).toBe(0);
    expect(compositor.painted).toEqual([0, 0]);
    await session.close();
  });

  it("invalidates pending seek paint when close interrupts its runtime epoch", async () => {
    const { held, session } = createCancellationHarness();
    await session.open();
    const pending = observeSettlement(session.seek(8));
    await vi.waitFor(() => expect(held.map(({ frame }) => frame)).toEqual([8]));
    const supersededAt = performance.now();
    const closing = session.close();
    await Promise.all([
      expectSettledWithin(pending, supersededAt),
      vi.waitFor(() => expect(held[0]!.signal.aborted).toBe(true)),
    ]);
    await closing;
    expect(session.getSnapshot().status).toBe("closed");
    expect(compositor.painted).toEqual([0]);
  });

  it("invalidates pending seek paint when replacement opens a new runtime", async () => {
    const { binding, held, runtimes, session } = createCancellationHarness();
    await session.open();
    const pending = observeSettlement(session.seek(8));
    await vi.waitFor(() => expect(held.map(({ frame }) => frame)).toEqual([8]));
    const supersededAt = performance.now();
    const replacing = session.replace(binding);
    await Promise.all([
      expectSettledWithin(pending, supersededAt),
      vi.waitFor(() => expect(held[0]!.signal.aborted).toBe(true)),
    ]);
    await replacing;
    expect(runtimes).toHaveLength(2);
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(session.getPresentation()?.frame).toBe(0);
    expect(compositor.painted).toEqual([0, 0]);
    await session.close();
  });
});
