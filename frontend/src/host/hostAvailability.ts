/**
 * Host socket availability, read from the ComfyUI `api` seam.
 *
 * The host owns the websocket to its own backend. When it closes, the extension has lost its
 * event channel but has neither failed nor lost authority over a run that is still executing on
 * the host. This module classifies the three availability events into a phase so the shell can
 * present the interruption as a host condition instead of an App Mode error, and so a reconnect
 * drives exactly one reconciliation.
 */

import type {
  GenerationJobState,
  GenerationSequenceProjection,
} from "../contracts/generationSequenceCodec";

/**
 * The only availability events this extension subscribes to. `status` doubles as the live queue
 * channel, so its payload — not its name — decides whether the socket is open.
 */
export const HOST_AVAILABILITY_EVENT_NAMES = Object.freeze([
  "status",
  "reconnecting",
  "reconnected",
] as const);

export type HostAvailabilityEventName =
  (typeof HOST_AVAILABILITY_EVENT_NAMES)[number];

export type HostAvailabilityPhase = "available" | "reconnecting" | "lost";

export type HostAvailabilityTransition = Readonly<{
  phase: HostAvailabilityPhase;
  previous: HostAvailabilityPhase;
}>;

/**
 * Classify one host event. An event this module does not own returns `undefined` so a caller can
 * subscribe defensively without inventing a phase for an unrelated channel.
 */
export function classifyHostAvailability(
  eventName: string,
  detail: unknown,
): HostAvailabilityPhase | undefined {
  if (eventName === "reconnecting") return "reconnecting";
  if (eventName === "reconnected") return "available";
  if (eventName !== "status") return undefined;
  // CRITICAL: the host publishes `status` with a null payload exactly when the socket closes and
  // with an object payload (`sid` on connect, `exec_info` while running) while it is open. Reading
  // the event name alone would turn every ordinary queue update into a lost host.
  return detail === null || detail === undefined ? "lost" : "available";
}

/**
 * Track the phase and report only real transitions. The host repeats `reconnected` on every retry
 * of the same recovery, and a repeated transition would start a second reconciliation for one run.
 */
export function createHostAvailabilityTracker(
  initialPhase: HostAvailabilityPhase = "available",
): Readonly<{
  observe(
    eventName: string,
    detail: unknown,
  ): HostAvailabilityTransition | undefined;
  phase(): HostAvailabilityPhase;
}> {
  let current: HostAvailabilityPhase = initialPhase;
  return Object.freeze({
    observe(eventName, detail) {
      const next = classifyHostAvailability(eventName, detail);
      if (next === undefined || next === current) return undefined;
      const previous = current;
      current = next;
      return Object.freeze({ phase: next, previous });
    },
    phase: () => current,
  });
}

/**
 * What a reconnect may conclude about a managed run from the coordinator's own record of it.
 *
 * `continue` is the only outcome that leaves the run owned by the host; every other outcome is
 * terminal and settles the shell.
 */
export type ReconciledRunOutcome =
  | Readonly<{ kind: "continue"; verifying: boolean }>
  | Readonly<{ kind: "succeeded" }>
  | Readonly<{ kind: "failed"; interrupted: boolean }>
  | Readonly<{ kind: "cancelled" }>
  | Readonly<{ kind: "ownership_unknown" }>;

function reconciledOutcomeForJobState(
  jobState: GenerationJobState,
): ReconciledRunOutcome {
  switch (jobState) {
    case "planned":
    case "projected":
    case "submitted":
    case "running":
      return Object.freeze({ kind: "continue", verifying: false } as const);
    case "output_verification_failed":
      return Object.freeze({ kind: "continue", verifying: true } as const);
    case "succeeded":
      return Object.freeze({ kind: "succeeded" });
    case "cancelled":
      return Object.freeze({ kind: "cancelled" });
    case "unknown_ownership":
      return Object.freeze({ kind: "ownership_unknown" });
    case "failed":
      return Object.freeze({ kind: "failed", interrupted: false } as const);
    case "timed_out":
      return Object.freeze({ kind: "failed", interrupted: true } as const);
    default: {
      // CRITICAL: exhaustive on purpose. A default that guessed would have to guess terminally,
      // and terminalizing an unrecognised state would abandon a generation the host may still be
      // running. A new GenerationJobState must therefore fail to compile here, not fall through.
      const unreachable: never = jobState;
      return unreachable;
    }
  }
}

/**
 * Read a reconnect's conclusion out of the sequence projection.
 *
 * CRITICAL: recorded job state decides terminal outcomes before the caller considers the
 * nonterminal `verification_pending` read hint. Most reads return `current`; treating that as a
 * running state would leave completed runs generating, while letting a waiting hint override a
 * terminal projection would retain work that has already finished.
 *
 * The projection knows only what this browser recorded before it went blind, because nothing in
 * this repository observes the host's queue independently. A run whose terminal was never recorded
 * therefore reconciles as `continue` and stays owned by the host, which is the safe reading: the
 * generation may still be executing, and guessing otherwise would abandon it.
 */
export function classifyReconciledRun(
  sequence: GenerationSequenceProjection,
  queuePromptId?: string,
): ReconciledRunOutcome {
  const rows = sequence.progress;
  const owned =
    queuePromptId === undefined
      ? undefined
      : rows.find((row) => row.queue_prompt_id === queuePromptId);
  const row = owned ?? (rows.length === 1 ? rows[0] : undefined);
  // An ambiguous projection is not evidence of a terminal. Leave the run owned by the host.
  if (row === undefined)
    return Object.freeze({ kind: "continue", verifying: false } as const);
  // CRITICAL: the recorded job state is the whole authority; `cancellation_requested` is a request,
  // not an outcome, and must never terminalize a job on its own. A cancel that reached a submitted
  // or running job is recorded as `unknown_ownership` and refused here; one that reached a job the
  // host never saw is recorded as `cancelled`. Reading the flag instead would end a generation the
  // host is still executing the moment a sequence carries more than one segment.
  return reconciledOutcomeForJobState(row.state);
}
