import { describe, expect, it, vi } from "vitest";

import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { createProductionActionClient } from "../src/host/productionActions";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";

const fingerprint = `sha256:${"a".repeat(64)}`;

function unavailableProjectionWire() {
  return {
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"a".repeat(43)}`,
    workspace_id: "workspace_qualification",
    workspace_revision: 2,
    workspace_fingerprint: fingerprint,
    segments: [
      {
        segment_id: "segment_1",
        ordinal: 1,
        task_mode: "t2va",
        duration: {
          duration_milliseconds: 10000,
          delivered_milliseconds: 10000,
          frame_count: 240,
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
    blocker_codes: [
      "sequence_authority_unavailable",
      "assembly_unavailable:media_runtime_not_authorized",
    ],
    limits: { max_segments: 64, max_outputs: 65 },
  };
}

describe("M26-05 candidate assembly qualification", () => {
  it("decodes the current unavailable projection and refuses assembly before transport", async () => {
    const wire = unavailableProjectionWire();
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => wire,
    }));
    const client = createProductionActionClient({ fetchApi });

    const read = await client.send(
      "m26.qualification.read",
      "read_projection",
      {
        workspaceHandle: wire.workspace_handle,
      },
    );
    expect(read.projection).toEqual(decodeProductionWorkbenchProjection(wire));
    expect(read.projection?.assembly).toMatchObject({
      state: "unavailable",
      capabilityFingerprint: null,
      managedSequenceFingerprint: null,
      failureCode: "media_runtime_not_authorized",
    });
    expect(read.projection?.allowedActions).not.toContain("assemble_sequence");

    await expect(
      client.send("m26.qualification.assemble", "assemble_sequence", {
        projection: read.projection!,
      }),
    ).rejects.toThrow("production action is not allowed");
    expect(fetchApi).toHaveBeenCalledTimes(1);
  });

  it("rejects a non-closed HTTP response through the production client", async () => {
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        ...unavailableProjectionWire(),
        private_path: "hidden",
      }),
    }));
    const client = createProductionActionClient({ fetchApi });
    await expect(
      client.send("m26.qualification.closed", "read_projection", {
        workspaceHandle: `pw_${"a".repeat(43)}`,
      }),
    ).rejects.toThrow("production projection must be closed");
    expect(fetchApi).toHaveBeenCalledTimes(1);
  });
});
