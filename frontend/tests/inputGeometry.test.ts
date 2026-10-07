import { describe, expect, it, vi } from "vitest";

import {
  INPUT_GEOMETRY_ERROR_SCHEMA,
  INPUT_GEOMETRY_RECEIPT_SCHEMA,
  INPUT_GEOMETRY_REQUEST_SCHEMA,
  INPUT_GEOMETRY_ROUTE,
  InputGeometryClientError,
  createInputGeometryClient,
  type InputGeometryRequest,
} from "../src/host/inputGeometry";

const sourceFingerprint = "sha256:" + "b".repeat(64);
const receiptHandle = "ig_" + "c".repeat(40);

const request: InputGeometryRequest = Object.freeze({
  schema: INPUT_GEOMETRY_REQUEST_SCHEMA,
  locator: "portrait/source.jpg",
});

const receipt = Object.freeze({
  schema: INPUT_GEOMETRY_RECEIPT_SCHEMA,
  receipt_handle: receiptHandle,
  source_fingerprint: sourceFingerprint,
});

function response(
  value: unknown,
  options: Readonly<{ ok?: boolean; status?: number }> = {},
) {
  return {
    ok: options.ok ?? true,
    status: options.status ?? 200,
    text: vi.fn().mockResolvedValue(JSON.stringify(value)),
  };
}

describe("M23-29 source identity client", () => {
  it("sends one exact same-origin request and decodes the closed receipt", async () => {
    const fetchApi = vi.fn().mockResolvedValue(response(receipt));
    const signal = new AbortController().signal;

    const observed = await createInputGeometryClient({ fetchApi }).observe(
      request,
      signal,
    );

    expect(observed).toEqual(receipt);
    expect(Object.isFrozen(observed)).toBe(true);
    expect(fetchApi).toHaveBeenCalledOnce();
    const [route, init] = fetchApi.mock.calls[0]!;
    expect(route).toBe(INPUT_GEOMETRY_ROUTE);
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
      },
      signal,
    });
    expect(JSON.parse(String(init.body))).toEqual(request);
  });

  it.each([
    { ...receipt, foreign_field: "opaque-media-marker" },
    { ...receipt, width: 32 },
    { ...receipt, height: 8193 },
  ])("rejects a foreign or unbounded receipt %#", async (wire) => {
    const client = createInputGeometryClient({
      fetchApi: vi.fn().mockResolvedValue(response(wire)),
    });

    await expect(client.observe(request)).rejects.toMatchObject({
      name: "InputGeometryClientError",
      category: "payload_rejected",
      status: 200,
    });
  });

  it("preserves only an allow-listed privacy-safe backend category", async () => {
    const safeClient = createInputGeometryClient({
      fetchApi: vi.fn().mockResolvedValue(
        response(
          {
            schema: INPUT_GEOMETRY_ERROR_SCHEMA,
            category: "input_changed",
          },
          { ok: false, status: 409 },
        ),
      ),
    });
    await expect(safeClient.observe(request)).rejects.toMatchObject({
      name: "InputGeometryClientError",
      category: "input_changed",
      status: 409,
      message: "input_changed",
    });

    const unsafeClient = createInputGeometryClient({
      fetchApi: vi.fn().mockResolvedValue(
        response(
          {
            schema: INPUT_GEOMETRY_ERROR_SCHEMA,
            category: "opaque-media-marker must not cross",
          },
          { ok: false, status: 422 },
        ),
      ),
    });
    await expect(unsafeClient.observe(request)).rejects.toEqual(
      new InputGeometryClientError("route_rejected", 422),
    );
  });

  it.each([
    { ...request, locator: "../source.jpg" },
    { ...request, locator: "C:/source.jpg" },
    { ...request, locator: "folder/source.jpg." },
    { ...request, locator: "CON.jpg" },
    { ...request, locator: "folder/source?.jpg" },
    { ...request, upscale_method: "foreign" },
    { ...request, megapixels: Number.NaN },
    { ...request, resolution_steps: 0 },
    { ...request, foreign: true },
  ])("refuses an invalid request before transport %#", async (wire) => {
    const fetchApi = vi.fn();
    await expect(
      createInputGeometryClient({ fetchApi }).observe(
        wire as InputGeometryRequest,
      ),
    ).rejects.toMatchObject({ category: "invalid_request", status: 0 });
    expect(fetchApi).not.toHaveBeenCalled();
  });
});
