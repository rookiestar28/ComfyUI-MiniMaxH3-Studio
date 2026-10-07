import { afterEach, describe, expect, it, vi } from "vitest";

import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  evaluateMediaCapabilities,
} from "../src/runtime/mediaCapabilities";
import {
  createEditorRuntime,
  type MediaPresentation,
  type MediaTransport,
  type MediaTransportFactory,
  type MediaTransportSeek,
} from "../src/runtime/editorRuntime";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import {
  runtimeContractAdmission,
  runtimeFixtureScene,
  runtimeFixtureSnapshot,
} from "./fixtures/browserNleRuntimeFixture";

function admittedCapability() {
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

function twoVideoScene(
  frame: number,
  snapshot: ReturnType<typeof runtimeFixtureSnapshot>,
) {
  return runtimeFixtureScene(frame, snapshot, true);
}

type ControlledTransport = MediaTransport & {
  pending: number;
  playCalls: number;
  emit(value: MediaPresentation): void;
  fail(): void;
};

function abortAwareTransportFactory(control: { hold: boolean }) {
  const transports: ControlledTransport[] = [];
  const factory: MediaTransportFactory = async ({ asset, ownerId, epoch }) => {
    const listeners = new Set<(value: MediaPresentation) => void>();
    const failureListeners = new Set<(code: "transport_failure") => void>();
    const transport: ControlledTransport = {
      ownerId,
      assetId: asset.assetId,
      openedEpoch: epoch,
      pending: 0,
      playCalls: 0,
      get pendingFrameObservations() {
        return transport.pending;
      },
      async seek(request: MediaTransportSeek) {
        if (control.hold) {
          transport.pending += 1;
          await new Promise<void>((resolve, reject) => {
            const abort = () => {
              request.signal.removeEventListener("abort", abort);
              reject(new Error("cancelled"));
            };
            request.signal.addEventListener("abort", abort, { once: true });
          }).finally(() => {
            transport.pending -= 1;
          });
        }
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
      async play() {
        transport.playCalls += 1;
      },
      async pause() {},
      subscribe(listener) {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      subscribeFailure(listener) {
        failureListeners.add(listener);
        return () => failureListeners.delete(listener);
      },
      emit(value) {
        for (const listener of [...listeners]) listener(value);
      },
      fail() {
        for (const listener of [...failureListeners])
          listener("transport_failure");
      },
      async close() {
        listeners.clear();
        failureListeners.clear();
      },
    };
    transports.push(transport);
    return transport;
  };
  return { factory, transports };
}

function closeControlledTransportFactory(control: {
  close: Promise<void>;
}): MediaTransportFactory {
  return async ({ asset, ownerId, epoch }) => ({
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
      };
    },
    async play() {},
    async pause() {},
    subscribe() {
      return () => undefined;
    },
    async close() {
      await control.close;
    },
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("EditorRuntime lifecycle arbitration", () => {
  it("preserves seek anchors while publishing only well-formed observed PTS facts", async () => {
    const snapshot = runtimeFixtureSnapshot();
    const harness = abortAwareTransportFactory({ hold: false });
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    expect(runtime.snapshot().sources[0]).toMatchObject({
      sourceFrame: 0,
      sourcePts: 0,
      observedSourcePts: 0,
      observation: "request_video_frame_callback",
    });

    harness.transports[0]!.emit({
      ownerId: "clip-main",
      assetId: "vid-primary",
      epoch: runtime.snapshot().epoch,
      sourceFrame: 0,
      sourcePts: 0,
      observedSourcePts: 512,
      observation: "request_video_frame_callback",
    });
    expect(runtime.snapshot().sources[0]).toMatchObject({
      sourceFrame: 0,
      sourcePts: 0,
      observedSourcePts: 512,
    });

    harness.transports[0]!.emit({
      ownerId: "clip-main",
      assetId: "vid-primary",
      epoch: runtime.snapshot().epoch,
      sourceFrame: 0,
      sourcePts: 0,
      observedSourcePts: -1,
      observation: "request_video_frame_callback",
    });
    expect(runtime.snapshot().sources[0]?.observedSourcePts).toBe(512);

    harness.transports[0]!.emit({
      ownerId: "clip-main",
      assetId: "vid-primary",
      epoch: runtime.snapshot().epoch,
      sourceFrame: 0,
      sourcePts: 0,
      observedSourcePts: 1_024,
    });
    harness.transports[0]!.emit({
      ownerId: "clip-main",
      assetId: "vid-primary",
      epoch: runtime.snapshot().epoch,
      sourceFrame: 0,
      sourcePts: 0,
      observation: "event_fallback",
    });
    expect(runtime.snapshot().sources[0]?.observedSourcePts).toBe(512);
    await runtime.close();
  });

  it("blocks and releases a playing session when its owned transport reports failure", async () => {
    const snapshot = runtimeFixtureSnapshot();
    const harness = abortAwareTransportFactory({ hold: false });
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    harness.transports[0]!.fail();
    await vi.waitFor(() => {
      expect(runtime.snapshot()).toMatchObject({
        status: "blocked",
        blocker: { code: "transport_failure" },
        resources: { activeVideoOwners: 0 },
      });
    });
  });

  it("blocks a seek whose transport returns an unpaired observation tag", async () => {
    const snapshot = runtimeFixtureSnapshot();
    const harness = abortAwareTransportFactory({ hold: false });
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    const transport = harness.transports[0]!;
    Object.assign(transport, {
      async seek(request: MediaTransportSeek): Promise<MediaPresentation> {
        return {
          ownerId: transport.ownerId,
          assetId: transport.assetId,
          epoch: request.epoch,
          sourceFrame: request.sourceFrame,
          sourcePts: request.sourcePts,
          observation: "event_fallback",
        };
      },
    });

    await expect(runtime.seek(1)).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "contract_mismatch" },
    });
    expect(runtime.snapshot().resources.activeVideoOwners).toBe(0);
  });

  it("resumes every current owner after a seek that began while playing", async () => {
    const snapshot = runtimeFixtureSnapshot();
    const harness = abortAwareTransportFactory({ hold: false });
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    await runtime.seek(1);
    expect(runtime.snapshot().status).toBe("playing");
    expect(harness.transports[0]?.playCalls).toBe(2);
    await runtime.close();
  });

  it("lets the newest seek drain two cancelled owner operations without exceeding the limit", async () => {
    const snapshot = runtimeFixtureSnapshot();
    const control = { hold: false };
    const harness = abortAwareTransportFactory(control);
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: twoVideoScene,
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    expect(harness.transports[0]?.playCalls).toBe(1);

    control.hold = true;
    const stale = runtime.seek(12);
    await vi.waitFor(() => {
      expect(runtime.snapshot().resources).toMatchObject({
        activeVideoOwners: 2,
        pendingOperations: 2,
        pendingRvfcOwners: 2,
      });
    });

    control.hold = false;
    const newest = runtime.seek(0);
    await expect(stale).resolves.toMatchObject({ status: "cancelled" });
    await expect(newest).resolves.toMatchObject({
      status: "applied",
      outputFrame: 0,
      blocker: null,
    });
    expect(runtime.snapshot()).toMatchObject({
      status: "playing",
      outputFrame: 0,
      resources: {
        activeVideoOwners: 1,
        pendingOperations: 0,
        pendingRvfcOwners: 0,
      },
    });
    expect(harness.transports[0]?.playCalls).toBe(2);
    await runtime.close();
  });

  it("keeps a timed-out close owner in the resource ledger until release settles", async () => {
    vi.useFakeTimers();
    const snapshot = runtimeFixtureSnapshot();
    let finishClose!: () => void;
    const close = new Promise<void>((resolve) => {
      finishClose = resolve;
    });
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
      openTransport: closeControlledTransportFactory({ close }),
    });
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );

    const closing = runtime.close();
    await vi.advanceTimersByTimeAsync(500);
    await expect(closing).resolves.toMatchObject({
      status: "closed",
      blocker: { code: "transport_failure" },
    });
    expect(runtime.snapshot().resources.activeVideoOwners).toBe(1);

    finishClose();
    await vi.advanceTimersByTimeAsync(0);
    expect(runtime.snapshot().resources.activeVideoOwners).toBe(0);
    await expect(runtime.close()).resolves.toMatchObject({
      status: "closed",
      blocker: null,
    });
  });

  it("does not hide cleanup failure behind the blocker that triggered teardown", async () => {
    vi.useFakeTimers();
    const snapshot = runtimeFixtureSnapshot();
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
      openTransport: closeControlledTransportFactory({
        close: new Promise<void>(() => undefined),
      }),
    });
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );

    const blocked = runtime.seek(-1);
    await vi.advanceTimersByTimeAsync(500);
    await expect(blocked).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "transport_failure" },
    });
    expect(runtime.snapshot()).toMatchObject({
      status: "blocked",
      resources: { activeVideoOwners: 1 },
    });
  });

  it("quarantines a valid transport acquired after its opening epoch was cancelled", async () => {
    const snapshot = runtimeFixtureSnapshot();
    let finishAcquire!: () => void;
    let finishClose!: () => void;
    const close = new Promise<void>((resolve) => {
      finishClose = resolve;
    });
    const closeMock = vi.fn(async () => close);
    const factory = vi.fn(
      ({ asset, ownerId, epoch }: Parameters<MediaTransportFactory>[0]) =>
        new Promise<MediaTransport>((resolve) => {
          finishAcquire = () =>
            resolve({
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
                };
              },
              async play() {},
              async pause() {},
              subscribe() {
                return () => undefined;
              },
              close: closeMock,
            });
        }),
    );
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
      openTransport: factory,
    });

    const opening = runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await vi.waitFor(() =>
      expect(runtime.snapshot().resources.pendingOperations).toBe(1),
    );
    const closing = runtime.close();
    finishAcquire();

    await expect(opening).resolves.toMatchObject({ status: "cancelled" });
    await expect(closing).resolves.toMatchObject({
      status: "closed",
      blocker: { code: "transport_failure" },
    });
    expect(factory).toHaveBeenCalledTimes(1);
    expect(closeMock).toHaveBeenCalledTimes(1);
    expect(runtime.snapshot().resources).toMatchObject({
      activeVideoOwners: 1,
      pendingOperations: 0,
    });

    finishClose();
    await vi.waitFor(() =>
      expect(runtime.snapshot().resources.activeVideoOwners).toBe(0),
    );
  });

  it("retains a valid transport when malformed subscription cleanup times out", async () => {
    vi.useFakeTimers();
    const snapshot = runtimeFixtureSnapshot();
    let finishClose!: () => void;
    const close = new Promise<void>((resolve) => {
      finishClose = resolve;
    });
    const factory: MediaTransportFactory = async ({ asset, ownerId, epoch }) =>
      ({
        ownerId,
        assetId: asset.assetId,
        openedEpoch: epoch,
        pendingFrameObservations: 0,
        async seek(request: MediaTransportSeek) {
          return {
            ownerId,
            assetId: asset.assetId,
            epoch: request.epoch,
            sourceFrame: request.sourceFrame,
            sourcePts: request.sourcePts,
          };
        },
        async play() {},
        async pause() {},
        subscribe() {
          return undefined;
        },
        async close() {
          await close;
        },
      }) as unknown as MediaTransport;
    const runtime = createEditorRuntime({
      capability: admittedCapability(),
      resolveScene: (frame, current) => runtimeFixtureScene(frame, current),
      openTransport: factory,
    });

    const opening = runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await vi.advanceTimersByTimeAsync(500);
    await expect(opening).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "transport_failure" },
    });
    expect(runtime.snapshot().resources.activeVideoOwners).toBe(1);

    finishClose();
    await vi.advanceTimersByTimeAsync(0);
    expect(runtime.snapshot().resources.activeVideoOwners).toBe(0);
  });
});
