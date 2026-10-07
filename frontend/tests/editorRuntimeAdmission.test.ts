import { describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  createEditorRuntime,
  createHtmlMediaElementTransportFactory,
  type HtmlMediaElementSourceOwner,
  type MediaTransport,
  type MediaTransportFactory,
  type MediaTransportOpen,
} from "../src/runtime/editorRuntime";
import {
  evaluateMediaCapabilities,
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
} from "../src/runtime/mediaCapabilities";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import {
  runtimeContractAdmission,
  runtimeFixtureScene,
  runtimeFixtureSnapshot,
} from "./fixtures/browserNleRuntimeFixture";

function capability() {
  const proof = runtimeContractAdmission();
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
    proof.qualification,
    proof.authority,
  );
}

function transport(request: MediaTransportOpen) {
  const subscribe = vi.fn(() => () => undefined);
  const subscribeFailure = vi.fn(() => () => undefined);
  const close = vi.fn(async () => undefined);
  const value: MediaTransport = {
    ownerId: request.ownerId,
    assetId: request.asset.assetId,
    openedEpoch: request.epoch,
    pendingFrameObservations: 0,
    seek: async (next) => ({
      ownerId: request.ownerId,
      assetId: request.asset.assetId,
      epoch: next.epoch,
      sourceFrame: next.sourceFrame,
      sourcePts: next.sourcePts,
    }),
    play: async () => undefined,
    pause: async () => undefined,
    subscribe,
    subscribeFailure,
    rebind: async () => true,
    close,
  };
  return { value, subscribe, subscribeFailure, close };
}

describe("runtime admission of independently supplied transports", () => {
  it("retires a clip's old asset before acquiring its newly selected asset", async () => {
    const rebind = vi.fn(async () => true);
    const made: ReturnType<typeof transport>[] = [];
    const openTransport = vi.fn(async (request: MediaTransportOpen) => {
      const owner = transport(request);
      made.push(owner);
      return { ...owner.value, rebind };
    });
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene: (frame, snapshot) => ({
        ...runtimeFixtureScene(frame, snapshot),
        layers: runtimeFixtureScene(frame, snapshot).layers.map((layer) => ({
          ...layer,
          asset_id: snapshot.clips.find(
            (clip) => clip.clipId === layer.clip_id,
          )!.assetId,
        })),
      }),
      openTransport,
    });
    const first = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 1;
    const asset = structuredClone(wire.assets[0]!);
    asset.asset_id += "-replacement";
    wire.assets.push(asset);
    wire.clips[0]!.asset_id = asset.asset_id;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    try {
      await expect(
        runtime.replace(buildPublicAssetManifest(next), next),
      ).resolves.toMatchObject({ status: "applied" });
      expect(rebind).not.toHaveBeenCalled();
      expect(made[0]!.close).toHaveBeenCalledOnce();
      expect(openTransport).toHaveBeenCalledTimes(2);
      expect(openTransport).toHaveBeenLastCalledWith(
        expect.objectContaining({
          asset: expect.objectContaining({ assetId: asset.asset_id }),
        }),
      );
    } finally {
      await runtime.close();
    }
  });
  const flush = async () => {
    for (let step = 0; step < 24; step++) await Promise.resolve();
  };
  function held<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((yes) => {
      resolve = yes;
    });
    return { promise, resolve };
  }

  it("clears an earlier seek's playback intent before a new seek supersedes replacement", async () => {
    const gate = held<void>();
    let hold = false;
    const play = vi.fn(async () => undefined);
    const seek = vi.fn(
      async (request: Parameters<MediaTransport["seek"]>[0]) => {
        if (hold && request.sourceFrame === 12) await gate.promise;
        return {
          ownerId: "clip-main",
          assetId: "vid-primary",
          epoch: request.epoch,
          sourceFrame: request.sourceFrame,
          sourcePts: request.sourcePts,
        };
      },
    );
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene: (frame, snapshot) => runtimeFixtureScene(frame, snapshot),
      openTransport: async (request) => ({
        ...transport(request).value,
        seek,
        play,
      }),
    });
    const snapshot = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    hold = true;
    const old = runtime.seek(12);
    await vi.waitFor(() =>
      expect(seek).toHaveBeenLastCalledWith(
        expect.objectContaining({ sourceFrame: 12 }),
      ),
    );
    const next = runtimeFixtureSnapshot(12);
    const replacing = runtime.replace(buildPublicAssetManifest(next), next);
    await flush();
    const newest = runtime.seek(24);
    gate.resolve();
    try {
      await Promise.all([old, replacing, newest]);
      expect(runtime.snapshot().status).toBe("paused");
      expect(runtime.snapshot().outputFrame).toBe(24);
      expect(play).toHaveBeenCalledOnce();
    } finally {
      gate.resolve();
      await runtime.close();
    }
  });

  it.each(["drain", "preparation"] as const)(
    "settles a cancelled replacement before a successor's held %s boundary",
    async (phase) => {
      const warming = held<MediaTransport>();
      const moving = held<void>();
      const closing = held<void>();
      let warm!: ReturnType<typeof transport>;
      const openTransport = async (request: MediaTransportOpen) => {
        const made = transport(request);
        if (request.ownerId === "clip-video-overlay") {
          warm = made;
          if (phase === "preparation")
            made.close.mockImplementationOnce(async () => {
              await closing.promise;
            });
          return warming.promise;
        }
        const original = made.value.seek;
        return {
          ...made.value,
          seek: async (next: Parameters<MediaTransport["seek"]>[0]) => {
            if (phase === "drain" && next.sourceFrame === 4)
              await moving.promise;
            return original(next);
          },
        };
      };
      const runtime = createEditorRuntime({
        capability: capability(),
        resolveScene: (frame, snapshot) => runtimeFixtureScene(frame, snapshot),
        openTransport,
      });
      const snapshot = runtimeFixtureSnapshot();
      await runtime.open(
        buildPublicAssetManifest(snapshot),
        snapshot,
        RUNTIME_PROFILE_FINGERPRINT,
      );
      await runtime.play();
      const prepared = runtime.prepare!([
        runtimeFixtureScene(12, snapshot, true),
      ]);
      await flush();
      const seeking = phase === "drain" ? runtime.seek(4) : undefined;
      await flush();
      const next = runtimeFixtureSnapshot(12);
      let oldSettled = false;
      const first = runtime
        .replace(buildPublicAssetManifest(next), next)
        .then((receipt) => {
          oldSettled = true;
          return receipt;
        });
      await flush();
      let newerSettled = false;
      const newer = runtime
        .replace(buildPublicAssetManifest(next), next)
        .then((receipt) => {
          newerSettled = true;
          return receipt;
        });
      await flush();
      if (phase === "drain") moving.resolve();
      else warming.resolve(warm.value);
      for (let step = 0; step < 80; step++) await Promise.resolve();
      try {
        expect(oldSettled).toBe(true);
        expect(newerSettled).toBe(false);
      } finally {
        moving.resolve();
        warming.resolve(warm.value);
        closing.resolve();
        await Promise.all([seeking, prepared, first, newer]);
        await runtime.close();
      }
    },
  );

  it("closes cancelled successful reauthorization before a successor acquires", async () => {
    const gate = held<boolean>();
    const rebind = vi.fn(async () => true).mockReturnValueOnce(gate.promise);
    const made: ReturnType<typeof transport>[] = [];
    const openTransport = vi.fn(async (request: MediaTransportOpen) => {
      const result = transport(request);
      made.push(result);
      return { ...result.value, rebind };
    });
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene: (frame, snapshot) => runtimeFixtureScene(frame, snapshot),
      openTransport,
    });
    const first = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    const next = runtimeFixtureSnapshot(12);
    const replacing = runtime.replace(buildPublicAssetManifest(next), next, 12);
    await flush();
    expect(rebind).toHaveBeenCalledOnce();
    const newest = runtime.replace(buildPublicAssetManifest(next), next, 24);
    gate.resolve(true);
    await expect(replacing).resolves.toMatchObject({ status: "cancelled" });
    await expect(newest).resolves.toMatchObject({
      status: "applied",
      outputFrame: 24,
    });
    expect(made[0]!.close).toHaveBeenCalledOnce();
    expect(openTransport).toHaveBeenCalledTimes(2);
    await runtime.close();
  });
  it("blocks a replacement when the superseded rebind does not drain by its own deadline", async () => {
    vi.useFakeTimers();
    const gate = held<boolean>();
    const rebind = vi.fn(async () => true).mockReturnValueOnce(gate.promise);
    const resolveScene = vi.fn(
      (frame: number, snapshot: Parameters<typeof runtimeFixtureScene>[1]) =>
        runtimeFixtureScene(frame, snapshot),
    );
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene,
      openTransport: async (request) => ({
        ...transport(request).value,
        rebind,
      }),
    });
    let replacing: ReturnType<typeof runtime.replace> | undefined;
    let newest: ReturnType<typeof runtime.replace> | undefined;
    try {
      const first = runtimeFixtureSnapshot();
      await runtime.open(
        buildPublicAssetManifest(first),
        first,
        RUNTIME_PROFILE_FINGERPRINT,
      );
      const next = runtimeFixtureSnapshot(12);
      replacing = runtime.replace(buildPublicAssetManifest(next), next, 12);
      await flush();
      expect(rebind).toHaveBeenCalledOnce();
      resolveScene.mockClear();
      newest = runtime.replace(buildPublicAssetManifest(next), next, 24);
      await flush();
      await vi.advanceTimersByTimeAsync(1_000);
      await expect(newest).resolves.toMatchObject({
        status: "blocked",
        blocker: { code: "transport_failure" },
      });
      expect(resolveScene).not.toHaveBeenCalled();
    } finally {
      gate.resolve(true);
      await Promise.all([replacing, newest]);
      await runtime.close();
      vi.useRealTimers();
    }
  });
  it("refuses a throwing initial-frame callback before handing any frame to the scene resolver", async () => {
    const resolveScene = vi.fn(
      (frame: number, snapshot: Parameters<typeof runtimeFixtureScene>[1]) =>
        runtimeFixtureScene(frame, snapshot),
    );
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene,
      openTransport: async (request) => transport(request).value,
    });
    const first = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    resolveScene.mockClear();
    const next = runtimeFixtureSnapshot(12);
    await expect(
      runtime.replace(buildPublicAssetManifest(next), next, () => {
        throw new Error("controlled target refusal");
      }),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "contract_mismatch" },
    });
    expect(resolveScene).not.toHaveBeenCalled();
    await runtime.close();
  });
  it("joins a held warm acquisition before reauthorizing the displayed owner", async () => {
    const warming = held<MediaTransport>();
    let warm!: ReturnType<typeof transport>;
    const rebind = vi.fn(async () => true);
    const openTransport = vi.fn(async (request: MediaTransportOpen) => {
      const result = transport(request);
      if (request.ownerId === "clip-video-overlay") {
        warm = result;
        return warming.promise;
      }
      return { ...result.value, rebind };
    });
    const resolveScene = vi.fn(
      (frame: number, snapshot: Parameters<typeof runtimeFixtureScene>[1]) =>
        runtimeFixtureScene(frame, snapshot),
    );
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene,
      openTransport,
    });
    const first = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    const preparing = runtime.prepare!([runtimeFixtureScene(12, first, true)]);
    await flush();
    expect(warm).toBeDefined();
    resolveScene.mockClear();
    const next = runtimeFixtureSnapshot(12);
    let settled = false;
    const replacing = runtime
      .replace(buildPublicAssetManifest(next), next, 0)
      .then((receipt) => {
        settled = true;
        return receipt;
      });
    await flush();
    expect(settled).toBe(false);
    expect(rebind).not.toHaveBeenCalled();
    expect(resolveScene).not.toHaveBeenCalled();
    warming.resolve(warm.value);
    await preparing;
    await expect(replacing).resolves.toMatchObject({ status: "applied" });
    expect(rebind).toHaveBeenCalledOnce();
    expect(warm.close).toHaveBeenCalledOnce();
    await runtime.close();
  });
  it("aborts the timed-out warm acquisition before publishing the replacement blocker", async () => {
    vi.useFakeTimers();
    let warmSignal!: AbortSignal;
    const warming = held<MediaTransport>();
    const openTransport = vi.fn(async (request: MediaTransportOpen) => {
      if (request.ownerId === "clip-video-overlay") {
        warmSignal = request.signal;
        request.signal.addEventListener(
          "abort",
          () => warming.resolve(transport(request).value),
          { once: true },
        );
        return warming.promise;
      }
      return transport(request).value;
    });
    const resolveScene = vi.fn(
      (frame: number, snapshot: Parameters<typeof runtimeFixtureScene>[1]) =>
        runtimeFixtureScene(frame, snapshot),
    );
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene,
      openTransport,
    });
    const abortAtPublication: boolean[] = [];
    runtime.subscribe(() => {
      const value = runtime.snapshot();
      if (value.status === "blocked")
        abortAtPublication.push(warmSignal?.aborted ?? false);
    });
    try {
      const first = runtimeFixtureSnapshot();
      await runtime.open(
        buildPublicAssetManifest(first),
        first,
        RUNTIME_PROFILE_FINGERPRINT,
      );
      await runtime.play();
      const preparing = runtime.prepare!([
        runtimeFixtureScene(12, first, true),
      ]);
      await flush();
      resolveScene.mockClear();
      const next = runtimeFixtureSnapshot(12);
      const replacing = runtime.replace(
        buildPublicAssetManifest(next),
        next,
        0,
      );
      await flush();
      await vi.advanceTimersByTimeAsync(5_000);
      await expect(replacing).resolves.toMatchObject({
        status: "blocked",
        blocker: { code: "transport_failure" },
      });
      expect(abortAtPublication.length).toBeGreaterThan(0);
      expect(abortAtPublication[0]).toBe(true);
      expect(resolveScene).not.toHaveBeenCalled();
      await preparing;
      await runtime.close();
    } finally {
      warming.resolve({} as MediaTransport);
      await vi.runOnlyPendingTimersAsync();
      vi.useRealTimers();
    }
  });
  it("refuses replacement when the warm owner's unsubscribe fails even though its close succeeds", async () => {
    const rebind = vi.fn(async () => true);
    const resolveScene = vi.fn(
      (frame: number, snapshot: Parameters<typeof runtimeFixtureScene>[1]) =>
        runtimeFixtureScene(frame, snapshot),
    );
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene,
      openTransport: async (request) => ({
        ...transport(request).value,
        rebind,
        subscribeFailure:
          request.ownerId === "clip-video-overlay"
            ? () => () => {
                throw new Error("controlled warm unsubscribe refusal");
              }
            : () => () => undefined,
      }),
    });
    const first = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    await runtime.prepare!([runtimeFixtureScene(12, first, true)]);
    resolveScene.mockClear();
    const next = runtimeFixtureSnapshot(12);
    await expect(
      runtime.replace(buildPublicAssetManifest(next), next, 0),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "transport_failure" },
    });
    expect(resolveScene).not.toHaveBeenCalled();
    expect(rebind).not.toHaveBeenCalled();
    await runtime.close();
  });
  it("keeps a failed closing owner as an admission blocker on the next replacement", async () => {
    let refuseClose = true;
    const resolveScene = vi.fn(
      (frame: number, snapshot: Parameters<typeof runtimeFixtureScene>[1]) =>
        runtimeFixtureScene(frame, snapshot),
    );
    const openTransport = vi.fn(async (request: MediaTransportOpen) => ({
      ...transport(request).value,
      rebind: async () => false,
      close: async () => {
        if (refuseClose) throw new Error("controlled closing refusal");
      },
    }));
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene,
      openTransport,
    });
    const first = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    const next = runtimeFixtureSnapshot(12);
    await runtime.replace(buildPublicAssetManifest(next), next, 0);
    resolveScene.mockClear();
    try {
      await expect(
        runtime.replace(buildPublicAssetManifest(next), next, 0),
      ).resolves.toMatchObject({
        status: "blocked",
        blocker: { code: "transport_failure" },
      });
      expect(resolveScene).not.toHaveBeenCalled();
      expect(openTransport).toHaveBeenCalledOnce();
    } finally {
      refuseClose = false;
      await runtime.close();
    }
  });
  it("does not retire an owner after a replacement publication is superseded by a seek", async () => {
    const made: ReturnType<typeof transport>[] = [];
    const openTransport = vi.fn(async (request: MediaTransportOpen) => {
      const result = transport(request);
      made.push(result);
      return { ...result.value, rebind: undefined };
    });
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene: (frame, snapshot) => runtimeFixtureScene(frame, snapshot),
      openTransport,
    });
    const first = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    const next = runtimeFixtureSnapshot(12);
    let newest: ReturnType<typeof runtime.seek> | undefined;
    const unsubscribe = runtime.subscribe(() => {
      const value = runtime.snapshot();
      if (
        newest === undefined &&
        value.status === "seeking" &&
        value.publicFingerprint === next.publicFingerprint
      ) {
        newest = Promise.resolve(
          {} as Awaited<ReturnType<typeof runtime.seek>>,
        );
        newest = runtime.seek(24);
      }
    });
    await expect(
      runtime.replace(buildPublicAssetManifest(next), next, 0),
    ).resolves.toMatchObject({ status: "cancelled" });
    await expect(newest).resolves.toMatchObject({
      status: "applied",
      outputFrame: 24,
    });
    expect(openTransport).toHaveBeenCalledOnce();
    expect(made[0]!.close).not.toHaveBeenCalled();
    unsubscribe();
    await runtime.close();
  });
  it("does not consult a retired initial-frame callback after a replacement with no owners is superseded", async () => {
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene: (frame, snapshot) => ({
        ...runtimeFixtureScene(frame, snapshot),
        layers: [],
      }),
      openTransport: async (request) => transport(request).value,
    });
    const first = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    const next = runtimeFixtureSnapshot(12);
    const initialFrame = vi.fn(() => 0);
    let newest: ReturnType<typeof runtime.seek> | undefined;
    const unsubscribe = runtime.subscribe(() => {
      const value = runtime.snapshot();
      if (
        newest === undefined &&
        value.status === "seeking" &&
        value.publicFingerprint === next.publicFingerprint
      ) {
        newest = Promise.resolve(
          {} as Awaited<ReturnType<typeof runtime.seek>>,
        );
        newest = runtime.seek(24);
      }
    });
    await expect(
      runtime.replace(buildPublicAssetManifest(next), next, initialFrame),
    ).resolves.toMatchObject({ status: "cancelled" });
    await expect(newest).resolves.toMatchObject({
      status: "applied",
      outputFrame: 24,
    });
    expect(initialFrame).not.toHaveBeenCalled();
    unsubscribe();
    await runtime.close();
  });
  it.each(["incompatible", "refused"] as const)(
    "reports %s owner cleanup failure before resolving another scene",
    async (condition) => {
      let refuseClose = true;
      const resolveScene = vi.fn(
        (frame: number, snapshot: Parameters<typeof runtimeFixtureScene>[1]) =>
          runtimeFixtureScene(frame, snapshot),
      );
      const openTransport = vi.fn(async (request: MediaTransportOpen) => ({
        ...transport(request).value,
        rebind: condition === "incompatible" ? undefined : async () => false,
        close: async () => {
          if (refuseClose) throw new Error("controlled owner cleanup refusal");
        },
      }));
      const runtime = createEditorRuntime({
        capability: capability(),
        resolveScene,
        openTransport,
      });
      const snapshot = runtimeFixtureSnapshot();
      await expect(
        runtime.open(
          buildPublicAssetManifest(snapshot),
          snapshot,
          RUNTIME_PROFILE_FINGERPRINT,
        ),
      ).resolves.toMatchObject({ status: "applied" });
      resolveScene.mockClear();
      const next = runtimeFixtureSnapshot(12);
      try {
        await expect(
          runtime.replace(buildPublicAssetManifest(next), next, 12),
        ).resolves.toMatchObject({
          status: "blocked",
          blocker: { code: "transport_failure" },
        });
        expect(resolveScene).not.toHaveBeenCalled();
        expect(openTransport).toHaveBeenCalledOnce();
      } finally {
        refuseClose = false;
        await runtime.close();
      }
      expect(runtime.snapshot().resources.activeVideoOwners).toBe(0);
    },
  );
  it("does not seek or publish a replacement before successful reauthorization settles", async () => {
    let finish!: (value: boolean) => void;
    const seek = vi.fn(
      async (request: Parameters<MediaTransport["seek"]>[0]) => ({
        ownerId: "clip-main",
        assetId: "vid-primary",
        epoch: request.epoch,
        sourceFrame: request.sourceFrame,
        sourcePts: request.sourcePts,
      }),
    );
    const rebind = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          finish = resolve;
        }),
    );
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene: (frame, snapshot) => runtimeFixtureScene(frame, snapshot),
      openTransport: async (request) => ({
        ...transport(request).value,
        seek,
        rebind,
      }),
    });
    const snapshot = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    seek.mockClear();
    const next = runtimeFixtureSnapshot(12);
    let settled = false;
    const replacing = runtime
      .replace(buildPublicAssetManifest(next), next, 12)
      .then((receipt) => {
        settled = true;
        return receipt;
      });
    await vi.waitFor(() => expect(rebind).toHaveBeenCalledOnce());
    for (let step = 0; step < 12; step++) await Promise.resolve();
    expect(settled).toBe(false);
    expect(seek).not.toHaveBeenCalled();
    expect(runtime.snapshot().status).toBe("seeking");
    finish(true);
    await expect(replacing).resolves.toMatchObject({
      status: "applied",
      outputFrame: 12,
    });
    expect(seek).toHaveBeenCalledOnce();
    await runtime.close();
  });
  it.each([
    ["ownerId", 3],
    ["assetId", 3],
    ["openedEpoch", 1.5],
    ["pendingFrameObservations", 0.5],
    ["pendingFrameObservations", -1],
    ["pendingFrameObservations", 2],
    ["seek", undefined],
    ["play", undefined],
    ["pause", undefined],
    ["subscribe", undefined],
    ["subscribeFailure", 3],
    ["rebind", 3],
    ["close", undefined],
  ])(
    "rejects invalid %s=%s before registering the owner",
    async (field, invalid) => {
      const made: ReturnType<typeof transport>[] = [];
      const factory: MediaTransportFactory = async (request) => {
        const result = transport(request);
        made.push(result);
        return {
          ...result.value,
          [field]: invalid,
        } as unknown as MediaTransport;
      };
      const runtime = createEditorRuntime({
        capability: capability(),
        resolveScene: (frame, snapshot) => runtimeFixtureScene(frame, snapshot),
        openTransport: factory,
      });
      const snapshot = runtimeFixtureSnapshot();
      const receipt = await runtime.open(
        buildPublicAssetManifest(snapshot),
        snapshot,
        RUNTIME_PROFILE_FINGERPRINT,
      );
      expect(receipt).toMatchObject({
        status: "blocked",
        blocker: { code: "contract_mismatch" },
      });
      expect(made).toHaveLength(1);
      expect(made[0]!.subscribe).not.toHaveBeenCalled();
      expect(made[0]!.subscribeFailure).not.toHaveBeenCalled();
      await runtime.close();
    },
  );

  it.each([undefined, () => true])(
    "accepts optional rebind %s and keeps the prior paused frame when no new frame is requested",
    async (rebind) => {
      const factory: MediaTransportFactory = async (request) => ({
        ...transport(request).value,
        rebind: rebind === undefined ? undefined : async () => rebind(),
      });
      const runtime = createEditorRuntime({
        capability: capability(),
        resolveScene: (frame, snapshot) => runtimeFixtureScene(frame, snapshot),
        openTransport: factory,
      });
      const snapshot = runtimeFixtureSnapshot();
      await runtime.open(
        buildPublicAssetManifest(snapshot),
        snapshot,
        RUNTIME_PROFILE_FINGERPRINT,
      );
      await runtime.seek(12);
      const next = runtimeFixtureSnapshot(12);
      await expect(
        runtime.replace(buildPublicAssetManifest(next), next),
      ).resolves.toMatchObject({ status: "applied", outputFrame: 12 });
      await runtime.close();
    },
  );

  it.each([
    NaN,
    -1,
    1.5,
    () => {
      throw new Error("controlled frame failure");
    },
  ])("refuses the independently requested frame %s", async (frame) => {
    const resolveScene = vi.fn(
      (current: number, snapshot: Parameters<typeof runtimeFixtureScene>[1]) =>
        runtimeFixtureScene(current, snapshot),
    );
    const runtime = createEditorRuntime({
      capability: capability(),
      resolveScene,
      openTransport: async (request) => transport(request).value,
    });
    const snapshot = runtimeFixtureSnapshot();
    await runtime.open(
      buildPublicAssetManifest(snapshot),
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    resolveScene.mockClear();
    const next = runtimeFixtureSnapshot(12);
    await expect(
      runtime.replace(buildPublicAssetManifest(next), next, frame),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "contract_mismatch" },
    });
    expect(resolveScene).not.toHaveBeenCalled();
    await runtime.close();
  });
});

function native() {
  const element = {
    play: vi.fn(async () => undefined),
    pause: vi.fn(),
    load: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    removeAttribute: vi.fn(),
    currentTime: 0,
    paused: true,
    seeking: false,
  } as unknown as HTMLVideoElement;
  const release = vi.fn(async () => undefined);
  return { element, release };
}

describe("native source owner admission through a permissive source factory", () => {
  it("refuses a closed transport before asking its permissive source owner to rebind", async () => {
    const controlled = native();
    const sourceRebind = vi.fn(async () => true);
    const factory = createHtmlMediaElementTransportFactory(
      async () => ({ ...controlled, rebind: sourceRebind }),
      { frameObserver: "event_fallback" },
    );
    const snapshot = runtimeFixtureSnapshot();
    const request: MediaTransportOpen = {
      asset: buildPublicAssetManifest(snapshot).assets[0]!,
      ownerId: "clip-main",
      epoch: 1,
      signal: new AbortController().signal,
    };
    const owner = await factory(request);
    await owner.close();
    await expect(owner.rebind!({ ...request, epoch: 2 })).resolves.toBe(false);
    expect(sourceRebind).not.toHaveBeenCalled();
    expect(controlled.release).toHaveBeenCalledOnce();
  });
  it("reports a native pause refusal before delegating retained authority", async () => {
    const controlled = native();
    const sourceRebind = vi.fn(async () => true);
    const factory = createHtmlMediaElementTransportFactory(
      async () => ({ ...controlled, rebind: sourceRebind }),
      { frameObserver: "event_fallback" },
    );
    const snapshot = runtimeFixtureSnapshot();
    const request: MediaTransportOpen = {
      asset: buildPublicAssetManifest(snapshot).assets[0]!,
      ownerId: "clip-main",
      epoch: 1,
      signal: new AbortController().signal,
    };
    const owner = await factory(request);
    const actualPause = owner.pause.bind(owner);
    const observedErrors: unknown[] = [];
    // Observe the same public promise without changing its value or rejection for its caller.
    // This also keeps a broken detached call from making an unhandled error the mutation witness.
    vi.spyOn(owner, "pause").mockImplementation(() => {
      const result = actualPause();
      void result.catch((error: unknown) => {
        observedErrors.push(error);
      });
      return result;
    });
    vi.mocked(controlled.element.pause).mockImplementationOnce(() => {
      throw new Error("controlled native pause refusal");
    });
    try {
      await expect(
        owner.rebind!({ ...request, epoch: 2 }),
      ).rejects.toMatchObject({ code: "transport_failure" });
      expect(sourceRebind).not.toHaveBeenCalled();
      expect(observedErrors).toHaveLength(1);
    } finally {
      await owner.close();
    }
  });
  it("pauses the native transport before delegating retained authority", async () => {
    const controlled = native();
    const events: string[] = [];
    vi.mocked(controlled.element.pause).mockImplementation(() => {
      events.push("pause");
    });
    const sourceRebind = vi.fn(async () => {
      events.push("authority");
      return true;
    });
    const factory = createHtmlMediaElementTransportFactory(
      async () => ({ ...controlled, rebind: sourceRebind }),
      { frameObserver: "event_fallback" },
    );
    const snapshot = runtimeFixtureSnapshot();
    const request: MediaTransportOpen = {
      asset: buildPublicAssetManifest(snapshot).assets[0]!,
      ownerId: "clip-main",
      epoch: 1,
      signal: new AbortController().signal,
    };
    const owner = await factory(request);
    events.length = 0;
    try {
      await expect(owner.rebind!({ ...request, epoch: 2 })).resolves.toBe(true);
      expect(events).toEqual(["pause", "authority"]);
    } finally {
      await owner.close();
    }
  });
  const invalidFields = [
    "element",
    "play",
    "pause",
    "addEventListener",
    "removeEventListener",
    "removeAttribute",
    "load",
    "rebind",
    "release",
  ];
  it.each(invalidFields)(
    "refuses invalid %s before native listeners are installed",
    async (field) => {
      const controlled = native();
      const owner = { ...controlled } as unknown as Record<string, unknown>;
      if (field === "element") owner.element = undefined;
      else if (field === "rebind" || field === "release") owner[field] = 3;
      else (owner.element as unknown as Record<string, unknown>)[field] = 3;
      const factory = createHtmlMediaElementTransportFactory(
        async () => owner as unknown as HtmlMediaElementSourceOwner,
        { frameObserver: "event_fallback" },
      );
      const snapshot = runtimeFixtureSnapshot();
      const request: MediaTransportOpen = {
        asset: buildPublicAssetManifest(snapshot).assets[0]!,
        ownerId: "clip-main",
        epoch: 1,
        signal: new AbortController().signal,
      };
      await expect(factory(request)).rejects.toMatchObject({
        code: "contract_mismatch",
      });
      if (field !== "element" && field !== "addEventListener")
        expect(controlled.element.addEventListener).not.toHaveBeenCalled();
      if (field !== "release")
        expect(controlled.release).toHaveBeenCalledOnce();
    },
  );

  it.each([false, 1, undefined])(
    "requires a true rebind result, not %s",
    async (result) => {
      const controlled = native();
      const factory = createHtmlMediaElementTransportFactory(
        async () => ({ ...controlled, rebind: async () => result as boolean }),
        { frameObserver: "event_fallback" },
      );
      const snapshot = runtimeFixtureSnapshot();
      const request: MediaTransportOpen = {
        asset: buildPublicAssetManifest(snapshot).assets[0]!,
        ownerId: "clip-main",
        epoch: 1,
        signal: new AbortController().signal,
      };
      const owner = await factory(request);
      await expect(owner.rebind!({ ...request, epoch: 2 })).resolves.toBe(
        false,
      );
      await owner.close();
    },
  );

  it("fences abort and close independently after a held source rebind resolves", async () => {
    for (const action of ["abort", "close"]) {
      const controlled = native();
      let finish!: (value: boolean) => void;
      const sourceRebind = vi.fn(
        () =>
          new Promise<boolean>((resolve) => {
            finish = resolve;
          }),
      );
      const factory = createHtmlMediaElementTransportFactory(
        async () => ({ ...controlled, rebind: sourceRebind }),
        { frameObserver: "event_fallback" },
      );
      const snapshot = runtimeFixtureSnapshot();
      const request: MediaTransportOpen = {
        asset: buildPublicAssetManifest(snapshot).assets[0]!,
        ownerId: "clip-main",
        epoch: 1,
        signal: new AbortController().signal,
      };
      const owner = await factory(request);
      const signal = new AbortController();
      const replacing = owner.rebind!({
        ...request,
        epoch: 2,
        signal: signal.signal,
      });
      for (let step = 0; step < 8; step++) await Promise.resolve();
      expect(sourceRebind).toHaveBeenCalledOnce();
      if (action === "abort") signal.abort();
      else await owner.close();
      finish(true);
      await expect(replacing).resolves.toBe(false);
      await owner.close();
    }
  });
});
