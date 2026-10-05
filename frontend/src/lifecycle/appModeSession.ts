// App Mode session: managed bootstrap, preparation, start, edit, native preference and retry.
// The lifecycle interpreter owns the run token; only the controller writes the host graph or queues.

import type { AppModeDraft } from "../components/H3Sidebar";
import type { ProductionAccumulatedProject } from "../contracts/productionAccumulationCodec";
import {
  type AppModeAdmission,
  AppModeError,
  type AppModeInputs,
  type AppModeStartOptions,
  type AppModeStartResult,
  type AppModeCanvasResult,
  type ManagedAppModeCanvasIdentity,
  type ManagedAppModePreflight,
  type ManagedAppModePreparation,
  type ManagedAppModeQueueAuthority,
} from "../host/appMode";
import { matchesCreatedProductionContext } from "../host/managedProductionContext";
import { ProductionDestinationError } from "../host/productionActions";
import type { OwnedGraphReference } from "../host/ownedGraphIdentity";
import { buildProductionGenerationBinding } from "../host/productionGeneration";
import {
  type CoordinatorProductionMemberAuthority,
  SequenceCoordinatorClientError,
  type SequenceCoordinatorResult,
} from "../host/sequenceCoordinator";
import type { AppModeEvent } from "./appModeMachine";
import { managedJournal } from "../state/managedJournal";
import {
  hasExistingGraphAuthority,
  projectAppModeSnapshot,
  type ShellErrorCode,
} from "../state/shellState";
import { probeGraphSerializer } from "../host/hostSeams";
import {
  type ShellRuntime,
  type ActiveManagedRun,
  type ManagedBootstrapProjection,
} from "./shellSession";
import { createManagedSerialSession } from "./managedSerialSession";
import {
  admitManagedProjectDestination,
  beginManagedBootstrap,
  buildMemberGenerationBinding,
  installManagedBootstrapContext,
  installManagedMemberProject,
  memberAuthorityMatchesDestination,
  releaseManagedProjectDestination,
  settleAcceptedManagedProjectFailure,
  type ManagedMemberRetry,
  type ManagedProjectDestinationAdmission,
} from "./managedProjectMember";

import { browserAcceptanceManagedExecutionProjector } from "../host/appModeManagedPreparation";
import {
  createAppModeLifecycleTransitions,
  routeDecision,
  requestedAppModeRoute,
  workingRouteRetainsExistingGraph,
} from "./appModeSessionPolicy";

export type { ManagedSerialSequenceBindings } from "./managedSerialSession";

export function createAppModeSession(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;
  const {
    completeAppModeLifecycle,
    failAppModeLifecycle,
    currentAppModeCapability,
    currentAppModeRun,
    isCurrentAppModeRun,
  } = createAppModeLifecycleTransitions(ctx);
  const managedSerialSession = createManagedSerialSession(
    ctx,
    (inputs, options) => startAppMode(inputs, options),
  );
  const {
    cancelManagedSerialSequence,
    detachManagedSerialSequence,
    managedSerialCanvasLabel,
    managedSerialSequenceSnapshot,
    observeManagedSerialArtifact,
    observeManagedSerialRunning,
    observeManagedSerialTerminal,
    reattachManagedSerialSequence,
    resumeManagedSerialSequence,
    retryManagedSerialSequence,
    settleManagedSerialSequence,
    startManagedSerialSequence,
  } = managedSerialSession;

  function currentAppModeAdmission(
    taskMode: string,
  ): AppModeAdmission | undefined {
    const known = session.appModeAdmissions.get(taskMode);
    if (known !== undefined) return known;
    if (
      !session.appModeAdmissionsPending.has(taskMode) &&
      session.appModeAdmissionAttemptRevision.get(taskMode) !==
        session.appModeDraftRevision
    ) {
      session.appModeAdmissionAttemptRevision.set(
        taskMode,
        session.appModeDraftRevision,
      );
      session.appModeAdmissionsPending.add(taskMode);
      void deps.appModeController.admission(taskMode).then(
        (value) => {
          session.appModeAdmissionsPending.delete(taskMode);
          // A host that could not be asked is not a decision, so it is not cached:
          // caching it would leave the sidebar refusing for the rest of the
          // session over one unreachable read. The next render asks again, and no
          // re-render is requested here so the retry cannot spin.
          if (
            value.status === "refused" &&
            value.reason === "profile_unavailable"
          )
            return;
          session.appModeAdmissions.set(taskMode, value);
          actions.renderCurrent();
        },
        () => {
          session.appModeAdmissionsPending.delete(taskMode);
        },
      );
    }
    return undefined;
  }

  function updateAppModeDraft(next: AppModeDraft): void {
    session.appModeDraft = next;
    // IMPORTANT: retry an unanswered host-capability read only after authored
    // input changes. Unrelated async projections may re-render this shell.
    session.appModeDraftRevision += 1;
    actions.renderCurrent();
  }

  function startNewProductionProject(): void {
    deps.productionDestinations.startNew(
      deps.appModeController.activeWorkflow(),
    );
    session.retryableManagedProductionMember = undefined;
    session.productionAccumulationNotice = undefined;
    actions.renderCurrent();
  }

  function sendAppModeLifecycle(event: AppModeEvent, project = false): void {
    const snapshot = deps.appModeLifecycle.send(event);
    if (project)
      session.state = projectAppModeSnapshot(snapshot, session.state);
  }

  async function prepareManagedAppMode(
    run: number,
    inputs: AppModeInputs,
    options: AppModeStartOptions | undefined,
    prepared: ManagedAppModePreflight,
    signal: AbortSignal,
    retryMember?: ManagedMemberRetry,
  ): Promise<ManagedAppModeQueueAuthority> {
    if (session.pendingAppMode?.run !== run)
      throw new AppModeError(
        "ambiguous_host_ownership",
        "the managed canvas identity has no active run owner",
      );
    const workflow = deps.appModeController.activeWorkflow();
    const boundDestination = deps.productionDestinations.peek(workflow);
    let productionAdmission: ManagedProjectDestinationAdmission | undefined;
    let admissionReleased = false;
    const releaseProductionAdmission = async (): Promise<void> => {
      if (productionAdmission === undefined || admissionReleased) return;
      admissionReleased = true;
      try {
        await releaseManagedProjectDestination({
          client: deps.productionActions,
          requestId: `production.release.${actions.browserRequestSessionToken}.${run}`,
          admissionRequestId: productionAdmission.requestId,
        });
      } catch {
        // The backend also expires unopened admissions; preserve the lifecycle disposition.
      }
    };
    // IMPORTANT: a legacy existing graph without a bound project keeps its historical direct
    // route, but every Start after a project is bound must admit a fresh member. Skipping admission
    // for all existing routes silently stops automatic accumulation after the first segment.
    if (
      prepared.observation.route !== "existing" ||
      boundDestination?.kind === "project" ||
      // IMPORTANT: preparation is not generation. Its first explicit manual
      // queue still needs the destination admission formerly owned by Replace.
      deps.appModeController.hasPreparedCanvas?.() === true
    ) {
      try {
        if (retryMember !== undefined) {
          if (
            workflow !== retryMember.workflow ||
            boundDestination?.kind !== "project" ||
            boundDestination.workspaceHandle !== retryMember.workspaceHandle ||
            boundDestination.workspaceId !== retryMember.workspaceId
          )
            throw new AppModeError(
              "ambiguous_host_ownership",
              "the failed Production member no longer belongs to this workflow",
            );
        }
        productionAdmission = await admitManagedProjectDestination({
          client: deps.productionActions,
          destinations: deps.productionDestinations,
          workflow,
          current: actions.currentProductionProjection(),
          requestId: `production.admit.${actions.browserRequestSessionToken}.${run}`,
          segmentId: retryMember?.segmentId,
          signal,
        });
        if (session.pendingAppMode?.run !== run || signal.aborted) {
          // IMPORTANT: cancellation can clear the pending owner while an accepted admission
          // response is still in flight. Release without the aborted run signal or the project
          // remains reserved until its server TTL expires.
          await releaseProductionAdmission();
          throw new AppModeError(
            "cancelled",
            "the Production destination admission was superseded",
          );
        }
        session.pendingAppMode.productionAdmission = productionAdmission;
      } catch (error) {
        if (error instanceof AppModeError) throw error;
        throw new AppModeError(
          error instanceof ProductionDestinationError &&
            error.reason === "accumulation_unsupported"
            ? "incompatible_seam"
            : "queue_failed",
          error instanceof ProductionDestinationError
            ? `the Production destination was refused (${error.reason})`
            : "the Production destination could not be admitted",
        );
      }
    }
    session.pendingManagedArtifactAdmission = Object.freeze({
      run,
    });
    session.state = {
      status: "working",
      phase: "preparing_context",
      transactionId: run,
      existingGraph: prepared.observation.route === "existing",
      graphFingerprint: prepared.observation.graph_fingerprint,
    };
    actions.renderCurrent();
    // CRITICAL: install the ProductShell waiter before the one host queue call.
    // Its projection and the native model must share the exact same prompt ID;
    // a preparatory host-queue call would violate D12 and can duplicate host work.
    const bootstrapHandle = beginManagedBootstrap(ctx, run, prepared, signal);
    let boundPrepared: ManagedAppModePreparation | undefined;
    let preparedAuthority: SequenceCoordinatorResult | undefined;
    let memberProjection: ProductionAccumulatedProject | undefined;
    let productionMemberAuthority:
      CoordinatorProductionMemberAuthority | undefined;
    let managed: ActiveManagedRun | undefined;
    let claimedSubmission:
      ReturnType<typeof deps.generationSequenceDriver.claim> | undefined;
    return Object.freeze({
      expectedIdentity: Object.freeze({
        graphFingerprint: prepared.observation.graph_fingerprint,
        compiledPromptFingerprint:
          prepared.observation.compiled_prompt_fingerprint,
      }),
      bindCanvasIdentity(identity: ManagedAppModeCanvasIdentity) {
        if (session.pendingAppMode?.run !== run)
          throw new AppModeError(
            "ambiguous_host_ownership",
            "the managed canvas identity has no active run owner",
          );
        boundPrepared = Object.freeze({
          ...prepared,
          managedIdentity: identity,
        });
        session.pendingAppMode.managedIdentity =
          actions.managedCanvasIdentityFromPreparation(boundPrepared);
      },
      onQueueSubmitted() {
        session.state = {
          status: "working",
          phase: "generating",
          transactionId: run,
          existingGraph: prepared.observation.route === "existing",
          graphFingerprint: prepared.observation.graph_fingerprint,
        };
        actions.renderCurrent();
      },
      async onQueueAccepted(result) {
        try {
          const committedPreparation = boundPrepared;
          if (committedPreparation === undefined)
            throw new AppModeError(
              "ambiguous_host_ownership",
              "the accepted managed run has no bound canvas identity",
            );
          const bootstrap = await bootstrapHandle.bindPrompt(
            result.queuePromptId,
          );
          actions.recordManagedStage("managed_bootstrap_returned", run);
          if (signal.aborted || !isCurrentAppModeRun(run)) {
            actions.recordManagedStage("managed_bootstrap_stale", run);
            throw new AppModeError(
              "ambiguous_host_ownership",
              "the accepted managed run was superseded",
            );
          }
          actions.recordManagedStage("aggregate_prepare_invoked", run);
          preparedAuthority = await actions.sendManagedCoordinator(
            run,
            actions.nextManagedRequestId("prepare", run),
            "prepare_managed_run",
            {
              context_workspace_handle: bootstrap.workspace.workspace_id,
              correlation: {
                prompt_id: result.queuePromptId,
                execution_node_id:
                  bootstrap.projection.correlation.execution_node_id,
              },
              observation: prepared.observation,
              ...(productionAdmission === undefined
                ? {}
                : {
                    production_admission_request_id:
                      productionAdmission.requestId,
                  }),
            },
          );
          actions.recordManagedStage("aggregate_prepare_returned", run);
          if (
            preparedAuthority.disposition !== "prepared" ||
            preparedAuthority.sequence.eligible_commands.length !== 1
          )
            throw new AppModeError(
              "ambiguous_host_ownership",
              "the accepted generation has no initial Production command",
            );
          const workspaceAuthority = {
            reportId: bootstrap.workspace.report_id,
            taskMode: bootstrap.workspace.task_mode,
            requestedDurationMilliseconds:
              bootstrap.workspace.capabilities.output_duration
                .requested_seconds * 1000,
            effectiveDurationMilliseconds:
              bootstrap.workspace.planning.timeline
                .effective_duration_milliseconds,
            frameCount:
              bootstrap.workspace.planning.timeline.effective_frame_count,
            referenceIds: bootstrap.workspace.reference_candidates.map(
              (item) => item.asset_id,
            ),
          } as const;
          let binding;
          if (productionAdmission !== undefined) {
            if (
              preparedAuthority.schema !==
                "h3.context.generation_coordinator.managed_member_response.v1" ||
              !memberAuthorityMatchesDestination(
                preparedAuthority.productionMemberAuthority,
                productionAdmission.target,
              )
            )
              throw new SequenceCoordinatorClientError(
                "cross_authority_response",
                500,
              );
            const refreshed = await deps.productionActions.readAccumulated(
              `production.read_member.${actions.browserRequestSessionToken}.${run}`,
              {
                workspaceHandle:
                  preparedAuthority.productionMemberAuthority.workspaceHandle,
                workspaceId:
                  preparedAuthority.productionMemberAuthority.workspaceId,
              },
              signal,
            );
            memberProjection = refreshed;
            productionMemberAuthority =
              preparedAuthority.productionMemberAuthority;
            binding = buildMemberGenerationBinding({
              project: memberProjection,
              member: productionMemberAuthority,
              sequence: preparedAuthority.sequence,
              workspace: workspaceAuthority,
              request: { inputs, options },
            });
          } else {
            // CRITICAL: a serial-child reference cannot authorize legacy bootstrap binding.
            if (preparedAuthority.production === null)
              throw new SequenceCoordinatorClientError("invalid_response", 500);
            if (
              !matchesCreatedProductionContext(preparedAuthority.production, {
                contextWorkspaceId: bootstrap.workspace.workspace_id,
                taskMode: bootstrap.workspace.task_mode,
                effectiveDurationMilliseconds:
                  bootstrap.workspace.planning.timeline
                    .effective_duration_milliseconds,
                frameCount:
                  bootstrap.workspace.planning.timeline.effective_frame_count,
              })
            )
              throw new AppModeError(
                "ambiguous_host_ownership",
                "the accepted generation has no matching Production workspace",
              );
            binding = buildProductionGenerationBinding({
              production: preparedAuthority.production,
              sequence: preparedAuthority.sequence,
              jobId: preparedAuthority.sequence.eligible_commands[0]!.job_id,
              workspace: workspaceAuthority,
              request: { inputs, options },
            });
          }
          if (binding === undefined)
            throw new AppModeError(
              "ambiguous_host_ownership",
              "the accepted generation does not match its Production command",
            );
          claimedSubmission = deps.generationSequenceDriver.claim(
            preparedAuthority.sequence,
            binding,
          );
          managed = {
            run,
            prepared: committedPreparation,
            bootstrap,
            authority: preparedAuthority,
            acceptingModelPromptId: result.queuePromptId,
            pendingProjectionPromptIds: new Set(),
            pendingTerminals: new Map(),
            pendingArtifacts: new Map(),
            artifactRetryInFlight: false,
            transition: Promise.resolve(),
            completed: false,
            ...(productionAdmission === undefined ||
            memberProjection === undefined ||
            productionMemberAuthority === undefined
              ? {}
              : {
                  projectMember: {
                    admission: productionAdmission,
                    member: productionMemberAuthority,
                    projection: memberProjection,
                  },
                }),
          };
          const deferredArtifact = session.deferredManagedArtifacts.get(
            result.queuePromptId,
          );
          if (deferredArtifact?.run === run)
            managed.pendingArtifacts.set(
              result.queuePromptId,
              deferredArtifact.event,
            );
          const deferredTerminal = session.deferredAppModeTerminals.get(
            result.queuePromptId,
          );
          if (deferredTerminal?.run === run)
            managed.pendingTerminals.set(
              result.queuePromptId,
              deferredTerminal.kind,
            );
          // CRITICAL: prepare runs after the bootstrap waiter closes but before active managed
          // ownership exists. Transfer only the accepted prompt or its one terminal is lost.
          actions.clearDeferredAppModeTerminals(run);
          actions.clearDeferredManagedArtifacts(run);
          session.activeManagedRun = managed;
          claimedSubmission.onQueueSubmitted();
          const observation = claimedSubmission.complete(result);
          const submitted = await actions.sendManagedCoordinator(
            run,
            actions.nextManagedRequestId("submission", run),
            "submit_managed_run",
            {
              run_handle: managed.authority.runHandle,
              expected_state_fingerprint:
                managed.authority.sequence.state_fingerprint,
              job_id: observation.job_id,
              transaction_id: observation.transaction_id,
              graph_fingerprint: observation.graph_fingerprint,
              compiled_prompt_fingerprint:
                observation.compiled_prompt_fingerprint,
              queue_prompt_id: observation.queue_prompt_id,
            },
          );
          if (session.activeManagedRun !== managed)
            throw new AppModeError(
              "ambiguous_host_ownership",
              "the accepted generation no longer has the active Production owner",
            );
          if (managed.projectMember !== undefined) {
            if (
              submitted.schema !==
                "h3.context.generation_coordinator.managed_member_response.v1" ||
              submitted.productionMemberAuthority.workspaceHandle !==
                managed.projectMember.projection.workspaceHandle ||
              submitted.productionMemberAuthority.workspaceId !==
                managed.projectMember.projection.workspaceId ||
              submitted.productionMemberAuthority.memberSegmentId !==
                managed.projectMember.member.memberSegmentId
            )
              throw new SequenceCoordinatorClientError(
                "cross_authority_response",
                500,
              );
            installManagedMemberProject(
              ctx,
              bootstrap,
              managed.projectMember.projection,
              managed.projectMember.admission,
              committedPreparation.managedIdentity.workflowAuthority,
            );
          } else {
            // CRITICAL: compact serial-child authority cannot populate a legacy bootstrap.
            if (submitted.production === null)
              throw new SequenceCoordinatorClientError("invalid_response", 500);
            installManagedBootstrapContext(
              ctx,
              bootstrap,
              submitted.production,
            );
          }
          managed.modelPromptId = result.queuePromptId;
          managed.acceptingModelPromptId = undefined;
          await actions.settleManagedDisposition(managed, submitted);
          const earlyArtifact = managed.pendingArtifacts.get(
            result.queuePromptId,
          );
          const earlyTerminal = managed.pendingTerminals.get(
            result.queuePromptId,
          );
          managed.pendingArtifacts.clear();
          managed.pendingTerminals.clear();
          if (earlyArtifact?.promptId === result.queuePromptId)
            actions.enqueueManagedHostEvent(managed, {
              kind: "artifact",
              value: earlyArtifact,
            });
          if (earlyTerminal !== undefined)
            actions.enqueueManagedHostEvent(managed, {
              kind: "terminal",
              value: { promptId: result.queuePromptId, kind: earlyTerminal },
            });
        } catch (error) {
          if (
            productionAdmission !== undefined &&
            preparedAuthority === undefined
          )
            admissionReleased = await settleAcceptedManagedProjectFailure(
              ctx,
              productionAdmission,
              run,
              error,
              boundPrepared?.managedIdentity.workflowAuthority,
            );
          // IMPORTANT: accepted bootstrap terminals already have an exact class.
          // Replacing them here fabricates ambiguous ownership after a real host failure.
          if (error instanceof AppModeError) throw error;
          throw new AppModeError(
            "ambiguous_host_ownership",
            error instanceof SequenceCoordinatorClientError
              ? "the host accepted generation but its Production owner could not be confirmed"
              : "the accepted generation has ambiguous Production ownership",
          );
        }
      },
      async onQueueFailed(disposition) {
        const ambiguous = disposition === "ambiguous";
        actions.clearDeferredManagedArtifacts(run);
        bootstrapHandle.reject(
          new AppModeError(
            ambiguous ? "ambiguous_host_ownership" : "queue_failed",
            ambiguous
              ? "the managed prompt crossed host ownership without an exact acknowledgement"
              : disposition === "rejected"
                ? "the managed prompt was refused before host ownership"
                : "the managed prompt stopped before host queue invocation",
          ),
        );
        try {
          claimedSubmission?.fail();
        } catch {
          // A completed claim is already bound to the accepted host prompt.
        }
        // CRITICAL: explicit queue refusal ends local work immediately. Waiting
        // for reservation cleanup here leaves Sidebar working when that request stalls.
        void releaseProductionAdmission();
        // IMPORTANT: only an unacknowledged invocation can already belong to the
        // host; treating an explicit rejection as truthy skips safe rollback.
        if (ambiguous) {
          if (managed !== undefined && session.activeManagedRun === managed)
            actions.failManagedRun(
              managed,
              "ambiguous_host_ownership",
              "use_native",
            );
          return;
        }
        if (managed !== undefined && session.activeManagedRun === managed)
          session.activeManagedRun = undefined;
      },
    });
  }

  function startAppMode(
    inputs: AppModeInputs,
    options?: AppModeStartOptions,
    retryMember?: ManagedMemberRetry,
  ): Promise<AppModeStartResult> {
    return runAppMode(inputs, options, false, retryMember);
  }

  function prepareAppModeCanvas(
    inputs: AppModeInputs,
    options?: AppModeStartOptions,
  ): Promise<AppModeCanvasResult> {
    return runAppMode(inputs, options, true);
  }

  function runAppMode(
    inputs: AppModeInputs,
    options: AppModeStartOptions | undefined,
    canvasOnly: false,
    retryMember?: ManagedMemberRetry,
  ): Promise<AppModeStartResult>;
  function runAppMode(
    inputs: AppModeInputs,
    options: AppModeStartOptions | undefined,
    canvasOnly: true,
    retryMember?: ManagedMemberRetry,
  ): Promise<AppModeCanvasResult>;
  async function runAppMode(
    inputs: AppModeInputs,
    options: AppModeStartOptions | undefined,
    canvasOnly: boolean,
    retryMember?: ManagedMemberRetry,
  ): Promise<AppModeStartResult | AppModeCanvasResult> {
    if (retryMember === undefined)
      session.retryableManagedProductionMember = undefined;
    const requestedRoute = requestedAppModeRoute(options);
    const existingGraphDuringRun =
      workingRouteRetainsExistingGraph(requestedRoute);
    const existingGraphBeforeRun =
      hasExistingGraphAuthority(session.state) ||
      options?.useExisting === true ||
      options?.replaceExisting === true ||
      options?.connectExisting !== undefined;
    const run = deps.appModeLifecycle.begin({
      route: requestedRoute,
      existingGraph: existingGraphDuringRun,
    });
    const journalRun = run;
    sendAppModeLifecycle({
      type: "CENSUS_RESOLVED",
      decision: routeDecision(requestedRoute),
    });
    sendAppModeLifecycle({ type: "DECISION_ACCEPTED" }, true);
    const runSignal = deps.appModeLifecycle.signalFor(run);
    // CRITICAL: every App Mode side effect must share the interpreter-owned run
    // signal; recreating a controller here would reintroduce stale completion.
    if (runSignal === undefined)
      throw new AppModeError("cancelled", "the App Mode run was superseded");
    const currentAdmission = currentAppModeAdmission(inputs.task_mode);
    const runCapability = currentAppModeCapability();
    for (const name of [
      "missing_load_graph_data",
      "missing_graph_to_prompt",
      "missing_detached_graph_constructor",
      "missing_queue_prompt",
      "missing_workflow_store",
      "missing_load_api_json",
    ] as const)
      managedJournal.recordCapability(
        journalRun,
        name,
        runCapability.status === "unavailable" && runCapability.reason === name,
      );
    managedJournal.recordCapability(
      journalRun,
      "profile_unavailable",
      currentAdmission?.status === "refused" &&
        currentAdmission.reason === "profile_unavailable",
    );
    const previousStartSnapshot = session.priorManagedStartSnapshot;
    const startSnapshot = actions.serializeGraphForDiagnostics();
    session.priorManagedStartSnapshot = startSnapshot;
    // Keep a cancelled transaction quarantine until the graph identity changes;
    // a newly queued result is admitted only by its exact pending prompt ID.
    actions.releaseProjectionQuarantineIfGraphChanged();
    session.acceptedManagedIdentity = undefined;
    session.ignoreProjectionUntilGraphRefresh = false;
    session.graphChangedDuringRun = false;
    session.executedGraphRefresh = undefined;
    actions.clearDeferredAppModeProjections();
    actions.clearDeferredAppModeTerminals();
    actions.clearDeferredManagedArtifacts();
    actions.clearActiveAppModeExecution();
    session.lastAppModeRequest = { inputs, options };
    const suppliedManagedPreparation = canvasOnly
      ? undefined
      : (options?.prepareManaged ??
        (options?.connectExisting === undefined &&
        options?.expectedIdentity === undefined
          ? (prepared: ManagedAppModePreflight) =>
              prepareManagedAppMode(
                run,
                inputs,
                options,
                prepared,
                runSignal,
                retryMember,
              )
          : undefined));
    let managedOwnedReference: OwnedGraphReference | undefined;
    const managedPreparation =
      suppliedManagedPreparation === undefined
        ? undefined
        : async (prepared: ManagedAppModePreflight) => {
            const authority = await suppliedManagedPreparation(prepared);
            return Object.freeze({
              ...authority,
              bindCanvasIdentity(identity: ManagedAppModeCanvasIdentity) {
                const pending = session.pendingAppMode;
                if (pending?.run !== run || !isCurrentAppModeRun(run))
                  throw new AppModeError(
                    "ambiguous_host_ownership",
                    "the managed canvas identity has no active run owner",
                  );
                managedOwnedReference = identity.ownedReference;
                actions.recordSurroundingsEvidence(
                  journalRun,
                  "consecutive_start",
                  previousStartSnapshot,
                  startSnapshot,
                  managedOwnedReference,
                );
                authority.bindCanvasIdentity?.(identity);
                if (
                  session.pendingAppMode !== pending ||
                  !isCurrentAppModeRun(run)
                )
                  throw new AppModeError(
                    "ambiguous_host_ownership",
                    "the managed canvas identity owner changed during binding",
                  );
                // CRITICAL: external serial preparation needs the same owned identity as
                // aggregate preparation. Otherwise projection correlation hashes foreign
                // host metadata and rejects a successfully submitted child as graph drift.
                pending.managedIdentity =
                  actions.managedCanvasIdentityFromPreparation({
                    ...prepared,
                    managedIdentity: identity,
                  });
              },
            });
          };
    const runOptions: AppModeStartOptions = {
      ...(options ?? {}),
      signal: runSignal,
      transactionId: run,
      // M17-20 D5: the artifact location is scoped to the workspace this run
      // belongs to, so two workspaces never write into each other's output. The
      // handle is a backend-issued identifier; App Mode validates it and falls
      // back to a session scope rather than putting anything unvalidated in a path.
      artifactScope:
        options?.artifactScope ?? actions.currentProjection()?.workspace_id,
      prepareManaged: managedPreparation,
      projectManagedExecution: canvasOnly
        ? undefined
        : (options?.projectManagedExecution ??
          browserAcceptanceManagedExecutionProjector()),
      onQueueSeamObserved: (observation) => {
        try {
          managedJournal.recordQueueSeam(journalRun, observation);
        } catch {
          // Diagnostics cannot alter the sole host queue operation.
        }
        options?.onQueueSeamObserved?.(observation);
      },
      onHostLoadBeforeCompile: () => {
        options?.onHostLoadBeforeCompile?.();
        actions.recordManagedStage("candidate_host_load_before_compile", run);
      },
      observeSourceIdentity:
        options?.observeSourceIdentity ??
        ((request, signal) =>
          deps.inputGeometryClient.observe(request, signal)),
      onQueueSubmitted: () => {
        options?.onQueueSubmitted?.();
        if (!isCurrentAppModeRun(run)) return;
        // The managed callback already moved the one accepted prompt into its
        // generating phase. Do not overwrite that truth with the legacy
        // unmanaged queueing label while its ProductShell projection is pending.
        if (managedPreparation !== undefined) return;
        if (session.activeManagedRun?.run === run) return;
        session.state = {
          status: "working",
          phase: "queueing",
          transactionId: run,
          existingGraph: existingGraphDuringRun,
        };
        actions.renderCurrent();
      },
    };
    session.pendingAppMode = { run, inputs, options: runOptions };
    session.state = {
      status: "working",
      phase: options?.useExisting === true ? "compiling" : "materializing",
      transactionId: run,
      existingGraph: existingGraphDuringRun,
    };
    actions.renderCurrent();
    try {
      const result = canvasOnly
        ? await deps.appModeController.prepareCanvas(inputs, runOptions)
        : await deps.appModeController.start(inputs, runOptions);
      if (!isCurrentAppModeRun(run))
        throw new AppModeError("cancelled", "the App Mode run was superseded");
      sendAppModeLifecycle({
        type: "VALIDATED",
        executionIdentity: result.compiledPromptFingerprint,
      });
      if (result.preparedOnly === true) {
        // IMPORTANT: readiness owns the written canvas, never an execution. A
        // subsequent user queue must compile the visible graph, including edits.
        sendAppModeLifecycle({
          type: "CANVAS_READY",
          ownedNodeIds: result.ownedNodeIds,
          ownedLinkIds: result.ownedLinkIds,
          ownedProjectionFingerprint: result.ownedProjectionFingerprint,
        });
        deps.appModeLifecycle.complete(run);
        session.pendingAppMode = undefined;
        session.state = {
          status: "interactive",
          reason: "canvas_ready",
          existingGraph: true,
        };
        actions.renderCurrent();
        return result;
      }
      sendAppModeLifecycle({
        type: "WRITTEN",
        ownedNodeIds: result.ownedNodeIds,
        ownedLinkIds: result.ownedLinkIds,
        ownedProjectionFingerprint: result.ownedProjectionFingerprint,
      });
      sendAppModeLifecycle({ type: "PREPARATION_READY" });
      sendAppModeLifecycle({
        type: "QUEUE_ACCEPTED",
        promptId: result.queuePromptId,
      });
      if (session.completedManagedPromptIds.has(result.queuePromptId))
        return result;
      const managedIdentity =
        session.pendingAppMode?.run === run
          ? session.pendingAppMode.managedIdentity
          : undefined;
      const productionAdmission =
        session.pendingAppMode?.run === run
          ? session.pendingAppMode.productionAdmission
          : undefined;
      session.pendingAppMode = {
        run,
        inputs,
        options: runOptions,
        result,
        managedIdentity,
        productionAdmission,
      };
      if (session.activeManagedRun?.run === run) {
        sendAppModeLifecycle({ type: "EXECUTION_STARTED" });
        actions.clearDeferredAppModeProjections(run);
        actions.clearDeferredAppModeTerminals(run);
        actions.clearDeferredManagedArtifacts(run);
        session.state = {
          status: "working",
          phase: "generating",
          transactionId: run,
          existingGraph: workingRouteRetainsExistingGraph(result.route),
          graphFingerprint: result.graphFingerprint,
        };
        return result;
      }
      session.activeAppModeExecution = { run, promptId: result.queuePromptId };
      session.state = {
        status: "working",
        phase: "queueing",
        transactionId: run,
        existingGraph: workingRouteRetainsExistingGraph(result.route),
        graphFingerprint: result.graphFingerprint,
      };
      // CRITICAL: supported hosts may emit ProductShell execution before the
      // queue promise exposes its prompt ID. Replay only the exact correlated
      // event after queue identity is available; every foreign event is dropped.
      const deferred = session.deferredAppModeProjections.get(
        result.queuePromptId,
      );
      const deferredTerminal = session.deferredAppModeTerminals.get(
        result.queuePromptId,
      );
      actions.clearDeferredAppModeProjections(run);
      actions.clearDeferredAppModeTerminals(run);
      actions.clearDeferredManagedArtifacts(run);
      if (
        deferredTerminal?.kind === "error" ||
        deferredTerminal?.kind === "interrupted"
      ) {
        actions.terminalizeAppModeExecution({
          promptId: result.queuePromptId,
          kind: deferredTerminal.kind,
        });
        return result;
      }
      if (deferred !== undefined) {
        const identity = actions.currentGraphProjectionIdentity();
        const serializer = probeGraphSerializer(deps.app);
        if (
          identity !== undefined &&
          identity.executionNodeId === deferred.executionNodeId &&
          serializer.status === "ready"
        ) {
          try {
            const serialized = serializer.value();
            if (
              actions.isModelFreeProjectionGraph(
                serialized,
                deferred.executionNodeId,
              )
            )
              session.executedGraphRefresh = {
                run,
                graphFingerprint: identity.graphFingerprint,
                executionNodeId: deferred.executionNodeId,
              };
          } catch {
            // The normal correlation checks below fail closed on unreadable graph state.
          }
        }
      }
      deferred?.accept();
      if (deferredTerminal?.kind === "success")
        actions.terminalizeAppModeExecution({
          promptId: result.queuePromptId,
          kind: "success",
        });
      return result;
    } catch (error) {
      if (!isCurrentAppModeRun(run)) throw error;
      if (error instanceof AppModeError)
        managedJournal.recordAppModeError(journalRun, {
          code: error.code,
          source: error.source,
          ...(error.reason === undefined ? {} : { reason: error.reason.kind }),
        });
      if (error instanceof AppModeError && error.code === "cancelled") {
        session.pendingAppMode = undefined;
        session.state = { status: "interactive", reason: "cancelled" };
        deps.appModeLifecycle.invalidate();
      } else if (
        error instanceof AppModeError &&
        error.code === "dirty_graph"
      ) {
        session.pendingAppMode = undefined;
        session.state = {
          status: "interactive",
          reason: "dirty_graph",
          existingGraph: true,
        };
        sendAppModeLifecycle({ type: "REFUSED", reason: error.code });
        deps.appModeLifecycle.invalidate();
      } else if (
        error instanceof AppModeError &&
        error.code === "incompatible_graph"
      ) {
        session.pendingAppMode = undefined;
        if (error.reason === undefined)
          actions.recordManagedStage("refusal_reason_unknown", run);
        session.state = {
          status: "interactive",
          reason: error.code,
          existingGraph: true,
          refusalReason: error.reason,
        };
        sendAppModeLifecycle({ type: "REFUSED", reason: error.code });
        deps.appModeLifecycle.invalidate();
      } else {
        const safeError =
          error instanceof AppModeError
            ? error
            : new AppModeError(
                "queue_failed",
                "the H3 run could not be completed",
              );
        const safeCode: ShellErrorCode =
          safeError.code === "compile_failed" ||
          safeError.code === "queue_failed" ||
          safeError.code === "execution_failed" ||
          safeError.code === "execution_interrupted" ||
          safeError.code === "incompatible_seam" ||
          safeError.code === "stale_graph" ||
          safeError.code === "rollback_failed" ||
          safeError.code === "ambiguous_host_ownership"
            ? safeError.code
            : "compile_failed";
        if (safeError.reason === undefined)
          actions.recordManagedStage("refusal_reason_unknown", run);
        if (
          safeError.code === "compile_failed" ||
          safeError.code === "queue_failed" ||
          safeError.code === "execution_failed" ||
          safeError.code === "execution_interrupted" ||
          safeError.code === "stale_graph" ||
          safeError.code === "rollback_failed" ||
          safeError.code === "ambiguous_host_ownership"
        ) {
          // CRITICAL: a failed compile/queue may already have reached the host
          // execution boundary; quarantine late ProductShell events until a new
          // matching run or a changed graph identity authenticates them.
          session.cancelledProjectionSuppression =
            actions.currentProjectionSuppression();
          session.ignoreProjectionUntilGraphRefresh = true;
        }
        session.pendingAppMode = undefined;
        session.state = {
          status: "error",
          code: safeCode,
          severity: "error",
          source:
            safeCode === "execution_failed" ||
            safeCode === "execution_interrupted"
              ? "app_mode"
              : safeError.source,
          message: safeError.code,
          recovery: safeError.recovery,
          existingGraph: existingGraphBeforeRun,
          transactionId: run,
          refusalReason: safeError.reason,
        };
        failAppModeLifecycle(run, safeCode, safeError.recovery);
        // M23-25 validation refusals happen before the sole candidate write. Keep
        // only the graph authority the user already had; a rescan cannot turn the
        // untouched canvas into proof that the refused candidate was repaired.
      }
      actions.clearDeferredAppModeProjections(run);
      actions.clearDeferredAppModeTerminals(run);
      actions.clearDeferredManagedArtifacts(run);
      actions.clearActiveAppModeExecution(run);
      actions.renderCurrent();
      throw error;
    } finally {
      if (managedOwnedReference !== undefined)
        actions.recordSurroundingsEvidence(
          journalRun,
          "within_run",
          startSnapshot,
          actions.serializeGraphForDiagnostics(),
          managedOwnedReference,
        );
      actions.renderCurrent();
    }
  }

  function cancelAppMode(): void {
    // A lost host suspends the run but does not end it, so cancellation reads the interrupted state.
    const interrupted = actions.interruptedShellState();
    if (
      session.pendingAppMode === undefined &&
      interrupted.status !== "working"
    )
      return;
    // IMPORTANT: local cancellation ends at the queue submission boundary;
    // ComfyUI may already be executing and the visible graph must stay honest.
    if (
      interrupted.status === "working" &&
      ["queueing", "generating", "verifying_output"].includes(interrupted.phase)
    )
      return;
    session.cancelledProjectionSuppression =
      actions.currentProjectionSuppression();
    deps.appModeLifecycle.invalidate();
    if (session.pendingAppMode?.result?.queuePromptId !== undefined)
      session.ignoredProjectionPromptIds.add(
        session.pendingAppMode.result.queuePromptId,
      );
    session.ignoreProjectionUntilGraphRefresh = true;
    session.pendingAppMode = undefined;
    actions.clearDeferredAppModeProjections();
    actions.clearDeferredAppModeTerminals();
    actions.clearDeferredManagedArtifacts();
    actions.clearActiveAppModeExecution();
    session.acceptedManagedIdentity = undefined;
    session.graphChangedDuringRun = false;
    session.executedGraphRefresh = undefined;
    session.state = { status: "interactive", reason: "cancelled" };
    actions.renderCurrent();
  }

  function chooseNative(): void {
    const managedPromptId =
      session.activeManagedRun?.modelPromptId ??
      session.activeManagedRun?.acceptingModelPromptId;
    deps.appModeLifecycle.invalidate();
    // IMPORTANT: a cancellation before queuePrompt resolves has no prompt ID.
    // Keep that quarantine through native preference; otherwise a late App Mode
    // event is indistinguishable from a legitimate native execution event.
    if (session.cancelledProjectionSuppression === undefined)
      session.ignoreProjectionUntilGraphRefresh = false;
    const existingGraph = hasExistingGraphAuthority(session.state);
    session.pendingAppMode = undefined;
    // IMPORTANT: choosing native recovery releases local managed authority, so every late event
    // from its already-owned prompt must remain quarantined from the next native interaction.
    if (managedPromptId !== undefined) {
      session.ignoredProjectionPromptIds.add(managedPromptId);
      actions.rememberCompletedManagedPrompt(managedPromptId);
    }
    session.activeManagedRun = undefined;
    session.retryableManagedProductionMember = undefined;
    actions.clearDeferredAppModeProjections();
    actions.clearDeferredAppModeTerminals();
    actions.clearDeferredManagedArtifacts();
    actions.clearActiveAppModeExecution();
    session.acceptedManagedIdentity = undefined;
    session.graphChangedDuringRun = false;
    session.executedGraphRefresh = undefined;
    session.state = {
      status: "interactive",
      reason: "native_preference",
      existingGraph,
    };
    actions.renderCurrent();
    // IMPORTANT: recovery clears stale run ownership before this one-shot census.
    // Defer only to the next microtask so React can commit the recovery render;
    // without the fresh inspection, a compatible kept canvas is mistaken for a
    // dirty graph and the next Start loops back to Replace/Keep.
    void Promise.resolve().then(() => {
      session.nativePreferenceRecoveryRefresh = true;
      try {
        deps.host.refreshGraph();
      } finally {
        session.nativePreferenceRecoveryRefresh = false;
      }
    });
  }

  function retryAppMode(): void {
    const request = session.lastAppModeRequest;
    if (request === undefined) {
      session.state = { status: "interactive", reason: "pending_capability" };
      actions.renderCurrent();
      return;
    }
    void (request.options?.prepareOnly ? prepareAppModeCanvas : startAppMode)(
      request.inputs,
      request.options,
      session.retryableManagedProductionMember,
    ).catch(() => undefined);
  }

  return {
    beginManagedBootstrap,
    browserAcceptanceManagedExecutionProjector,
    cancelManagedSerialSequence,
    cancelAppMode,
    chooseNative,
    completeAppModeLifecycle,
    currentAppModeAdmission,
    currentAppModeCapability,
    currentAppModeRun,
    detachManagedSerialSequence,
    failAppModeLifecycle,
    installManagedBootstrapContext,
    isCurrentAppModeRun,
    managedSerialCanvasLabel,
    managedSerialSequenceSnapshot,
    observeManagedSerialArtifact,
    observeManagedSerialRunning,
    observeManagedSerialTerminal,
    prepareManagedAppMode,
    prepareAppModeCanvas,
    reattachManagedSerialSequence,
    requestedAppModeRoute,
    resumeManagedSerialSequence,
    retryAppMode,
    retryManagedSerialSequence,
    routeDecision,
    sendAppModeLifecycle,
    settleManagedSerialSequence,
    startManagedSerialSequence,
    startNewProductionProject,
    startAppMode,
    updateAppModeDraft,
    workingRouteRetainsExistingGraph,
  };
}
