import { afterEach, describe, expect, it, vi } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER,
  AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER,
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  AUTHORING_MEDIA_LEASE_REVISION_HEADER,
  AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
  canonicalPublicRuntimeAssetFingerprint,
  type AuthoringMediaLeaseCreateRequest,
} from "../src/contracts/authoringMediaLeaseCodec";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  AuthoringMediaSourceLeaseError,
  createAuthoringMediaSourceLeaseClient,
} from "../src/host/authoringMediaSourceLease";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import { acquireAuthoringVideoSource } from "../src/host/authoringVideoSourceOwner";

const fp = (digit: string) => `sha256:${digit.repeat(64)}`;

function binding(revision = 0) {
  const wire = structuredClone(fixture.snapshot);
  wire.workspace_revision += revision;
  wire.timeline_revision += revision;
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  const snapshot = decodePublicCompositionSnapshot(wire);
  const manifest = buildPublicAssetManifest(snapshot);
  const asset = manifest.assets[0]!;
  const clipId = snapshot.clips[0]!.clipId;
  const context = {
    snapshot,
    manifest,
    clipId,
    sourceStartFrame: 0,
    sourceEndFrame: asset.sourceFrameCount!,
  };
  const request = {
    asset,
    ownerId: clipId,
    epoch: revision + 1,
    signal: new AbortController().signal,
  };
  return { context, request };
}

function authority() {
  const requests: Record<string, unknown>[] = [];
  const live = new Map<string, Record<string, unknown>>();
  let serial = 0;
  let audioFingerprint = fp("5");
  let rejectCreate = false;
  let beforeRequest: (
    wire: Record<string, unknown>,
  ) => void | Promise<void> = () => undefined;
  let renew: (init: RequestInit) => void | Promise<void> = () => undefined;
  const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
    const wire = JSON.parse(String(init.body)) as Record<string, unknown>;
    requests.push(wire);
    await beforeRequest(wire);
    const response = (body: object, cap = false) => {
      const text = JSON.stringify(body);
      return new Response(text, {
        headers: {
          "content-type": "application/json",
          "content-length": String(new TextEncoder().encode(text).length),
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
          ...(cap
            ? { [AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER]: "a".repeat(64) }
            : {}),
        },
      });
    };
    if (wire.operation === "create") {
      if (rejectCreate) throw new AuthoringMediaSourceLeaseError("stale", 409);
      const audio = wire.derivativeKind === "audio_preview";
      const asset = binding().request.asset;
      const receipt = {
        schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
        operation: "create",
        requestId: wire.requestId,
        leaseId: `lease-${++serial}`,
        revision: 1,
        ownerId: wire.ownerId,
        runtimeEpoch: wire.runtimeEpoch,
        ttlMs: 60_000,
        derivativeKind: wire.derivativeKind,
        mediaType: audio ? "audio/wav" : "video/mp4",
        byteCount: audio ? 44 : 16,
        derivativeFingerprint: audio ? audioFingerprint : fp("4"),
        assetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
        derivativeProfileId: "h3.authoring.media_derivatives.v6",
        profileFingerprint: wire.profileFingerprint,
        audioDisposition: "present_bound",
      };
      live.set(receipt.leaseId, receipt);
      return response(receipt, true);
    }
    const receipt = live.get(String(wire.leaseId))!;
    if (wire.operation === "open") {
      return new Response(new Uint8Array(Number(receipt.byteCount)), {
        headers: {
          "content-type": String(receipt.mediaType),
          "content-length": String(receipt.byteCount),
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
          [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: String(receipt.revision),
          [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: String(
            receipt.derivativeFingerprint,
          ),
        },
      });
    }
    if (wire.operation === "renew") {
      await renew(init);
      const next = {
        ...receipt,
        requestId: wire.requestId,
        operation: "renew",
        revision: Number(receipt.revision) + 1,
      };
      live.set(String(wire.leaseId), next);
      return response(next);
    }
    live.delete(String(wire.leaseId));
    return response({
      schema: "h3.context.authoring_media_lease.released.v1",
      requestId: wire.requestId,
      operation: "release",
      leaseId: wire.leaseId,
    });
  });
  const video = document.createElement("video");
  Object.defineProperty(video, "readyState", { value: 1 });
  vi.spyOn(video, "load").mockImplementation(() => undefined);
  vi.spyOn(video, "pause").mockImplementation(() => undefined);
  const createVideoElement = vi.fn(() => video);
  const digest = vi.fn(async (body: Uint8Array) =>
    body.length === 44 ? fp("5") : fp("4"),
  );
  const revokeObjectURL = vi.fn();
  const client = createAuthoringMediaSourceLeaseClient({
    fetchApi,
    digest,
    requestId: () => `request-${++serial}`,
    createVideoElement,
    createObjectURL: () => "blob:retained",
    revokeObjectURL,
  });
  return {
    client,
    requests,
    live,
    createVideoElement,
    digest,
    revokeObjectURL,
    video,
    audioChanges: () => {
      audioFingerprint = fp("6");
    },
    refuse: () => {
      rejectCreate = true;
    },
    setRenew: (operation: typeof renew) => {
      renew = operation;
    },
    setRequestHook: (operation: typeof beforeRequest) => {
      beforeRequest = operation;
    },
  };
}

function leaseRequest(): AuthoringMediaLeaseCreateRequest {
  const { request, context } = binding();
  return {
    schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
    operation: "create",
    requestId: "initial",
    workspaceHandle: context.snapshot.workspaceHandle,
    workspaceRevision: context.snapshot.workspaceRevision,
    timelineRevision: context.snapshot.timelineRevision,
    publicFingerprint: context.snapshot.publicFingerprint,
    manifestFingerprint: context.manifest.manifestFingerprint,
    profileFingerprint: context.manifest.profileFingerprint,
    scope: "clip",
    clipId: request.ownerId,
    assetId: request.asset.assetId,
    derivativeKind: "video_proxy",
    ownerId: request.ownerId,
    runtimeEpoch: request.epoch,
    sourceStartFrame: 0,
    sourceEndFrame: context.sourceEndFrame,
  };
}

afterEach(() => vi.useRealTimers());

describe("authoring authority adoption and in-place source rebind", () => {
  it("does not restart renewal when a second held open finishes after authority failure", async () => {
    vi.useFakeTimers();
    const a = authority();
    let finish!: (value: string) => void;
    a.digest.mockResolvedValueOnce(fp("4")).mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    );
    a.setRenew(() => {
      throw new AuthoringMediaSourceLeaseError("stale", 409);
    });
    const lease = await a.client.create(
      leaseRequest(),
      new AbortController().signal,
      canonicalPublicRuntimeAssetFingerprint(binding().request.asset),
    );
    const failure = vi.fn();
    lease.subscribeFailure(failure);
    const first = lease.open(new AbortController().signal);
    const second = lease.open(new AbortController().signal);
    try {
      await first;
      expect(finish).toBeTypeOf("function");
      await vi.advanceTimersByTimeAsync(30_000);
      expect(failure).toHaveBeenCalledOnce();
      finish(fp("4"));
      await second;
      expect(vi.getTimerCount()).toBe(0);
      await expect(
        lease.renew(new AbortController().signal),
      ).rejects.toMatchObject({ disposition: "lease_gone" });
    } finally {
      finish?.(fp("4"));
      await Promise.allSettled([first, second]);
      await lease.release();
    }
  });
  it("does not restart renewal when a held integrity check finishes after release", async () => {
    vi.useFakeTimers();
    const a = authority();
    let finish!: (value: string) => void;
    a.digest.mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    );
    const lease = await a.client.create(
      leaseRequest(),
      new AbortController().signal,
      canonicalPublicRuntimeAssetFingerprint(binding().request.asset),
    );
    const opening = lease.open(new AbortController().signal);
    for (let step = 0; step < 40; step++) await Promise.resolve();
    try {
      expect(finish).toBeTypeOf("function");
      await lease.release();
      finish(fp("4"));
      await opening;
      expect(lease.state().released).toBe(true);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      finish?.(fp("4"));
      await opening.catch(() => undefined);
      await lease.release();
    }
  });
  it("does not schedule automatic renewal for a receipt that has not opened or adopted a body", async () => {
    vi.useFakeTimers();
    const a = authority();
    const lease = await a.client.create(
      leaseRequest(),
      new AbortController().signal,
      canonicalPublicRuntimeAssetFingerprint(binding().request.asset),
    );
    await lease.renew(new AbortController().signal);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(a.requests.map((wire) => wire.operation)).toEqual([
      "create",
      "renew",
    ]);
    expect(lease.state().opened).toBe(false);
    await a.client.close();
  });
  it.each(["opened", "release-requested", "released", "failed"])(
    "refuses adoption after %s without restarting authority",
    async (state) => {
      vi.useFakeTimers();
      const a = authority();
      const lease = await a.client.create(
        leaseRequest(),
        new AbortController().signal,
        canonicalPublicRuntimeAssetFingerprint(binding().request.asset),
      );
      let release: Promise<void> | undefined;
      let finish!: () => void;
      if (state === "opened") lease.adopt!();
      if (state === "released") await lease.release();
      if (state === "release-requested") {
        a.setRequestHook(async (wire) => {
          if (wire.operation === "release")
            await new Promise<void>((resolve) => {
              finish = resolve;
            });
        });
        release = lease.release();
        for (let step = 0; step < 20; step++) await Promise.resolve();
      }
      if (state === "failed") {
        lease.adopt!();
        a.setRenew(async () => {
          throw new AuthoringMediaSourceLeaseError("stale", 409);
        });
        await vi.advanceTimersByTimeAsync(30_000);
      }
      const before = a.requests.length;
      expect(() => lease.adopt!()).toThrowError(AuthoringMediaSourceLeaseError);
      expect(a.requests).toHaveLength(before);
      if (release) {
        finish();
        await release;
      }
      await a.client.close();
      expect(vi.getTimerCount()).toBe(0);
    },
  );
  it("fences queued retired failures while a current adopted failure still revokes playback", async () => {
    const a = authority();
    const callbacks: (() => void)[] = [];
    const first = binding();
    const source = await acquireAuthoringVideoSource(
      {
        client: {
          create: async (...args) => {
            const lease = await a.client.create(...args);
            return {
              ...lease,
              subscribeFailure: (callback) => {
                callbacks.push(callback);
                return lease.subscribeFailure(callback);
              },
            };
          },
        },
        requestId: () => crypto.randomUUID(),
        createVideoElement: a.createVideoElement,
        createObjectURL: () => "blob:retained",
        revokeObjectURL: a.revokeObjectURL,
        waitForMetadata: async () => undefined,
        teardown: () => undefined,
      },
      first.request,
      first.context,
    );
    const error = vi.fn();
    a.video.addEventListener("error", error);
    const next = binding(1);
    expect(await source.rebind!(next.request, next.context)).toBe(true);
    callbacks[0]!();
    callbacks[1]!();
    expect(error).not.toHaveBeenCalled();
    callbacks[2]!();
    expect(error).toHaveBeenCalledTimes(1);
    expect(await source.rebind!(next.request, next.context)).toBe(false);
    await source.release();
    callbacks[3]!();
    expect(error).toHaveBeenCalledTimes(1);
    expect(a.live.size).toBe(0);
  });
  it("keeps the acquisition refusal primary when fresh authority cleanup also fails", async () => {
    const a = authority();
    const first = binding();
    const source = await a.client.acquireVideoSource(
      first.request,
      first.context,
    );
    let freshId: unknown;
    a.setRequestHook((wire) => {
      if (
        wire.operation === "create" &&
        wire.runtimeEpoch === 2 &&
        wire.derivativeKind === "audio_preview"
      )
        throw new AuthoringMediaSourceLeaseError("stale", 409);
      if (wire.operation === "release" && wire.leaseId === freshId)
        throw new AuthoringMediaSourceLeaseError("busy", 409);
      if (wire.operation === "create" && wire.runtimeEpoch === 2)
        freshId = `lease-${Number(String(wire.requestId).split("-")[1]) + 1}`;
    });
    const next = binding(1);
    await expect(
      source.rebind!(next.request, next.context),
    ).rejects.toMatchObject({ disposition: "stale" });
    expect(a.live.size).toBe(3);
    a.setRequestHook(() => undefined);
    await source.release();
    await a.client.close();
    expect(a.live.size).toBe(0);
  });

  it("cannot acquire another body or resurrect an owner released during reauthorization", async () => {
    const a = authority();
    const first = binding();
    const source = await a.client.acquireVideoSource(
      first.request,
      first.context,
    );
    let enter!: () => void;
    let finish!: () => void;
    const entered = new Promise<void>((resolve) => {
      enter = resolve;
    });
    const held = new Promise<void>((resolve) => {
      finish = resolve;
    });
    a.setRequestHook(async (wire) => {
      if (wire.operation === "create" && wire.runtimeEpoch === 2) {
        enter();
        await held;
      }
    });
    const next = binding(1);
    const pending = source.rebind!(next.request, next.context);
    await entered;
    await source.release();
    finish();
    expect(await pending).toBe(false);
    expect(
      a.requests.filter((wire) => wire.operation === "create"),
    ).toHaveLength(3);
    expect(a.live.size).toBe(0);
    expect(a.revokeObjectURL).toHaveBeenCalledTimes(1);
  });
  it("adopts admitted identity without downloading and starts renewal", async () => {
    vi.useFakeTimers();
    const a = authority();
    const lease = await a.client.create(
      leaseRequest(),
      new AbortController().signal,
      canonicalPublicRuntimeAssetFingerprint(binding().request.asset),
    );
    expect(typeof lease.derivative).toBe("function");
    expect(typeof lease.adopt).toBe("function");
    expect(lease.derivative!()).toEqual({
      fingerprint: fp("4"),
      byteCount: 16,
      kind: "video_proxy",
    });
    lease.adopt!();
    expect(lease.state().opened).toBe(true);
    expect(a.requests.map((r) => r.operation)).toEqual(["create"]);
    await expect(
      lease.open(new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "stale" });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(a.requests.map((r) => r.operation)).toEqual(["create", "renew"]);
    await lease.release();
    expect(() => lease.adopt!()).toThrow("stale");
  });

  it("reauthorizes both verified bodies without opening, digesting or replacing the element", async () => {
    const a = authority();
    const first = binding();
    const source = await a.client.acquireVideoSource(
      first.request,
      first.context,
    );
    const body = source.audioBody;
    const next = binding(1);
    expect(typeof source.rebind).toBe("function");
    expect(await source.rebind!(next.request, next.context)).toBe(true);
    expect(source.element).toBe(a.video);
    expect(source.audioBody).toBe(body);
    expect(a.digest).toHaveBeenCalledTimes(2);
    expect(a.createVideoElement).toHaveBeenCalledTimes(1);
    expect(a.revokeObjectURL).not.toHaveBeenCalled();
    expect(a.requests.filter((r) => r.operation === "open")).toHaveLength(2);
    expect(a.requests.filter((r) => r.operation === "create")).toEqual([
      expect.objectContaining({
        workspaceRevision: first.context.snapshot.workspaceRevision,
        derivativeKind: "video_proxy",
        runtimeEpoch: 1,
      }),
      expect.objectContaining({
        derivativeKind: "audio_preview",
        runtimeEpoch: 1,
      }),
      expect.objectContaining({
        workspaceRevision: next.context.snapshot.workspaceRevision,
        publicFingerprint: next.context.snapshot.publicFingerprint,
        derivativeKind: "video_proxy",
        runtimeEpoch: 2,
      }),
      expect.objectContaining({
        derivativeKind: "audio_preview",
        runtimeEpoch: 2,
      }),
    ]);
    expect(a.live.size).toBe(2);
    await source.release();
    expect(a.live.size).toBe(0);
    expect(a.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(await source.rebind!(next.request, next.context)).toBe(false);
  });

  it("refuses reuse when only the audio fingerprint changes and cleans fresh authorities", async () => {
    const a = authority();
    const first = binding();
    const source = await a.client.acquireVideoSource(
      first.request,
      first.context,
    );
    a.audioChanges();
    const next = binding(1);
    expect(typeof source.rebind).toBe("function");
    expect(await source.rebind!(next.request, next.context)).toBe(false);
    expect(a.live.size).toBe(2);
    expect(a.requests.filter((r) => r.operation === "open")).toHaveLength(2);
    await source.release();
    expect(a.live.size).toBe(0);
  });

  it("preserves a create refusal and never reuses cancelled or foreign owner requests", async () => {
    const a = authority();
    const first = binding();
    const source = await a.client.acquireVideoSource(
      first.request,
      first.context,
    );
    const next = binding(1);
    expect(typeof source.rebind).toBe("function");
    expect(
      await source.rebind!(
        { ...next.request, ownerId: "other-clip" },
        next.context,
      ),
    ).toBe(false);
    const cancelled = new AbortController();
    cancelled.abort();
    expect(
      await source.rebind!(
        { ...next.request, signal: cancelled.signal },
        next.context,
      ),
    ).toBe(false);
    expect(a.requests.filter((r) => r.operation === "create")).toHaveLength(2);
    a.refuse();
    await expect(
      source.rebind!(next.request, next.context),
    ).rejects.toMatchObject({ disposition: "stale" });
    await source.release();
    expect(a.live.size).toBe(0);
  });

  it("retries busy renewal every 250 ms without publishing an early failure", async () => {
    vi.useFakeTimers();
    const a = authority();
    let calls = 0;
    a.setRenew(() => {
      if (++calls < 3) throw new AuthoringMediaSourceLeaseError("busy", 409);
    });
    const first = binding();
    const source = await a.client.acquireVideoSource(
      first.request,
      first.context,
    );
    const error = vi.fn();
    a.video.addEventListener("error", error);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(error).not.toHaveBeenCalled();
    expect(calls).toBe(2);
    await vi.advanceTimersByTimeAsync(249);
    expect(calls).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(calls).toBe(4);
    expect(error).not.toHaveBeenCalled();
    await source.release();
  });

  it("keeps the original renewal deadline despite busy replies and fails other refusals at once", async () => {
    vi.useFakeTimers();
    const a = authority();
    a.setRenew(() => {
      throw new AuthoringMediaSourceLeaseError("busy", 409);
    });
    const first = binding();
    const source = await a.client.acquireVideoSource(
      first.request,
      first.context,
    );
    const error = vi.fn();
    a.video.addEventListener("error", error);
    await vi.advanceTimersByTimeAsync(59_999);
    expect(error).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(error).toHaveBeenCalled();
    const attempts = a.requests.length;
    await vi.advanceTimersByTimeAsync(1_000);
    expect(a.requests).toHaveLength(attempts);
    await source.release();
    const b = authority();
    b.setRenew(() => {
      throw new AuthoringMediaSourceLeaseError("stale", 409);
    });
    const owned = await b.client.acquireVideoSource(
      first.request,
      first.context,
    );
    const refused = vi.fn();
    b.video.addEventListener("error", refused);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(refused).toHaveBeenCalled();
    await owned.release();
  });
});
