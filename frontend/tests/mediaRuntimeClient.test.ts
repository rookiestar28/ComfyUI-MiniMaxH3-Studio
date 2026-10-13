import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/media_runtime_wire_v4.json";
import {
  createMediaRuntimeClient,
  type MediaRuntimeFetch,
} from "../src/host/mediaRuntimeClient";
const sample = fixture.samples.status_setup_required;
function subject(status = 200, wire: unknown = sample) {
  const fetch = vi.fn<MediaRuntimeFetch>(async () => ({
    ok: status === 200,
    status,
    json: async () => wire,
  }));
  return { fetch, client: createMediaRuntimeClient({ fetchApi: fetch }) };
}
describe("manual setup client", () => {
  it("sends a closed same-origin revision-bound local selection", async () => {
    const s = subject();
    expect(
      (
        await s.client.send("use_local_directory", {
          directory: "C:/tools/bin",
          expectedRevision: 0,
        })
      ).ok,
    ).toBe(true);
    const [path, init] = s.fetch.mock.calls[0]!;
    expect(path).toBe("/h3-context/v1/media-runtime/setup");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body as string)).toEqual({
      schema: "h3.context.media_runtime_setup_request.v2",
      action: "use_local_directory",
      directory: "C:/tools/bin",
      expected_revision: 0,
    });
    expect("readJob" in s.client).toBe(false);
  });
  it.each([-1, NaN, Infinity, 1.5])(
    "never sends an invalid revision %s",
    async (revision) => {
      const s = subject();
      expect(
        await s.client.send("restore_auto", { expectedRevision: revision }),
      ).toMatchObject({ ok: false, code: "invalid_request" });
      expect(s.fetch).not.toHaveBeenCalled();
    },
  );
  it("keeps a 409 refusal definite and a lost response uncertain", async () => {
    expect(
      await subject(409, { error: "config_conflict" }).client.send(
        "restore_auto",
        { expectedRevision: 0 },
      ),
    ).toEqual({ ok: false, code: "config_conflict", outcomeUnknown: false });
    const s = subject();
    s.fetch.mockRejectedValue(new Error("lost"));
    expect(await s.client.send("rescan", {})).toEqual({
      ok: false,
      code: "transport_failure",
      outcomeUnknown: true,
    });
    expect(s.fetch).toHaveBeenCalledTimes(1);
  });
  it("refuses stale or malformed responses", async () => {
    expect(
      await subject(200, { ...sample, setup: null }).client.readStatus(),
    ).toMatchObject({ ok: false, code: "malformed_response" });
    expect(await subject(202).client.send("rescan", {})).toMatchObject({
      ok: false,
      code: "unexpected_status",
      outcomeUnknown: true,
    });
  });
});
