import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/workspace_state_v1.json";
import { createWorkspaceStateClient } from "../src/host/workspaceStateClient";

import * as sessionModule from "../src/lifecycle/workspaceStateSession";
async function loadSession() {
  return sessionModule;
}
afterEach(() => vi.useRealTimers());
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("workspace state UI lifecycle", () => {
  it("reads only on demand, selects finite records and sends exact revision CAS", async () => {
    const { createWorkspaceStateSession } = await loadSession();
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) => ({
      status: 200,
      json: async () => structuredClone(fixture),
    }));
    const session = createWorkspaceStateSession({
      client: createWorkspaceStateClient({ fetchApi }),
      changed: () => {},
    });
    expect(fetchApi).not.toHaveBeenCalled();
    session.binding().ensure();
    await settle();
    session.binding().select("run_old");
    expect(session.binding().state.selected).toBe(null);
    session.binding().select(fixture.projection.records[0].record_id);
    await session.binding().save();
    expect(JSON.parse(fetchApi.mock.calls[1]![1].body as string)).toEqual({
      intent: "save",
      expected_revision: 2,
    });
    session.release();
  });

  it("late reads cannot repopulate a released sidebar or trigger another operation", async () => {
    const { createWorkspaceStateSession } = await loadSession();
    let resolve!: (value: { status: number; json(): Promise<unknown> }) => void;
    const fetchApi = vi.fn(
      () =>
        new Promise<{ status: number; json(): Promise<unknown> }>((done) => {
          resolve = done;
        }),
    );
    const changed = vi.fn();
    const session = createWorkspaceStateSession({
      client: createWorkspaceStateClient({ fetchApi }),
      changed,
    });
    session.binding().ensure();
    session.release();
    const count = changed.mock.calls.length;
    resolve({ status: 200, json: async () => structuredClone(fixture) });
    await settle();
    expect(session.binding().state.projection).toBe(null);
    expect(changed).toHaveBeenCalledTimes(count);
    expect(fetchApi).toHaveBeenCalledTimes(1);
  });

  it("lost save acknowledgement disables mutation until explicit refresh, with no replay", async () => {
    const { createWorkspaceStateSession } = await loadSession();
    const fetchApi = vi.fn(async (_path: string, init: RequestInit) => {
      if (JSON.parse(init.body as string).intent === "save")
        throw new Error("lost commit");
      return { status: 200, json: async () => structuredClone(fixture) };
    });
    const session = createWorkspaceStateSession({
      client: createWorkspaceStateClient({ fetchApi }),
      changed: () => {},
    });
    session.binding().ensure();
    await settle();
    await session.binding().save();
    expect(session.binding().state.confirmed).toBe(false);
    expect(session.binding().state.error).toBe("outcome_unknown");
    await session.binding().save();
    expect(fetchApi).toHaveBeenCalledTimes(2);
    await session.binding().refresh();
    expect(session.binding().state.confirmed).toBe(true);
    expect(fetchApi).toHaveBeenCalledTimes(3);
    session.release();
  });

  it("keeps a read-only result only while its exact recovery facts remain current", async () => {
    const { createWorkspaceStateSession } = await loadSession();
    let value = structuredClone(fixture);
    const fetchApi = vi.fn(async (_path: string, init: RequestInit) => {
      const action = JSON.parse(init.body as string);
      const row = value.projection.records[0];
      return {
        status: 200,
        json: async () => ({
          ...value,
          recovered:
            action.intent === "restore"
              ? {
                  recovery_handle: "recovery_" + "a".repeat(43),
                  record_id: row.record_id,
                  kind: row.kind,
                  state: row.state,
                  source_status: "source_reauthorization_required",
                  executable: false,
                  segment_count: row.segment_count,
                  revisions: row.revisions,
                }
              : null,
        }),
      };
    });
    const session = createWorkspaceStateSession({
      client: createWorkspaceStateClient({ fetchApi }),
      changed: () => {},
    });
    session.binding().ensure();
    await settle();
    session.binding().select(value.projection.records[0].record_id);
    await session.binding().restore();
    expect(session.binding().state.recovered).not.toBe(null);
    await session.binding().refresh();
    expect(session.binding().state.recovered).not.toBe(null);
    value.projection.records[0].revisions.workspace = 2;
    await session.binding().refresh();
    expect(session.binding().state.selected).toBe(
      value.projection.records[0].record_id,
    );
    expect(session.binding().state.recovered).toBe(null);
    session.release();
  });
});
