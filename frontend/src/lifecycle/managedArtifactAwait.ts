// Managed output wait: the backend has recorded the exact success terminal but no SaveVideo
// artifact has closed the run yet. This module owns that waiting state and its exit together
//, so no path can mark a run as waiting without also scheduling the way out.

import { ComfyPromptHistoryError } from "../host/comfyPromptHistory";
import type { SaveVideoArtifactEvent } from "../host/sidebarHost";
import type { ActiveManagedRun, ShellRuntime } from "./shellSession";

/** Delays before each `/history` read while the host has not yet stored the finished prompt. */
const ARTIFACT_HISTORY_DELAYS_MS = [
  250, 500, 1_000, 2_000, 4_000, 8_000,
] as const;

export function createManagedArtifactAwait(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;
  const recordedSuccessWithoutArtifact = new WeakSet<ActiveManagedRun>();
  const historyRecoveries = new WeakSet<ActiveManagedRun>();

  function awaitingRecordedArtifact(managed: ActiveManagedRun): boolean {
    return (
      session.activeManagedRun === managed &&
      !managed.completed &&
      managed.artifactRecovery === undefined &&
      recordedSuccessWithoutArtifact.has(managed)
    );
  }

  /**
   * Record that the backend holds the exact success terminal without an artifact.
   *
   * CRITICAL: this is the only way to enter the wait, and it always schedules the exit. Before
   *  `verification_pending` could only be left by a matching SaveVideo event. ComfyUI never
   * resends `executed`, so an undecodable or lost event left the run in `verifying_output` forever
   * with no error and no recovery. One bounded `/history` read now either recovers exactly one
   * SaveVideo artifact, which takes the ordinary verified close, or fails the run as
   * `artifact_verification_failed`. Do not split the mark from the scheduling, remove the final
   * failure, or make it conditional on a host capability: a waiting state without an exit is the
   * defect. A late websocket artifact still wins, because every step re-checks that the run is
   * still waiting and the failure is queued behind any close already in `managed.transition`.
   */
  function markRecordedSuccessWithoutArtifact(managed: ActiveManagedRun): void {
    recordedSuccessWithoutArtifact.add(managed);
    const promptId = managed.modelPromptId;
    if (promptId === undefined || historyRecoveries.has(managed)) return;
    historyRecoveries.add(managed);
    void recoverArtifactFromHistory(managed, promptId);
  }

  async function recoverArtifactFromHistory(
    managed: ActiveManagedRun,
    promptId: string,
  ): Promise<void> {
    let artifacts: readonly SaveVideoArtifactEvent[] | undefined;
    for (const delay of ARTIFACT_HISTORY_DELAYS_MS) {
      await new Promise<void>((resolve) => setTimeout(resolve, delay));
      if (!awaitingRecordedArtifact(managed)) return;
      try {
        // ComfyUI stores history just after it sends `execution_success`; retry while absent.
        const read =
          await deps.comfyPromptHistoryClient.readSavedVideos(promptId);
        if (read.disposition === "not_yet_observed") continue;
        artifacts = read.promptId === promptId ? read.artifacts : undefined;
        break;
      } catch (error) {
        // A foreign, malformed or refused record is final; only an unreachable read is retried.
        if (
          !(error instanceof ComfyPromptHistoryError) ||
          error.code !== "history_unavailable"
        )
          break;
      }
    }
    managed.transition = managed.transition
      .then(() => {
        if (!awaitingRecordedArtifact(managed)) return;
        if (artifacts !== undefined && artifacts.length === 1) {
          actions.recordManagedStage("artifact_history_recovered", managed.run);
          actions.enqueueManagedHostEvent(managed, {
            kind: "artifact",
            value: artifacts[0],
          });
          return;
        }
        actions.recordManagedStage("artifact_history_unresolved", managed.run);
        actions.failManagedRun(
          managed,
          "artifact_verification_failed",
          "use_native",
        );
      })
      .catch(() => {
        actions.failManagedRun(
          managed,
          "artifact_verification_failed",
          "use_native",
        );
      });
  }

  return {
    markRecordedSuccessWithoutArtifact,
    clearRecordedSuccessWithoutArtifact: (managed: ActiveManagedRun) =>
      recordedSuccessWithoutArtifact.delete(managed),
    hasRecordedSuccessWithoutArtifact: (managed: ActiveManagedRun) =>
      recordedSuccessWithoutArtifact.has(managed),
  };
}
