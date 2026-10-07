import { describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
  type PublicCompositionSnapshot,
} from "../src/contracts/compositionCodec";
import type { AuthoringMediaLeaseCreateRequest } from "../src/contracts/authoringMediaLeaseCodec";
import type { AuthoringMediaVideoSourceContext } from "../src/runtime/authoringDecorationLeaseRequest";
import type { MediaTransportOpen } from "../src/runtime/editorRuntime";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import {
  type AuthoringMediaLeaseBody,
  type AuthoringMediaSourceLease,
  type AuthoringMediaSourceLeaseClient,
} from "../src/host/authoringMediaSourceLease";
import { AuthoringMediaSourceLeaseError } from "../src/host/authoringMediaLeaseTransport";
import { acquireAuthoringVideoSource } from "../src/host/authoringVideoSourceOwner";

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;
const settle = async () => {
  for (let step = 0; step < 12; step++) await Promise.resolve();
};
function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function inputs(
  snapshot = decodePublicCompositionSnapshot(structuredClone(fixture.snapshot)),
) {
  const manifest = buildPublicAssetManifest(snapshot);
  const asset = manifest.assets.find(
    (candidate) => candidate.assetId === "vid-primary",
  )!;
  const request: MediaTransportOpen = {
    asset,
    ownerId: "clip-main",
    epoch: 1,
    signal: new AbortController().signal,
  };
  const context: AuthoringMediaVideoSourceContext = {
    snapshot,
    manifest,
    clipId: "clip-main",
    sourceStartFrame: 0,
    sourceEndFrame: asset.sourceFrameCount!,
  };
  return { request, context };
}

function leaseDouble(request: AuthoringMediaLeaseCreateRequest, index: number) {
  const body: AuthoringMediaLeaseBody = {
    blob: new Blob([new Uint8Array(8)], {
      type:
        request.derivativeKind === "video_proxy" ? "video/mp4" : "audio/wav",
    }),
    receipt: {
      derivativeFingerprint: fingerprint(
        request.derivativeKind === "video_proxy" ? "4" : "5",
      ),
      derivativeKind: request.derivativeKind,
    } as AuthoringMediaLeaseBody["receipt"],
    geometry: null,
  };
  const open = vi.fn<AuthoringMediaSourceLease["open"]>(async () => body);
  const release = vi.fn<AuthoringMediaSourceLease["release"]>(
    async () => undefined,
  );
  const adopt = vi.fn();
  const derivative = vi.fn(() => ({
    fingerprint: body.receipt.derivativeFingerprint,
    byteCount: body.blob.size,
    kind: body.receipt.derivativeKind,
  }));
  const queued: (() => void)[] = [];
  const unsubscribe = vi.fn();
  const lease: AuthoringMediaSourceLease = {
    state: () => ({
      leaseId: `lease-${index}`,
      revision: 1,
      ownerId: request.ownerId,
      runtimeEpoch: request.runtimeEpoch,
      opened: false,
      released: false,
    }),
    open,
    release,
    adopt,
    derivative,
    renew: async () => undefined,
    transfer: async () => undefined,
    subscribeFailure(listener) {
      queued.push(listener);
      return unsubscribe;
    },
  };
  return {
    lease,
    body,
    open,
    release,
    adopt,
    derivative,
    unsubscribe,
    fail: () => queued.forEach((listener) => listener()),
  };
}

function harness() {
  const leases: ReturnType<typeof leaseDouble>[] = [];
  let configure = (
    _lease: ReturnType<typeof leaseDouble>,
    _index: number,
  ) => {};
  const create = vi.fn<AuthoringMediaSourceLeaseClient["create"]>(
    async (request) => {
      const lease = leaseDouble(
        request as AuthoringMediaLeaseCreateRequest,
        leases.length,
      );
      configure(lease, leases.length);
      leases.push(lease);
      // This collaborator admits every descriptor. Admission and retention checks belong to the owner.
      return lease.lease;
    },
  );
  const createVideoElement = vi.fn(() => document.createElement("video"));
  const createObjectURL = vi.fn(() => "blob:controlled-video");
  const revokeObjectURL = vi.fn();
  const waitForMetadata = vi.fn(
    async (_element: HTMLVideoElement, _signal: AbortSignal) => undefined,
  );
  const teardown = vi.fn();
  let serial = 0;
  const dependencies = {
    client: { create },
    requestId: () => `owner-request-${++serial}`,
    createVideoElement,
    createObjectURL,
    revokeObjectURL,
    waitForMetadata,
    teardown,
  };
  return {
    dependencies,
    create,
    leases,
    createVideoElement,
    createObjectURL,
    revokeObjectURL,
    waitForMetadata,
    teardown,
    configure(value: typeof configure) {
      configure = value;
    },
    acquire(value = inputs()) {
      return acquireAuthoringVideoSource(
        dependencies,
        value.request,
        value.context,
      );
    },
  };
}

describe("video owner checks through a permissive lease collaborator", () => {
  it("retires old subscriptions once when a fresh subscription throws", async () => {
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    const error = new Error("controlled fresh subscription failure");
    h.configure((fresh, index) => {
      if (index === 2)
        vi.spyOn(fresh.lease, "subscribeFailure").mockImplementation(() => {
          throw error;
        });
    });
    try {
      await expect(
        owner.rebind!({ ...value.request, epoch: 2 }, value.context),
      ).rejects.toBe(error);
      await owner.release();
      expect(h.leases[0]!.unsubscribe).toHaveBeenCalledOnce();
      expect(h.leases[1]!.unsubscribe).toHaveBeenCalledOnce();
      expect(h.revokeObjectURL).toHaveBeenCalledOnce();
      for (const lease of h.leases)
        expect(lease.release).toHaveBeenCalledOnce();
    } finally {
      await owner.release();
    }
  });
  it("refuses a missing fresh audio authority without losing the old playable binding", async () => {
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    const acquire = h.create.getMockImplementation()!;
    h.create.mockImplementation(async (...args) => {
      if (h.leases.length === 3)
        return undefined as unknown as AuthoringMediaSourceLease;
      return acquire(...args);
    });
    try {
      await expect(
        owner.rebind!({ ...value.request, epoch: 2 }, value.context),
      ).resolves.toBe(false);
      expect(h.leases[2]!.release).toHaveBeenCalledOnce();
      expect(h.leases[0]!.release).not.toHaveBeenCalled();
      expect(h.leases[1]!.release).not.toHaveBeenCalled();
      expect(h.teardown).not.toHaveBeenCalled();
    } finally {
      await owner.release();
    }
  });
  it("restores old failure reporting after a fresh authority is refused", async () => {
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    const error = vi.fn();
    owner.element.addEventListener("error", error);
    h.configure((lease, index) => {
      if (index === 2)
        lease.derivative.mockReturnValue({
          fingerprint: fingerprint("9"),
          byteCount: 8,
          kind: "video_proxy",
        });
    });
    try {
      await expect(
        owner.rebind!({ ...value.request, epoch: 2 }, value.context),
      ).resolves.toBe(false);
      expect(error).not.toHaveBeenCalled();
      h.leases[0]!.fail();
      expect(error).toHaveBeenCalledOnce();
      const creates = h.create.mock.calls.length;
      await expect(
        owner.rebind!({ ...value.request, epoch: 3 }, value.context),
      ).resolves.toBe(false);
      expect(h.create).toHaveBeenCalledTimes(creates);
    } finally {
      await owner.release();
    }
  });
  it("observes a fresh authority failure while successful retirement is still held", async () => {
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    const gate = deferred<void>();
    h.leases[0]!.release.mockImplementationOnce(() => gate.promise);
    const error = vi.fn();
    owner.element.addEventListener("error", error);
    const rebinding = owner.rebind!(
      { ...value.request, epoch: 2 },
      value.context,
    );
    await vi.waitFor(() => expect(h.leases[0]!.release).toHaveBeenCalledOnce());
    h.leases[2]!.fail();
    expect(error).toHaveBeenCalledOnce();
    gate.resolve();
    await expect(rebinding).resolves.toBe(false);
    await owner.release();
  });
  it("reports both refused authorities and retries them without repeating local revocation", async () => {
    const h = harness();
    const owner = await h.acquire();
    h.leases[0]!.release.mockRejectedValueOnce(
      new Error("first authority refusal"),
    );
    h.leases[1]!.release.mockRejectedValueOnce(
      new Error("second authority refusal"),
    );
    await expect(owner.release()).rejects.toBeInstanceOf(AggregateError);
    expect(h.revokeObjectURL).toHaveBeenCalledOnce();
    await owner.release();
    for (const lease of h.leases)
      expect(lease.release).toHaveBeenCalledTimes(2);
    await owner.release();
    for (const lease of h.leases)
      expect(lease.release).toHaveBeenCalledTimes(2);
    expect(h.revokeObjectURL).toHaveBeenCalledOnce();
  });

  it("reports cancellation when metadata fails after abort", async () => {
    const h = harness();
    const value = inputs();
    const abort = new AbortController();
    h.waitForMetadata.mockImplementationOnce(async () => {
      abort.abort();
      throw new Error("controlled metadata failure");
    });
    await expect(
      h.acquire({
        ...value,
        request: { ...value.request, signal: abort.signal },
      }),
    ).rejects.toMatchObject({ disposition: "cancelled", status: 499 });
    for (const lease of h.leases) expect(lease.release).toHaveBeenCalledOnce();
  });
  const refusals: [
    string,
    (value: ReturnType<typeof inputs>) => ReturnType<typeof inputs>,
  ][] = [
    [
      "missing clip",
      (value) => ({
        ...value,
        context: { ...value.context, clipId: "missing" },
      }),
    ],
    [
      "different owner",
      (value) => ({
        ...value,
        request: { ...value.request, ownerId: "foreign" },
      }),
    ],
    [
      "different clip asset",
      (value) => ({
        ...value,
        request: { ...value.request, asset: value.context.manifest.assets[1]! },
      }),
    ],
    [
      "missing asset",
      (value) => {
        const snapshot = {
          ...value.context.snapshot,
          assets: value.context.snapshot.assets.filter(
            (asset) => asset.assetId !== "vid-primary",
          ),
        };
        return {
          ...value,
          context: {
            ...value.context,
            snapshot,
            manifest: buildPublicAssetManifest(snapshot),
          },
        };
      },
    ],
    [
      "substituted asset fingerprint",
      (value) => ({
        ...value,
        request: {
          ...value.request,
          asset: { ...value.request.asset, sourceFrameCount: 73 },
        },
      }),
    ],
    [
      "non-video asset",
      (value) => {
        const snapshot: PublicCompositionSnapshot = {
          ...value.context.snapshot,
          assets: value.context.snapshot.assets.map((asset) =>
            asset.assetId === "vid-primary"
              ? { ...asset, kind: "image" }
              : asset,
          ),
        };
        return inputs(snapshot);
      },
    ],
    [
      "unknown frame count",
      (value) => {
        const snapshot = {
          ...value.context.snapshot,
          assets: value.context.snapshot.assets.map((asset) =>
            asset.assetId === "vid-primary"
              ? { ...asset, sourceFrameCount: null }
              : asset,
          ),
        };
        return inputs(snapshot);
      },
    ],
    [
      "partial source start",
      (value) => ({
        ...value,
        context: { ...value.context, sourceStartFrame: 1 },
      }),
    ],
    [
      "partial source end",
      (value) => ({
        ...value,
        context: { ...value.context, sourceEndFrame: 71 },
      }),
    ],
  ];
  it.each(refusals)(
    "refuses %s before any lease I/O",
    async (_label, change) => {
      const h = harness();
      await expect(h.acquire(change(inputs()))).rejects.toMatchObject({
        disposition: "contract_mismatch",
      });
      expect(h.create).not.toHaveBeenCalled();
      expect(h.createVideoElement).not.toHaveBeenCalled();
    },
  );

  it("validates the supplied manifest before I/O", async () => {
    const h = harness();
    const value = inputs();
    await expect(
      h.acquire({
        ...value,
        context: {
          ...value.context,
          manifest: {
            ...value.context.manifest,
            publicFingerprint: fingerprint("9"),
          },
        },
      }),
    ).rejects.toThrow("contract_mismatch");
    expect(h.create).not.toHaveBeenCalled();
  });

  it.each([
    "absent audio",
    "disabled clip",
    "overlay track",
    "disabled track",
    "missing track",
  ])("does not acquire PCM for %s", async (condition) => {
    const h = harness();
    const base = inputs().context.snapshot;
    const snapshot: PublicCompositionSnapshot = {
      ...base,
      assets: base.assets.map((asset) =>
        condition === "absent audio" && asset.assetId === "vid-primary"
          ? { ...asset, embeddedAudio: "absent" }
          : asset,
      ),
      clips: base.clips.map((clip) =>
        condition === "disabled clip" && clip.clipId === "clip-main"
          ? { ...clip, enabled: false }
          : clip,
      ),
      tracks: base.tracks
        .filter(
          (track) =>
            !(
              condition === "missing track" && track.trackId === "track-primary"
            ),
        )
        .map((track) =>
          track.trackId !== "track-primary"
            ? track
            : condition === "overlay track"
              ? { ...track, kind: "video_overlay" }
              : condition === "disabled track"
                ? { ...track, enabled: false }
                : track,
        ),
    };
    const owner = await h.acquire(inputs(snapshot));
    expect(h.create).toHaveBeenCalledOnce();
    expect(owner.audioBody).toBeUndefined();
    await owner.release();
  });

  it("keeps decoded video and PCM while separately adopting matching fresh authorities", async () => {
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    expect(h.create).toHaveBeenCalledTimes(2);
    const element = owner.element;
    const pcm = owner.audioBody;
    await expect(
      owner.rebind!({ ...value.request, epoch: 2 }, value.context),
    ).resolves.toBe(true);
    expect(owner.element).toBe(element);
    expect(owner.audioBody).toBe(pcm);
    expect(h.createObjectURL).toHaveBeenCalledOnce();
    expect(h.waitForMetadata).toHaveBeenCalledOnce();
    for (const lease of h.leases.slice(2)) {
      expect(lease.adopt).toHaveBeenCalledOnce();
      expect(lease.open).not.toHaveBeenCalled();
    }
    for (const lease of h.leases.slice(0, 2)) {
      expect(lease.release).toHaveBeenCalledOnce();
      expect(lease.unsubscribe).toHaveBeenCalledOnce();
    }
    await owner.release();
    expect(h.revokeObjectURL).toHaveBeenCalledOnce();
    for (const lease of h.leases) expect(lease.release).toHaveBeenCalledOnce();
    for (const lease of h.leases.slice(2))
      expect(lease.unsubscribe).toHaveBeenCalledOnce();
  });

  it("follows an edit of the clip's own audio by reauthorizing what it holds, opening nothing", async () => {
    // A clip's gain, mute and fades live on the clip wire, which no derivative holds. The edit
    // moves the public fingerprint, so each held lease is reauthorized once -- as for any accepted
    // edit -- and adopted on the bytes already decoded: no open, no element, no metadata wait.
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    const element = owner.element;
    const pcm = owner.audioBody;
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 1;
    (wire.clips as Array<Record<string, unknown>>).find(
      (clip) => clip.clip_id === "clip-main",
    )!.audio = {
      gain_mb: -600,
      muted: true,
      fade_in_frames: 12,
      fade_out_frames: 12,
    };
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const edited = inputs(decodePublicCompositionSnapshot(wire));
    await expect(
      owner.rebind!({ ...edited.request, epoch: 2 }, edited.context),
    ).resolves.toBe(true);
    expect(h.create).toHaveBeenCalledTimes(4);
    expect(
      h.create.mock.calls
        .slice(2)
        .map(
          ([request]) =>
            (request as AuthoringMediaLeaseCreateRequest).derivativeKind,
        ),
    ).toEqual(["video_proxy", "audio_preview"]);
    for (const lease of h.leases.slice(2)) {
      expect(lease.adopt).toHaveBeenCalledOnce();
      expect(lease.open).not.toHaveBeenCalled();
    }
    expect(owner.element).toBe(element);
    expect(owner.audioBody).toBe(pcm);
    expect(h.createVideoElement).toHaveBeenCalledOnce();
    expect(h.createObjectURL).toHaveBeenCalledOnce();
    expect(h.waitForMetadata).toHaveBeenCalledOnce();
    await owner.release();
  });

  it("refuses another valid video asset independently of its kind and full range", async () => {
    const h = harness();
    const value = inputs();
    const other = value.context.manifest.assets.find(
      (asset) => asset.assetId === "vid-overlay",
    )!;
    await expect(
      h.acquire({
        request: { ...value.request, asset: other },
        context: { ...value.context, sourceEndFrame: other.sourceFrameCount! },
      }),
    ).rejects.toMatchObject({ disposition: "contract_mismatch" });
    expect(h.create).not.toHaveBeenCalled();
  });

  it.each([
    "fingerprint",
    "byteCount",
    "kind",
    "missing derivative",
    "missing adopt",
  ])("refuses a fresh %s independently for video and audio", async (field) => {
    for (const freshIndex of [2, 3]) {
      const h = harness();
      const value = inputs();
      const owner = await h.acquire(value);
      h.configure((lease, index) => {
        if (index !== freshIndex) return;
        if (field === "missing derivative")
          (lease.lease as { derivative?: unknown }).derivative = undefined;
        else if (field === "missing adopt")
          (lease.lease as { adopt?: unknown }).adopt = undefined;
        else
          lease.derivative.mockReturnValue({
            fingerprint:
              field === "fingerprint"
                ? fingerprint("9")
                : lease.body.receipt.derivativeFingerprint,
            byteCount: field === "byteCount" ? 9 : 8,
            kind:
              field === "kind"
                ? "thumbnail"
                : lease.body.receipt.derivativeKind,
          });
      });
      await expect(
        owner.rebind!({ ...value.request, epoch: 2 }, value.context),
      ).resolves.toBe(false);
      for (const lease of h.leases.slice(2)) {
        expect(lease.adopt).not.toHaveBeenCalled();
        expect(lease.release).toHaveBeenCalledOnce();
      }
      for (const lease of h.leases.slice(0, 2))
        expect(lease.release).not.toHaveBeenCalled();
      await owner.release();
    }
  });

  it.each([0, 1])(
    "reports the current %i authority's failure, but ignores suspended, retired and revoked callbacks",
    async (currentIndex) => {
      const h = harness();
      const value = inputs();
      const owner = await h.acquire(value);
      const errors = vi.fn();
      owner.element.addEventListener("error", errors);
      const gate = deferred();
      h.create.mockImplementationOnce(async (request) => {
        await gate.promise;
        const fresh = leaseDouble(
          request as AuthoringMediaLeaseCreateRequest,
          2,
        );
        h.leases.push(fresh);
        return fresh.lease;
      });
      const replacing = owner.rebind!(
        { ...value.request, epoch: 2 },
        value.context,
      );
      await settle();
      h.leases[currentIndex]!.fail();
      expect(errors).not.toHaveBeenCalled();
      gate.resolve();
      await expect(replacing).resolves.toBe(true);
      h.leases[currentIndex]!.fail();
      expect(errors).not.toHaveBeenCalled();
      h.leases[currentIndex + 2]!.fail();
      expect(errors).toHaveBeenCalledOnce();
      await owner.release();
      h.leases[currentIndex + 2]!.fail();
      expect(errors).toHaveBeenCalledOnce();
    },
  );

  it("joins metadata before publishing and tears down every acquired resource on metadata failure", async () => {
    const h = harness();
    const gate = deferred();
    h.waitForMetadata.mockImplementationOnce(async () => {
      await gate.promise;
      return undefined;
    });
    let published = false;
    const acquiring = h.acquire().then((owner) => {
      published = true;
      return owner;
    });
    await settle();
    expect(h.waitForMetadata).toHaveBeenCalledOnce();
    expect(published).toBe(false);
    gate.reject(new Error("controlled metadata failure"));
    await expect(acquiring).rejects.toMatchObject({
      disposition: "internal_failure",
    });
    expect(h.teardown).toHaveBeenCalledOnce();
    expect(h.revokeObjectURL).toHaveBeenCalledOnce();
    for (const lease of h.leases) expect(lease.release).toHaveBeenCalledOnce();
  });

  it("waits for retired release and retains failed authority for a later owner release", async () => {
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    const gate = deferred();
    h.leases[0]!.release.mockImplementationOnce(() => gate.promise);
    let settled = false;
    const replacement = owner.rebind!(
      { ...value.request, epoch: 2 },
      value.context,
    ).finally(() => {
      settled = true;
    });
    const observed = replacement.catch((error: unknown) => error);
    await settle();
    expect(h.leases[0]!.release).toHaveBeenCalledOnce();
    expect(settled).toBe(false);
    const failure = new Error("controlled retired release failure");
    gate.reject(failure);
    expect(await observed).toBe(failure);
    await owner.release();
    expect(h.leases[0]!.release).toHaveBeenCalledTimes(2);
  });

  it("joins refusal cleanup without changing the held decoder", async () => {
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    const gate = deferred();
    h.configure((lease, index) => {
      if (index !== 2) return;
      lease.derivative.mockReturnValue({
        fingerprint: fingerprint("9"),
        byteCount: 8,
        kind: "video_proxy",
      });
      lease.release.mockImplementationOnce(() => gate.promise);
    });
    let settled = false;
    const refusing = owner.rebind!(
      { ...value.request, epoch: 2 },
      value.context,
    ).then((value) => {
      settled = true;
      return value;
    });
    await settle();
    expect(h.leases[2]!.release).toHaveBeenCalledOnce();
    expect(settled).toBe(false);
    expect(h.revokeObjectURL).not.toHaveBeenCalled();
    gate.resolve();
    await expect(refusing).resolves.toBe(false);
    await owner.release();
  });

  it.each([
    "revoked",
    "failed",
    "aborted",
    "owner",
    "context",
    "workspace",
    "profile",
    "asset",
    "audio requirement",
  ])(
    "refuses rebind for %s without issuing any fresh authority",
    async (condition) => {
      const h = harness();
      const value = inputs();
      const owner = await h.acquire(value);
      let request = { ...value.request, epoch: 2 };
      let context: AuthoringMediaVideoSourceContext | undefined = value.context;
      if (condition === "revoked") await owner.release();
      if (condition === "failed") h.leases[0]!.fail();
      if (condition === "aborted")
        request = { ...request, signal: AbortSignal.abort() };
      if (condition === "owner") request = { ...request, ownerId: "foreign" };
      if (condition === "context") context = undefined;
      if (condition === "workspace")
        context = {
          ...value.context,
          snapshot: {
            ...value.context.snapshot,
            workspaceHandle: "foreign-workspace",
          },
        };
      if (condition === "profile")
        context = {
          ...value.context,
          manifest: {
            ...value.context.manifest,
            profileFingerprint: fingerprint(
              "9",
            ) as typeof value.context.manifest.profileFingerprint,
          },
        };
      if (condition === "asset") {
        const snapshot = {
          ...value.context.snapshot,
          assets: value.context.snapshot.assets.map((asset) =>
            asset.assetId === "vid-primary"
              ? { ...asset, sourceFrameCount: 73 }
              : asset,
          ),
        };
        const next = inputs(snapshot);
        request = { ...next.request, epoch: 2 };
        context = next.context;
      }
      if (condition === "audio requirement") {
        const snapshot = {
          ...value.context.snapshot,
          clips: value.context.snapshot.clips.map((clip) =>
            clip.clipId === "clip-main" ? { ...clip, enabled: false } : clip,
          ),
        };
        context = inputs(snapshot).context;
      }
      await expect(owner.rebind!(request, context)).resolves.toBe(false);
      expect(h.create).toHaveBeenCalledTimes(2);
      await owner.release();
    },
  );

  it("refuses an overlapping rebind and admits the next call after the first finishes", async () => {
    const h = harness();
    const value = inputs();
    const owner = await h.acquire(value);
    const gate = deferred();
    const make = h.create.getMockImplementation()!;
    h.create.mockImplementationOnce(async (...args) => {
      await gate.promise;
      return make(...args);
    });
    const replacing = owner.rebind!(
      { ...value.request, epoch: 2 },
      value.context,
    );
    await settle();
    await expect(
      owner.rebind!({ ...value.request, epoch: 3 }, value.context),
    ).resolves.toBe(false);
    expect(h.create).toHaveBeenCalledTimes(3);
    gate.resolve();
    await expect(replacing).resolves.toBe(true);
    await expect(
      owner.rebind!({ ...value.request, epoch: 4 }, value.context),
    ).resolves.toBe(true);
    await owner.release();
  });

  it.each(["abort video", "revoke video", "abort audio", "revoke audio"])(
    "fences %s arriving during fresh admission",
    async (condition) => {
      const h = harness();
      const value = inputs();
      const owner = await h.acquire(value);
      const signal = new AbortController();
      const gate = deferred();
      const make = h.create.getMockImplementation()!;
      if (condition.endsWith("audio")) h.create.mockImplementationOnce(make);
      h.create.mockImplementationOnce(async (...args) => {
        await gate.promise;
        return make(...args);
      });
      const replacing = owner.rebind!(
        { ...value.request, epoch: 2, signal: signal.signal },
        value.context,
      );
      await settle();
      if (condition.startsWith("abort")) signal.abort();
      else await owner.release();
      gate.resolve();
      await expect(replacing).resolves.toBe(false);
      expect(h.create).toHaveBeenCalledTimes(
        condition.endsWith("video") ? 3 : 4,
      );
      for (const lease of h.leases.slice(2))
        expect(lease.adopt).not.toHaveBeenCalled();
      await owner.release();
    },
  );

  it.each(["abort", "revoke", "active failure"])(
    "reports an unsuccessful final rebind when %s occurs while retired release is held",
    async (condition) => {
      const h = harness();
      const value = inputs();
      const owner = await h.acquire(value);
      const signal = new AbortController();
      const gate = deferred();
      h.leases[0]!.release.mockImplementationOnce(() => gate.promise);
      const replacing = owner.rebind!(
        { ...value.request, epoch: 2, signal: signal.signal },
        value.context,
      );
      await settle();
      expect(h.leases[0]!.release).toHaveBeenCalledOnce();
      if (condition === "abort") signal.abort();
      if (condition === "revoke") await owner.release();
      if (condition === "active failure") h.leases[2]!.fail();
      gate.resolve();
      await expect(replacing).resolves.toBe(false);
      await owner.release();
    },
  );

  it("rebinds a video-only owner without trying to match nonexistent PCM", async () => {
    const h = harness();
    const base = inputs().context.snapshot;
    const snapshot: PublicCompositionSnapshot = {
      ...base,
      assets: base.assets.map((asset) =>
        asset.assetId === "vid-primary"
          ? { ...asset, embeddedAudio: "absent" }
          : asset,
      ),
    };
    const value = inputs(snapshot);
    const owner = await h.acquire(value);
    await expect(
      owner.rebind!({ ...value.request, epoch: 2 }, value.context),
    ).resolves.toBe(true);
    expect(h.create).toHaveBeenCalledTimes(2);
    expect(owner.audioBody).toBeUndefined();
    await owner.release();
  });

  it("preserves primary acquisition failure and reports a refusal's cleanup failure", async () => {
    for (const primary of [false, true]) {
      const h = harness();
      const value = inputs();
      const owner = await h.acquire(value);
      const cleanup = new Error("controlled fresh cleanup failure");
      const acquisition = new AuthoringMediaSourceLeaseError("stale", 409);
      h.configure((lease, index) => {
        if (index !== 2) return;
        lease.derivative.mockReturnValue({
          fingerprint: fingerprint("9"),
          byteCount: 8,
          kind: "video_proxy",
        });
        lease.release.mockRejectedValueOnce(cleanup);
      });
      if (primary) {
        const make = h.create.getMockImplementation()!;
        h.create
          .mockImplementationOnce(make)
          .mockRejectedValueOnce(acquisition);
      }
      await expect(
        owner.rebind!({ ...value.request, epoch: 2 }, value.context),
      ).rejects.toBe(primary ? acquisition : cleanup);
      await owner.release();
      expect(h.leases[2]!.release).toHaveBeenCalledTimes(2);
    }
  });

  it("deduplicates pending release, revokes once and retries failed authorities", async () => {
    const h = harness();
    const owner = await h.acquire();
    const gate = deferred();
    h.leases[0]!.release.mockImplementationOnce(() => gate.promise);
    const one = owner.release();
    const two = owner.release();
    const results = Promise.allSettled([one, two]);
    await settle();
    expect(h.leases[0]!.release).toHaveBeenCalledOnce();
    expect(h.revokeObjectURL).toHaveBeenCalledOnce();
    gate.reject(new Error("controlled release failure"));
    expect((await results).map((result) => result.status)).toEqual([
      "rejected",
      "rejected",
    ]);
    await owner.release();
    const releaseCounts = h.leases.map(
      (lease) => lease.release.mock.calls.length,
    );
    await owner.release();
    expect(h.leases.map((lease) => lease.release.mock.calls.length)).toEqual(
      releaseCounts,
    );
    expect(h.revokeObjectURL).toHaveBeenCalledOnce();
  });
});
