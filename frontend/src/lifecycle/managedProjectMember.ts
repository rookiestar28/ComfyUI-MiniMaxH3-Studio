import type { GenerationSequenceProjection } from "../contracts/generationSequenceCodec";
import {
  PRODUCTION_ACCUMULATED_PROJECT_SCHEMA,
  type ProductionAccumulatedProject,
} from "../contracts/productionAccumulationCodec";
import type {
  ProductionDestinationTarget,
  ProductionWorkbenchProjection,
} from "../contracts/productionWorkbenchCodec";
import type { GenerationSequenceBinding } from "../host/generationSequence";
import { AppModeError, type ManagedAppModePreflight } from "../host/appMode";
import {
  destinationTarget,
  type ProductionDestination,
  type ProductionDestinationStore,
} from "../host/productionDestination";
import type { ProductionProposalSourceClaim } from "../host/productionProposalDispatcher";
import {
  createProductionActionClient,
  ProductionDestinationError,
} from "../host/productionActions";
import {
  matchesAdmittedMemberProduction,
  type ManagedProductionMemberAuthority,
} from "../host/managedProductionContext";
import type {
  ProductionGenerationRequest,
  ProductionGenerationWorkspaceAuthority,
} from "../host/productionGeneration";
import {
  MANAGED_MEMBER_COORDINATOR_RESPONSE_SCHEMA,
  SequenceCoordinatorClientError,
  type CoordinatorProductionMemberAuthority,
  type SequenceCoordinatorResult,
} from "../host/sequenceCoordinator";
import { reduceWorkspaceState } from "../state/sidebarWorkspace";
import type {
  ActiveManagedRun,
  ManagedBootstrapHandle,
  ManagedBootstrapProjection,
  ManagedBootstrapWaiter,
  ShellRuntime,
} from "./shellSession";

type ProductionDestinationClient = Pick<
  ReturnType<typeof createProductionActionClient>,
  "sendDestination"
>;
type ProductionProjectClient =
  | Pick<ReturnType<typeof createProductionActionClient>, "readAccumulated">
  | Pick<ReturnType<typeof createProductionActionClient>, "send">;

function accumulatedProject(
  value: ProductionAccumulatedProject | ProductionWorkbenchProjection,
): ProductionAccumulatedProject {
  if (value.schema === PRODUCTION_ACCUMULATED_PROJECT_SCHEMA) return value;
  return Object.freeze({
    schema: PRODUCTION_ACCUMULATED_PROJECT_SCHEMA,
    workspaceHandle: value.workspaceHandle,
    workspaceId: value.workspaceId,
    projectRevision: value.workspaceRevision,
    projectFingerprint: value.workspaceFingerprint,
    workspace: value,
    attempts: Object.freeze([]),
  });
}

export function reconcileManagedProposalProject(
  dispatcher: ShellRuntime["deps"]["productionProposalDispatcher"],
  before: ProductionWorkbenchProjection | null,
  after: ProductionWorkbenchProjection | null,
  target: ProductionDestinationTarget,
  source?: ProductionProposalSourceClaim,
): boolean {
  if (after === null) return false;
  if (before === null || target === null)
    return dispatcher.bind({ action: "create", source, after });
  if (
    before.workspaceHandle !== after.workspaceHandle ||
    before.workspaceId !== after.workspaceId
  ) {
    dispatcher.refreshProjection(after);
    return false;
  }
  if (
    before.workspaceRevision === after.workspaceRevision &&
    before.workspaceFingerprint === after.workspaceFingerprint
  ) {
    dispatcher.refreshProjection(after);
    return true;
  }
  return target.segmentId === null
    ? dispatcher.bind({ action: "add", source, before, after })
    : dispatcher.bind({
        action: "replace",
        source,
        before,
        after,
        segmentId: target.segmentId,
      });
}

export function managedBootstrapTerminalError(
  terminal: "error" | "interrupted",
): AppModeError {
  return new AppModeError(
    terminal === "error" ? "execution_failed" : "execution_interrupted",
    terminal === "error"
      ? "the accepted managed Context prompt failed on the host"
      : "the accepted managed Context prompt was interrupted on the host",
  );
}

export function beginManagedBootstrap(
  ctx: ShellRuntime,
  run: number,
  prepared: ManagedAppModePreflight,
  signal: AbortSignal,
): ManagedBootstrapHandle {
  const { session, actions } = ctx;
  actions.recordManagedStage("bootstrap_started", run);
  let timeout = 0;
  let removeAbort = (): void => undefined;
  let settled = false;
  let accept!: (value: ManagedBootstrapProjection) => void;
  let decline!: (error: Error) => void;
  const projection = new Promise<ManagedBootstrapProjection>(
    (resolve, reject) => {
      accept = resolve;
      decline = reject;
    },
  );
  const close = (): void => {
    if (settled) return;
    settled = true;
    window.clearTimeout(timeout);
    removeAbort();
  };
  const waiter: ManagedBootstrapWaiter = {
    run,
    executionNodeId: prepared.productShellNodeId,
    buffered: new Map(),
    terminal: new Map(),
    resolve(value) {
      close();
      accept(value);
    },
    reject(error) {
      close();
      decline(error);
    },
    close,
  };
  session.managedBootstrapWaiter?.reject(
    new AppModeError("cancelled", "the prior Context bootstrap was superseded"),
  );
  session.managedBootstrapWaiter = waiter;
  actions.recordManagedStage("bootstrap_waiter_installed", run);
  const abort = (): void => {
    actions.closeManagedBootstrapWaiter(waiter);
    waiter.reject(
      new AppModeError("cancelled", "the Context bootstrap was cancelled"),
    );
  };
  signal.addEventListener("abort", abort, { once: true });
  removeAbort = () => signal.removeEventListener("abort", abort);
  timeout = window.setTimeout(() => {
    actions.closeManagedBootstrapWaiter(waiter);
    waiter.reject(
      new AppModeError(
        "queue_failed",
        "the managed Context prompt did not return its exact ProductShell projection",
      ),
    );
  }, 120_000);
  // The ProductShell projection now belongs to the same prompt as the model. Observe early
  // rejection immediately; bindPrompt still preserves the exact failure for the queue owner.
  void projection.catch(() => undefined);
  return Object.freeze({
    async bindPrompt(promptId: string) {
      if (signal.aborted)
        throw new AppModeError(
          "cancelled",
          "the managed Context prompt was cancelled",
        );
      waiter.promptId = promptId;
      actions.recordManagedStage("bootstrap_prompt_bound", run);
      const terminal = waiter.terminal.get(promptId);
      if (terminal === "error" || terminal === "interrupted") {
        const error = managedBootstrapTerminalError(terminal);
        actions.closeManagedBootstrapWaiter(waiter);
        waiter.reject(error);
        throw error;
      }
      if (terminal === "success") {
        // CRITICAL: a fast host can finish before queue acceptance binds this exact prompt.
        // Preserve the terminal for managed ownership; ComfyUI will not replay it later.
        actions.rememberDeferredAppModeTerminal(run, {
          promptId,
          kind: "success",
        });
      }
      const buffered = waiter.buffered.get(promptId);
      if (buffered !== undefined) {
        actions.recordManagedStage("bootstrap_buffer_resolved", run);
        actions.closeManagedBootstrapWaiter(waiter);
        waiter.resolve(buffered);
      }
      const accepted = await projection;
      actions.recordManagedStage("bootstrap_projection_returned", run);
      return accepted;
    },
    reject(error: Error) {
      actions.closeManagedBootstrapWaiter(waiter);
      waiter.reject(error);
    },
  });
}

export function installManagedBootstrapContext(
  ctx: ShellRuntime,
  bootstrap: ManagedBootstrapProjection,
  production: ProductionWorkbenchProjection,
): void {
  const { session, deps, actions } = ctx;
  session.workspaceState = reduceWorkspaceState(session.workspaceState, {
    type: "host_execution",
    projection: bootstrap.workspace,
  });
  session.transactionTransparency = bootstrap.transactionTransparency;
  deps.productionProposalDispatcher.resetAll();
  if (bootstrap.semanticProposalReview === undefined)
    deps.productionProposalDispatcher.clearCurrentSource();
  else
    deps.productionProposalDispatcher.observe(
      {
        workspace_id: bootstrap.workspace.workspace_id,
        report_revision: bootstrap.workspace.report_revision,
        report_fingerprint: bootstrap.workspace.report_fingerprint,
      },
      bootstrap.semanticProposalReview,
    );
  deps.productionProposalDispatcher.bind({
    action: "create",
    source: deps.productionProposalDispatcher.captureCurrent(),
    after: production,
  });
  actions.rememberAcceptedProjectionPromptId(
    bootstrap.projection.correlation.prompt_id,
  );
  session.acceptedGenerationSequence = undefined;
  session.productionAbort?.abort();
  session.productionAbort = undefined;
  session.productionSessionHandle = undefined;
  actions.clearProductionSessionHandle();
  session.productionContextBinding = Object.freeze({
    productionWorkspaceHandle: production.workspaceHandle,
    productionWorkspaceId: production.workspaceId,
    contextWorkspaceHandle: bootstrap.workspace.workspace_id,
  });
  session.productionState = { status: "absent" };
  session.retryableProductionRequest = undefined;
  session.productionEnsureActive = undefined;
  session.productionEnsureAttemptedIdentities.clear();
  session.productionEnsureClosedIdentities.clear();
  deps.pageRegistry.register({ id: "production" });
  actions.renderCurrent();
}

export type ManagedProjectDestinationAdmission = Readonly<{
  requestId: string;
  destination: ProductionDestination;
  target: ProductionDestinationTarget;
  workflow: object | undefined;
}>;

export type ManagedMemberRetry = Readonly<{
  workflow: object | undefined;
  workspaceHandle: string;
  workspaceId: string;
  segmentId: string;
}>;

export function managedMemberRetry(
  managed: ActiveManagedRun,
): ManagedMemberRetry | undefined {
  const projectMember = managed.projectMember;
  return projectMember === undefined
    ? undefined
    : Object.freeze({
        workflow: projectMember.admission.workflow,
        workspaceHandle: projectMember.member.workspaceHandle,
        workspaceId: projectMember.member.workspaceId,
        segmentId: projectMember.member.memberSegmentId,
      });
}

export async function admitManagedProjectDestination({
  client,
  destinations,
  workflow,
  current,
  requestId,
  segmentId,
  signal,
}: Readonly<{
  client: ProductionDestinationClient;
  destinations: ProductionDestinationStore;
  workflow: object | undefined;
  current: ProductionWorkbenchProjection | undefined;
  requestId: string;
  segmentId?: string;
  signal?: AbortSignal;
}>): Promise<ManagedProjectDestinationAdmission> {
  const destination = destinations.resolve(workflow, current);
  const target = destinationTarget(destination, segmentId ?? null);
  const send = () =>
    client.sendDestination(
      requestId,
      "admit_generation_destination",
      { target },
      signal,
    );
  const markUnavailable = (error: unknown): void => {
    if (
      error instanceof ProductionDestinationError &&
      error.reason === "destination_unavailable" &&
      target !== null
    )
      destinations.markUnavailable(target.workspaceHandle);
  };
  let result;
  try {
    result = await send();
  } catch (error) {
    markUnavailable(error);
    if (signal?.aborted || error instanceof ProductionDestinationError)
      throw error;
    // IMPORTANT: the backend keys admission replay by this exact request id. Retrying with a
    // new identity can reserve two destinations after a lost response.
    try {
      result = await send();
    } catch (retryError) {
      markUnavailable(retryError);
      throw retryError;
    }
  }
  if (result.status === 204)
    throw new ProductionDestinationError("destination_failed", 500);
  if (!("project" in result))
    return Object.freeze({ requestId, destination, target, workflow });
  // CRITICAL: bind the server-minted empty project before the host queue. Waiting for a committed
  // segment loses the destination on first failure and makes the next Start create another project.
  const captured = destinations.bindProject(workflow, result.project);
  return Object.freeze({ requestId, destination: captured, target, workflow });
}

export async function releaseManagedProjectDestination({
  client,
  requestId,
  admissionRequestId,
  signal,
}: Readonly<{
  client: ProductionDestinationClient;
  requestId: string;
  admissionRequestId: string;
  signal?: AbortSignal;
}>): Promise<void> {
  await client.sendDestination(
    requestId,
    "release_generation_destination",
    { admissionRequestId },
    signal,
  );
}

export async function settleManagedProjectDestination({
  client,
  requestId,
  admissionRequestId,
  terminal,
}: Readonly<{
  client: ProductionDestinationClient;
  requestId: string;
  admissionRequestId: string;
  terminal: "failed" | "cancelled" | "unknown_ownership";
}>): Promise<ProductionAccumulatedProject> {
  const result = await client.sendDestination(
    requestId,
    "settle_generation_destination",
    { admissionRequestId, terminal },
  );
  if (!("project" in result))
    throw new ProductionDestinationError("destination_failed", 500);
  return result.project;
}

export function installSettledAdmissionProject(
  ctx: ShellRuntime,
  admission: ManagedProjectDestinationAdmission,
  project: ProductionAccumulatedProject,
  committedWorkflow: object | undefined,
): void {
  const { session, deps, actions } = ctx;
  if (committedWorkflow !== undefined)
    deps.productionDestinations.commitCapturedProject(
      admission.workflow,
      committedWorkflow,
      admission.destination,
      project,
    );
  const workflow = committedWorkflow ?? admission.workflow;
  const destination = deps.productionDestinations.peek(workflow);
  if (
    destination?.kind === "project" &&
    destination.workspaceHandle === project.workspaceHandle &&
    destination.workspaceId === project.workspaceId
  ) {
    session.productionSessionHandle = project.workspaceHandle;
    actions.writeProductionSessionHandle(project.workspaceHandle);
    session.productionState =
      project.workspace === null
        ? { status: "empty", project }
        : { status: "ready", projection: project.workspace };
  }
  deps.pageRegistry.register({ id: "production" });
  actions.renderCurrent();
}

export async function settleAcceptedManagedProjectFailure(
  ctx: ShellRuntime,
  admission: ManagedProjectDestinationAdmission,
  run: number,
  error: unknown,
  committedWorkflow: object | undefined,
): Promise<boolean> {
  const { session, deps, actions } = ctx;
  const terminal =
    error instanceof AppModeError && error.code === "execution_failed"
      ? "failed"
      : error instanceof AppModeError && error.code === "execution_interrupted"
        ? "cancelled"
        : "unknown_ownership";
  try {
    // IMPORTANT: the accepted host queue can fail before ProductShell returns, so no coordinator
    // member exists yet. Settle the exact admission or Retry silently mints another candidate.
    const settled = await settleManagedProjectDestination({
      client: deps.productionActions,
      requestId: `production.settle.${actions.browserRequestSessionToken}.${run}`,
      admissionRequestId: admission.requestId,
      terminal,
    });
    installSettledAdmissionProject(ctx, admission, settled, committedWorkflow);
    const retryable = settled.attempts.find(
      (attempt) => attempt.recovery === "retry",
    );
    if (retryable !== undefined && deps.appModeLifecycle.isCurrent(run))
      session.retryableManagedProductionMember = Object.freeze({
        workflow: committedWorkflow ?? admission.workflow,
        workspaceHandle: settled.workspaceHandle,
        workspaceId: settled.workspaceId,
        segmentId: retryable.memberSegmentId,
      });
    return true;
  } catch {
    // Preserve the exact accepted-host error; a lost settlement remains bounded by TTL.
    return false;
  }
}

export async function readManagedMemberProject({
  client,
  requestId,
  member,
  signal,
}: Readonly<{
  client: ProductionProjectClient;
  requestId: string;
  member: CoordinatorProductionMemberAuthority;
  signal?: AbortSignal;
}>): Promise<ProductionAccumulatedProject | ProductionWorkbenchProjection> {
  const projection =
    "readAccumulated" in client
      ? await client.readAccumulated(
          requestId,
          {
            workspaceHandle: member.workspaceHandle,
            workspaceId: member.workspaceId,
          },
          signal,
        )
      : (
          await client.send(
            requestId,
            "read_projection",
            { workspaceHandle: member.workspaceHandle },
            signal,
          )
        ).projection;
  if (
    projection === undefined ||
    projection.workspaceHandle !== member.workspaceHandle ||
    projection.workspaceId !== member.workspaceId ||
    !(
      (projection.schema === PRODUCTION_ACCUMULATED_PROJECT_SCHEMA
        ? projection.workspace?.segments
        : projection.segments
      )?.some((segment) => segment.segmentId === member.memberSegmentId) ||
      (projection.schema === PRODUCTION_ACCUMULATED_PROJECT_SCHEMA &&
        projection.attempts.some(
          (attempt) => attempt.memberSegmentId === member.memberSegmentId,
        ))
    )
  )
    throw new SequenceCoordinatorClientError("cross_authority_response", 500);
  return projection;
}

export async function refreshManagedMemberProject(
  ctx: ShellRuntime,
  managed: ActiveManagedRun,
  requestId: string,
  announceAdded: boolean,
): Promise<ProductionAccumulatedProject> {
  const projectMember = managed.projectMember;
  if (projectMember === undefined)
    throw new SequenceCoordinatorClientError("invalid_response", 500);
  const { session, deps, actions } = ctx;
  const readback = await readManagedMemberProject({
    client: deps.productionActions,
    requestId,
    member: projectMember.member,
  });
  const projection = accumulatedProject(readback);
  if (session.activeManagedRun !== managed || managed.completed)
    return projection;
  const destination = deps.productionDestinations.peek(
    deps.appModeController.activeWorkflow(),
  );
  const destinationMatches =
    destination?.kind === "project" &&
    destination.workspaceHandle === projection.workspaceHandle &&
    destination.workspaceId === projection.workspaceId;
  const current = actions.currentProductionProjection();
  const visibleMatches =
    current === undefined ||
    (current.workspaceHandle === projection.workspaceHandle &&
      current.workspaceId === projection.workspaceId);
  const viewUpdated = destinationMatches && visibleMatches;
  // IMPORTANT: an old-project read can finish after New project or a workflow switch. Mutating
  // proposal bindings before this ownership check invalidates the newer project's exact source.
  if (viewUpdated)
    reconcileManagedProposalProject(
      deps.productionProposalDispatcher,
      accumulatedProject(projectMember.projection).workspace,
      projection.workspace,
      projectMember.admission.target,
      projectMember.proposalSource,
    );
  managed.projectMember = Object.freeze({
    ...projectMember,
    projection,
  });
  if (viewUpdated) {
    session.productionSessionHandle = projection.workspaceHandle;
    actions.writeProductionSessionHandle(projection.workspaceHandle);
    session.productionState =
      projection.workspace === null
        ? { status: "empty", project: projection }
        : { status: "ready", projection: projection.workspace };
  }
  if (announceAdded) {
    const segment = projection.workspace?.segments.find(
      (candidate) =>
        candidate.segmentId === projectMember.member.memberSegmentId,
    );
    if (segment === undefined)
      throw new SequenceCoordinatorClientError("cross_authority_response", 500);
    session.productionAccumulationNotice = Object.freeze({
      kind: "added" as const,
      projectOrdinal:
        destination?.kind === "project"
          ? destination.ordinal
          : projectMember.admission.destination.ordinal,
      segmentOrdinal: segment.ordinal,
      viewUpdated,
    });
  }
  return projection;
}

export function installManagedMemberProject(
  ctx: ShellRuntime,
  bootstrap: ManagedBootstrapProjection,
  projectValue: ProductionAccumulatedProject | ProductionWorkbenchProjection,
  admission: ManagedProjectDestinationAdmission,
  committedWorkflow: object,
): Readonly<{
  viewUpdated: boolean;
  proposalSource?: ProductionProposalSourceClaim;
}> {
  const project = accumulatedProject(projectValue);
  const { session, deps, actions } = ctx;
  const priorProduction = actions.currentProductionProjection();
  session.workspaceState = reduceWorkspaceState(session.workspaceState, {
    type: "host_execution",
    projection: bootstrap.workspace,
  });
  session.transactionTransparency = bootstrap.transactionTransparency;
  // IMPORTANT: host replace commits a new workflow object after admission. Move only the exact
  // captured destination; a later New project must keep its newer destination and visible view.
  const destinationStillCurrent =
    deps.productionDestinations.commitCapturedProject(
      admission.workflow,
      committedWorkflow,
      admission.destination,
      project,
    );
  const activeDestination = deps.productionDestinations.peek(
    deps.appModeController.activeWorkflow(),
  );
  const visible = actions.currentProductionProjection();
  // IMPORTANT: an old callback may retain its own workflow mapping after the user switches.
  // Gate proposal and Context mutations on the active, visible project or it clears another Context.
  const viewUpdated =
    destinationStillCurrent &&
    activeDestination?.kind === "project" &&
    activeDestination.workspaceHandle === project.workspaceHandle &&
    activeDestination.workspaceId === project.workspaceId &&
    (visible === undefined ||
      (visible.workspaceHandle === project.workspaceHandle &&
        visible.workspaceId === project.workspaceId));

  const production = project.workspace;
  let proposalSource: ProductionProposalSourceClaim | undefined;
  if (viewUpdated) {
    if (admission.target === null) deps.productionProposalDispatcher.resetAll();
    if (bootstrap.semanticProposalReview === undefined)
      deps.productionProposalDispatcher.clearCurrentSource();
    else
      deps.productionProposalDispatcher.observe(
        {
          workspace_id: bootstrap.workspace.workspace_id,
          report_revision: bootstrap.workspace.report_revision,
          report_fingerprint: bootstrap.workspace.report_fingerprint,
        },
        bootstrap.semanticProposalReview,
      );
    proposalSource = deps.productionProposalDispatcher.captureCurrent();
    // IMPORTANT: managed pending attempts do not own proposal rows. Reconcile only the exact
    // committed project transition, preserving unrelated sources until that member is committed.
    reconcileManagedProposalProject(
      deps.productionProposalDispatcher,
      admission.target === null ? null : (priorProduction ?? null),
      production,
      admission.target,
      proposalSource,
    );
  }
  actions.rememberAcceptedProjectionPromptId(
    bootstrap.projection.correlation.prompt_id,
  );
  session.acceptedGenerationSequence = undefined;

  const activeManagedRun = session.activeManagedRun;
  if (
    proposalSource !== undefined &&
    activeManagedRun?.projectMember?.admission === admission
  )
    activeManagedRun.projectMember = Object.freeze({
      ...activeManagedRun.projectMember,
      proposalSource,
    });

  const priorBinding = session.productionContextBinding;
  if (viewUpdated && admission.target === null)
    session.productionContextBinding = Object.freeze({
      productionWorkspaceHandle: project.workspaceHandle,
      productionWorkspaceId: project.workspaceId,
      contextWorkspaceHandle: bootstrap.workspace.workspace_id,
    });
  else if (
    viewUpdated &&
    (priorBinding?.productionWorkspaceHandle !== project.workspaceHandle ||
      priorBinding?.productionWorkspaceId !== project.workspaceId)
  )
    session.productionContextBinding = undefined;

  if (viewUpdated) {
    session.productionSessionHandle = project.workspaceHandle;
    actions.writeProductionSessionHandle(project.workspaceHandle);
    session.productionState =
      production === null
        ? { status: "empty", project }
        : { status: "ready", projection: production };
  }
  deps.pageRegistry.register({ id: "production" });
  actions.renderCurrent();
  return Object.freeze({
    viewUpdated,
    ...(proposalSource === undefined ? {} : { proposalSource }),
  });
}

export function memberAuthorityMatchesDestination(
  member: CoordinatorProductionMemberAuthority,
  target: ProductionDestinationTarget,
): boolean {
  return (
    target === null ||
    (member.workspaceHandle === target.workspaceHandle &&
      member.workspaceId === target.workspaceId &&
      (target.segmentId === null ||
        member.memberSegmentId === target.segmentId))
  );
}

export function applyManagedCoordinatorAuthority(
  ctx: ShellRuntime,
  managed: ActiveManagedRun,
  result: SequenceCoordinatorResult,
): void {
  const { session, actions } = ctx;
  if (
    session.activeManagedRun !== managed ||
    !actions.isCurrentAppModeRun(managed.run)
  )
    return;
  if (managed.projectMember !== undefined) {
    const expected = managed.projectMember.member;
    // CRITICAL: a compact member may update only the exact project member captured at prepare.
    // Installing another member's response would make Retry overwrite an unrelated segment.
    if (
      result.schema !== MANAGED_MEMBER_COORDINATOR_RESPONSE_SCHEMA ||
      result.productionMemberAuthority.workspaceHandle !==
        expected.workspaceHandle ||
      result.productionMemberAuthority.workspaceId !== expected.workspaceId ||
      result.productionMemberAuthority.memberSegmentId !==
        expected.memberSegmentId
    )
      throw new SequenceCoordinatorClientError("cross_authority_response", 500);
    managed.authority = result;
    session.acceptedGenerationSequence = undefined;
    return;
  }
  // CRITICAL: legacy bootstrap installs a full Production projection. A serial child's compact
  // owner reference must fail before any session authority is changed.
  if (result.production === null)
    throw new SequenceCoordinatorClientError("invalid_response", 500);
  managed.authority = result;
  session.acceptedGenerationSequence = result.sequence;
  session.productionSessionHandle = result.production.workspaceHandle;
  actions.writeProductionSessionHandle(session.productionSessionHandle);
  session.productionState = {
    status: "ready",
    projection: result.production,
  };
  actions.nleSyncOwnerRenewal();
}

export function buildMemberGenerationBinding({
  project,
  production: legacyProduction,
  member,
  sequence,
  workspace,
  request,
}: Readonly<{
  project?: ProductionAccumulatedProject;
  production?: ProductionWorkbenchProjection;
  member: CoordinatorProductionMemberAuthority;
  sequence: GenerationSequenceProjection;
  workspace: ProductionGenerationWorkspaceAuthority;
  request: ProductionGenerationRequest;
}>): GenerationSequenceBinding | undefined {
  const command =
    sequence.eligible_commands.length === 1
      ? sequence.eligible_commands[0]
      : undefined;
  const production = project?.workspace ?? legacyProduction;
  const segment = production?.segments.find(
    (candidate) => candidate.segmentId === member.memberSegmentId,
  );
  const authority: ManagedProductionMemberAuthority = {
    ...member,
    taskMode: workspace.taskMode,
    effectiveDurationMilliseconds: workspace.effectiveDurationMilliseconds,
    frameCount: workspace.frameCount,
  };
  const existing = request.options?.useExisting === true;
  // IMPORTANT: member sequences use a private one-job workspace whose ordinal restarts at one,
  // while the visible accumulating project gives the same segment its project ordinal. Join them
  // through the compact member segment id; equating the ordinals rejects every appended Start.
  if (
    command === undefined ||
    (project?.workspaceHandle ?? production?.workspaceHandle) !==
      member.workspaceHandle ||
    (project?.workspaceId ?? production?.workspaceId) !== member.workspaceId ||
    !(
      segment !== undefined ||
      project?.attempts.some(
        (attempt) => attempt.memberSegmentId === member.memberSegmentId,
      ) === true
    ) ||
    (segment !== undefined &&
      production !== undefined &&
      !matchesAdmittedMemberProduction(production, authority)) ||
    command.segment_id !== member.memberSegmentId ||
    (segment !== undefined && command.task_mode !== segment.taskMode) ||
    (segment !== undefined &&
      command.duration.duration_milliseconds !==
        segment.duration.requestedMilliseconds) ||
    (segment !== undefined &&
      command.duration.frame_count !== segment.duration.frameCount) ||
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
