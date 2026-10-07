import { describe, expect, it, vi } from "vitest";

import { AUTHORING_PREVIEW_REQUEST_SCHEMA } from "../src/contracts/authoringPreviewCodec";
import {
  AUTHORING_MEDIA_PREVIEW_ROUTE,
  MAX_AUTHORING_MEDIA_PREVIEW_BYTES,
  createAuthoringMediaPreviewClient,
} from "../src/host/authoringMediaPreview";

const request = {
  schema: AUTHORING_PREVIEW_REQUEST_SCHEMA,
  requestId: "request-1",
  workspaceHandle: "authoring-1",
  referenceRevision: 4,
  timelineRevision: 7,
  timelineContentFingerprint: `sha256:${"a".repeat(64)}`,
  clipId: "clip-1",
} as const;

function response(payload: Uint8Array, audio = "present_bound"): Response {
  const body = new ReadableStream<Uint8Array>({
    type: "bytes",
    pull(controller) {
      const request = (controller as ReadableByteStreamController).byobRequest;
      if (request === null) return;
      const view = request.view as Uint8Array;
      view.set(payload);
      request.respond(payload.byteLength);
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: {
      "Content-Type": "video/mp4",
      "Content-Length": String(payload.byteLength),
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Content-Disposition": 'inline; filename="h3-authoring-preview.mp4"',
      "X-H3-Context-Embedded-Audio": audio,
    },
  });
}

function errorResponse(payload: string, status: number): Response {
  const bytes = new TextEncoder().encode(payload);
  const value = response(bytes);
  value.headers.set("Content-Type", "application/json");
  value.headers.delete("Content-Disposition");
  value.headers.delete("X-H3-Context-Embedded-Audio");
  return new Response(value.body, {
    status,
    headers: value.headers,
  });
}

describe("authoring media preview client", () => {
  it("posts only the closed identity and returns Blob plus audio disposition", async () => {
    const objectUrl = vi.spyOn(URL, "createObjectURL");
    const fetchApi = vi.fn(async (_route: string, _init: RequestInit) =>
      response(new Uint8Array([1, 2, 3])),
    );
    const client = createAuthoringMediaPreviewClient({ fetchApi });
    const result = await client.open(request, new AbortController().signal);

    expect(AUTHORING_MEDIA_PREVIEW_ROUTE).toBe(
      "/h3-context/v1/authoring/media-preview",
    );
    expect(result.audioDisposition).toBe("present_bound");
    expect(await result.blob.arrayBuffer()).toEqual(
      new Uint8Array([1, 2, 3]).buffer,
    );
    expect(JSON.parse(fetchApi.mock.calls[0]![1].body as string)).toEqual(
      request,
    );
    expect(objectUrl).not.toHaveBeenCalled();
  });

  it("decodes only bounded typed errors and never exposes raw response text", async () => {
    const errorBody = JSON.stringify({
      schema: "h3.context.authoring_source_preview.error.v1",
      requestId: "request-1",
      reason: "stale",
    });
    const fetchApi = vi.fn(async () => errorResponse(errorBody, 409));
    const client = createAuthoringMediaPreviewClient({ fetchApi });
    await expect(
      client.open(request, new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "stale", status: 409 });
  });

  it("fails closed when an error reason does not match its HTTP status", async () => {
    const errorBody = JSON.stringify({
      schema: "h3.context.authoring_source_preview.error.v1",
      requestId: "request-1",
      reason: "stale",
    });
    const fetchApi = vi.fn(async () => errorResponse(errorBody, 500));
    const client = createAuthoringMediaPreviewClient({ fetchApi });
    await expect(
      client.open(request, new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "failed", status: 500 });
  });

  it.each([
    ["authority_mismatch", 404, "unavailable"],
    ["stale", 409, "stale"],
    ["unsupported", 422, "unsupported"],
    ["source_too_large", 413, "too_large"],
    ["source_too_long", 422, "too_long"],
    ["busy", 429, "busy"],
    ["cancelled", 499, "cancelled"],
    ["timeout", 504, "timeout"],
    ["conversion_failed", 422, "failed"],
    ["internal_failure", 500, "failed"],
  ] as const)(
    "maps the closed %s error without exposing its body",
    async (reason, status, expected) => {
      const errorBody = JSON.stringify({
        schema: "h3.context.authoring_source_preview.error.v1",
        requestId: "request-1",
        reason,
      });
      const client = createAuthoringMediaPreviewClient({
        fetchApi: async () => errorResponse(errorBody, status),
      });
      await expect(
        client.open(request, new AbortController().signal),
      ).rejects.toMatchObject({ disposition: expected, status });
    },
  );

  it("cancels malformed success and error responses before failing closed", async () => {
    const malformedSuccess = response(new Uint8Array([1]));
    malformedSuccess.headers.set("Location", "/not-allowed");
    const successCancel = vi.spyOn(malformedSuccess.body!, "cancel");
    const successClient = createAuthoringMediaPreviewClient({
      fetchApi: async () => malformedSuccess,
    });
    await expect(
      successClient.open(request, new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "failed", status: 200 });
    expect(successCancel).toHaveBeenCalledOnce();

    const malformedError = errorResponse(
      JSON.stringify({
        schema: "h3.context.authoring_source_preview.error.v1",
        requestId: "request-1",
        reason: "stale",
      }),
      409,
    );
    malformedError.headers.delete("Cache-Control");
    const errorCancel = vi.spyOn(malformedError.body!, "cancel");
    const errorClient = createAuthoringMediaPreviewClient({
      fetchApi: async () => malformedError,
    });
    await expect(
      errorClient.open(request, new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "failed", status: 409 });
    expect(errorCancel).toHaveBeenCalledOnce();
  });

  it("enforces declared and actual body bounds with exact BYOB cleanup", async () => {
    const declaredOversize = response(new Uint8Array([1]));
    declaredOversize.headers.set(
      "Content-Length",
      String(MAX_AUTHORING_MEDIA_PREVIEW_BYTES + 1),
    );
    const declaredCancel = vi.spyOn(declaredOversize.body!, "cancel");
    const declaredClient = createAuthoringMediaPreviewClient({
      fetchApi: async () => declaredOversize,
    });
    await expect(
      declaredClient.open(request, new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "too_large", status: 200 });
    expect(declaredCancel).toHaveBeenCalledOnce();

    const actualOversize = response(new Uint8Array([1, 2]));
    actualOversize.headers.set("Content-Length", "1");
    const actualClient = createAuthoringMediaPreviewClient({
      fetchApi: async () => actualOversize,
    });
    await expect(
      actualClient.open(request, new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "too_large", status: 200 });

    const shortBody = response(new Uint8Array([1]));
    shortBody.headers.set("Content-Length", "2");
    const shortClient = createAuthoringMediaPreviewClient({
      fetchApi: async () => shortBody,
    });
    await expect(
      shortClient.open(request, new AbortController().signal),
    ).rejects.toMatchObject({ disposition: "failed", status: 200 });
  });

  it("maps an untrusted transport exception to one content-free failure", async () => {
    const client = createAuthoringMediaPreviewClient({
      fetchApi: async () => {
        throw new TypeError("private browser transport detail");
      },
    });
    await expect(
      client.open(request, new AbortController().signal),
    ).rejects.toMatchObject({
      message: "failed",
      disposition: "failed",
      status: 0,
    });
  });

  it("aborts the previous request and suppresses its late response", async () => {
    let settleFirst: ((value: Response) => void) | undefined;
    const first = new Promise<Response>((resolve) => {
      settleFirst = resolve;
    });
    const fetchApi = vi
      .fn<(route: string, init: RequestInit) => Promise<Response>>()
      .mockImplementationOnce(async (_route, init) => {
        expect((init.signal as AbortSignal).aborted).toBe(false);
        return first;
      })
      .mockImplementationOnce(async () => response(new Uint8Array([9])));
    const client = createAuthoringMediaPreviewClient({ fetchApi });
    const oldRequest = client.open(request, new AbortController().signal);
    const newest = client.open(
      { ...request, requestId: "request-2" },
      new AbortController().signal,
    );
    settleFirst?.(response(new Uint8Array([1])));

    await expect(oldRequest).rejects.toMatchObject({
      disposition: "cancelled",
    });
    expect((await newest).audioDisposition).toBe("present_bound");
    client.close();
  });
});
