import { afterEach, describe, expect, it, vi } from "vitest";
import { webcrypto } from "node:crypto";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import { mediaOwnership, workspaceMediaClient } from "../e2e/nleWorkspaceMedia";
import {
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  canonicalPublicRuntimeAssetFingerprint,
  type AuthoringMediaLeaseCreateRequest,
} from "../src/contracts/authoringMediaLeaseCodec";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";

function binding(revision = 0) {
  const wire = structuredClone(fixture.snapshot);
  wire.workspace_revision += revision;
  wire.timeline_revision += revision;
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  const snapshot = decodePublicCompositionSnapshot(wire);
  const manifest = buildPublicAssetManifest(snapshot);
  const asset = manifest.assets[0]!;
  const clipId = snapshot.clips[0]!.clipId;
  return {
    request: {
      asset,
      ownerId: clipId,
      epoch: revision + 1,
      signal: new AbortController().signal,
    },
    context: {
      snapshot,
      manifest,
      clipId,
      sourceStartFrame: 0,
      sourceEndFrame: asset.sourceFrameCount!,
    },
  };
}

afterEach(async () => {
  await workspaceMediaClient.close();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("workspace fixture retained source authority", () => {
  it("matches the product's rebind without another fetch, element or PCM allocation", async () => {
    const fetchApi = vi.fn(
      async () =>
        new Response(new Uint8Array(16), {
          headers: { "content-type": "video/mp4" },
        }),
    );
    vi.stubGlobal("fetch", fetchApi);
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:fixture");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(
      () => undefined,
    );
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    const first = binding();
    const source = await workspaceMediaClient.acquireVideoSource(
      first.request,
      first.context,
    );
    const before = mediaOwnership();
    const audio = source.audioBody;
    const next = binding(1);
    expect(typeof source.rebind).toBe("function");
    expect(await source.rebind!(next.request, next.context)).toBe(true);
    expect(source.audioBody).toBe(audio);
    expect(fetchApi).toHaveBeenCalledTimes(1);
    expect(mediaOwnership()).toMatchObject({
      videoElements: before.videoElements,
      surfaces: before.surfaces,
      retainedBytes: before.retainedBytes,
      leaseCreates: before.leaseCreates + 2,
      leaseOpens: before.leaseOpens,
      sourceRebinds: before.sourceRebinds + 1,
    });
    expect(
      await source.rebind!(
        { ...next.request, ownerId: "foreign" },
        next.context,
      ),
    ).toBe(false);
    await source.release();
    expect(await source.rebind!(next.request, next.context)).toBe(false);
  });

  it("creates a static authority without bytes and adopts its verified identity under a new lease", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const fetchApi = vi.fn(
      async () =>
        new Response(new Uint8Array(12), {
          headers: { "content-type": "font/woff2" },
        }),
    );
    vi.stubGlobal("fetch", fetchApi);
    const { context, request } = binding();
    const create: AuthoringMediaLeaseCreateRequest = {
      schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
      operation: "create",
      requestId: "font-first",
      workspaceHandle: context.snapshot.workspaceHandle,
      workspaceRevision: context.snapshot.workspaceRevision,
      timelineRevision: context.snapshot.timelineRevision,
      publicFingerprint: context.snapshot.publicFingerprint,
      manifestFingerprint: context.manifest.manifestFingerprint,
      profileFingerprint: context.manifest.profileFingerprint,
      scope: "asset",
      clipId: null,
      assetId: "font",
      derivativeKind: "packaged_font_face",
      ownerId: "text-owner",
      runtimeEpoch: 1,
      sourceStartFrame: 0,
      sourceEndFrame: 1,
    };
    const fp = canonicalPublicRuntimeAssetFingerprint(request.asset);
    const first = await workspaceMediaClient.create(create, request.signal, fp);
    expect(fetchApi).not.toHaveBeenCalled();
    const body = await first.open(request.signal);
    const before = mediaOwnership();
    const fresh = await workspaceMediaClient.create(
      { ...create, runtimeEpoch: 2, requestId: "font-next" },
      request.signal,
      fp,
    );
    expect(typeof fresh.derivative).toBe("function");
    expect(typeof fresh.adopt).toBe("function");
    expect(fresh.derivative!()).toEqual({
      fingerprint: body.receipt.derivativeFingerprint,
      byteCount: body.blob.size,
      kind: "packaged_font_face",
    });
    fresh.adopt!();
    expect(fetchApi).toHaveBeenCalledTimes(1);
    expect(mediaOwnership().retainedBytes).toBe(before.retainedBytes);
    await first.release();
    expect(mediaOwnership().retainedBytes).toBe(before.retainedBytes);
    expect(fresh.state().opened).toBe(true);
    await fresh.release();
    expect(mediaOwnership().retainedBytes).toBe(before.retainedBytes - 12);
    expect(() => fresh.adopt!()).toThrow();
  });
});
