import { describe, expect, it, vi } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import { createAuthoringLeasedEditorRuntime } from "../src/runtime/authoringLeasedEditorRuntime";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  evaluateMediaCapabilities,
  type RuntimeCapabilityDisposition,
} from "../src/runtime/mediaCapabilities";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import {
  RUNTIME_RECEIPT_SCHEMA,
  RUNTIME_SNAPSHOT_SCHEMA,
  type EditorRuntime,
  type MediaTransportFactory,
  type RuntimeSnapshot,
} from "../src/runtime/editorRuntime";
import { runtimeContractAdmission } from "./fixtures/browserNleRuntimeFixture";

describe("M25-13 leased editor runtime", () => {
  it("pins the snapshot manifest and maps a video owner to a full-source lease", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    let openTransport!: MediaTransportFactory;
    const sourceRelease = vi.fn(async () => undefined);
    const acquireVideoSource = vi.fn<
      AuthoringMediaSourceLeaseClient["acquireVideoSource"]
    >(async () => ({
      element: nativeVideo(),
      release: sourceRelease,
    }));
    const capability = qualifiedCapability();
    const runtime = fakeRuntime(capability);
    const leased = createAuthoringLeasedEditorRuntime({
      snapshot,
      manifest,
      capability,
      resolveScene: async () => ({}),
      leaseClient: {
        acquireVideoSource,
      } as unknown as AuthoringMediaSourceLeaseClient,
      runtimeFactory: (dependencies) => {
        openTransport = dependencies.openTransport;
        return runtime;
      },
      transportOptions: { frameObserver: "event_fallback" },
    });

    await expect(
      leased.open(manifest, snapshot, RUNTIME_PROFILE_FINGERPRINT),
    ).resolves.toMatchObject({ status: "applied" });
    const transport = await openTransport({
      asset: manifest.assets[1]!,
      ownerId: snapshot.clips[1]!.clipId,
      epoch: 1,
      signal: new AbortController().signal,
    });
    expect(acquireVideoSource).toHaveBeenCalledOnce();
    expect(acquireVideoSource.mock.calls[0]?.[0]).toMatchObject({
      asset: manifest.assets[1],
      ownerId: snapshot.clips[1]?.clipId,
      epoch: 1,
    });
    expect(acquireVideoSource.mock.calls[0]?.[1]).toMatchObject({
      snapshot,
      manifest,
      clipId: snapshot.clips[1]?.clipId,
      sourceStartFrame: 0,
      sourceEndFrame: manifest.assets[1]?.sourceFrameCount,
    });
    await transport.close();
    expect(sourceRelease).toHaveBeenCalledOnce();
    await leased.close();
  });

  it("fails closed before lease I/O when callers substitute snapshot context", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const acquireVideoSource = vi.fn();
    const capability = qualifiedCapability();
    const runtime = fakeRuntime(capability);
    const leased = createAuthoringLeasedEditorRuntime({
      snapshot,
      manifest,
      capability,
      resolveScene: async () => ({}),
      leaseClient: {
        acquireVideoSource,
      } as unknown as AuthoringMediaSourceLeaseClient,
      runtimeFactory: () => runtime,
    });
    const foreignManifest = {
      ...manifest,
      manifestFingerprint: `sha256:${"9".repeat(64)}`,
    };
    const receipt = await leased.open(
      foreignManifest,
      snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    expect(receipt).toMatchObject({
      status: "blocked",
      blocker: { code: "contract_mismatch", subjectId: null },
    });
    expect(acquireVideoSource).not.toHaveBeenCalled();
  });

  it("updates revision authority and forwards the new context to a held source", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 1;
    wire.timeline_fingerprint = `sha256:${"7".repeat(64)}`;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const next = decodePublicCompositionSnapshot(wire);
    const nextManifest = buildPublicAssetManifest(next);
    const rebind = vi.fn(async () => true);
    const owner = {
      element: nativeVideo(),
      rebind,
      async release() {
        expect(this).toBe(owner);
      },
    };
    const acquireVideoSource = vi.fn(async () => owner);
    const capability = qualifiedCapability();
    const runtime = fakeRuntime(capability);
    let dependencies!: Parameters<
      NonNullable<
        Parameters<
          typeof createAuthoringLeasedEditorRuntime
        >[0]["runtimeFactory"]
      >
    >[0];
    const resolveScene = vi.fn(async () => ({}));
    const leased = createAuthoringLeasedEditorRuntime({
      snapshot,
      manifest,
      capability,
      resolveScene,
      leaseClient: {
        acquireVideoSource,
      } as unknown as AuthoringMediaSourceLeaseClient,
      transportOptions: { frameObserver: "event_fallback" },
      runtimeFactory: (value) => {
        dependencies = value;
        return runtime;
      },
    });
    const request = {
      asset: manifest.assets[1]!,
      ownerId: snapshot.clips[1]!.clipId,
      epoch: 1,
      signal: new AbortController().signal,
    };
    const transport = await dependencies.openTransport(request);
    const target = () => 17;
    await expect(
      leased.replace(nextManifest, next, target),
    ).resolves.toMatchObject({ status: "applied" });
    expect(runtime.replace).toHaveBeenCalledExactlyOnceWith(
      nextManifest,
      next,
      target,
    );
    const rebinding = { ...request, asset: nextManifest.assets[1]!, epoch: 2 };
    await expect(transport.rebind!(rebinding)).resolves.toBe(true);
    expect(rebind).toHaveBeenCalledExactlyOnceWith(
      rebinding,
      expect.objectContaining({
        snapshot: next,
        manifest: nextManifest,
        clipId: request.ownerId,
        sourceStartFrame: 0,
        sourceEndFrame: request.asset.sourceFrameCount,
      }),
    );
    expect(acquireVideoSource).toHaveBeenCalledOnce();
    await expect(
      dependencies.resolveScene(17, next, { epoch: 2, signal: request.signal }),
    ).resolves.toEqual({});
    await expect(
      dependencies.resolveScene(17, snapshot, {
        epoch: 2,
        signal: request.signal,
      }),
    ).rejects.toThrow("contract_mismatch");
    expect(resolveScene).toHaveBeenCalledOnce();
    await transport.close();
  });

  it("refuses a different workspace before changing the pinned binding or doing lease I/O", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const wire = structuredClone(fixture.snapshot);
    wire.workspace_handle = "f".repeat(64);
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const foreign = decodePublicCompositionSnapshot(wire);
    const runtime = fakeRuntime(qualifiedCapability());
    const acquireVideoSource = vi.fn();
    const leased = createAuthoringLeasedEditorRuntime({
      snapshot,
      manifest,
      capability: runtime.capabilities(),
      resolveScene: async () => ({}),
      leaseClient: {
        acquireVideoSource,
      } as unknown as AuthoringMediaSourceLeaseClient,
      runtimeFactory: () => runtime,
    });
    await expect(
      leased.replace(buildPublicAssetManifest(foreign), foreign),
    ).resolves.toMatchObject({
      status: "blocked",
      blocker: { code: "contract_mismatch" },
    });
    expect(runtime.replace).not.toHaveBeenCalled();
    expect(acquireVideoSource).not.toHaveBeenCalled();
    await expect(
      leased.open(manifest, snapshot, RUNTIME_PROFILE_FINGERPRINT),
    ).resolves.toMatchObject({ status: "applied" });
  });
});

function fakeRuntime(capability: RuntimeCapabilityDisposition): EditorRuntime {
  const receipt = Object.freeze({
    schema: RUNTIME_RECEIPT_SCHEMA,
    status: "applied" as const,
    epoch: 1,
    outputFrame: 0,
    blocker: null,
  });
  return Object.freeze({
    capabilities: () => capability,
    open: vi.fn(async () => receipt),
    replace: vi.fn(async () => receipt),
    seek: vi.fn(async () => receipt),
    advance: vi.fn(async () => receipt),
    play: vi.fn(async () => receipt),
    pause: vi.fn(async () => receipt),
    snapshot: (): RuntimeSnapshot => ({
      schema: RUNTIME_SNAPSHOT_SCHEMA,
      status: "paused",
      epoch: 1,
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      publicFingerprint: null,
      outputFrame: 0,
      sources: [],
      blocker: null,
      resources: {
        activeVideoOwners: 0,
        warmVideoOwners: 0,
        canvasOwners: 0,
        pendingOperations: 0,
        pendingRvfcOwners: 0,
      },
    }),
    subscribe: () => () => undefined,
    close: vi.fn(async () => ({ ...receipt, status: "closed" as const })),
  });
}

function qualifiedCapability(): RuntimeCapabilityDisposition {
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

function nativeVideo(): HTMLVideoElement {
  return {
    currentTime: 0,
    duration: 1,
    paused: true,
    seeking: false,
    src: "blob:leased",
    play: vi.fn(async () => undefined),
    pause: vi.fn(),
    load: vi.fn(),
    removeAttribute: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  } as unknown as HTMLVideoElement;
}
