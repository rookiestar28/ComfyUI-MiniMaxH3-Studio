import { generationSequenceWire } from "../generationSequenceFixture";
import { unavailableProductionAssemblyWire } from "./productionAssemblyWire";

export type ManagedSequenceWire = ReturnType<typeof generationSequenceWire>;
export type ManagedLifecycleState =
  "planned" | "submitted" | "running" | "succeeded";

export type ManagedDurationWire = Readonly<{
  duration_milliseconds: number;
  delivered_milliseconds: number;
  frame_count: number;
  snapped: boolean;
}>;

const DEFAULT_DURATION: ManagedDurationWire = Object.freeze({
  duration_milliseconds: 8000,
  delivered_milliseconds: 8000,
  frame_count: 192,
  snapped: false,
});

export function productionProjectionWire(
  duration: ManagedDurationWire = DEFAULT_DURATION,
): Record<string, unknown> {
  return {
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"p".repeat(43)}`,
    workspace_id: `workspace_${"m".repeat(24)}`,
    workspace_revision: 1,
    workspace_fingerprint: `sha256:${"a".repeat(64)}`,
    segments: [
      {
        segment_id: "segment_1",
        ordinal: 1,
        task_mode: "t2va",
        duration: structuredClone(duration),
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

function sequenceState(
  planned: ManagedSequenceWire,
  state: ManagedLifecycleState,
  queuePromptId: string,
): ManagedSequenceWire {
  const sequence = structuredClone(planned);
  const progress = sequence.progress[0]!;
  sequence.state_fingerprint = `sha256:${(
    { planned: "b", submitted: "c", running: "d", succeeded: "e" } as const
  )[state].repeat(64)}`;
  progress.state = state;
  if (state !== "planned") {
    sequence.eligible_commands = [];
    Object.assign(progress, {
      transaction_id: "generation.command.1",
      queue_prompt_id: queuePromptId,
    });
  }
  if (state === "running" || state === "succeeded")
    Object.assign(progress, { host_owner_id: queuePromptId });
  if (state === "succeeded") {
    sequence.complete = true;
    Object.assign(progress, {
      artifact_receipt_fingerprint: `sha256:${"6".repeat(64)}`,
      artifact_output_fingerprint: `sha256:${"7".repeat(64)}`,
    });
  }
  return sequence;
}

function productionState(
  sequence: ManagedSequenceWire,
  state: ManagedLifecycleState,
  commandSource: ManagedSequenceWire,
  duration: ManagedDurationWire,
): Record<string, unknown> {
  const production = productionProjectionWire(duration);
  const command = commandSource.eligible_commands[0]!;
  const segment = (production.segments as Array<Record<string, unknown>>)[0]!;
  Object.assign(segment, {
    segment_id: command.segment_id,
    ordinal: command.ordinal,
    task_mode: command.task_mode,
    duration: structuredClone(command.duration),
    closure_state: state === "succeeded" ? "clean" : "dirty_self",
    job_state: state,
    artifact_state: state === "succeeded" ? "complete" : "unavailable",
  });
  Object.assign(production, {
    workspace_id: sequence.workspace_id,
    workspace_revision: sequence.workspace_revision,
    workspace_fingerprint: sequence.workspace_fingerprint,
    selected_segment_ids: [command.segment_id],
    run: {
      state:
        state === "planned"
          ? "ready"
          : state === "succeeded"
            ? "succeeded"
            : "running",
      completed: state === "succeeded" ? 1 : 0,
      total: 1,
    },
    generation_sequence: {
      schema: sequence.schema,
      sequence_id: sequence.sequence_id,
      sequence_fingerprint: sequence.sequence_fingerprint,
      state_fingerprint: sequence.state_fingerprint,
      workspace_id: sequence.workspace_id,
      workspace_revision: sequence.workspace_revision,
      workspace_fingerprint: sequence.workspace_fingerprint,
      correlation: {
        prompt_id: sequence.correlation.prompt_id,
        execution_node_id: sequence.correlation.execution_node_id,
      },
    },
    authority_versions:
      state === "succeeded"
        ? [sequence.schema, "h3.context.segment_artifact_receipt.v1"]
        : [sequence.schema],
    outputs:
      state === "succeeded"
        ? [
            {
              output_handle: `out_${"o".repeat(24)}`,
              ordinal: 1,
              state: "ready",
              segment_id: command.segment_id,
              preview: true,
            },
          ]
        : [],
    allowed_actions: [
      ...(production.allowed_actions as string[]),
      ...(state === "planned" ? ["submit_generation_job"] : []),
      ...(state === "succeeded" ? ["preview_output"] : []),
    ],
    blocker_codes: [],
  });
  return production;
}

export function managedCoordinatorResponse(
  disposition: string,
  sequence: ManagedSequenceWire,
  production: Record<string, unknown>,
): Record<string, unknown> {
  return {
    schema: "h3.context.generation_coordinator.response.v1",
    run_handle: `mc_${"m".repeat(40)}`,
    disposition,
    artifact_authority: null,
    terminal_fingerprint: null,
    sequence,
    production,
  };
}

export function managedMemberCoordinatorResponse(
  disposition: string,
  sequence: ManagedSequenceWire,
  production: Record<string, unknown>,
  memberSegmentId: string,
): Record<string, unknown> {
  const wire = managedCoordinatorResponse(disposition, sequence, production);
  delete wire.production;
  wire.schema = "h3.context.generation_coordinator.managed_member_response.v1";
  wire.production_member_authority = {
    schema: "h3.context.generation_coordinator.production_member_authority.v1",
    workspace_handle: production.workspace_handle,
    workspace_id: production.workspace_id,
    member_segment_id: memberSegmentId,
  };
  return wire;
}

export function managedLifecycleWires(
  graphFingerprint: string,
  compiledPromptFingerprint = `sha256:${"2".repeat(64)}`,
  productShellNodeId = "17",
  queuePromptId = "prompt.model.1",
  duration: ManagedDurationWire = DEFAULT_DURATION,
) {
  const workspaceId = `ws_${"m".repeat(40)}`;
  const productionWorkspaceId = `workspace_${"m".repeat(24)}`;
  const planned = generationSequenceWire();
  Object.assign(planned, {
    sequence_id: "sequence.1",
    sequence_fingerprint: `sha256:${"d".repeat(64)}`,
    state_fingerprint: `sha256:${"b".repeat(64)}`,
    workspace_id: productionWorkspaceId,
    workspace_revision: 1,
    workspace_fingerprint: `sha256:${"a".repeat(64)}`,
    correlation: {
      prompt_id: queuePromptId,
      execution_node_id: productShellNodeId,
    },
  });
  Object.assign(planned.progress[0]!, {
    job_id: "job.1",
    segment_id: "segment_1",
    ordinal: 1,
    state: "planned",
    attempt: 1,
    transaction_id: "generation.command.1",
  });
  Object.assign(planned.eligible_commands[0]!, {
    job_id: "job.1",
    segment_id: "segment_1",
    ordinal: 1,
    task_mode: "t2va",
    source_id: "report-1",
    reference_ids: [],
    duration: structuredClone(duration),
    graph_fingerprint: graphFingerprint,
    compiled_prompt_fingerprint: compiledPromptFingerprint,
    expected_shape: [duration.frame_count, 512, 512, 3],
    attempt: 1,
    transaction_id: "generation.command.1",
  });
  const submitted = sequenceState(planned, "submitted", queuePromptId);
  const running = sequenceState(planned, "running", queuePromptId);
  const succeeded = sequenceState(planned, "succeeded", queuePromptId);
  const initialProduction = productionProjectionWire(duration);
  initialProduction.workspace_id = productionWorkspaceId;
  return {
    workspaceId,
    productionWorkspaceId,
    planned,
    submitted,
    running,
    succeeded,
    initialProduction,
    production: {
      planned: productionState(planned, "planned", planned, duration),
      submitted: productionState(submitted, "submitted", planned, duration),
      running: productionState(running, "running", planned, duration),
      succeeded: productionState(succeeded, "succeeded", planned, duration),
    },
  } as const;
}
