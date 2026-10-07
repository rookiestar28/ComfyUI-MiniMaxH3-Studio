import { describe, expect, it, vi } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";

import {
  AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER,
  AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER,
  AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER,
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  AUTHORING_MEDIA_LEASE_REVISION_HEADER,
  AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
  NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
  canonicalPublicRuntimeAssetFingerprint,
  type AuthoringMediaLeaseCreateRequest,
  type NleAuthoringAssetLeaseCreateRequest,
} from "../src/contracts/authoringMediaLeaseCodec";
import {
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  type NleAuthoringStateV2,
} from "../src/contracts/authoringWorkbenchCodec";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
  AUTHORING_MEDIA_LEASE_ROUTE,
  AuthoringMediaSourceLeaseError,
  createAuthoringMediaSourceLeaseClient,
  type AuthoringMediaAssetDecoration,
} from "../src/host/authoringMediaSourceLease";
import { createNleLeaseScheduler } from "../src/host/nleLeaseScheduler";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import { buildNleAuthoringAssetManifest } from "../src/runtime/nleAuthoringAssetManifest";

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;
const capability = "a".repeat(64);
const bytes = new TextEncoder().encode("bounded-video-body");
const derivativeFingerprint = fingerprint("4");

function metadataReadyVideo(): HTMLVideoElement {
  const video = document.createElement("video");
  vi.spyOn(video, "load").mockImplementation(() => {
    queueMicrotask(() => video.dispatchEvent(new Event("loadedmetadata")));
  });
  return video;
}

function denseFilmstripSnapshot() {
  const wire = structuredClone(fixture.snapshot);
  const video = wire.assets.find(({ kind }) => kind === "video")!;
  video.source_frame_count = 4;
  video.landmarks = Array.from({ length: 4 }, (_, frameIndex) => ({
    frame_index: frameIndex,
    pts: frameIndex * 512,
    dts: frameIndex * 512,
    duration_ticks: 512,
  }));
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return decodePublicCompositionSnapshot(wire);
}

function videoSourceFixture() {
  const snapshot = decodePublicCompositionSnapshot(
    structuredClone(fixture.snapshot),
  );
  const manifest = buildPublicAssetManifest(snapshot);
  const asset = manifest.assets[1]!;
  const ownerId = snapshot.clips[1]!.clipId;
  return {
    snapshot,
    manifest,
    asset,
    ownerId,
    expectedAssetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
  };
}

function videoProxyResponse() {
  return new Response(bytes, {
    status: 200,
    headers: {
      "Content-Type": "video/mp4",
      "Content-Length": String(bytes.byteLength),
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
      [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
    },
  });
}

const createRequest: AuthoringMediaLeaseCreateRequest = {
  schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  operation: "create",
  requestId: "request-create",
  workspaceHandle: "workspace-1",
  workspaceRevision: 1,
  timelineRevision: 2,
  publicFingerprint: fingerprint("1"),
  manifestFingerprint: fingerprint("2"),
  profileFingerprint: fingerprint("3"),
  scope: "clip",
  clipId: "clip-1",
  assetId: "asset-1",
  derivativeKind: "video_proxy",
  ownerId: "clip-1",
  runtimeEpoch: 4,
  sourceStartFrame: 0,
  sourceEndFrame: 2,
};

const authoringState: NleAuthoringStateV2 = {
  schema: NLE_AUTHORING_SCHEMA,
  profileId: NLE_AUTHORING_PROFILE_ID,
  operationProfileId: NLE_OPERATION_PROFILE_ID_V2,
  projectId: "project-asset-lease",
  workspaceHandle: "workspace-asset-lease",
  workspaceRevision: 7,
  workspaceFingerprint: fingerprint("6"),
  timelineRevision: 11,
  timelineFingerprint: fingerprint("7"),
  authoringFingerprint: fingerprint("8"),
  editCapacityFrames: 3_600,
  contentEndExclusive: 0,
  assets: [
    {
      assetId: "asset-image",
      kind: "image",
      sourceTimeBase: null,
      sourceFrameCount: null,
      sourceSampleCount: null,
      embeddedAudio: "absent",
      timestampPolicy: "not_applicable",
      landmarks: [],
    },
  ],
  tracks: [],
  clips: [],
  audioExtension: Object.freeze({}),
  blockers: [],
};

function jsonResponse(value: object, cap: string | null = null, status = 200) {
  const body = JSON.stringify(value);
  const headers = new Headers({
    "Content-Type": "application/json",
    "Content-Length": String(new TextEncoder().encode(body).byteLength),
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  if (cap !== null) headers.set(AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER, cap);
  return new Response(body, { status, headers });
}

function success(operation: "create" | "renew" | "transfer") {
  return {
    schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
    requestId: `request-${operation}`,
    operation,
    leaseId: "lease-1",
    revision: operation === "create" ? 1 : 2,
    ownerId: operation === "transfer" ? "clip-2" : "clip-1",
    runtimeEpoch: operation === "transfer" ? 5 : 4,
    ttlMs: 60_000,
    derivativeKind: "video_proxy",
    mediaType: "video/mp4",
    byteCount: bytes.byteLength,
    derivativeFingerprint,
    assetFingerprint: fingerprint("5"),
    derivativeProfileId: "h3.authoring.media_derivatives.v6",
    profileFingerprint: fingerprint("3"),
    audioDisposition: "absent",
  };
}

describe("M25-57 V2 authoring asset lease", () => {
  it("sends a closed create request bound to the current authoring manifest", async () => {
    const manifest = buildNleAuthoringAssetManifest(authoringState);
    const asset = manifest.assets[0]!;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const request: NleAuthoringAssetLeaseCreateRequest = {
      schema: NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
      operation: "create",
      requestId: "nle-asset-create",
      authoringSchema: NLE_AUTHORING_SCHEMA,
      profileId: NLE_AUTHORING_PROFILE_ID,
      workspaceHandle: authoringState.workspaceHandle,
      workspaceRevision: authoringState.workspaceRevision,
      timelineRevision: authoringState.timelineRevision,
      authoringFingerprint: authoringState.authoringFingerprint,
      manifestFingerprint: manifest.manifestFingerprint,
      profileFingerprint: manifest.profileFingerprint,
      assetId: asset.assetId,
      derivativeKind: "thumbnail",
      ownerId: "nle-bin-thumbnail",
      runtimeEpoch: 9,
      sourceStartFrame: 0,
      sourceEndFrame: 1,
    };
    const requests: Record<string, unknown>[] = [];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const wire = JSON.parse(String(init.body)) as Record<string, unknown>;
      requests.push(wire);
      if (wire.operation === "create")
        return jsonResponse(
          {
            schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
            requestId: wire.requestId,
            operation: "create",
            leaseId: "nle-asset-lease",
            revision: 1,
            ownerId: wire.ownerId,
            runtimeEpoch: wire.runtimeEpoch,
            ttlMs: 60_000,
            derivativeKind: "thumbnail",
            mediaType: "image/png",
            byteCount: 16,
            derivativeFingerprint,
            assetFingerprint,
            derivativeProfileId: "h3.authoring.media_derivatives.v6",
            profileFingerprint: manifest.profileFingerprint,
            audioDisposition: "absent",
          },
          capability,
        );
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: wire.requestId,
        operation: "release",
        leaseId: "nle-asset-lease",
      });
    });
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      requestId: () => "nle-asset-release",
    });

    const lease = await client.create(
      request,
      new AbortController().signal,
      assetFingerprint,
    );
    await lease.release();

    expect(requests[0]).toEqual(request);
    expect(requests[0]).not.toHaveProperty("publicFingerprint");
    expect(requests[0]).not.toHaveProperty("scope");
    expect(requests[1]).toMatchObject({
      schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
      operation: "release",
      requestId: "nle-asset-release",
    });
  });
});

describe("M25-13 browser media source lease", () => {
  it.each([
    null,
    {
      schema: "h3.authoring.media_geometry.v1",
      sourceWidth: 1920,
      sourceHeight: 1080,
      derivativeWidth: 320,
      derivativeHeight: 180,
    },
  ])(
    "keeps authority and source geometry after one bounded open: %j",
    async (geometry) => {
      const requests: Array<{ route: string; init: RequestInit }> = [];
      const fetchApi = vi.fn(async (route: string, init: RequestInit) => {
        requests.push({ route, init });
        const body = JSON.parse(String(init.body)) as { operation: string };
        if (body.operation === "create")
          return jsonResponse(success("create"), capability);
        if (body.operation === "open")
          return new Response(bytes, {
            status: 200,
            headers: {
              "Content-Type": "video/mp4",
              "Content-Length": String(bytes.byteLength),
              "Cache-Control": "no-store",
              "X-Content-Type-Options": "nosniff",
              [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
              [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
              ...(geometry === null
                ? {}
                : {
                    [AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER]:
                      JSON.stringify(geometry),
                  }),
            },
          });
        if (body.operation === "renew") return jsonResponse(success("renew"));
        return jsonResponse({
          schema: "h3.context.authoring_media_lease.released.v1",
          requestId: "request-release",
          operation: "release",
          leaseId: "lease-1",
        });
      });
      const client = createAuthoringMediaSourceLeaseClient({
        fetchApi,
        digest: vi.fn(async () => derivativeFingerprint),
        requestId: (() => {
          const values = ["request-open", "request-renew", "request-release"];
          return () => values.shift()!;
        })(),
      });

      const lease = await client.create(
        createRequest,
        new AbortController().signal,
        fingerprint("5"),
      );
      const body = await lease.open(new AbortController().signal);
      expect(body.blob).toBeInstanceOf(Blob);
      expect(body.blob.type).toBe("video/mp4");
      expect(body.geometry).toEqual(geometry);
      expect(lease.state()).toMatchObject({
        revision: 1,
        ownerId: "clip-1",
        opened: true,
      });
      await expect(
        lease.open(new AbortController().signal),
      ).rejects.toMatchObject({
        disposition: "stale",
      });
      await lease.renew(new AbortController().signal);
      expect(lease.state().revision).toBe(2);
      await lease.release();
      await lease.release();
      expect(requests.map(({ route }) => route)).toEqual([
        AUTHORING_MEDIA_LEASE_ROUTE,
        AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
        AUTHORING_MEDIA_LEASE_ROUTE,
        AUTHORING_MEDIA_LEASE_ROUTE,
      ]);
      expect(
        (requests[1].init.headers as Record<string, string>)[
          AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER
        ],
      ).toBe(capability);
    },
  );

  it("revokes local ownership before transfer and rotates capability", async () => {
    const order: string[] = [];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body)) as { operation: string };
      order.push(body.operation);
      if (body.operation === "create")
        return jsonResponse(success("create"), capability);
      return jsonResponse(success("transfer"), "b".repeat(64));
    });
    const ids = ["request-transfer"];
    const lease = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
    }).create(createRequest, new AbortController().signal, fingerprint("5"));
    await lease.transfer(
      "clip-2",
      5,
      async () => {
        order.push("local-revoke");
      },
      new AbortController().signal,
    );
    expect(order).toEqual(["create", "local-revoke", "transfer"]);
    expect(lease.state()).toMatchObject({
      revision: 2,
      ownerId: "clip-2",
      runtimeEpoch: 5,
    });
  });

  it("builds release from the renewed revision when owner controls overlap", async () => {
    let finishRenew!: () => void;
    const renewing = new Promise<void>((resolve) => {
      finishRenew = resolve;
    });
    const requests: Array<Record<string, unknown>> = [];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      requests.push(request);
      if (request.operation === "create")
        return jsonResponse(success("create"), capability);
      if (request.operation === "renew") {
        await renewing;
        return jsonResponse(success("renew"));
      }
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "request-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const ids = ["request-renew", "request-release"];
    const lease = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
    }).create(createRequest, new AbortController().signal, fingerprint("5"));

    const renewal = lease.renew(new AbortController().signal);
    const release = lease.release();
    finishRenew();
    await renewal;
    await release;

    expect(requests.map((request) => request.operation)).toEqual([
      "create",
      "renew",
      "release",
    ]);
    expect(requests[2]).toMatchObject({ revision: 2 });
  });

  it("keeps only release delivery alive when navigation cancels ordinary fetches", async () => {
    const requests: Array<{
      operation: string;
      init: RequestInit;
    }> = [];
    let navigationStarted = false;
    let resolveRenewStarted!: () => void;
    const renewStarted = new Promise<void>((resolve) => {
      resolveRenewStarted = resolve;
    });
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as {
        operation: string;
        requestId: string;
      };
      requests.push({ operation: request.operation, init });
      if (request.operation === "create")
        return jsonResponse(success("create"), capability);
      if (request.operation === "renew") {
        resolveRenewStarted();
        await new Promise<never>((_resolve, reject) => {
          const signal = init.signal!;
          const abort = () =>
            reject(new DOMException("renew cancelled", "AbortError"));
          if (signal.aborted) abort();
          else signal.addEventListener("abort", abort, { once: true });
        });
      }
      if (navigationStarted && init.keepalive !== true)
        throw new DOMException("navigation cancelled fetch", "AbortError");
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: request.requestId,
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const ids = ["request-renew", "request-release", "request-release-cleanup"];
    const createSignal = new AbortController();
    const lease = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
    }).create(createRequest, createSignal.signal, fingerprint("5"));

    const renewAbort = new AbortController();
    const renewal = lease.renew(renewAbort.signal);
    await renewStarted;
    renewAbort.abort();
    await expect(renewal).rejects.toMatchObject({ disposition: "cancelled" });
    navigationStarted = true;
    const releaseSignal = new AbortController();
    try {
      const outcome = await lease.release(releaseSignal.signal).then(
        () => "released" as const,
        (error: unknown) =>
          error instanceof AuthoringMediaSourceLeaseError
            ? error.disposition
            : ("unexpected" as const),
      );
      expect(lease.state().released).toBe(outcome === "released");
      expect(outcome).toBe("released");
      expect(requests.map(({ operation }) => operation)).toEqual([
        "create",
        "renew",
        "release",
      ]);
      expect(requests[0]!.init.keepalive).not.toBe(true);
      expect(requests[0]!.init.signal).toBe(createSignal.signal);
      expect(requests[1]!.init.keepalive).not.toBe(true);
      expect(requests[1]!.init.signal).toBe(renewAbort.signal);
      expect(requests[2]!.init.keepalive).toBe(true);
      expect(requests[2]!.init.signal).toBe(releaseSignal.signal);
    } finally {
      navigationStarted = false;
      await lease.release().catch(() => undefined);
    }
  });

  it("cancels a held scheduled renewal before releasing its owner", async () => {
    let scheduledRenewal: (() => void) | undefined;
    let resolveRenewStarted!: () => void;
    const renewStarted = new Promise<void>((resolve) => {
      resolveRenewStarted = resolve;
    });
    let renewSignal: AbortSignal | undefined;
    const operations: string[] = [];
    const fetchApi = vi.fn(async (route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as { operation: string };
      operations.push(request.operation);
      if (request.operation === "create")
        return jsonResponse(success("create"), capability);
      if (request.operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "video/mp4",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
          },
        });
      if (request.operation === "renew") {
        renewSignal = init.signal ?? undefined;
        resolveRenewStarted();
        await new Promise<never>((_resolve, reject) => {
          renewSignal?.addEventListener(
            "abort",
            () => reject(new DOMException("renew cancelled", "AbortError")),
            { once: true },
          );
        });
      }
      expect(route).toBe(AUTHORING_MEDIA_LEASE_ROUTE);
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "request-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const ids = ["request-open", "request-renew", "request-release"];
    const lease = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      schedule: (callback) => {
        scheduledRenewal = callback;
        return "scheduled-renewal";
      },
      cancelScheduled: vi.fn(),
    }).create(createRequest, new AbortController().signal, fingerprint("5"));
    await lease.open(new AbortController().signal);

    scheduledRenewal?.();
    await renewStarted;
    const outcome = await Promise.race([
      lease.release(AbortSignal.timeout(250)).then(() => "released" as const),
      new Promise<"timed_out">((resolve) =>
        setTimeout(() => resolve("timed_out"), 500),
      ),
    ]);

    expect(outcome).toBe("released");
    expect(renewSignal?.aborted).toBe(true);
    expect(operations).toEqual(["create", "open", "renew", "release"]);
  });

  it("publishes authority failure when a scheduled renewal stalls through its receipt deadline", async () => {
    const scheduled: Array<
      Readonly<{ callback: () => void; delayMs: number }>
    > = [];
    let resolveRenewStarted!: () => void;
    const renewStarted = new Promise<void>((resolve) => {
      resolveRenewStarted = resolve;
    });
    const operations: string[] = [];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      operations.push(String(request.operation));
      if (request.operation === "create")
        return jsonResponse(success("create"), capability);
      if (request.operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "video/mp4",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
          },
        });
      if (request.operation === "renew") {
        resolveRenewStarted();
        // Deliberately ignore abort to prove the client deadline is locally authoritative.
        return await new Promise<Response>(() => undefined);
      }
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: request.requestId,
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const ids = ["request-open", "request-renew", "request-release"];
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      schedule: (callback, delayMs) => {
        scheduled.push({ callback, delayMs });
        return callback;
      },
      cancelScheduled: vi.fn(),
    });
    const lease = await client.create(
      createRequest,
      new AbortController().signal,
      fingerprint("5"),
    );
    await lease.open(new AbortController().signal);
    const failure = vi.fn();
    lease.subscribeFailure(failure);

    expect(scheduled.map(({ delayMs }) => delayMs)).toEqual([30_000]);
    scheduled.shift()!.callback();
    await renewStarted;
    expect(scheduled.map(({ delayMs }) => delayMs)).toEqual([30_000]);
    scheduled.shift()!.callback();
    await vi.waitFor(() => expect(failure).toHaveBeenCalledTimes(1));
    await expect(
      lease.renew(new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "lease_gone" });
    await lease.release(AbortSignal.timeout(250));
    expect(operations).toEqual(["create", "open", "renew", "release"]);
  });

  it("forgets a locally stale handle when release confirms lease_gone", async () => {
    const operations: string[] = [];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      operations.push(String(request.operation));
      if (request.operation === "create")
        return jsonResponse(success("create"), capability);
      return jsonResponse(
        {
          schema: "h3.context.authoring_media_lease.error.v1",
          requestId: "request-release",
          reason: "lease_gone",
        },
        null,
        410,
      );
    });
    const lease = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => "request-release",
    }).create(createRequest, new AbortController().signal, fingerprint("5"));

    await lease.release();
    expect(lease.state().released).toBe(true);
    expect(operations).toEqual(["create", "release"]);
  });

  it("rejects mismatched body integrity and closes the lease", async () => {
    const operations: string[] = [];
    const ids = ["request-open", "request-release"];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const operation = (JSON.parse(String(init.body)) as { operation: string })
        .operation;
      operations.push(operation);
      if (operation === "create")
        return jsonResponse(success("create"), capability);
      if (operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "video/mp4",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
          },
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "request-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const lease = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => fingerprint("9"),
      requestId: () => ids.shift()!,
    }).create(createRequest, new AbortController().signal, fingerprint("5"));
    await expect(
      lease.open(new AbortController().signal),
    ).rejects.toBeInstanceOf(AuthoringMediaSourceLeaseError);
    expect(operations).toEqual(["create", "open", "release"]);
  });

  it("rejects an open response that arrives after abort and releases authority", async () => {
    let finishOpen!: () => void;
    const delayed = new Promise<void>((resolve) => {
      finishOpen = resolve;
    });
    const operations: string[] = [];
    const ids = ["request-open", "request-release"];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const operation = (JSON.parse(String(init.body)) as { operation: string })
        .operation;
      operations.push(operation);
      if (operation === "create")
        return jsonResponse(success("create"), capability);
      if (operation === "open") {
        await delayed;
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "video/mp4",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
          },
        });
      }
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "request-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const lease = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
    }).create(createRequest, new AbortController().signal, fingerprint("5"));
    const controller = new AbortController();
    const opening = lease.open(controller.signal);
    controller.abort();
    finishOpen();
    await expect(opening).rejects.toMatchObject({ disposition: "cancelled" });
    expect(operations).toEqual(["create", "open", "release"]);
    expect(lease.state().released).toBe(true);
  });

  it("binds a native video owner to the full source span and revokes locally first", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets[1]!;
    const ownerId = snapshot.clips[1]!.clipId;
    const order: string[] = [];
    const requests: Array<Record<string, unknown>> = [];
    const expectedAssetFingerprint =
      canonicalPublicRuntimeAssetFingerprint(asset);
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      requests.push(request);
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            ownerId,
            assetFingerprint: expectedAssetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "video/mp4",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
          },
        });
      order.push("server-release");
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "request-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const ids = ["request-create", "request-open", "request-release"];
    const video = metadataReadyVideo();
    const source = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createVideoElement: () => video,
      createObjectURL: () => "blob:leased-video",
      revokeObjectURL: () => order.push("local-revoke"),
    }).acquireVideoSource(
      {
        asset,
        ownerId,
        epoch: 4,
        signal: new AbortController().signal,
      },
      {
        snapshot,
        manifest,
        clipId: ownerId,
        sourceStartFrame: 0,
        sourceEndFrame: asset.sourceFrameCount!,
      },
    );
    expect(source.element).toBe(video);
    expect(video.src).toContain("blob:leased-video");
    expect(requests[0]).toMatchObject({
      operation: "create",
      ownerId,
      sourceStartFrame: 0,
      sourceEndFrame: asset.sourceFrameCount,
    });
    await source.release();
    expect(order).toEqual(["local-revoke", "server-release"]);
  });

  it("binds a primary present-audio owner to independent video and WAV authorities", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets[0]!;
    const ownerId = snapshot.clips[0]!.clipId;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const requests: Array<Record<string, unknown>> = [];
    const audioBytes = new Uint8Array(44);
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const wire = JSON.parse(String(init.body)) as Record<string, unknown>;
      requests.push(wire);
      if (wire.operation === "create") {
        const audio = wire.derivativeKind === "audio_preview";
        return jsonResponse(
          {
            schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
            requestId: wire.requestId,
            operation: "create",
            leaseId: audio ? "lease-audio" : "lease-video",
            revision: 1,
            ownerId,
            runtimeEpoch: 4,
            ttlMs: 60_000,
            derivativeKind: wire.derivativeKind,
            mediaType: audio ? "audio/wav" : "video/mp4",
            byteCount: audio ? audioBytes.byteLength : bytes.byteLength,
            derivativeFingerprint,
            assetFingerprint,
            derivativeProfileId: "h3.authoring.media_derivatives.v6",
            profileFingerprint: manifest.profileFingerprint,
            audioDisposition: "present_bound",
          },
          capability,
        );
      }
      if (wire.operation === "open") {
        const audio = wire.leaseId === "lease-audio";
        const body = audio ? audioBytes : bytes;
        return new Response(body, {
          status: 200,
          headers: {
            "Content-Type": audio ? "audio/wav" : "video/mp4",
            "Content-Length": String(body.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
          },
        });
      }
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: wire.requestId,
        operation: "release",
        leaseId: wire.leaseId,
      });
    });
    const ids = ["vc", "vo", "ac", "ao", "ar", "vr"];
    const source = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createVideoElement: metadataReadyVideo,
      createObjectURL: () => "blob:primary-video",
      revokeObjectURL: vi.fn(),
    }).acquireVideoSource(
      { asset, ownerId, epoch: 4, signal: new AbortController().signal },
      {
        snapshot,
        manifest,
        clipId: ownerId,
        sourceStartFrame: 0,
        sourceEndFrame: asset.sourceFrameCount!,
      },
    );

    expect(requests.filter((wire) => wire.operation === "create")).toEqual([
      expect.objectContaining({ derivativeKind: "video_proxy", ownerId }),
      expect.objectContaining({ derivativeKind: "audio_preview", ownerId }),
    ]);
    expect(source.audioBody).toBeInstanceOf(Blob);
    expect(source.audioBody?.type).toBe("audio/wav");
    await source.release();
    expect(
      requests
        .filter((wire) => wire.operation === "release")
        .map((wire) => wire.leaseId),
    ).toEqual(["lease-audio", "lease-video"]);
  });

  it("acquires and releases a bounded audio-only preview for adjacent handoff prewarming", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets[0]!;
    const ownerId = snapshot.clips[0]!.clipId;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const audioBytes = new Uint8Array(44);
    const requests: Array<Record<string, unknown>> = [];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const wire = JSON.parse(String(init.body)) as Record<string, unknown>;
      requests.push(wire);
      if (wire.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            requestId: "audio-create",
            leaseId: "lease-audio-only",
            ownerId,
            runtimeEpoch: 11,
            derivativeKind: "audio_preview",
            mediaType: "audio/wav",
            byteCount: audioBytes.byteLength,
            derivativeFingerprint,
            assetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
            audioDisposition: "present_bound",
          },
          capability,
        );
      if (wire.operation === "open")
        return new Response(audioBytes, {
          status: 200,
          headers: {
            "Content-Type": "audio/wav",
            "Content-Length": String(audioBytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
          },
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "audio-release",
        operation: "release",
        leaseId: "lease-audio-only",
      });
    });
    const ids = ["audio-create", "audio-open", "audio-release"];
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
    });

    const preview = await client.acquireAudioPreview!(
      { asset, ownerId, epoch: 11, signal: new AbortController().signal },
      {
        snapshot,
        manifest,
        clipId: ownerId,
        sourceStartFrame: 0,
        sourceEndFrame: asset.sourceFrameCount!,
      },
    );
    expect(preview.audioBody.type).toBe("audio/wav");
    expect(requests[0]).toMatchObject({
      operation: "create",
      scope: "clip",
      derivativeKind: "audio_preview",
      ownerId,
      sourceStartFrame: 0,
      sourceEndFrame: asset.sourceFrameCount,
    });
    await preview.release();
    expect(requests.map(({ operation }) => operation)).toEqual([
      "create",
      "open",
      "release",
    ]);
  });

  it("tears down a decoder and authority when metadata reaches its deadline", async () => {
    vi.useFakeTimers();
    try {
      const { snapshot, manifest, asset, ownerId, expectedAssetFingerprint } =
        videoSourceFixture();
      const order: string[] = [];
      const video = document.createElement("video");
      vi.spyOn(video, "pause").mockImplementation(() => {
        order.push("local-pause");
      });
      vi.spyOn(video, "load").mockImplementation(() => {
        order.push("local-load");
      });
      const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
        const request = JSON.parse(String(init.body)) as Record<
          string,
          unknown
        >;
        if (request.operation === "create")
          return jsonResponse(
            {
              ...success("create"),
              ownerId,
              assetFingerprint: expectedAssetFingerprint,
              profileFingerprint: manifest.profileFingerprint,
            },
            capability,
          );
        if (request.operation === "open") return videoProxyResponse();
        order.push("server-release");
        return jsonResponse({
          schema: "h3.context.authoring_media_lease.released.v1",
          requestId: request.requestId,
          operation: "release",
          leaseId: "lease-1",
        });
      });
      const ids = ["request-create", "request-open", "request-release"];
      const acquiring = createAuthoringMediaSourceLeaseClient({
        fetchApi,
        digest: async () => derivativeFingerprint,
        requestId: () => ids.shift()!,
        createVideoElement: () => video,
        createObjectURL: () => "blob:metadata-timeout",
        revokeObjectURL: () => order.push("local-revoke"),
      }).acquireVideoSource(
        {
          asset,
          ownerId,
          epoch: 4,
          signal: new AbortController().signal,
        },
        {
          snapshot,
          manifest,
          clipId: ownerId,
          sourceStartFrame: 0,
          sourceEndFrame: asset.sourceFrameCount!,
        },
      );

      const rejected = expect(acquiring).rejects.toMatchObject({
        disposition: "internal_failure",
      });
      await vi.advanceTimersByTimeAsync(3_000);
      await rejected;
      expect(video.hasAttribute("src")).toBe(false);
      expect(order).toEqual([
        "local-load",
        "local-pause",
        "local-load",
        "local-revoke",
        "server-release",
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("preserves cancellation and retries authority after a stalled metadata cleanup", async () => {
    const { snapshot, manifest, asset, ownerId, expectedAssetFingerprint } =
      videoSourceFixture();
    const order: string[] = [];
    const video = document.createElement("video");
    vi.spyOn(video, "pause").mockImplementation(() => {
      order.push("local-pause");
    });
    vi.spyOn(video, "load").mockImplementation(() => {
      order.push("local-load");
    });
    let releaseAttempts = 0;
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            ownerId,
            assetFingerprint: expectedAssetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open") return videoProxyResponse();
      releaseAttempts += 1;
      order.push(`server-release-${releaseAttempts}`);
      if (releaseAttempts === 1)
        return await new Promise<Response>(() => undefined);
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: request.requestId,
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const ids = [
      "request-create",
      "request-open",
      "request-release-1",
      "request-release-2",
    ];
    const controller = new AbortController();
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createVideoElement: () => video,
      createObjectURL: () => "blob:metadata-cancelled",
      revokeObjectURL: () => order.push("local-revoke"),
    });
    const acquiring = client.acquireVideoSource(
      { asset, ownerId, epoch: 4, signal: controller.signal },
      {
        snapshot,
        manifest,
        clipId: ownerId,
        sourceStartFrame: 0,
        sourceEndFrame: asset.sourceFrameCount!,
      },
    );
    await vi.waitFor(() => expect(video.load).toHaveBeenCalledTimes(1));
    controller.abort();

    await expect(acquiring).rejects.toMatchObject({ disposition: "cancelled" });
    expect(video.hasAttribute("src")).toBe(false);
    await client.close();
    expect(releaseAttempts).toBe(2);
    expect(order).toEqual([
      "local-load",
      "local-pause",
      "local-load",
      "local-revoke",
      "server-release-1",
      "server-release-2",
    ]);
  });

  it("tears down local video state before releasing a metadata media error", async () => {
    const { snapshot, manifest, asset, ownerId, expectedAssetFingerprint } =
      videoSourceFixture();
    const order: string[] = [];
    const video = document.createElement("video");
    vi.spyOn(video, "pause").mockImplementation(() => {
      order.push("local-pause");
    });
    vi.spyOn(video, "load").mockImplementation(() => {
      order.push("local-load");
      queueMicrotask(() => video.dispatchEvent(new Event("error")));
    });
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            ownerId,
            assetFingerprint: expectedAssetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open") return videoProxyResponse();
      order.push("server-release");
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: request.requestId,
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const ids = ["request-create", "request-open", "request-release"];
    const acquiring = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createVideoElement: () => video,
      createObjectURL: () => "blob:metadata-error",
      revokeObjectURL: () => order.push("local-revoke"),
    }).acquireVideoSource(
      {
        asset,
        ownerId,
        epoch: 4,
        signal: new AbortController().signal,
      },
      {
        snapshot,
        manifest,
        clipId: ownerId,
        sourceStartFrame: 0,
        sourceEndFrame: asset.sourceFrameCount!,
      },
    );

    await expect(acquiring).rejects.toMatchObject({
      disposition: "internal_failure",
    });
    expect(video.hasAttribute("src")).toBe(false);
    expect(order).toEqual([
      "local-load",
      "local-pause",
      "local-load",
      "local-revoke",
      "server-release",
    ]);
  });

  it("aborts a stalled video-owner release so its authority retry can run", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets[1]!;
    const ownerId = snapshot.clips[1]!.clipId;
    const expectedAssetFingerprint =
      canonicalPublicRuntimeAssetFingerprint(asset);
    const order: string[] = [];
    const releaseSignals: AbortSignal[] = [];
    let releaseAttempts = 0;
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            ownerId,
            assetFingerprint: expectedAssetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "video/mp4",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
          },
        });
      releaseAttempts += 1;
      order.push(`server-release-${releaseAttempts}`);
      const signal = init.signal!;
      releaseSignals.push(signal);
      if (releaseAttempts === 1)
        await new Promise<never>((_resolve, reject) => {
          const abort = () =>
            reject(new DOMException("release cancelled", "AbortError"));
          if (signal.aborted) abort();
          else signal.addEventListener("abort", abort, { once: true });
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: request.requestId,
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const ids = [
      "request-create",
      "request-open",
      "request-release-1",
      "request-release-2",
    ];
    const video = metadataReadyVideo();
    const source = await createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createVideoElement: () => video,
      createObjectURL: () => "blob:retryable-leased-video",
      revokeObjectURL: () => order.push("local-revoke"),
    }).acquireVideoSource(
      {
        asset,
        ownerId,
        epoch: 4,
        signal: new AbortController().signal,
      },
      {
        snapshot,
        manifest,
        clipId: ownerId,
        sourceStartFrame: 0,
        sourceEndFrame: asset.sourceFrameCount!,
      },
    );

    const firstOutcome = await Promise.race([
      source.release().then(
        () => "released" as const,
        (error: unknown) =>
          error instanceof AuthoringMediaSourceLeaseError
            ? error.disposition
            : ("unexpected" as const),
      ),
      new Promise<"still_pending">((resolve) =>
        setTimeout(() => resolve("still_pending"), 750),
      ),
    ]);
    expect(firstOutcome).toBe("cancelled");
    expect(releaseSignals[0]?.aborted).toBe(true);
    await expect(source.release()).resolves.toBeUndefined();
    expect(releaseAttempts).toBe(2);
    expect(order).toEqual([
      "local-revoke",
      "server-release-1",
      "server-release-2",
    ]);
  });

  it("decodes an asset-scoped thumbnail and releases it before publication", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets.find(({ kind }) => kind === "image")!;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const requests: Record<string, unknown>[] = [];
    const bitmap = { width: 32, height: 18, close: vi.fn() };
    const ids = ["asset-create", "asset-open", "asset-release"];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      requests.push(request);
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            requestId: "asset-create",
            ownerId: "decoration-1",
            runtimeEpoch: 9,
            derivativeKind: "thumbnail",
            mediaType: "image/png",
            assetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "image/png",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
            [AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER]: JSON.stringify({
              schema: "h3.authoring.media_geometry.v1",
              sourceWidth: 1920,
              sourceHeight: 1080,
              derivativeWidth: 32,
              derivativeHeight: 18,
            }),
          },
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "asset-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createImageBitmap: vi.fn(async () => bitmap as unknown as ImageBitmap),
    });
    const decoration = await client.acquireAssetDecoration(
      {
        snapshot,
        manifest,
        assetId: asset.assetId,
        ownerId: "decoration-1",
        runtimeEpoch: 9,
        derivativeKind: "thumbnail",
      },
      new AbortController().signal,
    );
    expect(requests[0]).toMatchObject({
      operation: "create",
      scope: "asset",
      clipId: null,
      assetId: asset.assetId,
      derivativeKind: "thumbnail",
      sourceStartFrame: 0,
      sourceEndFrame: 1,
    });
    if (decoration.value.derivativeKind !== "thumbnail")
      throw new Error("expected thumbnail decoration");
    expect(decoration.value.bitmap).toBe(bitmap);
    expect(decoration.value.cacheKey).toEqual({
      workspaceHandle: snapshot.workspaceHandle,
      assetFingerprint,
      derivativeProfileId: "h3.authoring.media_derivatives.v6",
      derivativeKind: "thumbnail",
    });
    await decoration.release();
    expect(requests.map(({ operation }) => operation)).toEqual([
      "create",
      "open",
      "release",
    ]);
    expect(bitmap.close).not.toHaveBeenCalled();
    decoration.discard();
    expect(bitmap.close).toHaveBeenCalledTimes(1);
  });

  it("admits a whole-video filmstrip and releases authority before scheduler publication", async () => {
    const snapshot = denseFilmstripSnapshot();
    const video = snapshot.assets.find(({ kind }) => kind === "video")!;
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets.find(
      ({ assetId }) => assetId === video.assetId,
    )!;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
    const filmstripFingerprint = fingerprint("7");
    const bitmap = { width: 344, height: 48, close: vi.fn() };
    const operations: string[] = [];
    const ids = ["filmstrip-create", "filmstrip-open", "filmstrip-release"];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      operations.push(String(request.operation));
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            requestId: "filmstrip-create",
            ownerId: "filmstrip-1",
            runtimeEpoch: 10,
            derivativeKind: "filmstrip",
            mediaType: "image/jpeg",
            byteCount: jpeg.byteLength,
            derivativeFingerprint: filmstripFingerprint,
            assetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(jpeg, {
          status: 200,
          headers: {
            "Content-Type": "image/jpeg",
            "Content-Length": String(jpeg.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: filmstripFingerprint,
            [AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER]: JSON.stringify({
              schema: "h3.authoring.media_geometry.v1",
              sourceWidth: 160,
              sourceHeight: 90,
              derivativeWidth: 344,
              derivativeHeight: 48,
            }),
          },
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "filmstrip-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => filmstripFingerprint,
      requestId: () => ids.shift()!,
      createImageBitmap: vi.fn(async () => bitmap as unknown as ImageBitmap),
    });
    const scheduler =
      createNleLeaseScheduler<
        Awaited<ReturnType<(typeof client)["acquireAssetDecoration"]>>["value"]
      >();
    const publish = vi.fn();
    scheduler.updateDecorationDemand(
      [
        {
          key: "filmstrip-1",
          acquire: (signal) =>
            client.acquireAssetDecoration(
              {
                snapshot,
                manifest,
                assetId: asset.assetId,
                ownerId: "filmstrip-1",
                runtimeEpoch: 10,
                derivativeKind: "filmstrip",
              },
              signal,
            ),
        },
      ],
      publish,
    );
    await scheduler.whenIdle();

    expect(operations).toEqual(["create", "open", "release"]);
    expect(publish).toHaveBeenCalledTimes(1);
    const value = publish.mock.calls[0]![0];
    expect(value).toMatchObject({
      bitmap,
      derivativeKind: "filmstrip",
      tileCount: 4,
      cacheKey: { derivativeKind: "filmstrip" },
    });
    const create = JSON.parse(String(fetchApi.mock.calls[0]![1].body));
    expect(create).toMatchObject({
      scope: "asset",
      derivativeKind: "filmstrip",
      sourceStartFrame: 0,
      sourceEndFrame: 4,
    });
    value.bitmap.close();
    scheduler.close();
  });

  it("closes and releases a filmstrip whose tile aspect disagrees with source geometry", async () => {
    const snapshot = denseFilmstripSnapshot();
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets.find(({ kind }) => kind === "video")!;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
    const filmstripFingerprint = fingerprint("7");
    const bitmap = { width: 352, height: 48, close: vi.fn() };
    const operations: string[] = [];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      operations.push(String(request.operation));
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            requestId: "filmstrip-create",
            ownerId: "filmstrip-aspect",
            runtimeEpoch: 1,
            derivativeKind: "filmstrip",
            mediaType: "image/jpeg",
            byteCount: jpeg.byteLength,
            derivativeFingerprint: filmstripFingerprint,
            assetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(jpeg, {
          status: 200,
          headers: {
            "Content-Type": "image/jpeg",
            "Content-Length": String(jpeg.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: filmstripFingerprint,
            [AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER]: JSON.stringify({
              schema: "h3.authoring.media_geometry.v1",
              sourceWidth: 160,
              sourceHeight: 90,
              derivativeWidth: 352,
              derivativeHeight: 48,
            }),
          },
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "filmstrip-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => filmstripFingerprint,
      requestId: (() => {
        const ids = ["filmstrip-create", "filmstrip-open", "filmstrip-release"];
        return () => ids.shift()!;
      })(),
      createImageBitmap: vi.fn(async () => bitmap as unknown as ImageBitmap),
    });

    await expect(
      client.acquireAssetDecoration(
        {
          snapshot,
          manifest,
          assetId: asset.assetId,
          ownerId: "filmstrip-aspect",
          runtimeEpoch: 1,
          derivativeKind: "filmstrip",
        },
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ disposition: "contract_mismatch" });
    expect(operations).toEqual(["create", "open", "release"]);
    expect(bitmap.close).toHaveBeenCalledTimes(1);
    await client.close();
  });

  it("closes and releases a decoded thumbnail whose pixels disagree with geometry", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets.find(({ kind }) => kind === "image")!;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const bitmap = { width: 31, height: 18, close: vi.fn() };
    const operations: string[] = [];
    const ids = ["asset-create", "asset-open", "asset-release"];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      operations.push(String(request.operation));
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            requestId: "asset-create",
            ownerId: "decoration-1",
            runtimeEpoch: 9,
            derivativeKind: "thumbnail",
            mediaType: "image/png",
            assetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "image/png",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
            [AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER]: JSON.stringify({
              schema: "h3.authoring.media_geometry.v1",
              sourceWidth: 1920,
              sourceHeight: 1080,
              derivativeWidth: 32,
              derivativeHeight: 18,
            }),
          },
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "asset-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createImageBitmap: vi.fn(async () => bitmap as unknown as ImageBitmap),
    });
    await expect(
      client.acquireAssetDecoration(
        {
          snapshot,
          manifest,
          assetId: asset.assetId,
          ownerId: "decoration-1",
          runtimeEpoch: 9,
          derivativeKind: "thumbnail",
        },
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ disposition: "contract_mismatch" });
    expect(bitmap.close).toHaveBeenCalledTimes(1);
    expect(operations).toEqual(["create", "open", "release"]);
  });

  it("closes and releases a thumbnail when cancellation lands during decode", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets.find(({ kind }) => kind === "image")!;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const bitmap = { width: 32, height: 18, close: vi.fn() };
    let finishDecode!: (value: typeof bitmap) => void;
    const decode = new Promise<typeof bitmap>((resolve) => {
      finishDecode = resolve;
    });
    const operations: string[] = [];
    const ids = ["asset-create", "asset-open", "asset-release"];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      operations.push(String(request.operation));
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            requestId: "asset-create",
            ownerId: "decoration-1",
            runtimeEpoch: 9,
            derivativeKind: "thumbnail",
            mediaType: "image/png",
            assetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "image/png",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
            [AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER]: JSON.stringify({
              schema: "h3.authoring.media_geometry.v1",
              sourceWidth: 1920,
              sourceHeight: 1080,
              derivativeWidth: 32,
              derivativeHeight: 18,
            }),
          },
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "asset-release",
        operation: "release",
        leaseId: "lease-1",
      });
    });
    const controller = new AbortController();
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createImageBitmap: async () => (await decode) as unknown as ImageBitmap,
    });
    const acquisition = client.acquireAssetDecoration(
      {
        snapshot,
        manifest,
        assetId: asset.assetId,
        ownerId: "decoration-1",
        runtimeEpoch: 9,
        derivativeKind: "thumbnail",
      },
      controller.signal,
    );
    await vi.waitFor(() => expect(operations).toEqual(["create", "open"]));
    controller.abort();
    finishDecode(bitmap);
    await expect(acquisition).rejects.toMatchObject({
      disposition: "cancelled",
    });
    expect(bitmap.close).toHaveBeenCalledTimes(1);
    expect(operations).toEqual(["create", "open", "release"]);
  });

  it("blocks playback retry when aborted decoration acquisition cannot release authority", async () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets.find(({ kind }) => kind === "image")!;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const bitmap = { width: 32, height: 18, close: vi.fn() };
    let finishDecode!: (value: typeof bitmap) => void;
    const decode = new Promise<typeof bitmap>((resolve) => {
      finishDecode = resolve;
    });
    const operations: string[] = [];
    const ids = ["asset-create", "asset-open", "asset-release"];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as Record<string, unknown>;
      operations.push(String(request.operation));
      if (request.operation === "create")
        return jsonResponse(
          {
            ...success("create"),
            requestId: "asset-create",
            ownerId: "decoration-1",
            runtimeEpoch: 9,
            derivativeKind: "thumbnail",
            mediaType: "image/png",
            assetFingerprint,
            profileFingerprint: manifest.profileFingerprint,
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "image/png",
            "Content-Length": String(bytes.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: derivativeFingerprint,
            [AUTHORING_MEDIA_LEASE_GEOMETRY_HEADER]: JSON.stringify({
              schema: "h3.authoring.media_geometry.v1",
              sourceWidth: 1920,
              sourceHeight: 1080,
              derivativeWidth: 32,
              derivativeHeight: 18,
            }),
          },
        });
      throw new Error("release transport failed");
    });
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => derivativeFingerprint,
      requestId: () => ids.shift()!,
      createImageBitmap: async () => (await decode) as unknown as ImageBitmap,
    });
    const scheduler =
      createNleLeaseScheduler<AuthoringMediaAssetDecoration["value"]>();
    scheduler.updateDecorationDemand(
      [
        {
          key: "decoration-1",
          acquire: (signal) =>
            client.acquireAssetDecoration(
              {
                snapshot,
                manifest,
                assetId: asset.assetId,
                ownerId: "decoration-1",
                runtimeEpoch: 9,
                derivativeKind: "thumbnail",
              },
              signal,
            ),
        },
      ],
      vi.fn(),
    );
    await vi.waitFor(() => expect(operations).toEqual(["create", "open"]));

    const playbackOperation = vi.fn(async () => "unreachable");
    const playback = scheduler.acquirePlayback(playbackOperation);
    finishDecode(bitmap);
    const outcome = await playback.catch((error: unknown) => error);
    scheduler.close();

    expect(playbackOperation).not.toHaveBeenCalled();
    expect(outcome).toBeInstanceOf(AggregateError);
    const acquisitionFailure = outcome as AggregateError;
    expect(acquisitionFailure.errors[0]).toMatchObject({
      disposition: "cancelled",
    });
    expect(acquisitionFailure.errors[1]).toMatchObject({
      disposition: "internal_failure",
    });
    expect(bitmap.close).toHaveBeenCalledTimes(1);
    expect(operations).toEqual(["create", "open", "release"]);
  });
});

describe("M25-52 audio peaks decoration lease", () => {
  it("decodes bound peaks without ImageBitmap and synchronously clears discard", async () => {
    const snapshot = denseFilmstripSnapshot();
    const manifest = buildPublicAssetManifest(snapshot);
    const asset = manifest.assets.find(
      (candidate) => candidate.embeddedAudio === "present_bound",
    )!;
    const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
    const sampleCount = asset.sourceSampleCount! / 6;
    const pairCount = Math.ceil(sampleCount / 80);
    const body = new Uint8Array(20 + pairCount * 2);
    body.set([0x48, 0x33, 0x41, 0x50]);
    const view = new DataView(body.buffer);
    view.setUint16(4, 1, true);
    view.setUint16(6, 100, true);
    view.setUint32(8, 8_000, true);
    view.setUint32(12, sampleCount, true);
    view.setUint32(16, pairCount, true);
    const peaksFingerprint = fingerprint("8");
    const createImageBitmap = vi.fn();
    const operations: string[] = [];
    const ids = ["request-create", "request-open", "request-release"];
    const fetchApi = vi.fn(async (_route: string, init: RequestInit) => {
      const request = JSON.parse(String(init.body)) as {
        operation: string;
        ownerId: string;
        runtimeEpoch: number;
      };
      operations.push(request.operation);
      if (request.operation === "create")
        return jsonResponse(
          {
            schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
            requestId: "request-create",
            operation: "create",
            leaseId: "lease-peaks",
            revision: 1,
            ownerId: request.ownerId,
            runtimeEpoch: request.runtimeEpoch,
            ttlMs: 60_000,
            derivativeKind: "audio_peaks",
            mediaType: "application/octet-stream",
            byteCount: body.byteLength,
            derivativeFingerprint: peaksFingerprint,
            assetFingerprint,
            derivativeProfileId: "h3.authoring.media_derivatives.v6",
            profileFingerprint: manifest.profileFingerprint,
            audioDisposition: "present_bound",
          },
          capability,
        );
      if (request.operation === "open")
        return new Response(body, {
          status: 200,
          headers: {
            "Content-Type": "application/octet-stream",
            "Content-Length": String(body.byteLength),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            [AUTHORING_MEDIA_LEASE_REVISION_HEADER]: "1",
            [AUTHORING_MEDIA_LEASE_DERIVATIVE_HEADER]: peaksFingerprint,
          },
        });
      return jsonResponse({
        schema: "h3.context.authoring_media_lease.released.v1",
        requestId: "request-release",
        operation: "release",
        leaseId: "lease-peaks",
      });
    });
    const client = createAuthoringMediaSourceLeaseClient({
      fetchApi,
      digest: async () => peaksFingerprint,
      requestId: () => ids.shift()!,
      createImageBitmap,
    });

    const decoration = await client.acquireAssetDecoration(
      {
        snapshot,
        manifest,
        assetId: asset.assetId,
        ownerId: "waveform-owner",
        runtimeEpoch: 11,
        derivativeKind: "audio_peaks",
      },
      new AbortController().signal,
    );
    expect(decoration.value.derivativeKind).toBe("audio_peaks");
    if (decoration.value.derivativeKind !== "audio_peaks")
      throw new Error("expected audio peaks decoration");
    expect(decoration.value.peaks.sampleCount).toBe(sampleCount);
    expect(createImageBitmap).not.toHaveBeenCalled();
    await decoration.release();
    expect(operations).toEqual(["create", "open", "release"]);
    decoration.discard();
    expect([...decoration.value.peaks.pairs]).toEqual(
      Array(pairCount * 2).fill(0),
    );
  });
});
