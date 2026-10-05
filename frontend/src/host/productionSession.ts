// Production session: Production intents, workspace ensure, media preview, proposal review and
// the opaque Production session-storage handle (M23-28 split of entry.tsx).

import type {
  ProductionGenerationControl,
  ProductionIntent,
} from "../components/ProductionWorkbench";
import type { SemanticProposalReviewRequest } from "../components/SemanticProposalReview";
import type { GenerationSequenceProjection } from "../contracts/generationSequenceCodec";
import {
  encodeProductionAction,
  type ProductionAction,
  type ProductionActionInput,
  type ProductionWorkbenchProjection,
} from "../contracts/productionWorkbenchCodec";
import { ProductionClientError } from "./productionActions";
import {
  buildProductionGenerationBinding,
  productionGenerationKey,
} from "./productionGeneration";
import { ProductionMediaPreviewError } from "./productionMediaPreview";
import {
  type ShellRuntime,
  type ProductionEnsureAdmission,
} from "../lifecycle/shellSession";

export function createProductionSession(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;

  const PRODUCTION_SESSION_HANDLE_KEY =
    "h3.context.production.workspace_handle.v1";

  const productionWorkspaceHandle = /^pw_[A-Za-z0-9_-]{32,96}$/;

  function readProductionSessionHandle(): string | undefined {
    try {
      const value = window.sessionStorage.getItem(
        PRODUCTION_SESSION_HANDLE_KEY,
      );
      return value !== null && productionWorkspaceHandle.test(value)
        ? value
        : undefined;
    } catch {
      return undefined;
    }
  }

  function writeProductionSessionHandle(value: string): void {
    if (!productionWorkspaceHandle.test(value)) return;
    try {
      window.sessionStorage.setItem(PRODUCTION_SESSION_HANDLE_KEY, value);
    } catch {
      // Session persistence is optional; the process-local registry remains canonical.
    }
  }

  function clearProductionSessionHandle(): void {
    session.productionContextBinding = undefined;
    try {
      window.sessionStorage.removeItem(PRODUCTION_SESSION_HANDLE_KEY);
    } catch {
      // Session persistence is optional; clearing live in-memory ownership still succeeds.
    }
  }

  function currentProductionProjection():
    ProductionWorkbenchProjection | undefined {
    return "projection" in session.productionState
      ? session.productionState.projection
      : undefined;
  }

  function activeProductionWorkflow(): object | undefined {
    return deps.appModeController.activeWorkflow();
  }

  function productionEnsureAdmission(
    contextWorkspaceHandle: string,
  ): ProductionEnsureAdmission {
    // IMPORTANT: keep remounts on the same ensure identity; view generation only quarantines late settlement.
    const appModeRun = actions.currentAppModeRun();
    const destination = deps.productionDestinations.peek(
      activeProductionWorkflow(),
    );
    const identity = JSON.stringify({
      contextWorkspaceHandle,
      appModeRun,
      destination,
    });
    return Object.freeze({
      contextWorkspaceHandle,
      identity,
      key: JSON.stringify({
        identity,
        viewGeneration: session.viewFocusGeneration,
      }),
      appModeRun,
      viewGeneration: session.viewFocusGeneration,
    });
  }

  function rememberProductionEnsureIdentity(identity: string): void {
    session.productionEnsureAttemptedIdentities.add(identity);
    while (session.productionEnsureAttemptedIdentities.size > 64) {
      const oldest = session.productionEnsureAttemptedIdentities
        .values()
        .next().value;
      if (typeof oldest !== "string") break;
      session.productionEnsureAttemptedIdentities.delete(oldest);
    }
  }

  function clearProductionEnsureIdentity(identity: string): void {
    session.productionEnsureAttemptedIdentities.delete(identity);
    session.productionEnsureClosedIdentities.delete(identity);
  }

  function currentProductionEnsureIs(
    admission: ProductionEnsureAdmission,
  ): boolean {
    return (
      session.productionEnsureActive === admission &&
      deps.pageRegistry.getSnapshot().selected === "production" &&
      actions.currentProjection()?.workspace_id ===
        admission.contextWorkspaceHandle &&
      actions.currentAppModeRun() === admission.appModeRun &&
      session.viewFocusGeneration === admission.viewGeneration
    );
  }

  function exactProductionGeneration(jobId: string):
    | Readonly<{
        sequence: GenerationSequenceProjection;
        binding: NonNullable<
          ReturnType<typeof buildProductionGenerationBinding>
        >;
        key: string;
      }>
    | undefined {
    const projection = currentProductionProjection();
    const sequence = session.acceptedGenerationSequence;
    const workspace = actions.currentProjection();
    const request = session.lastAppModeRequest;
    if (
      projection === undefined ||
      sequence === undefined ||
      workspace === undefined ||
      request === undefined
    )
      return undefined;
    const binding = buildProductionGenerationBinding({
      production: projection,
      sequence,
      jobId,
      workspace: {
        reportId: workspace.report_id,
        taskMode: workspace.task_mode,
        requestedDurationMilliseconds:
          workspace.capabilities.output_duration.requested_seconds * 1000,
        effectiveDurationMilliseconds:
          workspace.planning.timeline.effective_duration_milliseconds,
        frameCount: workspace.planning.timeline.effective_frame_count,
        referenceIds: workspace.reference_candidates.map(
          (item) => item.asset_id,
        ),
      },
      request,
    });
    if (binding === undefined) return undefined;
    return Object.freeze({
      sequence,
      binding,
      key: productionGenerationKey(sequence, jobId),
    });
  }

  function currentProductionGenerationControls(): readonly ProductionGenerationControl[] {
    const sequence = session.acceptedGenerationSequence;
    if (sequence === undefined) return [];
    return sequence.eligible_commands.flatMap((command) => {
      const exact = exactProductionGeneration(command.job_id);
      return exact === undefined
        ? []
        : [
            Object.freeze({
              key: exact.key,
              jobId: command.job_id,
              ordinal: command.ordinal,
              disposition: deps.productionGenerationController.disposition(
                exact.key,
              ),
            }),
          ];
    });
  }

  function nextProductionRequestId(action: ProductionAction): string {
    session.productionRequestSequence += 1;
    // CRITICAL: the host keeps one replay ledger for every client, keyed by this id. A per-page
    // counter alone repeats `…create_workspace_from_context.1` in every tab and browser, and the
    // second session's first create is refused as a request-id conflict while the first
    // session's workspace (or its tombstone) is retained.
    return `production.${actions.browserRequestSessionToken}.${action}.${session.productionRequestSequence}`;
  }

  function claimProductionRequest(
    action: ProductionAction,
    input: ProductionActionInput,
  ): Readonly<{ key: string; requestId: string }> {
    const key = JSON.stringify(
      encodeProductionAction("production.retry.key", action, input),
    );
    if (session.retryableProductionRequest?.key === key)
      return session.retryableProductionRequest;
    session.retryableProductionRequest = Object.freeze({
      key,
      requestId: nextProductionRequestId(action),
    });
    return session.retryableProductionRequest;
  }

  async function runProductionIntent(
    intent: ProductionIntent,
    requestedEnsure?: ProductionEnsureAdmission,
  ): Promise<void> {
    if (
      session.productionState.status === "loading" ||
      session.productionState.status === "pending"
    )
      return;
    const currentContextWorkspaceHandle =
      actions.currentProjection()?.workspace_id;
    if (
      intent.action === "create_workspace_from_context" &&
      deps.productionDestinations.peek(activeProductionWorkflow())?.kind ===
        "unavailable"
    )
      deps.productionDestinations.startNew(activeProductionWorkflow());
    let ensureAdmission = requestedEnsure;
    if (
      ensureAdmission === undefined &&
      intent.action === "create_workspace_from_context" &&
      currentContextWorkspaceHandle !== undefined &&
      (session.productionState.status === "error" ||
        session.productionState.status === "gone")
    ) {
      ensureAdmission = productionEnsureAdmission(
        currentContextWorkspaceHandle,
      );
      clearProductionEnsureIdentity(ensureAdmission.identity);
      rememberProductionEnsureIdentity(ensureAdmission.identity);
    }
    if (ensureAdmission !== undefined)
      session.productionEnsureActive = ensureAdmission;
    if (intent.action !== "submit_generation_job")
      closeProductionMediaPreview(false);
    const contextWorkspaceHandle = currentContextWorkspaceHandle;
    const projection = currentProductionProjection();
    if (intent.action === "submit_generation_job") {
      const exact = exactProductionGeneration(intent.jobId);
      if (
        exact === undefined ||
        deps.productionGenerationController.disposition(exact.key) !==
          "available"
      )
        return;
      const mountedContainer = session.container;
      const mountedGeneration = session.viewFocusGeneration;
      const mountedPage = deps.pageRegistry.getSnapshot().selected;
      const execution = deps.productionGenerationController.execute(
        exact.key,
        () =>
          deps.generationSequenceDriver.execute(exact.sequence, exact.binding),
      );
      actions.renderCurrent();
      try {
        await execution;
      } finally {
        if (
          session.container === mountedContainer &&
          session.viewFocusGeneration === mountedGeneration &&
          mountedPage === "production" &&
          deps.pageRegistry.getSnapshot().selected === mountedPage &&
          exactProductionGeneration(intent.jobId)?.key === exact.key
        )
          actions.renderCurrent();
      }
      return;
    }
    let input: ProductionActionInput;
    switch (intent.action) {
      case "create_workspace_from_context":
        if (contextWorkspaceHandle === undefined) return;
        input = { contextWorkspaceHandle };
        break;
      case "add_segment_from_context":
        if (projection === undefined || contextWorkspaceHandle === undefined)
          return;
        input = {
          projection,
          contextWorkspaceHandle,
          relation: "independent",
          predecessorSegmentId: null,
        };
        break;
      case "replace_segment_from_context":
        if (projection === undefined || contextWorkspaceHandle === undefined)
          return;
        input = {
          projection,
          contextWorkspaceHandle,
          segmentId: intent.segmentId,
        };
        break;
      case "set_segment_relation":
        if (projection === undefined) return;
        input = {
          projection,
          segmentId: intent.segmentId,
          relation: intent.relation,
          predecessorSegmentId: intent.predecessorSegmentId,
        };
        break;
      case "delete_segment":
        if (projection === undefined) return;
        input = { projection, segmentId: intent.segmentId };
        break;
      case "reorder_segments":
      case "set_selection":
        if (projection === undefined) return;
        input = { projection, segmentIds: intent.segmentIds };
        break;
      case "read_projection":
        if (projection !== undefined) input = { projection };
        else if (session.productionSessionHandle !== undefined)
          input = { workspaceHandle: session.productionSessionHandle };
        else return;
        break;
      case "release_workspace":
      case "assemble_sequence":
      case "cancel_assembly":
      case "retry_assembly":
        if (projection === undefined) return;
        input = { projection };
        break;
    }
    const proposalSource =
      intent.action === "create_workspace_from_context" ||
      intent.action === "add_segment_from_context" ||
      intent.action === "replace_segment_from_context"
        ? deps.productionProposalDispatcher.captureCurrent()
        : undefined;
    const request = claimProductionRequest(intent.action, input);
    const abort = new AbortController();
    session.productionAbort = abort;
    session.productionState =
      projection === undefined
        ? { status: "loading" }
        : { status: "pending", projection };
    actions.renderCurrent();
    try {
      const result = await deps.productionActions.send(
        request.requestId,
        intent.action,
        input,
        abort.signal,
      );
      if (abort.signal.aborted || session.productionAbort !== abort) return;
      if (
        ensureAdmission !== undefined &&
        !currentProductionEnsureIs(ensureAdmission)
      ) {
        // CRITICAL: a quarantined ensure result must still settle the visible
        // loading state, or the page reports a pending action forever.
        session.productionEnsureClosedIdentities.add(ensureAdmission.identity);
        if (session.productionState.status === "loading")
          session.productionState = {
            status: "error",
            reason: "context_changed",
            recovery:
              session.productionSessionHandle !== undefined ? "read" : "create",
          };
        return;
      }
      if (session.retryableProductionRequest === request)
        session.retryableProductionRequest = undefined;
      if (result.status === 204) {
        const releasedHandle = projection?.workspaceHandle;
        deps.productionProposalDispatcher.resetAll();
        session.productionSessionHandle = undefined;
        clearProductionSessionHandle();
        if (releasedHandle !== undefined) {
          deps.productionDestinations.releaseProject(releasedHandle);
          deps.productionDestinations.markUnavailable(releasedHandle);
        }
        // A released workspace leaves no Production owner: the current Context
        // identity becomes ensure-eligible again on the next Production entry.
        const releasedContext = actions.currentProjection()?.workspace_id;
        if (releasedContext !== undefined)
          clearProductionEnsureIdentity(
            productionEnsureAdmission(releasedContext).identity,
          );
        session.productionState = { status: "released" };
        actions.selectPage("context");
      } else if (result.projection !== undefined) {
        // IMPORTANT (M25-36): the Production view and App Mode must share the same
        // workflow owner. Showing a project without binding it here lets the next Start
        // silently create or append somewhere else.
        deps.productionDestinations.bindProject(
          activeProductionWorkflow(),
          result.projection,
        );
        // IMPORTANT: serial child projections replace the visible Context, not this owner's
        // origin. Retain the accepted create selector or later Authoring import gets lineage409.
        if (
          intent.action === "create_workspace_from_context" &&
          result.status === 201
        )
          session.productionContextBinding = Object.freeze({
            productionWorkspaceHandle: result.projection.workspaceHandle,
            productionWorkspaceId: result.projection.workspaceId,
            contextWorkspaceHandle: contextWorkspaceHandle!,
          });
        else if (
          session.productionContextBinding?.productionWorkspaceHandle !==
            result.projection.workspaceHandle ||
          session.productionContextBinding.productionWorkspaceId !==
            result.projection.workspaceId
        )
          session.productionContextBinding = undefined;
        if (result.status === 409 || intent.action === "read_projection")
          deps.productionProposalDispatcher.refreshProjection(
            result.projection,
          );
        else {
          if (intent.action === "create_workspace_from_context")
            deps.productionProposalDispatcher.bind({
              action: "create",
              source: proposalSource,
              after: result.projection,
            });
          else if (
            intent.action === "add_segment_from_context" &&
            projection !== undefined
          )
            deps.productionProposalDispatcher.bind({
              action: "add",
              source: proposalSource,
              before: projection,
              after: result.projection,
            });
          else if (
            intent.action === "replace_segment_from_context" &&
            projection !== undefined
          )
            deps.productionProposalDispatcher.bind({
              action: "replace",
              source: proposalSource,
              before: projection,
              after: result.projection,
              segmentId: intent.segmentId,
            });
          else if (projection !== undefined)
            deps.productionProposalDispatcher.advanceProjection(
              projection,
              result.projection,
            );
        }
        session.productionSessionHandle = result.projection.workspaceHandle;
        writeProductionSessionHandle(session.productionSessionHandle);
        session.productionState = {
          status: result.status === 409 ? "conflict" : "ready",
          projection: result.projection,
        };
        actions.nleNoteProductionExchange(result.projection);
        if (ensureAdmission !== undefined)
          session.productionEnsureClosedIdentities.delete(
            ensureAdmission.identity,
          );
      }
    } catch (error) {
      if (abort.signal.aborted || session.productionAbort !== abort) return;
      if (
        ensureAdmission !== undefined &&
        !currentProductionEnsureIs(ensureAdmission)
      ) {
        session.productionEnsureClosedIdentities.add(ensureAdmission.identity);
        if (session.productionState.status === "loading")
          session.productionState = {
            status: "error",
            reason: "context_changed",
            recovery:
              session.productionSessionHandle !== undefined ? "read" : "create",
          };
        return;
      }
      if (ensureAdmission !== undefined)
        session.productionEnsureClosedIdentities.add(ensureAdmission.identity);
      if (
        error instanceof ProductionClientError &&
        (error.status === 404 || error.status === 410)
      ) {
        const unavailableHandle =
          projection?.workspaceHandle ?? session.productionSessionHandle;
        if (unavailableHandle !== undefined)
          deps.productionDestinations.markUnavailable(unavailableHandle);
        session.productionSessionHandle = undefined;
        deps.productionProposalDispatcher.resetAll();
        clearProductionSessionHandle();
        session.productionState =
          error.status === 410
            ? { status: "gone", reason: error.code, recovery: "create" }
            : { status: "error", reason: error.code, recovery: "create" };
      } else {
        session.productionState = {
          status: "error",
          projection,
          reason:
            error instanceof ProductionClientError
              ? error.code
              : "internal_failure",
          recovery:
            intent.action === "read_projection" &&
            session.productionSessionHandle !== undefined
              ? "read"
              : "create",
        };
      }
    } finally {
      if (session.productionAbort === abort)
        session.productionAbort = undefined;
      if (
        ensureAdmission !== undefined &&
        session.productionEnsureActive === ensureAdmission
      )
        session.productionEnsureActive = undefined;
      actions.renderCurrent();
      actions.nleSyncOwnerRenewal();
    }
  }

  function ensureProductionWorkspace(): void {
    if (deps.pageRegistry.getSnapshot().selected !== "production") return;
    if (currentProductionProjection() !== undefined) return;
    if (
      session.productionState.status === "loading" ||
      session.productionState.status === "empty" ||
      session.productionState.status === "error" ||
      session.productionState.status === "gone"
    )
      return;

    const workflow = activeProductionWorkflow();
    const destination = deps.productionDestinations.peek(workflow);
    if (destination?.kind === "project") {
      session.productionSessionHandle = destination.workspaceHandle;
      session.productionState = { status: "loading" };
      actions.renderCurrent();
      void deps.productionActions
        .readAccumulated(actions.nextProductionRequestId("read_projection"), {
          workspaceHandle: destination.workspaceHandle,
          workspaceId: destination.workspaceId,
        })
        .then((project) => {
          const current = deps.productionDestinations.peek(workflow);
          if (
            current?.kind !== "project" ||
            current.workspaceHandle !== project.workspaceHandle ||
            current.workspaceId !== project.workspaceId
          )
            return;
          session.productionState =
            project.workspace === null
              ? { status: "empty", project }
              : { status: "ready", projection: project.workspace };
          if (project.workspace !== null)
            actions.nleNoteProductionExchange(project.workspace);
        })
        .catch(() => {
          deps.productionDestinations.markUnavailable(
            destination.workspaceHandle,
          );
          session.productionState = {
            status: "gone",
            reason: "workspace_unavailable",
            recovery: "create",
          };
        })
        .finally(() => {
          actions.renderCurrent();
          actions.nleSyncOwnerRenewal();
        });
      return;
    }
    if (destination?.kind === "new" || destination?.kind === "unavailable")
      return;

    if (
      session.productionSessionHandle !== undefined &&
      !deps.productionDestinations.ownedByAnotherWorkflow(
        session.productionSessionHandle,
        workflow,
      )
    ) {
      void runProductionIntent({ action: "read_projection" });
      return;
    }

    const contextWorkspaceHandle = actions.currentProjection()?.workspace_id;
    if (contextWorkspaceHandle === undefined) return;
    const admission = productionEnsureAdmission(contextWorkspaceHandle);
    if (
      session.productionEnsureAttemptedIdentities.has(admission.identity) ||
      session.productionEnsureClosedIdentities.has(admission.identity)
    )
      return;
    rememberProductionEnsureIdentity(admission.identity);
    void runProductionIntent(
      { action: "create_workspace_from_context" },
      admission,
    );
  }

  function closeProductionMediaPreview(render = true): void {
    // CRITICAL: every replace and unmount must use this close path; bypassing it
    // leaves private proxy Blob bytes live behind an unreclaimed object URL.
    session.productionMediaPreviewAbort?.abort();
    session.productionMediaPreviewAbort = undefined;
    if (session.productionMediaPreview.status === "ready")
      URL.revokeObjectURL(session.productionMediaPreview.url);
    session.productionMediaPreview = { status: "closed" };
    if (render) actions.renderCurrent();
  }

  async function openProductionMediaPreview(
    outputHandle: string,
  ): Promise<void> {
    const projection = currentProductionProjection();
    const output = projection?.outputs.find(
      (candidate) =>
        candidate.outputHandle === outputHandle && candidate.preview,
    );
    if (
      projection === undefined ||
      output === undefined ||
      !projection.allowedActions.includes("preview_output")
    )
      return;
    closeProductionMediaPreview(false);
    const abort = new AbortController();
    session.productionMediaPreviewAbort = abort;
    session.productionMediaPreview = {
      status: "loading",
      workspaceRevision: projection.workspaceRevision,
      outputHandle,
    };
    actions.renderCurrent();
    try {
      const blob = await deps.productionMediaPreviewClient.open(
        projection,
        output,
        abort.signal,
      );
      const current = currentProductionProjection();
      const currentOutput = current?.outputs.find(
        (candidate) =>
          candidate.outputHandle === outputHandle && candidate.preview,
      );
      if (
        abort.signal.aborted ||
        session.productionMediaPreviewAbort !== abort ||
        current?.workspaceHandle !== projection.workspaceHandle ||
        current.workspaceRevision !== projection.workspaceRevision ||
        current.workspaceFingerprint !== projection.workspaceFingerprint ||
        currentOutput === undefined ||
        !current.allowedActions.includes("preview_output")
      )
        return;
      const url = URL.createObjectURL(blob);
      session.productionMediaPreview = {
        status: "ready",
        workspaceRevision: projection.workspaceRevision,
        outputHandle,
        url,
      };
    } catch (error) {
      if (abort.signal.aborted || session.productionMediaPreviewAbort !== abort)
        return;
      const reason =
        error instanceof ProductionMediaPreviewError
          ? error.disposition
          : "failed";
      session.productionMediaPreview = {
        status: "error",
        workspaceRevision: projection.workspaceRevision,
        outputHandle,
        reason,
      };
      if (reason === "stale")
        void runProductionIntent({ action: "read_projection" });
    } finally {
      if (session.productionMediaPreviewAbort === abort)
        session.productionMediaPreviewAbort = undefined;
      actions.renderCurrent();
    }
  }

  function closeSemanticProposalReview(): void {
    deps.productionProposalDispatcher.closeContext();
  }

  async function runSemanticProposalReview(
    request: { action: "proposal_read" } | SemanticProposalReviewRequest,
  ): Promise<void> {
    await deps.productionProposalDispatcher.runContext(
      request,
      currentProductionProjection(),
    );
  }

  // IMPORTANT: the handle bootstrap runs after every declaration above so the storage
  // key constant is initialized; an earlier read would hit its temporal dead zone and the
  // guarded reader would report "no handle" instead of the retained one.
  session.productionSessionHandle = readProductionSessionHandle();

  return {
    claimProductionRequest,
    clearProductionEnsureIdentity,
    clearProductionSessionHandle,
    closeProductionMediaPreview,
    closeSemanticProposalReview,
    currentProductionEnsureIs,
    currentProductionGenerationControls,
    currentProductionProjection,
    ensureProductionWorkspace,
    exactProductionGeneration,
    nextProductionRequestId,
    openProductionMediaPreview,
    productionEnsureAdmission,
    readProductionSessionHandle,
    rememberProductionEnsureIdentity,
    runProductionIntent,
    runSemanticProposalReview,
    writeProductionSessionHandle,
  };
}
