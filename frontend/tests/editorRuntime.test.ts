import { describe, expect, it, vi } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  RESOLVED_SCENE_SCHEMA,
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
  type PublicCompositionSnapshot,
} from "../src/contracts/compositionCodec";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  evaluateMediaCapabilities,
  runtimeQualificationFingerprint,
  type RuntimeCapabilityObservation,
  type RuntimeQualification,
  type RuntimeQualificationAuthority,
  type RuntimeQualificationPayload,
} from "../src/runtime/mediaCapabilities";
import {
  buildPublicAssetManifest,
  validatePublicAssetManifest,
  type PublicAssetManifest,
} from "../src/runtime/publicAssetManifest";
import {
  createHtmlMediaElementTransportFactory,
  createEditorRuntime,
  type MediaPresentation,
  type MediaTransport,
  type MediaTransportFactory,
  type MediaTransportSeek,
} from "../src/runtime/editorRuntime";
import { runtimeContractAdmission } from "./fixtures/browserNleRuntimeFixture";
import { resolveCompositionScene } from "../src/runtime/sceneResolver";
import { SMOKE_SHAPE, snapshotFixture } from "./support/nleWorkspaceFixture";

function snapshot(): PublicCompositionSnapshot {
  return decodePublicCompositionSnapshot(
    structuredClone(fixture.snapshot) as Record<string, unknown>,
  );
}

function replacementSnapshot(): PublicCompositionSnapshot {
  const wire = structuredClone(fixture.snapshot) as Record<string, unknown>;
  wire.timeline_revision = 12;
  wire.timeline_fingerprint = `sha256:${"3".repeat(64)}`;
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return decodePublicCompositionSnapshot(wire);
}

function adjacentCutSnapshot(): PublicCompositionSnapshot {
  const wire = structuredClone(fixture.snapshot) as Record<string, any>;
  const first = wire.clips[0];
  const second = structuredClone(wire.clips[1]);
  first.duration_frames = 24;
  second.clip_id = "clip-second";
  second.track_id = "track-primary";
  second.start_frame = 24;
  second.duration_frames = 24;
  second.transition = { kind: "none", duration_frames: 0 };
  second.effect = {
    kind: "none",
    brightness_permille: 0,
    contrast_permille: 1000,
    saturation_permille: 1000,
  };
  wire.clips = [first, second];
  wire.assets = wire.assets.filter((asset: Record<string, unknown>) =>
    ["vid-primary", "vid-overlay"].includes(String(asset.asset_id)),
  );
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return decodePublicCompositionSnapshot(wire);
}

function observation(
  overrides: Partial<RuntimeCapabilityObservation> = {},
): RuntimeCapabilityObservation {
  return {
    schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    htmlMediaElement: true,
    canPlayMp4H264Aac: "probably",
    requestVideoFrameCallback: true,
    seekedEvent: true,
    timeupdateEvent: true,
    canvas2d: true,
    crossOriginIsolated: false,
    ...overrides,
  };
}

function qualification(
  overrides: Partial<RuntimeQualificationPayload> = {},
): RuntimeQualification {
  return runtimeContractAdmission(overrides).qualification;
}

function qualifiedCapability() {
  const admission = runtimeContractAdmission();
  return evaluateMediaCapabilities(
    observation(),
    admission.qualification,
    admission.authority,
  );
}

function baseOperations(): string[] {
  return [
    "SelectSourceRangeV1",
    "CropV1",
    "Transform2DV1",
    "OpacityV1",
    "BlendV1",
  ];
}

function primaryLayer(frame: number): Record<string, unknown> {
  const clip = structuredClone(fixture.snapshot.clips[0]);
  return {
    clip_id: clip.clip_id,
    asset_id: clip.asset_id,
    track_id: clip.track_id,
    source_frame: frame,
    source_pts: frame * 512,
    transition_elapsed_frames: null,
    operation_ids: baseOperations(),
    transform: clip.transform,
    crop: clip.crop,
    opacity_bp: clip.opacity_bp,
    blend: clip.blend,
    text: clip.text,
    effect: clip.effect,
  };
}

function overlayLayer(frame: number): Record<string, unknown> {
  const clip = structuredClone(fixture.snapshot.clips[1]);
  const sourceFrame = frame - 12;
  return {
    clip_id: clip.clip_id,
    asset_id: clip.asset_id,
    track_id: clip.track_id,
    source_frame: sourceFrame,
    source_pts: sourceFrame * 512,
    transition_elapsed_frames: sourceFrame,
    operation_ids: [
      "SelectSourceRangeV1",
      "CropV1",
      "Transform2DV1",
      "ColorAdjustV1",
      "OpacityV1",
      "BlendV1",
      "CrossDissolveV1",
    ],
    transform: clip.transform,
    crop: clip.crop,
    opacity_bp: clip.opacity_bp,
    blend: clip.blend,
    text: clip.text,
    effect: clip.effect,
  };
}

function sceneWire(
  frame: number,
  value: PublicCompositionSnapshot,
): Record<string, unknown> {
  return {
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: value.profileId,
    public_fingerprint: value.publicFingerprint,
    frame,
    layers:
      frame === 12
        ? [primaryLayer(frame), overlayLayer(frame)]
        : [primaryLayer(frame)],
    audio_span: null,
    blockers: [],
  };
}

function adjacentCutSceneWire(
  frame: number,
  value: PublicCompositionSnapshot,
): Record<string, unknown> {
  const first = primaryLayer(frame);
  const secondLocal = frame - 24;
  return {
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: value.profileId,
    public_fingerprint: value.publicFingerprint,
    frame,
    layers:
      frame < 24
        ? [first]
        : [
            {
              ...first,
              clip_id: "clip-second",
              asset_id: "vid-overlay",
              source_frame: secondLocal,
              source_pts: secondLocal * 512,
            },
          ],
    audio_span: null,
    blockers: [],
  };
}

type FakeTransport = MediaTransport & {
  closeMock: ReturnType<typeof vi.fn>;
  pauseMock: ReturnType<typeof vi.fn>;
  playMock: ReturnType<typeof vi.fn>;
  seekMock: ReturnType<typeof vi.fn>;
  emit(value: MediaPresentation): void;
};

function transportHarness() {
  const transports: FakeTransport[] = [];
  let live = 0;
  let maximumLive = 0;
  const factory: MediaTransportFactory = vi.fn(
    async ({ asset, ownerId, epoch }): Promise<FakeTransport> => {
      live += 1;
      maximumLive = Math.max(maximumLive, live);
      const listeners: Array<(value: MediaPresentation) => void> = [];
      const closeMock = vi.fn(async () => {
        live -= 1;
      });
      const pauseMock = vi.fn(async () => undefined);
      const playMock = vi.fn(async () => undefined);
      const seekMock = vi.fn(async (request: MediaTransportSeek) => ({
        ownerId,
        assetId: asset.assetId,
        epoch: request.epoch,
        sourceFrame: request.sourceFrame,
        sourcePts: request.sourcePts,
      }));
      const transport: FakeTransport = {
        ownerId,
        assetId: asset.assetId,
        openedEpoch: epoch,
        pendingFrameObservations: 0,
        close: closeMock,
        pause: pauseMock,
        play: playMock,
        seek: seekMock,
        subscribe(listener) {
          listeners.push(listener);
          return () => {
            const index = listeners.indexOf(listener);
            if (index >= 0) listeners.splice(index, 1);
          };
        },
        closeMock,
        pauseMock,
        playMock,
        seekMock,
        emit(value) {
          for (const listener of [...listeners]) listener(value);
        },
      };
      transports.push(transport);
      return transport;
    },
  );
  return {
    factory,
    transports,
    live: () => live,
    maximumLive: () => maximumLive,
  };
}

function nativeVideoHarness(useRvfc: boolean, autoPresent = true) {
  const eventListeners = new Map<string, Set<(event: Event) => void>>();
  const frameCallbacks = new Map<
    number,
    (now: number, metadata: VideoFrameCallbackMetadata) => void
  >();
  let sequence = 0;
  let currentTime = 0;
  const play = vi.fn(async () => undefined);
  const pause = vi.fn();
  const removeAttribute = vi.fn();
  const load = vi.fn();
  const cancelVideoFrameCallback = vi.fn((id: number) => {
    frameCallbacks.delete(id);
  });
  const presentPending = () => {
    if (useRvfc) {
      for (const [id, callback] of [...frameCallbacks]) {
        frameCallbacks.delete(id);
        callback(0, {
          mediaTime: currentTime,
        } as VideoFrameCallbackMetadata);
      }
    } else {
      for (const listener of eventListeners.get("seeked") ?? [])
        listener(new Event("seeked"));
    }
  };
  const video = {
    get currentTime() {
      return currentTime;
    },
    set currentTime(value: number) {
      currentTime = value;
      if (autoPresent) queueMicrotask(presentPending);
    },
    play,
    pause,
    removeAttribute,
    load,
    addEventListener(
      type: string,
      listener: EventListenerOrEventListenerObject,
    ) {
      const callback = listener as (event: Event) => void;
      const listeners = eventListeners.get(type) ?? new Set();
      listeners.add(callback);
      eventListeners.set(type, listeners);
    },
    removeEventListener(
      type: string,
      listener: EventListenerOrEventListenerObject,
    ) {
      eventListeners.get(type)?.delete(listener as (event: Event) => void);
    },
    requestVideoFrameCallback: useRvfc
      ? (
          callback: (now: number, metadata: VideoFrameCallbackMetadata) => void,
        ) => {
          const id = ++sequence;
          frameCallbacks.set(id, callback);
          return id;
        }
      : undefined,
    cancelVideoFrameCallback,
  } as unknown as HTMLVideoElement;
  return {
    video,
    play,
    pause,
    removeAttribute,
    load,
    cancelVideoFrameCallback,
    presentPending,
  };
}

function privateKeys(value: unknown): string[] {
  if (Array.isArray(value)) return value.flatMap(privateKeys);
  if (value === null || typeof value !== "object") return [];
  const record = value as Record<string, unknown>;
  return Object.entries(record).flatMap(([key, child]) => [
    ...(new Set([
      "path",
      "url",
      "blob",
      "lease",
      "credential",
      "runtimeIdentity",
    ]).has(key)
      ? [key]
      : []),
    ...privateKeys(child),
  ]);
}

describe("M25-12 media capability profile", () => {
  it("pins the exact accepted profile and requires encoded-corpus qualification", () => {
    expect(RUNTIME_PROFILE_FINGERPRINT).toBe(
      "sha256:f0a226902d89e48113905b43c09fd99c63b2307f8d39ae740330431e091861ff",
    );
    expect(evaluateMediaCapabilities(observation())).toMatchObject({
      status: "qualification_required",
      fallback: "selected_source_only",
      frameObserver: "request_video_frame_callback",
    });
    expect(qualifiedCapability()).toMatchObject({
      status: "available",
      fallback: "selected_source_only",
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    });
  });

  it("does not let a structurally valid caller qualification supply its own authority", () => {
    expect(
      evaluateMediaCapabilities(observation(), qualification()),
    ).toMatchObject({
      status: "qualification_required",
      fallback: "selected_source_only",
    });
  });

  it("binds the validated payload digest to trusted authority and every pending-operation bound", () => {
    const admission = runtimeContractAdmission();
    const { receiptFingerprint, ...payload } = admission.qualification;
    const reorderedPayload = Object.fromEntries(
      Object.entries({
        ...payload,
        browser: Object.fromEntries(Object.entries(payload.browser).reverse()),
      }).reverse(),
    ) as RuntimeQualificationPayload;
    expect(runtimeQualificationFingerprint(reorderedPayload)).toBe(
      receiptFingerprint,
    );
    expect(
      evaluateMediaCapabilities(
        observation(),
        {
          ...admission.qualification,
          maximumCancelMs: admission.qualification.maximumCancelMs + 1,
        },
        admission.authority,
      ),
    ).toMatchObject({
      status: "capability_mismatch",
      fallback: "selected_source_only",
    });

    const {
      maximumPendingOperations: _maximumPendingOperations,
      ...withoutPendingOperations
    } = admission.qualification;
    const pendingOperationCases: ReadonlyArray<
      readonly [RuntimeQualification, RuntimeQualificationAuthority]
    > = [
      [withoutPendingOperations as RuntimeQualification, admission.authority],
      [
        { ...admission.qualification, maximumPendingOperations: 1.5 },
        admission.authority,
      ],
      (() => {
        const overLimit = runtimeContractAdmission({
          maximumPendingOperations: 3,
        });
        return [overLimit.qualification, overLimit.authority];
      })(),
    ];
    for (const [candidate, authority] of pendingOperationCases)
      expect(
        evaluateMediaCapabilities(observation(), candidate, authority),
      ).toMatchObject({
        status: "capability_mismatch",
        fallback: "selected_source_only",
      });

    const sparseCorpus = Array(
      admission.qualification.corpusFingerprints.length,
    ) as string[];
    expect(
      evaluateMediaCapabilities(
        observation(),
        { ...admission.qualification, corpusFingerprints: sparseCorpus },
        admission.authority,
      ),
    ).toMatchObject({
      status: "capability_mismatch",
      fallback: "selected_source_only",
    });

    expect(
      evaluateMediaCapabilities(observation(), admission.qualification, {
        ...admission.authority,
        expectedQualificationFingerprint: `sha256:${"f".repeat(64)}`,
      }),
    ).toMatchObject({
      status: "capability_mismatch",
      fallback: "selected_source_only",
    });
    expect(
      evaluateMediaCapabilities(
        observation(),
        {
          ...admission.qualification,
          receiptFingerprint: `sha256:${"e".repeat(64)}`,
        },
        admission.authority,
      ),
    ).toMatchObject({
      status: "capability_mismatch",
      fallback: "selected_source_only",
    });
    expect(
      evaluateMediaCapabilities(observation(), admission.qualification, {
        ...admission.authority,
        expectedProfileFingerprint: `sha256:${"f".repeat(64)}`,
      }),
    ).toMatchObject({
      status: "profile_unavailable",
      fallback: "unavailable",
    });
    for (const invalidAuthority of [
      null,
      {
        expectedProfileFingerprint:
          admission.authority.expectedProfileFingerprint,
      },
      { ...admission.authority, unexpected: true },
    ])
      expect(
        evaluateMediaCapabilities(
          observation(),
          admission.qualification,
          invalidAuthority as unknown as RuntimeQualificationAuthority,
        ),
      ).toMatchObject({
        status: "capability_mismatch",
        fallback: "selected_source_only",
      });
  });

  it("fails closed on drift and distinguishes unavailable from selected-source fallback", () => {
    expect(
      evaluateMediaCapabilities(
        observation({ profileFingerprint: `sha256:${"f".repeat(64)}` }),
        qualification(),
      ),
    ).toMatchObject({ status: "profile_unavailable", fallback: "unavailable" });
    expect(
      evaluateMediaCapabilities(observation({ htmlMediaElement: false })),
    ).toMatchObject({ status: "capability_mismatch", fallback: "unavailable" });
    expect(
      evaluateMediaCapabilities(
        observation({ canvas2d: false }),
        qualification(),
      ),
    ).toMatchObject({
      status: "capability_mismatch",
      fallback: "selected_source_only",
    });
    expect(
      (() => {
        const admission = runtimeContractAdmission();
        return evaluateMediaCapabilities(
          observation({
            requestVideoFrameCallback: false,
            seekedEvent: true,
            timeupdateEvent: true,
          }),
          admission.qualification,
          admission.authority,
        );
      })(),
    ).toMatchObject({ status: "available", frameObserver: "event_fallback" });
    expect(
      evaluateMediaCapabilities(
        observation(),
        qualification({ maximumCancelMs: -1 }),
      ),
    ).toMatchObject({
      status: "capability_mismatch",
      fallback: "selected_source_only",
    });
  });
});

describe("M25-12 public runtime asset manifest", () => {
  it("derives a frozen, fingerprint-bound and private-free manifest", () => {
    const value = snapshot();
    const manifest = buildPublicAssetManifest(value);
    expect(manifest.profileFingerprint).toBe(RUNTIME_PROFILE_FINGERPRINT);
    expect(manifest.publicFingerprint).toBe(value.publicFingerprint);
    expect(manifest.assets.map((asset) => asset.assetId)).toEqual([
      "vid-primary",
      "vid-overlay",
      "img-overlay",
      "font-main",
    ]);
    expect(privateKeys(manifest)).toEqual([]);
    expect(Object.isFrozen(manifest)).toBe(true);
    expect(Object.isFrozen(manifest.assets[0])).toBe(true);
    expect(validatePublicAssetManifest(manifest, value)).toBe(manifest);
  });

  it("rejects a forged manifest before any transport owner is opened", async () => {
    const value = snapshot();
    const forged = structuredClone(
      buildPublicAssetManifest(value),
    ) as PublicAssetManifest;
    Object.assign(forged, { publicFingerprint: `sha256:${"f".repeat(64)}` });
    expect(() => validatePublicAssetManifest(forged, value)).toThrow(
      /contract_mismatch/,
    );

    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: harness.factory,
    });
    await expect(
      runtime.open(forged, value, RUNTIME_PROFILE_FINGERPRINT),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "contract_mismatch" },
    });
    expect(harness.factory).not.toHaveBeenCalled();
    expect(privateKeys(runtime.snapshot())).toEqual([]);
  });
});

describe("M25-12 editor runtime lifecycle", () => {
  it.each([true, false])(
    "drives the intrinsic media element through %s frame observation and owns teardown",
    async (useRvfc) => {
      const value = snapshot();
      const natives = new Map<string, ReturnType<typeof nativeVideoHarness>>();
      const releases = new Map<string, ReturnType<typeof vi.fn>>();
      const runtime = createEditorRuntime({
        capability: qualifiedCapability(),
        resolveScene: sceneWire,
        openTransport: createHtmlMediaElementTransportFactory(
          async ({ ownerId }) => {
            const native = nativeVideoHarness(useRvfc);
            const release = vi.fn(async () => undefined);
            natives.set(ownerId, native);
            releases.set(ownerId, release);
            return { element: native.video, release };
          },
          {
            frameObserver: useRvfc
              ? "request_video_frame_callback"
              : "event_fallback",
          },
        ),
      });
      await runtime.open(
        buildPublicAssetManifest(value),
        value,
        RUNTIME_PROFILE_FINGERPRINT,
      );
      await runtime.seek(12);
      const primary = natives.get("clip-main");
      const overlay = natives.get("clip-video-overlay");
      expect(primary?.video.currentTime).toBe(0.5);
      expect(overlay?.video.currentTime).toBe(0);
      await runtime.play();
      await runtime.pause();
      expect(primary?.play).toHaveBeenCalledTimes(1);
      expect(overlay?.play).toHaveBeenCalledTimes(1);
      expect(primary?.pause).toHaveBeenCalledTimes(1);
      expect(overlay?.pause).toHaveBeenCalledTimes(1);
      await runtime.close();
      for (const ownerId of ["clip-main", "clip-video-overlay"]) {
        const native = natives.get(ownerId);
        expect(native?.pause).toHaveBeenCalledTimes(2);
        expect(native?.removeAttribute).toHaveBeenCalledWith("src");
        expect(native?.load).toHaveBeenCalledTimes(1);
        expect(releases.get(ownerId)).toHaveBeenCalledTimes(1);
        expect(native?.cancelVideoFrameCallback.mock.calls.length).toBe(
          useRvfc ? 1 : 0,
        );
      }
    },
  );

  it("reports a pending native frame observation until presentation completes", async () => {
    const value = snapshot();
    const native = nativeVideoHarness(true, false);
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: createHtmlMediaElementTransportFactory(async () => ({
        element: native.video,
        release: async () => undefined,
      })),
    });
    const opening = runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await vi.waitFor(() => {
      expect(runtime.snapshot().resources.pendingRvfcOwners).toBe(1);
    });
    native.presentPending();
    await expect(opening).resolves.toMatchObject({ status: "applied" });
    expect(runtime.snapshot().resources.pendingRvfcOwners).toBe(0);
    await runtime.close();
  });

  it("rejects two live transport owners sharing one native media element", async () => {
    const value = snapshot();
    const asset = buildPublicAssetManifest(value).assets[0];
    const native = nativeVideoHarness(true);
    const releases = new Map([
      ["owner-a", vi.fn(async () => undefined)],
      ["owner-b", vi.fn(async () => undefined)],
    ]);
    const factory = createHtmlMediaElementTransportFactory(
      async ({ ownerId }) => ({
        element: native.video,
        release: releases.get(ownerId)!,
      }),
    );
    const first = await factory({
      asset,
      ownerId: "owner-a",
      epoch: 1,
      signal: new AbortController().signal,
    });
    await expect(
      factory({
        asset,
        ownerId: "owner-b",
        epoch: 1,
        signal: new AbortController().signal,
      }),
    ).rejects.toMatchObject({ code: "contract_mismatch" });
    expect(releases.get("owner-a")).not.toHaveBeenCalled();
    expect(releases.get("owner-b")).toHaveBeenCalledTimes(1);
    await first.close();
    expect(releases.get("owner-a")).toHaveBeenCalledTimes(1);
  });

  it("fails closed and remains releasable when native frame registration throws", async () => {
    const value = snapshot();
    const asset = buildPublicAssetManifest(value).assets[0];
    const native = nativeVideoHarness(true);
    Object.defineProperty(native.video, "requestVideoFrameCallback", {
      configurable: true,
      value: vi.fn(() => {
        throw new Error("registration failed");
      }),
    });
    const release = vi.fn(async () => undefined);
    const factory = createHtmlMediaElementTransportFactory(async () => ({
      element: native.video,
      release,
    }));
    const transport = await factory({
      asset,
      ownerId: "owner-a",
      epoch: 1,
      signal: new AbortController().signal,
    });
    await expect(
      transport.seek({
        epoch: 1,
        sourceFrame: 0,
        sourcePts: 0,
        signal: new AbortController().signal,
      }),
    ).rejects.toMatchObject({ code: "transport_failure" });
    await transport.close();
    expect(release).toHaveBeenCalledTimes(1);
  });

  it("does not let a transport command overtake an opening epoch", async () => {
    const value = snapshot();
    const harness = transportHarness();
    let resolveOpen: ((value: Record<string, unknown>) => void) | undefined;
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: (frame, current) =>
        new Promise<Record<string, unknown>>((resolve) => {
          resolveOpen = () => resolve(sceneWire(frame, current));
        }),
      openTransport: harness.factory,
    });
    const opening = runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    expect(runtime.snapshot().status).toBe("opening");
    await expect(runtime.play()).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "unsupported" },
    });
    expect(runtime.snapshot().status).toBe("opening");
    expect(harness.factory).not.toHaveBeenCalled();
    resolveOpen?.(sceneWire(0, value));
    await expect(opening).resolves.toMatchObject({
      status: "applied",
      outputFrame: 0,
    });
    await runtime.close();
  });

  it("does not let pause overtake a pending play operation", async () => {
    const value = snapshot();
    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    let finishPlay: (() => void) | undefined;
    harness.transports[0]!.playMock.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          finishPlay = resolve;
        }),
    );
    const playing = runtime.play();
    expect(runtime.snapshot().resources.pendingOperations).toBe(1);
    await expect(runtime.pause()).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "unsupported" },
    });
    expect(runtime.snapshot().status).toBe("paused");
    finishPlay?.();
    await expect(playing).resolves.toMatchObject({ status: "applied" });
    expect(runtime.snapshot().status).toBe("playing");
    await runtime.close();
  });

  it("rejects a forged available capability before transport acquisition", async () => {
    const value = snapshot();
    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: { ...qualifiedCapability(), qualified: false },
      resolveScene: sceneWire,
      openTransport: harness.factory,
    });
    await expect(
      runtime.open(
        buildPublicAssetManifest(value),
        value,
        RUNTIME_PROFILE_FINGERPRINT,
      ),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "qualification_required" },
    });
    expect(harness.factory).not.toHaveBeenCalled();
  });

  it("rejects a copied available capability before transport acquisition", async () => {
    const value = snapshot();
    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: { ...qualifiedCapability() },
      resolveScene: sceneWire,
      openTransport: harness.factory,
    });
    await expect(
      runtime.open(
        buildPublicAssetManifest(value),
        value,
        RUNTIME_PROFILE_FINGERPRINT,
      ),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "qualification_required" },
    });
    expect(harness.factory).not.toHaveBeenCalled();
  });

  it("closes an unowned malformed transport before blocking", async () => {
    const value = snapshot();
    const close = vi.fn(async () => undefined);
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: vi.fn(
        async ({ asset, ownerId, epoch }) =>
          ({
            ownerId,
            assetId: asset.assetId,
            openedEpoch: epoch,
            close,
          }) as unknown as MediaTransport,
      ),
    });
    await expect(
      runtime.open(
        buildPublicAssetManifest(value),
        value,
        RUNTIME_PROFILE_FINGERPRINT,
      ),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "contract_mismatch" },
    });
    expect(close).toHaveBeenCalledTimes(1);
    expect(runtime.snapshot().resources.activeVideoOwners).toBe(0);
  });

  it("maps exact scene frames into at most two opaque VIDEO transport owners", async () => {
    const value = snapshot();
    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: harness.factory,
    });
    const notifications = vi.fn();
    const unsubscribe = runtime.subscribe(notifications);

    await expect(
      runtime.open(
        buildPublicAssetManifest(value),
        value,
        RUNTIME_PROFILE_FINGERPRINT,
      ),
    ).resolves.toMatchObject({ status: "applied", outputFrame: 0 });
    expect(runtime.snapshot()).toMatchObject({
      status: "paused",
      outputFrame: 0,
      sources: [
        {
          ownerId: "clip-main",
          assetId: "vid-primary",
          sourceFrame: 0,
          sourcePts: 0,
        },
      ],
      resources: {
        activeVideoOwners: 1,
        warmVideoOwners: 0,
        canvasOwners: 0,
        pendingOperations: 0,
        pendingRvfcOwners: 0,
      },
    });

    await expect(runtime.seek(12)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 12,
    });
    expect(runtime.snapshot()).toMatchObject({
      status: "paused",
      outputFrame: 12,
      sources: [
        { assetId: "vid-primary", sourceFrame: 12, sourcePts: 6144 },
        { assetId: "vid-overlay", sourceFrame: 0, sourcePts: 0 },
      ],
      resources: { activeVideoOwners: 2 },
    });
    expect(harness.maximumLive()).toBe(2);

    await expect(runtime.play()).resolves.toMatchObject({ status: "applied" });
    expect(runtime.snapshot().status).toBe("playing");
    await expect(runtime.pause()).resolves.toMatchObject({
      status: "applied",
    });
    expect(runtime.snapshot().status).toBe("paused");
    expect(
      harness.transports.every((item) => item.playMock.mock.calls.length <= 1),
    ).toBe(true);
    expect(JSON.stringify(runtime.snapshot())).not.toMatch(
      /audio(Status|Control|Track|Mixer)/i,
    );
    expect(notifications).toHaveBeenCalled();
    unsubscribe();
    await runtime.close();
  });

  it("advances through observations after the final sparse landmark while the source remains admitted", async () => {
    const value = snapshot();
    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: (frame, current) =>
        frame >= 12 && frame < 24
          ? {
              ...sceneWire(frame, current),
              layers: [primaryLayer(frame), overlayLayer(frame)],
            }
          : sceneWire(frame, current),
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.seek(12);
    await runtime.play();
    const epoch = 2;
    harness.transports
      .find((transport) => transport.ownerId === "clip-main")!
      .emit({
        ownerId: "clip-main",
        assetId: "vid-primary",
        epoch,
        sourceFrame: 12,
        sourcePts: 6_144,
        observedSourcePts: 7_168,
        observation: "request_video_frame_callback",
      });
    harness.transports
      .find((transport) => transport.ownerId === "clip-video-overlay")!
      .emit({
        ownerId: "clip-video-overlay",
        assetId: "vid-overlay",
        epoch,
        sourceFrame: 0,
        sourcePts: 0,
        // The fixture's final sparse anchor is frame 12 / PTS 6,144, but the admitted asset has
        // 24 frames. A healthy native observation at frame 15 must not be mistaken for source end.
        observedSourcePts: 7_680,
        observation: "request_video_frame_callback",
      });

    await expect(runtime.advance(14)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 14,
    });
    expect(runtime.snapshot().sources).toEqual([
      expect.objectContaining({
        ownerId: "clip-main",
        observedSourcePts: 7_168,
      }),
      expect.objectContaining({
        ownerId: "clip-video-overlay",
        observedSourcePts: 7_680,
      }),
    ]);
    // Native callbacks keep the seek anchors while their observed PTS advances. They must remain
    // admissible after the runtime has published a later scene source frame.
    harness.transports
      .find((transport) => transport.ownerId === "clip-main")!
      .emit({
        ownerId: "clip-main",
        assetId: "vid-primary",
        epoch,
        sourceFrame: 12,
        sourcePts: 6_144,
        observedSourcePts: 8_192,
        observation: "request_video_frame_callback",
      });
    harness.transports
      .find((transport) => transport.ownerId === "clip-video-overlay")!
      .emit({
        ownerId: "clip-video-overlay",
        assetId: "vid-overlay",
        epoch,
        sourceFrame: 0,
        sourcePts: 0,
        observedSourcePts: 8_704,
        observation: "request_video_frame_callback",
      });
    await expect(runtime.advance(16)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 16,
    });
    await runtime.close();
  });

  it("keeps a shared scene behind every live video clock until the lagging owner catches up", async () => {
    const value = snapshotFixture(SMOKE_SHAPE);
    const harness = transportHarness();
    const resolver = vi.fn(resolveCompositionScene);
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: (frame, current) => resolver(current, frame),
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    const emit = (ownerId: string, frame: number) =>
      harness.transports
        .find((item) => item.ownerId === ownerId)!
        .emit({
          ownerId,
          assetId: "vid-primary",
          epoch: 1,
          sourceFrame: 0,
          sourcePts: 0,
          observedSourcePts: frame * 512,
          observation: "request_video_frame_callback",
        });
    emit("clip-0", 11);
    emit("clip-1", 11);
    await expect(runtime.advance(11)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 11,
    });
    const resolvedBeforeLag = resolver.mock.calls.length;
    const nativeCommands = harness.transports.map((item) => [
      item.seekMock.mock.calls.length,
      item.pauseMock.mock.calls.length,
      item.playMock.mock.calls.length,
    ]);

    emit("clip-0", 12);
    // Same native receipt as the real-media failure: primary PTS6144, overlay PTS5632.
    await expect(runtime.advance(12)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 11,
      blocker: null,
    });
    expect(runtime.snapshot()).toMatchObject({
      status: "playing",
      outputFrame: 11,
    });
    // Resolving a future scene mutates the compositor's scene binding even if publication waits.
    expect(resolver).toHaveBeenCalledTimes(resolvedBeforeLag);
    emit("clip-1", 12);
    await expect(runtime.advance(12)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 12,
    });
    expect(runtime.snapshot().sources).toEqual([
      expect.objectContaining({
        ownerId: "clip-0",
        sourcePts: 6144,
        observedSourcePts: 6144,
      }),
      expect.objectContaining({
        ownerId: "clip-1",
        sourcePts: 6144,
        observedSourcePts: 6144,
      }),
    ]);
    expect(
      harness.transports.map((item) => [
        item.seekMock.mock.calls.length,
        item.pauseMock.mock.calls.length,
        item.playMock.mock.calls.length,
      ]),
    ).toEqual(nativeCommands);
    await runtime.close();
    expect(harness.live()).toBe(0);
  });

  it("hands an expired overlay to exact seek instead of pinning the primary clock", async () => {
    const value = snapshot();
    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: (frame, current) => resolveCompositionScene(current, frame),
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.seek(12);
    await runtime.play();
    harness.transports
      .find((item) => item.ownerId === "clip-main")!
      .emit({
        ownerId: "clip-main",
        assetId: "vid-primary",
        epoch: 2,
        sourceFrame: 12,
        sourcePts: 6144,
        observedSourcePts: 24 * 512,
        observation: "request_video_frame_callback",
      });
    // The overlay's last observation remains at its start while its timeline interval ends24.
    await expect(runtime.advance(24)).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "unsupported" },
    });
    await expect(runtime.seek(24)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 24,
    });
    expect(runtime.snapshot().sources.map((row) => row.ownerId)).toEqual([
      "clip-main",
    ]);
    expect(harness.live()).toBe(1);
    await runtime.close();
    expect(harness.live()).toBe(0);
  });

  it("does not resolve the same frame again while the native observation is unchanged", async () => {
    const value = snapshot();
    const harness = transportHarness();
    const resolver = vi.fn(
      (frame: number, current: PublicCompositionSnapshot) =>
        frame >= 12 && frame < 24
          ? {
              ...sceneWire(frame, current),
              layers: [primaryLayer(frame), overlayLayer(frame)],
            }
          : sceneWire(frame, current),
    );
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: resolver,
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.seek(12);
    await runtime.play();
    const epoch = 2;
    harness.transports
      .find((transport) => transport.ownerId === "clip-main")!
      .emit({
        ownerId: "clip-main",
        assetId: "vid-primary",
        epoch,
        sourceFrame: 12,
        sourcePts: 6_144,
        observedSourcePts: 7_168,
        observation: "request_video_frame_callback",
      });
    harness.transports
      .find((transport) => transport.ownerId === "clip-video-overlay")!
      .emit({
        ownerId: "clip-video-overlay",
        assetId: "vid-overlay",
        epoch,
        sourceFrame: 0,
        sourcePts: 0,
        observedSourcePts: 7_680,
        observation: "request_video_frame_callback",
      });

    await expect(runtime.advance(20)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 14,
    });
    const callsAfterFirstAdvance = resolver.mock.calls.length;
    await expect(runtime.advance(20)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 14,
    });
    expect(resolver).toHaveBeenCalledTimes(callsAfterFirstAdvance);
    await runtime.close();
  });

  it("hands an adjacent cut back to exact seek after the native source reaches its final frame", async () => {
    const value = adjacentCutSnapshot();
    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: adjacentCutSceneWire,
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    harness.transports
      .find((transport) => transport.ownerId === "clip-main")!
      .emit({
        ownerId: "clip-main",
        assetId: "vid-primary",
        epoch: 1,
        sourceFrame: 0,
        sourcePts: 0,
        observedSourcePts: 23 * 512,
        observation: "request_video_frame_callback",
      });

    // The steady native owner cannot produce frame 24: that frame belongs to the next clip.
    // `unsupported` is the scheduler's explicit signal to perform one exact ownership-changing
    // seek instead of repainting frame 23 forever after the first element ends.
    await expect(runtime.advance(24)).resolves.toMatchObject({
      status: "blocked",
      outputFrame: 0,
      blocker: { code: "unsupported", subjectId: "clip-main" },
    });
    await expect(runtime.seek(24)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 24,
    });
    expect(runtime.snapshot().sources).toEqual([
      expect.objectContaining({
        ownerId: "clip-second",
        assetId: "vid-overlay",
      }),
    ]);
    await runtime.close();
  });

  it("cancels a stale resolver epoch without publishing or allocating it", async () => {
    const value = snapshot();
    const harness = transportHarness();
    let resolveStale: ((value: Record<string, unknown>) => void) | undefined;
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: (frame, current) =>
        frame === 12
          ? new Promise<Record<string, unknown>>((resolve) => {
              resolveStale = resolve;
            })
          : sceneWire(frame, current),
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );

    const stale = runtime.seek(12);
    const current = runtime.seek(0);
    await expect(current).resolves.toMatchObject({
      status: "applied",
      outputFrame: 0,
    });
    resolveStale?.(sceneWire(12, value));
    await expect(stale).resolves.toMatchObject({ status: "cancelled" });
    expect(runtime.snapshot()).toMatchObject({
      status: "paused",
      outputFrame: 0,
      resources: { activeVideoOwners: 1 },
    });
    expect(harness.maximumLive()).toBe(1);
    await runtime.close();
  });

  it("replaces generations transactionally and closes every owner idempotently", async () => {
    const first = snapshot();
    const second = replacementSnapshot();
    const harness = transportHarness();
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: harness.factory,
    });
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    const oldTransport = harness.transports[0]!;
    const oldEpoch = runtime.snapshot().epoch;

    await expect(
      runtime.replace(buildPublicAssetManifest(second), second),
    ).resolves.toMatchObject({ status: "applied", outputFrame: 0 });
    expect(oldTransport.closeMock).toHaveBeenCalledTimes(1);
    expect(runtime.snapshot()).toMatchObject({
      publicFingerprint: second.publicFingerprint,
      resources: { activeVideoOwners: 1 },
    });
    oldTransport.emit({
      ownerId: oldTransport.ownerId,
      assetId: oldTransport.assetId,
      epoch: oldEpoch,
      sourceFrame: 47,
      sourcePts: 24064,
    });
    expect(runtime.snapshot().sources[0]?.sourceFrame).toBe(0);

    await expect(runtime.close()).resolves.toMatchObject({ status: "closed" });
    await expect(runtime.close()).resolves.toMatchObject({ status: "closed" });
    expect(runtime.snapshot()).toMatchObject({
      status: "closed",
      resources: {
        activeVideoOwners: 0,
        warmVideoOwners: 0,
        canvasOwners: 0,
        pendingOperations: 0,
        pendingRvfcOwners: 0,
      },
    });
    expect(harness.live()).toBe(0);
    expect(
      harness.transports.every(
        (item) => item.closeMock.mock.calls.length === 1,
      ),
    ).toBe(true);
  });
});

describe("reauthorizing held transports for an accepted revision", () => {
  it("keeps a matching owner and its nonzero frame without publishing opening", async () => {
    const first = snapshot();
    const next = replacementSnapshot();
    const harness = transportHarness();
    const rebind = vi.fn(async () => true);
    const factory: MediaTransportFactory = vi.fn(async (request) => ({
      ...(await harness.factory(request)),
      rebind,
    }));
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: factory,
    });
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.seek(17);
    const states: ReturnType<typeof runtime.snapshot>[] = [];
    const unsubscribe = runtime.subscribe(() =>
      states.push(runtime.snapshot()),
    );
    const priorEpoch = runtime.snapshot().epoch;
    await expect(
      runtime.replace(buildPublicAssetManifest(next), next),
    ).resolves.toMatchObject({
      status: "applied",
      outputFrame: 17,
    });
    expect(factory).toHaveBeenCalledTimes(1);
    expect(rebind).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        ownerId: "clip-main",
        epoch: priorEpoch + 1,
        asset: buildPublicAssetManifest(next).assets.find(
          (asset) => asset.assetId === "vid-primary",
        ),
        signal: expect.any(AbortSignal),
      }),
    );
    expect(harness.transports[0]!.closeMock).not.toHaveBeenCalled();
    expect(
      states.every(
        (state) => state.status !== "opening" && state.outputFrame === 17,
      ),
    ).toBe(true);
    expect(
      harness.transports[0]!.seekMock.mock.calls.at(-1)?.[0],
    ).toMatchObject({ epoch: priorEpoch + 1 });
    unsubscribe();
    await runtime.close();
    expect(harness.live()).toBe(0);
  });

  it("reopens only the owner whose reauthorization is refused", async () => {
    const first = snapshot();
    const next = replacementSnapshot();
    const harness = transportHarness();
    const rebinds = new Map<string, ReturnType<typeof vi.fn>>();
    const overlayId = first.clips[1]!.clipId;
    const factory: MediaTransportFactory = vi.fn(async (request) => {
      const rebind = vi.fn(async () => request.ownerId !== overlayId);
      rebinds.set(request.ownerId, rebind);
      return { ...(await harness.factory(request)), rebind };
    });
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: factory,
    });
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.seek(12);
    const primary = harness.transports[0]!;
    const overlay = harness.transports[1]!;
    const priorPrimaryRebind = rebinds.get("clip-main")!;
    const priorOverlayRebind = rebinds.get(overlayId)!;
    await expect(
      runtime.replace(buildPublicAssetManifest(next), next),
    ).resolves.toMatchObject({
      status: "applied",
      outputFrame: 12,
    });
    expect(priorPrimaryRebind).toHaveBeenCalledOnce();
    expect(priorOverlayRebind).toHaveBeenCalledOnce();
    expect(primary.closeMock).not.toHaveBeenCalled();
    expect(overlay.closeMock).toHaveBeenCalledOnce();
    expect(factory).toHaveBeenCalledTimes(3);
    expect(harness.maximumLive()).toBe(2);
    await runtime.close();
    expect(harness.live()).toBe(0);
  });

  it("closes a cancelled rebind before the newer replacement can acquire its successor", async () => {
    const first = snapshot();
    const harness = transportHarness();
    const rebind = vi.fn(
      (request: Parameters<MediaTransportFactory>[0]) =>
        new Promise<boolean>((resolve) => {
          request.signal.addEventListener("abort", () => resolve(false), {
            once: true,
          });
        }),
    );
    const factory: MediaTransportFactory = vi.fn(async (request) => ({
      ...(await harness.factory(request)),
      rebind,
    }));
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: sceneWire,
      openTransport: factory,
    });
    await runtime.open(
      buildPublicAssetManifest(first),
      first,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.seek(17);
    const next = replacementSnapshot();
    const replacing = runtime.replace(buildPublicAssetManifest(next), next);
    await vi.waitFor(() => expect(rebind).toHaveBeenCalledOnce());
    const wire = structuredClone(fixture.snapshot) as Record<string, unknown>;
    wire.timeline_revision = 13;
    wire.timeline_fingerprint = `sha256:${"4".repeat(64)}`;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const latest = decodePublicCompositionSnapshot(wire);
    const successor = runtime.replace(buildPublicAssetManifest(latest), latest);
    await expect(replacing).resolves.toMatchObject({ status: "cancelled" });
    await expect(successor).resolves.toMatchObject({
      status: "applied",
      outputFrame: 17,
    });
    expect(harness.transports[0]!.closeMock).toHaveBeenCalledOnce();
    expect(factory).toHaveBeenCalledTimes(2);
    expect(runtime.snapshot()).toMatchObject({
      publicFingerprint: latest.publicFingerprint,
      resources: { activeVideoOwners: 1, pendingOperations: 0 },
    });
    await runtime.close();
    expect(harness.live()).toBe(0);
  });
});

// The profile declares one warm video owner. While a clip plays, the owner of the next ownership
// change is opened and sought ahead of it, so the move at the change adopts an owner instead of
// acquiring one. These cases hold the warm owner to what makes that safe: it is outside the
// epoch's operation accounting, it is never abandoned mid-acquisition, there is never more than
// one, every release path closes it, and its failure is its own.
describe("the warm video owner prepared ahead of an ownership change", () => {
  // Three adjacent primary clips: clip-main [0, 24), clip-second [24, 48), clip-third [48, 72).
  function threeClipSnapshot(revision?: number): PublicCompositionSnapshot {
    const wire = structuredClone(fixture.snapshot) as Record<string, any>;
    const first = wire.clips[0];
    const second = structuredClone(wire.clips[1]);
    first.duration_frames = 24;
    second.clip_id = "clip-second";
    second.track_id = "track-primary";
    second.start_frame = 24;
    second.duration_frames = 24;
    second.transition = { kind: "none", duration_frames: 0 };
    second.effect = structuredClone(first.effect);
    const third = structuredClone(first);
    third.clip_id = "clip-third";
    third.start_frame = 48;
    wire.clips = [first, second, third];
    wire.output.duration_frames = 72;
    wire.assets = wire.assets.filter((asset: Record<string, unknown>) =>
      ["vid-primary", "vid-overlay"].includes(String(asset.asset_id)),
    );
    if (revision !== undefined) {
      wire.timeline_revision = revision;
      wire.timeline_fingerprint = `sha256:${"3".repeat(64)}`;
    }
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    return decodePublicCompositionSnapshot(wire);
  }

  function threeClipSceneWire(
    frame: number,
    value: PublicCompositionSnapshot,
  ): Record<string, unknown> {
    const first = primaryLayer(frame);
    const owner =
      frame < 24
        ? first
        : frame < 48
          ? {
              ...first,
              clip_id: "clip-second",
              asset_id: "vid-overlay",
              source_frame: frame - 24,
              source_pts: (frame - 24) * 512,
            }
          : {
              ...first,
              clip_id: "clip-third",
              source_frame: frame - 48,
              source_pts: (frame - 48) * 512,
            };
    return {
      schema: RESOLVED_SCENE_SCHEMA,
      profile_id: value.profileId,
      public_fingerprint: value.publicFingerprint,
      frame,
      layers: [owner],
      audio_span: null,
      blockers: [],
    };
  }

  function gate() {
    let open!: () => void;
    const opened = new Promise<void>((resolve) => {
      open = resolve;
    });
    return { opened, open };
  }

  /** A runtime that is playing `clip-main`, with every transport request observable. */
  async function playing(
    intercept?: (
      request: Parameters<MediaTransportFactory>[0],
      open: MediaTransportFactory,
    ) => ReturnType<MediaTransportFactory>,
  ) {
    const value = threeClipSnapshot();
    const harness = transportHarness();
    const requests: Parameters<MediaTransportFactory>[0][] = [];
    const runtime = createEditorRuntime({
      capability: qualifiedCapability(),
      resolveScene: threeClipSceneWire,
      openTransport: (request) => {
        requests.push(request);
        return intercept === undefined
          ? harness.factory(request)
          : intercept(request, harness.factory);
      },
    });
    await runtime.open(
      buildPublicAssetManifest(value),
      value,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    await runtime.play();
    const owner = (ownerId: string) =>
      harness.transports.find((transport) => transport.ownerId === ownerId);
    return {
      value,
      harness,
      runtime,
      requests,
      owner,
      prepare: (...frames: number[]) =>
        runtime.prepare!(
          frames.map((frame) => threeClipSceneWire(frame, value)),
        ),
    };
  }

  it("opens and seeks the incoming owner while playing, and the move at the cut adopts it", async () => {
    const h = await playing();
    await expect(h.prepare(24)).resolves.toMatchObject({ status: "applied" });

    const warm = h.owner("clip-second")!;
    expect(warm.seekMock).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ preroll: true, sourceFrame: 0, sourcePts: 0 }),
    );
    expect(warm.playMock).not.toHaveBeenCalled();
    // The presented scene is untouched: one active owner, still playing its own frame.
    expect(h.runtime.snapshot()).toMatchObject({
      status: "playing",
      outputFrame: 0,
      blocker: null,
      resources: {
        activeVideoOwners: 1,
        warmVideoOwners: 1,
        pendingOperations: 0,
      },
    });
    expect(h.runtime.snapshot().sources.map((row) => row.ownerId)).toEqual([
      "clip-main",
    ]);

    await expect(h.runtime.seek(24)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 24,
    });
    // No third request: the move took the owner that was already open.
    expect(h.requests.map((request) => request.ownerId)).toEqual([
      "clip-main",
      "clip-second",
    ]);
    expect(warm.closeMock).not.toHaveBeenCalled();
    expect(warm.seekMock).toHaveBeenCalledTimes(2);
    expect(warm.seekMock.mock.calls[1]![0]).not.toHaveProperty("preroll");
    expect(warm.playMock).toHaveBeenCalledTimes(1);
    expect(h.owner("clip-main")!.closeMock).toHaveBeenCalledTimes(1);
    expect(h.runtime.snapshot()).toMatchObject({
      status: "playing",
      resources: { activeVideoOwners: 1, warmVideoOwners: 0 },
    });
    expect(h.runtime.snapshot().sources.map((row) => row.ownerId)).toEqual([
      "clip-second",
    ]);
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("is idempotent for the same owner and source position", async () => {
    const h = await playing();
    await h.prepare(24);
    await expect(h.prepare(24, 48)).resolves.toMatchObject({
      status: "applied",
    });
    expect(h.requests).toHaveLength(2);
    expect(h.owner("clip-second")!.seekMock).toHaveBeenCalledTimes(1);
    await h.runtime.close();
  });

  it("prepares only while playing, and a paused runtime gives the owner back", async () => {
    const h = await playing();
    await h.runtime.pause();
    await expect(h.prepare(24)).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "unsupported" },
    });
    expect(h.requests).toHaveLength(1);

    await h.runtime.play();
    await expect(h.prepare(24)).resolves.toMatchObject({ status: "applied" });
    await h.runtime.pause();
    await expect(h.prepare()).resolves.toMatchObject({ status: "applied" });
    expect(h.owner("clip-second")!.closeMock).toHaveBeenCalledTimes(1);
    expect(h.runtime.snapshot()).toMatchObject({
      status: "paused",
      blocker: null,
      resources: { activeVideoOwners: 1, warmVideoOwners: 0 },
    });
    // Nothing held: releasing again is still an applied no-op.
    await expect(h.prepare()).resolves.toMatchObject({ status: "applied" });
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("is not an operation of the epoch: pause and play are accepted during a preparation", async () => {
    const slow = gate();
    const h = await playing(async (request, open) => {
      if (request.ownerId === "clip-second") await slow.opened;
      return open(request);
    });
    const preparing = h.prepare(24);
    await vi.waitFor(() => expect(h.requests).toHaveLength(2));

    expect(h.runtime.snapshot().resources.pendingOperations).toBe(0);
    await expect(h.runtime.pause()).resolves.toMatchObject({
      status: "applied",
    });
    await expect(h.runtime.play()).resolves.toMatchObject({
      status: "applied",
    });
    slow.open();
    await expect(preparing).resolves.toMatchObject({ status: "applied" });
    expect(h.runtime.snapshot().resources.warmVideoOwners).toBe(1);
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("makes a move wait for a preparation in flight instead of aborting it", async () => {
    const slow = gate();
    const h = await playing(async (request, open) => {
      if (request.ownerId === "clip-second") await slow.opened;
      return open(request);
    });
    const preparing = h.prepare(24);
    await vi.waitFor(() => expect(h.requests).toHaveLength(2));
    const moving = h.runtime.seek(24);
    await new Promise((resolve) => setImmediate(resolve));

    // One request for the incoming owner, still alive: the move neither cancelled it nor
    // opened a second one beside it.
    expect(h.requests).toHaveLength(2);
    expect(h.requests[1]!.signal.aborted).toBe(false);
    slow.open();
    await expect(moving).resolves.toMatchObject({
      status: "applied",
      outputFrame: 24,
    });
    await preparing;
    expect(h.requests).toHaveLength(2);
    expect(h.requests[1]!.signal.aborted).toBe(false);
    expect(h.owner("clip-second")!.closeMock).not.toHaveBeenCalled();
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("keeps a waiting move out of the set a superseding seek drains within the cancel deadline", async () => {
    const slow = gate();
    const h = await playing(async (request, open) => {
      if (request.ownerId === "clip-second") await slow.opened;
      return open(request);
    });
    const preparing = h.prepare(24);
    await vi.waitFor(() => expect(h.requests).toHaveLength(2));
    vi.useFakeTimers();
    try {
      const first = h.runtime.seek(24);
      await vi.advanceTimersByTimeAsync(0);
      const second = h.runtime.seek(30);
      // The preparation outlasts the 250 ms cancel deadline. A wait registered as an operation
      // would fail that drain and block the session; an ordinary acquisition is this long.
      await vi.advanceTimersByTimeAsync(400);
      slow.open();
      await vi.advanceTimersByTimeAsync(0);
      await expect(first).resolves.toMatchObject({ status: "cancelled" });
      await expect(second).resolves.toMatchObject({
        status: "applied",
        outputFrame: 30,
      });
    } finally {
      vi.useRealTimers();
    }
    await preparing;
    expect(h.runtime.snapshot().blocker).toBeNull();
    expect(h.requests).toHaveLength(2);
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("leaves the warm owner alone for a move that needs no owner, and closes it first for one that needs another", async () => {
    const h = await playing();
    await h.prepare(24);
    const warm = h.owner("clip-second")!;

    await expect(h.runtime.seek(5)).resolves.toMatchObject({
      status: "applied",
    });
    expect(warm.closeMock).not.toHaveBeenCalled();
    expect(h.runtime.snapshot().resources.warmVideoOwners).toBe(1);

    await expect(h.runtime.seek(50)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 50,
    });
    expect(warm.closeMock).toHaveBeenCalledTimes(1);
    expect(h.runtime.snapshot().sources.map((row) => row.ownerId)).toEqual([
      "clip-third",
    ]);
    expect(h.runtime.snapshot().resources).toMatchObject({
      activeVideoOwners: 1,
      warmVideoOwners: 0,
    });
    // Never a third transport alive: the warm one was closed before the new one was opened.
    expect(h.harness.maximumLive()).toBe(2);
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("holds one warm owner at most", async () => {
    const h = await playing();
    await h.prepare(24);
    await expect(h.prepare(48)).resolves.toMatchObject({ status: "applied" });
    expect(h.owner("clip-second")!.closeMock).toHaveBeenCalledTimes(1);
    expect(h.owner("clip-third")!.closeMock).not.toHaveBeenCalled();
    expect(h.runtime.snapshot().resources).toMatchObject({
      activeVideoOwners: 1,
      warmVideoOwners: 1,
    });
    expect(h.harness.maximumLive()).toBe(2);
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("returns a refusal without touching playback, and the cut then acquires as before", async () => {
    let refuse = true;
    const h = await playing(async (request, open) => {
      if (request.ownerId === "clip-second" && refuse) {
        refuse = false;
        throw new Error("synthetic_source_refused");
      }
      return open(request);
    });
    await expect(h.prepare(24)).resolves.toMatchObject({ status: "blocked" });
    expect(h.runtime.snapshot()).toMatchObject({
      status: "playing",
      outputFrame: 0,
      blocker: null,
      resources: { activeVideoOwners: 1, warmVideoOwners: 0 },
    });

    await expect(h.runtime.seek(24)).resolves.toMatchObject({
      status: "applied",
      outputFrame: 24,
    });
    expect(h.requests.map((request) => request.ownerId)).toEqual([
      "clip-main",
      "clip-second",
      "clip-second",
    ]);
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("closes a failing warm owner alone", async () => {
    let fail: ((code: "transport_failure") => void) | undefined;
    const h = await playing(async (request, open) => {
      const transport = await open(request);
      if (request.ownerId !== "clip-second") return transport;
      return Object.assign(transport, {
        subscribeFailure(listener: (code: "transport_failure") => void) {
          fail = listener;
          return () => {
            fail = undefined;
          };
        },
      });
    });
    await h.prepare(24);
    expect(fail).toBeTypeOf("function");

    fail!("transport_failure");
    await vi.waitFor(() =>
      expect(h.owner("clip-second")!.closeMock).toHaveBeenCalledTimes(1),
    );
    expect(h.runtime.snapshot()).toMatchObject({
      status: "playing",
      blocker: null,
      resources: { activeVideoOwners: 1, warmVideoOwners: 0 },
    });
    expect(h.owner("clip-main")!.closeMock).not.toHaveBeenCalled();
    await h.runtime.close();
    expect(h.harness.live()).toBe(0);
  });

  it("refuses a scene of another composition before any transport is opened", async () => {
    const h = await playing();
    await expect(
      h.runtime.prepare!([
        {
          ...threeClipSceneWire(24, h.value),
          public_fingerprint: `sha256:${"0".repeat(64)}`,
        },
      ]),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "contract_mismatch" },
    });
    expect(h.requests).toHaveLength(1);
    expect(h.runtime.snapshot()).toMatchObject({
      status: "playing",
      blocker: null,
    });
    await h.runtime.close();
  });

  it("closes the warm owner with the active ones on replace and on close", async () => {
    const replaced = await playing();
    await replaced.prepare(24);
    const next = threeClipSnapshot(12);
    await expect(
      replaced.runtime.replace(buildPublicAssetManifest(next), next),
    ).resolves.toMatchObject({ status: "applied" });
    expect(replaced.owner("clip-second")!.closeMock).toHaveBeenCalledTimes(1);
    expect(replaced.runtime.snapshot().resources).toMatchObject({
      activeVideoOwners: 1,
      warmVideoOwners: 0,
    });
    await replaced.runtime.close();
    expect(replaced.harness.live()).toBe(0);

    const closed = await playing();
    await closed.prepare(24);
    await expect(closed.runtime.close()).resolves.toMatchObject({
      status: "closed",
    });
    expect(closed.runtime.snapshot()).toMatchObject({
      blocker: null,
      resources: { activeVideoOwners: 0, warmVideoOwners: 0 },
    });
    expect(closed.harness.live()).toBe(0);
    expect(
      closed.harness.transports.every(
        (item) => item.closeMock.mock.calls.length === 1,
      ),
    ).toBe(true);
  });

  it("leaves no transport behind when it is closed under a preparation in flight", async () => {
    const slow = gate();
    const h = await playing(async (request, open) => {
      if (request.ownerId === "clip-second") await slow.opened;
      return open(request);
    });
    const preparing = h.prepare(24);
    await vi.waitFor(() => expect(h.requests).toHaveLength(2));

    const closing = h.runtime.close();
    slow.open();
    await expect(closing).resolves.toMatchObject({ status: "closed" });
    await expect(preparing).resolves.toMatchObject({ status: "blocked" });
    await vi.waitFor(() => expect(h.harness.live()).toBe(0));
    expect(h.runtime.snapshot()).toMatchObject({
      status: "closed",
      blocker: null,
      resources: { activeVideoOwners: 0, warmVideoOwners: 0 },
    });
  });
});
