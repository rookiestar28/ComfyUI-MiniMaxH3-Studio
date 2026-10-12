import { describe, expect, it, vi } from "vitest";
import { createPerceptionHostClient } from "../src/host/perceptionHostClient";

const wire = {
  schema: "h3.context.perception_host_status.v1",
  state: "configured",
  visual: true,
  audio: false,
  reason: "none",
  cancellation_observed: false,
};

describe("read-only perception host client", () => {
  it("sends only one same-origin GET with the caller's cancellation signal", async () => {
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(wire),
    }));
    const client = createPerceptionHostClient({ fetchApi });
    const controller = new AbortController();
    expect(await client.read(controller.signal)).toEqual({
      ok: true,
      status: wire,
    });
    expect(fetchApi).toHaveBeenCalledTimes(1);
    expect(fetchApi).toHaveBeenCalledWith("/h3-context/v1/perception/status", {
      method: "GET",
      credentials: "same-origin",
      headers: { accept: "application/json" },
      signal: controller.signal,
    });
    expect(Object.keys(client)).toEqual(["read"]);
  });

  it.each([
    " ".repeat(4097),
    JSON.stringify({ ...wire, python_executable: "untrusted_operator_value" }),
    JSON.stringify({ ...wire, reason: "untrusted_operator_value" }),
    "invalid-json",
  ])(
    "reports a finite malformed result for an oversized or hostile body",
    async (body) => {
      const fetchApi = vi.fn(async () => ({
        ok: true,
        status: 200,
        text: async () => body,
      }));
      const result = await createPerceptionHostClient({ fetchApi }).read();
      expect(result).toEqual({ ok: false, code: "malformed_response" });
      expect(JSON.stringify(result)).not.toContain("untrusted_operator_value");
    },
  );

  it("does not copy a path-bearing transport exception into diagnostics", async () => {
    const fetchApi = vi.fn(async () => {
      throw new Error("untrusted_operator_value");
    });
    expect(await createPerceptionHostClient({ fetchApi }).read()).toEqual({
      ok: false,
      code: "transport_failure",
    });
  });

  it("refuses a non-success response without parsing its private body", async () => {
    const text = vi.fn(async () => "untrusted_operator_value");
    const fetchApi = vi.fn(async () => ({ ok: false, status: 500, text }));
    expect(await createPerceptionHostClient({ fetchApi }).read()).toEqual({
      ok: false,
      code: "unexpected_status",
    });
    expect(text).not.toHaveBeenCalled();
  });

  it("checks the UTF-8 byte budget before parsing a multibyte body", async () => {
    const body = JSON.stringify({ padding: "é".repeat(2200) });
    expect(body.length).toBeLessThan(4096);
    expect(new TextEncoder().encode(body).byteLength).toBeGreaterThan(4096);
    const parse = vi.spyOn(JSON, "parse");
    try {
      const fetchApi = vi.fn(async () => ({
        ok: true,
        status: 200,
        text: async () => body,
      }));
      expect(await createPerceptionHostClient({ fetchApi }).read()).toEqual({
        ok: false,
        code: "malformed_response",
      });
      expect(parse).not.toHaveBeenCalled();
    } finally {
      parse.mockRestore();
    }
  });

  it("refuses a success status other than the owned endpoint's 200 without reading", async () => {
    const text = vi.fn(async () => "untrusted_operator_value");
    const fetchApi = vi.fn(async () => ({ ok: true, status: 201, text }));
    expect(await createPerceptionHostClient({ fetchApi }).read()).toEqual({
      ok: false,
      code: "unexpected_status",
    });
    expect(text).not.toHaveBeenCalled();
  });

  it("has a finite unavailable result when the host fetch port is absent", async () => {
    expect(await createPerceptionHostClient({}).read()).toEqual({
      ok: false,
      code: "seam_unavailable",
    });
  });
});
