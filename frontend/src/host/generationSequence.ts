import type {
  GenerationFingerprintDomain,
  GenerationSequenceCommand,
  GenerationSequenceProjection,
} from "../contracts/generationSequenceCodec";
import type {
  AppModeInputs,
  AppModeStartOptions,
  AppModeStartResult,
} from "./appMode";

export const GENERATION_SEQUENCE_HOST_OBSERVATION_SCHEMA =
  "h3.context.generation_sequence_host_observation.v1" as const;

export type GenerationSequenceBinding = {
  jobId: string;
  sourceId: string;
  referenceIds: string[];
  route: "new" | "replace" | "existing";
  requestedDurationMilliseconds: number;
  effectiveDurationMilliseconds: number;
  inputs: AppModeInputs;
};

export type GenerationSequenceHostObservation = {
  schema: typeof GENERATION_SEQUENCE_HOST_OBSERVATION_SCHEMA;
  sequence_id: string;
  sequence_fingerprint: string;
  state_fingerprint: string;
  job_id: string;
  segment_id: string;
  attempt: number;
  transaction_id: string;
  graph_fingerprint: string;
  compiled_prompt_fingerprint: string;
  queue_prompt_id: string;
  // M17-20 D6: the observation reports which graph it measured, so the sequence
  // authority compares like with like instead of reading a domain change as a
  // drifted graph.
  fingerprint_domain: GenerationFingerprintDomain;
};

type AppModeStart = (
  inputs: AppModeInputs,
  options?: AppModeStartOptions,
) => Promise<AppModeStartResult>;

export type GenerationSequenceSubmissionClaim = Readonly<{
  command: GenerationSequenceCommand;
  expectedIdentity: Readonly<{
    graphFingerprint: string;
    compiledPromptFingerprint: string;
  }>;
  onQueueSubmitted(): void;
  complete(result: AppModeStartResult): GenerationSequenceHostObservation;
  fail(): void;
}>;

export class GenerationSequenceDriverError extends Error {
  readonly code:
    | "job_not_eligible"
    | "input_authority_mismatch"
    | "duplicate_submission"
    | "ambiguous_host_ownership"
    | "host_identity_mismatch";

  constructor(code: GenerationSequenceDriverError["code"]) {
    super(code);
    this.name = "GenerationSequenceDriverError";
    this.code = code;
  }
}

function sameIdentifiers(
  left: readonly string[],
  right: readonly string[],
): boolean {
  return (
    left.length === right.length &&
    left.every((item, index) => item === right[index])
  );
}

function verifyBinding(
  command: GenerationSequenceCommand,
  binding: GenerationSequenceBinding,
): void {
  if (
    binding.sourceId !== command.source_id ||
    !sameIdentifiers(binding.referenceIds, command.reference_ids) ||
    binding.inputs.task_mode !== command.task_mode
  )
    throw new GenerationSequenceDriverError("input_authority_mismatch");
  // IMPORTANT: the input carries authored duration while the command carries
  // effective lattice duration. Collapsing them rejects valid snapped requests;
  // each must remain joined to its own explicit binding authority.
  if (
    binding.inputs.duration_milliseconds !==
      binding.requestedDurationMilliseconds ||
    binding.effectiveDurationMilliseconds !==
      command.duration.duration_milliseconds ||
    binding.inputs.frame_count !== command.duration.frame_count
  )
    throw new GenerationSequenceDriverError("input_authority_mismatch");
}

function commandKey(
  projection: GenerationSequenceProjection,
  command: GenerationSequenceCommand,
): string {
  return `${projection.sequence_fingerprint}:${command.job_id}:${command.attempt}`;
}

export function createGenerationSequenceDriver(start: AppModeStart) {
  const pending = new Set<string>();
  const hostOwned = new Set<string>();
  const completed = new Set<string>();
  const claim = (
    projection: GenerationSequenceProjection,
    binding: GenerationSequenceBinding,
  ): GenerationSequenceSubmissionClaim => {
    const command = projection.eligible_commands.find(
      (candidate) => candidate.job_id === binding.jobId,
    );
    if (command === undefined)
      throw new GenerationSequenceDriverError("job_not_eligible");
    verifyBinding(command, binding);
    const key = commandKey(projection, command);
    if (completed.has(key))
      throw new GenerationSequenceDriverError("duplicate_submission");
    if (hostOwned.has(key))
      throw new GenerationSequenceDriverError("ambiguous_host_ownership");
    if (pending.has(key))
      throw new GenerationSequenceDriverError("duplicate_submission");
    pending.add(key);
    let submitted = false;
    let settled = false;
    const markSubmitted = (): void => {
      if (settled) return;
      submitted = true;
      hostOwned.add(key);
    };
    return Object.freeze({
      command,
      expectedIdentity: Object.freeze({
        graphFingerprint: command.graph_fingerprint,
        compiledPromptFingerprint: command.compiled_prompt_fingerprint,
      }),
      onQueueSubmitted: markSubmitted,
      complete(result: AppModeStartResult): GenerationSequenceHostObservation {
        if (settled)
          throw new GenerationSequenceDriverError("duplicate_submission");
        markSubmitted();
        if (
          result.compiledPromptFingerprint !==
          command.compiled_prompt_fingerprint
        ) {
          settled = true;
          pending.delete(key);
          throw new GenerationSequenceDriverError("host_identity_mismatch");
        }
        settled = true;
        pending.delete(key);
        hostOwned.delete(key);
        completed.add(key);
        return Object.freeze({
          schema: GENERATION_SEQUENCE_HOST_OBSERVATION_SCHEMA,
          sequence_id: projection.sequence_id,
          sequence_fingerprint: projection.sequence_fingerprint,
          state_fingerprint: projection.state_fingerprint,
          job_id: command.job_id,
          segment_id: command.segment_id,
          attempt: command.attempt,
          transaction_id: command.transaction_id,
          graph_fingerprint: result.graphFingerprint,
          compiled_prompt_fingerprint: result.compiledPromptFingerprint,
          queue_prompt_id: result.queuePromptId,
          fingerprint_domain: command.fingerprint_domain,
        });
      },
      fail(): void {
        if (settled) return;
        settled = true;
        pending.delete(key);
        // Once the queue call was made, retain the host-owned guard forever.
        // A remount or retry cannot truthfully prove that work was not accepted.
        if (!submitted) hostOwned.delete(key);
      },
    });
  };
  return Object.freeze({
    claim,
    async execute(
      projection: GenerationSequenceProjection,
      binding: GenerationSequenceBinding,
    ): Promise<GenerationSequenceHostObservation> {
      const submission = claim(projection, binding);
      const options: AppModeStartOptions = {
        ...(binding.route === "replace"
          ? { replaceExisting: true }
          : binding.route === "existing"
            ? { useExisting: true }
            : {}),
        expectedIdentity: submission.expectedIdentity,
        onQueueSubmitted: submission.onQueueSubmitted,
      };
      try {
        const result = await start(binding.inputs, options);
        return submission.complete(result);
      } catch (error) {
        submission.fail();
        throw error;
      }
    },
  });
}
