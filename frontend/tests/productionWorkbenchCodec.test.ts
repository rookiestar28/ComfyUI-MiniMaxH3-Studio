import { describe, expect, it } from "vitest";

import {
  decodeProductionWorkbenchProjection,
  encodeProductionAction,
} from "../src/contracts/productionWorkbenchCodec";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";

const fingerprint = `sha256:${"a".repeat(64)}`;
const fp = (value: string) => `sha256:${value.repeat(64)}`;

function wire() {
  return {
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
        closure_state: "clean",
        job_state: "clean",
        artifact_state: "complete",
        continuity_state: "unavailable",
        delivered_geometry: {
          format: "webm",
          frame_count: 124,
          width: 768,
          height: 512,
        },
      },
    ],
    selected_segment_ids: ["segment_1"],
    run: { state: "succeeded", completed: 1, total: 1 },
    generation_sequence: {
      schema: "h3.context.generation_sequence_projection.v1",
      sequence_id: "sequence.1",
      sequence_fingerprint: fp("8"),
      state_fingerprint: fp("9"),
      workspace_id: "workspace_1",
      workspace_revision: 1,
      workspace_fingerprint: fingerprint,
      correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
    },
    reconstruction: { state: "complete" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: [
      "h3.context.generation_sequence_projection.v1",
      "h3.context.segment_artifact_receipt.v1",
      "h3.context.av_reconstruction_receipt.v1",
    ],
    outputs: [],
    allowed_actions: [
      "add_segment_from_context",
      "replace_segment_from_context",
      "set_segment_relation",
      "set_selection",
      "read_projection",
      "release_workspace",
    ],
    blocker_codes: ["sequence_authority_unavailable"],
    limits: { max_segments: 64, max_outputs: 65 },
  };
}

describe("M17-12 Production workbench wire", () => {
  it("imports retained original rows after a completed automatic assembly", () => {
    const base = wire();
    const original = {
      output_handle: `out_${"o".repeat(24)}`,
      ordinal: 1,
      state: "ready",
      segment_id: "segment_1",
      preview: false,
    };
    const aggregate = {
      output_handle: `out_${"a".repeat(24)}`,
      ordinal: 2,
      state: "ready",
      segment_id: null,
      preview: false,
    };
    const candidate = {
      ...base,
      outputs: [original, aggregate],
      authority_versions: [
        ...base.authority_versions,
        "h3.context.m26.production_assembly_receipt.v1",
      ],
      allowed_actions: [
        ...base.allowed_actions,
        "import_production_outputs_to_authoring",
      ],
      assembly: {
        ...base.assembly,
        state: "succeeded",
        progress: { completed: 1, total: 1 },
        capability_fingerprint: fp("b"),
        managed_sequence_fingerprint: fp("c"),
        artifact_receipt_fingerprints: [fp("d")],
        assembly_job_id: "assembly.original",
        authorization_fingerprint: fp("e"),
        receipt_fingerprint: fp("f"),
        failure_code: null,
      },
    };
    const decoded = decodeProductionWorkbenchProjection(candidate);
    expect(decoded.allowedActions).toContain(
      "import_production_outputs_to_authoring",
    );
    expect(decoded.outputs.map((output) => output.segmentId)).toEqual([
      "segment_1",
      null,
    ]);
    expect(decoded.outputs[0]?.outputHandle).toBe(original.output_handle);
    for (const invalid of [
      { ...candidate, assembly: base.assembly },
      { ...candidate, authority_versions: base.authority_versions },
      { ...candidate, outputs: [{ ...aggregate, ordinal: 1 }] },
    ]) {
      expect(() => decodeProductionWorkbenchProjection(invalid)).toThrow(
        /authoring import/i,
      );
    }
  });

  it("decodes the exact allowlist and freezes nested values", () => {
    const decoded = decodeProductionWorkbenchProjection(wire());
    expect(decoded.workspaceRevision).toBe(1);
    expect(decoded.segments[0]?.duration).toEqual({
      requestedMilliseconds: 5167,
      deliveredMilliseconds: 5167,
      frameCount: 124,
      snapped: false,
    });
    expect(decoded.runProgress).toEqual({ completed: 1, total: 1 });
    expect(decoded.generationSequence).toMatchObject({
      sequenceId: "sequence.1",
      stateFingerprint: fp("9"),
      correlation: { promptId: "prompt.1", executionNodeId: "node.1" },
    });
    expect(decoded.reconstructionState).toBe("complete");
    expect(decoded.segments[0]?.deliveredGeometry).toEqual({
      format: "webm",
      frameCount: 124,
      width: 768,
      height: 512,
    });
    expect(Object.isFrozen(decoded)).toBe(true);
    expect(Object.isFrozen(decoded.segments)).toBe(true);
    expect(Object.isFrozen(decoded.segments[0]?.deliveredGeometry)).toBe(true);
  });

  it("accepts only a same-workspace historical generation summary", () => {
    const historical = wire();
    historical.workspace_revision = 2;
    historical.workspace_fingerprint = fp("b");
    historical.blocker_codes = [];
    const decoded = decodeProductionWorkbenchProjection(historical);
    expect(decoded.workspaceRevision).toBe(2);
    expect(decoded.generationSequence?.workspaceRevision).toBe(1);
    expect(decoded.generationSequence?.workspaceFingerprint).toBe(fingerprint);

    const foreign = structuredClone(historical);
    foreign.generation_sequence!.workspace_id = "workspace_foreign";
    expect(() => decodeProductionWorkbenchProjection(foreign)).toThrow(
      /does not match/i,
    );

    const future = structuredClone(historical);
    future.generation_sequence!.workspace_revision = 3;
    expect(() => decodeProductionWorkbenchProjection(future)).toThrow(
      /does not match/i,
    );

    const sameRevisionFork = structuredClone(historical);
    sameRevisionFork.generation_sequence!.workspace_revision = 2;
    sameRevisionFork.generation_sequence!.workspace_fingerprint = fp("c");
    expect(() => decodeProductionWorkbenchProjection(sameRevisionFork)).toThrow(
      /does not match/i,
    );
  });

  it("encodes a handle-only reload read without a canonical browser copy", () => {
    expect(
      encodeProductionAction("request.restore", "read_projection", {
        workspaceHandle: `pw_${"a".repeat(43)}`,
      }),
    ).toMatchObject({
      action: "read_projection",
      payload: { workspace_handle: `pw_${"a".repeat(43)}` },
    });
  });

  it("rejects unknown members, private-looking fields, bad counts and duplicates", () => {
    expect(() =>
      decodeProductionWorkbenchProjection({ ...wire(), prompt: "private" }),
    ).toThrow(/closed/i);
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...wire(),
        selected_segment_ids: ["segment_1", "segment_1"],
      }),
    ).toThrow(/duplicate/i);
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...wire(),
        limits: { max_segments: 65, max_outputs: 65 },
      }),
    ).toThrow(/limit/i);
    const premature = wire();
    premature.segments[0]!.artifact_state = "unavailable";
    expect(() => decodeProductionWorkbenchProjection(premature)).toThrow(
      /delivered geometry/i,
    );
  });

  it("encodes only the closed action payload without canonical local mutation", () => {
    const projection = decodeProductionWorkbenchProjection(wire());
    expect(
      encodeProductionAction("request.select", "set_selection", {
        projection,
        segmentIds: [],
      }),
    ).toEqual({
      schema: "h3.context.production_workbench.action.v1",
      request_id: "request.select",
      action: "set_selection",
      payload: {
        workspace_handle: projection.workspaceHandle,
        expected_workspace_revision: 1,
        expected_workspace_fingerprint: fingerprint,
        segment_ids: [],
      },
    });
  });

  it("encodes ready M26 assembly from projected identities without locator authority", () => {
    const base = wire();
    const candidate = {
      ...base,
      assembly: {
        schema: "h3.context.production_assembly.projection.v1",
        state: "unavailable",
        progress: { completed: 0, total: 1 },
        capability_fingerprint: fp("b"),
        managed_sequence_fingerprint: fp("c"),
        artifact_receipt_fingerprints: [fp("d")],
        cut_boundary_receipt_fingerprints: [],
        output_profile_id: "legacy_av_30fps_48khz_stereo",
        assembly_job_id: null,
        authorization_fingerprint: null,
        receipt_fingerprint: null,
        failure_code: null,
        projection_fingerprint: fp("e"),
      },
      allowed_actions: [...base.allowed_actions, "assemble_sequence"],
    };
    const projection = decodeProductionWorkbenchProjection(candidate);
    const action = encodeProductionAction(
      "request.assemble",
      "assemble_sequence",
      { projection },
    );

    expect(action).toEqual({
      schema: "h3.context.production_workbench.action.v1",
      request_id: "request.assemble",
      action: "assemble_sequence",
      payload: {
        workspace_handle: projection.workspaceHandle,
        workspace_id: projection.workspaceId,
        expected_workspace_revision: projection.workspaceRevision,
        expected_workspace_fingerprint: projection.workspaceFingerprint,
        managed_sequence_fingerprint: fp("c"),
        artifact_receipt_fingerprints: [fp("d")],
        cut_boundary_receipt_fingerprints: [],
        assembly_capability_fingerprint: fp("b"),
        output_profile_id: "legacy_av_30fps_48khz_stereo",
      },
    });
    expect(JSON.stringify(action)).not.toMatch(
      /path|url|locator|derived_lease|private_store|approval/i,
    );
    expect(() =>
      encodeProductionAction("request.assemble.extra", "assemble_sequence", {
        projection,
        path: "private/output.mp4",
      } as never),
    ).toThrow(/closed/i);
  });

  it("rejects forbidden action inputs instead of ignoring or inferring them", () => {
    const projection = decodeProductionWorkbenchProjection(wire());
    expect(() =>
      encodeProductionAction("request.unknown", "set_selection", {
        projection,
        segmentIds: ["segment_unknown"],
      }),
    ).toThrow(/unknown/i);
    expect(() =>
      encodeProductionAction("request.relation", "set_segment_relation", {
        projection,
        segmentId: "segment_1",
        relation: "predecessor",
        predecessorSegmentId: null,
      }),
    ).toThrow(/predecessor/i);
    expect(() =>
      encodeProductionAction("request.delete", "delete_segment", {
        projection,
        segmentId: "segment_1",
      }),
    ).toThrow(/not allowed/i);
    expect(() =>
      encodeProductionAction("request.extra", "set_selection", {
        projection,
        segmentIds: [],
        prompt: "must not be ignored",
      } as never),
    ).toThrow(/closed/i);
  });

  it("rejects cross-field projection drift and duplicate opaque outputs", () => {
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...wire(),
        segments: [
          {
            ...wire().segments[0],
            boundary_kind: "cut",
          },
        ],
      }),
    ).toThrow(/boundary/i);
    const output = {
      output_handle: `out_${"o".repeat(24)}`,
      ordinal: 1,
      state: "ready",
      segment_id: null,
      preview: false,
    };
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...wire(),
        outputs: [output, { ...output, ordinal: 2 }],
      }),
    ).toThrow(/duplicate/i);
  });

  it("decodes content-free aggregate and segment preview linkage", () => {
    const candidate = wire();
    const outputs = [
      {
        output_handle: `out_${"a".repeat(24)}`,
        ordinal: 1,
        state: "ready",
        segment_id: null,
        preview: false,
      },
      {
        output_handle: `out_${"b".repeat(24)}`,
        ordinal: 2,
        state: "ready",
        segment_id: "segment_1",
        preview: true,
      },
    ];
    const decoded = decodeProductionWorkbenchProjection({
      ...candidate,
      outputs,
      allowed_actions: [...candidate.allowed_actions, "preview_output"],
    });

    expect(decoded.outputs).toEqual([
      {
        outputHandle: outputs[0]!.output_handle,
        ordinal: 1,
        state: "ready",
        segmentId: null,
        preview: false,
      },
      {
        outputHandle: outputs[1]!.output_handle,
        ordinal: 2,
        state: "ready",
        segmentId: "segment_1",
        preview: true,
      },
    ]);
    expect(Object.isFrozen(decoded.outputs)).toBe(true);

    const missingField = structuredClone(outputs[1]) as Record<string, unknown>;
    delete missingField.preview;
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...candidate,
        outputs: [outputs[0], missingField],
      }),
    ).toThrow(/closed/i);
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...candidate,
        outputs: [{ ...outputs[1], segment_id: "segment.unknown" }],
        allowed_actions: [...candidate.allowed_actions, "preview_output"],
      }),
    ).toThrow(/segment/i);
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...candidate,
        outputs,
      }),
    ).toThrow(/preview/i);
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...candidate,
        outputs: [{ ...outputs[1], state: "pending" }],
        allowed_actions: [...candidate.allowed_actions, "preview_output"],
      }),
    ).toThrow(/preview/i);
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...candidate,
        outputs: outputs.map((output) => ({
          ...output,
          segment_id: "segment_1",
        })),
        allowed_actions: [...candidate.allowed_actions, "preview_output"],
      }),
    ).toThrow(/aggregate/i);
  });

  it("decodes a generated segment preview before aggregate reconstruction", () => {
    const candidate = wire();
    const segmentOutput = {
      output_handle: `out_${"g".repeat(24)}`,
      ordinal: 1,
      state: "ready",
      segment_id: "segment_1",
      preview: true,
    };
    const decoded = decodeProductionWorkbenchProjection({
      ...candidate,
      reconstruction: { state: "unavailable" },
      outputs: [segmentOutput],
      allowed_actions: [...candidate.allowed_actions, "preview_output"],
    });

    expect(decoded.outputs[0]).toMatchObject({
      outputHandle: segmentOutput.output_handle,
      segmentId: "segment_1",
      preview: true,
    });
    expect(() =>
      decodeProductionWorkbenchProjection({
        ...candidate,
        reconstruction: { state: "unavailable" },
        outputs: [{ ...segmentOutput, segment_id: null }],
        allowed_actions: [...candidate.allowed_actions, "preview_output"],
      }),
    ).toThrow(/aggregate/i);
  });

  // M17-25, distinct review finding: the duration consistency check was the one
  // invariant this codec adds over its sibling, and only the sibling's copy of it
  // was tested. A backend that claimed a segment moved when it did not -- or that
  // it did not when it moved -- would have been decoded without complaint here.
  it("rejects a duration whose snapped flag disagrees with its two lengths", () => {
    const withDuration = (duration: Record<string, unknown>) => () => {
      const candidate = wire();
      const frameCount = duration.frame_count;
      return decodeProductionWorkbenchProjection({
        ...candidate,
        segments: [
          {
            ...candidate.segments[0],
            duration,
            delivered_geometry: {
              ...candidate.segments[0]!.delivered_geometry,
              frame_count: frameCount,
            },
          },
        ],
      });
    };

    expect(
      withDuration({
        duration_milliseconds: 5167,
        delivered_milliseconds: 5167,
        frame_count: 124,
        snapped: true,
      }),
    ).toThrow(/snapped/i);
    expect(
      withDuration({
        duration_milliseconds: 4167,
        delivered_milliseconds: 4458,
        frame_count: 107,
        snapped: false,
      }),
    ).toThrow(/snapped/i);
    // A derived frame count above the host ceiling is refused rather than
    // carried; this bound used to be a day of milliseconds.
    expect(
      withDuration({
        duration_milliseconds: 5167,
        delivered_milliseconds: 5167,
        frame_count: 3_601,
        snapped: false,
      }),
    ).toThrow(/frame count/i);
    // The honest pairing still decodes, so the guard rejects disagreement rather
    // than rejecting movement.
    expect(
      withDuration({
        duration_milliseconds: 4167,
        delivered_milliseconds: 4458,
        frame_count: 107,
        snapped: true,
      }),
    ).not.toThrow();
  });
});
