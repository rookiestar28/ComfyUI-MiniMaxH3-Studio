import { describe, expect, it, vi } from "vitest";
import * as outputActions from "../src/host/authoringOutputActions";
import { OUTPUT_CAPABILITY } from "../src/contracts/authoringOutputCodec";

const request = {
  schema: "h3.authoring.output_create.v1",
  workspace_handle: `authoring-${"a".repeat(32)}`,
  workspace_revision: 1,
  timeline_revision: 2,
  snapshot_fingerprint: `sha256:${"b".repeat(64)}`,
  output_profile_id: OUTPUT_CAPABILITY.output_profile_id,
  idempotency_key: "request_123456789",
} as const;
const status = {
  ...request,
  schema: "h3.authoring.output_status.v1",
  job_handle: `arj_${"c".repeat(22)}`,
  output_handle: null,
  state_version: 0,
  phase: "queued",
  progress_bp: 0,
  failure: null,
  currency: "current",
  availability: "gone",
  output: null,
};
delete (status as Record<string, unknown>).idempotency_key;
delete (status as Record<string, unknown>).output_profile_id;

function response(
  wire: unknown,
  code = 200,
  mutation: Record<string, string> = {},
) {
  const bytes = new TextEncoder().encode(JSON.stringify(wire));
  let offset = 0;
  const body = new ReadableStream<Uint8Array>({
    type: "bytes",
    pull(controller) {
      const byteController = controller as ReadableByteStreamController;
      const request = byteController.byobRequest!;
      const view = request.view as Uint8Array;
      const count = Math.min(view.byteLength, bytes.byteLength - offset);
      view.set(bytes.subarray(offset, offset + count));
      offset += count;
      if (count) request.respond(count);
      if (offset === bytes.byteLength) {
        controller.close();
        if (!count) request.respond(0);
      }
    },
  });
  return new Response(body, {
    status: code,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "content-length": String(bytes.length),
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      ...mutation,
    },
  });
}
async function module() {
  return outputActions;
}

describe("final-output same-origin client", () => {
  it("uses exact bindings and rejects absent/stale capability before network", async () => {
    const m = await module(),
      fetchApi = vi.fn(async () => response(status));
    const client = m.createOutputClient(fetchApi);
    await expect(
      client.create(
        request,
        { ...OUTPUT_CAPABILITY, supported: true },
        new AbortController().signal,
      ),
    ).resolves.toEqual(status);
    expect(fetchApi.mock.calls[0]).toMatchObject([
      OUTPUT_CAPABILITY.job_path,
      {
        method: "POST",
        redirect: "error",
        cache: "no-store",
        credentials: "same-origin",
        body: JSON.stringify(request),
      },
    ]);
    for (const capability of [
      null,
      OUTPUT_CAPABILITY,
      { ...OUTPUT_CAPABILITY, max_jobs: 99 },
    ]) {
      await expect(
        client.create(request, capability, new AbortController().signal),
      ).rejects.toThrow();
    }
    expect(fetchApi).toHaveBeenCalledTimes(1);
  });
  it("checks every response join and same-job identity", async () => {
    const m = await module();
    for (const mutation of [
      { workspace_revision: 2 },
      { timeline_revision: 8 },
      { snapshot_fingerprint: `sha256:${"e".repeat(64)}` },
      { workspace_handle: `authoring-${"f".repeat(32)}` },
      { job_handle: `arj_${"g".repeat(22)}` },
    ]) {
      const client = m.createOutputClient(async () =>
        response({ ...status, ...mutation }),
      );
      await expect(
        client.status(
          status.job_handle,
          request,
          { ...OUTPUT_CAPABILITY, supported: true },
          new AbortController().signal,
        ),
      ).rejects.toThrow("invalid_output_contract");
    }
  });
  it("admits closed errors and rejects response redirects/oversize before reading", async () => {
    const m = await module();
    const run = (value: Response) =>
      m
        .createOutputClient(async () => value)
        .create(
          request,
          { ...OUTPUT_CAPABILITY, supported: true },
          new AbortController().signal,
        );
    await expect(
      run(
        response(
          { schema: "h3.authoring.output_error.v1", code: "expired" },
          410,
        ),
      ),
    ).rejects.toMatchObject({ code: "expired" });
    for (const headers of [
      { location: "https://foreign.invalid" },
      { "content-length": "8193" },
      { "content-encoding": "gzip" },
      { "cache-control": "public" },
    ] as Record<string, string>[]) {
      await expect(run(response(status, 200, headers))).rejects.toThrow(
        "invalid_output_contract",
      );
    }
  });
  it("builds an explicit relative original-download link without fetching", async () => {
    const m = await module();
    const output = `aro_${"d".repeat(22)}`;
    expect(
      m.outputMediaPath(output, request.workspace_handle, "download"),
    ).toBe(
      `${OUTPUT_CAPABILITY.output_path}/${output}/download?workspace_handle=${request.workspace_handle}`,
    );
    for (const bad of [
      "../../private",
      output + "\n",
      "https://foreign.invalid",
    ])
      expect(() =>
        m.outputMediaPath(bad, request.workspace_handle, "download"),
      ).toThrow();
  });

  it("cancels an abandoned body, rejects timeout and never sends cancel implicitly", async () => {
    const controller = new AbortController();
    let resolve!: (value: Response) => void;
    const fetchApi = vi.fn(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    const client = outputActions.createOutputClient(fetchApi);
    const pending = client.create(
      request,
      { ...OUTPUT_CAPABILITY, supported: true },
      controller.signal,
    );
    const rejected = expect(pending).rejects.toMatchObject({ code: "aborted" });
    controller.abort();
    await rejected;
    const late = response(status),
      cancel = vi.spyOn(late.body!, "cancel");
    resolve(late);
    await new Promise((done) => setTimeout(done, 0));
    expect(cancel).toHaveBeenCalled();
    expect(fetchApi).toHaveBeenCalledTimes(1);
    vi.useFakeTimers();
    try {
      const hung = outputActions.createOutputClient(
        () => new Promise(() => undefined),
      );
      const result = hung.create(
        request,
        { ...OUTPUT_CAPABILITY, supported: true },
        new AbortController().signal,
      );
      const timeout = expect(result).rejects.toMatchObject({ code: "aborted" });
      await vi.advanceTimersByTimeAsync(120000);
      await timeout;
    } finally {
      vi.useRealTimers();
    }
  });
});
