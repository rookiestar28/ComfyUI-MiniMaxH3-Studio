import { describe, expect, it, vi } from "vitest";
import * as outputPreview from "../src/host/authoringOutputPreview";
import {
  OUTPUT_CAPABILITY,
  decodeOutputStatus,
} from "../src/contracts/authoringOutputCodec";

const capability = { ...OUTPUT_CAPABILITY, supported: true };
const status = decodeOutputStatus({
  schema: "h3.authoring.output_status.v1",
  workspace_handle: `authoring-${"a".repeat(32)}`,
  workspace_revision: 1,
  timeline_revision: 2,
  snapshot_fingerprint: `sha256:${"b".repeat(64)}`,
  job_handle: `arj_${"c".repeat(22)}`,
  output_handle: `aro_${"d".repeat(22)}`,
  state_version: 7,
  phase: "succeeded",
  progress_bp: 10000,
  failure: null,
  currency: "old_revision",
  availability: "available",
  output: {
    output_fingerprint: `sha256:${"e".repeat(64)}`,
    byte_length: 10000,
    width: 1280,
    height: 720,
    frame_count: 48,
    frame_rate_num: 24,
    frame_rate_den: 1,
    audio_streams: 1,
    output_profile_id: OUTPUT_CAPABILITY.output_profile_id,
    verified: true,
  },
});
function response(length = 3) {
  const body = new ReadableStream<Uint8Array>({
    type: "bytes",
    pull(controller) {
      const request = (controller as ReadableByteStreamController).byobRequest!;
      (request.view as Uint8Array).set([1, 2, 3]);
      request.respond(3);
      controller.close();
    },
  });
  return new Response(body, {
    headers: {
      "content-type": "video/mp4",
      "content-length": String(length),
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      "accept-ranges": "bytes",
      "content-disposition": 'inline; filename="authoring-preview.mp4"',
    },
  });
}
async function module() {
  return outputPreview;
}
describe("final-output preview ownership", () => {
  it("owns one bounded Blob URL, revokes on replacement, close and caller abort", async () => {
    const m = await module();
    const create = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValueOnce("blob:first")
      .mockReturnValueOnce("blob:second");
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const fetchApi = vi.fn(async () => response());
    const owner = m.createOutputPreview(fetchApi);
    const first = await owner.open(
      status,
      capability,
      new AbortController().signal,
    );
    expect(first.url).toBe("blob:first");
    expect((create.mock.calls[0][0] as Blob).size).toBe(3);
    const controller = new AbortController();
    const second = await owner.open(status, capability, controller.signal);
    expect(second.url).toBe("blob:second");
    expect(revoke).toHaveBeenCalledWith("blob:first");
    first.close();
    expect(revoke).not.toHaveBeenCalledWith("blob:second");
    controller.abort();
    expect(revoke).toHaveBeenCalledWith("blob:second");
    owner.close();
    expect(
      revoke.mock.calls.filter(([url]) => url === "blob:second"),
    ).toHaveLength(1);
    expect(fetchApi.mock.calls).toHaveLength(2);
  });
  it("refuses original fallback, oversize, dishonest length and unavailable parent", async () => {
    const m = await module(),
      create = vi.spyOn(URL, "createObjectURL");
    for (const length of [2, 4, 16777217]) {
      const owner = m.createOutputPreview(async () => response(length));
      await expect(
        owner.open(status, capability, new AbortController().signal),
      ).rejects.toThrow();
      owner.close();
    }
    const fetchApi = vi.fn(async () => response());
    await expect(
      m
        .createOutputPreview(fetchApi)
        .open(
          { ...status, availability: "gone" },
          capability,
          new AbortController().signal,
        ),
    ).rejects.toThrow();
    expect(fetchApi).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
  });
});
