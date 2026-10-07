import type { AutomaticPlanProjection } from "../../src/contracts/productionPlanningCodec";
import { decodeProductionWorkbenchProjection } from "../../src/contracts/productionWorkbenchCodec";
import { unavailableProductionAssemblyWire } from "./productionAssemblyWire";
export const fp = (digit: string) => `sha256:${digit.repeat(64)}`;
export const HANDLE = `pw_${"a".repeat(43)}`;
export const CONTEXT_HANDLE = `ws_${"b".repeat(43)}`;

export function productionProjection(
  fingerprint = fp("a"),
  overrides: Record<string, unknown> = {},
) {
  return decodeProductionWorkbenchProjection({
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: HANDLE,
    workspace_id: "workspace_actual",
    workspace_revision: 2,
    workspace_fingerprint: fingerprint,
    segments: [
      {
        segment_id: "segment_1",
        ordinal: 1,
        task_mode: "t2va",
        duration: {
          duration_milliseconds: 4167,
          delivered_milliseconds: 4458,
          frame_count: 107,
          snapped: true,
        },
        relation: "independent",
        predecessor_segment_id: null,
        boundary_kind: "independent",
        closure_state: "dirty_self",
        job_state: "planned",
        artifact_state: "unavailable",
        continuity_state: "unavailable",
        delivered_geometry: null,
      },
    ],
    selected_segment_ids: ["segment_1"],
    run: { state: "ready", completed: 0, total: 1 },
    generation_sequence: {
      schema: "h3.context.generation_sequence_projection.v1",
      sequence_id: "sequence.1",
      sequence_fingerprint: fp("b"),
      state_fingerprint: fp("c"),
      workspace_id: "workspace_actual",
      workspace_revision: 2,
      workspace_fingerprint: fingerprint,
      correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
    },
    reconstruction: { state: "unavailable" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: ["h3.context.generation_sequence_projection.v1"],
    outputs: [],
    allowed_actions: [
      "add_segment_from_context",
      "set_selection",
      "read_projection",
      "release_workspace",
    ],
    blocker_codes: [],
    limits: { max_segments: 64, max_outputs: 65 },
    ...overrides,
  });
}

export function planningProjection(overrides: Record<string, unknown> = {}) {
  return Object.freeze({
    schema: "h3.context.production_planning.projection.v1",
    request_id: "prepare",
    workspace_handle: HANDLE,
    workspace_id: "workspace_actual",
    workspace_revision: 2,
    workspace_fingerprint: fp("a"),
    planning_context_id: "planning_owned",
    planning_revision: 1,
    source_duration_seconds: 10,
    target_seconds: 20,
    policy: "fixed_10",
    admission_id: null,
    proposal: null,
    ...overrides,
  });
}

export function proposedProjection() {
  return planningProjection({
    admission_id: "admission_owned",
    proposal: {
      proposal_id: "proposal_owned",
      revision: 1,
      fingerprint: fp("6"),
      importable: true,
      blocker_codes: [],
      // A real planning context is always pending managed qualification (M26-04); only the
      // exact-plan readiness resolves this hold. A hold-free fixture hid B-M2522-START-01.
      start_hold_codes: ["managed_execution_qualification_pending"],
      segments: [1, 2].map((ordinal) => ({
        segment_id: `segment_${ordinal}`,
        ordinal,
        task_mode: "t2va",
        duration_seconds: 10,
        local_prompt: "A blue sphere turns.",
      })),
    },
  });
}

export function plan(
  overrides: Partial<AutomaticPlanProjection> = {},
): AutomaticPlanProjection {
  return {
    schema: "h3.context.production_automatic_plan_projection.v1",
    request_id: "import.plan",
    workspace_id: "workspace_actual",
    proposal_id: "proposal_owned",
    proposal_revision: 1,
    proposal_fingerprint: fp("7"),
    plan_fingerprint: fp("b"),
    workspace_revision: 2,
    workspace_fingerprint: fp("a"),
    segment_ids: ["segment.one", "segment.two"],
    materialization_receipt_fingerprints: [fp("1"), fp("2")],
    cut_boundary_receipts: [],
    manifest_fingerprints: [fp("8"), fp("9")],
    reconstruction_order: ["segment.one", "segment.two"],
    start_hold_codes: ["managed_execution_qualification_pending"],
    startable: false,
    ...overrides,
  };
}

export function readyReadiness(): Record<string, unknown> {
  return {
    schema: "h3.context.managed_readiness.v1",
    request_id: "readiness.1",
    status: "ready",
    reason: "qualified",
    qualification_fingerprint: fp("c"),
    qualification: {
      schema: "h3.context.managed_mode_qualification.v2",
      baseline: {
        schema: "h3.context.managed_mode_qualification.v1",
        host_capability_fingerprint: fp("d"),
        compiler_fingerprint: fp("e"),
        qualified_global_modes: ["t2va", "i2va", "fl2va", "l2va", "ref2va"],
        qualified_materialization_receipts: [fp("1"), fp("2")],
        observed_at: "4024000000000000",
        expires_at: "4051800000000000",
      },
      production_plan_fingerprint: fp("b"),
      composition_fingerprints: [fp("3"), fp("4")],
      guide_readiness: ["incomplete", "modified"],
      host_profile_fingerprint: fp("5"),
      asset_resolution_fingerprint: fp("6"),
    },
  };
}
