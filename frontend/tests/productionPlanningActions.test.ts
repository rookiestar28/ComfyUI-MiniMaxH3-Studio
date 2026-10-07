import { describe, expect, it, vi } from "vitest";
import {
  decodeProductionPlanningResponse,
  encodeProductionPlanningAction,
  PRODUCTION_PLANNING_PROJECTION_SCHEMA,
  planningSelectors,
} from "../src/contracts/productionPlanningCodec";
import {
  createProductionPlanningClient,
  PRODUCTION_PLANNING_ROUTE,
} from "../src/host/productionPlanningActions";

const fp = `sha256:${"a".repeat(64)}`;
const handle = `pw_${"a".repeat(43)}`;
const prepare = {
  workspace_handle: handle,
  expected_workspace_revision: 1,
  expected_workspace_fingerprint: fp,
  context_workspace_handle: `ws_${"b".repeat(43)}`,
  expected_report_revision: 0,
  expected_report_fingerprint: fp,
  expected_planning_revision: 0,
  target_seconds: 20,
  policy: "fixed_10",
};
function projection() {
  return {
    schema: PRODUCTION_PLANNING_PROJECTION_SCHEMA,
    request_id: "prepare",
    workspace_handle: handle,
    workspace_id: "workspace_actual",
    workspace_revision: 1,
    workspace_fingerprint: fp,
    planning_context_id: "planning_owned",
    planning_revision: 1,
    source_duration_seconds: 10,
    target_seconds: 20,
    policy: "fixed_10",
    admission_id: null,
    proposal: null,
  };
}
function proposed() {
  return {
    ...projection(),
    admission_id: "admission_owned",
    proposal: {
      proposal_id: "proposal_owned",
      revision: 1,
      fingerprint: fp,
      importable: true,
      blocker_codes: [],
      start_hold_codes: ["managed_execution_qualification_pending"],
      segments: [1, 2].map((ordinal) => ({
        segment_id: `segment_${ordinal}`,
        ordinal,
        task_mode: "t2va",
        duration_seconds: 10,
        local_prompt: "A blue sphere turns.",
      })),
    },
  };
}

describe("owned planning action codec and transport", () => {
  it("sends one explicit same-origin action and adopts only its correlated response", async () => {
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => projection(),
    }));
    const client = createProductionPlanningClient({ fetchApi });
    const result = await client.send("prepare", "prepare_context", prepare);
    expect(fetchApi).toHaveBeenCalledOnce();
    expect(fetchApi.mock.calls[0]).toEqual([
      PRODUCTION_PLANNING_ROUTE,
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
      }),
    ]);
    expect(result).toEqual(projection());
    expect(Object.isFrozen(result)).toBe(true);
  });
  it.each([
    "materialize",
    "source_request_fingerprint",
    "report",
    "qualification",
    "path",
  ])("rejects injected %s before transport", async (key) => {
    const fetchApi = vi.fn();
    await expect(
      createProductionPlanningClient({ fetchApi }).send(
        "prepare",
        "prepare_context",
        { ...prepare, [key]: "untrusted" },
      ),
    ).rejects.toThrow();
    expect(fetchApi).not.toHaveBeenCalled();
  });
  it("takes a detached immutable request snapshot for exact retry", () => {
    const mutable = { ...prepare };
    const request = encodeProductionPlanningAction(
      "prepare",
      "prepare_context",
      mutable,
    );
    mutable.target_seconds = 30;
    expect(request.payload.target_seconds).toBe(20);
    expect(Object.isFrozen(request.payload)).toBe(true);
  });
  it.each([
    { workspace_revision: 2 },
    { workspace_fingerprint: `sha256:${"b".repeat(64)}` },
    { planning_revision: 2 },
    { target_seconds: 30 },
    { policy: "fixed_5" },
  ])(
    "rejects a response that does not echo exact planning intent/CAS: %j",
    async (drift) => {
      const fetchApi = vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ ...projection(), ...drift }),
      }));
      await expect(
        createProductionPlanningClient({ fetchApi }).send(
          "prepare",
          "prepare_context",
          prepare,
        ),
      ).rejects.toThrow();
    },
  );
  it.each(["request_id", "workspace_handle", "planning_context_id"])(
    "rejects a mismatched %s response",
    async (key) => {
      const value = {
        ...projection(),
        [key]: key === "workspace_handle" ? `pw_${"c".repeat(43)}` : "foreign",
      };
      const fetchApi = vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => value,
      }));
      const source = decodeProductionPlanningResponse(projection());
      if (source.schema !== PRODUCTION_PLANNING_PROJECTION_SCHEMA)
        throw new Error("fixture");
      await expect(
        createProductionPlanningClient({ fetchApi }).send(
          "prepare",
          "read_plan",
          planningSelectors(source),
        ),
      ).rejects.toThrow();
    },
  );
  it("rejects a foreign schema and every unexpected response field", () => {
    expect(() =>
      decodeProductionPlanningResponse({
        ...projection(),
        schema: "h3.context.production_planning.projection.v0",
      }),
    ).toThrow();
    expect(() =>
      decodeProductionPlanningResponse({ ...projection(), context_report: {} }),
    ).toThrow();
  });
  it("checks complete ordered segment duration, closure and importability", () => {
    expect(decodeProductionPlanningResponse(proposed())).toEqual(proposed());
    for (const change of [
      (value: ReturnType<typeof proposed>) => {
        value.proposal.segments[1]!.ordinal = 1;
      },
      (value: ReturnType<typeof proposed>) => {
        value.proposal.segments[1]!.duration_seconds = 9;
      },
      (value: ReturnType<typeof proposed>) => {
        value.proposal.segments[1]!.segment_id = "segment_1";
      },
      (value: ReturnType<typeof proposed>) => {
        value.proposal.importable = false;
      },
    ]) {
      const value = proposed();
      change(value);
      expect(() => decodeProductionPlanningResponse(value)).toThrow();
    }
  });
  it("rejects refusal without interpreting arbitrary response bodies or retrying", async () => {
    const json = vi.fn();
    const fetchApi = vi.fn(async () => ({ ok: false, status: 409, json }));
    await expect(
      createProductionPlanningClient({ fetchApi }).send(
        "prepare",
        "prepare_context",
        prepare,
      ),
    ).rejects.toThrow();
    expect(json).not.toHaveBeenCalled();
    expect(fetchApi).toHaveBeenCalledOnce();
  });
});
