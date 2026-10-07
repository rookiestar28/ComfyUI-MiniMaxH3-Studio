import { describe, expect, it, vi } from "vitest";

import type { ProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  MAX_MEDIA_PREVIEW_BYTES,
  MEDIA_PREVIEW_REQUEST_SCHEMA,
  createProductionMediaPreviewClient,
  encodeMediaPreviewRequest,
} from "../src/host/productionMediaPreview";

const projection = {
  schema: "h3.context.production_workbench_projection.v1",
  workspaceHandle: `pw_${"a".repeat(40)}`,
  workspaceId: "workspace.preview",
  workspaceRevision: 3,
  workspaceFingerprint: `sha256:${"b".repeat(64)}`,
  segments: [],
  selectedSegmentIds: [],
  run: { state: "succeeded", completed: 1, total: 1 },
  generationSequence: null,
  reconstruction: { state: "complete" },
  authorityVersions: [],
  outputs: [
    {
      outputHandle: `out_${"c".repeat(40)}`,
      ordinal: 1,
      state: "ready",
      segmentId: null,
      preview: true,
    },
  ],
  allowedActions: ["preview_output"],
  blockerCodes: [],
  limits: { maxSegments: 64, maxOutputs: 65 },
} as unknown as ProductionWorkbenchProjection;

function byobResponse(payload: Uint8Array, onCancel?: () => void): Response {
  let offset = 0;
  const stream = new ReadableStream<Uint8Array>({
    type: "bytes" as const,
    pull(controller) {
      const byteController =
        controller as unknown as ReadableByteStreamController;
      const request = byteController.byobRequest;
      if (request === null) throw new Error("BYOB request required");
      if (offset === payload.length) {
        byteController.close();
        return;
      }
      const view = request.view;
      if (view === null) throw new Error("BYOB view required");
      new Uint8Array(view.buffer, view.byteOffset, 1)[0] = payload[offset++]!;
      request.respond(1);
      if (offset === payload.length) byteController.close();
    },
    cancel() {
      onCancel?.();
    },
  } as UnderlyingSource<Uint8Array>);
  return new Response(stream, {
    status: 200,
    headers: {
      "Content-Type": "video/mp4",
      "Content-Length": String(payload.length),
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Content-Disposition": 'inline; filename="h3-preview.mp4"',
    },
  });
}

describe("production media preview client", () => {
  it("encodes only the five exact opaque identity fields", () => {
    const wire = JSON.parse(
      encodeMediaPreviewRequest(projection, projection.outputs[0]!),
    );
    expect(wire).toEqual({
      schema: MEDIA_PREVIEW_REQUEST_SCHEMA,
      workspace_handle: projection.workspaceHandle,
      expected_workspace_revision: projection.workspaceRevision,
      expected_workspace_fingerprint: projection.workspaceFingerprint,
      output_handle: projection.outputs[0]!.outputHandle,
    });
  });

  it("packs repeated one-byte BYOB settlements into one exact Blob", async () => {
    const payload = new Uint8Array([0, 1, 2, 3, 4, 5, 6]);
    const fetchApi = vi.fn(async (_route: string, _init: RequestInit) =>
      byobResponse(payload),
    );
    const client = createProductionMediaPreviewClient({ fetchApi });
    const blob = await client.open(
      projection,
      projection.outputs[0]!,
      new AbortController().signal,
    );

    expect(fetchApi).toHaveBeenCalledOnce();
    expect(blob.type).toBe("video/mp4");
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(payload);
    const firstCall = fetchApi.mock.calls[0];
    expect(firstCall).toBeDefined();
    expect(JSON.parse(String(firstCall![1].body))).toEqual(
      JSON.parse(encodeMediaPreviewRequest(projection, projection.outputs[0]!)),
    );
  });

  it.each([
    [400, "invalid_request"],
    [403, "forbidden"],
    [404, "unavailable"],
    [409, "stale"],
    [410, "gone"],
    [413, "too_large"],
    [415, "unsupported"],
    [422, "unavailable"],
    [423, "busy"],
    [504, "timed_out"],
    [500, "failed"],
  ] as const)("maps status %i to %s", async (status, disposition) => {
    const client = createProductionMediaPreviewClient({
      fetchApi: async () => new Response(null, { status }),
    });
    await expect(
      client.open(
        projection,
        projection.outputs[0]!,
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({
      disposition,
      status,
    });
  });

  it("rejects an oversized declared body before allocating or reading", async () => {
    const cancelled = vi.fn();
    const response = byobResponse(new Uint8Array([1]), cancelled);
    response.headers.set("Content-Length", String(MAX_MEDIA_PREVIEW_BYTES + 1));
    const client = createProductionMediaPreviewClient({
      fetchApi: async () => response,
    });
    await expect(
      client.open(
        projection,
        projection.outputs[0]!,
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ disposition: "too_large" });
    expect(cancelled).toHaveBeenCalledOnce();
  });

  it("tears down a streaming body rejected before BYOB admission", async () => {
    const cancelled = vi.fn();
    const response = byobResponse(new Uint8Array([1]), cancelled);
    response.headers.set("Content-Type", "application/octet-stream");
    const client = createProductionMediaPreviewClient({
      fetchApi: async () => response,
    });
    await expect(
      client.open(
        projection,
        projection.outputs[0]!,
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ disposition: "failed" });
    expect(cancelled).toHaveBeenCalledOnce();
  });

  it("cancels and unlocks the BYOB reader when accumulator allocation fails", async () => {
    const cancelled = vi.fn();
    const response = byobResponse(new Uint8Array([1]), cancelled);
    const NativeUint8Array = Uint8Array;
    vi.stubGlobal(
      "Uint8Array",
      new Proxy(NativeUint8Array, {
        construct(target, argumentsList, newTarget) {
          if (argumentsList[0] === 1) throw new RangeError("allocation failed");
          return Reflect.construct(target, argumentsList, newTarget);
        },
      }),
    );
    const client = createProductionMediaPreviewClient({
      fetchApi: async () => response,
    });

    try {
      await expect(
        client.open(
          projection,
          projection.outputs[0]!,
          new AbortController().signal,
        ),
      ).rejects.toThrow("allocation failed");
      expect(cancelled).toHaveBeenCalledOnce();
      expect(response.body?.locked).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
