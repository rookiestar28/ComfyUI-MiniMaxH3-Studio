import { describe, expect, it, vi } from "vitest";
import {
  decodeRetainedAssetsResponse,
  encodeRetainedAssetsAction,
} from "../src/contracts/retainedAssetsCodec";
import { createRetainedAssetsClient } from "../src/host/retainedAssetsClient";
import {
  assetId,
  retainedFailureWire,
  retainedResponse,
  retainedWire,
  restoredWire,
  useHandle,
} from "./retainedAssetFixtures";

describe("retained assets closed wire and client", () => {
  it("accepts only bounded safe inventory and fresh resource fields", () => {
    const value = decodeRetainedAssetsResponse(restoredWire());
    expect(value.restored?.use_handle).toBe(useHandle);
    expect(Object.isFrozen(value.projection.assets[0])).toBe(true);
    for (const field of [
      "owner_id",
      "path",
      "source_id",
      "receipt",
      "content_fingerprint",
    ]) {
      const wire = retainedWire();
      Object.assign(wire.projection.assets[0]!, { [field]: "private" });
      expect(() => decodeRetainedAssetsResponse(wire)).toThrow();
    }
  });
  it("refuses aliases, inconsistent counts, bounds and invented error codes", () => {
    for (const alter of [
      (row: ReturnType<typeof retainedWire>) => {
        row.projection.count = 2;
      },
      (row: ReturnType<typeof retainedWire>) => {
        row.projection.charged_bytes = -1;
      },
      (row: ReturnType<typeof retainedWire>) => {
        row.projection.charged_bytes = 256 * 1024 * 1024 + 1;
      },
      (row: ReturnType<typeof retainedWire>) => {
        row.projection.charged_bytes = 0.5;
      },
      (row: ReturnType<typeof retainedWire>) => {
        Object.assign(row.projection, { charged_bytes: "0" });
      },
      (row: ReturnType<typeof retainedWire>) => {
        row.projection.assets[0]!.width = 2049;
      },
      (row: ReturnType<typeof retainedWire>) => {
        row.projection.assets[0]!.frame_count = 513;
      },
      (row: ReturnType<typeof retainedWire>) => {
        row.projection.assets.push(row.projection.assets[0]!);
        row.projection.count = 2;
      },
      (row: ReturnType<typeof retainedWire>) => {
        Object.assign(row, { error: "private-error" });
      },
    ]) {
      const wire = retainedWire();
      alter(wire);
      expect(() => decodeRetainedAssetsResponse(wire)).toThrow();
    }
    expect(() =>
      encodeRetainedAssetsAction({
        intent: "status",
        path: "private",
      } as never),
    ).toThrow();
    expect(() =>
      encodeRetainedAssetsAction({
        intent: "clear",
        expected_revision: true,
      } as never),
    ).toThrow();
  });
  it.each([0, 100, 2000])(
    "preserves missing-copy catalog metadata with physical charge %i",
    (charged) => {
      const wire = retainedWire();
      wire.projection.charged_bytes = charged;
      const result = decodeRetainedAssetsResponse(wire);
      expect(result.projection.assets[0]?.byte_length).toBe(1000);
      expect(result.projection.charged_bytes).toBe(charged);
      expect(result.projection.count).toBe(1);
      expect(result.restored).toBe(null);
    },
  );
  it.each(["status", "list", "release", "set_enabled"] as const)(
    "decodes %s reconciliation when a copy is absent without issuing use authority",
    async (intent) => {
      const wire = retainedWire();
      wire.projection.charged_bytes = 100;
      const fetchApi = vi.fn(async () => retainedResponse(wire));
      const action =
        intent === "release"
          ? { intent, use_handle: useHandle }
          : intent === "set_enabled"
            ? { intent, enabled: true, expected_revision: 2 }
            : { intent };
      const result = await createRetainedAssetsClient({ fetchApi }).send(
        action,
      );
      expect(result).toMatchObject({
        ok: true,
        response: {
          projection: { count: 1, charged_bytes: 100 },
          restored: null,
          retained_id: null,
        },
      });
      expect(fetchApi).toHaveBeenCalledTimes(1);
    },
  );
  it("classifies known missing-copy restore as unavailable without replay", async () => {
    const fetchApi = vi.fn(async () =>
      retainedResponse(retainedFailureWire("asset_unavailable"), 404),
    );
    expect(
      await createRetainedAssetsClient({ fetchApi }).send({
        intent: "restore",
        asset_id: assetId,
        expected_revision: 2,
      }),
    ).toEqual({
      ok: false,
      code: "asset_unavailable",
      outcomeUnknown: false,
    });
    expect(fetchApi).toHaveBeenCalledTimes(1);
  });
  it.each(["clear", "collect"] as const)(
    "accepts protected %s when physical charge is below catalog lengths",
    async (intent) => {
      const wire = retainedWire();
      wire.projection.charged_bytes = 100;
      wire.projection.protected = 1;
      wire.projection.assets[0]!.referenced = true;
      Object.assign(wire.projection.assets[0]!, { closed_at_ms: null });
      const fetchApi = vi.fn(async () =>
        retainedResponse({ ...wire, cleanup: { removed: 0, protected: 1 } }),
      );
      expect(
        await createRetainedAssetsClient({ fetchApi }).send({
          intent,
          expected_revision: 2,
        }),
      ).toMatchObject({
        ok: true,
        response: {
          projection: { count: 1, charged_bytes: 100, protected: 1 },
          cleanup: { removed: 0, protected: 1 },
          restored: null,
        },
      });
      expect(fetchApi).toHaveBeenCalledTimes(1);
    },
  );
  it("restores one healthy row when another copy is absent without inferring authority from charge", async () => {
    const wire = restoredWire();
    const healthy = wire.projection.assets[0]!;
    healthy.byte_length = 32;
    wire.projection.assets.push({
      ...healthy,
      asset_id: "asset_" + "c".repeat(32),
      byte_length: 1000,
    });
    wire.projection.count = 2;
    wire.projection.charged_bytes = 100;
    const fetchApi = vi.fn(async () => retainedResponse(wire));
    expect(
      await createRetainedAssetsClient({ fetchApi }).send({
        intent: "restore",
        asset_id: assetId,
        expected_revision: 2,
      }),
    ).toMatchObject({
      ok: true,
      response: {
        projection: { count: 2, charged_bytes: 100 },
        restored: { asset_id: assetId, use_handle: useHandle },
      },
    });
    expect(fetchApi).toHaveBeenCalledTimes(1);
  });
  it("posts exact CAS with no replay and classifies lost restore as unknown", async () => {
    const fetchApi = vi.fn(async (_path: string, init: RequestInit) => {
      if (JSON.parse(init.body as string).intent === "restore")
        throw new Error("lost acknowledgement");
      return retainedResponse(retainedWire());
    });
    const client = createRetainedAssetsClient({ fetchApi });
    expect(
      await client.send({
        intent: "restore",
        asset_id: assetId,
        expected_revision: 2,
      }),
    ).toEqual({ ok: false, code: "transport_failure", outcomeUnknown: true });
    expect(fetchApi).toHaveBeenCalledTimes(1);
    expect(fetchApi.mock.calls[0]![1]).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
  });
  it("releases a known mismatched restore resource without replay or preview", async () => {
    const fetchApi = vi.fn(async (_path: string, init: RequestInit) =>
      retainedResponse(
        JSON.parse(init.body as string).intent === "release"
          ? retainedWire()
          : restoredWire(),
      ),
    );
    const client = createRetainedAssetsClient({ fetchApi });
    const result = await client.send({
      intent: "restore",
      asset_id: "asset_" + "c".repeat(32),
      expected_revision: 2,
    });
    expect(result).toEqual({
      ok: false,
      code: "malformed_response",
      outcomeUnknown: true,
    });
    expect(fetchApi).toHaveBeenCalledTimes(2);
    expect(fetchApi.mock.calls[1]![0]).toBe("/h3-context/retained-assets");
    expect(JSON.parse(fetchApi.mock.calls[1]![1].body as string)).toEqual({
      intent: "release",
      use_handle: useHandle,
    });
    expect(fetchApi.mock.calls[1]![1].signal).toBeUndefined();
  });
  it("reads explicit binary preview with bounded headers, no URL allocation and abort checks", async () => {
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) =>
      retainedResponse(null, 200, true),
    );
    const client = createRetainedAssetsClient({ fetchApi });
    const allocate = vi.spyOn(URL, "createObjectURL");
    const result = await client.preview(
      useHandle,
      new AbortController().signal,
    );
    expect(result.ok).toBe(true);
    expect(allocate).not.toHaveBeenCalled();
    expect(fetchApi.mock.calls[0]![0]).toBe(
      "/h3-context/retained-assets/preview",
    );
    const bad = retainedResponse(null, 200, true);
    bad.headers.set("content-length", String(8 * 1024 * 1024 + 1));
    const cancel = vi.spyOn(bad.body!, "cancel");
    expect(
      await createRetainedAssetsClient({ fetchApi: async () => bad }).preview(
        useHandle,
        new AbortController().signal,
      ),
    ).toMatchObject({ ok: false, code: "malformed_response" });
    expect(cancel).toHaveBeenCalled();
  });
});
