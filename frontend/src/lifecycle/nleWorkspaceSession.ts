// M25-16 NLE workspace session: the overlay lifecycle, the explicit Production-output import,
// the M26 planning/readiness/sequence/assembly projection and the B1 leave/return flow, all
// held in the one shell session and dispatched as named backend actions.
//
// Nothing here compiles prompts, decides eligibility, computes storyboards, schedules queue
// work or reconstructs media. Every mutation is one existing accepted action; every read is a
// bounded, effect-free projection read. Mount, remount, navigation, polling and recovery never
// plan, import, qualify or queue on their own.

import type { AuthoringIntent } from "../state/authoringViewState";
import type { ProductionIntent } from "../components/ProductionWorkbench";
import { deriveNleSurfaceCapability } from "../contracts/nleSurfaceCapability";
import type { ManagedReadinessSelection } from "../contracts/managedQualificationCodec";
import { readActiveWorkflow } from "../host/canvasOwnedWrite";
import { probeFetchApi, serializeGraph } from "../host/hostSeams";
import {
  buildQualifiedManagedStartIntent,
  planStartHoldsResolvedByQualification,
} from "../host/managedQualificationActions";
import { createProductionManagedChildResolver } from "../host/productionManagedChildResolver";
import {
  observeOwnedGraph,
  type OwnedGraphReference,
} from "../host/ownedGraphIdentity";
import { isSidebarFocusKey } from "../navigation/focusRegistry";
import {
  ACCEPTED_RUNTIME_QUALIFICATION,
  RUNTIME_QUALIFICATION_AUTHORITY,
} from "../runtime/acceptedRuntimeQualification";
import {
  evaluateMediaCapabilities,
  observeBrowserMediaCapabilities,
} from "../runtime/mediaCapabilities";
import {
  clampOverlayBounds,
  overlayDefaultBounds,
  overlayPaneOrDefault,
  type OverlayBounds,
  type OverlayInternalPane,
  type OverlayViewport,
} from "../runtime/nleOverlayGeometry";
import { normalizeLayout, type NleLayout } from "../runtime/nleLayoutGeometry";
import {
  initialNleSequenceState,
  initialNleSurfaceState,
  planningIsStale,
  readinessIsStale,
  type NleCloseReason,
  type NleSequenceState,
  type NleSequenceUiState,
  type NleWorkspaceState,
} from "../state/nleWorkspaceState";
import { UNSCOPED } from "../state/sidebarRetention";
import { createNleWorkspaceImport } from "./nleWorkspaceImport";
import { createNleOwnerRenewal } from "./nleOwnerRenewal";
import { createNleWorkspacePlanning } from "./nleWorkspacePlanning";
import { createRenderCapabilityRead } from "./nleRenderCapabilityRead";
import type { ShellRuntime } from "./shellSession";

/**
 * The reference used before any managed child has written an owned projection. The accepted
 * child resolver captures the current canvas against this reference (an empty owned set), which
 * is exactly how the accepted M26-04 host qualification seeded its first child.
 */
export const UNBOUND_OWNED_GRAPH_REFERENCE: OwnedGraphReference = Object.freeze(
  {
    nodeIds: Object.freeze([]),
    linkIds: Object.freeze([]),
    anchorNodeId: "unbound",
    authoredWidgetNodeIds: Object.freeze([]),
  },
);

export const NLE_SEQUENCE_POLL_INTERVAL_MS = 2_000;
export const NLE_LAUNCHER_FOCUS_KEY = "nle-open-overlay" as const;
export const NLE_CLOSE_FOCUS_KEY = "nle-close-overlay" as const;

type SequenceUiInputs = Readonly<{
  attached: boolean;
  parentState: string | null;
  activeSegmentId: string | null;
  activeQueuePromptId: string | null;
  failure: string | null;
}>;

function deriveSequenceUi(
  previous: NleSequenceUiState,
  snapshot: SequenceUiInputs,
): NleSequenceUiState {
  if (snapshot.parentState === null)
    return previous === "starting" ? "starting" : "idle";
  if (
    snapshot.parentState === "succeeded" ||
    snapshot.parentState === "cancelled"
  )
    return "finished";
  if (snapshot.parentState === "paused_failure") return "failed";
  if (snapshot.parentState === "paused_unknown_ownership")
    return "paused_unknown_ownership";
  if (snapshot.parentState === "paused_client_absent")
    return previous === "resume_ready" ||
      previous === "current_segment_completed" ||
      previous === "current_segment_still_owned"
      ? previous
      : "safe_to_leave";
  if (snapshot.attached) {
    if (previous === "detaching") return "detaching";
    return snapshot.activeQueuePromptId !== null
      ? "detach_ready"
      : "attached_current_child";
  }
  return previous === "reattach_reconciling" ||
    previous === "current_segment_completed" ||
    previous === "current_segment_still_owned" ||
    previous === "resume_ready" ||
    previous === "recovery_unavailable_or_expired"
    ? previous
    : "safe_to_leave";
}

export function createNleWorkspaceSession(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let pollAbort: AbortController | undefined;
  let requestCounter = 0;
  let viewIntentEpoch = 0;
  // One random token per browser session keeps mutation request IDs unique across reloads
  // without persisting anything; the counter keeps them unique within the session.
  const sessionToken = (() => {
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);
    return [...bytes]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  })();

  function nextRequestId(prefix: string): string {
    requestCounter += 1;
    return `${prefix}.${sessionToken}.${requestCounter}`;
  }

  function authoringHistoryIsMissing(): boolean {
    const authoring = session.authoringState;
    if (authoring.status !== "ready") return false;
    // CRITICAL: an empty V2 authoring history is initialized even when it has no render snapshot.
    // Checking only the retired V1 slot reinitializes accepted V2 state on every editor open.
    const v1 =
      "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
    const v2 =
      "timelineHistoryV2" in authoring
        ? authoring.timelineHistoryV2
        : undefined;
    return v1 === undefined && v2 === undefined;
  }

  function state(): NleWorkspaceState {
    return session.nleWorkspace;
  }

  function patch(next: Partial<NleWorkspaceState>): void {
    const previousSequenceUi = session.nleWorkspace.sequence.ui;
    session.nleWorkspace = Object.freeze({ ...session.nleWorkspace, ...next });
    actions.renderCurrent();
    // IMPORTANT (B-M2522-ASSEMBLY-01): the serial runner never touches the Production projection
    // this pane renders, so on the transition into `finished` read it once. Without this read the
    // pre-sequence projection stays on screen and Assemble stays disabled although the backend
    // already allows it. Trigger on the transition only, never on every finished patch.
    if (
      next.sequence !== undefined &&
      next.sequence.ui === "finished" &&
      previousSequenceUi !== "finished"
    )
      void Promise.resolve(
        actions.runProductionIntent({ action: "read_projection" }),
      ).catch(() => undefined);
  }

  function productionProjection() {
    return "projection" in session.productionState
      ? session.productionState.projection
      : undefined;
  }

  const ownerRenewal = createNleOwnerRenewal({
    currentOwner: () => {
      if (
        session.container === undefined ||
        document.visibilityState !== "visible" ||
        deps.pageRegistry.getSnapshot().selected !== "production"
      )
        return null;
      if (session.productionState.status !== "ready") return null;
      const production = productionProjection();
      if (production === undefined) return null;
      const editor =
        session.authoringState.status === "ready" &&
        "projection" in session.authoringState
          ? session.authoringState.projection
          : undefined;
      return {
        productionHandle: production.workspaceHandle,
        productionId: production.workspaceId,
        editorHandle: editor?.workspaceHandle ?? null,
      };
    },
    // CRITICAL: a renewal is a touch, not a user intent. Routing it through runProductionIntent
    // or runAuthoringIntent marks the owner pending, which silently drops the user's own clicks
    // and timeline commands for the read's duration, takes over the abort slot and closes an
    // open preview every interval. The reply is discarded; visible state has its own reads.
    renewProduction: async () => {
      const production = productionProjection();
      if (production === undefined) return;
      await deps.productionActions.send(
        actions.nextProductionRequestId("read_projection"),
        "read_projection",
        { projection: production },
      );
    },
    renewEditor: async () => {
      const editor =
        "projection" in session.authoringState
          ? session.authoringState.projection
          : undefined;
      if (editor === undefined) return;
      await deps.authoringActions.send(
        actions.nextAuthoringRequestId(),
        "read_projection",
        { workspace_handle: editor.workspaceHandle },
        editor.workspaceHandle,
      );
    },
  });

  function nleSyncOwnerRenewal(reconcile = false): void {
    ownerRenewal.sync(reconcile);
  }

  function nleStopOwnerRenewal(): void {
    ownerRenewal.stop();
  }

  function nleNoteProductionExchange(
    projection: Readonly<{ workspaceHandle: string; workspaceId: string }>,
  ): void {
    ownerRenewal.noteProductionExchange(
      projection.workspaceHandle,
      projection.workspaceId,
    );
  }

  function contextProjection() {
    return session.workspaceState.status === "awaiting"
      ? undefined
      : session.workspaceState.projection;
  }

  function currentViewport(): OverlayViewport {
    const visual = globalThis.visualViewport;
    return {
      width: visual?.width ?? globalThis.innerWidth ?? 0,
      height: visual?.height ?? globalThis.innerHeight ?? 0,
    };
  }

  // ------------------------------------------------------------------ overlay lifecycle

  function nleSurfaceCapability() {
    const runtime = evaluateMediaCapabilities(
      observeBrowserMediaCapabilities(),
      ACCEPTED_RUNTIME_QUALIFICATION,
      RUNTIME_QUALIFICATION_AUTHORITY,
    );
    return deriveNleSurfaceCapability({
      runtime,
      documentAvailable:
        typeof document !== "undefined" && document.body !== null,
      mountAdmitted: session.container?.isConnected === true,
      focusReturnToken: NLE_LAUNCHER_FOCUS_KEY,
      viewportBounds: overlayDefaultBounds(currentViewport()),
    });
  }

  function nleRuntimeDisposition() {
    return evaluateMediaCapabilities(
      observeBrowserMediaCapabilities(),
      ACCEPTED_RUNTIME_QUALIFICATION,
      RUNTIME_QUALIFICATION_AUTHORITY,
    );
  }

  /** The launcher is enabled only for an accepted `overlay_v1` capability. */
  function nleLauncherCapability() {
    const capability = nleSurfaceCapability();
    return capability.capability === "overlay_v1" ? capability : null;
  }

  function nleOpenOverlay(): void {
    const surface = state().surface;
    // Opening is busy and cannot create a second request/root; expanded/closing repeat opens
    // are idempotent no-ops.
    if (
      surface.status !== "compact_ready" &&
      surface.status !== "compact_unsupported"
    )
      return;
    const capability = nleSurfaceCapability();
    if (capability.capability !== "overlay_v1") {
      patch({
        surface: Object.freeze({
          ...surface,
          status: "compact_unsupported",
          capability,
          lastCloseReason: "capability_or_mount_failure",
        }),
      });
      return;
    }
    const generation = surface.generation + 1;
    // M25-21: only this explicit open applies retained geometry, clamped to today's viewport.
    const retained = session.retention.restore("nle.overlay", UNSCOPED).value;
    patch({
      surface: Object.freeze({
        ...surface,
        status: "opening",
        capability,
        bounds:
          retained?.bounds === undefined
            ? overlayDefaultBounds(currentViewport())
            : clampOverlayBounds(currentViewport(), retained.bounds),
        pane: overlayPaneOrDefault(retained?.pane),
        layout: normalizeLayout(retained?.layout),
        lastCloseReason: null,
        generation,
      }),
    });
    // A newly created compact workspace has no NLE history projection yet.
    // Initialize it through the accepted CAS route on this explicit open;
    // reading an uninitialized history returns 422 and leaves the editor empty.
    if (authoringHistoryIsMissing()) {
      void actions.runAuthoringIntent({
        action: "initialize_timeline_history",
      });
    }
  }

  /** Called by the overlay root after its owned dialog is connected and focused. */
  function nleOverlayMounted(generation: number): void {
    const surface = state().surface;
    if (surface.status !== "opening" || surface.generation !== generation)
      return;
    patch({ surface: Object.freeze({ ...surface, status: "expanded" }) });
    void nleReadRenderCapability(generation);
  }

  const capabilityRead = createRenderCapabilityRead({
    read: () => deps.authoringOutputCapabilityClient.read(),
    state,
    patch,
  });
  const nleReadRenderCapability = capabilityRead.start;
  const releaseCapabilityRead = capabilityRead.release;

  /** Called by the overlay root when its owned mount failed; releases and records the failure. */
  function nleOverlayMountFailed(generation: number): void {
    const surface = state().surface;
    if (surface.generation !== generation) return;
    if (surface.status !== "opening" && surface.status !== "expanded") return;
    stopSequencePolling();
    releaseCapabilityRead();
    patch({
      surface: Object.freeze({
        ...surface,
        status: "compact_unsupported",
        capability:
          surface.capability === null
            ? null
            : Object.freeze({
                ...surface.capability,
                capability: "unsupported" as const,
                failureDisposition: "mount_admission_refused" as const,
              }),
        lastCloseReason: "capability_or_mount_failure",
      }),
    });
  }

  function nleCloseOverlay(reason: NleCloseReason): void {
    viewIntentEpoch += 1;
    const surface = state().surface;
    if (surface.status !== "opening" && surface.status !== "expanded") return;
    stopSequencePolling();
    releaseCapabilityRead();
    actions.mediaRuntimeLeaveContext("overlay");
    // Close cancels only unaccepted local pointer/transport work; accepted commands stay.
    patch({
      surface: Object.freeze({
        ...surface,
        status: "closing",
        lastCloseReason: reason,
      }),
    });
  }

  /** Called by the overlay root after teardown completed; returns to the compact surface. */
  function nleOverlayReleased(generation: number): void {
    const surface = state().surface;
    if (surface.generation !== generation || surface.status !== "closing")
      return;
    patch({
      surface: Object.freeze({
        ...surface,
        status: "compact_ready",
        bounds: Object.freeze({ width: 0, height: 0 }),
        pane: "assets",
      }),
    });
  }

  /** Full view destroy: geometry and pane reset; the close reason is recorded once. */
  function nleDisposeOverlay(): void {
    viewIntentEpoch += 1;
    stopSequencePolling();
    releaseCapabilityRead();
    planningSlice.abortAtViewDestroy();
    importSlice.abortAtViewDestroy();
    const surface = state().surface;
    const wasOpen =
      surface.status === "opening" ||
      surface.status === "expanded" ||
      surface.status === "closing";
    session.nleWorkspace = Object.freeze({
      ...session.nleWorkspace,
      surface: Object.freeze({
        ...initialNleSurfaceState,
        generation: surface.generation,
        lastCloseReason: wasOpen ? "view_destroy" : surface.lastCloseReason,
      }),
    });
  }

  function nleResizeOverlay(requested: OverlayBounds): void {
    const surface = state().surface;
    if (surface.status !== "expanded") return;
    const bounds = clampOverlayBounds(currentViewport(), requested);
    if (
      bounds.width === surface.bounds.width &&
      bounds.height === surface.bounds.height
    )
      return;
    retainOverlay({ bounds, pane: surface.pane, layout: surface.layout });
    patch({ surface: Object.freeze({ ...surface, bounds }) });
  }

  function nleSelectPane(pane: OverlayInternalPane): void {
    const surface = state().surface;
    if (surface.status !== "expanded" || surface.pane === pane) return;
    retainOverlay({ bounds: surface.bounds, pane, layout: surface.layout });
    patch({ surface: Object.freeze({ ...surface, pane }) });
  }

  /** M25-44: a splitter move; a local view change that issues no command. */
  function nleSetLayout(requested: NleLayout): void {
    const surface = state().surface;
    if (surface.status !== "expanded") return;
    const layout = normalizeLayout(requested);
    const current = surface.layout;
    if (
      layout.bin === current.bin &&
      layout.inspector === current.inspector &&
      layout.top === current.top
    )
      return;
    retainOverlay({ bounds: surface.bounds, pane: surface.pane, layout });
    patch({ surface: Object.freeze({ ...surface, layout }) });
  }

  function retainOverlay(value: {
    bounds: OverlayBounds;
    pane: OverlayInternalPane;
    layout: NleLayout;
  }) {
    const { retention } = session;
    retention.write("nle.overlay", UNSCOPED, value, retention.generation());
  }

  /**
   * M25-44 (one NLE): the create action the retired compact editor was the only home of. It
   * creates the Authoring workspace from the current Context; while the overlay is open it then
   * initializes the timeline history, because an explicit open is otherwise the only initializer
   * and the open already happened.
   */
  async function nleStartAuthoring(): Promise<void> {
    await actions.runAuthoringIntent({ action: "create_authoring_workspace" });
    const status = state().surface.status;
    if (
      (status === "opening" || status === "expanded") &&
      authoringHistoryIsMissing()
    )
      await actions.runAuthoringIntent({
        action: "initialize_timeline_history",
      });
  }

  // ------------------------------------------------------------------ M25-29 import

  const importSlice = createNleWorkspaceImport({
    ctx,
    state,
    patch,
    nextRequestId,
    productionProjection,
    viewIntentEpoch: () => viewIntentEpoch,
    focusClipEditor: () => nleFocusClipEditor(),
  });
  const {
    nleImportSelectionState,
    nleImportSelectedOutputs,
    nleRetryImport,
    nleOpenImportedEditor,
    nleImportStateForCurrentOwner,
    nleClearImportHighlight,
  } = importSlice;

  // ------------------------------------------------------------------ M26 planning

  const planningSlice = createNleWorkspacePlanning({
    ctx,
    state,
    patch,
    nextRequestId,
    productionProjection,
    contextProjection,
  });
  const {
    nleAdmitStoryboard,
    nleSetPlanningScript,
    nleApproveAndImportPlan,
    nleCreatePlannedProject,
    nleOpenStoryboardReview,
    nlePrepareContext,
    nlePropose,
    nleSetSegmentationPolicy,
    nleSetStoryboardRows,
    nleSetTargetSeconds,
  } = planningSlice;

  // ------------------------------------------------------------------ M26-04 readiness

  function readinessSelection(): ManagedReadinessSelection | undefined {
    const plan = state().planning.plan;
    const production = productionProjection();
    if (plan === null || production === undefined) return undefined;
    if (
      plan.workspace_revision !== production.workspaceRevision ||
      plan.workspace_fingerprint !== production.workspaceFingerprint
    )
      return undefined;
    return {
      workspace_handle: production.workspaceHandle,
      expected_workspace_revision: plan.workspace_revision,
      expected_workspace_fingerprint: plan.workspace_fingerprint,
      expected_plan_fingerprint: plan.plan_fingerprint,
    };
  }

  /** Explicit readiness request; a held row cannot start and names its reason. */
  async function nleRequestReadiness(): Promise<void> {
    const readiness = state().readiness;
    if (readiness.status === "requesting") return;
    const selection = readinessSelection();
    if (selection === undefined) return;
    patch({
      readiness: Object.freeze({
        ...readiness,
        status: "requesting",
        error: null,
      }),
    });
    try {
      const result = await deps.managedQualificationClient.send(
        nextRequestId("readiness"),
        "prepare_managed_readiness",
        selection,
      );
      patch({
        readiness: Object.freeze({
          status: result.status,
          readiness: result,
          boundPlanFingerprint: selection.expected_plan_fingerprint,
          error: null,
        }),
      });
    } catch {
      patch({
        readiness: Object.freeze({
          status: "error",
          readiness: null,
          boundPlanFingerprint: selection.expected_plan_fingerprint,
          error: "managed_readiness_unavailable",
        }),
      });
    }
  }

  function nleSequenceStartable(): boolean {
    const { planning, readiness, sequence } = state();
    const production = productionProjection();
    return (
      production !== undefined &&
      planning.plan !== null &&
      !planningIsStale(planning, production.workspaceFingerprint) &&
      readiness.status === "ready" &&
      readiness.readiness !== null &&
      !readinessIsStale(readiness, planning.plan.plan_fingerprint) &&
      // The exact-plan readiness above is the authority for the pending-qualification hold.
      planStartHoldsResolvedByQualification(planning.plan) &&
      // A paused failed parent still owns its runner (B-M1605-SEQ-02): retry or cancel it first.
      (sequence.ui === "idle" || sequence.ui === "finished") &&
      !sequence.busy
    );
  }

  // ------------------------------------------------------------------ M26 sequence + B1

  /**
   * The busy state an explicit retry, resume or cancel starts from. IMPORTANT (B-M1605-SEQ-01):
   * the sync below keeps `previous.failure` whenever the runner reports none, so it can keep a
   * local failure such as canvas_revalidation_unavailable. An action that clears the runner's
   * failure must therefore clear the pane's copy as well, or a recovered sequence keeps showing
   * its old error.
   */
  function clearedSequence(sequence: NleSequenceState): NleSequenceState {
    return Object.freeze({ ...sequence, busy: true, failure: null });
  }

  function syncSequenceFromRunner(
    overrides: Partial<NleSequenceState> = {},
  ): void {
    const snapshot = actions.managedSerialSequenceSnapshot();
    const previous = state().sequence;
    const ui = deriveSequenceUi(previous.ui, snapshot);
    const pointer = deps.managedSequenceReattachStore.read();
    patch({
      sequence: Object.freeze({
        ...previous,
        ui,
        parentSequenceId:
          snapshot.parentSequenceId ?? previous.parentSequenceId,
        recoveryExpiresAtEpochMs:
          pointer?.parentSequenceId === snapshot.parentSequenceId
            ? pointer.expiresAtEpochMs
            : null,
        failure: snapshot.failure ?? previous.failure,
        ...overrides,
      }),
    });
  }

  function currentOwnedGraphReference(): OwnedGraphReference {
    return (
      session.acceptedManagedIdentity?.ownedReference ??
      UNBOUND_OWNED_GRAPH_REFERENCE
    );
  }

  function sequenceBindings() {
    // CRITICAL: pass the fetchApi bound by probeFetchApi, never `deps.api.fetchApi` itself. The
    // host's fetchApi reads `this`, so a detached reference throws a TypeError before the first
    // child's request, and every managed sequence stops on its first child as
    // managed_sequence_failed.
    const fetchApi = probeFetchApi(deps.api);
    const resolveChild = createProductionManagedChildResolver({
      app: deps.app,
      fetchApi: (fetchApi.status === "ready"
        ? fetchApi.value
        : undefined) as unknown as (
        path: string,
        init: RequestInit,
      ) => Promise<Response>,
      currentOwnedGraphReference,
    });
    return { resolveChild };
  }

  /** Explicit `Generate approved sequence`: authorizes and starts exactly the qualified plan. */
  async function nleStartSequence(): Promise<void> {
    if (!nleSequenceStartable()) return;
    const { planning, readiness } = state();
    const selection = readinessSelection();
    const plan = planning.plan;
    const generationPlanFingerprint =
      planning.projection?.proposal?.fingerprint;
    if (
      selection === undefined ||
      plan === null ||
      readiness.readiness === null ||
      generationPlanFingerprint === undefined
    )
      return;
    let intent;
    try {
      intent = buildQualifiedManagedStartIntent(
        readiness.readiness,
        selection,
        plan,
        generationPlanFingerprint,
      );
    } catch {
      patch({
        readiness: Object.freeze({
          ...readiness,
          status: "error",
          error: "managed_start_unqualified",
        }),
      });
      return;
    }
    patch({
      sequence: Object.freeze({
        ...initialNleSequenceState,
        ui: "starting",
        busy: true,
      }),
    });
    try {
      await actions.startManagedSerialSequence(intent, sequenceBindings());
    } catch {
      // The session recorded the safe failure code in its snapshot.
    } finally {
      syncSequenceFromRunner({ busy: false });
      startSequencePolling();
    }
  }

  /** `Continue current segment while I leave`: one idempotent detach after queue acceptance. */
  async function nleDetachSequence(): Promise<void> {
    const sequence = state().sequence;
    if (sequence.ui !== "detach_ready" || sequence.busy) return;
    patch({
      sequence: Object.freeze({ ...sequence, ui: "detaching", busy: true }),
    });
    try {
      await actions.detachManagedSerialSequence();
    } finally {
      // IMPORTANT: detach may race a terminal or ownership result; do not overwrite
      // the runner's disposition with a fabricated safe-to-leave state.
      syncSequenceFromRunner({ busy: false });
    }
  }

  /** Return: projection-first read-only reconciliation, then exact-prompt history if needed. */
  async function nleReattachSequence(): Promise<void> {
    const sequence = state().sequence;
    if (sequence.busy) return;
    patch({
      sequence: Object.freeze({
        ...sequence,
        ui: "reattach_reconciling",
        busy: true,
        failure: null,
      }),
    });
    let ui: NleSequenceUiState = "recovery_unavailable_or_expired";
    let reattach: NleSequenceState["reattach"] = null;
    try {
      reattach =
        await actions.reattachManagedSerialSequence(sequenceBindings());
      switch (reattach.disposition) {
        case "resumable":
          ui = "resume_ready";
          break;
        case "waiting_for_current_child":
          ui = "current_segment_still_owned";
          break;
        case "artifact_authority_unavailable":
          ui = "paused_unknown_ownership";
          break;
        case "terminal_reconciled":
          ui = "current_segment_completed";
          break;
        case "current":
          ui =
            reattach.parentState === "succeeded" ||
            reattach.parentState === "cancelled"
              ? "finished"
              : reattach.parentState === "paused_failure"
                ? "failed"
                : "current_segment_completed";
          break;
        default:
          ui = "recovery_unavailable_or_expired";
      }
    } catch {
      ui = "recovery_unavailable_or_expired";
    } finally {
      syncSequenceFromRunner({ busy: false, ui, reattach });
      await refreshSequenceProjection();
      startSequencePolling();
    }
  }

  function currentCanvasIdentity() {
    // IMPORTANT (B-M2522-RESUME-01): present the runner's bound workflow label, never a hash of
    // the canvas now. Every child write and host rewrite changes the whole graph, so a fresh hash
    // is refused as canvas_drift at the first resume after a completed child. The label is valid
    // only while its workflow object is still the active open tab; the owned projection is
    // observed now, through the accepted reference the children were written against. Without
    // that reference the backend's written projection cannot be matched, so resume stays local
    // and typed rather than sending an identity that can only drift.
    const label = actions.managedSerialCanvasLabel();
    const reference = session.acceptedManagedIdentity?.ownedReference;
    const workflow = readActiveWorkflow(deps.app);
    const openWorkflows = deps.app.extensionManager?.workflow?.openWorkflows;
    const serialized = serializeGraph(deps.app);
    if (
      label === null ||
      reference === undefined ||
      workflow === undefined ||
      workflow !== label.workflowAuthority ||
      !Array.isArray(openWorkflows) ||
      !openWorkflows.includes(workflow) ||
      serialized.status !== "ready"
    )
      return undefined;
    return {
      activeWorkflowFingerprint: label.activeWorkflowFingerprint,
      ownedProjectionFingerprint: observeOwnedGraph(serialized.value, reference)
        .fingerprint,
    };
  }

  /** `Resume remaining segments`: a fresh explicit mutation after current canvas revalidation. */
  async function nleResumeSequence(): Promise<void> {
    const sequence = state().sequence;
    if (sequence.ui !== "resume_ready" || sequence.busy) return;
    const identity = currentCanvasIdentity();
    if (identity === undefined) {
      patch({
        sequence: Object.freeze({
          ...sequence,
          ui: "recovery_unavailable_or_expired",
          failure: "canvas_revalidation_unavailable",
        }),
      });
      return;
    }
    patch({ sequence: clearedSequence(sequence) });
    try {
      await actions.resumeManagedSerialSequence(identity);
    } catch {
      // Safe failure code retained in the session snapshot.
    } finally {
      syncSequenceFromRunner({ busy: false });
      startSequencePolling();
    }
  }

  async function nleCancelSequence(): Promise<void> {
    const sequence = state().sequence;
    if (sequence.busy || sequence.ui === "idle" || sequence.ui === "finished")
      return;
    patch({ sequence: clearedSequence(sequence) });
    try {
      await actions.cancelManagedSerialSequence();
    } catch {
      // Retained in the snapshot.
    } finally {
      syncSequenceFromRunner({ busy: false });
      await refreshSequenceProjection();
    }
  }

  async function nleRetrySegment(segmentId: string): Promise<void> {
    const sequence = state().sequence;
    if (sequence.busy) return;
    const identity = currentCanvasIdentity();
    if (identity === undefined) return;
    patch({ sequence: clearedSequence(sequence) });
    try {
      await actions.retryManagedSerialSequence({ segmentId, ...identity });
    } catch {
      // Retained in the snapshot.
    } finally {
      syncSequenceFromRunner({ busy: false });
      startSequencePolling();
    }
  }

  /**
   * Bounded read of the current managed-sequence projection through the accepted non-ledger
   * route with its ETag. It carries no mutation request ID, consumes no ledger row, renews no
   * lease and cannot prune or cause host effects.
   */
  async function refreshSequenceProjection(): Promise<void> {
    const pointer = deps.managedSequenceReattachStore.read();
    const sequence = state().sequence;
    if (pointer === undefined) {
      // IMPORTANT: successful completion clears the pointer before the next UI tick.
      // Keep the runner's terminal result visible without issuing an unauthorised read.
      syncSequenceFromRunner();
      return;
    }
    pollAbort?.abort();
    const abort = new AbortController();
    pollAbort = abort;
    try {
      const read = await deps.managedSequenceClient.read(
        pointer.parentSequenceId,
        pointer.readAuthorityFingerprint,
        sequence.etag ?? undefined,
        abort.signal,
      );
      if (pollAbort !== abort) return;
      if (read.status === 200 && read.projection !== null) {
        const snapshot = actions.managedSerialSequenceSnapshot();
        const previous = state().sequence;
        patch({
          sequence: Object.freeze({
            ...previous,
            ui: deriveSequenceUi(previous.ui, {
              ...snapshot,
              parentState: read.projection.state,
              activeSegmentId: read.projection.activeSegmentId,
            }),
            parentSequenceId: read.projection.parentSequenceId,
            projection: read.projection,
            etag: read.etag,
          }),
        });
      }
    } catch {
      // A failed read leaves the last accepted projection in place; it never infers an outcome.
    } finally {
      if (pollAbort === abort) pollAbort = undefined;
    }
  }

  function startSequencePolling(): void {
    stopSequencePolling();
    const sequence = state().sequence;
    if (
      state().surface.status !== "expanded" ||
      sequence.ui === "idle" ||
      sequence.ui === "finished" ||
      sequence.ui === "failed" ||
      sequence.ui === "recovery_unavailable_or_expired"
    )
      return;
    patch({ sequence: Object.freeze({ ...state().sequence, polling: true }) });
    const tick = () => {
      pollTimer = undefined;
      void refreshSequenceProjection().finally(() => {
        const current = state().sequence;
        if (
          !current.polling ||
          current.ui === "finished" ||
          current.ui === "failed" ||
          current.ui === "recovery_unavailable_or_expired"
        ) {
          if (current.polling)
            patch({ sequence: Object.freeze({ ...current, polling: false }) });
          return;
        }
        pollTimer = setTimeout(tick, NLE_SEQUENCE_POLL_INTERVAL_MS);
      });
    };
    pollTimer = setTimeout(tick, NLE_SEQUENCE_POLL_INTERVAL_MS);
  }

  function stopSequencePolling(): void {
    if (pollTimer !== undefined) clearTimeout(pollTimer);
    pollTimer = undefined;
    pollAbort?.abort();
    pollAbort = undefined;
    const sequence = state().sequence;
    if (sequence.polling)
      session.nleWorkspace = Object.freeze({
        ...session.nleWorkspace,
        sequence: Object.freeze({ ...sequence, polling: false }),
      });
  }

  /** Recovery entry on mount: report only whether a pointer exists; no read, no queue. */
  function nleRecoveryPointerPresent(): boolean {
    return deps.managedSequenceReattachStore.read() !== undefined;
  }

  // ------------------------------------------------------------------ assembly

  async function nleAssembly(
    action: "assemble_sequence" | "cancel_assembly" | "retry_assembly",
  ): Promise<void> {
    const production = productionProjection();
    if (production === undefined || !production.allowedActions.includes(action))
      return;
    await actions.runProductionIntent({ action });
  }

  async function nleRefreshProduction(): Promise<void> {
    await actions.runProductionIntent({ action: "read_projection" });
  }

  function nleDispatchTimeline(intent: AuthoringIntent): Promise<void> {
    return Promise.resolve(actions.runAuthoringIntent(intent));
  }

  /**
   * Switch the sidebar to the Production page and its `clip_editor` function so the imported
   * rows are visible in the compact editor. The request is a generation counter the sidebar
   * consumes once; it never opens the overlay.
   */
  function nleFocusClipEditor(): void {
    actions.selectPage("production");
    patch({
      functionRequest: Object.freeze({
        id: "clip_editor" as const,
        generation: state().functionRequest.generation + 1,
      }),
    });
  }

  function nleFocusKeyForReturn(): string {
    const surface = state().surface;
    switch (surface.lastCloseReason) {
      case "function_switch":
      case "top_level_navigation":
        return "";
      default:
        return NLE_LAUNCHER_FOCUS_KEY;
    }
  }

  function nleIsSidebarFocusKey(value: unknown): boolean {
    return isSidebarFocusKey(value);
  }

  return {
    nleAdmitStoryboard,
    nleSetPlanningScript,
    nleApproveAndImportPlan,
    nleCreatePlannedProject,
    nleAssembly,
    nleCancelSequence,
    nleClearImportHighlight,
    nleCloseOverlay,
    nleDetachSequence,
    nleDispatchTimeline,
    nleDisposeOverlay,
    nleFocusClipEditor,
    nleFocusKeyForReturn,
    nleImportSelectedOutputs,
    nleImportSelectionState,
    nleIsSidebarFocusKey,
    nleLauncherCapability,
    nleOpenOverlay,
    nleOpenStoryboardReview,
    nleOverlayMountFailed,
    nleOverlayMounted,
    nleOverlayReleased,
    nlePrepareContext,
    nlePropose,
    nleReadRenderCapability,
    nleReattachSequence,
    nleRecoveryPointerPresent,
    nleRefreshProduction,
    nleRefreshSequenceProjection: refreshSequenceProjection,
    nleRequestReadiness,
    nleResizeOverlay,
    nleResumeSequence,
    nleRetryImport,
    nleOpenImportedEditor,
    nleImportStateForCurrentOwner,
    nleRetrySegment,
    nleRuntimeDisposition,
    nleSelectPane,
    nleSequenceStartable,
    nleSetLayout,
    nleSetSegmentationPolicy,
    nleSetStoryboardRows,
    nleSetTargetSeconds,
    nleStartAuthoring,
    nleStartSequence,
    nleNoteProductionExchange,
    nleStopOwnerRenewal,
    nleSurfaceCapability,
    nleSyncOwnerRenewal,
  };
}

export type NleWorkspaceActions = ReturnType<typeof createNleWorkspaceSession>;
