// Presentation binding: the EntrySidebar element, view-focus claims, page selection,
// diagnostics recording and the single render binding (M23-28 split of entry.tsx).

import { H3_CONTEXT_VERSION } from "../buildMetadata";
import { H3Sidebar, type SidebarNleBinding } from "../components/H3Sidebar";
import { NleOverlay } from "../components/nle/NleOverlay";
import type { NleWorkspaceBinding } from "../components/nle/nleWorkspaceBinding";
import type { TransactionIntent } from "../contracts/transactionTransparencyCodec";
import type { PublicCompositionSnapshot } from "../contracts/compositionCodec";
import {
  deriveNleMonitorBinding,
  type NleMonitorBinding,
} from "../runtime/nleWorkspaceRuntime";
import type { VisualCompositionBinding } from "../runtime/visualCompositionSession";
import { sidebarCopy } from "../i18n/catalog";
import {
  isSidebarFocusKey,
  type SidebarFocusKey,
} from "../navigation/focusRegistry";
import type { PageId } from "../navigation/pageRegistry";
import {
  diagnosticShellStateSignature,
  managedJournal,
} from "../state/managedJournal";
import { reduceShellState } from "../state/shellState";
import { Profiler, useLayoutEffect } from "react";
import { probeToast, seamReady } from "../host/hostSeams";
import { acquireModalKeyboardGuard } from "../host/modalKeyboardGuard";
import { acquireCanvasKeyboardGuard } from "../host/canvasKeyboardGuard";
import { type ShellRuntime, type ViewFocusClaim } from "./shellSession";

export function createPresentationBinding(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;

  function ensureBuildProvenance(): void {
    if (session.buildProvenanceRequested) return;
    session.buildProvenanceRequested = true;
    void deps.buildProvenanceClient.load().then(
      (value) => {
        session.buildProvenance = value;
        renderCurrent();
      },
      () => {
        // Provenance is advisory observability; route loss cannot gate App Mode.
      },
    );
  }

  function handleTransactionIntent(intent: TransactionIntent): void {
    if (session.transactionTransparency === undefined) return;
    if (intent === "rerun") actions.retryAppMode();
    else actions.chooseNative();
  }

  function editAppModeSetup(): void {
    if (session.state.status === "error") {
      if (session.state.recovery !== "retry") return;
      const failedManagedGraph =
        session.cancelledProjectionSuppression?.kind === "managed"
          ? session.cancelledProjectionSuppression.identity
          : undefined;
      if (
        failedManagedGraph !== undefined &&
        actions.managedCanvasIdentityStatus(failedManagedGraph) !== "match"
      ) {
        deps.host.refreshGraph();
        return;
      }
      // IMPORTANT: returning to the form clears the failed member retry target. Keeping it would
      // turn an ordinary Start with edited inputs into regeneration of the failed candidate.
      session.retryableManagedProductionMember = undefined;
      // IMPORTANT: a failed managed run can leave its exact owned graph on a previously empty
      // canvas. Keep explicit replacement consent for that verified graph; treating it as a new
      // route makes the next Start refuse dirty_graph, while a broad replace could erase edits.
      session.state = {
        status: "interactive",
        reason:
          failedManagedGraph === undefined
            ? "pending_capability"
            : "dirty_graph",
        existingGraph:
          failedManagedGraph === undefined ? session.state.existingGraph : true,
      };
      session.pendingSidebarFocusKey =
        `app-stage-${session.appModeDraft.activeStage}` as SidebarFocusKey;
      renderCurrent();
      return;
    }
    if (session.state.status !== "projected") return;
    const projection = actions.currentProjection();
    const identity = actions.currentGraphProjectionIdentity();
    // CRITICAL: a projected result must not become setup authority after the
    // visible graph has changed between the last host refresh and this click.
    const managedIdentityStatus =
      session.acceptedManagedIdentity === undefined
        ? undefined
        : actions.managedCanvasIdentityStatus(session.acceptedManagedIdentity);
    if (
      managedIdentityStatus === undefined
        ? identity?.graphFingerprint !== session.state.graphFingerprint
        : managedIdentityStatus !== "match"
    ) {
      deps.host.refreshGraph();
      return;
    }
    const requestedSeconds =
      projection?.capabilities.output_duration.requested_seconds;
    if (
      requestedSeconds === undefined ||
      !Number.isInteger(requestedSeconds) ||
      requestedSeconds < 4 ||
      requestedSeconds > 15
    ) {
      deps.host.refreshGraph();
      return;
    }
    const next = reduceShellState(session.state, { type: "edit_setup" });
    if (next === session.state) return;
    if (session.appModeDraft.requestedSeconds !== requestedSeconds) {
      // IMPORTANT: setup resumes the accepted request, never the rounded
      // delivered duration or a mutable canvas widget.
      session.appModeDraft = { ...session.appModeDraft, requestedSeconds };
      session.appModeDraftRevision += 1;
    }
    session.state = next;
    session.pendingSidebarFocusKey =
      `app-stage-${session.appModeDraft.activeStage}` as SidebarFocusKey;
    renderCurrent();
  }

  function cancelAppModeSetup(): void {
    if (session.state.status !== "editing_setup") return;
    const priorFingerprint = session.state.prior.graphFingerprint;
    const identity = actions.currentGraphProjectionIdentity();
    const managedIdentityStatus =
      session.acceptedManagedIdentity === undefined
        ? undefined
        : actions.managedCanvasIdentityStatus(session.acceptedManagedIdentity);
    const graphDrifted =
      managedIdentityStatus === undefined
        ? identity?.graphFingerprint !== priorFingerprint
        : managedIdentityStatus !== "match";
    session.state = reduceShellState(session.state, {
      type: "cancel_edit",
      graphFingerprint:
        managedIdentityStatus === "match"
          ? priorFingerprint
          : identity?.graphFingerprint,
    });
    session.pendingSidebarFocusKey =
      session.state.status === "projected"
        ? "edit-app-mode-setup"
        : (`app-stage-${session.appModeDraft.activeStage}` as SidebarFocusKey);
    renderCurrent();
    if (graphDrifted) deps.host.refreshGraph();
  }

  function beginViewFocusClaim(nextContainer: HTMLElement): ViewFocusClaim {
    const claim = Object.freeze({
      container: nextContainer,
      generation: ++session.viewFocusGeneration,
    });
    session.viewFocusClaim = claim;
    return claim;
  }

  function invalidateViewFocusClaim(expected?: ViewFocusClaim): void {
    if (expected !== undefined && session.viewFocusClaim !== expected) return;
    session.viewFocusGeneration += 1;
    session.viewFocusClaim = undefined;
  }

  function consumeViewFocusClaim(claim: ViewFocusClaim): void {
    if (
      session.viewFocusClaim !== claim ||
      claim.generation !== session.viewFocusGeneration ||
      session.container !== claim.container ||
      !claim.container.isConnected
    )
      return;
    // Consume before focusing so StrictMode or a later ordinary render cannot
    // reuse this mount authority and steal focus from the user.
    session.viewFocusClaim = undefined;
    const fallbackKeys: SidebarFocusKey[] = [
      `page-${deps.pageRegistry.getSnapshot().selected}`,
      session.workspaceDraft === undefined
        ? `app-stage-${session.appModeDraft.activeStage}`
        : `workspace-stage-${session.workspaceDraft.activeStage}`,
    ];
    const pageFocus = session.pageFocusKeys.get(
      deps.pageRegistry.getSnapshot().selected,
    );
    const preferred = pageFocus ?? session.lastViewFocusKey;
    const keys =
      preferred === undefined ? fallbackKeys : [preferred, ...fallbackKeys];
    const controls = Array.from(
      claim.container.querySelectorAll<HTMLElement>("[data-h3-focus-key]"),
    );
    const target = keys
      .map((key) =>
        controls.find((control) => control.dataset.h3FocusKey === key),
      )
      .find((control) => control !== undefined);
    target?.focus({ preventScroll: true });
  }

  function activeSidebarFocusKey(): SidebarFocusKey | undefined {
    const active = document.activeElement;
    if (
      !(active instanceof HTMLElement) ||
      session.container?.contains(active) !== true
    )
      return undefined;
    const explicit = active.dataset.h3FocusKey;
    return isSidebarFocusKey(explicit) ? explicit : undefined;
  }

  function recordPageLocalFocus(event: FocusEvent): void {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const key = target.dataset.h3FocusKey;
    if (!isSidebarFocusKey(key) || key.startsWith("page-")) return;
    session.pageFocusKeys.set(deps.pageRegistry.getSnapshot().selected, key);
  }

  function selectPage(id: PageId): void {
    const current = deps.pageRegistry.getSnapshot().selected;
    if (current === id) return;
    // M25-16: programmatic or user navigation closes an expanded editor before the transition.
    actions.nleCloseOverlay("top_level_navigation");
    actions.mediaRuntimeLeaveContext("page");
    if (
      current === "production" &&
      session.productionAbort !== undefined &&
      session.productionState.status === "loading"
    ) {
      // Only the projection-free entry flight is bounced on navigation; pending
      // ordinary actions keep their accepted M17-12 background settlement.
      const activeEnsure = session.productionEnsureActive;
      session.productionAbort.abort();
      session.productionAbort = undefined;
      if (activeEnsure !== undefined)
        session.productionEnsureClosedIdentities.add(activeEnsure.identity);
      session.productionEnsureActive = undefined;
      session.productionState = {
        status: "error",
        reason: "navigation_cancelled",
        recovery:
          session.productionSessionHandle !== undefined ? "read" : "create",
      };
    }
    actions.closeSemanticProposalReview();
    actions.closeProductionMediaPreview(false);
    deps.productionProposalDispatcher.clearSensitive();
    const currentFocus = activeSidebarFocusKey();
    if (currentFocus !== undefined && !currentFocus.startsWith("page-"))
      session.pageFocusKeys.set(current, currentFocus);
    session.pendingPageFocus = Object.freeze({
      page: id,
      key: session.pageFocusKeys.get(id) ?? `page-${id}`,
    });
    deps.pageRegistry.select(id);
  }

  function consumePageFocus(page: PageId): void {
    const pending = session.pendingPageFocus;
    if (
      pending === undefined ||
      pending.page !== page ||
      session.container === undefined ||
      !session.container.isConnected
    )
      return;
    session.pendingPageFocus = undefined;
    const target = Array.from(
      session.container.querySelectorAll<HTMLElement>("[data-h3-focus-key]"),
    ).find((control) => control.dataset.h3FocusKey === pending.key);
    (
      target ??
      session.container.querySelector<HTMLElement>(
        `[data-h3-focus-key="page-${page}"]`,
      )
    )?.focus({ preventScroll: true });
  }

  function consumePendingSidebarFocus(): void {
    const key = session.pendingSidebarFocusKey;
    if (
      key === undefined ||
      session.container === undefined ||
      !session.container.isConnected
    )
      return;
    const target = Array.from(
      session.container.querySelectorAll<HTMLElement>("[data-h3-focus-key]"),
    ).find((control) => control.dataset.h3FocusKey === key);
    if (target === undefined) return;
    session.pendingSidebarFocusKey = undefined;
    target.focus({ preventScroll: true });
  }

  function nleSidebarBinding(): SidebarNleBinding {
    const context = actions.currentProjection();
    return {
      surface: session.nleWorkspace.surface,
      supported: actions.nleLauncherCapability() !== null,
      onOpen: actions.nleOpenOverlay,
      onStartAuthoring: () => void actions.nleStartAuthoring(),
      onFunctionChange: () => {
        actions.mediaRuntimeLeaveContext("function");
        actions.nleCloseOverlay("function_switch");
      },
      requestedFunction: session.nleWorkspace.functionRequest,
      planning: {
        state: session.nleWorkspace,
        contextAvailable: context !== undefined,
        // A redacted prompt is not the Context's text, so it is no source for a script.
        ...(context === undefined || context.prompt_text_redacted
          ? {}
          : { contextPromptText: context.prompt_text }),
        actions: nleWorkspaceBinding(deps.localeStore.getSnapshot().locale)
          .actions,
      },
      importAction: {
        state: actions.nleImportStateForCurrentOwner(),
        selectionState: actions.nleImportSelectionState,
        onImport: (segmentIds) =>
          void actions.nleImportSelectedOutputs(segmentIds),
        onRetry: () => void actions.nleRetryImport(),
        onOpen: () => void actions.nleOpenImportedEditor(),
        mediaTools: actions.mediaToolsBinding(),
      },
    };
  }

  function nleWorkspaceBinding(
    locale: NleWorkspaceBinding["locale"],
  ): NleWorkspaceBinding {
    return {
      locale,
      state: session.nleWorkspace,
      retention: session.retention,
      authoring: session.authoringState,
      production: session.productionState,
      importAction: {
        state: actions.nleImportStateForCurrentOwner(),
        selectionState: actions.nleImportSelectionState,
        onImport: (segmentIds) =>
          void actions.nleImportSelectedOutputs(segmentIds),
        onRetry: () => void actions.nleRetryImport(),
        onOpen: () => void actions.nleOpenImportedEditor(),
        mediaTools: actions.mediaToolsBinding(),
      },
      contextAvailable: actions.currentProjection() !== undefined,
      runtime: actions.nleRuntimeDisposition(),
      leaseClient: deps.authoringMediaSourceLeaseClient,
      openSourcePreview: deps.authoringMediaPreviewClient.open,
      ...(deps.nlePresentationObserver === undefined
        ? {}
        : { onPresentationMeasurement: deps.nlePresentationObserver }),
      output: {
        client: deps.authoringOutputClient,
        preview: deps.authoringOutputPreview,
      },
      mediaTools: actions.mediaToolsBinding(),
      actions: {
        bindMonitorReplace,
        acquireKeyboardGuard(onInvalidated) {
          const modal = acquireModalKeyboardGuard(deps.app);
          if (modal.status !== "ready") return modal;
          const canvas = acquireCanvasKeyboardGuard(deps.app, onInvalidated);
          if (canvas.status !== "ready") {
            modal.value.release();
            return canvas;
          }
          let released = false;
          return seamReady(
            Object.freeze({
              release() {
                if (released) return;
                released = true;
                canvas.value.release();
                modal.value.release();
              },
            }),
          );
        },
        mounted: actions.nleOverlayMounted,
        mountFailed: actions.nleOverlayMountFailed,
        close: actions.nleCloseOverlay,
        released: actions.nleOverlayReleased,
        resize: actions.nleResizeOverlay,
        selectPane: actions.nleSelectPane,
        setLayout: actions.nleSetLayout,
        startAuthoring: actions.nleStartAuthoring,
        timeline: actions.nleDispatchTimeline,
        clearImportHighlight: actions.nleClearImportHighlight,
        setTargetSeconds: actions.nleSetTargetSeconds,
        setPolicy: actions.nleSetSegmentationPolicy,
        prepareContext: actions.nlePrepareContext,
        openStoryboardReview: actions.nleOpenStoryboardReview,
        setStoryboardRows: actions.nleSetStoryboardRows,
        admitStoryboard: actions.nleAdmitStoryboard,
        propose: actions.nlePropose,
        approveAndImportPlan: actions.nleApproveAndImportPlan,
        createPlannedProject: actions.nleCreatePlannedProject,
        requestReadiness: actions.nleRequestReadiness,
        sequenceStartable: actions.nleSequenceStartable,
        startSequence: actions.nleStartSequence,
        detachSequence: actions.nleDetachSequence,
        reattachSequence: actions.nleReattachSequence,
        resumeSequence: actions.nleResumeSequence,
        cancelSequence: actions.nleCancelSequence,
        retrySegment: actions.nleRetrySegment,
        refreshSequence: actions.nleRefreshSequenceProjection,
        recoveryPointerPresent: actions.nleRecoveryPointerPresent,
        assembly: actions.nleAssembly,
        refreshProduction: actions.nleRefreshProduction,
      },
    };
  }

  function EntrySidebar({ focusClaim }: { focusClaim?: ViewFocusClaim }) {
    const selectedPage = deps.pageRegistry.getSnapshot().selected;
    const locale = deps.localeStore.getSnapshot().locale;
    const projection = actions.currentProjection();
    const currentProduction = actions.currentProductionProjection();
    const destination = deps.productionDestinations.describe(
      deps.appModeController.activeWorkflow(),
      currentProduction,
    );
    const productionDestination = {
      kind: destination.kind,
      ordinal: destination.ordinal,
      segmentCount:
        destination.kind === "project" &&
        currentProduction?.workspaceHandle === destination.workspaceHandle &&
        currentProduction.workspaceId === destination.workspaceId
          ? currentProduction.segments.length
          : 0,
    } as const;
    const nleSurface = session.nleWorkspace.surface.status;
    const overlayLive =
      nleSurface === "opening" ||
      nleSurface === "expanded" ||
      nleSurface === "closing";
    useLayoutEffect(() => {
      if (focusClaim !== undefined) consumeViewFocusClaim(focusClaim);
    }, [focusClaim]);
    useLayoutEffect(() => consumePageFocus(selectedPage), [selectedPage]);
    useLayoutEffect(() => {
      consumePendingSidebarFocus();
    }, [
      focusClaim,
      selectedPage,
      session.state.status,
      session.appModeDraft.activeStage,
    ]);
    const sidebar = (
      <H3Sidebar
        state={session.state}
        workspaceState={session.workspaceState}
        appMode={{
          capability: actions.currentAppModeCapability(),
          imageSources: deps.appModeController.imageSources(),
          mediaSources: deps.appModeController.mediaSources(),
          existingGraph:
            session.state.status === "interactive" &&
            session.state.existingGraph === true,
          durationResolution: deps.durationResolutionController.snapshot(),
          onResolveDuration: deps.durationResolutionController.request,
          onRetryDuration: deps.durationResolutionController.retry,
          admission: actions.currentAppModeAdmission(
            session.appModeDraft.taskMode,
          ),
          connect: deps.appModeController.connectCandidates(),
          productionDestination,
          onNewProject: actions.startNewProductionProject,
          onStart: async (inputs, options) => {
            try {
              if (options?.prepareOnly === true)
                await actions.prepareAppModeCanvas(inputs, options);
              else await actions.startAppMode(inputs, options);
            } catch {
              // startAppMode already reduced the safe UI failure state.
            }
          },
          onCancel: actions.cancelAppMode,
        }}
        onWorkspaceAction={actions.runWorkspaceAction}
        onWorkspaceFailure={actions.reportWorkspaceFailure}
        onChooseNative={actions.chooseNative}
        onRetry={actions.retryAppMode}
        onRetryOutputVerification={actions.retryManagedArtifactVerification}
        onEditAppModeSetup={editAppModeSetup}
        onCancelAppModeSetup={cancelAppModeSetup}
        locale={locale}
        pageRegistry={deps.pageRegistry.getSnapshot()}
        onSelectPage={selectPage}
        productionState={session.productionState}
        productionAccumulationNotice={session.productionAccumulationNotice}
        authoringState={session.authoringState}
        onAuthoringIntent={actions.runAuthoringIntent}
        onAuthoringMediaPreview={deps.authoringMediaPreviewClient.open}
        contextWorkspaceHandle={actions.currentProjection()?.workspace_id}
        onProductionIntent={actions.runProductionIntent}
        generationControls={actions.currentProductionGenerationControls()}
        productionProposalRows={
          actions.currentProductionProjection() === undefined
            ? []
            : deps.productionProposalDispatcher.rows(
                actions.currentProductionProjection()!,
              )
        }
        productionProposalCapacity={deps.productionProposalDispatcher.capacityStatus()}
        productionMediaPreview={session.productionMediaPreview}
        onProductionMediaPreview={actions.openProductionMediaPreview}
        onProductionMediaPreviewClose={actions.closeProductionMediaPreview}
        nle={nleSidebarBinding()}
        onProductionProposalRead={(segmentIds) => {
          const projection = actions.currentProductionProjection();
          if (projection !== undefined)
            return deps.productionProposalDispatcher.readSelected(
              projection,
              segmentIds,
            );
        }}
        onProductionProposalClose={(segmentId) =>
          deps.productionProposalDispatcher.closeRow(segmentId)
        }
        onProductionProposalAction={(segmentId, request) => {
          const projection = actions.currentProductionProjection();
          if (projection !== undefined)
            return deps.productionProposalDispatcher.action(
              projection,
              segmentId,
              request,
            );
        }}
        languageSettings={session.languageSettingsSnapshot}
        onLanguageWrite={(value) =>
          session.languageSettingsAdapter?.write(value) ??
          Promise.resolve(false)
        }
        providerSettings={session.providerSettingsProjection}
        assistedProposal={
          session.assistedProposal?.workspace_id ===
          actions.currentProjection()?.workspace_id
            ? session.assistedProposal
            : undefined
        }
        assistedBusy={session.assistedBusy}
        assistedFailure={session.assistedFailure}
        onProviderIntent={actions.runProviderIntent}
        providerRejection={session.providerSettingsRejection}
        providerBusy={session.providerSettingsBusy}
        providerBusyIntent={session.providerSettingsBusyIntent}
        onProviderCredentialClearerChange={actions.setProviderCredentialClearer}
        appModeDraft={session.appModeDraft}
        onAppModeDraftChange={actions.updateAppModeDraft}
        workspaceDraft={actions.currentWorkspaceDraft()}
        onWorkspaceDraftChange={actions.updateWorkspaceDraft}
        transactionTransparency={session.transactionTransparency}
        onTransactionIntent={handleTransactionIntent}
        semanticProposalReview={deps.productionProposalDispatcher.contextState()}
        onSemanticProposalOpen={() =>
          actions.runSemanticProposalReview({ action: "proposal_read" })
        }
        onSemanticProposalClose={actions.closeSemanticProposalReview}
        onSemanticProposalAction={actions.runSemanticProposalReview}
        buildProvenance={session.buildProvenance}
        retention={session.retention}
        mediaTools={actions.mediaToolsBinding()}
        diagnostics={{
          compose: () =>
            managedJournal.compose({
              packageVersion: H3_CONTEXT_VERSION,
              locale,
              hostFrontendVersion: projection?.resources.frontend_version,
              buildProvenance: session.buildProvenance,
              fingerprints: {
                report: projection?.report_fingerprint,
                prompt: projection?.prompt_fingerprint,
                basePrompt: projection?.base_prompt_fingerprint,
                currentPrompt: projection?.proposal.current_prompt_fingerprint,
              },
            }),
          writeText: async (payload) => {
            const clipboard = globalThis.navigator?.clipboard;
            if (typeof clipboard?.writeText !== "function")
              throw new Error("clipboard unavailable");
            await clipboard.writeText(payload);
          },
          notify: (outcome) => {
            const copy = sidebarCopy(locale).managedDiagnostics;
            const toast = probeToast(deps.app);
            if (toast.status === "ready")
              toast.value({
                severity: outcome === "copied" ? "success" : "info",
                summary: copy.action,
                detail: copy[outcome],
                life: 5000,
              });
          },
        }}
      />
    );
    return (
      <>
        {deps.h3SidebarProfilerObserver === undefined ? (
          sidebar
        ) : (
          // IMPORTANT: measure the Sidebar sibling itself. Subtracting overlay commits
          // from an ancestor Profiler hides Sidebar work committed alongside the overlay.
          <Profiler id="h3-sidebar" onRender={deps.h3SidebarProfilerObserver}>
            {sidebar}
          </Profiler>
        )}
        {overlayLive ? (
          deps.nleOverlayProfilerObserver === undefined ? (
            <NleOverlay
              key={session.nleWorkspace.surface.generation}
              binding={nleWorkspaceBinding(locale)}
            />
          ) : (
            // M25-16 performance-harness hook only: wrapping only happens when a harness
            // supplies the observer, so production's tree shape is unchanged (see ShellDeps).
            <Profiler
              id="nle-overlay"
              onRender={deps.nleOverlayProfilerObserver}
            >
              <NleOverlay
                key={session.nleWorkspace.surface.generation}
                binding={nleWorkspaceBinding(locale)}
              />
            </Profiler>
          )
        ) : null}
      </>
    );
  }

  function recordCurrentDiagnosticState(): void {
    if (session.diagnosticRun <= 0) return;
    const signature = diagnosticShellStateSignature(session.state);
    if (signature === session.lastDiagnosticState) return;
    session.lastDiagnosticState = signature;
    if (session.state.status === "working")
      managedJournal.recordState(session.diagnosticRun, {
        name: session.state.status,
        reason: session.state.phase,
      });
    else if (session.state.status === "interactive")
      managedJournal.recordState(session.diagnosticRun, {
        name: session.state.status,
        reason: session.state.reason,
      });
    else if (session.state.status === "error")
      managedJournal.recordState(session.diagnosticRun, {
        name: session.state.status,
        code: session.state.code,
      });
    else if (session.state.status === "host_unavailable")
      managedJournal.recordState(session.diagnosticRun, {
        name: session.state.status,
        reason: session.state.phase,
      });
    else
      managedJournal.recordState(session.diagnosticRun, {
        name: session.state.status,
      });
  }

  // M25-21 B3-D56: the monitor's replacement trigger, registered by `NleMonitor` while it is
  // mounted. `deriveNleMonitorBinding` builds a public asset manifest, so it is memoized on the
  // snapshot reference exactly as `NleWorkspace` memoizes it; `renderCurrent` runs on every shell
  // update and must not rebuild a 128-clip manifest each time.
  let monitorReplace: ((binding: VisualCompositionBinding) => void) | null =
    null;
  let derivedFromSnapshot: PublicCompositionSnapshot | undefined;
  let derivedMonitor: NleMonitorBinding | undefined;

  function bindMonitorReplace(
    replace: ((binding: VisualCompositionBinding) => void) | null,
  ): void {
    monitorReplace = replace;
  }

  /**
   * Start the monitor's replacement for the current accepted composition, before the shell
   * re-renders.
   *
   * IMPORTANT (M25-21 B3-D56): reacting to the binding prop inside `NleMonitor` can only start the
   * replacement *after* the commit that adopted the accepted revision, which makes the monitor's
   * `opening` state a second React commit for every accepted edit. Both states are truthful and
   * both are kept; publishing them together is what removes the surplus commit. The replacement
   * itself stays asynchronous, and the monitor's own effect remains as the fallback path, so
   * nothing here may throw into the render: a monitor that is not mounted simply has no trigger.
   */
  function beginMonitorReplacement(): void {
    if (monitorReplace === null) return;
    const authoring = session.authoringState;
    // IMPORTANT (M25-21 B3-D56): only an *adopted* state may start the replacement. Measured:
    // starting it from every shell render also started it on the `pending` render that carries the
    // fresh receipt, while the surface still had no selection -- the monitor tore down mid-edit
    // (selected clip emptied, composition `no_primary`, audio `silent`) and the following commits
    // put it all back, taking the workload from 67 attempts over the ceiling to 210 and the
    // maximum from 6 to 8. The monitor follows the state the user has been shown, never a state
    // the surface is still transitioning through.
    if (authoring.status !== "ready" && authoring.status !== "conflict") return;
    const history =
      "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
    const historyV2 =
      "timelineHistoryV2" in authoring
        ? authoring.timelineHistoryV2
        : undefined;
    const receipt =
      "lastTimelineReceipt" in authoring
        ? authoring.lastTimelineReceipt
        : undefined;
    const receiptV2 =
      "lastTimelineReceiptV2" in authoring
        ? authoring.lastTimelineReceiptV2
        : undefined;
    // The same rule `NleWorkspace` uses: an accepted transaction drops obsolete history before its
    // refresh, and its receipt already owns the new snapshot.
    const snapshot =
      historyV2 !== undefined
        ? (historyV2.renderSnapshot ?? undefined)
        : receiptV2 !== undefined
          ? (receiptV2.renderSnapshot ?? undefined)
          : (history?.snapshot ?? receipt?.snapshot);
    if (snapshot === undefined) return;
    if (snapshot !== derivedFromSnapshot || derivedMonitor === undefined) {
      derivedFromSnapshot = snapshot;
      derivedMonitor = deriveNleMonitorBinding(
        snapshot,
        actions.nleRuntimeDisposition(),
        deps.authoringMediaSourceLeaseClient,
      );
    }
    if (derivedMonitor.binding !== null) monitorReplace(derivedMonitor.binding);
  }

  function renderCurrent(): void {
    recordCurrentDiagnosticState();
    beginMonitorReplacement();
    if (session.container !== undefined)
      deps.performanceRecorder.measure("render", () =>
        deps.mount.update(() => (
          <EntrySidebar focusClaim={session.viewFocusClaim} />
        )),
      );
  }

  function captureViewFocus(): void {
    session.lastViewFocusKey = activeSidebarFocusKey();
    if (
      session.lastViewFocusKey !== undefined &&
      !session.lastViewFocusKey.startsWith("page-")
    )
      session.pageFocusKeys.set(
        deps.pageRegistry.getSnapshot().selected,
        session.lastViewFocusKey,
      );
  }

  return {
    EntrySidebar,
    activeSidebarFocusKey,
    beginViewFocusClaim,
    cancelAppModeSetup,
    captureViewFocus,
    consumePageFocus,
    consumePendingSidebarFocus,
    consumeViewFocusClaim,
    editAppModeSetup,
    ensureBuildProvenance,
    handleTransactionIntent,
    invalidateViewFocusClaim,
    recordCurrentDiagnosticState,
    recordPageLocalFocus,
    renderCurrent,
    selectPage,
  };
}
