import type { GenerationSequenceProjection } from "../contracts/generationSequenceCodec";
import type { ProductionWorkbenchProjection } from "../contracts/productionWorkbenchCodec";
import type { AppModeInputs, AppModeStartOptions } from "./appMode";
import type { GenerationSequenceBinding } from "./generationSequence";

export type ProductionGenerationWorkspaceAuthority = Readonly<{
  reportId: string;
  taskMode: AppModeInputs["task_mode"];
  requestedDurationMilliseconds: number;
  effectiveDurationMilliseconds: number;
  frameCount: number;
  referenceIds: readonly string[];
}>;

export type ProductionGenerationRequest = Readonly<{
  inputs: AppModeInputs;
  options?: AppModeStartOptions;
}>;

export const MAX_PRODUCTION_GENERATION_KEYS = 512;

export type ProductionGenerationDisposition =
  | "available"
  | "pending"
  | "submitted"
  | "generation_failed"
  | "generation_control_capacity";

type ProductionGenerationControlErrorCode =
  "generation_control_unavailable" | "generation_control_capacity";

const productionGenerationControlError = (
  code: ProductionGenerationControlErrorCode,
) => Object.assign(new Error(code), { code });

export function productionGenerationKey(
  sequence: GenerationSequenceProjection,
  jobId: string,
): string {
  const command = sequence.eligible_commands.find(
    (candidate) => candidate.job_id === jobId,
  );
  if (command === undefined)
    throw productionGenerationControlError("generation_control_unavailable");
  return `${sequence.sequence_fingerprint}:${sequence.state_fingerprint}:${command.job_id}:${command.attempt}`;
}

export function createProductionGenerationController() {
  const dispositions = new Map<
    string,
    Exclude<
      ProductionGenerationDisposition,
      "available" | "generation_control_capacity"
    >
  >();
  const disposition = (key: string): ProductionGenerationDisposition => {
    const retained = dispositions.get(key);
    if (retained !== undefined) return retained;
    return dispositions.size >= MAX_PRODUCTION_GENERATION_KEYS
      ? "generation_control_capacity"
      : "available";
  };
  return Object.freeze({
    get size(): number {
      return dispositions.size;
    },
    disposition,
    async execute<T>(key: string, driver: () => Promise<T>): Promise<T> {
      const current = disposition(key);
      if (current === "generation_control_capacity")
        throw productionGenerationControlError("generation_control_capacity");
      if (current !== "available")
        throw productionGenerationControlError(
          "generation_control_unavailable",
        );
      // IMPORTANT: keys are never evicted or reset during the module lifetime. This is the
      // bounded ownership guard that prevents remounts and late settlements from resubmitting.
      dispositions.set(key, "pending");
      try {
        const result = await driver();
        dispositions.set(key, "submitted");
        return result;
      } catch (error) {
        dispositions.set(key, "generation_failed");
        throw error;
      }
    },
  });
}

export function joinProductionGenerationSequence(
  production: ProductionWorkbenchProjection | undefined,
  sequence: GenerationSequenceProjection | undefined,
): GenerationSequenceProjection | undefined {
  const summary = production?.generationSequence;
  if (
    production === undefined ||
    sequence === undefined ||
    summary == null ||
    !production.allowedActions.includes("submit_generation_job") ||
    summary.schema !== sequence.schema ||
    summary.sequenceId !== sequence.sequence_id ||
    summary.sequenceFingerprint !== sequence.sequence_fingerprint ||
    summary.stateFingerprint !== sequence.state_fingerprint ||
    summary.workspaceId !== sequence.workspace_id ||
    summary.workspaceRevision !== sequence.workspace_revision ||
    summary.workspaceFingerprint !== sequence.workspace_fingerprint ||
    summary.correlation.promptId !== sequence.correlation.prompt_id ||
    summary.correlation.executionNodeId !==
      sequence.correlation.execution_node_id
  )
    return undefined;
  return sequence;
}

export function buildProductionGenerationBinding({
  production,
  sequence,
  jobId,
  workspace,
  request,
}: Readonly<{
  production: ProductionWorkbenchProjection;
  sequence: GenerationSequenceProjection;
  jobId: string;
  workspace: ProductionGenerationWorkspaceAuthority;
  request: ProductionGenerationRequest;
}>): GenerationSequenceBinding | undefined {
  const joined = joinProductionGenerationSequence(production, sequence);
  const command = joined?.eligible_commands.find(
    (item) => item.job_id === jobId,
  );
  const segment = production.segments.find(
    (item) => item.segmentId === command?.segment_id,
  );
  // CRITICAL (B-M1605-EXIST-02): the existing route never authors task mode; M24-07 keeps the
  // Request's own mode, so the executed workspace is the authority there and the retained inputs
  // adopt it. Requiring Sidebar parity refuses every existing graph whose Request mode differs
  // from the Sidebar selection, after ComfyUI has already accepted the generation, as ambiguous
  // host ownership. Routes that write the task mode keep the parity check.
  const existing = request.options?.useExisting === true;
  // IMPORTANT: authored and effective lattice durations are independent
  // authorities. Collapsing them falsely rejects a valid snapped request or
  // admits a request rewritten after Context planning.
  if (
    command === undefined ||
    segment === undefined ||
    !production.selectedSegmentIds.includes(segment.segmentId) ||
    segment.ordinal !== command.ordinal ||
    segment.taskMode !== command.task_mode ||
    segment.jobState !== "planned" ||
    segment.duration.requestedMilliseconds !==
      command.duration.duration_milliseconds ||
    segment.duration.frameCount !== command.duration.frame_count ||
    workspace.reportId !== command.source_id ||
    workspace.taskMode !== command.task_mode ||
    workspace.effectiveDurationMilliseconds !==
      command.duration.duration_milliseconds ||
    workspace.frameCount !== command.duration.frame_count ||
    workspace.referenceIds.length !== command.reference_ids.length ||
    !workspace.referenceIds.every(
      (value, index) => value === command.reference_ids[index],
    ) ||
    (!existing && request.inputs.task_mode !== workspace.taskMode) ||
    request.inputs.duration_milliseconds !==
      workspace.requestedDurationMilliseconds ||
    request.inputs.frame_count !== workspace.frameCount
  )
    return undefined;
  return Object.freeze({
    jobId: command.job_id,
    sourceId: workspace.reportId,
    referenceIds: [...workspace.referenceIds],
    requestedDurationMilliseconds: workspace.requestedDurationMilliseconds,
    effectiveDurationMilliseconds: workspace.effectiveDurationMilliseconds,
    route: existing
      ? "existing"
      : request.options?.replaceExisting
        ? "replace"
        : "new",
    inputs: existing
      ? { ...request.inputs, task_mode: workspace.taskMode }
      : request.inputs,
  });
}
