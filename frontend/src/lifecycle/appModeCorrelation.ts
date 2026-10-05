// App Mode correlation: projection/terminal/artifact deferral and quarantine, host reconnect
// reconciliation, managed host events and execution terminalization. Consumes prompt and
// execution identities; never queues (M23-28 split of entry.tsx).

import {
  AppModeError,
  graphFingerprint as appModeFingerprint,
  type ManagedAppModePreparation,
} from "../host/appMode";
import {
  H3_SHELL_MANIFEST,
  inspectH3GraphAdmission,
} from "../host/graphAdapter";
import {
  classifyReconciledRun,
  type HostAvailabilityTransition,
} from "../host/hostAvailability";
import {
  observeOwnedGraph,
  type OwnedGraphReference,
} from "../host/ownedGraphIdentity";
import {
  type SequenceCoordinatorAction,
  SequenceCoordinatorClientError,
  type SequenceCoordinatorResult,
} from "../host/sequenceCoordinator";
import type {
  ExecutionTerminalEvent,
  SaveVideoArtifactEvent,
} from "../host/sidebarHost";
import { diffGraphSurroundings } from "../host/surroundingsDiff";
import {
  type ManagedDiagnosticStage,
  managedJournal,
} from "../state/managedJournal";
import {
  reduceShellState,
  type RestorableShellState,
  retainedProjectionOwnsPrompt,
  type ShellErrorCode,
} from "../state/shellState";
import { probeWorkflowStore } from "../host/canvasOwnedWrite";
import { serializeGraph } from "../host/hostSeams";
import {
  type ShellRuntime,
  type ActiveManagedRun,
  type ManagedBootstrapProjection,
  type ManagedBootstrapWaiter,
  type ManagedCanvasIdentity,
} from "./shellSession";
import {
  applyManagedCoordinatorAuthority as applyProjectMemberAuthority,
  managedBootstrapTerminalError,
  managedMemberRetry,
  refreshManagedMemberProject,
} from "./managedProjectMember";

export function createAppModeCorrelation(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;
  const recordedSuccessWithoutArtifact = new WeakSet<ActiveManagedRun>();

  function recordManagedStage(
    stage: ManagedDiagnosticStage,
    appRun?: number,
  ): void {
    managedJournal.recordStage(
      appRun === undefined
        ? session.diagnosticRun
        : (session.diagnosticRunsByAppRun.get(appRun) ?? 0),
      stage,
    );
  }

  function serializeGraphForDiagnostics(): unknown {
    const serialized = serializeGraph(deps.app);
    return serialized.status === "ready" ? serialized.value : undefined;
  }

  function recordSurroundingsEvidence(
    run: number,
    name: "within_run" | "consecutive_start",
    beforeValue: unknown,
    afterValue: unknown,
    reference: OwnedGraphReference,
  ): void {
    if (beforeValue === undefined || afterValue === undefined) return;
    try {
      const ownedProjectionEqual =
        observeOwnedGraph(beforeValue, reference).fingerprint ===
        observeOwnedGraph(afterValue, reference).fingerprint;
      managedJournal.recordSurroundings(
        run,
        name,
        diffGraphSurroundings({
          beforeValue,
          afterValue,
          reference: {
            ownedNodeIds: reference.nodeIds,
            ownedLinkIds: reference.linkIds,
            anchorNodeId: reference.anchorNodeId,
            authoredWidgetNodeIds: reference.authoredWidgetNodeIds,
            ownedProjectionEqual,
          },
        }),
      );
    } catch {
      // CRITICAL: graph diagnostics are evidence only and never fail a managed run.
    }
  }

  async function sendManagedCoordinator(
    run: number,
    requestId: string,
    action: SequenceCoordinatorAction,
    payload: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<SequenceCoordinatorResult> {
    try {
      return await deps.sequenceCoordinator.send(
        requestId,
        action,
        payload,
        signal,
      );
    } catch (error) {
      if (error instanceof SequenceCoordinatorClientError)
        managedJournal.recordCoordinatorError(
          session.diagnosticRunsByAppRun.get(run) ?? 0,
          error.category,
        );
      throw error;
    }
  }

  /**
   * The state a host interruption is holding, or the visible state when none is.
   * Guards that ask "is a run in flight" must read through the wrapper, or a dropped socket would
   * silently withdraw cancellation from a run the user can still cancel.
   */
  function interruptedShellState(): RestorableShellState {
    return session.state.status === "host_unavailable"
      ? session.state.prior
      : session.state;
  }

  function enterHostUnavailable(phase: "lost" | "reconnecting"): void {
    actions.sendAppModeLifecycle({
      type: phase === "lost" ? "HOST_LOST" : "HOST_RECONNECTING",
    });
    session.state = reduceShellState(session.state, {
      type: "host_unavailable",
      phase,
    });
    actions.renderCurrent();
  }

  /**
   * Re-read the managed run from the coordinator after the socket returns.
   *
   * The recorded job projection decides terminal outcomes. Only a continuing run may use the
   * `verification_pending` read hint to retain waiting for output. A reconnect never queues: it
   * continues, terminalizes from recorded state, or refuses when the run is no longer known.
   *
   * What this deliberately does not claim: the coordinator knows only what this browser recorded
   * before it went blind. If the host finished a prompt during the interruption and no terminal was
   * ever recorded, the run reconciles as still running and stays owned by the host, because ComfyUI
   * replays neither `executed` nor `execution_success` on reconnect and nothing here observes its
   * queue independently. Recovering that case needs a host history probe, which this item does not
   * add.
   */
  async function reconcileAfterHostReconnect(): Promise<void> {
    if (session.hostReconcileInFlight) return;
    const managed = session.activeManagedRun;
    if (
      managed === undefined ||
      managed.completed ||
      !actions.isCurrentAppModeRun(managed.run)
    ) {
      session.state = reduceShellState(session.state, {
        type: "host_restored",
      });
      actions.renderCurrent();
      return;
    }
    session.hostReconcileInFlight = true;
    recordManagedStage("reconcile_started", managed.run);
    try {
      const current = await sendManagedCoordinator(
        managed.run,
        nextManagedRequestId("reconnect", managed.run),
        "read_managed_run",
        { run_handle: managed.authority.runHandle },
      );
      if (session.activeManagedRun !== managed || managed.completed) {
        session.state = reduceShellState(session.state, {
          type: "host_restored",
        });
        actions.renderCurrent();
        return;
      }
      // Leave the interruption before applying backend truth so the disposition writes the visible
      // state directly instead of being buried under a wrapper the reducer would then protect.
      session.state = reduceShellState(session.state, {
        type: "host_restored",
      });
      const outcome = classifyReconciledRun(
        current.sequence,
        managed.modelPromptId,
      );
      if (outcome.kind === "cancelled") {
        applyManagedCoordinatorAuthority(managed, current);
        finishManagedCancellation(managed);
        recordManagedStage("reconcile_terminalized", managed.run);
        actions.renderCurrent();
        return;
      }
      if (outcome.kind === "ownership_unknown") {
        applyManagedCoordinatorAuthority(managed, current);
        recordManagedStage("reconcile_refused", managed.run);
        failManagedRun(managed, "run_authority_mismatch", "inspect");
        return;
      }
      applyManagedCoordinatorAuthority(managed, current);
      const verificationPending =
        outcome.kind === "continue" &&
        current.disposition === "verification_pending";
      if (verificationPending) {
        // CRITICAL: a reconnect may be the first response that proves the backend recorded the
        // exact success terminal. Keep that admission and its prompt correlation locally, or a
        // later SaveVideo event would wait forever for a terminal the host does not replay.
        recordedSuccessWithoutArtifact.add(managed);
        if (managed.modelPromptId !== undefined)
          managed.pendingTerminals.set(managed.modelPromptId, "success");
      }
      if (outcome.kind === "succeeded") {
        recordManagedStage("reconcile_terminalized", managed.run);
        finishManagedSuccess(managed);
        return;
      }
      if (outcome.kind === "failed") {
        recordManagedStage("reconcile_terminalized", managed.run);
        failManagedRun(
          managed,
          outcome.interrupted ? "execution_interrupted" : "execution_failed",
          "retry",
        );
        return;
      }
      session.state = {
        status: "working",
        phase:
          outcome.verifying || verificationPending
            ? "verifying_output"
            : "generating",
        transactionId: managed.run,
        existingGraph: managed.prepared.observation.route === "existing",
        graphFingerprint: managed.prepared.observation.graph_fingerprint,
      };
      recordManagedStage("reconcile_continued", managed.run);
      actions.renderCurrent();
    } catch (error) {
      const stillOwned =
        session.activeManagedRun === managed && !managed.completed;
      session.state = reduceShellState(session.state, {
        type: "host_restored",
      });
      if (!stillOwned) {
        actions.renderCurrent();
        return;
      }
      // CRITICAL: only a backend that no longer knows this run may terminalize it. Any other read
      // failure leaves the run owned by the host and retryable; guessing here would abandon a
      // generation that is still executing.
      const authorityGone =
        error instanceof SequenceCoordinatorClientError &&
        (error.category === "run_authority_mismatch" ||
          error.status === 404 ||
          error.status === 410);
      if (authorityGone) {
        recordManagedStage("reconcile_refused", managed.run);
        failManagedRun(managed, "run_authority_mismatch", "inspect");
        return;
      }
      recordManagedStage("reconcile_unavailable", managed.run);
      actions.renderCurrent();
    } finally {
      session.hostReconcileInFlight = false;
    }
  }

  function observeHostAvailability(
    transition: HostAvailabilityTransition,
  ): void {
    if (transition.phase === "lost") {
      recordManagedStage("host_lost", session.activeManagedRun?.run);
      enterHostUnavailable("lost");
      return;
    }
    if (transition.phase === "reconnecting") {
      recordManagedStage("host_reconnecting", session.activeManagedRun?.run);
      enterHostUnavailable("reconnecting");
      return;
    }
    recordManagedStage("host_reconnected", session.activeManagedRun?.run);
    actions.sendAppModeLifecycle({ type: "HOST_RESTORED" });
    void reconcileAfterHostReconnect();
  }

  function rememberCompletedManagedPrompt(promptId: string): void {
    session.completedManagedPromptIds.add(promptId);
    while (session.completedManagedPromptIds.size > 32) {
      const oldest = session.completedManagedPromptIds.values().next().value;
      if (typeof oldest !== "string") break;
      session.completedManagedPromptIds.delete(oldest);
    }
  }

  function nextManagedRequestId(action: string, run: number): string {
    session.managedRequestSequence += 1;
    return `managed.${actions.browserRequestSessionToken}.${action}.${run}.${session.managedRequestSequence}`;
  }

  function closeManagedBootstrapWaiter(waiter: ManagedBootstrapWaiter): void {
    if (session.managedBootstrapWaiter === waiter)
      session.managedBootstrapWaiter = undefined;
    waiter.close();
  }

  function consumeManagedBootstrapProjection(
    value: ManagedBootstrapProjection,
  ): boolean {
    const waiter = session.managedBootstrapWaiter;
    if (
      waiter === undefined ||
      !actions.isCurrentAppModeRun(waiter.run) ||
      value.projection.correlation.execution_node_id !== waiter.executionNodeId
    )
      return false;
    const promptId = value.projection.correlation.prompt_id;
    if (waiter.promptId === undefined) {
      recordManagedStage("bootstrap_projection_buffered", waiter.run);
      waiter.buffered.set(promptId, value);
      while (waiter.buffered.size > 8) {
        const oldest = waiter.buffered.keys().next().value;
        if (typeof oldest !== "string") break;
        waiter.buffered.delete(oldest);
      }
      return true;
    }
    if (promptId !== waiter.promptId) return true;
    recordManagedStage("bootstrap_projection_resolved", waiter.run);
    closeManagedBootstrapWaiter(waiter);
    waiter.resolve(value);
    return true;
  }

  function consumeManagedProjection(
    value: ManagedBootstrapProjection,
  ): boolean {
    if (consumeManagedBootstrapProjection(value)) return true;
    const managed = session.activeManagedRun;
    if (
      managed === undefined ||
      !actions.isCurrentAppModeRun(managed.run) ||
      value.projection.correlation.execution_node_id !==
        managed.prepared.productShellNodeId
    )
      return false;
    const promptId = value.projection.correlation.prompt_id;
    if (managed.modelPromptId === undefined) {
      if (
        managed.acceptingModelPromptId !== undefined &&
        promptId !== managed.acceptingModelPromptId
      )
        return true;
      managed.pendingProjectionPromptIds.add(promptId);
      while (managed.pendingProjectionPromptIds.size > 8) {
        const oldest = managed.pendingProjectionPromptIds.values().next().value;
        if (typeof oldest !== "string") break;
        managed.pendingProjectionPromptIds.delete(oldest);
      }
    } else if (promptId === managed.modelPromptId) {
      session.ignoredProjectionPromptIds.add(promptId);
    }
    // A host may replay an executed payload for the one accepted model prompt.
    // The first exact ProductShell projection remains the Context owner; never
    // create a second random browser workspace from a replayed output.
    return true;
  }

  function consumeManagedBootstrapTerminal(
    event: ExecutionTerminalEvent,
  ): boolean {
    const waiter = session.managedBootstrapWaiter;
    if (waiter === undefined || !actions.isCurrentAppModeRun(waiter.run))
      return false;
    recordManagedStage("bootstrap_terminal_seen", waiter.run);
    if (waiter.promptId === undefined) {
      waiter.terminal.set(event.promptId, event.kind);
      while (waiter.terminal.size > 8) {
        const oldest = waiter.terminal.keys().next().value;
        if (typeof oldest !== "string") break;
        waiter.terminal.delete(oldest);
      }
      return true;
    }
    if (event.promptId !== waiter.promptId) return false;
    if (event.kind !== "success") {
      closeManagedBootstrapWaiter(waiter);
      waiter.reject(managedBootstrapTerminalError(event.kind));
    } else {
      // CRITICAL: the prompt may finish after bind but before its ProductShell projection.
      // Preserve this exact success because closing the waiter will otherwise erase it.
      rememberDeferredAppModeTerminal(waiter.run, event);
    }
    return true;
  }

  function applyManagedCoordinatorAuthority(
    managed: ActiveManagedRun,
    result: SequenceCoordinatorResult,
  ): void {
    applyProjectMemberAuthority(ctx, managed, result);
  }

  function clearManagedExecution(run: number): void {
    clearActiveAppModeExecution(run);
    clearDeferredAppModeProjections(run);
    clearDeferredAppModeTerminals(run);
    clearDeferredManagedArtifacts(run);
  }

  function markManagedRunCompleted(managed: ActiveManagedRun): void {
    managed.completed = true;
    const promptId = managed.modelPromptId ?? managed.acceptingModelPromptId;
    if (promptId === undefined) return;
    session.ignoredProjectionPromptIds.add(promptId);
    rememberCompletedManagedPrompt(promptId);
  }

  function finishManagedSuccess(managed: ActiveManagedRun): void {
    if (session.activeManagedRun !== managed || managed.completed) return;
    markManagedRunCompleted(managed);
    const { projection, workspace } = managed.bootstrap;
    session.pendingAppMode = undefined;
    session.retryableManagedProductionMember = undefined;
    clearManagedExecution(managed.run);
    session.state = reduceShellState(session.state, {
      type: "projection",
      anchorExecutionId: projection.correlation.execution_node_id,
      projection,
      transactionId: managed.run,
      graphFingerprint: managed.prepared.observation.graph_fingerprint,
      backendPromptFingerprint: workspace.prompt_fingerprint,
    });
    rememberAcceptedProjectionPromptId(projection.correlation.prompt_id);
    session.graphChangedDuringRun = false;
    session.executedGraphRefresh = undefined;
    session.transactionTransparency = managed.bootstrap.transactionTransparency;
    deps.pageRegistry.register({ id: "production" });
    session.acceptedManagedIdentity = managedCanvasIdentityFromPreparation(
      managed.prepared,
    );
    session.activeManagedRun = undefined;
    actions.completeAppModeLifecycle(managed.run);
    actions.renderCurrent();
  }

  function finishManagedCancellation(managed: ActiveManagedRun): void {
    session.retryableManagedProductionMember = managedMemberRetry(managed);
    managed.completed = true;
    session.activeManagedRun = undefined;
    clearManagedExecution(managed.run);
    session.state = { status: "interactive", reason: "cancelled" };
    if (actions.isCurrentAppModeRun(managed.run))
      deps.appModeLifecycle.invalidate();
  }

  function failManagedRun(
    managed: ActiveManagedRun,
    code: ShellErrorCode,
    recovery: "retry" | "inspect" | "use_native",
  ): void {
    if (session.activeManagedRun !== managed || managed.completed) return;
    session.retryableManagedProductionMember = managedMemberRetry(managed);
    markManagedRunCompleted(managed);
    session.pendingAppMode = undefined;
    clearManagedExecution(managed.run);
    session.state = {
      status: "error",
      code,
      severity: "error",
      source: "managed_app_mode",
      message: code,
      recovery,
      existingGraph: true,
      transactionId: managed.run,
    };
    session.activeManagedRun = undefined;
    actions.failAppModeLifecycle(managed.run, code, recovery);
    actions.renderCurrent();
  }

  async function retainManagedArtifactFailure(
    managed: ActiveManagedRun,
    event: SaveVideoArtifactEvent,
    failure: unknown,
  ): Promise<void> {
    if (session.activeManagedRun !== managed || managed.completed) return;
    const coordinatorFailure =
      failure instanceof SequenceCoordinatorClientError ? failure : undefined;
    let reconciled = false;
    try {
      const current = await sendManagedCoordinator(
        managed.run,
        nextManagedRequestId("read", managed.run),
        "read_managed_run",
        { run_handle: managed.authority.runHandle },
      );
      if (session.activeManagedRun !== managed || managed.completed) return;
      applyManagedCoordinatorAuthority(managed, current);
      reconciled = true;
    } catch {
      // The retained local projection is still safer than discarding the accepted workspace.
      // Retry remains disabled because the backend state could not be read back exactly.
    }
    if (session.activeManagedRun !== managed || managed.completed) return;
    const canRetry =
      reconciled &&
      coordinatorFailure?.sameRunAuthority === true &&
      coordinatorFailure.retryDisposition === "retry_output_verification";
    managed.artifactRecovery = canRetry ? Object.freeze({ event }) : undefined;
    managed.artifactRetryInFlight = false;
    session.pendingAppMode = undefined;
    clearActiveAppModeExecution(managed.run);
    clearDeferredAppModeProjections(managed.run);
    clearDeferredAppModeTerminals(managed.run);
    clearDeferredManagedArtifacts(managed.run);
    const code: ShellErrorCode = reconciled
      ? (coordinatorFailure?.category ?? "internal_failure")
      : "run_authority_mismatch";
    session.state = {
      status: "error",
      code,
      severity: "error",
      source: "managed_app_mode",
      message: code,
      recovery: canRetry ? "retry_output_verification" : "use_native",
      existingGraph: true,
      transactionId: managed.run,
    };
    actions.failAppModeLifecycle(
      managed.run,
      code,
      canRetry ? "retry_output_verification" : "use_native",
    );
    actions.renderCurrent();
  }

  async function settleManagedDisposition(
    managed: ActiveManagedRun,
    result: SequenceCoordinatorResult,
  ): Promise<void> {
    applyManagedCoordinatorAuthority(managed, result);
    if (
      managed.projectMember !== undefined &&
      ["succeeded", "failed", "interrupted", "cancelled"].includes(
        result.disposition,
      )
    )
      await refreshManagedMemberProject(
        ctx,
        managed,
        `production.read_member.${actions.browserRequestSessionToken}.${managed.run}`,
        result.disposition === "succeeded",
      );
    if (result.disposition === "succeeded") {
      finishManagedSuccess(managed);
      return;
    }
    if (result.disposition === "failed") {
      failManagedRun(managed, "execution_failed", "retry");
      return;
    }
    if (result.disposition === "interrupted") {
      failManagedRun(managed, "execution_interrupted", "retry");
      return;
    }
    if (result.disposition === "cancelled") {
      finishManagedCancellation(managed);
      actions.renderCurrent();
      return;
    }
    if (result.disposition === "output_verification_failed") return;
    session.state = {
      status: "working",
      phase:
        result.disposition === "verification_pending"
          ? "verifying_output"
          : "generating",
      transactionId: managed.run,
      existingGraph: managed.prepared.observation.route === "existing",
      graphFingerprint: managed.prepared.observation.graph_fingerprint,
    };
    actions.renderCurrent();
  }

  async function reconcileManagedTerminalFailure(
    managed: ActiveManagedRun,
  ): Promise<void> {
    if (session.activeManagedRun !== managed || managed.completed) return;
    try {
      const current = await sendManagedCoordinator(
        managed.run,
        nextManagedRequestId("close_read", managed.run),
        "read_managed_run",
        { run_handle: managed.authority.runHandle },
      );
      if (session.activeManagedRun !== managed || managed.completed) return;
      const outcome = classifyReconciledRun(
        current.sequence,
        managed.modelPromptId,
      );
      applyManagedCoordinatorAuthority(managed, current);
      if (
        outcome.kind === "continue" &&
        current.disposition === "verification_pending"
      ) {
        recordedSuccessWithoutArtifact.add(managed);
        if (managed.modelPromptId !== undefined)
          managed.pendingTerminals.set(managed.modelPromptId, "success");
        await settleManagedDisposition(managed, current);
        return;
      }
      if (outcome.kind === "succeeded") {
        finishManagedSuccess(managed);
        return;
      }
      if (outcome.kind === "failed") {
        failManagedRun(
          managed,
          outcome.interrupted ? "execution_interrupted" : "execution_failed",
          "retry",
        );
        return;
      }
      if (outcome.kind === "cancelled") {
        finishManagedCancellation(managed);
        actions.renderCurrent();
        return;
      }
      if (outcome.kind === "ownership_unknown") {
        failManagedRun(managed, "run_authority_mismatch", "inspect");
        return;
      }
      if (outcome.verifying) {
        session.state = {
          status: "working",
          phase: "verifying_output",
          transactionId: managed.run,
          existingGraph: managed.prepared.observation.route === "existing",
          graphFingerprint: managed.prepared.observation.graph_fingerprint,
        };
        actions.renderCurrent();
        return;
      }
    } catch {
      // Fall through to the fixed ambiguous-ownership refusal below.
    }
    if (session.activeManagedRun === managed && !managed.completed)
      failManagedRun(managed, "ambiguous_host_ownership", "use_native");
  }

  function enqueueManagedHostEvent(
    managed: ActiveManagedRun,
    event:
      | Readonly<{ kind: "terminal"; value: ExecutionTerminalEvent }>
      | Readonly<{ kind: "artifact"; value: SaveVideoArtifactEvent }>,
  ): void {
    let attemptedArtifact: SaveVideoArtifactEvent | undefined;
    managed.transition = managed.transition
      .then(async () => {
        if (
          session.activeManagedRun !== managed ||
          managed.completed ||
          managed.modelPromptId === undefined
        )
          return;
        if (event.kind === "terminal")
          managed.pendingTerminals.set(event.value.promptId, event.value.kind);
        else managed.pendingArtifacts.set(event.value.promptId, event.value);
        if (managed.artifactRecovery !== undefined) return;
        // IMPORTANT: once the backend has recorded a success terminal, duplicate or conflicting
        // terminal events cannot replace it. Only a matching late artifact may advance this run.
        if (
          event.kind === "terminal" &&
          recordedSuccessWithoutArtifact.has(managed)
        ) {
          managed.pendingTerminals.set(event.value.promptId, "success");
          return;
        }
        const terminalKind = managed.pendingTerminals.get(
          managed.modelPromptId,
        );
        if (terminalKind === undefined) return;
        const artifact = managed.pendingArtifacts.get(managed.modelPromptId);
        // CRITICAL: the aggregate takes an artifact for a success close and refuses one on any
        // other terminal with `invalid_managed_artifact` (400). A user-built graph may save an
        // output and then fail or be interrupted in a later node, so a buffered artifact and a
        // non-success terminal do coexist; forwarding it would misreport the run's own failure as
        // an artifact refusal, downgrade recovery from retry to use_native, and leave the aggregate
        // never terminalized because the close never lands. The terminal kind, not the presence of
        // a buffered artifact, decides what this close carries.
        const closingArtifact =
          terminalKind === "success" ? artifact : undefined;
        attemptedArtifact = closingArtifact;
        const current = managed.authority;
        const result = await sendManagedCoordinator(
          managed.run,
          nextManagedRequestId("close", managed.run),
          "close_managed_run",
          {
            run_handle: current.runHandle,
            expected_state_fingerprint: current.sequence.state_fingerprint,
            queue_prompt_id: managed.modelPromptId,
            kind: terminalKind,
            artifact:
              closingArtifact === undefined
                ? null
                : {
                    output_node_id: closingArtifact.outputNodeId,
                    locator: closingArtifact.locator,
                  },
          },
        );
        const pendingVerifiedOutput =
          terminalKind === "success" &&
          closingArtifact === undefined &&
          result.disposition === "verification_pending";
        if (pendingVerifiedOutput) recordedSuccessWithoutArtifact.add(managed);
        else {
          recordedSuccessWithoutArtifact.delete(managed);
          managed.pendingTerminals.delete(managed.modelPromptId);
        }
        managed.pendingArtifacts.delete(managed.modelPromptId);
        await settleManagedDisposition(managed, result);
      })
      .catch(async (error: unknown) => {
        if (attemptedArtifact !== undefined) {
          await retainManagedArtifactFailure(managed, attemptedArtifact, error);
          return;
        }
        if (event.kind === "terminal" && event.value.kind === "success") {
          await reconcileManagedTerminalFailure(managed);
          return;
        }
        failManagedRun(managed, "ambiguous_host_ownership", "use_native");
      });
  }

  function consumeManagedTerminal(event: ExecutionTerminalEvent): boolean {
    if (session.completedManagedPromptIds.has(event.promptId)) return true;
    if (consumeManagedBootstrapTerminal(event)) return true;
    const managed = session.activeManagedRun;
    if (managed === undefined || !actions.isCurrentAppModeRun(managed.run))
      return false;
    if (managed.modelPromptId === undefined) {
      if (
        managed.acceptingModelPromptId !== undefined &&
        event.promptId !== managed.acceptingModelPromptId
      )
        return false;
      managed.pendingTerminals.set(event.promptId, event.kind);
      while (managed.pendingTerminals.size > 8) {
        const oldest = managed.pendingTerminals.keys().next().value;
        if (typeof oldest !== "string") break;
        managed.pendingTerminals.delete(oldest);
      }
      return true;
    }
    if (event.promptId !== managed.modelPromptId) return false;
    enqueueManagedHostEvent(managed, { kind: "terminal", value: event });
    return true;
  }

  function consumeManagedArtifact(event: SaveVideoArtifactEvent): boolean {
    if (session.completedManagedPromptIds.has(event.promptId)) return true;
    const managed = session.activeManagedRun;
    if (managed === undefined) {
      const admission = session.pendingManagedArtifactAdmission;
      if (
        admission === undefined ||
        !actions.isCurrentAppModeRun(admission.run) ||
        session.pendingAppMode?.run !== admission.run
      )
        return false;
      // IMPORTANT: SaveVideo may emit after native queue acceptance but before bootstrap and
      // Production ownership finish. Retain bounded prompt evidence; claim only the exact ID later.
      session.deferredManagedArtifacts.set(event.promptId, {
        run: admission.run,
        event,
      });
      while (session.deferredManagedArtifacts.size > 8) {
        const oldest = session.deferredManagedArtifacts.keys().next().value;
        if (typeof oldest !== "string") break;
        session.deferredManagedArtifacts.delete(oldest);
      }
      return true;
    }
    if (!actions.isCurrentAppModeRun(managed.run)) return false;
    if (managed.modelPromptId === undefined) {
      if (
        managed.acceptingModelPromptId !== undefined &&
        event.promptId !== managed.acceptingModelPromptId
      )
        return false;
      // IMPORTANT: queue acknowledgement may trail host events; retain exact prompt ownership so
      // submission acknowledgement cannot race terminal/artifact ownership and a foreign event
      // cannot overwrite the managed SaveVideo receipt candidate.
      managed.pendingArtifacts.set(event.promptId, event);
      while (managed.pendingArtifacts.size > 8) {
        const oldest = managed.pendingArtifacts.keys().next().value;
        if (typeof oldest !== "string") break;
        managed.pendingArtifacts.delete(oldest);
      }
      return true;
    }
    if (event.promptId !== managed.modelPromptId) return false;
    enqueueManagedHostEvent(managed, { kind: "artifact", value: event });
    return true;
  }

  function clearDeferredAppModeProjections(run?: number): void {
    for (const [promptId, deferred] of session.deferredAppModeProjections) {
      if (run === undefined || deferred.run === run)
        session.deferredAppModeProjections.delete(promptId);
    }
  }

  function clearDeferredAppModeTerminals(run?: number): void {
    for (const [promptId, deferred] of session.deferredAppModeTerminals) {
      if (run === undefined || deferred.run === run)
        session.deferredAppModeTerminals.delete(promptId);
    }
  }

  function clearDeferredManagedArtifacts(run?: number): void {
    for (const [promptId, deferred] of session.deferredManagedArtifacts) {
      if (run === undefined || deferred.run === run)
        session.deferredManagedArtifacts.delete(promptId);
    }
    if (
      run === undefined ||
      session.pendingManagedArtifactAdmission?.run === run
    )
      session.pendingManagedArtifactAdmission = undefined;
  }

  function clearActiveAppModeExecution(run?: number): void {
    if (run === undefined || session.activeAppModeExecution?.run === run)
      session.activeAppModeExecution = undefined;
  }

  function rememberDeferredAppModeTerminal(
    run: number,
    event: ExecutionTerminalEvent,
  ): void {
    const priority = { success: 0, interrupted: 1, error: 2 } as const;
    const current = session.deferredAppModeTerminals.get(event.promptId);
    if (
      current === undefined ||
      current.run !== run ||
      priority[event.kind] > priority[current.kind]
    )
      session.deferredAppModeTerminals.set(event.promptId, {
        run,
        kind: event.kind,
      });
    while (session.deferredAppModeTerminals.size > 8) {
      const oldest = session.deferredAppModeTerminals.keys().next().value;
      if (typeof oldest !== "string") break;
      session.deferredAppModeTerminals.delete(oldest);
    }
  }

  function terminalizeAppModeExecution(event: ExecutionTerminalEvent): void {
    const active = session.activeAppModeExecution;
    if (
      active === undefined ||
      !actions.isCurrentAppModeRun(active.run) ||
      active.promptId !== event.promptId
    )
      return;
    const run = active.run;
    if (
      event.kind === "success" &&
      session.acceptedProjectionPromptIds.has(event.promptId) &&
      retainedProjectionOwnsPrompt(session.state, event.promptId)
    ) {
      clearActiveAppModeExecution(run);
      clearDeferredAppModeTerminals(run);
      clearDeferredAppModeProjections(run);
      clearDeferredManagedArtifacts(run);
      actions.completeAppModeLifecycle(run);
      return;
    }

    // A host terminal event ends only this exact App Mode transaction. It does
    // not claim cancellation of work already submitted to ComfyUI.
    session.ignoredProjectionPromptIds.add(event.promptId);
    session.pendingAppMode = undefined;
    clearActiveAppModeExecution(run);
    clearDeferredAppModeTerminals(run);
    clearDeferredAppModeProjections(run);
    clearDeferredManagedArtifacts(run);
    session.graphChangedDuringRun = false;
    session.executedGraphRefresh = undefined;
    const code: ShellErrorCode =
      event.kind === "error"
        ? "execution_failed"
        : event.kind === "interrupted"
          ? "execution_interrupted"
          : "projection_missing";
    session.state = {
      status: "error",
      code,
      severity: "error",
      source: "app_mode",
      message: code,
      recovery: "retry",
      existingGraph: true,
      transactionId: run,
    };
    actions.failAppModeLifecycle(run, code, "retry");
    actions.renderCurrent();
  }

  function observeAppModeExecutionTerminal(
    event: ExecutionTerminalEvent,
  ): void {
    const pending = session.pendingAppMode;
    if (
      pending !== undefined &&
      actions.isCurrentAppModeRun(pending.run) &&
      pending.result === undefined
    ) {
      rememberDeferredAppModeTerminal(pending.run, event);
      return;
    }
    terminalizeAppModeExecution(event);
  }

  function recordProjectionTrace(
    stage: string,
    detail: Record<string, string | number | boolean | undefined>,
  ): void {
    const sink = (
      globalThis as typeof globalThis & {
        __h3ProjectionTrace?: Array<Record<string, unknown>>;
      }
    ).__h3ProjectionTrace;
    if (Array.isArray(sink)) sink.push({ stage, ...detail });
  }

  function currentGraphProjectionIdentity():
    { graphFingerprint: string; executionNodeId?: string } | undefined {
    const visible = serializeGraph(deps.app);
    if (visible.status !== "ready") return undefined;
    try {
      const serialized = visible.value;
      let executionNodeId: string | undefined;
      try {
        executionNodeId = inspectH3GraphAdmission(serialized, H3_SHELL_MANIFEST)
          .anchors[0]?.executionId;
      } catch {
        // Keep the fingerprint even when a malformed graph has no anchor.
      }
      return {
        graphFingerprint: appModeFingerprint(serialized),
        executionNodeId,
      };
    } catch {
      return undefined;
    }
  }

  function managedCanvasIdentityFromPreparation(
    prepared: ManagedAppModePreparation,
  ): ManagedCanvasIdentity {
    return Object.freeze({
      workflowAuthority: prepared.managedIdentity.workflowAuthority,
      ownedReference: prepared.managedIdentity.ownedReference,
      ownedProjectionFingerprint:
        prepared.observation.owned_projection_fingerprint,
    });
  }

  function managedCanvasIdentityStatus(
    identity: ManagedCanvasIdentity,
  ): "match" | "mismatch" | "unavailable" {
    const store = probeWorkflowStore(deps.app);
    const activeWorkflow =
      store.status === "ready" ? store.value.activeWorkflow : undefined;
    if (activeWorkflow !== identity.workflowAuthority) return "mismatch";
    const visible = serializeGraph(deps.app);
    if (visible.status !== "ready") return "unavailable";
    try {
      const observation = observeOwnedGraph(
        visible.value,
        identity.ownedReference,
      );
      return observation.fingerprint === identity.ownedProjectionFingerprint
        ? "match"
        : "mismatch";
    } catch {
      return "unavailable";
    }
  }

  function currentProjectionSuppression(): NonNullable<
    typeof session.cancelledProjectionSuppression
  > {
    const managedIdentity =
      session.pendingAppMode?.managedIdentity ??
      (session.activeManagedRun === undefined
        ? session.acceptedManagedIdentity
        : managedCanvasIdentityFromPreparation(
            session.activeManagedRun.prepared,
          ));
    if (managedIdentity !== undefined)
      return Object.freeze({ kind: "managed", identity: managedIdentity });
    const graph = currentGraphProjectionIdentity();
    return Object.freeze({
      kind: "graph",
      graphFingerprint: graph?.graphFingerprint ?? "unavailable",
      executionNodeId: graph?.executionNodeId,
    });
  }

  function releaseProjectionQuarantineIfGraphChanged(): void {
    if (session.cancelledProjectionSuppression === undefined) return;
    const released =
      session.cancelledProjectionSuppression.kind === "managed"
        ? managedCanvasIdentityStatus(
            session.cancelledProjectionSuppression.identity,
          ) === "mismatch"
        : (() => {
            const current = currentGraphProjectionIdentity();
            return (
              current !== undefined &&
              current.graphFingerprint !==
                session.cancelledProjectionSuppression.graphFingerprint
            );
          })();
    if (released) {
      session.cancelledProjectionSuppression = undefined;
      session.ignoredProjectionPromptIds.clear();
      session.ignoreProjectionUntilGraphRefresh = false;
    }
  }

  function rememberAcceptedProjectionPromptId(promptId: string): void {
    session.acceptedProjectionPromptIds.add(promptId);
    while (session.acceptedProjectionPromptIds.size > 32) {
      const oldest = session.acceptedProjectionPromptIds.values().next().value;
      if (typeof oldest !== "string") break;
      session.acceptedProjectionPromptIds.delete(oldest);
    }
  }

  function isModelFreeProjectionGraph(
    serialized: unknown,
    executionNodeId: string,
  ): boolean {
    try {
      const inspection = inspectH3GraphAdmission(serialized, H3_SHELL_MANIFEST);
      return (
        inspection.status === "missing" &&
        inspection.reason === "missing_native_h3_core" &&
        inspection.anchors.some(
          (anchor) => anchor.executionId === executionNodeId,
        )
      );
    } catch {
      return false;
    }
  }

  function retryManagedArtifactVerification(): void {
    const managed = session.activeManagedRun;
    const recovery = managed?.artifactRecovery;
    if (
      managed === undefined ||
      recovery === undefined ||
      managed.completed ||
      managed.artifactRetryInFlight ||
      managed.modelPromptId === undefined ||
      recovery.event.promptId !== managed.modelPromptId
    )
      return;
    managed.artifactRetryInFlight = true;
    session.state = {
      status: "working",
      phase: "verifying_output",
      transactionId: managed.run,
      existingGraph: managed.prepared.observation.route === "existing",
      graphFingerprint: managed.prepared.observation.graph_fingerprint,
    };
    actions.renderCurrent();
    managed.transition = managed.transition
      .then(async () => {
        if (
          session.activeManagedRun !== managed ||
          managed.completed ||
          managed.modelPromptId === undefined
        )
          return;
        const current = managed.authority;
        const verified = await sendManagedCoordinator(
          managed.run,
          nextManagedRequestId("artifact_retry", managed.run),
          "close_managed_run",
          {
            run_handle: current.runHandle,
            expected_state_fingerprint: current.sequence.state_fingerprint,
            queue_prompt_id: managed.modelPromptId,
            kind: "success",
            artifact: {
              output_node_id: recovery.event.outputNodeId,
              locator: recovery.event.locator,
            },
          },
        );
        if (session.activeManagedRun !== managed || managed.completed) return;
        managed.artifactRetryInFlight = false;
        managed.artifactRecovery = undefined;
        await settleManagedDisposition(managed, verified);
        if (
          session.activeManagedRun !== managed ||
          managed.completed ||
          managed.modelPromptId === undefined
        )
          return;
        const terminalKind = managed.pendingTerminals.get(
          managed.modelPromptId,
        );
        managed.pendingTerminals.delete(managed.modelPromptId);
        managed.pendingArtifacts.delete(managed.modelPromptId);
        if (terminalKind !== undefined && terminalKind !== "success")
          throw new Error("managed artifact retry terminal changed");
      })
      .catch(async (error: unknown) => {
        managed.artifactRetryInFlight = false;
        await retainManagedArtifactFailure(managed, recovery.event, error);
      });
  }

  return {
    applyManagedCoordinatorAuthority,
    clearActiveAppModeExecution,
    clearDeferredAppModeProjections,
    clearDeferredAppModeTerminals,
    clearDeferredManagedArtifacts,
    closeManagedBootstrapWaiter,
    consumeManagedArtifact,
    consumeManagedBootstrapProjection,
    consumeManagedBootstrapTerminal,
    consumeManagedProjection,
    consumeManagedTerminal,
    currentGraphProjectionIdentity,
    currentProjectionSuppression,
    enqueueManagedHostEvent,
    enterHostUnavailable,
    failManagedRun,
    finishManagedSuccess,
    interruptedShellState,
    isModelFreeProjectionGraph,
    managedCanvasIdentityFromPreparation,
    managedCanvasIdentityStatus,
    nextManagedRequestId,
    observeAppModeExecutionTerminal,
    observeHostAvailability,
    reconcileAfterHostReconnect,
    recordManagedStage,
    recordProjectionTrace,
    recordSurroundingsEvidence,
    releaseProjectionQuarantineIfGraphChanged,
    rememberAcceptedProjectionPromptId,
    rememberCompletedManagedPrompt,
    rememberDeferredAppModeTerminal,
    retainManagedArtifactFailure,
    retryManagedArtifactVerification,
    sendManagedCoordinator,
    serializeGraphForDiagnostics,
    settleManagedDisposition,
    terminalizeAppModeExecution,
  };
}
