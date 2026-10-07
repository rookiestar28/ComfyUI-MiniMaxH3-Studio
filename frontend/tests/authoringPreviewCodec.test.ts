import { describe, expect, it } from "vitest";

import {
  AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
  AUTHORING_PREVIEW_ERROR_SCHEMA,
  AUTHORING_PREVIEW_REQUEST_SCHEMA,
  AUTHORING_PREVIEW_SUCCESS_SCHEMA,
  decodeAuthoringPreviewCapability,
  decodeAuthoringPreviewError,
  decodeAuthoringPreviewRequest,
  decodeAuthoringPreviewSuccess,
  encodeAuthoringPreviewRequest,
} from "../src/contracts/authoringPreviewCodec";

const FP = `sha256:${"a".repeat(64)}`;

function requestWire(overrides: Record<string, unknown> = {}) {
  return {
    schema: AUTHORING_PREVIEW_REQUEST_SCHEMA,
    requestId: "request-1",
    workspaceHandle: "authoring-1",
    referenceRevision: 4,
    timelineRevision: 7,
    timelineContentFingerprint: FP,
    clipId: "clip-1",
    ...overrides,
  };
}

describe("authoring preview v1 codec", () => {
  it("freezes and round-trips the exact logical request", () => {
    const request = decodeAuthoringPreviewRequest(requestWire());
    expect(Object.isFrozen(request)).toBe(true);
    expect(encodeAuthoringPreviewRequest(request)).toEqual(requestWire());
    expect(Object.keys(encodeAuthoringPreviewRequest(request)).sort()).toEqual(
      Object.keys(requestWire()).sort(),
    );
  });

  it.each(["path", "url", "bytes", "duration", "mime", "sourceRange"])(
    "rejects private or caller-selected field %s",
    (field) => {
      expect(() =>
        decodeAuthoringPreviewRequest(requestWire({ [field]: "private" })),
      ).toThrow(/closed/);
    },
  );

  it("rejects unsupported schemas, malformed identities, booleans and nested values", () => {
    expect(() =>
      decodeAuthoringPreviewRequest(requestWire({ schema: "preview.v2" })),
    ).toThrow(/schema/);
    expect(() =>
      decodeAuthoringPreviewRequest(requestWire({ requestId: "" })),
    ).toThrow(/request/);
    expect(() =>
      decodeAuthoringPreviewRequest(requestWire({ referenceRevision: true })),
    ).toThrow(/invalid/);
    expect(() =>
      decodeAuthoringPreviewRequest(requestWire({ clipId: ["clip-1"] })),
    ).toThrow(/clip/);
  });

  it("decodes only the closed content-free capability states", () => {
    expect(
      decodeAuthoringPreviewCapability({
        schema: AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
        available: true,
        reason: null,
      }),
    ).toEqual({
      schema: AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
      available: true,
      reason: null,
    });
    expect(
      decodeAuthoringPreviewCapability({
        schema: AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
        available: false,
        reason: "not_bound",
      }),
    ).toEqual({
      schema: AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
      available: false,
      reason: "not_bound",
    });
    expect(() =>
      decodeAuthoringPreviewCapability({
        schema: AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
        available: true,
        reason: "stale",
      }),
    ).toThrow(/reason/);
  });

  it("decodes success identity and bounded error without raw detail", () => {
    const success = decodeAuthoringPreviewSuccess({
      ...requestWire(),
      schema: AUTHORING_PREVIEW_SUCCESS_SCHEMA,
      sourceId: "video-1",
    });
    expect(Object.isFrozen(success)).toBe(true);
    expect(success.sourceId).toBe("video-1");

    expect(
      decodeAuthoringPreviewError({
        schema: AUTHORING_PREVIEW_ERROR_SCHEMA,
        requestId: null,
        reason: "invalid_request",
      }),
    ).toEqual({
      schema: AUTHORING_PREVIEW_ERROR_SCHEMA,
      requestId: null,
      reason: "invalid_request",
    });
    expect(() =>
      decodeAuthoringPreviewError({
        schema: AUTHORING_PREVIEW_ERROR_SCHEMA,
        requestId: null,
        reason: "internal_failure",
        stderr: "private",
      }),
    ).toThrow(/closed/);
  });

  it("pins all four schema identifiers", () => {
    expect(AUTHORING_PREVIEW_REQUEST_SCHEMA).toBe(
      "h3.context.authoring_source_preview.request.v1",
    );
    expect(AUTHORING_PREVIEW_CAPABILITY_SCHEMA).toBe(
      "h3.context.authoring_source_preview.capability.v1",
    );
    expect(AUTHORING_PREVIEW_SUCCESS_SCHEMA).toBe(
      "h3.context.authoring_source_preview.success.v1",
    );
    expect(AUTHORING_PREVIEW_ERROR_SCHEMA).toBe(
      "h3.context.authoring_source_preview.error.v1",
    );
  });
});
