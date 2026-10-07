import { describe, expect, it, vi } from "vitest";
import { createRetainedAssetsClient } from "../src/host/retainedAssetsClient";
import { createRetainedAssetsSession } from "../src/lifecycle/retainedAssetsSession";
import { decodeRetainedAssetsResponse } from "../src/contracts/retainedAssetsCodec";
import {
  assetId,
  retainedFailureWire,
  retainedResponse,
  retainedWire,
  restoredWire,
  useHandle,
} from "./retainedAssetFixtures";

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
function fixture() {
  const fetchApi = vi.fn(async (path: string, init: RequestInit) => {
    const action = JSON.parse(init.body as string);
    return path.endsWith("/preview")
      ? retainedResponse(null, 200, true)
      : retainedResponse(
          action.intent === "restore" ? restoredWire() : retainedWire(),
        );
  });
  const client = { ...createRetainedAssetsClient({ fetchApi }) };
  const session = createRetainedAssetsSession({ client, changed: vi.fn() });
  return { session, fetchApi, client };
}

describe("retained asset explicit UI lifecycle", () => {
  it("reconciles missing-copy status, refuses restore and allows only explicit refreshed clear", async () => {
    const missing = retainedWire();
    missing.projection.charged_bytes = 100;
    const commands: unknown[] = [];
    const fetchApi = vi.fn(async (path: string, init: RequestInit) => {
      expect(path).toBe("/h3-context/retained-assets");
      const action = JSON.parse(init.body as string);
      commands.push(action);
      if (action.intent === "restore")
        return retainedResponse(retainedFailureWire("asset_unavailable"), 404);
      if (action.intent === "clear")
        return retainedResponse({
          ...missing,
          cleanup: { removed: 1, protected: 0 },
          projection: {
            ...missing.projection,
            revision: 3,
            count: 0,
            assets: [],
          },
        });
      return retainedResponse(missing);
    });
    const session = createRetainedAssetsSession({
      client: createRetainedAssetsClient({ fetchApi }),
      changed: vi.fn(),
    });
    session.binding().ensure();
    await settle();
    expect(session.binding().state.confirmed).toBe(true);
    expect(session.binding().state.projection?.assets[0]?.byte_length).toBe(
      1000,
    );
    session.binding().select(assetId);
    await session.binding().restore();
    expect(session.binding().state.error).toBe("asset_unavailable");
    expect(session.binding().state.restored).toBe(null);
    expect(session.binding().state.confirmed).toBe(false);
    await session.binding().clear();
    expect(commands).toHaveLength(2);
    await session.binding().refresh();
    expect(session.binding().state.confirmed).toBe(true);
    await session.binding().clear();
    expect(session.binding().state.cleanup).toEqual({
      removed: 1,
      protected: 0,
    });
    expect(session.binding().state.projection?.count).toBe(0);
    expect(session.binding().state.restored).toBe(null);
    expect(commands).toEqual([
      { intent: "status" },
      { intent: "restore", asset_id: assetId, expected_revision: 2 },
      { intent: "status" },
      { intent: "clear", expected_revision: 2 },
    ]);
    session.release();
  });
  it("does nothing until ensured, restores without preview and releases on view leave", async () => {
    const { session, fetchApi } = fixture();
    expect(fetchApi).not.toHaveBeenCalled();
    session.binding().ensure();
    await settle();
    session.binding().select(assetId);
    await session.binding().restore();
    expect(session.binding().state.restored?.use_handle).toBe(useHandle);
    expect(
      fetchApi.mock.calls
        .map(([path]) => path)
        .every((path) => !path.endsWith("/preview")),
    ).toBe(true);
    session.release();
    await settle();
    expect(JSON.parse(fetchApi.mock.calls.at(-1)![1].body as string)).toEqual({
      intent: "release",
      use_handle: useHandle,
    });
    expect(session.binding().state.projection).toBe(null);
  });
  it("owns one preview URL and revokes it with its handle on selection change", async () => {
    const { session } = fixture();
    const allocate = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:owned-preview");
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    session.binding().ensure();
    await settle();
    session.binding().select(assetId);
    await session.binding().restore();
    await session.binding().preview();
    expect(session.binding().state.previewUrl).toBe("blob:owned-preview");
    expect(allocate).toHaveBeenCalledTimes(1);
    session.binding().select(null);
    await settle();
    expect(revoke).toHaveBeenCalledWith("blob:owned-preview");
    expect(session.binding().state.restored).toBe(null);
    session.release();
  });
  it("lost retention acknowledgements block another mutation until explicit read", async () => {
    const { session, client } = fixture();
    session.binding().ensure();
    await settle();
    vi.spyOn(client, "send").mockResolvedValueOnce({
      ok: false,
      code: "transport_failure",
      outcomeUnknown: true,
    });
    await session.binding().setEnabled(false);
    expect(session.binding().state.error).toBe("outcome_unknown");
    expect(session.binding().state.confirmed).toBe(false);
    const send = vi.spyOn(client, "send");
    await session.binding().clear();
    expect(send).toHaveBeenCalledTimes(1);
    await session.binding().refresh();
    expect(session.binding().state.confirmed).toBe(true);
    session.release();
  });
  it("a late restore response is released but cannot repopulate a hidden view", async () => {
    const { session, client, fetchApi } = fixture();
    session.binding().ensure();
    await settle();
    session.binding().select(assetId);
    let finish!: (value: Awaited<ReturnType<typeof client.send>>) => void;
    vi.spyOn(client, "send").mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const work = session.binding().restore();
    session.release();
    finish({ ok: true, response: restoredWire() as never });
    await work;
    await settle();
    expect(session.binding().state.restored).toBe(null);
    expect(
      JSON.parse(fetchApi.mock.calls.at(-1)![1].body as string).intent,
    ).toBe("release");
  });
  it("restorable nonuniform media disables only preview, not fresh restore", async () => {
    const { session, client } = fixture();
    session.binding().ensure();
    await settle();
    session.binding().select(assetId);
    vi.spyOn(client, "send").mockResolvedValueOnce({
      ok: true,
      response: restoredWire(false) as never,
    });
    await session.binding().restore();
    const preview = vi.spyOn(client, "preview");
    await session.binding().preview();
    expect(preview).not.toHaveBeenCalled();
    expect(session.binding().state.restored?.preview_available).toBe(false);
    session.release();
  });
  it("keeps cleanup busy until actual release completes and blocks intervening mutations", async () => {
    const { session, client, fetchApi } = fixture();
    session.binding().ensure();
    await settle();
    session.binding().select(assetId);
    await session.binding().restore();
    let finish!: (value: Awaited<ReturnType<typeof client.send>>) => void;
    vi.spyOn(client, "send").mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const work = session.binding().releaseUse();
    expect(session.binding().state.busy).toBe("release");
    expect(session.binding().state.restored).toBe(null);
    const count = fetchApi.mock.calls.length;
    await session.binding().refresh();
    await session.binding().clear();
    await session.binding().restore();
    expect(fetchApi).toHaveBeenCalledTimes(count);
    finish({
      ok: true,
      response: decodeRetainedAssetsResponse(retainedWire()),
    });
    await work;
    expect(session.binding().state.busy).toBe(null);
    expect(session.binding().state.confirmed).toBe(true);
    session.release();
  });
});
