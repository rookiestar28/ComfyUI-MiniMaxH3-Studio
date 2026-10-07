export const assetId = "asset_" + "a".repeat(32);
export const useHandle = "retained_" + "b".repeat(32);
export function retainedWire(enabled = true) {
  return {
    schema: "h3.context.retained_assets_response.v1",
    error: null,
    retained_id: null,
    restored: null,
    cleanup: null,
    projection: {
      schema: "h3.context.retained_assets_status.v1",
      supported: true,
      enabled,
      revision: 2,
      count: 1,
      charged_bytes: 2000,
      remnant_count: 0,
      protected: 0,
      assets: [
        {
          asset_id: assetId,
          byte_length: 1000,
          width: 320,
          height: 240,
          frame_count: 24,
          duration_ms: 2000,
          created_at_ms: 1791300000000,
          closed_at_ms: 1791300000000,
          referenced: false,
          state: "retained",
        },
      ],
    },
  };
}

export function restoredWire(previewAvailable = true) {
  return {
    ...retainedWire(),
    restored: {
      asset_id: assetId,
      use_handle: useHandle,
      width: 320,
      height: 240,
      frame_count: 24,
      preview_available: previewAvailable,
    },
  };
}

export function retainedFailureWire(code: string) {
  return {
    ...retainedWire(false),
    error: code,
    projection: {
      schema: "h3.context.retained_assets_status.v1",
      supported: false,
      enabled: false,
      revision: 0,
      count: 0,
      charged_bytes: 0,
      remnant_count: 0,
      protected: 0,
      assets: [],
    },
  };
}

export function retainedResponse(
  value: unknown,
  status = 200,
  media = false,
): Response {
  const payload = media
    ? new Uint8Array([1, 2, 3])
    : new TextEncoder().encode(JSON.stringify(value));
  const body = new ReadableStream<Uint8Array>({
    type: "bytes",
    pull(controller) {
      const request = (controller as ReadableByteStreamController).byobRequest!;
      (request.view as Uint8Array).set(payload);
      request.respond(payload.byteLength);
      controller.close();
    },
  });
  return new Response(body, {
    status,
    headers: {
      "content-type": media ? "video/mp4" : "application/json",
      "content-length": String(payload.byteLength),
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      ...(media
        ? {
            "content-disposition": 'inline; filename="h3-retained-preview.mp4"',
            "x-h3-context-embedded-audio": "absent",
          }
        : {}),
    },
  });
}
