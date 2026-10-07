import { describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/workspace_state_v1.json";

import * as codecModule from "../src/contracts/workspaceStateCodec";
import * as clientModule from "../src/host/workspaceStateClient";
async function codec() {
  return codecModule;
}
async function client() {
  return clientModule;
}
const copy = () => structuredClone(fixture);

describe("workspace state closed codec", () => {
  it("decodes only immutable closed metadata and no execution authority", async () => {
    const { decodeWorkspaceStateResponse } = await codec();
    const decoded = decodeWorkspaceStateResponse(copy());
    expect(decoded.projection.records[0].record_id).toMatch(
      /^record_[a-f0-9]{32}$/,
    );
    expect(Object.isFrozen(decoded.projection.records[0].revisions)).toBe(true);
    expect(Object.isFrozen(decoded.projection.records)).toBe(true);
    expect(decoded.recovered).toBe(null);
  });

  it("refuses unknown fields, unsafe IDs, bad revisions and inconsistent status", async () => {
    const { decodeWorkspaceStateResponse } = await codec();
    const bad = [
      { ...copy(), prompt: "must-not-leak" },
      { ...copy(), projection: { ...copy().projection, current: false } },
      { ...copy(), projection: { ...copy().projection, count: 0 } },
      { ...copy(), projection: { ...copy().projection, revision: true } },
      {
        ...copy(),
        projection: {
          ...copy().projection,
          records: [{ ...copy().projection.records[0], record_id: "pw_old" }],
        },
      },
      {
        ...copy(),
        projection: {
          ...copy().projection,
          records: [{ ...copy().projection.records[0], provider_payload: {} }],
        },
      },
    ];
    for (const value of bad)
      expect(() => decodeWorkspaceStateResponse(value)).toThrow();
  });

  it("requires a fresh read-only handle and exact selected-record metadata for recovery", async () => {
    const { decodeWorkspaceStateResponse } = await codec();
    const row = copy().projection.records[0];
    const recovered = {
      recovery_handle: "recovery_" + "a".repeat(43),
      record_id: row.record_id,
      kind: row.kind,
      state: row.state,
      source_status: "source_reauthorization_required",
      executable: false,
      segment_count: row.segment_count,
      revisions: row.revisions,
    };
    expect(
      decodeWorkspaceStateResponse({ ...copy(), recovered }).recovered
        ?.executable,
    ).toBe(false);
    expect(
      decodeWorkspaceStateResponse({
        ...copy(),
        recovered,
        error: "record_bound",
        projection: {
          ...copy().projection,
          save_state: "blocked",
          current: false,
        },
      }).recovered?.executable,
    ).toBe(false);
    for (const changes of [
      { executable: true },
      { recovery_handle: "run_old" },
      { state: "running" },
      { source_status: "authorized" },
      { segment_count: 2 },
    ]) {
      expect(() =>
        decodeWorkspaceStateResponse({
          ...copy(),
          recovered: { ...recovered, ...changes },
        }),
      ).toThrow();
    }
  });

  it("requires a workspace closed timestamp iff its state is closed", async () => {
    const { decodeWorkspaceStateResponse } = await codec();
    for (const kind of ["production", "authoring"]) {
      for (const [state, closed_at_ms] of [
        ["workspace_active", 1500],
        ["workspace_closed", null],
      ]) {
        const value = copy();
        Object.assign(value.projection.records[0], {
          kind,
          state,
          closed_at_ms,
          segment_count: 0,
          revisions: {
            workspace: 1,
            reference: kind === "authoring" ? 0 : null,
            timeline: kind === "authoring" ? 0 : null,
            context: null,
            transition: 0,
          },
        });
        expect(() => decodeWorkspaceStateResponse(value)).toThrow();
      }
    }
  });
});

describe("workspace state same-origin client", () => {
  it("sends a closed revision-bearing intent and never retries an unknown outcome", async () => {
    const { createWorkspaceStateClient } = await client();
    const fetchApi = vi.fn(async () => {
      throw new Error("private-host-value");
    });
    const result = await createWorkspaceStateClient({ fetchApi }).send({
      intent: "save",
      expected_revision: 2,
    });
    expect(result).toEqual({
      ok: false,
      code: "transport_failure",
      outcomeUnknown: true,
    });
    expect(fetchApi).toHaveBeenCalledTimes(1);
    expect(fetchApi.mock.calls[0]).toEqual([
      "/h3-context/workspace-state",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: '{"intent":"save","expected_revision":2}',
      }),
    ]);
    expect(JSON.stringify(result)).not.toContain("private-host-value");
  });

  it("refuses caller paths before fetch and decodes bounded typed refusal bodies", async () => {
    const { createWorkspaceStateClient } = await client();
    const fetchApi = vi.fn(async () => ({
      status: 200,
      json: async () => copy(),
    }));
    const api = createWorkspaceStateClient({ fetchApi });
    expect(
      (await api.send({ intent: "status", path: "private-value" } as never)).ok,
    ).toBe(false);
    expect(fetchApi).not.toHaveBeenCalled();
    expect((await api.send({ intent: "status" })).ok).toBe(true);
  });

  it("rejects missing, unsolicited or wrong-selected recovery projections", async () => {
    const { createWorkspaceStateClient } = await client();
    const value = copy();
    const first = value.projection.records[0];
    const second = { ...first, record_id: "record_" + "2".repeat(32) };
    value.projection.records.push(second);
    value.projection.count = 2;
    const recovered = {
      recovery_handle: "recovery_" + "a".repeat(43),
      record_id: second.record_id,
      kind: second.kind,
      state: second.state,
      source_status: "source_reauthorization_required",
      executable: false,
      segment_count: second.segment_count,
      revisions: second.revisions,
    };
    for (const [action, body] of [
      [
        { intent: "restore", record_id: first.record_id, expected_revision: 2 },
        value,
      ],
      [
        { intent: "restore", record_id: first.record_id, expected_revision: 2 },
        { ...value, recovered },
      ],
      [{ intent: "status" }, { ...value, recovered }],
    ] as const) {
      const api = createWorkspaceStateClient({
        fetchApi: async () => ({ status: 200, json: async () => body }),
      });
      expect(await api.send(action)).toEqual({
        ok: false,
        code: "malformed_response",
        outcomeUnknown: false,
      });
    }
  });

  it("treats even a typed server failure after a mutation as an unknown outcome", async () => {
    const { createWorkspaceStateClient } = await client();
    const api = createWorkspaceStateClient({
      fetchApi: async () => ({
        status: 500,
        json: async () => ({ ...copy(), error: "internal_failure" }),
      }),
    });
    expect(await api.send({ intent: "save", expected_revision: 2 })).toEqual({
      ok: false,
      code: "internal_failure",
      outcomeUnknown: true,
    });
    expect(await api.send({ intent: "status" })).toEqual({
      ok: false,
      code: "internal_failure",
      outcomeUnknown: false,
    });
  });
});
