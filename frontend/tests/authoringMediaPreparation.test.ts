import { describe, expect, it, vi } from "vitest";

import {
  AUTHORING_MEDIA_LEASE_CAPABILITY_HEADER,
  AUTHORING_MEDIA_LEASE_ERROR_SCHEMA,
  AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA,
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
  NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
  canonicalPublicRuntimeAssetFingerprint,
} from "../src/contracts/authoringMediaLeaseCodec";
import { prepareAuthoringAssetPlayback } from "../src/host/authoringMediaPlaybackPreparation";
import {
  AUTHORING_MEDIA_LEASE_ROUTE,
  AuthoringMediaSourceLeaseError,
  createAuthoringMediaSourceLeaseClient,
  type AuthoringMediaSourceLease,
  type AuthoringMediaSourceLeaseClient,
  type NleAuthoringMediaAssetPreparationContext,
} from "../src/host/authoringMediaSourceLease";
import { RUNTIME_PROFILE } from "../src/runtime/mediaCapabilities";
import { buildNleAuthoringAssetManifest } from "../src/runtime/nleAuthoringAssetManifest";
import {
  PREPARATION_ASSETS,
  PREPARATION_ASSETS_NO_DECODER_MAKES,
  preparationAuthoring,
} from "./support/nleAssetPreparationFixture";

const capability = "a".repeat(64);
const mediaTypes = { video_proxy: "video/mp4", audio_preview: "audio/wav" };

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

type Wire = Record<string, unknown>;

/** The service as the client sees it: it answers a create and a release, and records both. */
function service(
  overrides: Readonly<{
    create?(wire: Wire): Response | Promise<Response>;
    release?(wire: Wire, attempt: number): Response | Promise<Response>;
  }> = {},
  assets: Parameters<typeof preparationAuthoring>[1] = PREPARATION_ASSETS,
) {
  const requests: Array<Readonly<{ route: string; wire: Wire }>> = [];
  let releases = 0;
  const authoring = preparationAuthoring(1, assets);
  const manifest = buildNleAuthoringAssetManifest(authoring);
  const fetchApi = vi.fn(async (route: string, init: RequestInit) => {
    const wire = JSON.parse(String(init.body)) as Wire;
    requests.push({ route, wire });
    if (wire.operation === "create") {
      if (overrides.create !== undefined) return overrides.create(wire);
      const asset = manifest.assets.find(
        ({ assetId }) => assetId === wire.assetId,
      )!;
      const kind = wire.derivativeKind as keyof typeof mediaTypes;
      return jsonResponse(
        {
          schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
          requestId: wire.requestId,
          operation: "create",
          leaseId: "preparation-lease",
          revision: 1,
          ownerId: wire.ownerId,
          runtimeEpoch: wire.runtimeEpoch,
          ttlMs: 60_000,
          derivativeKind: kind,
          mediaType: mediaTypes[kind],
          byteCount: 4_096,
          derivativeFingerprint: `sha256:${"4".repeat(64)}`,
          assetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
          derivativeProfileId: "h3.authoring.media_derivatives.v6",
          profileFingerprint: manifest.profileFingerprint,
          audioDisposition: "present_bound",
        },
        capability,
      );
    }
    releases += 1;
    if (overrides.release !== undefined)
      return overrides.release(wire, releases);
    return jsonResponse({
      schema: AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA,
      requestId: wire.requestId,
      operation: "release",
      leaseId: wire.leaseId,
    });
  });
  let sequence = 0;
  const client = createAuthoringMediaSourceLeaseClient({
    fetchApi,
    requestId: () => `request-${++sequence}`,
  });
  const context = (
    assetId: string,
    derivativeKind: "video_proxy" | "audio_preview",
  ): NleAuthoringMediaAssetPreparationContext => ({
    authoring,
    manifest,
    assetId,
    ownerId: "nle-preparation-3-1",
    runtimeEpoch: 3,
    derivativeKind,
  });
  return { authoring, manifest, client, context, requests, fetchApi };
}

const refusal = (reason: string, status: number) => (wire: Wire) =>
  jsonResponse(
    {
      schema: AUTHORING_MEDIA_LEASE_ERROR_SCHEMA,
      requestId: wire.requestId,
      reason,
    },
    null,
    status,
  );

describe("asset playback preparation at the lease client", () => {
  it.each(["video_proxy", "audio_preview"] as const)(
    "asks for one %s over the whole source and never opens it",
    async (derivativeKind) => {
      const { authoring, manifest, client, context, requests } = service();
      const asset = manifest.assets.find(
        ({ assetId }) => assetId === "vid-audio",
      )!;
      const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);

      const prepared = await client.prepareAssetPlayback!(
        context("vid-audio", derivativeKind),
        new AbortController().signal,
      );

      expect(requests).toEqual([
        {
          route: AUTHORING_MEDIA_LEASE_ROUTE,
          wire: {
            schema: NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
            operation: "create",
            requestId: "request-1",
            authoringSchema: authoring.schema,
            profileId: authoring.profileId,
            workspaceHandle: authoring.workspaceHandle,
            workspaceRevision: authoring.workspaceRevision,
            timelineRevision: authoring.timelineRevision,
            authoringFingerprint: authoring.authoringFingerprint,
            manifestFingerprint: manifest.manifestFingerprint,
            profileFingerprint: manifest.profileFingerprint,
            assetId: "vid-audio",
            derivativeKind,
            ownerId: "nle-preparation-3-1",
            runtimeEpoch: 3,
            sourceStartFrame: 0,
            sourceEndFrame: 48,
          },
        },
      ]);
      expect(prepared.value).toEqual({
        derivativeKind,
        cacheKey: {
          workspaceHandle: authoring.workspaceHandle,
          assetFingerprint,
          derivativeProfileId: "h3.authoring.media_derivatives.v6",
          derivativeKind,
        },
      });
      expect(Object.isFrozen(prepared)).toBe(true);
      expect(Object.isFrozen(prepared.value)).toBe(true);
      expect(Object.isFrozen(prepared.value.cacheKey)).toBe(true);
      // The page holds no bytes: nothing but the fact and a lease to end.
      expect(Object.keys(prepared).sort()).toEqual(["release", "value"]);

      await prepared.release();
      expect(requests).toHaveLength(2);
      expect(requests[1]).toMatchObject({
        route: AUTHORING_MEDIA_LEASE_ROUTE,
        wire: {
          schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
          operation: "release",
          leaseId: "preparation-lease",
          revision: 1,
          ownerId: "nle-preparation-3-1",
          runtimeEpoch: 3,
        },
      });
    },
  );

  it("prepares the picture of a video that has no bound audio", async () => {
    const { client, context, requests } = service();
    await client.prepareAssetPlayback!(
      context("vid-silent", "video_proxy"),
      new AbortController().signal,
    );
    expect(requests[0]!.wire).toMatchObject({
      assetId: "vid-silent",
      derivativeKind: "video_proxy",
      sourceEndFrame: 12,
    });
  });

  it.each([
    ["an image", "img-still", "video_proxy"],
    ["a font", "font-face", "video_proxy"],
    ["a video without one timing row per frame", "vid-sparse", "video_proxy"],
    ["the audio of that video", "vid-sparse", "audio_preview"],
    ["the audio of a video that has none", "vid-silent", "audio_preview"],
    ["bound audio without a sample count", "vid-uncounted", "audio_preview"],
    ["an asset the catalog does not have", "vid-missing", "video_proxy"],
  ] as const)(
    "refuses %s without asking the service",
    async (_name, assetId, derivativeKind) => {
      const { client, context, fetchApi } = service();
      await expect(
        client.prepareAssetPlayback!(
          context(assetId, derivativeKind),
          new AbortController().signal,
        ),
      ).rejects.toMatchObject({ disposition: "contract_mismatch" });
      expect(fetchApi).not.toHaveBeenCalled();
    },
  );

  // The decoder makes neither state. The member is given a manifest and not a decoder's word,
  // so the asset's kind and the audio's state are each asked for themselves.
  it.each([
    ["an image that is timed like a video", "img-timed", "video_proxy"],
    [
      "audio that has a sample count and is not bound",
      "vid-unbound",
      "audio_preview",
    ],
  ] as const)(
    "refuses %s without asking the service",
    async (_name, assetId, derivativeKind) => {
      const { client, context, fetchApi } = service({}, [
        ...PREPARATION_ASSETS,
        ...PREPARATION_ASSETS_NO_DECODER_MAKES,
      ]);
      await expect(
        client.prepareAssetPlayback!(
          context(assetId, derivativeKind),
          new AbortController().signal,
        ),
      ).rejects.toMatchObject({ disposition: "contract_mismatch" });
      expect(fetchApi).not.toHaveBeenCalled();
    },
  );

  it("refuses a manifest that does not bind the authoring state it is sent with", async () => {
    const { client, context, fetchApi } = service();
    const current = context("vid-audio", "video_proxy");
    await expect(
      client.prepareAssetPlayback!(
        { ...current, authoring: preparationAuthoring(2) },
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ disposition: "contract_mismatch" });
    expect(fetchApi).not.toHaveBeenCalled();
  });

  it("makes no request for a preparation that was already aborted", async () => {
    const { client, context, fetchApi } = service();
    const controller = new AbortController();
    controller.abort();
    await expect(
      client.prepareAssetPlayback!(
        context("vid-audio", "video_proxy"),
        controller.signal,
      ),
    ).rejects.toMatchObject({ disposition: "cancelled" });
    expect(fetchApi).not.toHaveBeenCalled();
  });

  it.each([
    ["busy", 429],
    ["resource_limit", 413],
    ["stale", 409],
    ["unsupported", 422],
    ["generation_failed", 422],
  ] as const)(
    "passes the service's %s on with its disposition and holds no lease",
    async (reason, status) => {
      const { client, context, requests } = service({
        create: refusal(reason, status),
      });
      await expect(
        client.prepareAssetPlayback!(
          context("vid-audio", "video_proxy"),
          new AbortController().signal,
        ),
      ).rejects.toMatchObject({ disposition: reason, status });
      await client.close();
      // Nothing was leased, so closing the client has nothing to release.
      expect(requests.map(({ wire }) => wire.operation)).toEqual(["create"]);
    },
  );

  it("refuses a receipt that answers for another asset", async () => {
    const { client, context } = service({
      create: (wire) =>
        jsonResponse(
          {
            schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
            requestId: wire.requestId,
            operation: "create",
            leaseId: "preparation-lease",
            revision: 1,
            ownerId: wire.ownerId,
            runtimeEpoch: wire.runtimeEpoch,
            ttlMs: 60_000,
            derivativeKind: "video_proxy",
            mediaType: "video/mp4",
            byteCount: 4_096,
            derivativeFingerprint: `sha256:${"4".repeat(64)}`,
            assetFingerprint: `sha256:${"9".repeat(64)}`,
            derivativeProfileId: "h3.authoring.media_derivatives.v6",
            profileFingerprint: wire.profileFingerprint,
            audioDisposition: "absent",
          },
          capability,
        ),
    });
    await expect(
      client.prepareAssetPlayback!(
        context("vid-audio", "video_proxy"),
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ disposition: "contract_mismatch" });
  });

  it("reports a receipt it cannot read as a lease error, not as a bare one", async () => {
    const { client, context } = service({
      create: (wire) =>
        jsonResponse(
          {
            schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
            requestId: wire.requestId,
          },
          capability,
        ),
    });
    const outcome = await client.prepareAssetPlayback!(
      context("vid-audio", "video_proxy"),
      new AbortController().signal,
    ).then(
      () => null,
      (error: unknown) => error,
    );
    // The scheduler and the hook read `disposition`; an error without one says nothing.
    expect(outcome).toBeInstanceOf(AuthoringMediaSourceLeaseError);
    expect(outcome).toMatchObject({ disposition: "internal_failure" });
  });

  it("reports a transport fault as a lease error, cancelled when the caller aborted", async () => {
    const failing = service({
      create: () => {
        throw new TypeError("network");
      },
    });
    await expect(
      failing.client.prepareAssetPlayback!(
        failing.context("vid-audio", "video_proxy"),
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ disposition: "internal_failure" });

    const controller = new AbortController();
    const aborting = service({
      create: () => {
        controller.abort();
        throw new DOMException("aborted", "AbortError");
      },
    });
    await expect(
      aborting.client.prepareAssetPlayback!(
        aborting.context("vid-audio", "video_proxy"),
        controller.signal,
      ),
    ).rejects.toMatchObject({ disposition: "cancelled" });
  });

  it("leaves a lease whose release failed for close() to end", async () => {
    const { client, context, requests } = service({
      release: (wire, attempt) => {
        if (attempt === 1) throw new TypeError("network");
        return jsonResponse({
          schema: AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA,
          requestId: wire.requestId,
          operation: "release",
          leaseId: wire.leaseId,
        });
      },
    });
    const prepared = await client.prepareAssetPlayback!(
      context("vid-audio", "video_proxy"),
      new AbortController().signal,
    );
    await expect(prepared.release()).rejects.toMatchObject({
      disposition: "internal_failure",
    });
    await client.close();
    expect(requests.map(({ wire }) => wire.operation)).toEqual([
      "create",
      "release",
      "release",
    ]);
    // Ended once: a second close has no lease left to release.
    await client.close();
    expect(requests).toHaveLength(3);
  });
});

// The lease client's `create` refuses an aborted signal before it asks the service and gives its
// own faults a disposition, so through that client the member's own checks are never the ones
// that answer. Here the member gets a client that does neither.
describe("what the preparation member does itself", () => {
  type Create = AuthoringMediaSourceLeaseClient["create"];

  function preparation() {
    const authoring = preparationAuthoring();
    const context: NleAuthoringMediaAssetPreparationContext = {
      authoring,
      manifest: buildNleAuthoringAssetManifest(authoring),
      assetId: "vid-audio",
      ownerId: "nle-preparation-3-1",
      runtimeEpoch: 3,
      derivativeKind: "video_proxy",
    };
    const release = vi.fn(async (_signal: AbortSignal) => undefined);
    const lease = { release } as unknown as AuthoringMediaSourceLease;
    return { context, release, lease };
  }

  const outcomeOf = (pending: Promise<unknown>) =>
    pending.then(
      () => null,
      (error: unknown) => error,
    );

  it("asks the client for nothing when its signal is already aborted", async () => {
    const { context, lease } = preparation();
    const create = vi.fn<Create>(async () => lease);
    const controller = new AbortController();
    controller.abort();

    const outcome = await outcomeOf(
      prepareAuthoringAssetPlayback(
        { create },
        () => "request-1",
        context,
        controller.signal,
      ),
    );

    expect(outcome).toBeInstanceOf(AuthoringMediaSourceLeaseError);
    expect(outcome).toMatchObject({ disposition: "cancelled", status: 499 });
    expect(create).not.toHaveBeenCalled();
  });

  it("gives the create the signal it was given, so that an abort reaches the request", async () => {
    const { context, lease } = preparation();
    const create = vi.fn<Create>(async () => lease);
    const controller = new AbortController();

    await prepareAuthoringAssetPlayback(
      { create },
      () => "request-1",
      context,
      controller.signal,
    );

    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0]![1]).toBe(controller.signal);
  });

  it("reports a bare fault of the create as a failure, and as cancelled once the caller aborted", async () => {
    const { context } = preparation();
    const failed = await outcomeOf(
      prepareAuthoringAssetPlayback(
        {
          create: async () => {
            throw new TypeError("network");
          },
        },
        () => "request-1",
        context,
        new AbortController().signal,
      ),
    );
    expect(failed).toBeInstanceOf(AuthoringMediaSourceLeaseError);
    expect(failed).toMatchObject({
      disposition: "internal_failure",
      status: 0,
    });

    const controller = new AbortController();
    const aborted = await outcomeOf(
      prepareAuthoringAssetPlayback(
        {
          create: async () => {
            controller.abort();
            throw new TypeError("network");
          },
        },
        () => "request-2",
        context,
        controller.signal,
      ),
    );
    expect(aborted).toBeInstanceOf(AuthoringMediaSourceLeaseError);
    expect(aborted).toMatchObject({ disposition: "cancelled", status: 499 });
  });

  it("ends the lease under a deadline of its own, whatever became of the caller's signal", async () => {
    const { context, release, lease } = preparation();
    const deadline = new AbortController();
    const timeout = vi
      .spyOn(AbortSignal, "timeout")
      .mockReturnValue(deadline.signal);
    try {
      const controller = new AbortController();
      const prepared = await prepareAuthoringAssetPlayback(
        { create: async () => lease },
        () => "request-1",
        context,
        controller.signal,
      );
      expect(timeout).not.toHaveBeenCalled();
      // Playback preempts a preparation by aborting its signal. A release that ended with that
      // signal would end nothing, and the lease would stay held after the preparation is over.
      controller.abort();
      await prepared.release();

      expect(timeout).toHaveBeenCalledExactlyOnceWith(
        RUNTIME_PROFILE.limits.teardownDeadlineMs,
      );
      expect(release).toHaveBeenCalledTimes(1);
      expect(release.mock.calls[0]![0]).toBe(deadline.signal);
    } finally {
      timeout.mockRestore();
    }
  });
});
