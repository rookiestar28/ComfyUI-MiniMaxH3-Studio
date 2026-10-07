import { describe, expect, it } from "vitest";

import {
  AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  decodeAuthoringMediaLeaseSuccess,
  encodeAuthoringMediaLeaseRequest,
} from "../src/contracts/authoringMediaLeaseCodec";

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;

describe("audio_peaks lease codec", () => {
  it("admits only a whole-asset request", () => {
    const request = {
      schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
      operation: "create" as const,
      requestId: "request-peaks",
      workspaceHandle: "workspace-1",
      workspaceRevision: 1,
      timelineRevision: 1,
      publicFingerprint: fingerprint("1"),
      manifestFingerprint: fingerprint("2"),
      profileFingerprint: fingerprint("3"),
      scope: "asset" as const,
      clipId: null,
      assetId: "asset-video",
      derivativeKind: "audio_peaks" as const,
      ownerId: "waveform-1",
      runtimeEpoch: 1,
      sourceStartFrame: 0,
      sourceEndFrame: 12,
    };

    const encoded = encodeAuthoringMediaLeaseRequest(request);
    if (encoded.operation !== "create") throw new Error("expected create");
    expect(encoded.derivativeKind).toBe("audio_peaks");
    expect(() =>
      encodeAuthoringMediaLeaseRequest({
        ...request,
        scope: "clip",
        clipId: "clip-1",
      } as never),
    ).toThrow("source span is invalid");
  });

  it("requires octet-stream, 64 KiB and present_bound audio", () => {
    const success = {
      schema: "h3.context.authoring_media_lease.success.v1",
      requestId: "request-peaks",
      operation: "create",
      leaseId: "lease-peaks",
      revision: 1,
      ownerId: "waveform-1",
      runtimeEpoch: 1,
      ttlMs: 60_000,
      derivativeKind: "audio_peaks",
      mediaType: "application/octet-stream",
      byteCount: 64 * 1024,
      derivativeFingerprint: fingerprint("4"),
      assetFingerprint: fingerprint("5"),
      derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
      profileFingerprint: fingerprint("3"),
      audioDisposition: "present_bound",
    };

    expect(decodeAuthoringMediaLeaseSuccess(success).byteCount).toBe(65_536);
    expect(() =>
      decodeAuthoringMediaLeaseSuccess({ ...success, byteCount: 65_537 }),
    ).toThrow("byteCount is invalid");
    expect(() =>
      decodeAuthoringMediaLeaseSuccess({
        ...success,
        audioDisposition: "absent",
      }),
    ).toThrow("audioDisposition is invalid for derivativeKind");
  });
});
