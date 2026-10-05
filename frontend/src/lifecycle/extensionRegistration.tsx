// Extension registration: builds the one `registerExtension` payload (setup, configure hooks,
// performance receipt and disposal) from the session and feature handles. `entry.tsx` registers
// it exactly once (M23-28 split of entry.tsx).

import { graphFingerprint as appModeFingerprint } from "../host/appMode";
import { createLanguageSettingsAdapter } from "../host/languageSettings";
import {
  isUnverifiedModelFreeProjection,
  type ProjectionGraphContext,
} from "../host/sidebarHost";
import { createSidebarWidthController } from "../host/sidebarWidth";
import { sidebarCopy } from "../i18n/catalog";
import {
  bindLocaleStoreToHost,
  createLanguageSetting,
} from "../i18n/localeStore";
import { managedJournal } from "../state/managedJournal";
import {
  hasExistingGraphAuthority,
  initialShellState,
  reduceShellState,
} from "../state/shellState";
import {
  initialWorkspaceState,
  reduceWorkspaceState,
} from "../state/sidebarWorkspace";
import { probeGraphSerializer, probeUiSettings } from "../host/hostSeams";
import { type ShellRuntime } from "./shellSession";

export function createExtensionRegistration(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;
  const syncVisibleOwner = () => actions.nleSyncOwnerRenewal(true);

  // CRITICAL (M25-21 section 14.4): view release is host collapse, hide or takeover, not the end of
  // the session. It stops owned playback, polling, leases, requests and listeners, and it must
  // leave `session.retention` alone. Clearing it here to "clean up" silently throws away the
  // user's unsent drafts and view settings on every collapse. Only `disposeExtension` clears it.
  const releaseViewState = (expectedContainer: HTMLElement): void => {
    if (session.container !== expectedContainer) return;
    // CRITICAL: losing the shared sidebar slot is the same browser-owner loss as explicit
    // unmount. Revoke managed successor authority before clearing render ownership, or a host
    // takeover without destroy can leave the serial sequence attached to a vanished client.
    void actions.detachManagedSerialSequence();
    actions.captureViewFocus();
    // M25-16: full view destroy records `view_destroy` and resets overlay mount memory before
    // render authority is cleared; the portal root unmounts with the React tree.
    actions.nleDisposeOverlay();
    actions.nleStopOwnerRenewal();
    // Clear render authority before close/abort actions can publish another state update.
    session.container = undefined;
    actions.releaseProviderSession();
    // M25-33: stop job polling and drop the continuation; reopening re-reads the running job.
    actions.releaseMediaRuntime();
    actions.closeSemanticProposalReview();
    deps.productionProposalDispatcher.clearSensitive();
    actions.closeProductionMediaPreview(false);
    const activeEnsure = session.productionEnsureActive;
    const wasLoading = session.productionState.status === "loading";
    session.productionAbort?.abort();
    session.productionAbort = undefined;
    if (activeEnsure !== undefined)
      session.productionEnsureClosedIdentities.add(activeEnsure.identity);
    session.productionEnsureActive = undefined;
    session.productionState =
      session.productionState.status === "pending" &&
      "projection" in session.productionState
        ? {
            status: "ready",
            projection: session.productionState.projection,
          }
        : wasLoading
          ? {
              status: "error",
              reason: "view_unmounted",
              recovery:
                session.productionSessionHandle !== undefined
                  ? "read"
                  : "create",
            }
          : session.productionState;
    actions.invalidateViewFocusClaim();
  };

  return {
    name: "comfyui-h3-context.product-shell.v1",
    settings: [createLanguageSetting(deps.localeStore)],
    setup() {
      deps.entrySetup.setup(() => {
        try {
          try {
            managedJournal.initialize(globalThis.localStorage);
          } catch {
            managedJournal.initialize(undefined);
          }
          deps.appModeLifecycle.synchronizeRunSequence(
            managedJournal
              .snapshot()
              .reduce((maximum, entry) => Math.max(maximum, entry.run), 0),
          );
          const uiSettings = probeUiSettings(deps.app);
          if (uiSettings.status === "ready") {
            const settings = uiSettings.value;
            session.localeDispose = bindLocaleStoreToHost(
              deps.localeStore,
              settings,
            );
            session.languageSettingsAdapter = createLanguageSettingsAdapter(
              deps.localeStore,
              settings,
            );
            session.languageSettingsSnapshot =
              session.languageSettingsAdapter.getSnapshot();
            session.languageSettingsDispose =
              session.languageSettingsAdapter.subscribe(() => {
                session.languageSettingsSnapshot =
                  session.languageSettingsAdapter?.getSnapshot() ??
                  session.languageSettingsSnapshot;
                actions.renderCurrent();
              });
          }
          session.localeSubscriptionDispose = deps.localeStore.subscribe(
            actions.renderCurrent,
          );
          window.addEventListener(
            "pagehide",
            actions.detachManagedSerialSequence,
          );
          window.addEventListener("pagehide", actions.releaseProviderSession);
          window.addEventListener("pageshow", actions.ensureProviderProjection);
          document.addEventListener("visibilitychange", syncVisibleOwner);
          session.pageSubscriptionDispose = deps.pageRegistry.subscribe(() => {
            actions.renderCurrent();
            actions.ensureProductionWorkspace();
            actions.ensureProviderProjection();
            actions.nleSyncOwnerRenewal(true);
          });
          actions.ensureProviderProjection();
          actions.ensureBuildProvenance();
          if (session.productionSessionHandle !== undefined)
            void actions.runProductionIntent({ action: "read_projection" });
          deps.host.register({
            launcherCopy() {
              const copy = sidebarCopy(deps.localeStore.getSnapshot().locale);
              return {
                title: copy.launcherTitle,
                tooltip: copy.launcherTooltip,
              };
            },
            mountView(nextContainer: HTMLElement) {
              deps.performanceRecorder.measure("mount", () => {
                const previousWidthDispose = session.widthDispose;
                session.widthDispose = undefined;
                previousWidthDispose?.();
                const previousFocusTrackingDispose =
                  session.focusTrackingDispose;
                session.focusTrackingDispose = undefined;
                previousFocusTrackingDispose?.();
                let mounted = false;
                try {
                  deps.mount.mount(nextContainer, ({ own }) => {
                    // CRITICAL: install ownership cleanup before each shared-slot mutation. Newer
                    // ComfyUI can replace this connected mount without calling our destroy first.
                    let containerOwned = false;
                    own(() => {
                      if (!containerOwned) return;
                      containerOwned = false;
                      if (session.container === nextContainer)
                        session.container = undefined;
                    });
                    containerOwned = true;
                    session.container = nextContainer;

                    let focusTrackingActive = false;
                    const releaseFocusTracking = (): void => {
                      if (!focusTrackingActive) return;
                      focusTrackingActive = false;
                      try {
                        nextContainer.removeEventListener(
                          "focusin",
                          actions.recordPageLocalFocus,
                        );
                      } finally {
                        if (
                          session.focusTrackingDispose === releaseFocusTracking
                        )
                          session.focusTrackingDispose = undefined;
                      }
                    };
                    own(releaseFocusTracking);
                    nextContainer.addEventListener(
                      "focusin",
                      actions.recordPageLocalFocus,
                    );
                    focusTrackingActive = true;
                    session.focusTrackingDispose = releaseFocusTracking;

                    const focusClaim =
                      actions.beginViewFocusClaim(nextContainer);
                    own(() => actions.invalidateViewFocusClaim(focusClaim));

                    let nextWidthDispose: (() => void) | undefined;
                    let widthActive = true;
                    const releaseWidth = (): void => {
                      if (!widthActive) return;
                      widthActive = false;
                      try {
                        nextWidthDispose?.();
                      } finally {
                        if (session.widthDispose === releaseWidth)
                          session.widthDispose = undefined;
                      }
                    };
                    own(releaseWidth);
                    nextWidthDispose =
                      createSidebarWidthController(nextContainer);
                    session.widthDispose = releaseWidth;
                    // Run the same product-state teardown for host takeover as for destroy.
                    own(() => releaseViewState(nextContainer));

                    return <actions.EntrySidebar focusClaim={focusClaim} />;
                  });
                  mounted = true;
                  actions.ensureProviderProjection();
                  if (deps.pageRegistry.getSnapshot().selected === "production")
                    actions.ensureProductionWorkspace();
                  actions.nleSyncOwnerRenewal(true);
                } catch (error) {
                  if (mounted)
                    try {
                      deps.mount.unmount();
                    } catch {
                      // The initialization failure remains authoritative after full rollback.
                    }
                  throw error;
                }
              });
            },
            unmountView() {
              const activeContainer = session.container;
              if (activeContainer !== undefined)
                releaseViewState(activeContainer);
              try {
                deps.mount.unmount();
              } finally {
                session.focusTrackingDispose?.();
                session.focusTrackingDispose = undefined;
                session.widthDispose?.();
                session.widthDispose = undefined;
              }
            },
            disposeExtension() {
              deps.entrySetup.dispose(() => {
                deps.appModeLifecycle.dispose();
                session.pendingAppMode = undefined;
                actions.clearDeferredAppModeProjections();
                actions.clearDeferredAppModeTerminals();
                actions.clearDeferredManagedArtifacts();
                actions.clearActiveAppModeExecution();
                session.acceptedManagedIdentity = undefined;
                session.graphChangedDuringRun = false;
                session.executedGraphRefresh = undefined;
                session.ignoredProjectionPromptIds.clear();
                session.ignoreProjectionUntilGraphRefresh = true;
                session.cancelledProjectionSuppression = undefined;
                session.transactionTransparency = undefined;
                session.acceptedGenerationSequence = undefined;
                deps.productionProposalDispatcher.dispose();
                actions.nleStopOwnerRenewal();
                actions.releaseMediaRuntime();
                actions.closeProductionMediaPreview(false);
                session.actionAbort?.abort();
                session.actionAbort = undefined;
                session.productionAbort?.abort();
                session.productionAbort = undefined;
                session.productionEnsureActive = undefined;
                session.productionEnsureAttemptedIdentities.clear();
                session.productionEnsureClosedIdentities.clear();
                session.languageSettingsDispose?.();
                session.languageSettingsDispose = undefined;
                session.languageSettingsAdapter?.dispose();
                session.languageSettingsAdapter = undefined;
                session.localeSubscriptionDispose?.();
                session.localeSubscriptionDispose = undefined;
                session.pageSubscriptionDispose?.();
                session.pageSubscriptionDispose = undefined;
                window.removeEventListener(
                  "pagehide",
                  actions.detachManagedSerialSequence,
                );
                window.removeEventListener(
                  "pagehide",
                  actions.releaseProviderSession,
                );
                window.removeEventListener(
                  "pageshow",
                  actions.ensureProviderProjection,
                );
                document.removeEventListener(
                  "visibilitychange",
                  syncVisibleOwner,
                );
                actions.releaseProviderSession();
                session.localeDispose?.();
                session.localeDispose = undefined;
                session.retention.dispose();
                actions.invalidateViewFocusClaim();
                session.container = undefined;
                try {
                  deps.mount.unmount();
                } finally {
                  session.focusTrackingDispose?.();
                  session.focusTrackingDispose = undefined;
                  session.widthDispose?.();
                  session.widthDispose = undefined;
                }
              });
            },
            onProjection(
              projection,
              workspace,
              graphContext: ProjectionGraphContext,
              nextTransactionTransparency,
              nextGenerationSequence,
              nextSemanticProposalReview,
            ) {
              actions.observeManagedSerialRunning(
                projection.correlation.prompt_id,
              );
              if (
                actions.consumeManagedProjection({
                  projection,
                  workspace,
                  transactionTransparency: nextTransactionTransparency,
                  semanticProposalReview: nextSemanticProposalReview,
                })
              )
                return;
              const accept = (): void => {
                // CRITICAL: a cancelled transaction has no reliable prompt identity
                // when abort wins before queuePrompt returns. Hold every late event
                // until a fresh matching run or a changed normal graph event.
                const pending = session.pendingAppMode;
                const matchesPendingProjection =
                  pending?.result?.queuePromptId ===
                  projection.correlation.prompt_id;
                actions.recordProjectionTrace("accept", {
                  state: session.state.status,
                  pending: pending !== undefined,
                  has_result: pending?.result !== undefined,
                  prompt_matches: matchesPendingProjection,
                  graph_source: graphContext.source,
                  graph_status: graphContext.inspection.status,
                  has_executed_refresh:
                    session.executedGraphRefresh !== undefined,
                });
                if (
                  isUnverifiedModelFreeProjection(
                    graphContext,
                    pending?.run,
                    session.executedGraphRefresh,
                    projection.correlation.execution_node_id,
                  )
                ) {
                  actions.recordProjectionTrace(
                    "reject_unverified_model_free",
                    {},
                  );
                  return;
                }
                if (
                  session.cancelledProjectionSuppression !== undefined &&
                  !matchesPendingProjection
                )
                  return;
                if (
                  session.acceptedProjectionPromptIds.has(
                    projection.correlation.prompt_id,
                  )
                )
                  return;
                if (
                  session.ignoreProjectionUntilGraphRefresh ||
                  session.ignoredProjectionPromptIds.has(
                    projection.correlation.prompt_id,
                  )
                )
                  return;
                if (pending !== undefined && pending.result === undefined) {
                  actions.recordProjectionTrace("reject_pending_identity", {});
                  return;
                }
                if (pending?.result !== undefined) {
                  if (
                    pending.result.transactionId !== pending.run ||
                    pending.result.queuePromptId === undefined ||
                    pending.result.queuePromptId !==
                      projection.correlation.prompt_id
                  )
                    return;
                  if (pending.managedIdentity !== undefined) {
                    if (
                      actions.managedCanvasIdentityStatus(
                        pending.managedIdentity,
                      ) !== "match"
                    ) {
                      actions.recordProjectionTrace("reject_owned_mismatch", {
                        graph_changed_during_run: session.graphChangedDuringRun,
                      });
                      return;
                    }
                  } else {
                    const serializer = probeGraphSerializer(deps.app);
                    if (serializer.status !== "ready") return;
                    let currentGraphFingerprint: string;
                    let currentGraphSerialized: unknown;
                    try {
                      currentGraphSerialized = serializer.value();
                      currentGraphFingerprint = appModeFingerprint(
                        currentGraphSerialized,
                      );
                    } catch {
                      return;
                    }
                    if (
                      currentGraphFingerprint !==
                      pending.result.graphFingerprint
                    ) {
                      const modelFreeCandidate = session.executedGraphRefresh;
                      if (
                        modelFreeCandidate?.run !== pending.run ||
                        modelFreeCandidate.graphFingerprint !==
                          currentGraphFingerprint ||
                        modelFreeCandidate.executionNodeId !==
                          projection.correlation.execution_node_id ||
                        !actions.isModelFreeProjectionGraph(
                          currentGraphSerialized,
                          projection.correlation.execution_node_id,
                        )
                      ) {
                        actions.recordProjectionTrace("reject_graph_mismatch", {
                          graph_changed_during_run:
                            session.graphChangedDuringRun,
                          has_model_free_candidate:
                            modelFreeCandidate !== undefined,
                        });
                        return;
                      }
                    }
                  }
                }
                if (
                  pending?.result?.queuePromptId !== undefined &&
                  pending.result.queuePromptId !==
                    projection.correlation.prompt_id
                ) {
                  // A late/foreign execution must never project into the active run.
                  return;
                }
                session.actionAbort?.abort();
                session.actionAbort = undefined;
                let acceptedGraphFingerprint =
                  pending?.result?.graphFingerprint;
                if (pending === undefined) {
                  const identity = actions.currentGraphProjectionIdentity();
                  // CRITICAL: only an exact current serialized anchor may own a
                  // later remount; unavailable provenance must remain fail-closed.
                  if (
                    identity?.executionNodeId ===
                    projection.correlation.execution_node_id
                  )
                    acceptedGraphFingerprint = identity.graphFingerprint;
                }
                session.state = reduceShellState(session.state, {
                  type: "projection",
                  anchorExecutionId: projection.correlation.execution_node_id,
                  projection,
                  transactionId: pending?.run,
                  graphFingerprint: acceptedGraphFingerprint,
                  modelFreeRescanExpected:
                    session.executedGraphRefresh !== undefined,
                  // sidebarHost independently authenticates the unredacted workspace
                  // text against this backend-owned SHA-256 before it reaches state.
                  backendPromptFingerprint: workspace.prompt_fingerprint,
                });
                if (pending?.managedIdentity !== undefined)
                  session.acceptedManagedIdentity = pending.managedIdentity;
                else if (pending !== undefined)
                  session.acceptedManagedIdentity = undefined;
                if (
                  pending?.run !== undefined &&
                  actions.isCurrentAppModeRun(pending.run) &&
                  deps.appModeLifecycle.getSnapshot().value === "queued"
                )
                  actions.sendAppModeLifecycle({ type: "EXECUTION_STARTED" });
                session.pendingAppMode = undefined;
                if (pending?.run !== undefined) {
                  actions.clearDeferredAppModeProjections(pending.run);
                  actions.clearDeferredAppModeTerminals(pending.run);
                  actions.clearDeferredManagedArtifacts(pending.run);
                }
                actions.rememberAcceptedProjectionPromptId(
                  projection.correlation.prompt_id,
                );
                session.graphChangedDuringRun = false;
                session.executedGraphRefresh = undefined;
                session.cancelledProjectionSuppression = undefined;
                session.ignoredProjectionPromptIds.clear();
                session.ignoreProjectionUntilGraphRefresh = false;
                session.assistedAbort?.abort();
                session.assistedAbort = undefined;
                session.assistedProposal = undefined;
                session.assistedBusy = false;
                session.assistedFailure = undefined;
                session.workspaceState = reduceWorkspaceState(
                  session.workspaceState,
                  {
                    type: "host_execution",
                    projection: workspace,
                  },
                );
                if (nextSemanticProposalReview === undefined)
                  deps.productionProposalDispatcher.clearCurrentSource();
                else
                  deps.productionProposalDispatcher.observe(
                    {
                      workspace_id: workspace.workspace_id,
                      report_revision: workspace.report_revision,
                      report_fingerprint: workspace.report_fingerprint,
                    },
                    nextSemanticProposalReview,
                  );
                session.acceptedGenerationSequence = nextGenerationSequence;
                const liveProduction = actions.currentProductionProjection();
                if (
                  nextGenerationSequence !== undefined &&
                  liveProduction?.workspaceId ===
                    nextGenerationSequence.workspace_id &&
                  liveProduction.workspaceRevision ===
                    nextGenerationSequence.workspace_revision &&
                  liveProduction.workspaceFingerprint ===
                    nextGenerationSequence.workspace_fingerprint
                )
                  void actions.runProductionIntent({
                    action: "read_projection",
                  });
                deps.pageRegistry.register({ id: "production" });
                session.transactionTransparency = nextTransactionTransparency;
                actions.recordProjectionTrace("accepted", {
                  state: session.state.status,
                });
                actions.renderCurrent();
              };
              const pending = session.pendingAppMode;
              if (
                pending !== undefined &&
                pending.result === undefined &&
                graphContext.source === "executed"
              ) {
                actions.recordProjectionTrace("deferred", {
                  run: pending.run,
                  graph_status: graphContext.inspection.status,
                });
                session.deferredAppModeProjections.set(
                  projection.correlation.prompt_id,
                  {
                    run: pending.run,
                    executionNodeId: projection.correlation.execution_node_id,
                    accept,
                  },
                );
                while (session.deferredAppModeProjections.size > 8) {
                  const oldest = session.deferredAppModeProjections
                    .keys()
                    .next().value;
                  if (typeof oldest !== "string") break;
                  session.deferredAppModeProjections.delete(oldest);
                }
                return;
              }
              accept();
            },
            onExecutionTerminal(event) {
              actions.observeManagedSerialTerminal(event);
              if (actions.consumeManagedTerminal(event)) return;
              actions.observeAppModeExecutionTerminal(event);
            },
            onSaveVideoArtifact(event) {
              actions.observeManagedSerialArtifact(event);
              actions.consumeManagedArtifact(event);
            },
            onHostAvailability(transition) {
              actions.observeHostAvailability(transition);
            },
            onGraph(inspection, source = "graph") {
              // An ordinary graph event supersedes any executed-rescan provenance;
              // a later projection must not inherit model-free authority from an
              // event whose graph identity is no longer current.
              if (source === "graph") session.executedGraphRefresh = undefined;
              const pending = session.pendingAppMode;
              if (session.state.status === "working" && pending !== undefined) {
                const identity = actions.currentGraphProjectionIdentity();
                const graphChanged =
                  pending.result !== undefined &&
                  (pending.managedIdentity !== undefined
                    ? actions.managedCanvasIdentityStatus(
                        pending.managedIdentity,
                      ) === "mismatch"
                    : identity !== undefined &&
                      identity.graphFingerprint !==
                        pending.result.graphFingerprint);
                if (source === "graph" && graphChanged) {
                  session.graphChangedDuringRun = true;
                } else if (
                  source === "executed" &&
                  graphChanged &&
                  pending.managedIdentity === undefined &&
                  identity !== undefined &&
                  !session.graphChangedDuringRun &&
                  inspection.status === "missing" &&
                  inspection.reason === "missing_native_h3_core" &&
                  inspection.anchors[0]?.executionId !== undefined
                ) {
                  session.executedGraphRefresh = {
                    run: pending.run,
                    graphFingerprint: identity.graphFingerprint,
                    executionNodeId: inspection.anchors[0].executionId,
                  };
                } else if (
                  pending.result === undefined &&
                  source === "executed" &&
                  identity !== undefined &&
                  inspection.status === "missing" &&
                  inspection.reason === "missing_native_h3_core" &&
                  inspection.anchors[0]?.executionId !== undefined
                ) {
                  // queuePrompt can execute before its Promise returns the prompt
                  // identity. Preserve graph provenance here; the deferred event
                  // still needs an exact prompt-ID match before it is accepted.
                  session.executedGraphRefresh = {
                    run: pending.run,
                    graphFingerprint: identity.graphFingerprint,
                    executionNodeId: inspection.anchors[0].executionId,
                  };
                }
              }
              // A run owns its graph snapshot; refresh notifications during the
              // transaction cannot downgrade the visible working state.
              if (session.state.status === "working") return;
              if (
                source === "remount" &&
                (session.state.status === "projected" ||
                  session.state.status === "editing_setup")
              ) {
                const acceptedGraphFingerprint =
                  session.state.status === "projected"
                    ? session.state.graphFingerprint
                    : session.state.prior.graphFingerprint;
                const identity = actions.currentGraphProjectionIdentity();
                const identityMatches =
                  session.acceptedManagedIdentity !== undefined
                    ? actions.managedCanvasIdentityStatus(
                        session.acceptedManagedIdentity,
                      ) === "match"
                    : acceptedGraphFingerprint !== undefined &&
                      identity?.graphFingerprint === acceptedGraphFingerprint;
                if (identityMatches) {
                  // IMPORTANT: a view remount must refresh graph-derived source options
                  // without discarding a draft owned by the unchanged projected graph.
                  actions.renderCurrent();
                  return;
                }
              }
              if (source === "graph")
                actions.releaseProjectionQuarantineIfGraphChanged();
              // Keep a same-graph cancellation quarantine through executed events;
              // only a changed normal graph event releases it above.
              if (session.cancelledProjectionSuppression === undefined)
                session.ignoreProjectionUntilGraphRefresh = false;
              if (
                session.nativePreferenceRecoveryRefresh &&
                source === "graph" &&
                !(
                  inspection.status === "ready" &&
                  inspection.existingGraphCompatible === true
                )
              ) {
                // IMPORTANT: this is the one refresh requested by Keep itself.
                // A non-ready census may update diagnostics, but cannot undo the
                // user's explicit choice or mutate/queue the canvas. Later graph
                // events are classified normally after the call returns.
                session.state = {
                  status: "interactive",
                  reason: "native_preference",
                  existingGraph:
                    hasExistingGraphAuthority(session.state) ||
                    (inspection.nodeCount ?? 0) > 0,
                  inspection,
                };
                session.acceptedManagedIdentity = undefined;
                actions.renderCurrent();
                return;
              }
              session.state = reduceShellState(session.state, {
                type: "graph",
                inspection,
              });
              if (
                session.state.status !== "projected" &&
                session.state.status !== "editing_setup"
              )
                session.acceptedManagedIdentity = undefined;
              actions.renderCurrent();
            },
          });
        } catch {
          session.localeSubscriptionDispose?.();
          session.localeSubscriptionDispose = undefined;
          session.pageSubscriptionDispose?.();
          session.pageSubscriptionDispose = undefined;
          session.localeDispose?.();
          session.localeDispose = undefined;
          session.state = {
            status: "error",
            code: "incompatible_seam",
            severity: "error",
            source: "seam",
            message: "incompatible_seam",
            recovery: "use_native",
          };
        }
      });
    },
    beforeConfigureGraph() {
      // CRITICAL: a configure this run is performing is not the user replacing the
      // canvas. Abandoning the run here would cancel every materialization at the
      // moment it succeeded.
      if (session.ownedGraphConfigureDepth > 0) return;
      // CRITICAL: read the interrupted state, not the visible one. A dropped socket wraps the state,
      // and a canvas swap while the wrapper is up would otherwise see none of the three and arm
      // neither the cancellation notice nor the projection quarantine below -- so a late projection
      // for the abandoned run would land on the canvas the user just replaced. A lost host is the
      // most likely window for exactly that race.
      const interrupted = actions.interruptedShellState();
      const wasWorking = interrupted.status === "working";
      const wasProjected = interrupted.status === "projected";
      const wasEditingSetup = interrupted.status === "editing_setup";
      const priorSuppression = actions.currentProjectionSuppression();
      session.pendingSidebarFocusKey = undefined;
      deps.appModeLifecycle.invalidate();
      session.pendingAppMode = undefined;
      actions.clearDeferredAppModeProjections();
      actions.clearDeferredAppModeTerminals();
      actions.clearDeferredManagedArtifacts();
      actions.clearActiveAppModeExecution();
      session.acceptedManagedIdentity = undefined;
      session.graphChangedDuringRun = false;
      session.executedGraphRefresh = undefined;
      session.ignoreProjectionUntilGraphRefresh =
        wasWorking || wasProjected || wasEditingSetup;
      // CRITICAL: configure events can race an already-emitted executed event;
      // keep the old transaction quarantine through graph refresh until an
      // explicit new App Mode/native choice releases it.
      if (wasWorking || wasProjected || wasEditingSetup) {
        session.cancelledProjectionSuppression = priorSuppression;
      }
      session.actionAbort?.abort();
      session.actionAbort = undefined;
      session.productionAbort?.abort();
      session.productionAbort = undefined;
      session.productionEnsureActive = undefined;
      session.productionEnsureAttemptedIdentities.clear();
      session.productionEnsureClosedIdentities.clear();
      session.productionState = { status: "absent" };
      session.state = wasWorking
        ? { status: "interactive", reason: "cancelled" }
        : initialShellState;
      session.assistedAbort?.abort();
      session.assistedAbort = undefined;
      session.assistedProposal = undefined;
      session.assistedBusy = false;
      session.assistedFailure = undefined;
      session.workspaceState = initialWorkspaceState;
      session.acceptedGenerationSequence = undefined;
      session.transactionTransparency = undefined;
      deps.productionProposalDispatcher.resetAll();
      actions.closeProductionMediaPreview(false);
      actions.renderCurrent();
    },
    afterConfigureGraph() {
      deps.host.refreshGraph();
    },
    nodeCreated() {
      deps.host.refreshGraph();
    },
    loadedGraphNode() {
      deps.host.refreshGraph();
    },
    h3PerformanceReceipt() {
      return deps.host.performanceReceipt();
    },
    h3DisposeExtension() {
      deps.durationResolutionController.dispose();
      deps.host.disposeExtension();
    },
  };
}
