import { describe, expect, it } from "vitest";

import {
  AUTHORING_MEDIA_LEASE_ERROR_SCHEMA,
  AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA,
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
  NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
  canonicalPublicRuntimeAssetFingerprint,
  decodeAuthoringMediaLeaseError,
  decodeAuthoringMediaLeaseReleased,
  decodeAuthoringMediaLeaseSuccess,
  decodeAuthoringMediaGeometry,
  encodeAuthoringMediaLeaseRequest,
} from "../src/contracts/authoringMediaLeaseCodec";
import type { PublicRuntimeAsset } from "../src/runtime/publicAssetManifest";
import {
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
} from "../src/contracts/authoringWorkbenchCodec";

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;

const create = {
  schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  operation: "create" as const,
  requestId: "request-1",
  workspaceHandle: "authoring-test",
  workspaceRevision: 1,
  timelineRevision: 2,
  publicFingerprint: fingerprint("1"),
  manifestFingerprint: fingerprint("2"),
  profileFingerprint: fingerprint("3"),
  scope: "clip" as const,
  clipId: "clip-1",
  assetId: "asset-1",
  derivativeKind: "video_proxy" as const,
  ownerId: "clip-1",
  runtimeEpoch: 4,
  sourceStartFrame: 0,
  sourceEndFrame: 12,
};

const success = {
  schema: AUTHORING_MEDIA_LEASE_SUCCESS_SCHEMA,
  requestId: "request-1",
  operation: "create" as const,
  leaseId: "lease-1",
  revision: 1,
  ownerId: "clip-1",
  runtimeEpoch: 4,
  ttlMs: 60_000,
  derivativeKind: "video_proxy" as const,
  mediaType: "video/mp4" as const,
  byteCount: 42,
  derivativeFingerprint: fingerprint("4"),
  assetFingerprint: fingerprint("5"),
  derivativeProfileId: "h3.authoring.media_derivatives.v6" as const,
  profileFingerprint: fingerprint("3"),
  audioDisposition: "present_bound" as const,
};

describe("M25-13 authoring media lease codec", () => {
  it.each([64, 65, 256, 257, 362, 512])(
    "retains complete admitted source spans and final thumbnails at %i frames",
    (frames) => {
      for (const derivativeKind of [
        "video_proxy",
        "frame_timing_index",
        "thumbnail",
      ] as const) {
        const request = {
          ...create,
          derivativeKind,
          sourceStartFrame: derivativeKind === "thumbnail" ? frames - 1 : 0,
          sourceEndFrame: frames,
        };
        expect(encodeAuthoringMediaLeaseRequest(request)).toEqual(request);
      }
    },
  );
  it("preserves original geometry independently of a downscaled proxy", () => {
    const geometry = {
      schema: "h3.authoring.media_geometry.v1",
      sourceWidth: 1920,
      sourceHeight: 1080,
      derivativeWidth: 320,
      derivativeHeight: 180,
    };
    expect(
      decodeAuthoringMediaGeometry(JSON.stringify(geometry), "video_proxy"),
    ).toEqual(geometry);
    expect(() =>
      decodeAuthoringMediaGeometry(
        JSON.stringify({ ...geometry, sourceWidth: 0 }),
        "video_proxy",
      ),
    ).toThrow();
    expect(() =>
      decodeAuthoringMediaGeometry(
        JSON.stringify({ ...geometry, path: "private" }),
        "video_proxy",
      ),
    ).toThrow();
    expect(() =>
      decodeAuthoringMediaGeometry(
        JSON.stringify(geometry),
        "packaged_font_face",
      ),
    ).toThrow();
    expect(decodeAuthoringMediaGeometry(null, "video_proxy")).toBeNull();
  });
  it("round-trips the closed create and owner operations", () => {
    expect(encodeAuthoringMediaLeaseRequest(create)).toEqual(create);
    expect(
      encodeAuthoringMediaLeaseRequest({
        schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
        operation: "open",
        requestId: "request-2",
        leaseId: "lease-1",
        revision: 1,
        ownerId: "clip-1",
        runtimeEpoch: 4,
      }),
    ).toMatchObject({ operation: "open", revision: 1 });
    expect(
      encodeAuthoringMediaLeaseRequest({
        schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
        operation: "transfer",
        requestId: "request-3",
        leaseId: "lease-1",
        revision: 1,
        ownerId: "clip-1",
        runtimeEpoch: 4,
        nextOwnerId: "clip-2",
        nextRuntimeEpoch: 5,
      }),
    ).toMatchObject({ nextOwnerId: "clip-2", nextRuntimeEpoch: 5 });
  });

  it("requires explicit scope and permits a null clip only for asset thumbnails", () => {
    const asset = {
      ...create,
      scope: "asset" as const,
      clipId: null,
      assetId: "asset-image",
      derivativeKind: "thumbnail" as const,
      sourceStartFrame: 0,
      sourceEndFrame: 1,
    };
    expect(encodeAuthoringMediaLeaseRequest(asset)).toEqual(asset);
    expect(() =>
      encodeAuthoringMediaLeaseRequest({ ...asset, clipId: "clip-1" }),
    ).toThrow(/clipId/);
    expect(() =>
      encodeAuthoringMediaLeaseRequest({
        ...asset,
        derivativeKind: "video_proxy",
      }),
    ).toThrow(/asset scope/);
    expect(() =>
      encodeAuthoringMediaLeaseRequest({ ...asset, sourceEndFrame: 2 }),
    ).toThrow(/source span/);
    const missing = { ...create } as Record<string, unknown>;
    delete missing.scope;
    expect(() => encodeAuthoringMediaLeaseRequest(missing as never)).toThrow(
      /closed/,
    );
  });

  it("uses a separate authoring-bound closed create variant for bin decorations", () => {
    const request = {
      schema: NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
      operation: "create" as const,
      requestId: "nle-request-1",
      authoringSchema: NLE_AUTHORING_SCHEMA,
      profileId: NLE_AUTHORING_PROFILE_ID,
      workspaceHandle: "workspace.1",
      workspaceRevision: 3,
      timelineRevision: 4,
      authoringFingerprint: fingerprint("1"),
      manifestFingerprint: fingerprint("2"),
      profileFingerprint: fingerprint("3"),
      assetId: "asset.1",
      derivativeKind: "thumbnail" as const,
      ownerId: "bin-owner.1",
      runtimeEpoch: 1,
      sourceStartFrame: 0,
      sourceEndFrame: 1,
    };
    expect(encodeAuthoringMediaLeaseRequest(request)).toEqual(request);
    expect(() =>
      encodeAuthoringMediaLeaseRequest({
        ...request,
        publicFingerprint: fingerprint("4"),
      } as never),
    ).toThrow(/closed/u);
    expect(() =>
      encodeAuthoringMediaLeaseRequest({
        ...request,
        profileId: "wrong",
      } as never),
    ).toThrow(/profile/u);
    // The three kinds that need a clip stay refused in this form, as does a kind that is none.
    for (const derivativeKind of [
      "frame_timing_index",
      "image_proxy",
      "packaged_font_face",
      "video-proxy",
    ])
      expect(() =>
        encodeAuthoringMediaLeaseRequest({
          ...request,
          derivativeKind,
        } as never),
      ).toThrow(/asset scope/u);
  });

  it.each(["video_proxy", "audio_preview"] as const)(
    "admits %s without a clip in the authoring-bound form and nowhere else",
    (derivativeKind) => {
      const preparation = {
        schema: NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
        operation: "create" as const,
        requestId: "nle-preparation-request-1",
        authoringSchema: NLE_AUTHORING_SCHEMA,
        profileId: NLE_AUTHORING_PROFILE_ID,
        workspaceHandle: "workspace.1",
        workspaceRevision: 3,
        timelineRevision: 4,
        authoringFingerprint: fingerprint("1"),
        manifestFingerprint: fingerprint("2"),
        profileFingerprint: fingerprint("3"),
        assetId: "asset.1",
        derivativeKind,
        ownerId: "nle-preparation-1-0",
        runtimeEpoch: 1,
        sourceStartFrame: 0,
        sourceEndFrame: 48,
      };
      const encoded = encodeAuthoringMediaLeaseRequest(preparation);
      expect(encoded).toEqual(preparation);
      expect(Object.isFrozen(encoded)).toBe(true);
      // The snapshot form's asset scope is the three decorations, with or without this change.
      expect(() =>
        encodeAuthoringMediaLeaseRequest({
          ...create,
          scope: "asset",
          clipId: null,
          derivativeKind,
        }),
      ).toThrow(/asset scope/u);
    },
  );

  it("rejects unknown fields, unsafe ranges, and an invalid kind/profile", () => {
    expect(() =>
      encodeAuthoringMediaLeaseRequest({
        ...create,
        path: "private.mp4",
      } as never),
    ).toThrow(/closed/);
    expect(() =>
      encodeAuthoringMediaLeaseRequest({ ...create, sourceEndFrame: 513 }),
    ).toThrow(/sourceEndFrame/);
    expect(() =>
      encodeAuthoringMediaLeaseRequest({ ...create, sourceStartFrame: 12 }),
    ).toThrow(/source span/);
    expect(() =>
      encodeAuthoringMediaLeaseRequest({
        ...create,
        derivativeKind: "waveform",
      } as never),
    ).toThrow(/derivativeKind/);
  });

  it("decodes exact success, release, and error envelopes", () => {
    expect(decodeAuthoringMediaLeaseSuccess(success)).toEqual(success);
    expect(
      decodeAuthoringMediaLeaseReleased({
        schema: AUTHORING_MEDIA_LEASE_RELEASED_SCHEMA,
        requestId: "request-4",
        operation: "release",
        leaseId: "lease-1",
      }),
    ).toMatchObject({ operation: "release", leaseId: "lease-1" });
    expect(
      decodeAuthoringMediaLeaseError({
        schema: AUTHORING_MEDIA_LEASE_ERROR_SCHEMA,
        requestId: null,
        reason: "invalid_request",
      }),
    ).toEqual({
      schema: AUTHORING_MEDIA_LEASE_ERROR_SCHEMA,
      requestId: null,
      reason: "invalid_request",
    });
    expect(() =>
      decodeAuthoringMediaLeaseSuccess({ ...success, token: "secret" }),
    ).toThrow(/closed/);
  });

  it("fingerprints the exact canonical public asset without private authority", () => {
    const asset: PublicRuntimeAsset = {
      assetId: "asset-1",
      kind: "video",
      sourceTimeBase: { num: 1, den: 12_288 },
      sourceFrameCount: 2,
      sourceSampleCount: null,
      embeddedAudio: "absent",
      timestampPolicy: "nonnegative_monotonic_v1",
      landmarks: [
        { frameIndex: 0, pts: 0, dts: 0, durationTicks: 512 },
        { frameIndex: 1, pts: 512, dts: 512, durationTicks: 512 },
      ],
    };
    expect(canonicalPublicRuntimeAssetFingerprint(asset)).toMatch(
      /^sha256:[0-9a-f]{64}$/,
    );
    expect(
      canonicalPublicRuntimeAssetFingerprint({ ...asset, sourceFrameCount: 3 }),
    ).not.toBe(canonicalPublicRuntimeAssetFingerprint(asset));
  });
});
