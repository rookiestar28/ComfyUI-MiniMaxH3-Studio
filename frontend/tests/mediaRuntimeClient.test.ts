// M25-33: the media runtime client never throws, sends only closed action envelopes, maps every
// non-success to a closed code, and marks a mutation whose response was lost as outcome-unknown.

import { describe, expect, it, vi } from "vitest";

import wireFixture from "./fixtures/media_runtime_wire_v3.json";
import {
  MEDIA_RUNTIME_SETUP_ROUTE,
  MEDIA_RUNTIME_STATUS_ROUTE,
  createMediaRuntimeClient,
  type MediaRuntimeFetch,
} from "../src/host/mediaRuntimeClient";

const samples = wireFixture.samples as unknown as Record<string, unknown>;
const JOB = "abababababababababababababababab";

function respond(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function client(handler: MediaRuntimeFetch) {
  const fetchApi = vi.fn(handler);
  return { fetchApi, api: createMediaRuntimeClient({ fetchApi }) };
}

describe("status and job reads", () => {
  it("reads and decodes the status through a same-origin GET", async () => {
    const { fetchApi, api } = client(async () =>
      respond(200, samples.status_setup_required),
    );
    const result = await api.readStatus();
    expect(result.ok).toBe(true);
    const [path, init] = fetchApi.mock.calls[0]!;
    expect(path).toBe(MEDIA_RUNTIME_STATUS_ROUTE);
    expect(init).toMatchObject({ method: "GET", credentials: "same-origin" });
    expect(init.body).toBeUndefined();
  });

  it("fails closed on an undecodable status instead of throwing", async () => {
    const { api } = client(async () =>
      respond(200, { ...(samples.status_override as object), extra: 1 }),
    );
    expect(await api.readStatus()).toEqual({
      ok: false,
      code: "malformed_response",
      outcomeUnknown: false,
    });
  });

  it("maps transport loss, abort and closed refusal codes", async () => {
    expect(
      await client(async () => {
        throw new TypeError("network");
      }).api.readStatus(),
    ).toMatchObject({ ok: false, code: "transport_failure" });
    const controller = new AbortController();
    controller.abort();
    expect(
      await client(async () => {
        throw Object.assign(new Error("x"), { name: "AbortError" });
      }).api.readStatus(controller.signal),
    ).toMatchObject({ ok: false, code: "aborted" });
    expect(
      await client(async () =>
        respond(503, { error: "private_root_invalid" }),
      ).api.readStatus(),
    ).toMatchObject({ ok: false, code: "private_root_invalid" });
    expect(
      await client(async () =>
        respond(502, { error: "gateway exploded" }),
      ).api.readStatus(),
    ).toEqual({ ok: false, code: "unexpected_status", outcomeUnknown: false });
  });

  it("reads a job only by a well-formed id", async () => {
    const { fetchApi, api } = client(async () =>
      respond(200, samples.job_running),
    );
    expect(await api.readJob("../../etc")).toMatchObject({
      ok: false,
      code: "invalid_request",
    });
    expect(fetchApi).not.toHaveBeenCalled();
    const result = await api.readJob(JOB);
    expect(result).toMatchObject({ ok: true, job: { phase: "extracting" } });
    expect(fetchApi.mock.calls[0]![0]).toBe(
      `${MEDIA_RUNTIME_SETUP_ROUTE}/${JOB}`,
    );
  });

  it("rejects an oversized response body", async () => {
    const { api } = client(async () =>
      respond(200, { padding: "x".repeat(20_000) }),
    );
    expect(await api.readStatus()).toMatchObject({
      ok: false,
      code: "malformed_response",
    });
  });
});

describe("setup actions", () => {
  it("sends only the closed envelope for each action", async () => {
    const bodies: unknown[] = [];
    const { api } = client(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as { action: string };
      bodies.push(body);
      return body.action === "install_supported"
        ? respond(202, samples.job_running)
        : body.action === "cancel_setup"
          ? respond(200, {
              ...(samples.job_running as object),
              state: "cancelled",
              reason: "cancelled",
            })
          : respond(200, samples.status_local_selection);
    });
    await api.send("install_supported", {});
    await api.send("cancel_setup", { jobId: JOB });
    await api.send("rescan", {});
    await api.send("reclaim_parked_runtime", {});
    await api.send("use_local_directory", {
      directory: "D:/tools/ffmpeg",
      expectedRevision: 4,
    });
    await api.send("restore_auto", { expectedRevision: 4 });
    const schema = "h3.context.media_runtime_setup_request.v1";
    expect(bodies).toEqual([
      { schema, action: "install_supported" },
      { schema, action: "cancel_setup", job_id: JOB },
      { schema, action: "rescan" },
      { schema, action: "reclaim_parked_runtime" },
      {
        schema,
        action: "use_local_directory",
        directory: "D:/tools/ffmpeg",
        expected_revision: 4,
      },
      { schema, action: "restore_auto", expected_revision: 4 },
    ]);
  });

  it("refuses an incomplete payload locally without sending", async () => {
    const { fetchApi, api } = client(async () => respond(200, {}));
    for (const [action, payload] of [
      ["cancel_setup", {}],
      ["cancel_setup", { jobId: "nope" }],
      ["use_local_directory", { directory: "", expectedRevision: 1 }],
      ["use_local_directory", { directory: "x" }],
      ["restore_auto", { expectedRevision: 1.5 }],
    ] as const)
      expect(await api.send(action, payload)).toMatchObject({
        ok: false,
        code: "invalid_request",
      });
    expect(fetchApi).not.toHaveBeenCalled();
  });

  it("returns the job for install and cancel and the status otherwise", async () => {
    const install = await client(async () =>
      respond(202, samples.job_running),
    ).api.send("install_supported", {});
    expect(install.ok && "job" in install).toBe(true);
    const rescan = await client(async () =>
      respond(200, samples.status_override),
    ).api.send("rescan", {});
    expect(rescan.ok && "status" in rescan).toBe(true);
  });

  it("marks a lost or unreadable mutation response as outcome-unknown", async () => {
    expect(
      await client(async () => {
        throw new TypeError("connection reset");
      }).api.send("install_supported", {}),
    ).toEqual({ ok: false, code: "transport_failure", outcomeUnknown: true });
    expect(
      await client(async () => respond(202, "<html>")).api.send(
        "install_supported",
        {},
      ),
    ).toEqual({ ok: false, code: "malformed_response", outcomeUnknown: true });
    // A different success status is not the contract, and the job may still have started.
    expect(
      await client(async () => respond(200, samples.job_running)).api.send(
        "install_supported",
        {},
      ),
    ).toEqual({ ok: false, code: "unexpected_status", outcomeUnknown: true });
    expect(
      await client(async () => respond(500, null)).api.send(
        "install_supported",
        {},
      ),
    ).toEqual({ ok: false, code: "unexpected_status", outcomeUnknown: true });
  });

  it("carries a closed refusal as a known outcome", async () => {
    expect(
      await client(async () => respond(409, { error: "setup_busy" })).api.send(
        "install_supported",
        {},
      ),
    ).toEqual({ ok: false, code: "setup_busy", outcomeUnknown: false });
    expect(
      await client(async () =>
        respond(409, { error: "reclaim_unsafe" }),
      ).api.send("reclaim_parked_runtime", {}),
    ).toEqual({ ok: false, code: "reclaim_unsafe", outcomeUnknown: false });
  });
});
