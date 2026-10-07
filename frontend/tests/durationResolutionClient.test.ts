import { describe, expect, it, vi } from "vitest";

import { canonicalDurationResolutions } from "../src/contracts/generatedDurationResolution";
import {
  DURATION_RESOLUTION_REQUEST_SCHEMA,
  DURATION_RESOLUTION_RESPONSE_SCHEMA,
  DURATION_RESOLUTION_ROUTE,
  createDurationResolutionClient,
} from "../src/host/durationResolutionClient";

const exactEight = canonicalDurationResolutions.find(
  (row) => row.requested_seconds === 8,
)!;
const exactFifteen = canonicalDurationResolutions.find(
  (row) => row.requested_seconds === 15,
)!;

describe("duration resolution client", () => {
  it("posts the closed integer request and carries the exact backend response", async () => {
    const fetchApi = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(exactEight),
    });
    const client = createDurationResolutionClient({ fetchApi });

    await expect(client.resolve(8)).resolves.toEqual(exactEight);
    expect(fetchApi).toHaveBeenCalledWith(
      DURATION_RESOLUTION_ROUTE,
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        headers: {
          accept: "application/json",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          schema: DURATION_RESOLUTION_REQUEST_SCHEMA,
          requested_seconds: 8,
        }),
      }),
    );
  });

  it("accepts the canonical maximum even when delivered time exceeds authored time", async () => {
    const client = createDurationResolutionClient({
      fetchApi: async () => ({
        ok: true,
        status: 200,
        text: async () => JSON.stringify(exactFifteen),
      }),
    });

    await expect(client.resolve(15)).resolves.toEqual(exactFifteen);
  });

  it.each(canonicalDurationResolutions)(
    "accepts the generated $requested_seconds-second canonical tuple",
    async (canonical) => {
      const client = createDurationResolutionClient({
        fetchApi: async () => ({
          ok: true,
          status: 200,
          text: async () => JSON.stringify(canonical),
        }),
      });
      await expect(
        client.resolve(canonical.requested_seconds),
      ).resolves.toEqual(canonical);
    },
  );

  it.each([
    { ...exactFifteen, schema: "wrong" },
    { ...exactFifteen, requested_seconds: 14 },
    { ...exactFifteen, requested_milliseconds: 14999 },
    { ...exactFifteen, effective_milliseconds: 15082 },
    { ...exactFifteen, frame_count: 345 },
    { ...exactFifteen, snapped: false },
    { ...exactFifteen, extra: true },
  ])("rejects a malformed or mismatched closed response", async (body) => {
    const client = createDurationResolutionClient({
      fetchApi: async () => ({
        ok: true,
        status: 200,
        text: async () => JSON.stringify(body),
      }),
    });
    await expect(client.resolve(15)).rejects.toMatchObject({
      name: "DurationResolutionClientError",
      failure: "payload_rejected",
    });
  });

  it("refuses an unavailable seam and an invalid authored integer without a request", async () => {
    await expect(
      createDurationResolutionClient({}).resolve(8),
    ).rejects.toMatchObject({ failure: "seam_unavailable" });

    const fetchApi = vi.fn();
    const client = createDurationResolutionClient({ fetchApi });
    for (const invalid of [8.5, 16])
      await expect(client.resolve(invalid)).rejects.toMatchObject({
        failure: "invalid_request",
      });
    expect(fetchApi).not.toHaveBeenCalled();
  });
});
