import { describe, expect, it, vi } from "vitest";

import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { productionAccumulatedProjectFingerprint } from "../src/contracts/productionAccumulationCodec";
import {
  createProductionActionClient,
  ProductionDestinationError,
} from "../src/host/productionActions";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";

const fingerprint = `sha256:${"a".repeat(64)}`;
const projectionWire = {
  schema: "h3.context.production_workbench.projection.v1",
  workspace_handle: `pw_${"a".repeat(43)}`,
  workspace_id: "workspace_1",
  workspace_revision: 1,
  workspace_fingerprint: fingerprint,
  segments: [
    {
      segment_id: "segment_1",
      ordinal: 1,
      task_mode: "t2va",
      duration: {
        duration_milliseconds: 5167,
        delivered_milliseconds: 5167,
        frame_count: 124,
        snapped: false,
      },
      relation: "independent",
      predecessor_segment_id: null,
      boundary_kind: "independent",
      closure_state: "unavailable",
      job_state: "unavailable",
      artifact_state: "unavailable",
      continuity_state: "unavailable",
      delivered_geometry: null,
    },
  ],
  selected_segment_ids: ["segment_1"],
  run: { state: "unavailable", completed: 0, total: 0 },
  generation_sequence: null,
  reconstruction: { state: "unavailable" },
  assembly: unavailableProductionAssemblyWire(),
  authority_versions: [],
  outputs: [],
  allowed_actions: ["set_selection", "read_projection", "release_workspace"],
  blocker_codes: ["sequence_authority_unavailable"],
  limits: { max_segments: 64, max_outputs: 65 },
};

const accumulatedProjectMaterial = {
  schema: "h3.context.production_accumulated_project.v1",
  workspace_handle: projectionWire.workspace_handle,
  workspace_id: projectionWire.workspace_id,
  project_revision: 2,
  workspace: projectionWire,
  attempts: [],
  capabilities: ["read", "admit_generation", "release_generation"],
};
const accumulatedProjectWire = {
  ...accumulatedProjectMaterial,
  project_fingerprint: productionAccumulatedProjectFingerprint(
    accumulatedProjectMaterial,
  ),
};

const emptyAccumulatedProjectMaterial = {
  ...accumulatedProjectMaterial,
  project_revision: 1,
  workspace: null,
  attempts: [
    {
      candidate_id: "segment_future",
      attempt_id: "attempt_future",
      member_segment_id: "segment_future",
      status: "admitted",
      recovery: null,
      committed: false,
    },
  ],
};
const emptyAccumulatedProjectWire = {
  ...emptyAccumulatedProjectMaterial,
  project_fingerprint: productionAccumulatedProjectFingerprint(
    emptyAccumulatedProjectMaterial,
  ),
};

describe("M17-12 Production action client", () => {
  it("posts one same-origin closed wire and decodes acknowledged state", async () => {
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => projectionWire,
    }));
    const client = createProductionActionClient({ fetchApi });
    const projection = decodeProductionWorkbenchProjection(projectionWire);
    const result = await client.send("request.select", "set_selection", {
      projection,
      segmentIds: [],
    });
    expect(result.status).toBe(200);
    expect(result.projection?.workspaceHandle).toBe(projection.workspaceHandle);
    const [path, init] = fetchApi.mock.calls[0] ?? [];
    expect(path).toBe("/h3-context/v1/production/action");
    expect(init?.credentials).toBe("same-origin");
    const body = JSON.parse(String(init?.body));
    expect(Object.keys(body).sort()).toEqual([
      "action",
      "payload",
      "request_id",
      "schema",
    ]);
  });

  it("decodes 409 current projection but never parses other error bodies", async () => {
    const conflict = createProductionActionClient({
      fetchApi: vi.fn(async (_path: string, _init: RequestInit) => ({
        ok: false,
        status: 409,
        json: async () => projectionWire,
      })),
    });
    const projection = decodeProductionWorkbenchProjection(projectionWire);
    const result = await conflict.send("request.select", "set_selection", {
      projection,
      segmentIds: [],
    });
    expect(result.status).toBe(409);
    expect(result.projection).toEqual(projection);

    const json = vi.fn();
    const failed = createProductionActionClient({
      fetchApi: vi.fn(async (_path: string, _init: RequestInit) => ({
        ok: false,
        status: 410,
        json,
      })),
    });
    await expect(
      failed.send("request.read", "read_projection", { projection }),
    ).rejects.toMatchObject({
      code: "workspace_gone",
      status: 410,
    });
    expect(json).not.toHaveBeenCalled();
  });
});

describe("M25-36 destination admission client", () => {
  const target = Object.freeze({
    workspaceHandle: projectionWire.workspace_handle,
    workspaceId: projectionWire.workspace_id,
    segmentId: null,
  });

  function respond(status: number, body?: unknown) {
    const json = vi.fn(async () => {
      if (body === undefined) throw new SyntaxError("empty body");
      return body;
    });
    const fetchApi = vi.fn(async (_path: string, _init: RequestInit) => ({
      ok: status >= 200 && status < 300,
      status,
      json,
    }));
    return {
      client: createProductionActionClient({ fetchApi }),
      fetchApi,
      json,
    };
  }

  it("admits a create intent with a stable empty project before queue", async () => {
    const { client, fetchApi, json } = respond(
      201,
      emptyAccumulatedProjectWire,
    );
    await expect(
      client.sendDestination(
        "production.admit.1",
        "admit_generation_destination",
        {
          target: null,
        },
      ),
    ).resolves.toMatchObject({
      status: 201,
      project: { workspace: null, workspaceId: "workspace_1" },
    });
    expect(json).toHaveBeenCalledOnce();
    const body = JSON.parse(String(fetchApi.mock.calls[0]?.[1]?.body));
    expect(body).toEqual({
      schema: "h3.context.production_workbench.action.v1",
      request_id: "production.admit.1",
      action: "admit_generation_destination_v2",
      payload: {
        version: "h3.context.production_accumulation.v1",
        workspace_handle: null,
        workspace_id: null,
        segment_id: null,
      },
    });
  });

  it("admits an append or regeneration only for the exact targeted project", async () => {
    const { client, fetchApi } = respond(200, accumulatedProjectWire);
    const admitted = await client.sendDestination(
      "production.admit.2",
      "admit_generation_destination",
      { target: { ...target, segmentId: "segment_1" } },
    );
    expect(admitted).toMatchObject({
      status: 200,
      project: { workspaceHandle: projectionWire.workspace_handle },
    });
    expect(
      JSON.parse(String(fetchApi.mock.calls[0]?.[1]?.body)).payload,
    ).toEqual({
      version: "h3.context.production_accumulation.v1",
      workspace_handle: projectionWire.workspace_handle,
      workspace_id: "workspace_1",
      segment_id: "segment_1",
    });
    const foreign = respond(200, {
      ...(() => {
        const material = {
          ...accumulatedProjectMaterial,
          workspace_handle: `pw_${"b".repeat(43)}`,
          workspace: {
            ...projectionWire,
            workspace_handle: `pw_${"b".repeat(43)}`,
          },
        };
        return {
          ...material,
          project_fingerprint:
            productionAccumulatedProjectFingerprint(material),
        };
      })(),
    });
    await expect(
      foreign.client.sendDestination(
        "production.admit.3",
        "admit_generation_destination",
        { target },
      ),
    ).rejects.toMatchObject({ code: "cross_workspace_response" });
    const createWithBody = respond(201, emptyAccumulatedProjectWire);
    await expect(
      createWithBody.client.sendDestination(
        "production.admit.4",
        "admit_generation_destination",
        { target: null },
      ),
    ).resolves.toMatchObject({ status: 201 });
  });

  it.each([
    [409, accumulatedProjectWire, "destination_busy"],
    [409, undefined, "destination_changed"],
    [404, undefined, "destination_unavailable"],
    [410, undefined, "destination_unavailable"],
    [429, undefined, "destination_capacity"],
    [400, undefined, "accumulation_unsupported"],
    [500, undefined, "destination_failed"],
  ])("classifies a %i refusal as %s", async (status, body, reason) => {
    const { client } = respond(status, body);
    const refusal = client.sendDestination(
      "production.admit.5",
      "admit_generation_destination",
      { target },
    );
    await expect(refusal).rejects.toBeInstanceOf(ProductionDestinationError);
    await expect(refusal).rejects.toMatchObject({ reason, status });
  });

  it("releases with 204 and refuses malformed destination input before any request", async () => {
    const { client, fetchApi } = respond(204);
    await expect(
      client.sendDestination(
        "production.release.1",
        "release_generation_destination",
        { admissionRequestId: "production.admit.1" },
      ),
    ).resolves.toEqual({ status: 204 });
    expect(
      JSON.parse(String(fetchApi.mock.calls[0]?.[1]?.body)).payload,
    ).toEqual({
      version: "h3.context.production_accumulation.v1",
      admission_request_id: "production.admit.1",
    });
    await expect(
      client.sendDestination(
        "production.admit.6",
        "admit_generation_destination",
        { target: { ...target, workspaceHandle: "ws_not_a_project" } },
      ),
    ).rejects.toThrow("workspace handle is invalid");
    expect(fetchApi).toHaveBeenCalledTimes(1);
  });

  it("settles an accepted pre-prepare terminal and decodes the retained empty project", async () => {
    const terminalMaterial = {
      ...emptyAccumulatedProjectMaterial,
      project_revision: 2,
      attempts: [
        {
          ...emptyAccumulatedProjectMaterial.attempts[0],
          status: "failed",
          recovery: "retry",
        },
      ],
    };
    const terminalWire = {
      ...terminalMaterial,
      project_fingerprint:
        productionAccumulatedProjectFingerprint(terminalMaterial),
    };
    const { client, fetchApi } = respond(200, terminalWire);
    await expect(
      client.sendDestination(
        "production.settle.1",
        "settle_generation_destination",
        {
          admissionRequestId: "production.admit.1",
          terminal: "failed",
        },
      ),
    ).resolves.toMatchObject({
      status: 200,
      project: {
        workspace: null,
        attempts: [{ status: "failed", recovery: "retry" }],
      },
    });
    expect(JSON.parse(String(fetchApi.mock.calls[0]?.[1]?.body))).toEqual({
      schema: "h3.context.production_workbench.action.v1",
      request_id: "production.settle.1",
      action: "settle_generation_destination_v2",
      payload: {
        version: "h3.context.production_accumulation.v1",
        admission_request_id: "production.admit.1",
        terminal: "failed",
      },
    });
  });
});
